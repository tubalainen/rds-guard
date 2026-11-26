"""Place-name registry (gazetteer) for geolocating traffic messages.

The registry is a list of Swedish place names from OpenStreetMap (towns,
villages, motorway junctions, roundabouts, tunnels, bridges, rest areas,
lakes) built by tools/build_gazetteer.py and bundled in the Docker image,
so lookups run locally without any network access.

It is used for four things:
  - finding the node's own location from NODE_LOCATION ("Örebro, Örebro län")
  - correcting misheard names: "Skatscher" -> Skattkärr, "Voxnäs motet" ->
    Våxnäsmotet (Swedish sound-alike key + edit distance)
  - coordinates for the names a traffic message mentions
  - word lists that make the transcription hear local names right
    (ElevenLabs keyterms, local Whisper prompt)

Data (c) OpenStreetMap contributors, ODbL.
"""

import difflib
import gzip
import logging
import math
import os
import re
from array import array
from collections import Counter
from dataclasses import dataclass, field

log = logging.getLogger("rds-guard")

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "geodata", "gazetteer-se.tsv.gz")

COUNTIES = {
    "AB": "Stockholms län", "C": "Uppsala län", "D": "Södermanlands län",
    "E": "Östergötlands län", "F": "Jönköpings län", "G": "Kronobergs län",
    "H": "Kalmar län", "I": "Gotlands län", "K": "Blekinge län",
    "M": "Skåne län", "N": "Hallands län", "O": "Västra Götalands län",
    "S": "Värmlands län", "T": "Örebro län", "U": "Västmanlands län",
    "W": "Dalarnas län", "X": "Gävleborgs län", "Y": "Västernorrlands län",
    "Z": "Jämtlands län", "AC": "Västerbottens län", "BD": "Norrbottens län",
}

# How much a kind of place is worth when several names sound alike, and how
# precisely it pins down a position (metres)
KIND_WEIGHT = {
    "city": 6, "town": 5, "village": 4, "junction": 4, "roundabout": 3,
    "tunnel": 3, "bridge": 3, "rest_area": 3, "suburb": 3, "quarter": 2,
    "neighbourhood": 2, "hamlet": 2, "locality": 1, "isolated_dwelling": 1,
    "water": 1, "airport": 3,
}
KIND_RADIUS_M = {
    "junction": 500, "roundabout": 300, "tunnel": 500, "bridge": 400,
    "rest_area": 400, "isolated_dwelling": 800, "hamlet": 1000,
    "neighbourhood": 1000, "quarter": 1000, "suburb": 1500, "locality": 1500,
    "village": 1500, "airport": 1500, "water": 2500, "town": 3000,
    "city": 4000,
}
SETTLEMENTS = ("city", "town", "village", "suburb", "quarter",
               "neighbourhood", "hamlet", "locality", "isolated_dwelling")


@dataclass(frozen=True)
class Place:
    name: str
    kind: str
    lat: float
    lon: float
    county: str = ""
    ref: str = ""

    @property
    def point(self):
        return (self.lat, self.lon)

    @property
    def weight(self):
        return KIND_WEIGHT.get(self.kind, 1)

    @property
    def radius_m(self):
        return KIND_RADIUS_M.get(self.kind, 1500)


@dataclass
class Match:
    place: Place
    score: float          # 0..1 similarity of the sound-alike keys
    exact: bool           # the spelling matched, not just the sound
    heard: str = ""


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def km(a, b):
    """Great-circle distance in km between two (lat, lon) points."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 12742 * math.asin(min(1.0, math.sqrt(h)))


def bearing(a, b):
    """Compass bearing in degrees from a to b."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = (math.cos(la1) * math.sin(la2)
         - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def segment_km(p, a, b):
    """Distance in km from p to the straight segment a-b."""
    k = math.cos(math.radians(p[0]))
    px, py = (p[1] - a[1]) * k, p[0] - a[0]
    bx, by = (b[1] - a[1]) * k, b[0] - a[0]
    length = bx * bx + by * by
    t = 0.0 if length == 0 else max(0.0, min(1.0, (px * bx + py * by) / length))
    return km(p, (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))


def midpoint(a, b):
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


# ---------------------------------------------------------------------------
# Swedish sound-alike key
# ---------------------------------------------------------------------------

_PREFIX = re.compile(r"^(trafikplats|tpl|trafikplatsen)\s+")
_PAIRS = [
    ("sch", "S"), ("stj", "S"), ("skj", "S"), ("sj", "S"), ("ch", "S"),
    ("tj", "C"), ("kj", "C"), ("w", "v"), ("ck", "k"), ("c", "k"),
    ("z", "s"), ("ph", "f"), ("q", "k"), ("x", "ks"), ("rl", "l"),
    ("rn", "n"), ("rd", "d"), ("rt", "t"), ("rs", "S"), ("ie", "i"),
    ("dj", "j"), ("hj", "j"), ("lj", "j"), ("gj", "j"),
]
_LETTERS = str.maketrans({"å": "o", "ä": "e", "é": "e", "ü": "i", "y": "i",
                          "b": "p", "d": "t"})


def sound_key(text):
    """Key that is equal for names that sound alike in Swedish.

    "Skatscher" and "Skattkärr", "Voxnäs motet" and "Våxnäsmotet",
    "Kallskoga" and "Karlskoga" get the same or nearly the same key.
    """
    s = _PREFIX.sub("", text.lower().strip())
    s = re.sub(r"[\s\-,.'’]", "", s)
    for a, b in _PAIRS:
        s = s.replace(a, b)
    s = re.sub(r"sk(?=[eiyäö])", "S", s)
    s = re.sub(r"k(?=[eiyäö])", "C", s)
    s = re.sub(r"g(?=[eiyäö])", "j", s)
    s = s.translate(_LETTERS)
    return re.sub(r"(.)\1+", r"\1", s)


# Endings shared by many names (as sound keys): -rondellen, -motet, -korset,
# -tunneln, -bron, -backarna, -vägen, -leden, -rastplats
_SUFFIXES = tuple(sound_key(x) for x in (
    "rondellen", "motet", "korset", "tunneln", "bron", "backarna", "backen",
    "vägen", "leden", "rastplats", "rastplatsen", "korsningen"))


def _shared_suffix(a, b):
    for suf in _SUFFIXES:
        if (a.endswith(suf) and b.endswith(suf)
                and len(a) - len(suf) >= 3 and len(b) - len(suf) >= 3):
            return suf
    return None


def _pairs(key):
    return {key[i:i + 2] for i in range(len(key) - 1)}


def _norm(text):
    return re.sub(r"\s+", " ", _PREFIX.sub("", text.lower().strip()))


# ---------------------------------------------------------------------------
# Counties ("Region" in NODE_LOCATION)
# ---------------------------------------------------------------------------

def _county_stem(name):
    s = name.lower().replace(" län", "").strip()
    return s[:-1] if s.endswith("s") and s not in ("kalmar",) else s


_COUNTY_ALIASES = {_county_stem(v): k for k, v in COUNTIES.items()}
_COUNTY_ALIASES.update({"kalmar": "H", "skåne": "M", "gotland": "I",
                        "västra götaland": "O", "västergötland": "O",
                        "sörmland": "D"})


def county_code(region):
    """'Örebro län', 'Värmland', 'SE-T' or 'T' -> county code, else None."""
    if not region:
        return None
    r = region.strip()
    code = r.upper().removeprefix("SE-")
    if code in COUNTIES:
        return code
    stem = _county_stem(r)
    if stem in _COUNTY_ALIASES:
        return _COUNTY_ALIASES[stem]
    close = difflib.get_close_matches(stem, list(_COUNTY_ALIASES), n=1, cutoff=0.8)
    return _COUNTY_ALIASES[close[0]] if close else None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass
class Gazetteer:
    places: list = field(default_factory=list)

    def __post_init__(self):
        self._by_name = {}
        self._keys = []
        # Letter pairs of each sound key -> entries, so a fuzzy search only
        # compares names that share enough pairs with what was heard
        self._pairs = {}
        for i, p in enumerate(self.places):
            self._by_name.setdefault(_norm(p.name), []).append(p)
            key = sound_key(p.name)
            self._keys.append(key)
            for pair in _pairs(key):
                self._pairs.setdefault(pair, array("I")).append(i)

    def __len__(self):
        return len(self.places)

    @classmethod
    def load(cls, path=None, centers=None, radius_km=250):
        """Read the registry, keeping names within radius_km of any center.

        centers=None keeps the whole country.
        """
        path = path or os.environ.get("GAZETTEER_PATH") or DEFAULT_PATH
        centers = [c for c in (centers or []) if c]
        places = []
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 5:
                    continue
                lat, lon = float(parts[2]), float(parts[3])
                if centers and not any(km((lat, lon), c) <= radius_km
                                       for c in centers):
                    continue
                places.append(Place(parts[0], parts[1], lat, lon, parts[4],
                                    parts[5] if len(parts) > 5 else ""))
        log.info("Gazetteer: %d place names loaded from %s", len(places), path)
        return cls(places)

    # -- lookups ------------------------------------------------------------

    def exact(self, name):
        return list(self._by_name.get(_norm(name), []))

    def search(self, heard, near=None, max_km=None, limit=5, min_score=0.78,
               kinds=None):
        """Names that are spelt or sound like `heard`, best first.

        An exact spelling scores 1.0. Others score by how alike the Swedish
        sound keys are; a candidate's kind (town over farm) breaks ties.
        """
        heard = heard.strip()
        if len(heard) < 2:
            return []

        def ok(p):
            if kinds and p.kind not in kinds:
                return False
            return near is None or max_km is None or km(near, p.point) <= max_km

        found = {}
        for p in self.exact(heard):
            if ok(p):
                found[p] = Match(p, 1.0, True, heard)
        key = sound_key(heard)
        if len(key) >= 3:
            sm = difflib.SequenceMatcher(autojunk=False)
            sm.set_seq2(key)
            wanted = _pairs(key)
            hits = Counter()
            for pair in wanted:
                hits.update(self._pairs.get(pair, ()))
            # Names this alike share most letter pairs; a third is a safe floor
            need = max(1, len(wanted) // 3)
            for i, n in hits.items():
                if n < need:
                    continue
                p, k = self.places[i], self._keys[i]
                if p in found or abs(len(k) - len(key)) > 3:
                    continue
                sm.set_seq1(k)
                if sm.real_quick_ratio() < min_score or sm.quick_ratio() < min_score:
                    continue
                r = sm.ratio()
                suffix = _shared_suffix(key, k)
                if suffix:
                    # "Grumsrondellen" is not "Brunnsrondellen": compare
                    # what comes before the shared "-rondellen"
                    r = min(r, difflib.SequenceMatcher(
                        None, key[:-len(suffix)], k[:-len(suffix)]).ratio())
                if len(key) <= 4 and r < 1:
                    continue  # short names must sound exactly alike
                if r >= min_score and ok(p):
                    found[p] = Match(p, r, False, heard)

        def rank(m):
            dist = km(near, m.place.point) if near else 0
            return (-(m.score + 0.02 * m.place.weight), dist)

        return sorted(found.values(), key=rank)[:limit]

    def locate(self, text):
        """Find a node's location from 'Ort, Region' (e.g. 'Örebro, Örebro län').

        Returns a Place or None. The region (county) picks the right one of
        several places with the same name.
        """
        if not text or not text.strip():
            return None
        parts = [p.strip() for p in text.split(",") if p.strip()]
        name = parts[0]
        county = county_code(parts[1]) if len(parts) > 1 else None
        cands = [m.place for m in self.search(name, limit=40, min_score=0.85,
                                             kinds=SETTLEMENTS)]
        if county:
            cands = [p for p in cands if p.county == county] or []
        if not cands:
            return None
        exact = [p for p in cands if _norm(p.name) == _norm(name)]
        return max(exact or cands, key=lambda p: p.weight)

    def nearby(self, center, radius_km, kinds=None, limit=None):
        """Places within radius_km of center, most important and nearest first."""
        out = [p for p in self.places
               if (not kinds or p.kind in kinds) and km(center, p.point) <= radius_km]
        out.sort(key=lambda p: (-p.weight, km(center, p.point)))
        return out[:limit] if limit else out

    def word_list(self, center, limit=300, history=()):
        """Names worth listening for around center (ElevenLabs keyterms).

        Names that appeared in earlier messages come first, then junctions,
        roundabouts and tunnels nearby, then towns and villages further out.
        """
        names = []

        def add(n):
            if n and n not in names and len(n) <= 50 and len(n.split()) <= 5:
                names.append(n)

        for n in history:
            add(n)
        road_points = self.nearby(center, 60, kinds=(
            "junction", "roundabout", "tunnel", "bridge", "rest_area"))
        towns = self.nearby(center, 120, kinds=("city", "town", "village"))
        for p in sorted(road_points + towns,
                        key=lambda p: (-p.weight, km(center, p.point))):
            add(p.name)
            if len(names) >= limit:
                break
        return names[:limit]


def locate_in_file(text, path=None):
    """Find 'Ort, Region' by reading the registry file line by line.

    Keeps only settlements in memory, so finding the node's location does
    not need the whole country's names loaded at once.
    """
    path = path or os.environ.get("GAZETTEER_PATH") or DEFAULT_PATH
    places = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 5 and parts[1] in SETTLEMENTS:
                places.append(Place(parts[0], parts[1], float(parts[2]),
                                    float(parts[3]), parts[4]))
    return Gazetteer(places).locate(text)

