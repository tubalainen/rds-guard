"""Score the map engines against a labelled set of real traffic messages.

The set (tools/geo_corpus.json) holds real transcriptions from
one receiver and, for each, what a person says the message contains: which
incidents, of what kind (new, update, repeat, cleared, area warning),
where, and which incident each one is about across messages. The messages
are run in broadcast order through the same chain as RDS Guard (placing,
then merging into incidents, in an empty database), so linking messages
about the same incident is measured too.

    python3 tools/eval_geolocation.py                      # rules engine (free)
    python3 tools/eval_geolocation.py --engine claude      # needs ANTHROPIC_API_KEY
    python3 tools/eval_geolocation.py --import events.json # add messages to label
    python3 tools/eval_geolocation.py --stored events.json # score what an installation stored

events.json is the output of
    curl -s 'http://<host>:8022/api/events?type=traffic&limit=60'
Messages added with --import have "expect": null. They are still run (an
earlier message gives later ones something to link to) but not scored
until someone fills in "expect".

Each expected item:
    {"incident": "e18-ristjarn",      same key = same incident, across messages
     "kind": "new",                   new | update | repeat | cleared | area_warning
     "road": "E18",                   or null
     "near": ["Ristjärn"],            names in the registry (midpoint of several);
                                      "Karlskoga (town)" picks a kind when the
                                      name has several places; or "lat"/"lon"
                                      for a spot without a name
     "km": 2,                         how far off a placement may be (plus its radius)
     "what": "Bil med haveri"}        for the reader; not scored

Costs: the rules engine is free; Claude Haiku about 1 öre per message.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, "/app")
sys.path.insert(0, ROOT)

import gazetteer as gz  # noqa: E402
import geolocate  # noqa: E402

DEFAULT_CORPUS = os.path.join(ROOT, "tools", "geo_corpus.json")
KINDS = ("new", "update", "repeat", "cleared", "area_warning")
# What the incident store can tell apart today: a new incident, a message
# about one already known, and one that says it is over
KIND_GROUP = {"new": "new", "update": "linked", "repeat": "linked",
              "cleared": "cleared", "area_warning": "area"}
KIND_SV = {"new": "ny", "linked": "känd", "cleared": "avklarad",
           "area": "områdesvarning"}

# Claude Haiku 5.5 list prices, USD per million tokens (check current prices)
PRICE_IN, PRICE_OUT = 0.10, 0.50


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------

def load_corpus(path=DEFAULT_CORPUS):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_corpus(corpus, path=DEFAULT_CORPUS):
    corpus["cases"].sort(key=lambda c: c["at"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=1)
        f.write("\n")


def norm_ts(ts):
    """'2026-11-24 08:15:02Z' -> '2026-11-24T08:15:02' (the incidents' form)."""
    return str(ts).replace(" ", "T")[:19]


def import_events(corpus, events):
    """Add /api/events rows that have a transcription and are not in the set
    yet. Returns how many were added."""
    have = {c.get("event_id") for c in corpus["cases"]}
    added = 0
    for ev in events:
        text = (ev.get("transcription") or "").strip()
        if not text or ev.get("id") in have or not ev.get("started_at"):
            continue
        corpus["cases"].append({
            "id": f"ev{ev['id']}", "event_id": ev["id"],
            "at": norm_ts(ev["started_at"]),
            "station": ev.get("station_ps") or "",
            "text": text,
            "radiotext": ev.get("radiotext") or [],
            "expect": None,
        })
        have.add(ev["id"])
        added += 1
    corpus["cases"].sort(key=lambda c: c["at"])
    return added


def expected_point(item, reg, center):
    """Where an expected item is: its lat/lon, or the middle of its names
    (each the registry entry of that name nearest the receiver)."""
    if item.get("lat") is not None and item.get("lon") is not None:
        return (item["lat"], item["lon"])
    points = []
    for entry in item.get("near") or []:
        m = re.fullmatch(r"(.+?) \((\w+)\)", entry.strip())
        name, kind = (m.group(1), m.group(2)) if m else (entry, None)
        found = [p for p in reg.exact(name) if kind is None or p.kind == kind]
        if not found:
            raise ValueError(f"'{entry}' finns inte i ortnamnsregistret")
        points.append(min(found, key=lambda p: gz.km(center, p.point)).point)
    if not points:
        raise ValueError("varken 'near' eller 'lat'/'lon'")
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))


def validate(corpus, reg, center):
    """Problems in the set, as readable strings (empty: all fine)."""
    problems, ids = [], set()
    for case in corpus["cases"]:
        cid = case.get("id")
        if cid in ids:
            problems.append(f"{cid}: id finns två gånger")
        ids.add(cid)
        for key in ("at", "text"):
            if not case.get(key):
                problems.append(f"{cid}: '{key}' saknas")
        for i, item in enumerate(case.get("expect") or []):
            where = f"{cid} post {i + 1}"
            if item.get("kind") not in KINDS:
                problems.append(f"{where}: okänd kind {item.get('kind')!r}")
            if not item.get("incident"):
                problems.append(f"{where}: 'incident' saknas")
            if not item.get("km"):
                problems.append(f"{where}: 'km' saknas")
            try:
                expected_point(item, reg, center)
            except ValueError as e:
                problems.append(f"{where}: {e}")
    return problems


# ---------------------------------------------------------------------------
# Run: the messages in broadcast order, placed and merged as in RDS Guard
# ---------------------------------------------------------------------------

@dataclass
class Predicted:
    placement: geolocate.Placement
    incident_id: int
    group: str                       # "new", "linked" or "cleared"


@dataclass
class CaseRun:
    case: dict
    engine: str
    predicted: list
    corrected_text: str = None
    error: str = None
    usage: tuple = None              # (tokens in, tokens out) for Claude
    areas: int = 0                   # area warnings the engine found


def run(corpus, geolocator, reg, center, location_label=""):
    """Every message of the set through placing and merging. Returns
    [CaseRun] in broadcast order."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    geolocate.init_tables(conn)
    out = []
    for case in sorted(corpus["cases"], key=lambda c: c["at"]):
        at = norm_ts(case["at"])
        since = geolocate._shift_iso(at, -geolocate.INCIDENT_HOURS)
        known = geolocate.open_incidents(
            conn, geolocate._shift_iso(at, -geolocate.LINK_HOURS), at)
        result = geolocator.locate(case["text"], reg, center, location_label,
                                   case.get("radiotext"), known)
        before = conn.execute("SELECT COALESCE(MAX(id), 0) FROM incidents"
                              ).fetchone()[0]
        ids = geolocate.merge_placements(conn, result.placements, case["id"],
                                         at, since, result.engine)
        predicted = [Predicted(p, i, "cleared" if p.cleared
                               else "new" if i > before else "linked")
                     for p, i in zip(result.placements, ids)]
        reader = getattr(geolocator, "_reader", None)
        usage = getattr(reader, "last_usage", None) if result.engine == "claude" else None
        out.append(CaseRun(case, result.engine, predicted, result.corrected_text,
                           result.error, (usage.input_tokens, usage.output_tokens)
                           if usage else None, len(result.areas)))
    conn.close()
    return out


def stored_runs(corpus, events):
    """[CaseRun] from the incidents an installation already stored for these
    messages (the "incidents" of each /api/events row), without placing
    anything again. Whether a message cleared its incident is not stored
    per message, so the kind is only new or known."""
    by_id = {e.get("id"): e for e in events}
    seen, out = set(), []
    for case in sorted(corpus["cases"], key=lambda c: c["at"]):
        ev = by_id.get(case.get("event_id"))
        if ev is None:
            continue
        predicted = []
        for inc in ev.get("incidents") or []:
            pl = geolocate.Placement(
                lat=inc["lat"], lon=inc["lon"], radius_m=inc["radius_m"],
                road=geolocate.norm_road(inc.get("label") or ""),
                label=inc.get("label") or "")
            predicted.append(Predicted(pl, inc["id"],
                                       "linked" if inc["id"] in seen else "new"))
            seen.add(inc["id"])
        out.append(CaseRun(case, ev.get("geo_engine") or "stored", predicted,
                           ev.get("transcription_corrected")))
    return out


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------

@dataclass
class ItemResult:
    case_id: str
    item: dict
    point: tuple
    match: Predicted = None
    km_off: float = None


def _match_case(expect, predicted, reg, center):
    """Pair expected items with placements: nearest first, each used once,
    within the item's km plus the placement's radius, on the same road when
    both name one."""
    items = [ItemResult(None, it, expected_point(it, reg, center)) for it in expect]
    pairs = []
    for ii, res in enumerate(items):
        if KIND_GROUP[res.item["kind"]] == "area":
            # Area warnings are not points: they are scored per message
            # (score()), and a point made of one is a place without a match
            continue
        exp_road = geolocate.norm_road(res.item.get("road") or "") or res.item.get("road")
        for pi, pr in enumerate(predicted):
            pl = pr.placement
            if exp_road and pl.road and exp_road != pl.road:
                continue
            d = gz.km(res.point, (pl.lat, pl.lon))
            if d <= res.item["km"] + pl.radius_m / 1000:
                pairs.append((d, ii, pi))
    used_i, used_p = set(), set()
    for d, ii, pi in sorted(pairs):
        if ii in used_i or pi in used_p:
            continue
        used_i.add(ii)
        used_p.add(pi)
        items[ii].match, items[ii].km_off = predicted[pi], d
    extra = [pr for pi, pr in enumerate(predicted) if pi not in used_p]
    return items, extra


def score(runs, reg, center):
    """Numbers for a run, plus the per-message detail for the report."""
    items, extras, labelled = [], [], 0
    area_found = area_false = 0
    for r in runs:
        expect = r.case.get("expect")
        if expect is None:
            continue
        labelled += 1
        wants_area = any(KIND_GROUP[e["kind"]] == "area" for e in expect)
        area_found += bool(wants_area and r.areas)
        area_false += bool(r.areas and not wants_area)
        got, extra = _match_case(expect, r.predicted, reg, center)
        for g in got:
            g.case_id = r.case["id"]
        items += got
        extras += [(r.case["id"], e) for e in extra]
    areas = [i for i in items if KIND_GROUP[i.item["kind"]] == "area"]
    items = [i for i in items if KIND_GROUP[i.item["kind"]] != "area"]
    found = [i for i in items if i.match]
    kinds = found
    kind_ok = [i for i in kinds if KIND_GROUP[i.item["kind"]] == i.match.group]

    # Linking: every pair of found incidents (not area warnings), across
    # all messages
    same_ok = same_missed = merged_wrong = 0
    linkable = found
    for a in range(len(linkable)):
        for b in range(a + 1, len(linkable)):
            x, y = linkable[a], linkable[b]
            if x.case_id == y.case_id:
                continue
            should = x.item["incident"] == y.item["incident"]
            did = x.match.incident_id == y.match.incident_id
            if should and did:
                same_ok += 1
            elif should:
                same_missed += 1
            elif did:
                merged_wrong += 1

    # A hit that says where: its circle no wider than the facit allows
    precise = [i for i in found
               if i.match.placement.radius_m <= max(1000 * i.item["km"], 2500)]
    offs = sorted(i.km_off for i in found)
    radii = sorted(i.match.placement.radius_m for i in found)
    return {
        "messages": len(runs), "labelled": labelled,
        "expected": len(items), "found": len(found),
        "precise": len(precise), "area_warnings": len(areas),
        "area_found": area_found, "area_false": area_false,
        "extra": len(extras),
        "kind_scored": len(kinds), "kind_ok": len(kind_ok),
        "link_pairs": same_ok + same_missed, "linked_ok": same_ok,
        "merged_wrong": merged_wrong,
        "median_km_off": offs[len(offs) // 2] if offs else None,
        "median_radius_m": radii[len(radii) // 2] if radii else None,
        "items": items, "extras": extras,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _pct(n, d):
    return f"{n}/{d} ({100 * n / d:.0f} %)" if d else "–"


def print_report(engine_name, runs, s, verbose=True):
    by_case = {}
    for it in s["items"]:
        by_case.setdefault(it.case_id, []).append(it)
    extras = {}
    for cid, e in s["extras"]:
        extras.setdefault(cid, []).append(e)
    if verbose:
        for r in runs:
            if r.case.get("expect") is None:
                continue
            cid = r.case["id"]
            print("-" * 78)
            print(f"{cid}  {r.case['at']}"
                  + (f"  [{r.engine}: {r.error}]" if r.error else ""))
            for it in by_case.get(cid, []):
                if KIND_GROUP[it.item["kind"]] == "area":
                    continue
                want = KIND_GROUP[it.item["kind"]]
                label = f"{it.item.get('road') or ''} {'–'.join(it.item.get('near') or [])}".strip()
                if not it.match:
                    print(f"  MISSAD   {label} ({it.item['kind']})")
                    continue
                m = it.match
                kind = ("" if want in ("area", m.group)
                        else f"  typ: {KIND_SV[m.group]}, väntat {KIND_SV[want]}")
                print(f"  hittad   {label} → {m.placement.label} ±{m.placement.radius_m} m,"
                      f" {it.km_off:.1f} km bort, händelse #{m.incident_id}{kind}")
            for e in extras.get(cid, []):
                print(f"  EXTRA    {e.placement.label} ±{e.placement.radius_m} m"
                      f" (händelse #{e.incident_id})")
    print("=" * 78)
    print(f"Motor: {engine_name}. {s['messages']} meddelanden, {s['labelled']} med facit.")
    print(f"Hittade poster:        {_pct(s['found'], s['expected'])}")
    print(f"  varav precisa:       {_pct(s['precise'], s['expected'])}"
          "  (radien högst facits km, minst 2,5 km)")
    print(f"Platser utan facit:    {s['extra']}")
    print(f"Områdesvarningar:      {_pct(s['area_found'], s['area_warnings'])},"
          f" felaktiga {s['area_false']}")
    if s["median_km_off"] is not None:
        print(f"Avstånd till facit:    median {s['median_km_off']:.1f} km,"
              f" radie median {s['median_radius_m']} m")
    print(f"Rätt typ:              {_pct(s['kind_ok'], s['kind_scored'])}"
          "  (ny / känd / avklarad)")
    print(f"Rätt ihopknutna par:   {_pct(s['linked_ok'], s['link_pairs'])}")
    print(f"Fel ihopslagna par:    {s['merged_wrong']}")


def summary(s):
    """The numbers alone, for JSON."""
    return {k: v for k, v in s.items() if k not in ("items", "extras")}


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus", default=DEFAULT_CORPUS)
    ap.add_argument("--engine", choices=("rules", "claude", "both"), default="rules")
    ap.add_argument("--import", dest="import_file", metavar="EVENTS_JSON",
                    help="add the messages in a saved /api/events answer to the set")
    ap.add_argument("--stored", metavar="EVENTS_JSON",
                    help="score the incidents stored with a saved /api/events answer")
    ap.add_argument("--check", action="store_true", help="only check the set")
    ap.add_argument("--quiet", action="store_true", help="only the totals")
    ap.add_argument("--json", metavar="FILE", help="also write the totals here")
    args = ap.parse_args()

    corpus = load_corpus(args.corpus)
    if args.import_file:
        with open(args.import_file, encoding="utf-8") as f:
            data = json.load(f)
        added = import_events(corpus, data.get("events", data) if isinstance(data, dict) else data)
        save_corpus(corpus, args.corpus)
        print(f"{added} nya meddelanden lades till i {args.corpus} (utan facit).")
        return

    import geo_service
    svc = geo_service.GeoService(geolocate.create_geolocator("rules"))
    node = svc.locate_node(corpus["node_location"])
    if node is None:
        sys.exit(f"Platsen '{corpus['node_location']}' hittades inte.")
    reg = svc.area(node.point)
    problems = validate(corpus, reg, node.point)
    for p in problems:
        print(f"FACIT: {p}")
    if problems:
        sys.exit(1)
    if args.check:
        n = sum(1 for c in corpus["cases"] if c.get("expect") is not None)
        print(f"Facit ok: {len(corpus['cases'])} meddelanden, {n} med facit.")
        return

    totals = {}
    if args.stored:
        with open(args.stored, encoding="utf-8") as f:
            data = json.load(f)
        runs = stored_runs(corpus, data.get("events", data) if isinstance(data, dict) else data)
        s = score(runs, reg, node.point)
        print_report("sparade händelser (avklarad syns inte per sändning)", runs, s,
                     verbose=not args.quiet)
        totals["stored"] = summary(s)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(totals, f, ensure_ascii=False, indent=1)
        return
    engines = ["rules", "claude"] if args.engine == "both" else [args.engine]
    for name in engines:
        if name == "claude":
            import config
            if not config.ANTHROPIC_API_KEY:
                sys.exit("ANTHROPIC_API_KEY saknas.")
            g = geolocate.Geolocator(geolocate.ClaudeReader(
                config.ANTHROPIC_API_KEY, model=config.CLAUDE_MODEL,
                timeout=config.CLAUDE_TIMEOUT))
        else:
            g = geolocate.Geolocator(None)
        t0 = time.monotonic()
        runs = run(corpus, g, reg, node.point, corpus["node_location"])
        s = score(runs, reg, node.point)
        print_report(name, runs, s, verbose=not args.quiet)
        tin = sum(r.usage[0] for r in runs if r.usage)
        tout = sum(r.usage[1] for r in runs if r.usage)
        if tin:
            cost = tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT
            print(f"Tokens: {tin} in, {tout} ut, cirka {cost:.4f} USD.")
        print(f"Tid: {time.monotonic() - t0:.1f} s")
        totals[name] = summary(s)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(totals, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
