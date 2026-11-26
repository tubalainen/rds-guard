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

import calendar
import json
import logging
import math
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
                "SELECT id, seen_at, version_time, changes FROM "
                "trv_deviation_versions WHERE deviation_id = ? ORDER BY id",
                (dev_id,)):
            try:
                changes = json.loads(r["changes"])
            except ValueError:
                changes = {}
            out.append({"vid": r["id"], "seen_at": r["seen_at"],
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

    def layer(self, hours=12):
        """Deviations for the map layer: current accidents, obstacles and
        the like that no radio incident is linked to."""
        now = self.now()
        linked = {r[0] for r in self._conn().execute(
            "SELECT DISTINCT trv_id FROM incidents WHERE trv_id IS NOT NULL")}
        out = []
        for d in self.list(hours, include_ended=False):
            if d["id"] in linked or not layer_worthy(d, now):
                continue
            d["kinds"] = sorted(deviation_kinds(d))
            out.append(d)
        return out

    def touch_linked(self, every_sec=3600):
        """Keep radio incidents on the map while their deviation is still
        current at Trafikverket: their last_seen moves up once an hour.
        Returns the number of incidents moved."""
        now = self.now()
        with self._lock:
            conn = self._conn()
            cur = conn.execute(
                "UPDATE incidents SET last_seen = ? WHERE status = 'active' "
                "AND last_seen < ? AND trv_id IN (SELECT id FROM trv_deviations "
                "WHERE status = ? AND (end_time IS NULL OR "
                "until_further_notice = 1 OR end_time >= ?))",
                (now, self.ago(every_sec), STATUS_ACTIVE, now))
            conn.commit()
        return cur.rowcount

    def last_version_id(self):
        """The newest version's number: versions after it are new."""
        return self._conn().execute(
            "SELECT COALESCE(MAX(id), 0) FROM trv_deviation_versions").fetchone()[0]

    def count_active(self):
        return self._conn().execute(
            "SELECT COUNT(*) FROM trv_deviations WHERE status = ?",
            (STATUS_ACTIVE,)).fetchone()[0]


# ---------------------------------------------------------------------------
# What kind of incident: words in the radio message and in Trafikverket's
# ---------------------------------------------------------------------------

KIND_ACCIDENT = "accident"
KIND_OBSTACLE = "obstacle"
KIND_WORK = "work"
KIND_CONDITION = "condition"

_KIND_WORDS = (
    (KIND_ACCIDENT, re.compile(
        r"olyck|krock|kollision|avåkning|påkörning|voltat|singelolycka|"
        r"trafikolycka", re.I)),
    (KIND_OBSTACLE, re.compile(
        r"hinder|djur|älg|rådjur|vildsvin|\bren\b|renar|hjort|föremål|"
        r"\bträd\b|nedfallet|ras\b|jordskred|stillastående|fordonshaveri|"
        r"haveri|bärgning|fast i|brand|eld\b|tappad last|last på vägen", re.I)),
    (KIND_WORK, re.compile(
        r"arbete|vägarbete|beläggning|asfalt|underhåll|byggarbete|"
        r"körfältsavstängning|avstängning för|röjning|slåtter|besiktning", re.I)),
    (KIND_CONDITION, re.compile(
        r"halk|halt\b|snö|is på|översvämning|vatten på|dimma|storm", re.I)),
)


def kinds_in(text):
    """Kinds of incident a text speaks of: {'accident', 'obstacle', ...}."""
    text = text or ""
    return {k for k, rx in _KIND_WORDS if rx.search(text)}


def deviation_kinds(dev):
    kinds = kinds_in(" ".join(str(dev.get(k) or "") for k in (
        "message_code", "header", "message", "restriction")))
    mtype = dev.get("message_type")
    if mtype == "Olycka":
        kinds.add(KIND_ACCIDENT)
    elif mtype == "Hinder":
        kinds.add(KIND_OBSTACLE)
    if KIND_ACCIDENT in kinds or KIND_OBSTACLE in kinds:
        # "Olycka. Bärgningsarbete pågår" is an accident, not road works
        kinds.discard(KIND_WORK)
    return kinds


_ROAD_RE = re.compile(
    r"\b(?:(E)\s?(\d{1,2})|(?:väg|vag|rv|riksväg|lv|länsväg)\s*(\d{1,4}))\b",
    re.I)


def road_key(text):
    """'E18', 'E 18', 'Rv 61', 'Väg 924 från …' -> 'E18', '61', '924'."""
    m = _ROAD_RE.search(text or "")
    if not m:
        return None
    return f"E{int(m.group(2))}" if m.group(1) else str(int(m.group(3)))


def deviation_road(dev):
    """The road a deviation is on: from RoadNumber, else its description
    (RoadNumber is often empty: "Väg 924 från Valbo-Ryr till Rönningen")."""
    key = road_key(dev.get("road_number"))
    if key:
        return key
    num = dev.get("road_numeric")
    if num:
        return str(num)
    return road_key(dev.get("location"))


# ---------------------------------------------------------------------------
# Matching radio incidents to Trafikverket's deviations
# ---------------------------------------------------------------------------

LINKED_RADIUS_M = 300       # a linked incident is this precise
MATCH_SLACK_KM = 1.5        # a deviation this far outside the circle may match
RADIO_BEFORE_MIN = 45       # Trafikverket may publish this long after the radio
ENDED_BEFORE_MIN = 30       # a deviation that ended this long before: no match
PLANNED_DAYS = 2            # works created this long before are planned works
MIN_SCORE = 1.0

LAYER_TYPES = ("Olycka", "Hinder", "Viktig trafikinformation")


def _epoch(iso):
    if not iso:
        return None
    try:
        return calendar.timegm(time.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def _km_to_segment(p, a, b):
    """Distance in km from p to the segment a-b (lat, lon), flat earth."""
    k = math.cos(math.radians(p[0]))
    ax, ay = (a[1] - p[1]) * 111.32 * k, (a[0] - p[0]) * 110.57
    bx, by = (b[1] - p[1]) * 111.32 * k, (b[0] - p[0]) * 110.57
    dx, dy = bx - ax, by - ay
    seg = dx * dx + dy * dy
    t = 0.0 if seg == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg))
    return math.hypot(ax + t * dx, ay + t * dy)


def distance_km(dev, lat, lon):
    """How far (lat, lon) is from a deviation's point or stretch of road."""
    p = (lat, lon)
    best = None
    if dev.get("lat") is not None:
        best = _km_to_segment(p, (dev["lat"], dev["lon"]), (dev["lat"], dev["lon"]))
    line = dev.get("line") or []
    if isinstance(line, str):
        try:
            line = json.loads(line)
        except ValueError:
            line = []
    for a, b in zip(line, line[1:]):
        d = _km_to_segment(p, tuple(a), tuple(b))
        best = d if best is None else min(best, d)
    return best


def deviation_ended(dev, now_iso):
    if dev.get("status") == STATUS_DELETED:
        return True
    end = dev.get("end_time")
    return bool(end and not dev.get("until_further_notice")
                and _epoch(end) is not None and _epoch(now_iso) is not None
                and _epoch(end) < _epoch(now_iso))


def match_score(inc, dev, heard_at, text=""):
    """How well a radio incident fits a deviation: a score (higher is
    better, at least MIN_SCORE to link), or None when they cannot be the
    same.

    inc: an incidents row (road, lat, lon, radius_m, what); heard_at: when
    the radio spoke of it; text: the message, for the kind of incident.
    """
    if dev.get("lat") is None or inc.get("lat") is None:
        return None
    heard = _epoch(heard_at)
    # When: published long after the broadcast, or over before it
    published = _epoch(dev.get("created") or dev.get("start_time"))
    if heard is not None and published is not None:
        if published > heard + RADIO_BEFORE_MIN * 60:
            return None
    if heard is not None:
        if dev.get("status") == STATUS_DELETED:
            gone = _epoch(dev.get("changed_at"))
            if gone is not None and gone < heard - ENDED_BEFORE_MIN * 60:
                return None
        end = _epoch(dev.get("end_time"))
        if (end is not None and not dev.get("until_further_notice")
                and end < heard - ENDED_BEFORE_MIN * 60):
            return None
    # Where
    dist = distance_km(dev, inc["lat"], inc["lon"])
    limit = (inc.get("radius_m") or 0) / 1000 + MATCH_SLACK_KM
    if dist is None or dist > limit:
        return None
    # Which road
    r1, r2 = road_key(inc.get("road")), deviation_road(dev)
    if r1 and r2 and r1 != r2:
        return None
    same_road = bool(r1 and r2)
    # What
    radio_kinds = kinds_in(" ".join([text or "", inc.get("what") or ""]))
    dev_kinds = deviation_kinds(dev)
    same_kind = bool(radio_kinds & dev_kinds)
    if KIND_WORK in dev_kinds and KIND_WORK not in radio_kinds:
        if radio_kinds or (published is not None and heard is not None
                           and published < heard - PLANNED_DAYS * 86400):
            return None     # planned works are not what the radio is about
    if radio_kinds and dev_kinds and not same_kind:
        return None
    if not same_road and not same_kind:
        return None
    score = 1.0 - 0.5 * dist / limit
    score += 0.4 if same_road else 0.0
    score += 0.4 if same_kind else 0.0
    if heard is not None and published is not None \
            and heard - 3 * 3600 <= published <= heard + RADIO_BEFORE_MIN * 60:
        score += 0.2        # new around the time of the broadcast
    return score


def best_match(inc, devs, heard_at, text=""):
    """(deviation, score) that best fits inc, or (None, None)."""
    best, best_score = None, None
    for dev in devs:
        s = match_score(inc, dev, heard_at, text)
        if s is not None and s >= MIN_SCORE and (best_score is None or s > best_score):
            best, best_score = dev, s
    return best, best_score


def _candidates(conn, lat, lon, km=30):
    """Deviations around (lat, lon), from the database."""
    dlat = km / 110.0
    dlon = km / (111.0 * max(0.2, math.cos(math.radians(lat))))
    return [_row_dict(r) for r in conn.execute(
        "SELECT * FROM trv_deviations WHERE lat BETWEEN ? AND ? "
        "AND lon BETWEEN ? AND ?",
        (lat - dlat, lat + dlat, lon - dlon, lon + dlon))]


def _apply_link(conn, inc_id, dev, now_iso):
    """Move the incident to the deviation's place and link them."""
    sets = {"trv_id": dev["id"], "lat": dev["lat"], "lon": dev["lon"],
            "radius_m": LINKED_RADIUS_M, "level": "trafikverket"}
    if dev.get("direction") == "both":
        sets["both_directions"] = 1
    if deviation_ended(dev, now_iso):
        sets["status"] = "cleared"
    conn.execute("UPDATE incidents SET " + ", ".join(f"{k} = ?" for k in sets)
                 + " WHERE id = ?", [*sets.values(), inc_id])


def link_incidents(conn, incident_ids, heard_at, text=""):
    """Link newly placed radio incidents to Trafikverket's deviations.

    Called with the store's lock held, after merge_placements; the caller
    commits. Returns {incident_id: deviation_id} for the new links.
    """
    out = {}
    for inc_id in incident_ids:
        row = conn.execute("SELECT * FROM incidents WHERE id = ?",
                           (inc_id,)).fetchone()
        if row is None or row["trv_id"]:
            continue
        inc = dict(row)
        dev, _ = best_match(inc, _candidates(conn, inc["lat"], inc["lon"]),
                            heard_at, text)
        if dev is not None:
            _apply_link(conn, inc_id, dev, heard_at)
            out[inc_id] = dev["id"]
    return out


def relink_deviations(conn, dev_ids, now_iso, since_iso):
    """After new or changed deviations: link them to recent radio
    incidents that have none, and bring linked incidents up to date (an
    incident goes grey when Trafikverket ends it, and stays on the map
    while Trafikverket says it goes on).

    Called with the store's lock held; the caller commits. Returns
    (new_links {incident_id: deviation_id}, updated [incident_id, ...]).
    """
    new_links, updated = {}, []
    for dev_id in dev_ids:
        row = conn.execute("SELECT * FROM trv_deviations WHERE id = ?",
                           (dev_id,)).fetchone()
        if row is None:
            continue
        dev = _row_dict(row)
        ended = deviation_ended(dev, now_iso)
        for inc in conn.execute("SELECT * FROM incidents WHERE trv_id = ?",
                                (dev_id,)).fetchall():
            if ended and inc["status"] != "cleared":
                conn.execute("UPDATE incidents SET status = 'cleared', "
                             "last_seen = ? WHERE id = ?", (now_iso, inc["id"]))
                updated.append(inc["id"])
            elif not ended:
                conn.execute(
                    "UPDATE incidents SET last_seen = MAX(last_seen, ?), "
                    "lat = ?, lon = ? WHERE id = ?",
                    (now_iso, dev["lat"], dev["lon"], inc["id"]))
                updated.append(inc["id"])
        if ended or dev.get("lat") is None:
            continue
        # Radio incidents heard before Trafikverket published this one
        for inc in conn.execute(
                "SELECT * FROM incidents WHERE trv_id IS NULL AND last_seen >= ?",
                (since_iso,)).fetchall():
            inc = dict(inc)
            if inc["id"] in new_links:
                continue
            s = match_score(inc, dev, inc["first_seen"])
            if s is not None and s >= MIN_SCORE:
                _apply_link(conn, inc["id"], dev, now_iso)
                new_links[inc["id"]] = dev_id
    return new_links, updated


def summary(dev, versions=None):
    """What the web UI and push messages need of a linked deviation."""
    out = {k: dev.get(k) for k in (
        "id", "message_type", "message_code", "header", "message", "location",
        "severity_code", "severity_text", "restriction", "lanes",
        "temporary_limit", "direction", "lat", "lon", "line", "start_time",
        "end_time", "until_further_notice", "status", "web_link", "created",
        "changed_at")}
    if versions is not None:
        out["versions"] = versions
    return out


def attach(store, incidents, versions=True):
    """Add 'trafikverket' (the linked deviation, or None) to incident dicts."""
    now = store.now()
    cache = {}
    for inc in incidents:
        dev_id = inc.get("trv_id")
        if not dev_id:
            inc["trafikverket"] = None
            continue
        if dev_id not in cache:
            dev = store.get(dev_id, versions=False)
            if dev is not None:
                dev = summary(dev, store.versions(dev_id) if versions else None)
                dev["ended"] = deviation_ended(dev, now)
            cache[dev_id] = dev
        inc["trafikverket"] = cache[dev_id]
    return incidents


def layer_worthy(dev, now_iso):
    """Whether a deviation the radio has not mentioned goes on the map
    layer: accidents and obstacles, and anything else of great impact that
    is not road works."""
    if dev.get("lat") is None or dev.get("status") != STATUS_ACTIVE:
        return False
    if deviation_ended(dev, now_iso):
        return False
    kinds = deviation_kinds(dev)
    if dev.get("message_type") in LAYER_TYPES:
        return KIND_WORK not in kinds or KIND_ACCIDENT in kinds
    if kinds & {KIND_ACCIDENT, KIND_OBSTACLE}:
        return True
    return (dev.get("severity_code") or 0) >= 4 and KIND_WORK not in kinds


# ---------------------------------------------------------------------------
# Push messages for updates
# ---------------------------------------------------------------------------

PUSH_FIELDS = ("message", "severity_code", "restriction", "end_time", "status")
PUSH_MIN_INTERVAL_SEC = 600     # per deviation, unless it ends


def _local_time(iso, zone):
    t = _epoch(iso)
    if t is None:
        return None
    dt = datetime.fromtimestamp(t, timezone.utc)
    if zone is not None:
        dt = dt.astimezone(zone)
    return dt.strftime("%H:%M")


def update_text(dev, changes, zone=None, now_iso=None):
    """A short Swedish sentence about what changed, for a push message."""
    if dev.get("status") == STATUS_DELETED or (
            now_iso and deviation_ended(dev, now_iso)):
        return "Avslutad enligt Trafikverket."
    parts = []
    if "message" in changes and dev.get("message"):
        parts.append(dev["message"].strip())
    if "severity_code" in changes and dev.get("severity_text"):
        parts.append(f"{dev['severity_text']}.")
    if "restriction" in changes and dev.get("restriction"):
        parts.append(f"{dev['restriction']}.")
    if "end_time" in changes and dev.get("end_time"):
        hhmm = _local_time(dev["end_time"], zone)
        if hhmm:
            parts.append(f"Beräknas klart {hhmm}.")
    if changes.get("status") == STATUS_SUSPENDED:
        parts.append("Tillfälligt inaktiv.")
    return " ".join(p if p.endswith((".", "!", "?")) else p + "." for p in parts)


class UpdateGate:
    """Which deviation changes are worth a push: the fields people care
    about, not more often than every ten minutes (the end always)."""

    def __init__(self, interval=PUSH_MIN_INTERVAL_SEC):
        self.interval = interval
        self._last = {}
        self._lock = threading.Lock()

    def worth(self, dev_id, changes, ended, now=None):
        if not ended and not any(k in changes for k in PUSH_FIELDS):
            return False
        now = time.time() if now is None else now
        with self._lock:
            last = self._last.get(dev_id)
            if not ended and last is not None and now - last < self.interval:
                return False
            self._last[dev_id] = now
            if len(self._last) > 5000:
                cutoff = now - 86400
                self._last = {k: v for k, v in self._last.items() if v >= cutoff}
        return True


def latest_changes(store, dev_id, after_vid):
    """The fields changed in the versions numbered above after_vid."""
    changes = {}
    for v in store.versions(dev_id):
        if v["vid"] > after_vid:
            changes.update(v["changes"])
    return changes


def update_payload(dev, changes, incident, rule, event_ref, public_url="",
                   zone=None, now_iso=None):
    """The push message for an update of an incident a rule was sent for."""
    from datetime import datetime as _dt
    text = update_text(dev, changes, zone, now_iso)
    label = incident.get("label") or dev.get("location") or "Trafikhändelse"
    base = (public_url or "").rstrip("/")
    ended = dev.get("status") == STATUS_DELETED or bool(
        now_iso and deviation_ended(dev, now_iso))
    return {
        "title": f"Trafik, uppdatering: {label}",
        "message": text or (dev.get("message") or "").strip(),
        "priority": rule.get("priority", "normal"),
        "event_type": "traffic_update",
        "rule": {"id": rule.get("id"), "name": rule.get("name")},
        "event_id": event_ref,
        "incident_id": incident.get("id"),
        "url": f"{base}/#/karta?i={incident.get('id')}" if base else None,
        "trafikverket": {"id": dev.get("id"), "type": dev.get("message_type"),
                         "severity": dev.get("severity_text"),
                         "ended": ended, "web_link": dev.get("web_link")},
        "test": False,
        "timestamp": _dt.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------

class TrafikverketFeed:
    """Background thread that keeps a TrvStore up to date.

    centers_fn returns the (lat, lon) points to watch around (the node, or
    an aggregator's nodes); nothing is fetched while it returns none.
    on_change(ids, after_vid) is called after new or changed deviations were
    stored; their versions from this poll are those numbered above after_vid.
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
        before = self._store.last_version_id()
        changed = self._store.apply(situations, full=full)
        self._store.touch_linked()
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
                    self._on_change(changed, before)
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
