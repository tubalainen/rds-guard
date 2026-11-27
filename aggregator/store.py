"""SQLite store for the aggregator: nodes and the events they report."""

import calendar
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import threading
import time

import geolocate
import trafikverket
import gazetteer as gz

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
        conn.create_function("rg_contains", 4, _contains, deterministic=True)
        _local.conn = conn
        _local.path = _DB_PATH
    return conn


def _contains(radiotext, transcription, station_ps, word):
    """SQL helper for search: does any text field contain word?

    Python instead of LIKE: RadioText is stored as JSON (å, ä, ö escaped)
    and LIKE only ignores case for ASCII letters.
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

        CREATE TABLE IF NOT EXISTS settings (
            key             TEXT PRIMARY KEY,
            value           TEXT
        );

        CREATE TABLE IF NOT EXISTS notify_rules (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            data            TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notify_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uid       TEXT,
            rule_id         INTEGER,
            rule_name       TEXT,
            topic           TEXT,
            title           TEXT,
            status          TEXT NOT NULL,
            test            INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS receptions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            reception_uid   TEXT NOT NULL UNIQUE,
            event_id        INTEGER NOT NULL
                            REFERENCES events(id) ON DELETE CASCADE,
            node_id         INTEGER NOT NULL
                            REFERENCES nodes(id) ON DELETE CASCADE,
            receiver        TEXT NOT NULL DEFAULT '0',
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
            audio_bytes     INTEGER
        );

        CREATE TABLE IF NOT EXISTS join_requests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            join_id         TEXT NOT NULL UNIQUE,
            token_hash      TEXT NOT NULL UNIQUE,
            verify          TEXT NOT NULL,
            node_name       TEXT,
            version         TEXT,
            suggested_location TEXT,
            stations        TEXT,
            address         TEXT,
            status          TEXT NOT NULL DEFAULT 'pending',
            created_at      TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_notify_log_event ON notify_log(event_uid);
        CREATE INDEX IF NOT EXISTS idx_notify_log_created ON notify_log(created_at);
    """)
    # Geolocation (added in a later version: migrate older databases)
    for column in ("transcription_corrected", "geo_status", "geo_engine"):
        try:
            conn.execute(f"ALTER TABLE events ADD COLUMN {column} TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    # Where a node is, set by the aggregator's owner in Admin (overrides the
    # location the node reports in its heartbeat)
    _add_column(conn, "nodes", "location", "TEXT")
    # v0.11: an event is a broadcast, heard by one or more receptions
    _add_column(conn, "events", "primary_reception_id", "INTEGER")
    _add_column(conn, "events", "audio_pending_since", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_receptions_event ON receptions(event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_receptions_node ON receptions(node_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type_pi "
                 "ON events(type, pi, started_at)")
    ensure_receptions(conn)
    geolocate.init_tables(conn)
    trafikverket.init_tables(conn)
    conn.commit()
    log.info("Aggregator store initialized at %s", _DB_PATH)


def _add_column(conn, table, column, kind):
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def ensure_receptions(conn=None):
    """Give every event without receptions one, copied from the event (events
    stored before v0.11, and events added by the import). Idempotent."""
    own = conn is None
    conn = conn or _conn()
    cur = conn.execute(
        "INSERT INTO receptions (reception_uid, event_id, node_id, receiver, rev, "
        "type, state, pi, station_ps, long_ps, frequency, prog_type, radiotext, "
        "started_at, ended_at, duration_sec, sent_at, received_at, updated_at, "
        "end_received_at, audio_path, audio_received_at) "
        "SELECT e.event_uid, e.id, e.node_id, '0', e.rev, e.type, e.state, e.pi, "
        "e.station_ps, e.long_ps, e.frequency, e.prog_type, e.radiotext, "
        "e.started_at, e.ended_at, e.duration_sec, e.sent_at, e.received_at, "
        "e.updated_at, e.end_received_at, e.audio_path, e.audio_received_at "
        "FROM events e WHERE NOT EXISTS "
        "(SELECT 1 FROM receptions r WHERE r.event_id = e.id)")
    if cur.rowcount:
        conn.execute(
            "UPDATE events SET primary_reception_id = (SELECT r.id FROM receptions r "
            "WHERE r.event_id = events.id ORDER BY r.id LIMIT 1) "
            "WHERE primary_reception_id IS NULL")
        log.info("Gave %d events their reception", cur.rowcount)
    if own:
        conn.commit()
    return cur.rowcount


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


def free_name(name):
    """name, or "name (2)", "name (3)" … if it is taken."""
    base = (name or "").strip()[:58] or "Nod"
    candidate, n = base, 1
    conn = _conn()
    while conn.execute("SELECT 1 FROM nodes WHERE name = ?", (candidate,)).fetchone():
        n += 1
        candidate = f"{base} ({n})"
    return candidate


# --- join requests (protocol 2) ---------------------------------------------

JOIN_PENDING = "pending"
JOIN_REJECTED = "rejected"


def _join_dict(row):
    if row is None:
        return None
    d = dict(row)
    d.pop("token_hash", None)
    try:
        d["stations"] = json.loads(d["stations"]) if d.get("stations") else []
    except (TypeError, ValueError):
        d["stations"] = []
    return d


def get_join_request(request_id):
    return _join_dict(_conn().execute(
        "SELECT * FROM join_requests WHERE id = ?", (request_id,)).fetchone())


def join_request_by_verify(verify):
    return _join_dict(_conn().execute(
        "SELECT * FROM join_requests WHERE verify = ? AND status = ?",
        ((verify or "").strip().upper(), JOIN_PENDING)).fetchone())


def list_join_requests():
    rows = _conn().execute(
        "SELECT * FROM join_requests WHERE status = ? ORDER BY created_at",
        (JOIN_PENDING,)).fetchall()
    return [_join_dict(r) for r in rows]


def record_join_request(token, join_id, verify, info, address, max_pending):
    """Record (or refresh) a satellite's request to join.

    Returns (status, request): status is 'pending', 'rejected', 'conflict'
    (the join id comes with another token) or 'full' (too many pending).
    """
    token_hash = hash_token(token)
    ts = now_iso()
    with _lock:
        conn = _conn()
        row = conn.execute("SELECT * FROM join_requests WHERE join_id = ?",
                           (join_id,)).fetchone()
        if row is not None:
            if not hmac.compare_digest(row["token_hash"], token_hash):
                return "conflict", None
            if row["status"] == JOIN_REJECTED:
                return JOIN_REJECTED, _join_dict(row)
            conn.execute(
                "UPDATE join_requests SET node_name = ?, version = ?, "
                "suggested_location = ?, stations = ?, address = ?, "
                "last_seen_at = ? WHERE id = ?",
                (info.get("node_name"), info.get("version"),
                 info.get("suggested_location"),
                 json.dumps(info.get("stations") or []), address, ts, row["id"]))
            conn.commit()
            return JOIN_PENDING, get_join_request(row["id"])
        if conn.execute("SELECT 1 FROM join_requests WHERE token_hash = ?",
                        (token_hash,)).fetchone():
            return "conflict", None
        pending = conn.execute("SELECT COUNT(*) FROM join_requests WHERE status = ?",
                               (JOIN_PENDING,)).fetchone()[0]
        if pending >= max_pending:
            return "full", None
        cur = conn.execute(
            "INSERT INTO join_requests (join_id, token_hash, verify, node_name, "
            "version, suggested_location, stations, address, created_at, "
            "last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (join_id, token_hash, verify, info.get("node_name"),
             info.get("version"), info.get("suggested_location"),
             json.dumps(info.get("stations") or []), address, ts, ts))
        conn.commit()
        request_id = cur.lastrowid
    log.info("Join request from '%s' (code %s, %s): waiting for approval",
             info.get("node_name"), verify, address)
    return JOIN_PENDING, get_join_request(request_id)


def approve_join(request_id, name=None, location=None, replace_node_id=None):
    """Turn a pending request into a node (or give an existing node the
    satellite's token). Returns the node, or None if no such request.
    Raises ValueError if the name is taken."""
    with _lock:
        conn = _conn()
        req = conn.execute("SELECT * FROM join_requests WHERE id = ? AND status = ?",
                           (request_id, JOIN_PENDING)).fetchone()
        if req is None:
            return None
        if replace_node_id is not None:
            cur = conn.execute(
                "UPDATE nodes SET token_hash = ?, token_prefix = ?, enabled = 1 "
                "WHERE id = ?", (req["token_hash"], "join", replace_node_id))
            if cur.rowcount == 0:
                return None
            node_id = replace_node_id
            if location:
                conn.execute("UPDATE nodes SET location = ? WHERE id = ?",
                             (location, node_id))
        else:
            node_name = (name or "").strip() or req["node_name"] or f"Nod {req['verify']}"
            try:
                cur = conn.execute(
                    "INSERT INTO nodes (name, token_hash, token_prefix, created_at, "
                    "location) VALUES (?, ?, ?, ?, ?)",
                    (node_name, req["token_hash"], "join", now_iso(),
                     location or None))
            except sqlite3.IntegrityError:
                raise ValueError(f"A node named '{node_name}' already exists")
            node_id = cur.lastrowid
        conn.execute("DELETE FROM join_requests WHERE id = ?", (request_id,))
        conn.commit()
    node = get_node(node_id)
    log.info("Join request %s approved as node '%s'", req["verify"], node["name"])
    return node


def reject_join(request_id):
    """Reject a request: the satellite is told so until the request expires."""
    with _lock:
        conn = _conn()
        cur = conn.execute("UPDATE join_requests SET status = ? WHERE id = ? "
                           "AND status = ?", (JOIN_REJECTED, request_id, JOIN_PENDING))
        conn.commit()
    return cur.rowcount > 0


def purge_join_requests(hours):
    """Forget requests not repeated for `hours` (pending or rejected)."""
    with _lock:
        conn = _conn()
        cur = conn.execute("DELETE FROM join_requests WHERE last_seen_at < ?",
                           (iso_ago(hours * 3600),))
        conn.commit()
    return cur.rowcount


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
    """Delete a node and its receptions. Broadcasts only it heard go with
    it; those others heard too stay, with what the others heard. Returns the
    audio files to remove, or None if the node did not exist."""
    with _lock:
        conn = _conn()
        if conn.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone() is None:
            return None
        files = [r["audio_path"] for r in conn.execute(
            "SELECT audio_path FROM receptions WHERE node_id = ? "
            "AND audio_path IS NOT NULL", (node_id,))]
        event_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT event_id FROM receptions WHERE node_id = ?", (node_id,))]
        conn.execute("DELETE FROM receptions WHERE node_id = ?", (node_id,))
        for event_id in event_ids:
            _after_reception_removed(conn, event_id)
        conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        conn.commit()
    log.info("Node #%d deleted", node_id)
    return files


def set_node_location(node_id, location):
    """Set (or with None/'' clear) the location the owner gave a node."""
    with _lock:
        conn = _conn()
        cur = conn.execute("UPDATE nodes SET location = ? WHERE id = ?",
                           (location or None, node_id))
        conn.commit()
    return cur.rowcount > 0


def node_location(node):
    """Where a node is ("Ort, Län"): the location set in Admin, else the
    one the node reports in its heartbeat, else None."""
    if not node:
        return None
    return node.get("location") or (node.get("status") or {}).get("location") or None


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


def _epoch(ts):
    if not ts:
        return None
    try:
        return calendar.timegm(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def _offset(conn, node_id, cache):
    """Seconds to add to a node's timestamps to get ours (0 when unknown)."""
    if node_id not in cache:
        row = conn.execute("SELECT clock_offset_sec FROM nodes WHERE id = ?",
                           (node_id,)).fetchone()
        cache[node_id] = (row[0] if row and row[0] is not None else 0) or 0
    return cache[node_id]


# A reception that has not ended keeps its broadcast open for receptions
# starting at most this long after the broadcast began (a traffic
# announcement rarely lasts 10 minutes) ...
ONGOING_MAX_SEC = 900
# ... and only while its node is heard from: one that went silent must not
# keep the broadcast open for the next announcement
LIVE_NODE_SEC = 180


def _find_broadcast(conn, node_id, ev, window_sec, now=None):
    """The broadcast a new reception belongs to, or None.

    Same type and PI and, on the aggregator's clock (each node's measured
    offset applied), it started at most window_sec before the broadcast, or
    while the broadcast was going on: before its end if it has ended, else
    within ONGOING_MAX_SEC of its start while one of its open receptions
    comes from a node still heard from. A reception that starts after a
    broadcast ended is the next one.
    """
    start = _epoch(ev.get("started_at"))
    if start is None:
        return None
    now = time.time() if now is None else now
    offsets, live = {}, {}

    def node_live(nid):
        if nid not in live:
            row = conn.execute("SELECT last_seen_at FROM nodes WHERE id = ?",
                               (nid,)).fetchone()
            seen = _epoch(row[0]) if row and row[0] else None
            live[nid] = seen is None or now - seen <= LIVE_NODE_SEC
        return live[nid]

    start += _offset(conn, node_id, offsets)
    lo = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start - 6 * 3600))
    hi = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start + 6 * 3600))
    best = None
    for cand in conn.execute(
            "SELECT id, state FROM events WHERE type = ? AND pi = ? "
            "AND started_at BETWEEN ? AND ? ORDER BY id DESC",
            (ev["type"], ev["pi"], lo, hi)).fetchall():
        starts, ends, open_live = [], [], False
        for r in conn.execute("SELECT node_id, state, started_at, ended_at "
                              "FROM receptions WHERE event_id = ?", (cand["id"],)):
            off = _offset(conn, r["node_id"], offsets)
            s = _epoch(r["started_at"])
            if s is not None:
                starts.append(s + off)
            e = _epoch(r["ended_at"])
            if e is not None:
                ends.append(e + off)
            if r["state"] != "end" and node_live(r["node_id"]):
                open_live = True
        if not starts:
            continue
        c_start = min(starts)
        if start < c_start - window_sec:
            continue
        if cand["state"] != "end" and open_live:
            fits = start <= c_start + ONGOING_MAX_SEC
        else:
            # Ended (or only silent nodes still open): up to its last end
            c_end = max(ends) if ends else c_start
            fits = start <= max(c_end, c_start)
        if fits and (best is None or abs(start - c_start) < best[0]):
            best = (abs(start - c_start), cand["id"])
    return best[1] if best else None


def _recompute(conn, event_id, ts=None, end_grace_sec=None):
    """Derive a broadcast's fields from its receptions.

    It has ended when every reception has, or when some have and the last
    end came in end_grace_sec ago (a node that went silent must not hold
    the alert). Once ended it stays ended: late arrivals only add.
    """
    ts = ts or now_iso()
    ev = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    recs = conn.execute("SELECT * FROM receptions WHERE event_id = ? ORDER BY id",
                        (event_id,)).fetchall()
    if ev is None or not recs:
        return None
    by_id = {r["id"]: r for r in recs}
    primary = by_id.get(ev["primary_reception_id"]) or recs[0]

    def first(key):
        for r in [primary] + list(recs):
            if r[key]:
                return r[key]
        return None

    radiotext, seen = [], set()
    for r in recs:
        try:
            items = json.loads(r["radiotext"] or "[]")
        except (TypeError, ValueError):
            items = []
        for item in items:
            if item not in seen:
                seen.add(item)
                radiotext.append(item)
    starts = [r["started_at"] for r in recs if r["started_at"]]
    ended = [r for r in recs if r["state"] == "end"]
    end_marks = [r["end_received_at"] or r["updated_at"] for r in ended]
    grace = end_grace_sec if end_grace_sec is not None else 120
    lingering = (ended and len(ended) < len(recs)
                 and (_epoch(ts) or 0) - max(_epoch(m) or 0 for m in end_marks) >= grace)
    if ev["state"] == "end" or len(ended) == len(recs) or lingering:
        state = "end"
        ends = [r["ended_at"] for r in recs if r["ended_at"]]
        ended_at = max(ends) if ends else ev["ended_at"]
    else:
        open_ = [r for r in recs if r["state"] != "end"]
        state = "active" if any(r["state"] == "active" for r in open_) else (
            "update" if any(r["state"] == "update" for r in open_) else "start")
        ended_at = None
    started_at = min(starts) if starts else ev["started_at"]
    duration = None
    if started_at and ended_at:
        a, b = _epoch(started_at), _epoch(ended_at)
        if a is not None and b is not None and b >= a:
            duration = int(b - a)
    end_received = ev["end_received_at"] or (ts if state == "end" else None)
    conn.execute(
        "UPDATE events SET node_id = ?, primary_reception_id = ?, rev = ?, "
        "state = ?, station_ps = ?, long_ps = ?, frequency = ?, prog_type = ?, "
        "radiotext = ?, started_at = ?, ended_at = ?, duration_sec = ?, "
        "sent_at = ?, updated_at = ?, end_received_at = ? WHERE id = ?",
        (primary["node_id"], primary["id"], max(r["rev"] for r in recs), state,
         first("station_ps"), first("long_ps"), first("frequency"),
         first("prog_type"), json.dumps(radiotext, ensure_ascii=False),
         started_at, ended_at, duration,
         max((r["sent_at"] for r in recs if r["sent_at"]), default=None), ts,
         end_received, event_id))
    return state


def upsert_event(node_id, event_uid, ev, window_sec=30, end_grace_sec=120):
    """Store a validated snapshot of one reception and fold it into its
    broadcast (matched or new), in one transaction.

    Returns (result, previous_state, row): result is STORED, STALE (an equal
    or newer rev of this reception is stored) or CONFLICT (the uid belongs
    to another node). previous_state is the broadcast's state before (None
    for a new broadcast) and row the broadcast.
    """
    ts = now_iso()
    radiotext = json.dumps(ev.get("radiotext") or [], ensure_ascii=False)
    fields = [ev.get(k) for k in _EVENT_FIELDS]
    with _lock:
        conn = _conn()
        rec = conn.execute("SELECT * FROM receptions WHERE reception_uid = ?",
                           (event_uid,)).fetchone()
        if rec is not None:
            if rec["node_id"] != node_id:
                return CONFLICT, None, None
            prev = conn.execute("SELECT state FROM events WHERE id = ?",
                                (rec["event_id"],)).fetchone()
            if ev["rev"] <= rec["rev"]:
                return STALE, prev["state"] if prev else None, None
            end_received = rec["end_received_at"] or (ts if ev["state"] == "end" else None)
            conn.execute(
                "UPDATE receptions SET rev = ?, receiver = ?, "
                + ", ".join(f"{k} = ?" for k in _EVENT_FIELDS)
                + ", radiotext = ?, updated_at = ?, end_received_at = ? WHERE id = ?",
                [ev["rev"], ev.get("receiver") or "0"] + fields
                + [radiotext, ts, end_received, rec["id"]])
            event_id = rec["event_id"]
            prev_state = prev["state"] if prev else None
        else:
            if conn.execute("SELECT 1 FROM events WHERE event_uid = ?",
                            (event_uid,)).fetchone():
                return CONFLICT, None, None
            event_id = _find_broadcast(conn, node_id, ev, window_sec)
            prev_state = None
            if event_id is None:
                cur = conn.execute(
                    "INSERT INTO events (event_uid, node_id, rev, "
                    + ", ".join(_EVENT_FIELDS)
                    + ", radiotext, received_at, updated_at) VALUES (?, ?, ?, "
                    + ", ".join("?" * len(_EVENT_FIELDS)) + ", ?, ?, ?)",
                    [event_uid, node_id, ev["rev"]] + fields + [radiotext, ts, ts])
                event_id = cur.lastrowid
            else:
                prev_state = conn.execute("SELECT state FROM events WHERE id = ?",
                                          (event_id,)).fetchone()["state"]
            conn.execute(
                "INSERT INTO receptions (reception_uid, event_id, node_id, receiver, "
                "rev, " + ", ".join(_EVENT_FIELDS) + ", radiotext, received_at, "
                "updated_at, end_received_at) VALUES (?, ?, ?, ?, ?, "
                + ", ".join("?" * len(_EVENT_FIELDS)) + ", ?, ?, ?, ?)",
                [event_uid, event_id, node_id, ev.get("receiver") or "0", ev["rev"]]
                + fields + [radiotext, ts, ts, ts if ev["state"] == "end" else None])
        _recompute(conn, event_id, ts, end_grace_sec)
        conn.commit()
    return STORED, prev_state, get_event_by_id(event_id)


def get_reception(reception_uid):
    row = _conn().execute(
        "SELECT r.*, n.name AS node_name FROM receptions r "
        "JOIN nodes n ON n.id = r.node_id WHERE r.reception_uid = ?",
        (reception_uid,)).fetchone()
    return dict(row) if row else None


def set_event_audio(node_id, event_uid, audio_path, transcription_status=None,
                    audio_bytes=None):
    """Attach a recording to a reception owned by node_id.

    The broadcast's own audio is chosen by choose_audio (the longest of its
    receptions'); once chosen it is not changed by later recordings.
    Returns the broadcast row, or None if no such reception for this node.
    """
    ts = now_iso()
    with _lock:
        conn = _conn()
        rec = conn.execute("SELECT id, event_id FROM receptions WHERE "
                           "reception_uid = ? AND node_id = ?",
                           (event_uid, node_id)).fetchone()
        if rec is None:
            return None
        conn.execute("UPDATE receptions SET audio_path = ?, audio_received_at = ?, "
                     "audio_bytes = ? WHERE id = ?",
                     (audio_path, ts, audio_bytes, rec["id"]))
        conn.execute("UPDATE events SET audio_pending_since = COALESCE("
                     "audio_pending_since, ?) WHERE id = ? AND audio_path IS NULL",
                     (ts, rec["event_id"]))
        conn.commit()
    return get_event_by_id(rec["event_id"])


def choose_audio(event_id, transcription_status, wait_sec, now=None):
    """Pick the broadcast's recording once every reception has sent its own,
    or wait_sec after the first: the longest (by the reception's duration,
    then by size). Returns the broadcast row when one was chosen now (to be
    transcribed), else None."""
    now = time.time() if now is None else now
    with _lock:
        conn = _conn()
        ev = conn.execute("SELECT id, audio_path, audio_pending_since FROM events "
                          "WHERE id = ?", (event_id,)).fetchone()
        if ev is None or ev["audio_path"] or not ev["audio_pending_since"]:
            return None
        recs = conn.execute("SELECT * FROM receptions WHERE event_id = ? ORDER BY id",
                            (event_id,)).fetchall()
        with_audio = [r for r in recs if r["audio_path"]]
        if not with_audio:
            return None
        waited = now - (_epoch(ev["audio_pending_since"]) or now)
        if len(with_audio) < len(recs) and waited < wait_sec:
            return None
        best = max(with_audio, key=lambda r: (r["duration_sec"] or 0,
                                              r["audio_bytes"] or 0, -r["id"]))
        conn.execute(
            "UPDATE events SET audio_path = ?, audio_received_at = ?, node_id = ?, "
            "primary_reception_id = ?, audio_pending_since = NULL, "
            "transcription = NULL, transcription_duration_sec = NULL, "
            "transcription_corrected = NULL, geo_status = NULL, "
            "transcription_status = ? WHERE id = ?",
            (best["audio_path"], best["audio_received_at"], best["node_id"],
             best["id"], transcription_status, event_id))
        conn.commit()
    return get_event_by_id(event_id)


def events_waiting_for_audio():
    """Broadcasts whose recording is still to be chosen."""
    return [r[0] for r in _conn().execute(
        "SELECT id FROM events WHERE audio_pending_since IS NOT NULL "
        "AND audio_path IS NULL")]


def close_lingering_broadcasts(end_grace_sec):
    """End broadcasts where some receptions ended long enough ago and the
    rest went silent. Returns the broadcasts that ended now."""
    ended = []
    with _lock:
        conn = _conn()
        ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT e.id FROM events e JOIN receptions r ON r.event_id = e.id "
            "WHERE e.state != 'end' AND r.state = 'end'")]
        for event_id in ids:
            if _recompute(conn, event_id, None, end_grace_sec) == "end":
                ended.append(event_id)
        conn.commit()
    return [get_event_by_id(i) for i in ended]


def _after_reception_removed(conn, event_id):
    """Fold a broadcast again after one of its receptions went. Returns
    'deleted' if none is left (the broadcast goes too), else its state."""
    left = conn.execute("SELECT COUNT(*) FROM receptions WHERE event_id = ?",
                        (event_id,)).fetchone()[0]
    if left == 0:
        # The map is keyed by event uid, not by a foreign key: take the
        # broadcast off it, and the incidents only it mentioned (left behind,
        # a new import of the same history added them again)
        uid = conn.execute("SELECT event_uid FROM events WHERE id = ?",
                           (event_id,)).fetchone()
        if uid is not None:
            conn.execute("DELETE FROM incident_events WHERE event_key = ?", (uid[0],))
            conn.execute("DELETE FROM area_warnings WHERE event_key = ?", (uid[0],))
            conn.execute("DELETE FROM incidents WHERE id NOT IN "
                         "(SELECT incident_id FROM incident_events)")
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        return "deleted"
    ev = conn.execute("SELECT primary_reception_id, audio_path FROM events WHERE id = ?",
                      (event_id,)).fetchone()
    if not conn.execute("SELECT 1 FROM receptions WHERE id = ?",
                        (ev["primary_reception_id"],)).fetchone():
        # The primary went: the next one takes over, with its recording if
        # it has one (the text already made stays)
        nxt = conn.execute(
            "SELECT * FROM receptions WHERE event_id = ? ORDER BY "
            "(audio_path IS NULL), COALESCE(duration_sec, 0) DESC, id LIMIT 1",
            (event_id,)).fetchone()
        conn.execute("UPDATE events SET primary_reception_id = ?, audio_path = ?, "
                     "audio_received_at = ? WHERE id = ?",
                     (nxt["id"], nxt["audio_path"],
                      nxt["audio_received_at"] if nxt["audio_path"] else None,
                      event_id))
    return _recompute(conn, event_id)


def delete_event(node_id, event_uid):
    """Remove a reception owned by node_id (too short, discarded by its
    satellite). Returns (reception, broadcast_before, broadcast_after) or
    None: broadcast_after is None when the broadcast went with it."""
    rec = get_reception(event_uid)
    if rec is None or rec["node_id"] != node_id:
        return None
    before = get_event_by_id(rec["event_id"])
    with _lock:
        conn = _conn()
        conn.execute("DELETE FROM receptions WHERE id = ?", (rec["id"],))
        result = _after_reception_removed(conn, rec["event_id"])
        conn.commit()
    after = None if result == "deleted" else get_event_by_id(rec["event_id"])
    return rec, before, after


def _event_dict(row):
    if row is None:
        return None
    d = dict(row)
    try:
        d["radiotext"] = json.loads(d.get("radiotext") or "[]")
    except (TypeError, ValueError):
        d["radiotext"] = []
    if "node_names" in d:
        names = sorted(set((d.get("node_names") or "").split(","))) if d.get("node_names") else []
        d["node_names"] = names or ([d["node_name"]] if d.get("node_name") else [])
    return d


def event_node_ids(event_id):
    """The nodes that heard a broadcast, in the order they reported it."""
    return [r[0] for r in _conn().execute(
        "SELECT node_id FROM receptions WHERE event_id = ? GROUP BY node_id "
        "ORDER BY MIN(id)", (event_id,))]


def receptions_for_events(event_ids):
    """{event_id: [reception, ...]} (oldest first), with the node names."""
    ids = list(event_ids)
    out = {i: [] for i in ids}
    conn = _conn()
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in conn.execute(
                "SELECT r.*, n.name AS node_name FROM receptions r "
                "JOIN nodes n ON n.id = r.node_id WHERE r.event_id IN "
                f"({','.join('?' * len(part))}) ORDER BY r.id", part):
            out[r["event_id"]].append(dict(r))
    return out


_EVENT_SELECT = ("SELECT e.*, n.name AS node_name, "
                 "(SELECT GROUP_CONCAT(DISTINCT n2.name) FROM receptions r2 "
                 "JOIN nodes n2 ON n2.id = r2.node_id WHERE r2.event_id = e.id) "
                 "AS node_names FROM events e "
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
                 active=None, limit=50, offset=0, q=None, start=None,
                 end=None):
    """Query events. Returns (rows, total). node is a node name.

    q: free text, every word must appear in the RadioText, transcription or
    station name. start/end: UTC bounds on when the broadcast started.
    """
    where, params = [], []
    if start:
        where.append("COALESCE(e.started_at, e.received_at) >= ?")
        params.append(start)
    if end:
        where.append("COALESCE(e.started_at, e.received_at) < ?")
        params.append(end)
    for word in (q or "").split()[:8]:
        where.append("rg_contains(e.radiotext, COALESCE(e.transcription, '') || ' ' "
                     "|| COALESCE(e.transcription_corrected, ''), e.station_ps, ?)")
        params.append(word)
    if event_type:
        types = [t.strip() for t in event_type.split(",") if t.strip()]
        where.append(f"e.type IN ({','.join('?' * len(types))})")
        params.extend(types)
    if pi:
        where.append("e.pi = ?")
        params.append(pi)
    if node:
        # Heard by that node (any of the broadcast's receptions)
        where.append("EXISTS (SELECT 1 FROM receptions rn JOIN nodes nn "
                     "ON nn.id = rn.node_id WHERE rn.event_id = e.id AND nn.name = ?)")
        params.append(node)
    if since:
        where.append("COALESCE(e.started_at, e.received_at) > ?")
        params.append(since)
    if active is not None:
        op = "IN" if active else "NOT IN"
        where.append(f"e.state {op} ({','.join('?' * len(ACTIVE_STATES))})")
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
        "(SELECT GROUP_CONCAT(DISTINCT n.name) FROM receptions r "
        " JOIN nodes n ON n.id = r.node_id JOIN events e2 ON e2.id = r.event_id "
        " WHERE e2.pi = e.pi) AS nodes "
        "FROM events e GROUP BY e.pi ORDER BY station_ps").fetchall()
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
            "SELECT r.audio_path FROM receptions r JOIN events e ON e.id = r.event_id "
            "WHERE e.received_at < ? AND r.audio_path IS NOT NULL", (cutoff,)).fetchall()
        cur = conn.execute("DELETE FROM events WHERE received_at < ?",
                           (cutoff,))
        geolocate.purge_incidents(conn, cutoff)
        trv.purge(cutoff)
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


# ---------------------------------------------------------------------------
# Push notification rules and their log (same model as the satellite)
# ---------------------------------------------------------------------------

NOTIFY_SENT = "sent"
NOTIFY_FAILED = "failed"
NOTIFY_MQTT_OFF = "mqtt_off"
NOTIFY_NO_MATCH = "no_match"


def seed_rules_once(defaults):
    """Insert the default rules the first time the store is used."""
    with _lock:
        conn = _conn()
        if conn.execute("SELECT 1 FROM settings WHERE key = 'notify_seeded'"
                        ).fetchone():
            return False
        now = now_iso()
        for rule in defaults:
            conn.execute("INSERT INTO notify_rules (data, created_at, updated_at) "
                         "VALUES (?, ?, ?)", (json.dumps(rule), now, now))
        conn.execute("INSERT INTO settings (key, value) VALUES ('notify_seeded', '1')")
        conn.commit()
    return True


def _rule_dict(row):
    rule = json.loads(row["data"])
    rule.update(id=row["id"], created_at=row["created_at"],
                updated_at=row["updated_at"])
    return rule


def list_rules():
    rows = _conn().execute("SELECT * FROM notify_rules ORDER BY id").fetchall()
    return [_rule_dict(r) for r in rows]


def get_rule(rule_id):
    row = _conn().execute("SELECT * FROM notify_rules WHERE id = ?",
                          (rule_id,)).fetchone()
    return _rule_dict(row) if row else None


def create_rule(rule):
    with _lock:
        conn = _conn()
        now = now_iso()
        cur = conn.execute("INSERT INTO notify_rules (data, created_at, updated_at) "
                           "VALUES (?, ?, ?)", (json.dumps(rule), now, now))
        conn.commit()
    return get_rule(cur.lastrowid)


def update_rule(rule_id, rule):
    with _lock:
        conn = _conn()
        cur = conn.execute("UPDATE notify_rules SET data = ?, updated_at = ? "
                           "WHERE id = ?", (json.dumps(rule), now_iso(), rule_id))
        conn.commit()
    return get_rule(rule_id) if cur.rowcount else None


def delete_rule(rule_id):
    with _lock:
        conn = _conn()
        cur = conn.execute("DELETE FROM notify_rules WHERE id = ?", (rule_id,))
        conn.commit()
    return cur.rowcount > 0


def log_notification(event_uid, status, rule=None, topic=None, title=None,
                     test=False):
    with _lock:
        conn = _conn()
        conn.execute(
            "INSERT INTO notify_log (event_uid, rule_id, rule_name, topic, title, "
            "status, test, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event_uid, rule.get("id") if rule else None,
             rule.get("name") if rule else None, topic, title, status,
             1 if test else 0, now_iso()))
        conn.commit()


def has_notification(event_uid):
    return _conn().execute("SELECT 1 FROM notify_log WHERE event_uid = ? "
                           "AND test = 0 LIMIT 1", (event_uid,)).fetchone() is not None


def notify_log_recent(limit=30):
    rows = _conn().execute(
        "SELECT l.*, e.station_ps, e.started_at, e.radiotext, n.name AS node_name "
        "FROM notify_log l LEFT JOIN events e ON e.event_uid = l.event_uid "
        "LEFT JOIN nodes n ON n.id = e.node_id "
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


def notify_for_events(event_uids):
    """event_uid -> list of log rows (oldest first)."""
    uids = list(event_uids)
    out = {}
    conn = _conn()
    for i in range(0, len(uids), 500):
        part = uids[i:i + 500]
        rows = conn.execute(
            "SELECT event_uid, rule_id, rule_name, topic, status, created_at "
            "FROM notify_log WHERE test = 0 AND event_uid IN "
            f"({','.join('?' * len(part))}) ORDER BY id", part).fetchall()
        for r in rows:
            out.setdefault(r["event_uid"], []).append(
                {k: r[k] for k in ("rule_id", "rule_name", "topic", "status",
                                   "created_at")})
    return out


def purge_notify_log(days):
    with _lock:
        conn = _conn()
        conn.execute("DELETE FROM notify_log WHERE created_at < ?",
                     (iso_ago(days * 86400),))
        conn.commit()


def calendar_events(since):
    """Light rows for the archive calendar and statistics (newest first)."""
    rows = _conn().execute(
        "SELECT e.event_uid, e.type, e.pi, e.station_ps, e.started_at, "
        "e.received_at, e.duration_sec, n.name AS node_name "
        "FROM events e JOIN nodes n ON n.id = e.node_id "
        "WHERE COALESCE(e.started_at, e.received_at) >= ? "
        "ORDER BY COALESCE(e.started_at, e.received_at) DESC", (since,)).fetchall()
    return [dict(r) for r in rows]


def events_for_preview(since):
    """Ended events with the fields a rule looks at, newest first."""
    rows = _conn().execute(
        _EVENT_SELECT + " WHERE COALESCE(e.started_at, e.received_at) >= ? "
        f"AND e.state NOT IN ({','.join('?' * len(ACTIVE_STATES))}) "
        "ORDER BY COALESCE(e.started_at, e.received_at) DESC",
        (since, *ACTIVE_STATES)).fetchall()
    return [_event_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Geolocation: incidents on the map
# ---------------------------------------------------------------------------

def _norm_ts(ts):
    """'2026-11-24T08:15:02' or '... 08:15:02.5Z' -> '2026-11-24T08:15:02Z'."""
    return str(ts).replace(" ", "T")[:19] + "Z"


def _iso_before(at, seconds):
    import calendar
    t = calendar.timegm(time.strptime(_norm_ts(at), "%Y-%m-%dT%H:%M:%SZ"))
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t - seconds))


def save_geolocation(event_uid, result, status, at=None):
    """Store a message's corrected text and merge its incidents.

    at: when the message was broadcast, for older messages placed
    afterwards (default now)."""
    now = _norm_ts(at) if at else now_iso()
    with _lock:
        conn = _conn()
        ids = []
        if result is not None and result.placements:
            ids = geolocate.merge_placements(
                conn, result.placements, event_uid, now,
                _iso_before(now, geolocate.INCIDENT_HOURS * 3600), result.engine)
        if result is not None:
            geolocate.save_areas(conn, result.areas, event_uid, now, result.engine)
            row = conn.execute("SELECT transcription FROM events WHERE "
                               "event_uid = ?", (event_uid,)).fetchone()
            text = result.corrected_text or (row[0] if row else "") or ""
            trafikverket.link_incidents(conn, ids, now, text)
        conn.execute(
            "UPDATE events SET transcription_corrected = ?, geo_status = ?, "
            "geo_engine = ? WHERE event_uid = ?",
            (result.corrected_text if result else None, status,
             result.engine if result else None, event_uid))
        conn.commit()
    return ids


def _near(rows, center, km):
    if center is None:
        return rows
    return [r for r in rows if gz.km(center, (r["lat"], r["lon"])) <= km]


def open_incidents(center=None, km=geolocate.DEFAULT_AREA_KM, at=None):
    """Incidents near center a new message may be about (at a past time at,
    default now)."""
    if at:
        rows = geolocate.open_incidents(
            _conn(), _iso_before(at, geolocate.LINK_HOURS * 3600),
            _norm_ts(at))
    else:
        rows = geolocate.open_incidents(
            _conn(), iso_ago(geolocate.LINK_HOURS * 3600))
    return _near(rows, center, km)


def list_areas(hours=geolocate.INCIDENT_HOURS):
    """Area warnings of the last hours; hours=None: all that are stored."""
    return geolocate.list_areas(_conn(), iso_ago(hours * 3600) if hours else None)


def list_incidents(hours=geolocate.INCIDENT_HOURS):
    """Incidents for the map, with the nodes whose messages mentioned them.
    hours=None: all that are stored."""
    conn = _conn()
    rows = geolocate.list_incidents(conn, iso_ago(hours * 3600) if hours else None)
    for inc in rows:
        _incident_nodes(conn, inc)
    return rows


def _incident_nodes(conn, inc):
    nodes = conn.execute(
        "SELECT DISTINCT n.name FROM incident_events ie "
        "JOIN events e ON e.event_uid = ie.event_key "
        "JOIN receptions r ON r.event_id = e.id "
        "JOIN nodes n ON n.id = r.node_id WHERE ie.incident_id = ?",
        (inc["id"],)).fetchall()
    inc["nodes"] = sorted(r[0] for r in nodes)
    return inc


def get_incident(incident_id):
    """One incident with its broadcasts and nodes, or None."""
    conn = _conn()
    inc = geolocate.get_incident(conn, incident_id)
    return _incident_nodes(conn, inc) if inc else None


def event_briefs(event_uids):
    """{event_uid: {event_uid, started_at, station_ps, node_name,
    audio_path}} for the given events (for incident timelines)."""
    uids = sorted({str(u) for u in event_uids})
    out = {}
    for n in range(0, len(uids), 500):
        part = uids[n:n + 500]
        for r in _conn().execute(
                "SELECT e.event_uid, e.started_at, e.station_ps, e.audio_path, "
                "n.name AS node_name FROM events e JOIN nodes n ON n.id = e.node_id "
                f"WHERE e.event_uid IN ({','.join('?' * len(part))})", part):
            out[r["event_uid"]] = dict(r)
    return out


def incidents_for_events(event_uids):
    return geolocate.incidents_for_events(_conn(), event_uids)


_GEO_PENDING_WHERE = (
    " WHERE e.transcription IS NOT NULL AND TRIM(e.transcription) != '' "
    "AND e.transcription_status = 'done' AND (e.geo_status IS NULL OR "
    "e.geo_status IN (" + ",".join("'%s'" % s for s in geolocate.PENDING_STATUSES)
    + "))")


def geo_pending():
    """Transcribed messages never placed on the map, oldest first."""
    rows = _conn().execute(
        _EVENT_SELECT + _GEO_PENDING_WHERE
        + " ORDER BY COALESCE(e.started_at, e.received_at), e.id").fetchall()
    out = []
    for r in rows:
        d = _event_dict(r)
        d["at"] = d.get("started_at") or d.get("received_at")
        out.append(d)
    return out


_GEO_PLACEABLE_WHERE = (
    " WHERE transcription IS NOT NULL AND TRIM(transcription) != '' "
    "AND transcription_status = 'done'")


def geo_placeable_count():
    """Transcribed messages a rebuild would place again."""
    return _conn().execute(
        "SELECT COUNT(*) FROM events" + _GEO_PLACEABLE_WHERE).fetchone()[0]


def reset_geolocation():
    """Delete the incidents and mark every transcribed message as never
    placed, so the backfill places them all again, oldest first.
    Returns (incidents deleted, messages to place)."""
    with _lock:
        conn = _conn()
        gone = geolocate.reset_incidents(conn)
        cur = conn.execute("UPDATE events SET geo_status = NULL, geo_engine = NULL"
                           + _GEO_PLACEABLE_WHERE)
        conn.commit()
    return gone, cur.rowcount


def geo_pending_by_node():
    """{node_id: number of messages never placed on the map}."""
    return dict(_conn().execute(
        "SELECT e.node_id, COUNT(*) FROM events e" + _GEO_PENDING_WHERE
        + " GROUP BY e.node_id").fetchall())


def recent_place_names(center=None, days=90, limit=60):
    """Names from recent incidents near center, most mentioned first."""
    conn = _conn()
    since = iso_ago(days * 86400)
    if center is None:
        return geolocate.recent_names(conn, since, limit)
    counts = {}
    for r in conn.execute("SELECT names, lat, lon FROM incidents "
                          "WHERE last_seen >= ?", (since,)):
        if gz.km(center, (r["lat"], r["lon"])) > geolocate.DEFAULT_AREA_KM:
            continue
        try:
            for n in json.loads(r["names"] or "[]"):
                counts[n] = counts.get(n, 0) + 1
        except ValueError:
            continue
    return [n for n, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:limit]


# ---------------------------------------------------------------------------
# Trafikverket's deviations (trafikverket.py), in this database
# ---------------------------------------------------------------------------

trv = trafikverket.TrvStore(lambda: _conn(), _lock, fmt="%Y-%m-%dT%H:%M:%SZ")


def trv_relink(dev_ids):
    """Link new or changed deviations to the radio's incidents and update
    the linked ones. Returns (new links, updated incident ids)."""
    now = now_iso()
    with _lock:
        conn = _conn()
        out = trafikverket.relink_deviations(
            conn, dev_ids, now, iso_ago(geolocate.INCIDENT_HOURS * 3600))
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


def rules_sent_for(event_uids):
    """Ids of the rules a push went out for, for these events."""
    uids = [str(u) for u in event_uids]
    if not uids:
        return []
    return [r[0] for r in _conn().execute(
        "SELECT DISTINCT rule_id FROM notify_log WHERE rule_id IS NOT NULL "
        f"AND test = 0 AND status = ? AND event_uid IN ({','.join('?' * len(uids))})",
        [NOTIFY_SENT, *uids])]
