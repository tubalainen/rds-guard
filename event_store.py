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

log = logging.getLogger("rds-guard")

_DB_PATH = "/data/events.db"
_lock = threading.Lock()
_local = threading.local()


def _conn():
    """Get a thread-local database connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(_DB_PATH)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")
    return _local.conn


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
    with _lock:
        conn = _conn()
        cur = conn.execute(
            """INSERT INTO events
               (type, severity, state, pi, station_ps, frequency,
                radiotext, data, started_at, ended_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_type, severity, state, pi, station_ps, frequency,
             rt_json, data_json, started_at, ended_at)
        )
        conn.commit()
        return cur.lastrowid


def update_event_radiotext(event_id, radiotext_list):
    """Append RadioText to an existing event (traffic update)."""
    rt_json = json.dumps(radiotext_list)
    with _lock:
        conn = _conn()
        conn.execute(
            "UPDATE events SET radiotext = ?, state = 'update' WHERE id = ?",
            (rt_json, event_id)
        )
        conn.commit()


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
        conn.commit()


def query_events(event_type=None, since=None, limit=50, offset=0):
    """Query events with optional filters. Returns (rows, total_count)."""
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

    where_clause = " WHERE " + " AND ".join(where) if where else ""

    # Total count
    count_row = conn.execute(
        f"SELECT COUNT(*) FROM events{where_clause}", params
    ).fetchone()
    total = count_row[0] if count_row else 0

    # Paginated results
    rows = conn.execute(
        f"SELECT * FROM events{where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
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
        conn.commit()


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
    """Delete a single event by ID. Returns True if a row was deleted."""
    with _lock:
        conn = _conn()
        cur = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        conn.commit()
        deleted = cur.rowcount > 0
        if deleted:
            log.info("Deleted event %d", event_id)
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
        cur = conn.execute(
            "UPDATE events SET state = 'end', ended_at = ? "
            "WHERE state IN ('start', 'update', 'active') AND ended_at IS NULL",
            (now,)
        )
        conn.commit()
        if cur.rowcount > 0:
            log.info("Closed %d stale active events from previous run", cur.rowcount)
        return cur.rowcount


def delete_all_events():
    """Clear all events from the database."""
    with _lock:
        conn = _conn()
        cur = conn.execute("DELETE FROM events")
        conn.commit()
        log.info("Deleted all %d events", cur.rowcount)
        return cur.rowcount
