"""SQLite store for the aggregator: nodes and the events they report."""

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import threading
import time

log = logging.getLogger("rds-aggregator")

_DB_PATH = None
_lock = threading.Lock()
_local = threading.local()

ACTIVE_STATES = ("start", "update", "active")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def iso_ago(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() - seconds))


def _conn():
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != _DB_PATH:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
        _local.path = _DB_PATH
    return conn


def init_db(db_path):
    """Create tables and indexes if they don't exist."""
    global _DB_PATH
    _DB_PATH = db_path
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL UNIQUE,
            token_hash      TEXT NOT NULL UNIQUE,
            token_prefix    TEXT NOT NULL,
            enabled         INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT NOT NULL,
            last_seen_at    TEXT,
            last_heartbeat_at TEXT,
            reported_name   TEXT,
            version         TEXT,
            status          TEXT,
            clock_offset_sec REAL
        );

        CREATE TABLE IF NOT EXISTS events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uid       TEXT NOT NULL UNIQUE,
            node_id         INTEGER NOT NULL
                            REFERENCES nodes(id) ON DELETE CASCADE,
            rev             INTEGER NOT NULL,
            type            TEXT NOT NULL,
            state           TEXT NOT NULL,
            pi              TEXT NOT NULL,
            station_ps      TEXT,
            long_ps         TEXT,
            frequency       TEXT,
            prog_type       TEXT,
            radiotext       TEXT NOT NULL DEFAULT '[]',
            started_at      TEXT,
            ended_at        TEXT,
            duration_sec    INTEGER,
            sent_at         TEXT,
            received_at     TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            end_received_at TEXT,
            audio_path      TEXT,
            audio_received_at TEXT,
            transcription   TEXT,
            transcription_status TEXT,
            transcription_duration_sec REAL,
            alert_status    TEXT,
            alert_published_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_events_started ON events(started_at);
        CREATE INDEX IF NOT EXISTS idx_events_node ON events(node_id);
        CREATE INDEX IF NOT EXISTS idx_events_pi ON events(pi);
        CREATE INDEX IF NOT EXISTS idx_events_state ON events(state);
        CREATE INDEX IF NOT EXISTS idx_events_alert ON events(alert_published_at);
    """)
    conn.commit()
    log.info("Aggregator store initialized at %s", _DB_PATH)


def close():
    """Close this thread's connection (tests)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
    _local.conn = None
    _local.path = None


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token():
    return secrets.token_urlsafe(32)


def _node_dict(row):
    if row is None:
        return None
    d = dict(row)
    d.pop("token_hash", None)
    d["enabled"] = bool(d["enabled"])
    try:
        d["status"] = json.loads(d["status"]) if d.get("status") else None
    except (TypeError, ValueError):
        d["status"] = None
    return d


def create_node(name):
    """Create a node. Returns (node dict, plaintext token).

    The token is only ever returned here (and by rotate_token); only its
    hash is stored. Raises ValueError if the name is taken.
    """
    token = _new_token()
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "INSERT INTO nodes (name, token_hash, token_prefix, created_at)"
                " VALUES (?, ?, ?, ?)",
                (name, hash_token(token), token[:6], now_iso()))
        except sqlite3.IntegrityError:
            raise ValueError(f"A node named '{name}' already exists")
        conn.commit()
        node_id = cur.lastrowid
    log.info("Node created: %s (#%d)", name, node_id)
    return get_node(node_id), token


def rotate_token(node_id):
    """Replace a node's token. Returns the new plaintext token or None."""
    token = _new_token()
    with _lock:
        conn = _conn()
        cur = conn.execute(
            "UPDATE nodes SET token_hash = ?, token_prefix = ? WHERE id = ?",
            (hash_token(token), token[:6], node_id))
        conn.commit()
    if cur.rowcount == 0:
        return None
    log.info("Token rotated for node #%d", node_id)
    return token


def set_node_enabled(node_id, enabled):
    with _lock:
        conn = _conn()
        cur = conn.execute("UPDATE nodes SET enabled = ? WHERE id = ?",
                           (1 if enabled else 0, node_id))
        conn.commit()
    return cur.rowcount > 0


def delete_node(node_id):
    """Delete a node and all its events. Returns the audio files to remove,
    or None if the node did not exist."""
    with _lock:
        conn = _conn()
        rows = conn.execute(
            "SELECT audio_path FROM events WHERE node_id = ? "
            "AND audio_path IS NOT NULL", (node_id,)).fetchall()
        cur = conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        conn.commit()
    if cur.rowcount == 0:
        return None
    log.info("Node #%d deleted", node_id)
    return [r["audio_path"] for r in rows]


def get_node(node_id):
    row = _conn().execute("SELECT * FROM nodes WHERE id = ?",
                          (node_id,)).fetchone()
    return _node_dict(row)


def get_node_by_name(name):
    row = _conn().execute("SELECT * FROM nodes WHERE name = ?",
                          (name,)).fetchone()
    return _node_dict(row)


def list_nodes():
    rows = _conn().execute("SELECT * FROM nodes ORDER BY name").fetchall()
    return [_node_dict(r) for r in rows]


def authenticate(token):
    """Return the node dict for a token (enabled or not), or None."""
    if not token:
        return None
    wanted = hash_token(token)
    row = _conn().execute("SELECT * FROM nodes WHERE token_hash = ?",
                          (wanted,)).fetchone()
    if row is None or not hmac.compare_digest(row["token_hash"], wanted):
        return None
    return _node_dict(row)


def touch_node(node_id):
    with _lock:
        conn = _conn()
        conn.execute("UPDATE nodes SET last_seen_at = ? WHERE id = ?",
                     (now_iso(), node_id))
        conn.commit()


def record_heartbeat(node_id, reported_name, version, status, clock_offset):
    ts = now_iso()
    with _lock:
        conn = _conn()
        conn.execute(
            "UPDATE nodes SET last_seen_at = ?, last_heartbeat_at = ?, "
            "reported_name = ?, version = ?, status = ?, clock_offset_sec = ? "
            "WHERE id = ?",
            (ts, ts, reported_name, version, json.dumps(status),
             clock_offset, node_id))
        conn.commit()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

_EVENT_FIELDS = ("type", "state", "pi", "station_ps", "long_ps", "frequency",
                 "prog_type", "started_at", "ended_at", "duration_sec",
                 "sent_at")

STORED = "stored"
STALE = "stale"
CONFLICT = "conflict"


def upsert_event(node_id, event_uid, ev):
    """Store a validated event snapshot.

    Returns (result, previous_state, row) where result is STORED, STALE
    (an equal or newer rev is already stored) or CONFLICT (the uid belongs
    to another node). previous_state is None for a new event.
    """
    ts = now_iso()
    values = [ev.get(k) for k in _EVENT_FIELDS]
    radiotext = json.dumps(ev.get("radiotext") or [], ensure_ascii=False)
    with _lock:
        conn = _conn()
        existing = conn.execute(
            "SELECT node_id, rev, state, end_received_at FROM events "
            "WHERE event_uid = ?", (event_uid,)).fetchone()
        if existing is None:
            end_received = ts if ev["state"] == "end" else None
            conn.execute(
                "INSERT INTO events (event_uid, node_id, rev, "
                + ", ".join(_EVENT_FIELDS)
                + ", radiotext, received_at, updated_at, end_received_at) "
                "VALUES (?, ?, ?, " + ", ".join("?" * len(_EVENT_FIELDS))
                + ", ?, ?, ?, ?)",
                [event_uid, node_id, ev["rev"]] + values
                + [radiotext, ts, ts, end_received])
            prev_state = None
        else:
            if existing["node_id"] != node_id:
                return CONFLICT, None, None
            if ev["rev"] <= existing["rev"]:
                return STALE, existing["state"], None
            end_received = existing["end_received_at"]
            if ev["state"] == "end" and not end_received:
                end_received = ts
            conn.execute(
                "UPDATE events SET rev = ?, "
                + ", ".join(f"{k} = ?" for k in _EVENT_FIELDS)
                + ", radiotext = ?, updated_at = ?, end_received_at = ? "
                "WHERE event_uid = ?",
                [ev["rev"]] + values + [radiotext, ts, end_received,
                                        event_uid])
            prev_state = existing["state"]
        conn.commit()
    return STORED, prev_state, get_event(event_uid)


def set_event_audio(node_id, event_uid, audio_path, transcription_status):
    """Attach an audio file to an event owned by node_id.

    Clears any earlier transcription, since the audio may have changed.
    Returns the updated row, or None if no such event for this node.
    """
    with _lock:
        conn = _conn()
        cur = conn.execute(
            "UPDATE events SET audio_path = ?, audio_received_at = ?, "
            "transcription = NULL, transcription_duration_sec = NULL, "
            "transcription_status = ? "
            "WHERE event_uid = ? AND node_id = ?",
            (audio_path, now_iso(), transcription_status, event_uid, node_id))
        conn.commit()
    if cur.rowcount == 0:
        return None
    return get_event(event_uid)


def delete_event(node_id, event_uid):
    """Delete an event owned by node_id. Returns its row or None."""
    row = get_event(event_uid)
    if row is None or row["node_id"] != node_id:
        return None
    with _lock:
        conn = _conn()
        conn.execute("DELETE FROM events WHERE event_uid = ?", (event_uid,))
        conn.commit()
    return row


def _event_dict(row):
    if row is None:
        return None
    d = dict(row)
    try:
        d["radiotext"] = json.loads(d.get("radiotext") or "[]")
    except (TypeError, ValueError):
        d["radiotext"] = []
    return d


_EVENT_SELECT = ("SELECT e.*, n.name AS node_name FROM events e "
                 "JOIN nodes n ON n.id = e.node_id")


def get_event(event_uid):
    row = _conn().execute(_EVENT_SELECT + " WHERE e.event_uid = ?",
                          (event_uid,)).fetchone()
    return _event_dict(row)


def get_event_by_id(event_id):
    row = _conn().execute(_EVENT_SELECT + " WHERE e.id = ?",
                          (event_id,)).fetchone()
    return _event_dict(row)


def query_events(event_type=None, pi=None, node=None, since=None,
                 active=None, limit=50, offset=0):
    """Query events. Returns (rows, total). node is a node name."""
    where, params = [], []
    if event_type:
        types = [t.strip() for t in event_type.split(",") if t.strip()]
        where.append(f"e.type IN ({','.join('?' * len(types))})")
        params.extend(types)
    if pi:
        where.append("e.pi = ?")
        params.append(pi)
    if node:
        where.append("n.name = ?")
        params.append(node)
    if since:
        where.append("COALESCE(e.started_at, e.received_at) > ?")
        params.append(since)
    if active is True:
        where.append(f"e.state IN ({','.join('?' * len(ACTIVE_STATES))})")
        params.extend(ACTIVE_STATES)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    conn = _conn()
    total = conn.execute(
        "SELECT COUNT(*) FROM events e JOIN nodes n ON n.id = e.node_id"
        + clause, params).fetchone()[0]
    rows = conn.execute(
        _EVENT_SELECT + clause
        + " ORDER BY COALESCE(e.started_at, e.received_at) DESC, e.id DESC"
        " LIMIT ? OFFSET ?",
        params + [max(1, min(int(limit), 200)), max(0, int(offset))]
    ).fetchall()
    return [_event_dict(r) for r in rows], total


def list_stations():
    """Distinct stations seen in events, with the nodes that reported them."""
    rows = _conn().execute(
        "SELECT e.pi, MAX(e.station_ps) AS station_ps, "
        "MAX(e.frequency) AS frequency, COUNT(*) AS events, "
        "MAX(COALESCE(e.started_at, e.received_at)) AS last_event_at, "
        "GROUP_CONCAT(DISTINCT n.name) AS nodes "
        "FROM events e JOIN nodes n ON n.id = e.node_id "
        "GROUP BY e.pi ORDER BY station_ps").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["nodes"] = sorted((d.get("nodes") or "").split(",")) if d.get("nodes") else []
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Transcription and alert bookkeeping
# ---------------------------------------------------------------------------

def set_transcription_status(event_uid, status):
    with _lock:
        conn = _conn()
        conn.execute(
            "UPDATE events SET transcription_status = ? WHERE event_uid = ?",
            (status, event_uid))
        conn.commit()


def set_transcription(event_uid, text, status, duration_sec=None):
    with _lock:
        conn = _conn()
        conn.execute(
            "UPDATE events SET transcription = ?, transcription_status = ?, "
            "transcription_duration_sec = ? WHERE event_uid = ?",
            (text, status, duration_sec, event_uid))
        conn.commit()


def pending_transcriptions():
    """Events with audio whose transcription never finished (restart)."""
    rows = _conn().execute(
        _EVENT_SELECT + " WHERE e.audio_path IS NOT NULL "
        "AND e.transcription_status IN ('queued', 'transcribing') "
        "ORDER BY e.id").fetchall()
    return [_event_dict(r) for r in rows]


def alert_candidates():
    """Ended events whose alert has not been published or skipped yet."""
    rows = _conn().execute(
        _EVENT_SELECT + " WHERE e.state = 'end' "
        "AND e.alert_published_at IS NULL ORDER BY e.id").fetchall()
    return [_event_dict(r) for r in rows]


def mark_alert(event_uid, alert_status):
    """Record that an event's alert was published (or deliberately skipped)."""
    with _lock:
        conn = _conn()
        conn.execute(
            "UPDATE events SET alert_status = ?, alert_published_at = ? "
            "WHERE event_uid = ?", (alert_status, now_iso(), event_uid))
        conn.commit()


def purge_old_events(days):
    """Delete events older than `days`. Returns the audio files to remove."""
    cutoff = iso_ago(days * 86400)
    with _lock:
        conn = _conn()
        rows = conn.execute(
            "SELECT audio_path FROM events WHERE received_at < ? "
            "AND audio_path IS NOT NULL", (cutoff,)).fetchall()
        cur = conn.execute("DELETE FROM events WHERE received_at < ?",
                           (cutoff,))
        conn.commit()
    if cur.rowcount:
        log.info("Purged %d events older than %d days", cur.rowcount, days)
    return [r["audio_path"] for r in rows]


def counts():
    conn = _conn()
    return {
        "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "active": conn.execute(
            f"SELECT COUNT(*) FROM events WHERE state IN "
            f"({','.join('?' * len(ACTIVE_STATES))})",
            ACTIVE_STATES).fetchone()[0],
        "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
    }
