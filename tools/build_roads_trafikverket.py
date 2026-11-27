"""Build the road and junction registries from Trafikverket's road database.

Fetches from Trafikverket's open API (NVDB):
  - Vägnummer: the E-roads, riksvägar and länsvägar with their lines -> geodata/roads-se.tsv.gz, the same format as tools/build_roads.py
    (OpenStreetMap), read by roads.py
  - Korsning: the interchanges (trafikplatser, "Bergviksmotet"), and the
    names of Trafikverket's traffic cameras, speed cameras and road weather
    stations ("Stolpen västergående" on E18 -> Stolpen by E18)
    -> geodata/places-trafikverket.tsv.gz, read by gazetteer.py next to the
    OpenStreetMap names

Needs a free key from https://data.trafikverket.se/ in TRAFIKVERKET_API_KEY
(the same one RDS Guard uses for the map). The key is only sent to
Trafikverket and is never written anywhere.

    python3 tools/build_roads_trafikverket.py --probe    # a few rows of each, raw
    python3 tools/build_roads_trafikverket.py [OUTDIR]

It fetches one county at a time, in pages, and prints a line per page.
Finished pages are cached in .gazetteer-cache/trafikverket/, so an
interrupted run continues where it stopped.

Data: Trafikverket, NVDB (CC0).
"""

import gzip
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_roads import chain, encode, simplify  # noqa: E402

API_URL = "https://api.trafikinfo.trafikverket.se/v2/data.json"
NAMESPACE = "Vägdata.NVDB_DK_O"
SCHEMA = "1.2"
PAGE = 5000
# Länstillhörighet -> county letter
COUNTIES = {
    1: "AB", 3: "C", 4: "D", 5: "E", 6: "F", 7: "G", 8: "H", 9: "I", 10: "K",
    12: "M", 13: "N", 14: "O", 17: "S", 18: "T", 19: "U", 20: "W", 21: "X",
    22: "Y", 23: "Z", 24: "AC", 25: "BD",
}
NUM = re.compile(r"-?\d+(?:\.\d+)?")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def xml_escape(text):
    return (text.replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def request(key, objecttype, filters="", includes=(), limit=PAGE, skip=0,
            namespace=NAMESPACE, schema=SCHEMA):
    inc = "".join(f"<INCLUDE>{i}</INCLUDE>" for i in includes)
    body = (f'<REQUEST><LOGIN authenticationkey="{xml_escape(key)}"/>'
            f'<QUERY objecttype="{objecttype}" namespace="{namespace}" '
            f'schemaversion="{schema}" limit="{limit}" skip="{skip}">'
            f"<FILTER>{filters}</FILTER>{inc}</QUERY></REQUEST>")
    req = urllib.request.Request(API_URL, data=body.encode("utf-8"),
                                 headers={"Content-Type": "text/xml"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code in (400, 401, 403):
                raise SystemExit(f"Trafikverket said {e.code}: {detail}")
            wait = 15 * 2 ** attempt
            log(f"  HTTP {e.code}: {detail[:200]}; retrying in {wait}s")
            time.sleep(wait)
        except Exception as e:  # noqa: BLE001 - retry any network error
            wait = 15 * 2 ** attempt
            log(f"  {e}; retrying in {wait}s")
            time.sleep(wait)
    else:
        raise SystemExit("Trafikverket did not answer; run again to resume")
    result = data["RESPONSE"]["RESULT"][0]
    if "ERROR" in result:
        raise SystemExit(f"Trafikverket error: {result['ERROR']}")
    return result.get(objecttype, [])


def fetch_all(key, objecttype, filters, includes, cache, label, **kw):
    """All rows, page by page, each page cached."""
    rows, skip, page = [], 0, 1
    while True:
        path = os.path.join(cache, f"{objecttype}_{label}_{page}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                part = json.load(f)
            log(f"{objecttype} {label} page {page}: {len(part)} rows (cached)")
        else:
            t0 = time.time()
            part = request(key, objecttype, filters, includes, PAGE, skip, **kw)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(part, f)
            log(f"{objecttype} {label} page {page}: {len(part)} rows "
                f"in {time.time() - t0:.0f}s")
            time.sleep(1)
        rows += part
        if len(part) < PAGE:
            return rows
        skip += PAGE
        page += 1


def wgs84(row):
    """The WGS84 WKT of a row, whatever the field is called
    (WGS84, WKT-WGS84-3D, …)."""
    g = row.get("Geometry") or {}
    if isinstance(g, str):
        return g
    for name, value in g.items():
        if "WGS84" in name.upper() and isinstance(value, str):
            return value
    return ""


def wkt_lines(wkt):
    """'LINESTRING (13.1 59.3, …)' or MULTILINESTRING -> [[(lat, lon), …]]."""
    out = []
    for part in re.findall(r"\(([^()]+)\)", wkt or ""):
        pts = []
        for xy in part.split(","):
            nums = NUM.findall(xy)
            if len(nums) >= 2:
                pts.append((round(float(nums[1]), 5), round(float(nums[0]), 5)))
        if len(pts) >= 2:
            out.append(pts)
    return out


def wkt_point(wkt):
    nums = NUM.findall(wkt or "")
    return (float(nums[1]), float(nums[0])) if len(nums) >= 2 else None


def still_valid(row, today):
    if row.get("Deleted"):
        return False
    to = (row.get("Valid_To") or "")[:10]
    return not to or to >= today


# Sweden's E-roads; no riksväg has these numbers. (NVDB's Europaväg flag
# is not reliable: E18 is 0 in Värmland and -1 in Örebro län.)
E_ROADS = {4, 6, 10, 12, 14, 16, 18, 20, 22, 45, 47, 55, 65}


def road_key(row):
    """'E18', or the road number ('61', '236'). Länsvägar keep their county
    letter in the registry: their numbers repeat from county to county."""
    num = row.get("Huvudnummer")
    if num is None or int(num) <= 0:
        return None
    return f"E{int(num)}" if int(num) in E_ROADS else str(int(num))


def road_county(key, letter):
    return letter if key.isdigit() and int(key) >= 100 else ""


def junction_names(row):
    """The interchange's name ('Bergviksmotet', 'Hynboholm'); street pairs
    ('Horsengatan/Mossgatan') are left out."""
    name = re.sub(r"(?i)^(trafikplats|tpl)\s+", "", (row.get("Trafikplatsnamn") or "").strip())
    return [name] if name and "/" not in name else []


ROAD_FIELDS = ("Huvudnummer", "Undernummer", "Europaväg", "Länstillhörighet",
               "Geometry", "Deleted", "Valid_To")
JUNCTION_FIELDS = ("Trafikplatsnamn", "Trafikplatsnummer", "Geometry",
                   "Deleted", "Valid_To")
JUNCTION_FILTER = '<EXISTS name="Trafikplatsnamn" value="true"/>'
ROAD_FILTER = '<GT name="Huvudnummer" value="0"/>'


_HEADING = re.compile(r"(?i)[,\s]*\b(?:(?:söder|norr|öster|väster|syd|nord|öst|väst)(?:gående|ut)|riktning\s+\S+|"
                      r"mot\s+.+|norra|södra|östra|västra|[nsöv])\s*$")
ROAD_IN_TEXT = re.compile(r"\b(?:E\s?(\d{1,2})|(?:väg|riksväg|rv)\s+(\d{1,3}))\b", re.I)


def spot_name(name):
    """'Kroksvik, östergående ' -> 'Kroksvik'; 'Nikkalakorsning mot Kalix'
    -> 'Nikkalakorsning'."""
    name = (name or "").strip()
    while True:
        cut = _HEADING.sub("", name).strip(" ,")
        if cut == name or not cut:
            return name
        name = cut


def spot_road(text):
    """'E4', '61', 'Kameran är placerad på E45 vid …' -> 'E45' / '61'."""
    text = (text or "").strip()
    if re.fullmatch(r"E?\s?\d{1,3}", text):
        num = int(re.sub(r"\D", "", text))
        return f"E{num}" if num in E_ROADS else (str(num) if num < 100 else "")
    m = ROAD_IN_TEXT.search(text)
    if not m:
        return ""
    if m.group(1):
        return f"E{int(m.group(1))}"
    num = int(m.group(2))
    return (f"E{num}" if num in E_ROADS else str(num)) if num < 100 else ""


SPOT_SOURCES = (
    # objecttype, namespace, schema, fields, road field
    ("TrafficSafetyCamera", "Road.Infrastructure", "1.0",
     ("Name", "RoadNumber", "Geometry", "Deleted"), "RoadNumber"),
    ("Camera", "Road.Infrastructure", "1.0",
     ("Name", "Description", "Geometry", "Deleted"), "Description"),
    ("WeatherMeasurepoint", "Road.WeatherInfo", "2.1",
     ("Name", "Geometry"), None),
)


def fetch_spots(key, cache):
    """[(name, lat, lon, road)] from cameras and weather stations."""
    out = []
    for objecttype, ns, schema, fields, road_field in SPOT_SOURCES:
        rows = fetch_all(key, objecttype, "", fields, cache, "all",
                         namespace=ns, schema=schema)
        for r in rows:
            p = wkt_point(wgs84(r))
            name = spot_name(r.get("Name"))
            if p is None or r.get("Deleted") or not name or "/" in name:
                continue
            road = spot_road(r.get(road_field)) if road_field else ""
            out.append((name, p[0], p[1], road))
        log(f"{objecttype}: {len(rows)} rows")
    return out


def probe(key):
    for objecttype, filters, fields in (
            ("Vägnummer", ROAD_FILTER + '<EQ name="Länstillhörighet" value="17"/>',
             ROAD_FIELDS),
            ("Vägnummer", '<EQ name="Huvudnummer" value="18"/>', ROAD_FIELDS),
            ("Korsning", JUNCTION_FILTER, JUNCTION_FIELDS)):
        rows = request(key, objecttype, filters, fields, limit=3)
        print(f"===== {objecttype} {filters}")
        for r in rows:
            r = dict(r)
            if "Geometry" in r:
                r["Geometry"] = {k: (v[:160] + "…" if isinstance(v, str) and len(v) > 160 else v)
                                 for k, v in r["Geometry"].items()}
            print(json.dumps(r, ensure_ascii=False))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    key = os.environ.get("TRAFIKVERKET_API_KEY", "").strip()
    if not key:
        raise SystemExit("Set TRAFIKVERKET_API_KEY (free key from data.trafikverket.se)")
    if "--probe" in sys.argv:
        probe(key)
        return
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    outdir = args[0] if args else os.path.join(root, "geodata")
    cache = os.path.join(root, ".gazetteer-cache", "trafikverket")
    os.makedirs(cache, exist_ok=True)
    today = time.strftime("%Y-%m-%d")

    # Roads, county by county
    by_key, skipped = {}, 0
    for n, (code, letter) in enumerate(sorted(COUNTIES.items()), 1):
        rows = fetch_all(key, "Vägnummer",
                         ROAD_FILTER + f'<EQ name="Länstillhörighet" value="{code}"/>',
                         ROAD_FIELDS, cache, "all_" + letter)
        kept = 0
        for r in rows:
            k = road_key(r)
            lines = wkt_lines(wgs84(r))
            if k is None or not lines or not still_valid(r, today):
                skipped += 1
                continue
            by_key.setdefault((k, road_county(k, letter)), []).extend(lines)
            kept += 1
        log(f"county {n}/{len(COUNTIES)} {letter}: {len(rows)} rows, {kept} kept, "
            f"{len(by_key)} roads so far")

    rows_out, points = [], 0
    for n, ((k, county), lines) in enumerate(sorted(by_key.items()), 1):
        for line in chain(lines):
            line = simplify(line)
            points += len(line)
            rows_out.append(f"{k}\t{county}\t{encode(line)}")
        if n % 500 == 0:
            log(f"joined {n}/{len(by_key)} roads, {len(rows_out)} lines, {points} points")
    out = os.path.join(outdir, "roads-se.tsv.gz")
    with gzip.open(out, "wt", encoding="utf-8") as f:
        f.write("# RDS Guard road registry. Data: Trafikverket, NVDB (CC0)\n")
        f.write(f"# built {today} by tools/build_roads_trafikverket.py\n")
        f.write("# key\tcounty\tlat,lon;dlat,dlon;... (1e-5 degrees)\n")
        for row in rows_out:
            f.write(row + "\n")
    log(f"Wrote {len(rows_out)} lines ({points} points) for {len(by_key)} roads "
        f"to {out} ({os.path.getsize(out) // 1024} kB); {skipped} rows skipped")

    # Interchanges: the nodes of one are averaged into one point
    rows = fetch_all(key, "Korsning", JUNCTION_FILTER, JUNCTION_FIELDS, cache, "all")
    groups = {}
    for r in rows:
        p = wkt_point(wgs84(r))
        if p is None or not still_valid(r, today):
            continue
        kind = "junction"
        number = r.get("Trafikplatsnummer")
        for name in junction_names(r):
            # Nodes of one interchange lie close together; the same name
            # far away is another place
            clusters = groups.setdefault((name, kind, number), [])
            near = next((c for c in clusters
                         if abs(c[0][0] - p[0]) < 0.03 and abs(c[0][1] - p[1]) < 0.06), None)
            if near is None:
                clusters.append([p])
            else:
                near.append(p)
    # Cameras and weather stations: one spot per name and place
    spots = []
    for name, lat, lon, road in fetch_spots(key, cache):
        same = next((s for s in spots if s[0] == name
                     and abs(s[1] - lat) < 0.01 and abs(s[2] - lon) < 0.02), None)
        if same is None:
            spots.append([name, lat, lon, road])
        elif road and not same[3]:
            same[3] = road

    out = os.path.join(outdir, "places-trafikverket.tsv.gz")
    count = 0
    with gzip.open(out, "wt", encoding="utf-8") as f:
        f.write("# RDS Guard places from Trafikverket: interchanges (NVDB), "
                "cameras, weather stations. Data: Trafikverket (CC0)\n")
        f.write(f"# built {today} by tools/build_roads_trafikverket.py\n")
        f.write("# name\tkind\tlat\tlon\tcounty\tref\n")
        for (name, kind, number), clusters in sorted(groups.items(), key=lambda kv: kv[0][:2]):
            for pts in clusters:
                lat = sum(q[0] for q in pts) / len(pts)
                lon = sum(q[1] for q in pts) / len(pts)
                f.write(f"{name}\t{kind}\t{lat:.5f}\t{lon:.5f}\t\t{number or ''}\n")
                count += 1
        for name, lat, lon, road in sorted(spots):
            f.write(f"{name}\tlandmark\t{lat:.5f}\t{lon:.5f}\t\t{road}\n")
            count += 1
    log(f"Wrote {count} places ({len(spots)} cameras and weather stations) from {len(rows)} rows to {out}")


if __name__ == "__main__":
    main()
