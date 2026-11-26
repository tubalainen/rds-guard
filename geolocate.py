"""Geolocate traffic messages: which incidents a message mentions, and where.

One message can mention several incidents ("E18 vid Bergviksmotet … Sedan
riksväg 26 vid Björneborg …"). Each is turned into a placement: a point, a
radius that grows with the uncertainty, and the direction of traffic it
affects. Placements are merged into incidents, which are what the map shows.

Two engines read the text; they produce the same kind of result:
  - "rules": patterns for how P4's traffic desk phrases things ("mellan A och
    B", "i höjd med X", "i riktning mot Y"). Local, free, always available.
  - "claude": the Claude API reads the text, fixes misheard words and names,
    and returns the corrected text as well. Optional (ANTHROPIC_API_KEY); on
    any error the rules engine is used instead.

Either way, coordinates only ever come from the local place-name registry
(gazetteer.py): Claude picks names, it never invents positions.
"""

import calendar
import json
import logging
import re
import time
from dataclasses import dataclass, field

import gazetteer as gz

log = logging.getLogger("rds-guard")

MAX_RADIUS_M = 25000          # less certain than this: not placed
INCIDENT_HOURS = 12           # an incident stays on the map this long
# Besides NULL (stored before v0.5.0), messages with these geo_status values
# were never placed: the receiver's location was not known then
PENDING_STATUSES = ("no_location",)
DEFAULT_AREA_KM = 200         # names further than this from the node are ignored

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class Mention:
    """What a message says about one incident (names, not coordinates)."""
    road: str = None
    town: str = None
    between: tuple = None           # (name, name)
    between_all: list = field(default_factory=list)
    places: list = field(default_factory=list)
    beside: list = field(default_factory=list)    # "norr om X"
    emphasised: list = field(default_factory=list)
    toward: str = None              # "i riktning mot X"
    coming_from: str = None         # "om man kommer från X"
    both_directions: bool = False
    what: str = ""
    cleared: bool = False
    updates_incident: int = None


@dataclass
class Placement:
    lat: float
    lon: float
    radius_m: int
    road: str = None
    label: str = ""
    what: str = ""
    bearing: int = None             # direction of the affected traffic
    both_directions: bool = False
    cleared: bool = False
    level: str = ""                 # how it was placed, for the UI
    names: list = field(default_factory=list)
    updates_incident: int = None


@dataclass
class GeoResult:
    engine: str                     # engine that produced the result
    placements: list
    corrected_text: str = None
    error: str = None


# ---------------------------------------------------------------------------
# Rules engine: text -> mentions
# ---------------------------------------------------------------------------

ROAD_RE = re.compile(
    r"\b(?:(E)\s?(\d{1,2})|(riks\w*|rv|länsväg|lv|väg)\s+(\d{1,4}))\b",
    re.IGNORECASE)

_STOP = set("""
där det är i och så som har mot ska en ett då nu med på till från strax ungefär
här alltså när man vägen höjd riktning riktningen den de vi du om att inte för var
kan men eller vid efter innan igen just också ni jag sig alla både båda andra hela
lite mer mellan bron höger vänster nära läget tunneln rondellen väg hindret
klockan gränsen norska ungefär närmare bestämt detta infarten avfarten påfarten
trafiken köerna olyckan stan platsen området hållet håll norr söder öster väster
centrum körfält körfältet sedan vidare tack tipsa ring numret trafikredaktionen
olycka kö köer stopp korsningen
""".split())
_FILLER = {"trafikplats", "trafikplatsen", "tpl"}

# A place phrase after one of these says where the incident is
_TRIGGERS = (r"i höjd med|i höjd|höjd med|i anslutning till|strax efter|"
             r"strax innan|strax före|vid|i|på|med|genom")
# ... after these, it is next to the incident, not at it
_BESIDE = r"norr om|söder om|öster om|väster om|norr|söder"
_WHAT_WORDS = re.compile(
    r"olyck|krock|träd|älg|vilt|djur|halk|halt|stopp|kö|lastbil|personbil|"
    r"avstängd|blocker|vägarbete|räckes|brand|eld|bärgning|stillastående|"
    r"körfält|framkomlig|öppet|öppen", re.IGNORECASE)
_CLEARED = re.compile(
    r"öppe[tn] igen|öppnat (upp )?igen|fri och framkomlig|helt framkomlig|"
    r"framkomligt igen|är borta|har lösts|är klar\b", re.IGNORECASE)
_BOTH = re.compile(r"b[åa]d[ae]?\s+(riktning|håll)|åt båda|i båda",
                   re.IGNORECASE)
_NEW_PART = re.compile(r"^\s*(sedan|vidare|kvar på|över till|dessutom|"
                       r"även|och så|till sist)\b", re.IGNORECASE)


def norm_road(text):
    """'riksväg 61', 'Riksdags 61', 'E 18' -> 'Rv 61', 'E18'."""
    m = ROAD_RE.search(text or "")
    if not m:
        return None
    if m.group(1):
        return f"E{int(m.group(2))}"
    kind, num = m.group(3).lower(), int(m.group(4))
    if kind.startswith("riks") or kind == "rv":
        return f"Rv {num}"
    if kind in ("länsväg", "lv") or num >= 100:
        return f"Lv {num}"
    return f"Väg {num}"


def _phrase(text, max_words=3):
    """Up to three words that can be a name, cut at punctuation or a stop word."""
    words = []
    for m in re.finditer(r"\s*([,.!?;:–—]|[\wåäöÅÄÖéü\-]+)", text):
        w = m.group(1)
        if not words and w.lower() in _FILLER:
            continue  # "trafikplats Odlingen" -> "Odlingen"
        if (not re.match(r"[\wåäöÅÄÖ]", w) or w.lower() in _STOP
                or w.isdigit() or len(words) == max_words
                or re.fullmatch(r"(?i)e\d{1,2}|riks\w*|länsväg|rv|lv", w)):
            break
        words.append(w)
    return " ".join(words)


def _sentences(text):
    text = re.sub(r"\s*–\s*–\s*", " ", text or "")
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _chunks(text):
    """Group sentences into parts that are about the same road and place."""
    chunks = []
    for s in _sentences(text):
        road = norm_road(s)
        cur = chunks[-1] if chunks else None
        new = cur is None or (road and cur["road"] and road != cur["road"])
        new = new or (road and _NEW_PART.search(s) and cur["text"].strip())
        if new:
            cur = {"road": road, "text": ""}
            chunks.append(cur)
        cur["road"] = cur["road"] or road
        cur["text"] += " " + s
    return chunks


def _what(text):
    for s in _sentences(text):
        if _WHAT_WORDS.search(s):
            return s.strip()[:160]
    return ""


def _add(lst, p):
    if p and p.lower() not in (x.lower() for x in lst):
        lst.append(p)


def extract_rules(text):
    """Mentions in a transcription, found with patterns only."""
    out = []
    for ch in _chunks(text):
        t = ch["text"]
        m = Mention(road=ch["road"], what=_what(t),
                    both_directions=bool(_BOTH.search(t)),
                    cleared=bool(_CLEARED.search(t)))
        for pair in re.finditer(r"(?=\bmellan\s+(.{2,40}?)\s+(?:och|till)\s+(.{2,40}))",
                                t, re.IGNORECASE):
            a, b = _phrase(pair.group(1)), _phrase(pair.group(2))
            if a and b and a.lower() != b.lower():
                m.between_all.append((a, b))
        m.between = m.between_all[0] if m.between_all else None
        road_next = re.search(ROAD_RE.pattern + r"(?:,|\s+i|\s+vid|\s+på)?\s+(.{2,40})",
                              t, re.IGNORECASE)
        if road_next:
            m.town = _phrase(road_next.group(5)) or None
        for mm in re.finditer(r"(?=\b(?:" + _TRIGGERS + r")\s+(.{2,40}))", t,
                              re.IGNORECASE):
            _add(m.places, _phrase(mm.group(1)))
        for mm in re.finditer(r"(?=\b(?:" + _BESIDE + r")\s+(.{2,40}))", t,
                              re.IGNORECASE):
            _add(m.beside, _phrase(mm.group(1)))
        # A name opening a sentence: "Skatscher mellan WC och Karlstad",
        # "Nygängsrundellen, E18, Grums"
        for s in _sentences(t):
            p = _phrase(s.strip())
            rest = s.strip()[len(p):].lstrip()
            if (p and p[0].isupper() and not norm_road(p)
                    and re.match(r"(,|mellan\b|vid\b|i höjd\b)", rest)):
                _add(m.places, p)
        names_between = {x.lower() for pair in m.between_all for x in pair}
        m.places = [p for p in m.places if p.lower() not in m_lower(m.beside)
                    and (p.lower() != (m.town or "").lower())]
        m.emphasised = [p for p in m.places if p.lower() in names_between]
        tw = re.search(r"riktning(?:en)? (?:in )?mot\s+(.{2,40})|"
                       r"på väg mot\s+(.{2,40})|\bmot\s+([A-ZÅÄÖ].{1,40})", t)
        if tw:
            m.toward = _phrase(next(g for g in tw.groups() if g)) or None
        fr = re.search(r"kommer från\s+(.{2,40})|från\s+(.{2,40}?)\s+mot\b", t,
                       re.IGNORECASE)
        if fr and not m.toward:
            m.coming_from = _phrase(next(g for g in fr.groups() if g)) or None
        if m.road or m.between or m.places or m.town:
            out.append(m)
    return out


def m_lower(items):
    return {x.lower() for x in items}


# ---------------------------------------------------------------------------
# Resolver: mention + registry -> placement (shared by both engines)
# ---------------------------------------------------------------------------

def _best(matches, key):
    return max(matches, key=key) if matches else None


FUZZY_MIN = 0.84        # how alike two names must sound to count as the same


def resolve(mention, gaz, center, area_km=DEFAULT_AREA_KM):
    """Turn a mention into a placement, or None if it cannot be placed.

    Precision decides the radius: a junction or roundabout pins it down to a
    few hundred metres, two places a few km apart to the stretch between
    them, a road between two towns to half that distance, a town alone to
    5 km. A name that only sounded right (not spelt right) widens it by half.
    """
    if center is None:
        return None
    used = []

    def look(name, kinds=None):
        # Names written in lower case are common words as often as names:
        # those must be spelt right; capitalised ones may be misheard
        if not name:
            return []
        fuzzy = name[0].isupper() or " " in name.strip()
        return gaz.search(name, near=center, max_km=area_km, limit=8,
                          min_score=FUZZY_MIN if fuzzy else 1.01, kinds=kinds)

    # 1. The stretch "mellan A och B" (the shortest one that resolves)
    seg = None
    for a_name, b_name in (mention.between_all or
                           ([mention.between] if mention.between else [])):
        ca, cb = look(a_name), look(b_name)
        pairs = [(a, b) for a in ca for b in cb
                 if 1 <= gz.km(a.place.point, b.place.point) <= 50]
        best = _best(pairs, lambda ab: (
            ab[0].score + ab[1].score + 0.03 * (ab[0].place.weight + ab[1].place.weight),
            -gz.km(ab[0].place.point, ab[1].place.point)))
        if best and (seg is None or gz.km(best[0].place.point, best[1].place.point)
                     < gz.km(seg[0].place.point, seg[1].place.point)):
            seg = best
    places = list(mention.places)
    if seg:
        used += list(seg)
    seg_names = {m.heard.lower() for m in seg} if seg else set()
    for pair in (mention.between_all or ([mention.between] if mention.between else [])):
        for n in pair:
            if n.lower() not in seg_names:
                _add(places, n)

    # 2. The town the announcer gives ("E18 i Karlstad")
    town = None
    best = look(mention.town)[:1] if mention.town else []
    if best and best[0].place.local and best[0].place.kind not in gz.SETTLEMENTS:
        # A spot added by hand ("E18 vid Stolpen"), not a town
        _add(places, mention.town)
    elif mention.town:
        ms = look(mention.town, kinds=gz.SETTLEMENTS)
        if seg:
            ms = [m for m in ms if gz.segment_km(m.place.point, seg[0].place.point,
                                                 seg[1].place.point) <= 15]
        town = _best(ms, lambda m: (m.score + 0.03 * m.place.weight,
                                    -gz.km(center, m.place.point)))
        if town is None:
            # "E18 vid Bergviksmotet": the word after the road is a place
            _add(places, mention.town)

    def near_anchor(p, tw):
        if seg:
            a, b = seg[0].place.point, seg[1].place.point
            return gz.segment_km(p.point, a, b) <= max(6, 0.35 * gz.km(a, b))
        if tw:
            return gz.km(tw.place.point, p.point) <= 15
        return True

    # 3. Places that pin it down, near the stretch or the town
    def pick_landmarks(tw):
        found, taken = [], {m.place for m in used} | ({tw.place} if tw else set())
        for name in places:
            if seg and name.lower() in {m.heard.lower() for m in seg}:
                # "mellan Årjäng och Töcksfors, strax innan Töcksfors"
                end = next(m for m in seg if m.heard.lower() == name.lower())
                found.append(end)
                continue
            ms = [m for m in look(name) if near_anchor(m.place, tw)
                  and m.place not in taken]
            m = _best(ms, lambda m: (m.score + 0.02 * m.place.weight
                                     - 0.002 * gz.km(anchor(tw), m.place.point)))
            if m:
                found.append(m)
                taken.add(m.place)
        return found

    def anchor(tw):
        if seg:
            return gz.midpoint(seg[0].place.point, seg[1].place.point)
        return tw.place.point if tw else center

    landmarks = pick_landmarks(town)
    if town:
        # The town may be the misheard part ("på Skoga" for Karlskoga): a
        # landmark that matches clearly better away from it wins
        def top(ms):
            return max((m.score for m in ms if m.place.radius_m <= 2500), default=0)
        alone = pick_landmarks(None)
        if top(alone) >= 0.95 and top(alone) > top(landmarks) + 0.03:
            town, landmarks = None, alone
    if town:
        used.append(town)
    used += [m for m in landmarks if m not in used]

    fine = sorted((m for m in landmarks if m.place.radius_m <= 2500),
                  key=lambda m: m.place.radius_m)
    coarse = [m for m in landmarks if m.place.radius_m > 2500]
    beside = [m for n in mention.beside for m in look(n)[:1] if near_anchor(m.place, town)]
    names = []
    if len(fine) >= 2 and gz.km(fine[0].place.point, fine[1].place.point) <= 10:
        a, b = fine[0].place.point, fine[1].place.point
        point, radius = gz.midpoint(a, b), gz.km(a, b) * 500 + 300
        level, names = "mellan två platser", [fine[0], fine[1]]
    elif fine:
        f = fine[0]
        point, radius = f.place.point, f.place.radius_m
        if seg and f in seg:
            radius = max(radius, 2000)  # "strax innan Töcksfors"
        level, names = "nära en plats", [f]
    elif seg:
        a, b = seg[0].place.point, seg[1].place.point
        point, radius = gz.midpoint(a, b), gz.km(a, b) * 500
        level, names = "på en sträcka", list(seg)
    elif coarse:
        c = coarse[0]
        point, radius = c.place.point, c.place.radius_m
        level, names = "nära en plats", [c]
    elif beside:
        b = beside[0]
        point, radius = b.place.point, max(5000, b.place.radius_m)
        level, names = "nära en plats", [b]
        used.append(b)
    elif town:
        point, radius = town.place.point, max(5000, town.place.radius_m)
        level, names = "i eller nära en ort", [town]
    else:
        return None
    if any(not m.exact for m in names):
        radius *= 1.2 if level == "på en sträcka" else 1.5
    if radius > MAX_RADIUS_M:
        return None

    pl = Placement(lat=round(point[0], 5), lon=round(point[1], 5),
                   radius_m=int(round(radius, -1)), road=mention.road,
                   what=mention.what, both_directions=mention.both_directions,
                   cleared=mention.cleared, level=level,
                   names=list(dict.fromkeys(m.place.name for m in used)),
                   updates_incident=mention.updates_incident)
    if seg and names == list(seg):
        place_label = f"{seg[0].place.name}–{seg[1].place.name}"
    else:
        place_label = names[0].place.name
    pl.label = " ".join(x for x in (mention.road, place_label) if x)

    if not mention.both_directions:
        for name, reverse in ((mention.toward, False), (mention.coming_from, True)):
            if not name:
                continue
            ms = [m for m in look(name) if gz.km(point, m.place.point) > 0.5]
            m = _best(ms, lambda m: (m.score + 0.03 * m.place.weight,
                                     -gz.km(point, m.place.point)))
            if m:
                b = (gz.bearing(m.place.point, point) if reverse
                     else gz.bearing(point, m.place.point))
                pl.bearing = int(round(b))
                break
    return pl


# ---------------------------------------------------------------------------
# Claude engine (optional)
# ---------------------------------------------------------------------------

CLAUDE_SYSTEM = """You read automatic transcriptions of Swedish radio traffic \
announcements (Sveriges Radio P4) and extract where each traffic incident is.

The transcription is made by speech recognition and often mishears place names \
and words: "WC" can be "Väse", "Skatscher" can be "Skattkärr", "Kallskoga" is \
"Karlskoga", "eld" can be "älg", "Riksdags 61" is "riksväg 61".

You get the transcription, the receiver's location, a list of real place names \
near the receiver that sound like words in the text, and the incidents that are \
already open.

Return:
- corrected_text: the transcription with clear recognition errors fixed (place \
names, road numbers, misheard words). Keep the wording and sentences; do not \
summarise, add facts or remove anything. When unsure, keep the original word.
- incidents: one entry per distinct traffic problem and place. Ignore everything \
that is not road traffic (sport, music, news, phone numbers, trains unless a road \
is affected). For each:
  - road: as said, e.g. "E18", "Rv 61", "Lv 734", or null.
  - town: the town the announcer uses to say where on the road, or null.
  - between: the two places in "mellan A och B", or null.
  - places: other place names that pin it down (junctions, roundabouts, \
villages, rest areas, bridges, tunnels), most precise first.
  - toward: the place in "i riktning mot X" for the affected direction, or null.
  - coming_from: the place in "om man kommer från X", or null.
  - both_directions: true when both directions are affected.
  - what: a short Swedish description, e.g. "Olycka med lastbil, vägen avstängd".
  - cleared: true when the message says the road is open again or the \
problem is gone.
  - updates_incident: the id of an open incident this is about, or null.
Write every place name as the matching name in the list of real places when one \
fits; only use another name when the text clearly says it."""

_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NULLABLE_INT = {"anyOf": [{"type": "integer"}, {"type": "null"}]}

CLAUDE_SCHEMA = {
    "type": "object",
    "properties": {
        "corrected_text": {"type": "string"},
        "incidents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "road": _NULLABLE_STR,
                    "town": _NULLABLE_STR,
                    "between": {"anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"}]},
                    "places": {"type": "array", "items": {"type": "string"}},
                    "toward": _NULLABLE_STR,
                    "coming_from": _NULLABLE_STR,
                    "both_directions": {"type": "boolean"},
                    "what": {"type": "string"},
                    "cleared": {"type": "boolean"},
                    "updates_incident": _NULLABLE_INT,
                },
                "required": ["road", "town", "between", "places", "toward",
                             "coming_from", "both_directions", "what",
                             "cleared", "updates_incident"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["corrected_text", "incidents"],
    "additionalProperties": False,
}


def candidate_names(text, gaz, center, area_km=DEFAULT_AREA_KM, limit=80):
    """Real place names near the node that sound like words in the text."""
    words = re.findall(r"[\wåäöÅÄÖéü\-]+", text or "")
    seen, out = set(), []

    def add(p):
        key = (p.name, p.kind)
        if key not in seen:
            seen.add(key)
            out.append(p)

    for n in (3, 2, 1):
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            if any(w.lower() in _STOP or w.isdigit() for w in gram):
                continue
            phrase = " ".join(gram)
            if len(phrase) < 4:
                continue
            for m in gaz.search(phrase, near=center, max_km=area_km, limit=2,
                                min_score=0.82):
                add(m.place)
    for p in gaz.nearby(center, 100, kinds=("city", "town"), limit=25):
        add(p)
    return out[:limit]


class ClaudeReader:
    """Reads a transcription with the Claude API (structured JSON output)."""

    def __init__(self, api_key, model="claude-haiku-5-5", timeout=60):
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout,
                                           max_retries=2)
        self.model = model
        self.last_usage = None   # token counts of the latest call

    def read(self, text, radiotext, location_label, candidates, open_incidents):
        """Returns (corrected_text, [Mention]). Raises on any failure."""
        payload = {
            "transcription": text,
            "radiotext": radiotext or [],
            "receiver_location": location_label,
            "real_places_nearby": [
                f"{p.name} ({p.kind}, {gz.COUNTIES.get(p.county, p.county)})"
                for p in candidates],
            "open_incidents": [
                {"id": i["id"], "where": i["label"], "what": i["what"],
                 "last_mentioned": i["last_seen"]} for i in open_incidents],
        }
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=8000,
            system=CLAUDE_SYSTEM,
            messages=[{"role": "user",
                       "content": json.dumps(payload, ensure_ascii=False)}],
            output_config={"format": {"type": "json_schema",
                                      "schema": CLAUDE_SCHEMA}},
        )
        self.last_usage = getattr(resp, "usage", None)
        if resp.stop_reason != "end_turn":
            raise RuntimeError(f"Claude stopped: {resp.stop_reason}")
        data = json.loads(next(b.text for b in resp.content if b.type == "text"))
        mentions = []
        for it in data.get("incidents", []):
            between = it.get("between")
            mentions.append(Mention(
                road=norm_road(it.get("road") or "") or it.get("road"),
                town=it.get("town"),
                between=tuple(between[:2]) if between and len(between) >= 2 else None,
                places=[p for p in it.get("places") or [] if p],
                toward=it.get("toward"), coming_from=it.get("coming_from"),
                both_directions=bool(it.get("both_directions")),
                what=(it.get("what") or "")[:160],
                cleared=bool(it.get("cleared")),
                updates_incident=it.get("updates_incident")))
        corrected = (data.get("corrected_text") or "").strip() or None
        return corrected, mentions


# ---------------------------------------------------------------------------
# Facade used by the satellite and the aggregator
# ---------------------------------------------------------------------------

def create_geolocator(engine, api_key="", model="claude-haiku-5-5",
                      timeout=60, area_km=DEFAULT_AREA_KM):
    """Factory with the same fallback rules as the transcriber.

    "claude" without an API key runs as "rules"; "none" returns None.
    """
    engine = (engine or "rules").strip().lower()
    if engine in ("none", "off", "false", ""):
        log.info("Geolocation: disabled")
        return None
    reader = None
    if engine == "claude":
        if api_key:
            try:
                reader = ClaudeReader(api_key, model=model, timeout=timeout)
            except ImportError:
                log.error("Geolocation: the anthropic package is missing, "
                          "using the rules engine")
        else:
            log.info("Geolocation: no ANTHROPIC_API_KEY, using the rules engine")
    elif engine != "rules":
        log.warning("Geolocation: unknown GEO_ENGINE '%s', using rules", engine)
    log.info("Geolocation engine: %s", f"claude ({model}), fallback rules"
             if reader else "rules (local)")
    return Geolocator(reader, area_km)


class Geolocator:
    def __init__(self, reader=None, area_km=DEFAULT_AREA_KM):
        self._reader = reader
        self.area_km = area_km

    @property
    def engine(self):
        return "claude" if self._reader else "rules"

    def locate(self, text, gaz, center, location_label="", radiotext=None,
               open_incidents=()):
        """Read a transcription and place its incidents."""
        engine, corrected, error, mentions = "rules", None, None, None
        if self._reader and text and text.strip():
            try:
                cands = candidate_names(text, gaz, center, self.area_km)
                corrected, mentions = self._reader.read(
                    text, radiotext, location_label, cands, list(open_incidents))
                engine = "claude"
            except Exception as e:  # noqa: BLE001 - any API error -> rules
                error = f"{type(e).__name__}: {e}"[:300]
                log.warning("Claude geolocation failed (%s), using rules", error)
        if mentions is None:
            mentions = extract_rules(text or "")
        placements = []
        for m in mentions:
            p = resolve(m, gaz, center, self.area_km)
            if p:
                placements.append(p)
        return GeoResult(engine, placements, corrected, error)


# ---------------------------------------------------------------------------
# Incident storage (SQLite, shared by the satellite and the aggregator)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    road            TEXT,
    label           TEXT NOT NULL,
    what            TEXT,
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    radius_m        INTEGER NOT NULL,
    bearing         INTEGER,
    both_directions INTEGER NOT NULL DEFAULT 0,
    level           TEXT,
    names           TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'active',
    engine          TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_last_seen ON incidents(last_seen);
CREATE TABLE IF NOT EXISTS incident_events (
    incident_id     INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    event_key       TEXT NOT NULL,
    seen_at         TEXT NOT NULL,
    PRIMARY KEY (incident_id, event_key)
);
CREATE INDEX IF NOT EXISTS idx_incident_events_key ON incident_events(event_key);
"""


def init_tables(conn):
    conn.executescript(SCHEMA)


def _incident_dict(row):
    d = dict(row)
    d["both_directions"] = bool(d["both_directions"])
    try:
        d["names"] = json.loads(d.get("names") or "[]")
    except ValueError:
        d["names"] = []
    return d


def open_incidents(conn, since_iso, until_iso=None):
    """Active incidents mentioned since since_iso. until_iso leaves out the
    ones first heard after it (placing an older message afterwards)."""
    sql, args = ("SELECT * FROM incidents WHERE last_seen >= ? "
                 "AND status = 'active'", [since_iso])
    if until_iso:
        sql += " AND first_seen <= ?"
        args.append(until_iso)
    rows = conn.execute(sql + " ORDER BY last_seen DESC LIMIT 20",
                        args).fetchall()
    return [_incident_dict(r) for r in rows]


PRECISE_M = 2500      # a placement this precise names a spot, not an area


def _match(inc, pl):
    """How well a placement fits an existing incident: a distance in km
    (smaller is better), or None when they are different incidents.

    Two precise places (junctions, villages) are the same incident only when
    they are close together. A coarse placement ("E18 i Karlstad") belongs
    to an incident when their circles overlap.
    """
    if inc["road"] and pl.road and inc["road"] != pl.road:
        return None
    dist = gz.km((inc["lat"], inc["lon"]), (pl.lat, pl.lon))
    r1, r2 = inc["radius_m"] / 1000, pl.radius_m / 1000
    if max(r1, r2) > 10 and min(r1, r2) * 1000 <= PRECISE_M:
        return None  # "somewhere on 40 km of road" says too little to join
    if r1 * 1000 <= PRECISE_M and r2 * 1000 <= PRECISE_M:
        limit = max(2.0, r1 + r2)
    else:
        limit = r1 + r2
    return dist if dist <= limit else None


def _shift_iso(iso, hours):
    """An ISO timestamp (with or without a trailing Z) moved by hours."""
    t = calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t + hours * 3600)) + iso[19:]


def merge_placements(conn, placements, event_key, now_iso, since_iso, engine):
    """Store placements: update the incident they belong to or add a new one.

    The caller holds its write lock and commits. Returns the incident ids.

    now_iso is when the message was broadcast. It is in the past when older
    messages are placed afterwards: then incidents heard of up to 12 hours
    later can match too, and a message older than an incident's latest one
    does not change its status or description, only when it was first heard.
    """
    recent = [_incident_dict(r) for r in conn.execute(
        "SELECT * FROM incidents WHERE last_seen >= ? AND first_seen <= ?",
        (since_iso, _shift_iso(now_iso, INCIDENT_HOURS)))]
    ids = []
    for pl in placements:
        target = None
        if pl.updates_incident:
            target = next((i for i in recent if i["id"] == pl.updates_incident
                           and gz.km((i["lat"], i["lon"]), (pl.lat, pl.lon)) <= 30),
                          None)
        if target is None:
            fits = [(d, i) for i in recent if (d := _match(i, pl)) is not None]
            target = min(fits, key=lambda di: di[0])[1] if fits else None
        status = "cleared" if pl.cleared else "active"
        if target is None:
            cur = conn.execute(
                "INSERT INTO incidents (road, label, what, lat, lon, radius_m, "
                "bearing, both_directions, level, names, status, engine, "
                "first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pl.road, pl.label, pl.what, pl.lat, pl.lon, pl.radius_m,
                 pl.bearing, int(pl.both_directions), pl.level,
                 json.dumps(pl.names, ensure_ascii=False), status, engine,
                 now_iso, now_iso))
            inc_id = cur.lastrowid
            recent.append({"id": inc_id, "road": pl.road, "lat": pl.lat,
                           "lon": pl.lon, "radius_m": pl.radius_m,
                           "first_seen": now_iso, "last_seen": now_iso})
        else:
            inc_id = target["id"]
            if now_iso >= (target.get("last_seen") or ""):
                sets = {"last_seen": now_iso, "status": status, "engine": engine}
                if pl.what:
                    sets["what"] = pl.what
                if pl.bearing is not None or pl.both_directions:
                    sets["bearing"] = pl.bearing
                    sets["both_directions"] = int(pl.both_directions)
                target["last_seen"] = now_iso
            else:
                # An older message about it: the later ones say more
                sets = {}
            if now_iso < (target.get("first_seen") or now_iso):
                sets["first_seen"] = now_iso
                target["first_seen"] = now_iso
            dist_m = gz.km((target["lat"], target["lon"]), (pl.lat, pl.lon)) * 1000
            if (pl.radius_m < target["radius_m"]
                    and dist_m <= target["radius_m"] + pl.radius_m):
                # A more precise placement of the same incident moves the pin
                sets.update(lat=pl.lat, lon=pl.lon, radius_m=pl.radius_m,
                            label=pl.label, level=pl.level,
                            names=json.dumps(pl.names, ensure_ascii=False))
                target.update(lat=pl.lat, lon=pl.lon, radius_m=pl.radius_m)
            if sets:
                conn.execute(
                    "UPDATE incidents SET " + ", ".join(f"{k} = ?" for k in sets)
                    + " WHERE id = ?", [*sets.values(), inc_id])
        conn.execute(
            "INSERT OR REPLACE INTO incident_events (incident_id, event_key, "
            "seen_at) VALUES (?, ?, ?)", (inc_id, str(event_key), now_iso))
        ids.append(inc_id)
    return ids


def list_incidents(conn, since_iso=None):
    """Incidents mentioned since since_iso (None: all), newest first, with
    their events."""
    rows = [_incident_dict(r) for r in conn.execute(
        "SELECT * FROM incidents WHERE last_seen >= ? ORDER BY last_seen DESC",
        (since_iso or "",))]
    for inc in rows:
        inc["events"] = [r[0] for r in conn.execute(
            "SELECT event_key FROM incident_events WHERE incident_id = ? "
            "ORDER BY seen_at DESC", (inc["id"],))]
    return rows


def incidents_for_events(conn, event_keys):
    """{event_key: [incident summary, ...]} for the given events."""
    keys = [str(k) for k in event_keys]
    out = {k: [] for k in keys}
    for i in range(0, len(keys), 500):
        part = keys[i:i + 500]
        for r in conn.execute(
                "SELECT ie.event_key, i.id, i.label, i.lat, i.lon, i.radius_m, "
                "i.bearing, i.both_directions, i.status, i.last_seen "
                "FROM incident_events ie JOIN incidents i ON i.id = ie.incident_id "
                f"WHERE ie.event_key IN ({','.join('?' * len(part))})", part):
            d = dict(r)
            d["both_directions"] = bool(d["both_directions"])
            out[d.pop("event_key")].append(d)
    return out


def recent_names(conn, since_iso, limit=60):
    """Place names from recent incidents, most mentioned first."""
    counts = {}
    for (names,) in conn.execute(
            "SELECT names FROM incidents WHERE last_seen >= ?", (since_iso,)):
        try:
            for n in json.loads(names or "[]"):
                counts[n] = counts.get(n, 0) + 1
        except ValueError:
            continue
    return [n for n, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:limit]


def purge_incidents(conn, before_iso):
    conn.execute("DELETE FROM incident_events WHERE incident_id IN "
                 "(SELECT id FROM incidents WHERE last_seen < ?)", (before_iso,))
    conn.execute("DELETE FROM incidents WHERE last_seen < ?", (before_iso,))
