"""The satellite's own small database (/data/satellite.db).

Holds only what the satellite needs to get its broadcasts to the
aggregator: the events it detected (each traffic announcement or emergency
broadcast, one row from start to end), the uplink outbox, a few settings
(its join identity) and the receivers' gain calibration.

Everything else (transcriptions, the map, push rules, the archive) lives on
the aggregator. A database from before v1.0 (/data/events.db) is left
untouched for the aggregator's import; what it still owed the aggregator is
moved here once (migrate_from).
"""

import json
import logging
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("rds-guard")

_DB_PATH = "/data/satellite.db"
_lock = threading.Lock()
_local = threading.local()

# The uplink outbox: every event change the aggregator should hear about
# writes a row in the same transaction, so nothing is lost if the process
# dies between "event stored" and "event sent". On unless disabled (tests).
_outbox_enabled = False
_outbox_wake = None  # callable, invoked after a row is queued

OUTBOX_EVENT = "event"
OUTBOX_AUDIO = "audio"
OUTBOX_DELETE = "delete"


def _ts(epoch=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S",
                         time.gmtime(time.time() if epoch is None else epoch))


def _conn():
    """Get a thread-local database connection."""
    if getattr(_local, "conn", None) is None or getattr(_local, "path", None) != _DB_PATH:
        _local.conn = sqlite3.connect(_DB_PATH)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")
        _local.path = _DB_PATH
    return _local.conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid   TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL,
    state       TEXT NOT NULL,
    pi          TEXT NOT NULL,
    station_ps  TEXT,
    frequency   TEXT,
    receiver    TEXT,
    radiotext   TEXT NOT NULL DEFAULT '[]',
    data        TEXT NOT NULL DEFAULT '{}',
    started_at  TEXT,
    ended_at    TEXT,
    duration_sec INTEGER,
    audio_path  TEXT,
    audio_delivered_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);
CREATE INDEX IF NOT EXISTS idx_events_state ON events(state);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    event_id    INTEGER,
    event_uid   TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT
);

CREATE TABLE IF NOT EXISTS receiver_calibration (
    serial      TEXT PRIMARY KEY,
    frequencies TEXT NOT NULL,
    gain        REAL,
    ppm         INTEGER,
    result      TEXT,
    reason      TEXT,
    calibrated_at TEXT NOT NULL
);
"""


def init_db(db_path=None):
    """Create the tables if they don't exist."""
    global _DB_PATH
    if db_path:
        _DB_PATH = db_path
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = _conn()
    conn.executescript(SCHEMA)
    conn.commit()
    log.info("Satellite database at %s", _DB_PATH)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def insert_event(event_type, severity, state, pi, data_payload,
                 station_ps=None, frequency=None, radiotext=None,
                 started_at=None, ended_at=None, receiver=None):
    """Insert a new event row (and queue it). Returns the row id.

    severity is accepted for older callers and not stored.
    """
    event_uid = str(uuid.uuid4())
    with _lock:
        conn = _conn()
        cur = conn.execute(
            "INSERT INTO events (event_uid, type, state, pi, station_ps, frequency, "
            "receiver, radiotext, data, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_uid, event_type, state, pi, station_ps, frequency, receiver,
             json.dumps(radiotext or [], ensure_ascii=False),
             json.dumps(data_payload or {}, ensure_ascii=False),
             started_at, ended_at))
        event_id = cur.lastrowid
        _queue(conn, OUTBOX_EVENT, event_id, event_uid)
        conn.commit()
    _wake()
    return event_id


def update_event_radiotext(event_id, radiotext_list):
    """RadioText changed during a traffic announcement."""
    with _lock:
        conn = _conn()
        conn.execute("UPDATE events SET radiotext = ?, state = 'update' WHERE id = ?",
                     (json.dumps(radiotext_list, ensure_ascii=False), event_id))
        _queue(conn, OUTBOX_EVENT, event_id)
        conn.commit()
    _wake()


def end_event(event_id, ended_at, duration_sec, radiotext_list=None,
              data_payload=None):
    """The broadcast ended."""
    sets, params = ["state = 'end'", "ended_at = ?", "duration_sec = ?"], [ended_at, duration_sec]
    if radiotext_list is not None:
        sets.append("radiotext = ?")
        params.append(json.dumps(radiotext_list, ensure_ascii=False))
    if data_payload is not None:
        sets.append("data = ?")
        params.append(json.dumps(data_payload, ensure_ascii=False))
    with _lock:
        conn = _conn()
        conn.execute(f"UPDATE events SET {', '.join(sets)} WHERE id = ?",
                     params + [event_id])
        _queue(conn, OUTBOX_EVENT, event_id)
        conn.commit()
    _wake()


def update_event_audio(event_id, audio_path):
    """The recording is saved: queue its upload."""
    with _lock:
        conn = _conn()
        conn.execute("UPDATE events SET audio_path = ? WHERE id = ?",
                     (audio_path, event_id))
        _queue(conn, OUTBOX_AUDIO, event_id)
        conn.commit()
    _wake()


def mark_audio_delivered(event_id):
    with _lock:
        conn = _conn()
        conn.execute("UPDATE events SET audio_delivered_at = ? WHERE id = ?",
                     (_ts(), event_id))
        conn.commit()


def delete_event(event_id):
    """Delete an event that turned out too short to be real, and tell the
    aggregator (it may have heard of its start). Returns True if deleted."""
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


def get_event(event_id):
    row = _conn().execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return dict(row) if row else None


def query_events(limit=50):
    """The latest events, newest first. Returns (rows, total)."""
    conn = _conn()
    total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?",
                        (int(limit),)).fetchall()
    return [dict(r) for r in rows], total


def close_stale_events():
    """End events left open by a previous run (restarted mid-broadcast)."""
    now = _ts()
    with _lock:
        conn = _conn()
        stale = conn.execute(
            "SELECT id, event_uid FROM events "
            "WHERE state IN ('start', 'update', 'active') AND ended_at IS NULL").fetchall()
        conn.execute(
            "UPDATE events SET state = 'end', ended_at = ? "
            "WHERE state IN ('start', 'update', 'active') AND ended_at IS NULL", (now,))
        for row in stale:
            _queue(conn, OUTBOX_EVENT, row["id"], row["event_uid"])
        conn.commit()
    if stale:
        log.info("Closed %d events left open by the previous run", len(stale))
    _wake()
    return len(stale)


def purge(keep_days, spool_dir):
    """Forget what the aggregator has had for keep_days: event rows with
    nothing queued, and recordings it acknowledged. Recordings not yet
    delivered are kept until the outbox gives them up."""
    cutoff = _ts(time.time() - keep_days * 86400)
    with _lock:
        conn = _conn()
        rows = conn.execute(
            "SELECT id, audio_path, audio_delivered_at FROM events e "
            "WHERE created_at < ? AND NOT EXISTS "
            "(SELECT 1 FROM outbox o WHERE o.event_id = e.id)", (cutoff,)).fetchall()
        files = [r["audio_path"] for r in rows if r["audio_path"]]
        conn.executemany("DELETE FROM events WHERE id = ?", [(r["id"],) for r in rows])
        conn.commit()
    for name in files:
        try:
            (Path(spool_dir) / Path(name).name).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Could not remove %s: %s", name, e)
    if rows:
        log.info("Forgot %d delivered events older than %d days", len(rows), keep_days)
    return len(rows)


# ---------------------------------------------------------------------------
# Settings (the uplink's identity) and receiver calibration
# ---------------------------------------------------------------------------

def get_setting(key, default=None):
    row = _conn().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with _lock:
        conn = _conn()
        if value is None:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                         (key, str(value)))
        conn.commit()


def get_calibration(serial):
    row = _conn().execute("SELECT * FROM receiver_calibration WHERE serial = ?",
                          (serial,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["frequencies"] = json.loads(d["frequencies"])
    d["result"] = json.loads(d["result"]) if d.get("result") else None
    return d


def save_calibration(serial, frequencies, gain, ppm, result=None, reason=None, at=None):
    with _lock:
        conn = _conn()
        conn.execute(
            "INSERT OR REPLACE INTO receiver_calibration (serial, frequencies, gain, "
            "ppm, result, reason, calibrated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (serial, json.dumps(sorted(frequencies)), gain, ppm,
             json.dumps(result, ensure_ascii=False) if result is not None else None,
             reason, _ts(at)))
        conn.commit()


# ---------------------------------------------------------------------------
# Moving from a database of before v1.0 (events.db)
# ---------------------------------------------------------------------------

_IDENTITY = ("uplink_token", "uplink_join_id", "uplink_joined")


def migrate_from(old_db, old_audio_dir, spool_dir):
    """Bring over, once, what a pre-v1.0 satellite still owed the aggregator:
    its join identity, and the events (with their recordings) whose outbox
    rows were not delivered. The old database is only read; the aggregator's
    import can still use it. Returns the number of events moved."""
    if get_setting("migrated_from_events_db") or not Path(old_db).is_file():
        return 0
    old = sqlite3.connect(Path(old_db).resolve().as_uri() + "?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    moved = 0
    try:
        tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "settings" in tables:
            for key in _IDENTITY:
                row = old.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
                if row and row[0] and get_setting(key) is None:
                    set_setting(key, row[0])
        if "outbox" in tables:
            pending = old.execute(
                "SELECT kind, event_id, event_uid FROM outbox ORDER BY id").fetchall()
            ids = {}
            for item in pending:
                ev = old.execute("SELECT * FROM events WHERE id = ?",
                                 (item["event_id"],)).fetchone()
                with _lock:
                    conn = _conn()
                    if item["kind"] == OUTBOX_DELETE or ev is None:
                        conn.execute("INSERT INTO outbox (kind, event_id, event_uid) "
                                     "VALUES (?, NULL, ?)", (OUTBOX_DELETE, item["event_uid"]))
                        conn.commit()
                        continue
                    ev = dict(ev)
                    if ev["id"] not in ids:
                        audio = None
                        if ev.get("audio_path"):
                            name = Path(ev["audio_path"]).name
                            src = Path(old_audio_dir) / name
                            if src.is_file():
                                Path(spool_dir).mkdir(parents=True, exist_ok=True)
                                shutil.copyfile(src, Path(spool_dir) / name)
                                audio = name
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO events (event_uid, type, state, pi, "
                            "station_ps, frequency, radiotext, data, started_at, "
                            "ended_at, duration_sec, audio_path, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (ev["event_uid"], ev["type"], ev["state"], ev["pi"],
                             ev.get("station_ps"), ev.get("frequency"),
                             ev.get("radiotext") or "[]", ev.get("data") or "{}",
                             ev.get("started_at"), ev.get("ended_at"),
                             ev.get("duration_sec"), audio,
                             ev.get("created_at") or _ts()))
                        ids[ev["id"]] = cur.lastrowid or conn.execute(
                            "SELECT id FROM events WHERE event_uid = ?",
                            (ev["event_uid"],)).fetchone()[0]
                        moved += 1
                    conn.execute("INSERT INTO outbox (kind, event_id, event_uid) VALUES (?, ?, ?)",
                                 (item["kind"], ids[ev["id"]], ev["event_uid"]))
                    conn.commit()
    finally:
        old.close()
    set_setting("migrated_from_events_db", _ts())
    log.info("Moved from %s: join identity%s", old_db,
             f" and {moved} events still owed to the aggregator" if moved else "")
    return moved


# ---------------------------------------------------------------------------
# Uplink outbox
# ---------------------------------------------------------------------------

def enable_outbox(on_queued=None):
    """Record event changes in the outbox. on_queued is called (outside the
    lock) whenever something may have been queued."""
    global _outbox_enabled, _outbox_wake
    _outbox_enabled = True
    _outbox_wake = on_queued


def disable_outbox():
    """Stop recording changes (tests)."""
    global _outbox_enabled, _outbox_wake
    _outbox_enabled = False
    _outbox_wake = None


def _event_uid(conn, event_id):
    row = conn.execute("SELECT event_uid FROM events WHERE id = ?", (event_id,)).fetchone()
    return row["event_uid"] if row else None


def _queue(conn, kind, event_id, event_uid=None):
    """Add an outbox row inside the caller's transaction (lock held)."""
    if not _outbox_enabled:
        return
    if event_uid is None:
        event_uid = _event_uid(conn, event_id)
    if not event_uid:
        return
    conn.execute("INSERT INTO outbox (kind, event_id, event_uid) VALUES (?, ?, ?)",
                 (kind, event_id, event_uid))


def _wake():
    wake = _outbox_wake
    if _outbox_enabled and wake is not None:
        try:
            wake()
        except Exception:
            log.debug("Outbox wake callback failed", exc_info=True)


def outbox_head(kinds=None):
    """The oldest outbox row (of the given kinds), or None."""
    conn = _conn()
    if kinds:
        kinds = list(kinds)
        row = conn.execute(
            f"SELECT * FROM outbox WHERE kind IN ({','.join('?' * len(kinds))}) "
            "ORDER BY id ASC LIMIT 1", kinds).fetchone()
    else:
        row = conn.execute("SELECT * FROM outbox ORDER BY id ASC LIMIT 1").fetchone()
    return dict(row) if row else None


def outbox_event_pending(event_id, before_id):
    """Is a snapshot or delete of this event queued before before_id? The
    audio of an event waits for that."""
    row = _conn().execute(
        "SELECT 1 FROM outbox WHERE event_id = ? AND kind IN (?, ?) AND id < ? LIMIT 1",
        (event_id, OUTBOX_EVENT, OUTBOX_DELETE, before_id)).fetchone()
    return row is not None


def outbox_latest_event_rev(event_id):
    """Highest queued 'event' row id for this event (0 if none): read before
    the event, it is the snapshot's revision and acknowledgement mark."""
    row = _conn().execute("SELECT MAX(id) FROM outbox WHERE kind = ? AND event_id = ?",
                          (OUTBOX_EVENT, event_id)).fetchone()
    return row[0] or 0


def outbox_ack(outbox_id):
    with _lock:
        conn = _conn()
        conn.execute("DELETE FROM outbox WHERE id = ?", (outbox_id,))
        conn.commit()


def outbox_ack_event(event_id, up_to_rev):
    """Remove the queued snapshots of an event up to a revision (several
    changes are delivered as one snapshot)."""
    with _lock:
        conn = _conn()
        conn.execute("DELETE FROM outbox WHERE kind = ? AND event_id = ? AND id <= ?",
                     (OUTBOX_EVENT, event_id, up_to_rev))
        conn.commit()


def outbox_depth():
    row = _conn().execute("SELECT COUNT(*) FROM outbox").fetchone()
    return row[0] if row else 0


def outbox_purge(max_age_hours):
    """Drop queued rows older than max_age_hours."""
    cutoff = _ts(time.time() - max_age_hours * 3600)
    with _lock:
        conn = _conn()
        cur = conn.execute("DELETE FROM outbox WHERE created_at < ?", (cutoff,))
        conn.commit()
    if cur.rowcount > 0:
        log.warning("Uplink: dropped %d queued items older than %dh",
                    cur.rowcount, max_age_hours)
    return cur.rowcount
