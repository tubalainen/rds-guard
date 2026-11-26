"""SQLite event store for RDS Guard.

Stores traffic announcements, emergency alerts, and EON traffic
events. Traffic announcements are tracked as a single row through their
lifecycle (start → update → end).
"""

import json
import logging
import sqlite3
import threading
import time
import uuid

import geolocate
import trafikverket

log = logging.getLogger("rds-guard")

_DB_PATH = "/data/events.db"
_lock = threading.Lock()
_local = threading.local()

# Uplink outbox — off unless enable_outbox() is called (satellite mode).
# When on, every event mutation that the aggregator should hear about also
# writes an outbox row in the same transaction, so nothing is lost if the
# process dies between "event stored" and "event sent".
_outbox_enabled = False
_outbox_wake = None  # callable, invoked after a row is queued


def _conn():
    """Get a thread-local database connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(_DB_PATH)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")
        _local.conn.create_function("rg_contains", 4, _contains,
                                    deterministic=True)
    return _local.conn


def _contains(radiotext, transcription, station_ps, word):
    """SQL helper for search: does any text field contain word?

    Done in Python rather than with LIKE because RadioText is stored as
    JSON (å, ä, ö escaped) and LIKE only ignores case for ASCII letters.
    """
    needle = (word or "").casefold()
    if not needle:
        return 1
    parts = [transcription or "", station_ps or ""]
    try:
        rt = json.loads(radiotext) if radiotext else []
        parts.extend(str(r) for r in (rt if isinstance(rt, list) else [rt]))
    except (TypeError, ValueError):
        parts.append(radiotext or "")
    return 1 if any(needle in p.casefold() for p in parts) else 0


def init_db(db_path=None):
    """Create table and indexes if they don't exist."""
    global _DB_PATH
    if db_path:
        _DB_PATH = db_path
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            type        TEXT NOT NULL,
            severity    TEXT NOT NULL,
            state       TEXT NOT NULL,
            pi          TEXT NOT NULL,
            station_ps  TEXT,
            frequency   TEXT,
            radiotext   TEXT,
            data        TEXT NOT NULL,
            started_at  TEXT,
            ended_at    TEXT,
            duration_sec INTEGER,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        );

        CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
        CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
        CREATE INDEX IF NOT EXISTS idx_events_pi ON events(pi);
        CREATE INDEX IF NOT EXISTS idx_events_state ON events(state);
    """)
    conn.commit()

    # Schema migration: add audio/transcription columns (idempotent)
    _migrate_add_column(conn, "audio_path", "TEXT")
    _migrate_add_column(conn, "transcription", "TEXT")
    _migrate_add_column(conn, "transcription_status", "TEXT")
    _migrate_add_column(conn, "transcription_duration_sec", "REAL")
    # Globally unique event identity (used by the uplink to the aggregator)
    _migrate_add_column(conn, "event_uid", "TEXT")
    # Geolocation: the transcription with misheard words corrected (Claude
    # engine only) and how the message was placed on the map
    _migrate_add_column(conn, "transcription_corrected", "TEXT")
    _migrate_add_column(conn, "geo_status", "TEXT")
    _migrate_add_column(conn, "geo_engine", "TEXT")
    geolocate.init_tables(conn)
    trafikverket.init_tables(conn)
    conn.commit()

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS outbox (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT NOT NULL,
            event_id    INTEGER,
            event_uid   TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        );
    """)
    conn.commit()

    _init_notify_tables(conn)

    log.info("Event store initialized at %s", _DB_PATH)


def _migrate_add_column(conn, column_name, column_type):
    """Add a column to the events table if it doesn't exist."""
    try:
        conn.execute(
            f"ALTER TABLE events ADD COLUMN {column_name} {column_type}"
        )
        conn.commit()
        log.info("Migrated: added column '%s' to events table", column_name)
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            pass  # Column already exists
        else:
            raise


def insert_event(event_type, severity, state, pi, data_payload,
                 station_ps=None, frequency=None, radiotext=None,
                 started_at=None, ended_at=None):
    """Insert a new event row. Returns the row id."""
    rt_json = json.dumps(radiotext) if radiotext else json.dumps([])
    data_json = json.dumps(data_payload)
    event_uid = str(uuid.uuid4())
    with _lock:
        conn = _conn()
        cur = conn.execute(
            """INSERT INTO events
               (type, severity, state, pi, station_ps, frequency,
                radiotext, data, started_at, ended_at, event_uid)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_type, severity, state, pi, station_ps, frequency,
             rt_json, data_json, started_at, ended_at, event_uid)
        )
        event_id = cur.lastrowid
        _queue(conn, OUTBOX_EVENT, event_id, event_uid)
        conn.commit()
    _wake()
    return event_id


def update_event_radiotext(event_id, radiotext_list):
    """Append RadioText to an existing event (traffic update)."""
    rt_json = json.dumps(radiotext_list)
    with _lock:
        conn = _conn()
        conn.execute(
            "UPDATE events SET radiotext = ?, state = 'update' WHERE id = ?",
            (rt_json, event_id)
        )
        _queue(conn, OUTBOX_EVENT, event_id)
        conn.commit()
    _wake()


def end_event(event_id, ended_at, duration_sec, radiotext_list=None,
              data_payload=None):
    """Finalize a traffic announcement: set end state, duration."""
    with _lock:
        conn = _conn()
        params = [ended_at, duration_sec, event_id]
        sql = "UPDATE events SET state = 'end', ended_at = ?, duration_sec = ?"
        if radiotext_list is not None:
            sql += ", radiotext = ?"
            params = [ended_at, duration_sec, json.dumps(radiotext_list),
                      event_id]
        if data_payload is not None:
            sql += ", data = ?"
            params.insert(-1, json.dumps(data_payload))
        sql += " WHERE id = ?"
        conn.execute(sql, params)
        _queue(conn, OUTBOX_EVENT, event_id)
        conn.commit()
    _wake()


def query_events(event_type=None, since=None, limit=50, offset=0,
                 q=None, start=None, end=None, pi=None):
    """Query events with optional filters. Returns (rows, total_count).

    q: free text, every word must appear in the RadioText, transcription or
    station name. start/end: UTC bounds ('YYYY-MM-DDTHH:MM:SS') on when the
    broadcast started.
    """
    conn = _conn()
    where = []
    params = []

    if event_type:
        types = [t.strip() for t in event_type.split(",")]
        placeholders = ",".join("?" * len(types))
        where.append(f"type IN ({placeholders})")
        params.extend(types)

    if since:
        where.append("created_at > ?")
        params.append(since)

    if start:
        where.append("COALESCE(started_at, created_at) >= ?")
        params.append(start)
    if end:
        where.append("COALESCE(started_at, created_at) < ?")
        params.append(end)
    if pi:
        where.append("pi = ?")
        params.append(pi)
    for word in (q or "").split()[:8]:
        where.append("rg_contains(radiotext, COALESCE(transcription, '') || ' ' "
                     "|| COALESCE(transcription_corrected, ''), station_ps, ?)")
        params.append(word)

    where_clause = " WHERE " + " AND ".join(where) if where else ""

    # Total count
    count_row = conn.execute(
        f"SELECT COUNT(*) FROM events{where_clause}", params
    ).fetchone()
    total = count_row[0] if count_row else 0

    # Paginated results
    rows = conn.execute(
        f"SELECT * FROM events{where_clause} "
        "ORDER BY COALESCE(started_at, created_at) DESC, id DESC LIMIT ? OFFSET ?",
        params + [min(limit, 200), offset]
    ).fetchall()

    return [dict(r) for r in rows], total


def get_active_events():
    """Return events that are currently in progress (start or active state)."""
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM events WHERE state IN ('start', 'active', 'update') "
        "ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_event(event_id):
    """Return a single event row as a dict, or None if it does not exist."""
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM events WHERE id = ?", (event_id,)
    ).fetchone()
    return dict(row) if row else None


def get_active_traffic_event(pi):
    """Get the active traffic event for a specific PI code, if any."""
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM events WHERE pi = ? AND type = 'traffic' "
        "AND state IN ('start', 'update') ORDER BY created_at DESC LIMIT 1",
        (pi,)
    ).fetchone()
    return dict(row) if row else None


def update_event_audio(event_id, audio_path):
    """Set the audio file path for an event."""
    with _lock:
        conn = _conn()
        conn.execute(
            "UPDATE events SET audio_path = ? WHERE id = ?",
            (audio_path, event_id)
        )
        _queue(conn, OUTBOX_AUDIO, event_id)
        conn.commit()
    _wake()


def update_event_transcription(event_id, transcription, status="done",
                               duration_sec=None):
    """Set the transcription text, status, and optional duration for an event."""
    with _lock:
        conn = _conn()
        conn.execute(
            "UPDATE events SET transcription = ?, transcription_status = ?, "
            "transcription_duration_sec = ? WHERE id = ?",
            (transcription, status, duration_sec, event_id)
        )
        conn.commit()


def update_event_transcription_status(event_id, status):
    """Update just the transcription status (recording/saving/transcribing/error)."""
    with _lock:
        conn = _conn()
        conn.execute(
            "UPDATE events SET transcription_status = ? WHERE id = ?",
            (status, event_id)
        )
        conn.commit()


def delete_event(event_id):
    """Delete a single event by ID. Returns True if a row was deleted.

    Used when an announcement turns out to be too short to be real.  In
    satellite mode the deletion is forwarded, since the aggregator may
    already have been told that the event started.
    """
    with _lock:
        conn = _conn()
        event_uid = _event_uid(conn, event_id)
        cur = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        deleted = cur.rowcount > 0
        if deleted:
            _queue(conn, OUTBOX_DELETE, event_id, event_uid)
        conn.commit()
        if deleted:
            log.info("Deleted event %d", event_id)
    _wake()
    return deleted


def purge_old_events(days):
    """Delete events older than the given number of days.

    Also deletes associated audio files from disk.
    """
    import pathlib

    cutoff = time.strftime(
        "%Y-%m-%dT%H:%M:%S",
        time.gmtime(time.time() - days * 86400)
    )
    deleted = 0
    audio_paths = []
    with _lock:
        conn = _conn()
        # Collect audio paths before deleting rows
        rows = conn.execute(
            "SELECT audio_path FROM events WHERE created_at < ? "
            "AND audio_path IS NOT NULL",
            (cutoff,)
        ).fetchall()
        audio_paths = [r[0] for r in rows if r[0]]

        cur = conn.execute(
            "DELETE FROM events WHERE created_at < ?", (cutoff,)
        )
        geolocate.purge_incidents(conn, cutoff)
        trv.purge(cutoff)
        conn.commit()
        deleted = cur.rowcount
        if deleted > 0:
            log.info("Purged %d events older than %d days", deleted, days)

    # Delete audio files outside the lock
    if audio_paths:
        import config as cfg
        audio_dir = pathlib.Path(cfg.AUDIO_DIR)
        for audio_rel in audio_paths:
            base = audio_rel.rsplit(".", 1)[0] if "." in audio_rel else audio_rel
            for ext in (".ogg", ".wav"):
                path = audio_dir / (base + ext)
                try:
                    if path.exists():
                        path.unlink()
                except OSError:
                    pass

    return deleted


def close_stale_events():
    """Mark any leftover active events as ended on startup.

    If the app was restarted mid-announcement, traffic events with
    state 'start' or 'update' would appear as 'In progress' forever.
    This marks them as ended with a note that the app restarted.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    with _lock:
        conn = _conn()
        stale = conn.execute(
            "SELECT id, event_uid FROM events "
            "WHERE state IN ('start', 'update', 'active') AND ended_at IS NULL"
        ).fetchall()
        cur = conn.execute(
            "UPDATE events SET state = 'end', ended_at = ? "
            "WHERE state IN ('start', 'update', 'active') AND ended_at IS NULL",
            (now,)
        )
        for row in stale:
            _queue(conn, OUTBOX_EVENT, row["id"], row["event_uid"])
        conn.commit()
        if cur.rowcount > 0:
            log.info("Closed %d stale active events from previous run", cur.rowcount)
        closed = cur.rowcount
    _wake()
    return closed


def delete_all_events():
    """Clear all events from the database."""
    with _lock:
        conn = _conn()
        cur = conn.execute("DELETE FROM events")
        conn.commit()
        log.info("Deleted all %d events", cur.rowcount)
        return cur.rowcount


# ---------------------------------------------------------------------------
# Uplink outbox (satellite mode)
# ---------------------------------------------------------------------------
#
# Only three things are forwarded to an aggregator: the event itself (as a
# full snapshot, re-read at send time), its audio file, and the deletion of
# an event that was discarded as too short.  Transcriptions are deliberately
# not forwarded, and neither is local housekeeping (retention purge, the
# "clear all" button) — the aggregator keeps its own copy and retention.

OUTBOX_EVENT = "event"
OUTBOX_AUDIO = "audio"
OUTBOX_DELETE = "delete"


def enable_outbox(on_queued=None):
    """Start recording event mutations in the outbox.

    on_queued: optional callable invoked (outside the DB lock) whenever
    something may have been queued, so the uplink worker can wake up.
    """
    global _outbox_enabled, _outbox_wake
    _outbox_enabled = True
    _outbox_wake = on_queued


def disable_outbox():
    """Stop recording mutations (used by tests)."""
    global _outbox_enabled, _outbox_wake
    _outbox_enabled = False
    _outbox_wake = None


def _event_uid(conn, event_id):
    row = conn.execute(
        "SELECT event_uid FROM events WHERE id = ?", (event_id,)
    ).fetchone()
    return row["event_uid"] if row else None


def _queue(conn, kind, event_id, event_uid=None):
    """Add an outbox row inside the caller's transaction (lock held)."""
    if not _outbox_enabled:
        return
    if event_uid is None:
        event_uid = _event_uid(conn, event_id)
    if not event_uid:
        # Row predates event_uid (created before the upgrade) or is gone —
        # the aggregator has never heard of it, so there is nothing to send.
        return
    conn.execute(
        "INSERT INTO outbox (kind, event_id, event_uid) VALUES (?, ?, ?)",
        (kind, event_id, event_uid)
    )


def _wake():
    wake = _outbox_wake
    if _outbox_enabled and wake is not None:
        try:
            wake()
        except Exception:
            log.debug("Outbox wake callback failed", exc_info=True)


def outbox_head():
    """Return the oldest outbox row as a dict, or None if the queue is empty."""
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM outbox ORDER BY id ASC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def outbox_latest_event_rev(event_id):
    """Highest queued 'event' outbox id for this event (0 if none).

    A snapshot of the event read *after* this call reflects every mutation
    up to and including that outbox row, so the id doubles as a revision
    number and as the acknowledgement watermark.
    """
    conn = _conn()
    row = conn.execute(
        "SELECT MAX(id) FROM outbox WHERE kind = ? AND event_id = ?",
        (OUTBOX_EVENT, event_id)
    ).fetchone()
    return row[0] or 0


def outbox_ack(outbox_id):
    """Remove a single outbox row once it has been delivered (or dropped)."""
    with _lock:
        conn = _conn()
        conn.execute("DELETE FROM outbox WHERE id = ?", (outbox_id,))
        conn.commit()


def outbox_ack_event(event_id, up_to_rev):
    """Remove all queued snapshots of an event up to a revision.

    Several mutations of the same event collapse into one delivery.
    """
    with _lock:
        conn = _conn()
        conn.execute(
            "DELETE FROM outbox WHERE kind = ? AND event_id = ? AND id <= ?",
            (OUTBOX_EVENT, event_id, up_to_rev)
        )
        conn.commit()


def outbox_depth():
    """Number of rows waiting to be delivered."""
    conn = _conn()
    row = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()
    return row[0] if row else 0


def outbox_purge(max_age_hours):
    """Drop queued rows older than max_age_hours. Returns the number dropped."""
    cutoff = time.strftime(
        "%Y-%m-%dT%H:%M:%S",
        time.gmtime(time.time() - max_age_hours * 3600)
    )
    with _lock:
        conn = _conn()
        cur = conn.execute("DELETE FROM outbox WHERE created_at < ?", (cutoff,))
        conn.commit()
        if cur.rowcount > 0:
            log.warning("Uplink: dropped %d queued items older than %dh",
                        cur.rowcount, max_age_hours)
        return cur.rowcount


# ---------------------------------------------------------------------------
# Push notification rules and their log
# ---------------------------------------------------------------------------
#
# A rule is stored as JSON (validated by notify_rules.validate before it gets
# here). The log keeps one row per message sent, or a single 'no_match' row
# when an event matched no rule, so the archive can show "push skickad" or
# "ingen push" on every event.

NOTIFY_SENT = "sent"
NOTIFY_FAILED = "failed"
NOTIFY_MQTT_OFF = "mqtt_off"
NOTIFY_NO_MATCH = "no_match"


def _init_notify_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key         TEXT PRIMARY KEY,
            value       TEXT
        );

        CREATE TABLE IF NOT EXISTS notify_rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            data        TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        );

        CREATE TABLE IF NOT EXISTS notify_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    INTEGER,
            rule_id     INTEGER,
            rule_name   TEXT,
            topic       TEXT,
            title       TEXT,
            status      TEXT NOT NULL,
            test        INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        );

        CREATE INDEX IF NOT EXISTS idx_notify_log_event ON notify_log(event_id);
        CREATE INDEX IF NOT EXISTS idx_notify_log_created ON notify_log(created_at);
    """)
    conn.commit()


def seed_rules_once(defaults):
    """Insert the default rules the first time the store is used."""
    with _lock:
        conn = _conn()
        done = conn.execute(
            "SELECT value FROM settings WHERE key = 'notify_seeded'").fetchone()
        if done:
            return False
        for rule in defaults:
            conn.execute("INSERT INTO notify_rules (data) VALUES (?)",
                         (json.dumps(rule),))
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('notify_seeded', '1')")
        conn.commit()
    return True


def _rule_dict(row):
    rule = json.loads(row["data"])
    rule["id"] = row["id"]
    rule["created_at"] = row["created_at"]
    rule["updated_at"] = row["updated_at"]
    return rule


def list_rules():
    conn = _conn()
    rows = conn.execute("SELECT * FROM notify_rules ORDER BY id").fetchall()
    return [_rule_dict(r) for r in rows]


def get_rule(rule_id):
    conn = _conn()
    row = conn.execute("SELECT * FROM notify_rules WHERE id = ?",
                       (rule_id,)).fetchone()
    return _rule_dict(row) if row else None


def create_rule(rule):
    with _lock:
        conn = _conn()
        cur = conn.execute("INSERT INTO notify_rules (data) VALUES (?)",
                           (json.dumps(rule),))
        conn.commit()
        rule_id = cur.lastrowid
    return get_rule(rule_id)


def update_rule(rule_id, rule):
    with _lock:
        conn = _conn()
        cur = conn.execute(
            "UPDATE notify_rules SET data = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE id = ?",
            (json.dumps(rule), rule_id))
        conn.commit()
        if cur.rowcount == 0:
            return None
    return get_rule(rule_id)


def delete_rule(rule_id):
    with _lock:
        conn = _conn()
        cur = conn.execute("DELETE FROM notify_rules WHERE id = ?", (rule_id,))
        conn.commit()
        return cur.rowcount > 0


def log_notification(event_id, status, rule=None, topic=None, title=None,
                     test=False):
    with _lock:
        conn = _conn()
        conn.execute(
            "INSERT INTO notify_log (event_id, rule_id, rule_name, topic, "
            "title, status, test) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, rule.get("id") if rule else None,
             rule.get("name") if rule else None, topic, title, status,
             1 if test else 0))
        conn.commit()


def notify_log_recent(limit=30):
    conn = _conn()
    rows = conn.execute(
        "SELECT l.*, e.station_ps, e.started_at, e.radiotext "
        "FROM notify_log l LEFT JOIN events e ON e.id = l.event_id "
        "WHERE l.status != ? ORDER BY l.id DESC LIMIT ?",
        (NOTIFY_NO_MATCH, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["test"] = bool(d["test"])
        try:
            d["radiotext"] = json.loads(d["radiotext"]) if d["radiotext"] else []
        except (TypeError, ValueError):
            d["radiotext"] = []
        out.append(d)
    return out


def notify_for_events(event_ids):
    """event id -> list of log rows (oldest first) for those events."""
    ids = [int(i) for i in event_ids]
    if not ids:
        return {}
    conn = _conn()
    out = {}
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        rows = conn.execute(
            "SELECT event_id, rule_id, rule_name, topic, status, created_at "
            f"FROM notify_log WHERE test = 0 AND event_id IN "
            f"({','.join('?' * len(part))}) ORDER BY id", part).fetchall()
        for r in rows:
            out.setdefault(r["event_id"], []).append(
                {k: r[k] for k in ("rule_id", "rule_name", "topic",
                                   "status", "created_at")})
    return out


def purge_notify_log(days):
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S",
                           time.gmtime(time.time() - days * 86400))
    with _lock:
        conn = _conn()
        conn.execute("DELETE FROM notify_log WHERE created_at < ?", (cutoff,))
        conn.commit()


def calendar_events(since):
    """Light rows for the archive calendar and statistics (newest first)."""
    conn = _conn()
    rows = conn.execute(
        "SELECT id, type, pi, station_ps, started_at, created_at, duration_sec "
        "FROM events WHERE created_at >= ? "
        "ORDER BY COALESCE(started_at, created_at) DESC, id DESC",
        (since,)).fetchall()
    return [dict(r) for r in rows]


def events_for_preview(since):
    """Rows with the text fields a rule looks at, newest first."""
    conn = _conn()
    rows = conn.execute(
        "SELECT id, type, pi, station_ps, frequency, radiotext, transcription, "
        "started_at, created_at, duration_sec FROM events "
        "WHERE created_at >= ? AND state NOT IN ('start', 'update', 'active') "
        "ORDER BY COALESCE(started_at, created_at) DESC, id DESC",
        (since,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["radiotext"] = json.loads(d["radiotext"]) if d["radiotext"] else []
        except (TypeError, ValueError):
            d["radiotext"] = []
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Geolocation: incidents on the map
# ---------------------------------------------------------------------------

_ISO = "%Y-%m-%dT%H:%M:%S"


def _iso_hours_ago(hours, at=None):
    """UTC timestamp hours before at (an ISO timestamp; default now)."""
    base = _epoch(at) if at else time.time()
    return time.strftime(_ISO, time.gmtime(base - hours * 3600))


def _epoch(iso):
    import calendar
    return calendar.timegm(time.strptime(_norm_ts(iso), _ISO))


def _norm_ts(ts):
    """'2026-11-24 08:15:02Z' -> '2026-11-24T08:15:02' (this table's form)."""
    return str(ts).replace(" ", "T")[:19]


def save_geolocation(event_id, result, status, at=None):
    """Store how a message was placed: corrected text and its incidents.

    result is a geolocate.GeoResult or None; status is "done", "none" (no
    place found), "error", "no_location" or "off". at is when the message
    was broadcast, for older messages placed afterwards (default now).
    """
    now = _norm_ts(at) if at else time.strftime(_ISO, time.gmtime())
    with _lock:
        conn = _conn()
        ids = []
        if result is not None and result.placements:
            ids = geolocate.merge_placements(
                conn, result.placements, event_id, now,
                _iso_hours_ago(geolocate.INCIDENT_HOURS, now), result.engine)
            row = conn.execute("SELECT transcription FROM events WHERE id = ?",
                               (event_id,)).fetchone()
            text = result.corrected_text or (row[0] if row else "") or ""
            trafikverket.link_incidents(conn, ids, now, text)
        conn.execute(
            "UPDATE events SET transcription_corrected = ?, geo_status = ?, "
            "geo_engine = ? WHERE id = ?",
            (result.corrected_text if result else None, status,
             result.engine if result else None, event_id))
        conn.commit()
    return ids


def open_incidents(hours=geolocate.LINK_HOURS, at=None):
    """Incidents a new message may be about (at a past time at, default now)."""
    return geolocate.open_incidents(
        _conn(), _iso_hours_ago(hours, at), _norm_ts(at) if at else None)


def list_incidents(hours=geolocate.INCIDENT_HOURS):
    """Incidents of the last hours; hours=None: all that are stored."""
    return geolocate.list_incidents(
        _conn(), _iso_hours_ago(hours) if hours else None)


def get_incident(incident_id):
    """One incident with its broadcasts, or None."""
    return geolocate.get_incident(_conn(), incident_id)


def event_briefs(event_ids):
    """{id: {id, started_at, station_ps, audio_path}} for the given events,
    in one query per 500 ids (for incident timelines)."""
    ids = sorted({int(i) for i in event_ids if str(i).isdigit()})
    out = {}
    for n in range(0, len(ids), 500):
        part = ids[n:n + 500]
        for r in _conn().execute(
                "SELECT id, started_at, station_ps, audio_path FROM events "
                f"WHERE id IN ({','.join('?' * len(part))})", part):
            out[r["id"]] = dict(r)
    return out


_GEO_PENDING_WHERE = (
    "transcription IS NOT NULL AND TRIM(transcription) != '' "
    "AND transcription_status = 'done' AND (geo_status IS NULL OR geo_status IN ("
    + ",".join("'%s'" % s for s in geolocate.PENDING_STATUSES)
    + "))")


def geo_pending():
    """Transcribed messages never placed on the map, oldest first."""
    rows = _conn().execute(
        "SELECT id, transcription, radiotext, "
        "COALESCE(started_at, created_at) AS at FROM events WHERE "
        + _GEO_PENDING_WHERE + " ORDER BY COALESCE(started_at, created_at), id"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["radiotext"] = json.loads(d["radiotext"]) if d["radiotext"] else []
        except (TypeError, ValueError):
            d["radiotext"] = []
        out.append(d)
    return out


def geo_pending_count():
    return _conn().execute(
        "SELECT COUNT(*) FROM events WHERE " + _GEO_PENDING_WHERE).fetchone()[0]


def incidents_for_events(event_ids):
    return geolocate.incidents_for_events(_conn(), event_ids)


# Trafikverket's deviations (trafikverket.py), in this database
trv = trafikverket.TrvStore(lambda: _conn(), _lock)


def trv_relink(dev_ids):
    """Link new or changed deviations to the radio's incidents and update
    the linked ones. Returns (new links, updated incident ids)."""
    now = time.strftime(_ISO, time.gmtime())
    with _lock:
        conn = _conn()
        out = trafikverket.relink_deviations(
            conn, dev_ids, now, _iso_hours_ago(geolocate.INCIDENT_HOURS))
        conn.commit()
    return out


def incident(inc_id):
    row = _conn().execute("SELECT * FROM incidents WHERE id = ?",
                          (inc_id,)).fetchone()
    return dict(row) if row else None


def incidents_for_deviation(dev_id):
    return [dict(r) for r in _conn().execute(
        "SELECT * FROM incidents WHERE trv_id = ?", (dev_id,))]


def incident_event_keys(inc_id):
    return [r[0] for r in _conn().execute(
        "SELECT event_key FROM incident_events WHERE incident_id = ? "
        "ORDER BY seen_at", (inc_id,))]


def rules_sent_for(event_ids):
    """Ids of the rules a push went out for, for these events."""
    ids = [int(e) for e in event_ids if str(e).isdigit()]
    if not ids:
        return []
    return [r[0] for r in _conn().execute(
        "SELECT DISTINCT rule_id FROM notify_log WHERE rule_id IS NOT NULL "
        f"AND test = 0 AND status = ? AND event_id IN ({','.join('?' * len(ids))})",
        [NOTIFY_SENT, *ids])]


def recent_place_names(days=90, limit=60):
    return geolocate.recent_names(_conn(), _iso_hours_ago(days * 24), limit)
