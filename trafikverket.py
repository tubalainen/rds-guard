"""Trafikverket's traffic deviations (Situation): exact places and updates.

Trafikverket publishes the incidents P4's traffic desk talks about, with an
exact point (and often the affected stretch of road), the direction, how
much traffic is affected and a text that is updated while the incident
lasts ("Bärgning pågår", a new expected end time, removed). This module
keeps a local copy of the ones near the receiver(s):

  - TrafikverketFeed polls the open API with changeid, so each call only
    returns what changed (and what was removed) since the last one.
  - TrvStore keeps every deviation and, for the timeline, each version's
    changed fields, in the same SQLite database as the events.

Shared by the satellite and the aggregator. Optional: without
TRAFIKVERKET_API_KEY nothing here runs. The key is free; there is no cost
per message.

API: https://api.trafikinfo.trafikverket.se/v2/data.json, object type
Situation, namespace Road.TrafficInfo, schema version 1.6.
"""

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from xml.sax.saxutils import escape

log = logging.getLogger("rds-guard")

API_URL = "https://api.trafikinfo.trafikverket.se/v2/data.json"
SCHEMA_VERSION = "1.6"
# Deviation.MessageType values we keep. Vägarbete and Färjor are left out:
# road works are many and long-lived and would be matched to the wrong
# radio messages.
MESSAGE_TYPES = ("Olycka", "Hinder", "Restriktion", "Trafikmeddelande",
                 "Viktig trafikinformation")
DEFAULT_RADIUS_KM = 150
DEFAULT_POLL_SEC = 60
FULL_REFRESH_SEC = 6 * 3600    # re-read everything to catch what we missed
MAX_CENTERS = 20
TIMEOUT_SEC = 30
MAX_BACKOFF_SEC = 900

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_DELETED = "deleted"

# Fields whose changes make a new version on the timeline
TRACKED = ("message_type", "message_code", "header", "message", "location",
           "positional", "road_number", "severity_code", "severity_text",
           "restriction", "lanes", "temporary_limit", "direction", "lat",
           "lon", "start_time", "end_time", "until_further_notice", "status")


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

def _attr(value):
    """An XML attribute value in double quotes."""
    return '"' + escape(str(value), {'"': "&quot;"}) + '"'


def merge_centers(centers, radius_km, limit=MAX_CENTERS):
    """Drop centers that lie close to one already kept (an aggregator's
    nodes are often near each other); at most limit are kept."""
    import gazetteer as gz
    kept = []
    for c in centers:
        if c is None:
            continue
        c = (round(float(c[0]), 4), round(float(c[1]), 4))
        if all(gz.km(c, k) > radius_km / 4 for k in kept):
            kept.append(c)
        if len(kept) >= limit:
            break
    return kept


def build_request(api_key, centers, radius_km=DEFAULT_RADIUS_KM, changeid=0,
                  message_types=MESSAGE_TYPES):
    """The XML query: deviations of message_types within radius_km of any
    center ((lat, lon) pairs), changed after changeid."""
    areas = [
        f'<WITHIN name="Deviation.Geometry.Point.WGS84" shape="center" '
        f'value="{lon:.5f} {lat:.5f}" radius="{int(radius_km * 1000)}m"/>'
        for lat, lon in centers]
    types = [f'<EQ name="Deviation.MessageType" value={_attr(t)}/>'
             for t in message_types]

    def any_of(parts):
        return parts[0] if len(parts) == 1 else "<OR>" + "".join(parts) + "</OR>"

    return (
        "<REQUEST>"
        f"<LOGIN authenticationkey={_attr(api_key)}/>"
        f'<QUERY objecttype="Situation" namespace="Road.TrafficInfo" '
        f'schemaversion="{SCHEMA_VERSION}" includedeletedobjects="true" '
        f'changeid="{int(changeid)}">'
        f"<FILTER><AND>{any_of(areas)}{any_of(types)}</AND></FILTER>"
        "</QUERY></REQUEST>")


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

class ApiError(Exception):
    """The API answered with an error (bad key, bad query)."""


_NUM = r"(-?\d+(?:\.\d+)?)"
_POINT_RE = re.compile(r"POINT\s*Z?\s*\(\s*" + _NUM + r"\s+" + _NUM, re.I)
_LINE_RE = re.compile(r"LINESTRING\s*Z?\s*\(([^)]*)\)", re.I)


def parse_point(wkt):
    """'POINT (13.50 59.38 0)' -> (lat, lon), or None."""
    m = _POINT_RE.search(wkt or "")
    if not m:
        return None
    lon, lat = float(m.group(1)), float(m.group(2))
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def parse_line(wkt):
    """'LINESTRING (lon lat z, ...)' -> [(lat, lon), ...] (in the order
    of the affected direction), or []."""
    m = _LINE_RE.search(wkt or "")
    if not m:
        return []
    out = []
    for part in m.group(1).split(","):
        nums = part.split()
        if len(nums) >= 2:
            try:
                out.append((round(float(nums[1]), 6), round(float(nums[0]), 6)))
            except ValueError:
                continue
    return out


def parse_time(value, fmt="%Y-%m-%dT%H:%M:%S"):
    """Trafikverket's local time with offset -> UTC in the store's format."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(fmt)


def parse_response(obj):
    """Decoded JSON response -> (situations, lastchangeid).

    Raises ApiError when the API reports an error."""
    try:
        result = obj["RESPONSE"]["RESULT"][0]
    except (KeyError, IndexError, TypeError):
        raise ApiError("unexpected response") from None
    if "ERROR" in result:
        err = result["ERROR"] or {}
        raise ApiError(f"{err.get('SOURCE', '')}: {err.get('MESSAGE', '')}"
                       .strip(": "))
    info = result.get("INFO") or {}
    return result.get("Situation") or [], info.get("LASTCHANGEID")


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _geometry_wkt(geom, kind):
    g = (geom or {}).get(kind) or {}
    if isinstance(g, list):
        g = g[0] if g else {}
    return g.get("WGS84") if isinstance(g, dict) else None


def normalize(dev, situation_id, fmt="%Y-%m-%dT%H:%M:%S"):
    """One Deviation -> the dict stored in trv_deviations."""
    geom = dev.get("Geometry") or {}
    point = parse_point(_geometry_wkt(geom, "Point") or geom.get("WGS84"))
    line = parse_line(_geometry_wkt(geom, "Line"))
    if point is None and line:
        point = line[len(line) // 2]
    direction = {"BothDirections": "both", "OneDirection": "one"}.get(
        dev.get("AffectedDirectionValue"))
    return {
        "id": str(dev.get("Id") or ""),
        "situation_id": situation_id,
        "message_type": dev.get("MessageType"),
        "message_code": dev.get("MessageCode"),
        "header": dev.get("Header"),
        "message": dev.get("Message"),
        "location": dev.get("LocationDescriptor"),
        "positional": dev.get("PositionalDescription"),
        "road_number": dev.get("RoadNumber"),
        "road_numeric": _int_or_none(dev.get("RoadNumberNumeric")),
        "severity_code": _int_or_none(dev.get("SeverityCode")),
        "severity_text": dev.get("SeverityText"),
        "restriction": dev.get("TrafficRestrictionType"),
        "lanes": _int_or_none(dev.get("NumberOfLanesRestricted")),
        "temporary_limit": dev.get("TemporaryLimit"),
        "direction": direction,
        "lat": point[0] if point else None,
        "lon": point[1] if point else None,
        "line": json.dumps(line) if line else None,
        "county": _int_or_none(dev.get("CountyNo")),
        "start_time": parse_time(dev.get("StartTime"), fmt),
        "end_time": parse_time(dev.get("EndTime"), fmt),
        "until_further_notice": 1 if dev.get("ValidUntilFurtherNotice") else 0,
        "created": parse_time(dev.get("CreationTime"), fmt),
        "version_time": parse_time(dev.get("VersionTime"), fmt),
        "web_link": dev.get("WebLink"),
        "status": STATUS_SUSPENDED if dev.get("Suspended") else STATUS_ACTIVE,
    }


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS trv_deviations (
    id                   TEXT PRIMARY KEY,
    situation_id         TEXT NOT NULL,
    message_type         TEXT,
    message_code         TEXT,
    header               TEXT,
    message              TEXT,
    location             TEXT,
    positional           TEXT,
    road_number          TEXT,
    road_numeric         INTEGER,
    severity_code        INTEGER,
    severity_text        TEXT,
    restriction          TEXT,
    lanes                INTEGER,
    temporary_limit      TEXT,
    direction            TEXT,
    lat                  REAL,
    lon                  REAL,
    line                 TEXT,
    county               INTEGER,
    start_time           TEXT,
    end_time             TEXT,
    until_further_notice INTEGER NOT NULL DEFAULT 0,
    created              TEXT,
    version_time         TEXT,
    web_link             TEXT,
    status               TEXT NOT NULL DEFAULT 'active',
    first_seen           TEXT NOT NULL,
    last_seen            TEXT NOT NULL,
    changed_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trv_situation ON trv_deviations(situation_id);
CREATE INDEX IF NOT EXISTS idx_trv_last_seen ON trv_deviations(last_seen);
CREATE TABLE IF NOT EXISTS trv_deviation_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    deviation_id  TEXT NOT NULL,
    seen_at       TEXT NOT NULL,
    version_time  TEXT,
    changes       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trv_versions_dev
    ON trv_deviation_versions(deviation_id);
CREATE TABLE IF NOT EXISTS trv_state (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""

_COLUMNS = ("id", "situation_id", "message_type", "message_code", "header",
            "message", "location", "positional", "road_number",
            "road_numeric", "severity_code", "severity_text", "restriction",
            "lanes", "temporary_limit", "direction", "lat", "lon", "line",
            "county", "start_time", "end_time", "until_further_notice",
            "created", "version_time", "web_link", "status")


def init_tables(conn):
    conn.executescript(SCHEMA)


def _row_dict(row):
    d = dict(row)
    try:
        d["line"] = json.loads(d["line"]) if d.get("line") else []
    except ValueError:
        d["line"] = []
    d["until_further_notice"] = bool(d.get("until_further_notice"))
    return d


class TrvStore:
    """Trafikverket's deviations in a SQLite database.

    conn_fn returns the thread's connection, lock serialises writes (the
    store's own lock), fmt is the database's time format.
    """

    def __init__(self, conn_fn, lock, fmt="%Y-%m-%dT%H:%M:%S"):
        self._conn = conn_fn
        self._lock = lock
        self.fmt = fmt

    def now(self):
        return time.strftime(self.fmt, time.gmtime())

    def ago(self, seconds):
        return time.strftime(self.fmt, time.gmtime(time.time() - seconds))

    # -- state ----------------------------------------------------------------

    def get_state(self, key):
        row = self._conn().execute(
            "SELECT value FROM trv_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key, value):
        with self._lock:
            conn = self._conn()
            conn.execute("INSERT OR REPLACE INTO trv_state (key, value) "
                         "VALUES (?, ?)", (key, value))
            conn.commit()

    # -- writes ---------------------------------------------------------------

    def apply(self, situations, full=False):
        """Store a response's situations. full=True means the response holds
        everything that is current: active deviations it lacks are gone.

        Returns the ids of the deviations that are new or changed.
        """
        now = self.now()
        changed = []
        with self._lock:
            conn = self._conn()
            seen = set()
            for sit in situations:
                sit_id = str(sit.get("Id") or "")
                if not sit_id:
                    continue
                old_ids = {r[0] for r in conn.execute(
                    "SELECT id FROM trv_deviations WHERE situation_id = ? "
                    "AND status != ?", (sit_id, STATUS_DELETED))}
                devs = [] if sit.get("Deleted") else [
                    d for d in (sit.get("Deviation") or [])
                    if d.get("Id") and d.get("MessageType") in MESSAGE_TYPES]
                keep = set()
                for dev in devs:
                    rec = normalize(dev, sit_id, self.fmt)
                    keep.add(rec["id"])
                    if self._upsert(conn, rec, now):
                        changed.append(rec["id"])
                for gone in old_ids - keep:
                    if self._mark_deleted(conn, gone, now):
                        changed.append(gone)
                seen |= keep
            if full:
                for (dev_id,) in conn.execute(
                        "SELECT id FROM trv_deviations WHERE status != ?",
                        (STATUS_DELETED,)).fetchall():
                    if dev_id not in seen and self._mark_deleted(conn, dev_id, now):
                        changed.append(dev_id)
            conn.commit()
        return changed

    def _upsert(self, conn, rec, now):
        old = conn.execute("SELECT * FROM trv_deviations WHERE id = ?",
                           (rec["id"],)).fetchone()
        if old is None:
            cols = _COLUMNS + ("first_seen", "last_seen", "changed_at")
            conn.execute(
                f"INSERT INTO trv_deviations ({', '.join(cols)}) VALUES "
                f"({', '.join('?' * len(cols))})",
                [rec[c] for c in _COLUMNS] + [now, now, now])
            changes = {k: rec[k] for k in TRACKED if rec[k] is not None}
        else:
            old = dict(old)
            changes = {k: rec[k] for k in TRACKED if old.get(k) != rec[k]}
            sets = {c: rec[c] for c in _COLUMNS if c != "id"}
            sets["last_seen"] = now
            if changes:
                sets["changed_at"] = now
            conn.execute(
                "UPDATE trv_deviations SET "
                + ", ".join(f"{k} = ?" for k in sets) + " WHERE id = ?",
                [*sets.values(), rec["id"]])
        if changes:
            conn.execute(
                "INSERT INTO trv_deviation_versions (deviation_id, seen_at, "
                "version_time, changes) VALUES (?, ?, ?, ?)",
                (rec["id"], now, rec["version_time"],
                 json.dumps(changes, ensure_ascii=False)))
        return bool(changes)

    def _mark_deleted(self, conn, dev_id, now):
        cur = conn.execute(
            "UPDATE trv_deviations SET status = ?, last_seen = ?, "
            "changed_at = ? WHERE id = ? AND status != ?",
            (STATUS_DELETED, now, now, dev_id, STATUS_DELETED))
        if cur.rowcount:
            conn.execute(
                "INSERT INTO trv_deviation_versions (deviation_id, seen_at, "
                "version_time, changes) VALUES (?, ?, NULL, ?)",
                (dev_id, now, json.dumps({"status": STATUS_DELETED})))
        return bool(cur.rowcount)

    def purge(self, before_iso):
        """Remove deviations last seen before before_iso. The caller holds
        the store's lock and commits."""
        conn = self._conn()
        conn.execute(
            "DELETE FROM trv_deviation_versions WHERE deviation_id IN "
            "(SELECT id FROM trv_deviations WHERE last_seen < ?)",
            (before_iso,))
        conn.execute("DELETE FROM trv_deviations WHERE last_seen < ?",
                     (before_iso,))

    # -- reads ----------------------------------------------------------------

    def get(self, dev_id, versions=True):
        conn = self._conn()
        row = conn.execute("SELECT * FROM trv_deviations WHERE id = ?",
                           (dev_id,)).fetchone()
        if row is None:
            return None
        d = _row_dict(row)
        if versions:
            d["versions"] = self.versions(dev_id)
        return d

    def versions(self, dev_id):
        out = []
        for r in self._conn().execute(
                "SELECT seen_at, version_time, changes FROM "
                "trv_deviation_versions WHERE deviation_id = ? ORDER BY id",
                (dev_id,)):
            try:
                changes = json.loads(r["changes"])
            except ValueError:
                changes = {}
            out.append({"seen_at": r["seen_at"],
                        "version_time": r["version_time"],
                        "changes": changes})
        return out

    def list(self, hours=12, include_ended=True, versions=False):
        """Deviations that are current or changed in the last hours.

        A deviation whose end time has passed counts as ended."""
        now = self.now()
        rows = [_row_dict(r) for r in self._conn().execute(
            "SELECT * FROM trv_deviations WHERE status != ? OR changed_at >= ? "
            "ORDER BY changed_at DESC", (STATUS_DELETED, self.ago(hours * 3600)))]
        out = []
        for d in rows:
            d["ended"] = (d["status"] == STATUS_DELETED
                          or (d["end_time"] is not None
                              and not d["until_further_notice"]
                              and d["end_time"] < now))
            if d["ended"] and not include_ended:
                continue
            if versions:
                d["versions"] = self.versions(d["id"])
            out.append(d)
        return out

    def count_active(self):
        return self._conn().execute(
            "SELECT COUNT(*) FROM trv_deviations WHERE status = ?",
            (STATUS_ACTIVE,)).fetchone()[0]


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------

class TrafikverketFeed:
    """Background thread that keeps a TrvStore up to date.

    centers_fn returns the (lat, lon) points to watch around (the node, or
    an aggregator's nodes); nothing is fetched while it returns none.
    on_change(ids) is called after new or changed deviations were stored.
    """

    def __init__(self, api_key, store, centers_fn, radius_km=DEFAULT_RADIUS_KM,
                 poll_sec=DEFAULT_POLL_SEC, on_change=None, post=None,
                 url=API_URL):
        self._key = api_key
        self._store = store
        self._centers_fn = centers_fn
        self.radius_km = max(10, min(500, radius_km))
        self.poll_sec = max(30, poll_sec)
        self._on_change = on_change
        self._post = post
        self._url = url
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._state = {"last_ok": None, "last_error": None,
                       "last_error_at": None, "polls": 0, "centers": 0}
        self._last_full = 0.0
        self._failures = 0

    # -- lifecycle ------------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="trafikverket")
        self._thread.start()
        log.info("Trafikverket: watching deviations within %d km, every %d s",
                 self.radius_km, self.poll_sec)

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            wait = self.poll_sec
            try:
                if not self.poll_once():
                    wait = min(wait, 30)        # no location yet
                self._failures = 0
            except ApiError as e:
                wait = self._failed(f"API: {e}")
            except Exception as e:   # network, JSON, database
                wait = self._failed(str(e) or type(e).__name__)
            self._stop.wait(wait)

    def _failed(self, msg):
        self._failures += 1
        with self._lock:
            self._state["last_error"] = msg
            self._state["last_error_at"] = self._store.now()
        wait = min(MAX_BACKOFF_SEC, self.poll_sec * 2 ** min(self._failures, 6))
        log.warning("Trafikverket: %s (next try in %d s)", msg, wait)
        return wait

    # -- one poll -------------------------------------------------------------

    def _signature(self, centers):
        return json.dumps([self.radius_km, list(MESSAGE_TYPES),
                           [list(c) for c in centers]])

    def poll_once(self):
        """Fetch and store what changed. False when there is nowhere to
        watch yet. Raises on errors."""
        centers = merge_centers(self._centers_fn() or [], self.radius_km)
        with self._lock:
            self._state["centers"] = len(centers)
        if not centers:
            return False
        sig = self._signature(centers)
        changeid = self._store.get_state("lastchangeid")
        full = (changeid is None or self._store.get_state("signature") != sig
                or time.time() - self._last_full >= FULL_REFRESH_SEC)
        if full:
            changeid = 0
        body = build_request(self._key, centers, self.radius_km, changeid)
        situations, last = parse_response(self._fetch(body))
        changed = self._store.apply(situations, full=full)
        if full:
            self._last_full = time.time()
            self._store.set_state("signature", sig)
        if last is not None:
            self._store.set_state("lastchangeid", str(last))
        with self._lock:
            self._state["last_ok"] = self._store.now()
            self._state["polls"] += 1
            self._state["last_error"] = None
        if changed:
            log.info("Trafikverket: %d new or changed deviations", len(changed))
            if self._on_change:
                try:
                    self._on_change(changed)
                except Exception:
                    log.exception("Trafikverket: change handler failed")
        return True

    def _fetch(self, body):
        if self._post is not None:
            return self._post(self._url, body)
        import requests
        resp = requests.post(
            self._url, data=body.encode("utf-8"), timeout=TIMEOUT_SEC,
            headers={"Content-Type": "text/xml; charset=utf-8"})
        if resp.status_code == 401:
            raise ApiError("the key was not accepted (TRAFIKVERKET_API_KEY)")
        try:
            obj = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise ApiError(f"HTTP {resp.status_code}, not JSON") from None
        if resp.status_code >= 400:
            parse_response(obj)     # raises with the API's own message
            raise ApiError(f"HTTP {resp.status_code}")
        return obj

    # -- status ---------------------------------------------------------------

    def snapshot(self):
        with self._lock:
            out = dict(self._state)
        out.update(enabled=True, radius_km=self.radius_km,
                   poll_sec=self.poll_sec)
        try:
            out["active"] = self._store.count_active()
        except Exception:
            out["active"] = None
        return out


def create_feed(api_key, store, centers_fn, radius_km=DEFAULT_RADIUS_KM,
                poll_sec=DEFAULT_POLL_SEC, on_change=None):
    """A started feed, or None without a key."""
    if not api_key:
        return None
    feed = TrafikverketFeed(api_key, store, centers_fn, radius_km=radius_km,
                            poll_sec=poll_sec, on_change=on_change)
    feed.start()
    return feed


def disabled_snapshot():
    return {"enabled": False}
