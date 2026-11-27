"""Build the road registry used to place traffic messages on the right road.

Fetches the numbered roads in Sweden (E-roads, riksvägar and länsvägar, with
their OpenStreetMap ref) and writes their simplified lines to
geodata/roads-se.tsv.gz. The file is committed and bundled in the Docker
images, so nothing is downloaded while RDS Guard runs.

Run it again now and then to pick up changes:

    python3 tools/build_roads.py [OUTPUT]

Sweden is fetched from the Overpass API in tiles (OVERPASS_URL, default
overpass-api.de; a mirror such as
https://maps.mail.ru/osm/tools/overpass/api/interpreter works too). Finished
tiles are cached in .gazetteer-cache/roads/, so an interrupted run continues
where it stopped. It takes a while: one tile at a time, with a pause between.

Output, one line per stretch of road:

    key  county  lat,lon;dlat,dlon;...

key is "E18" or the road number ("61", "236", "554"); county is the county
letter of a länsväg ("S 554" -> S), else empty. Coordinates are in 1e-5
degrees, the first absolute and the rest as differences from the one before.

Data (c) OpenStreetMap contributors, ODbL.
"""

import gzip
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "rds-guard-roads (+https://github.com/tubalainen/rds-guard)"}
HIGHWAYS = "motorway|trunk|primary|secondary|tertiary|unclassified"
QUERY = """[out:json][timeout:600];
area["ISO3166-1"="SE"]["admin_level"="2"]->.se;
way["highway"~"^({highways})$"]["ref"](area.se)({bbox});
out geom qt;
"""
# Sweden: 55.3-69.1 N, 10.9-24.2 E, in tiles of 1 x 2 degrees
TILES = [(lat, lon, lat + 1, lon + 2)
         for lat in range(55, 70) for lon in range(10, 25, 2)]
TOLERANCE_M = 20        # simplification: a line moves at most this much
REF_RE = re.compile(r"^\s*(?:(E)\s*(\d{1,2})|([A-Z]{1,2})?\s*(\d{1,4}))\s*$")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def parse_ref(ref):
    """'E 18;S 554' -> [('E18', ''), ('554', 'S')]. Other refs are skipped."""
    out = []
    for part in (ref or "").split(";"):
        m = REF_RE.match(part)
        if not m:
            continue
        if m.group(1):
            out.append((f"E{int(m.group(2))}", ""))
        else:
            out.append((str(int(m.group(4))), m.group(3) or ""))
    return out


def http(url, data, timeout=900):
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _tile_name(tile):
    return "_".join(("%g" % v).replace(".", "p") for v in tile[:2]) \
        + ("" if tile[2] - tile[0] == 1 else "_%g" % (tile[2] - tile[0])).replace(".", "p")


def fetch_tile(url, tile, cache, n, total, attempts=4):
    """Ways of one tile. A tile Overpass keeps timing out on is split in four."""
    bbox = "%g,%g,%g,%g" % tile
    path = os.path.join(cache, "tile_%s.json" % _tile_name(tile))
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            elements = json.load(f)
        log(f"{n}/{total} tile {bbox}: {len(elements)} ways (cached)")
        return elements
    data = urllib.parse.urlencode(
        {"data": QUERY.format(highways=HIGHWAYS, bbox=bbox)}).encode()
    small = tile[2] - tile[0] <= 0.25
    for attempt in range(8 if small else attempts):
        t0 = time.time()
        log(f"{n}/{total} tile {bbox}: fetching …")
        try:
            elements = http(url, data)["elements"]
            break
        except Exception as e:  # noqa: BLE001 - retry any network error
            wait = min(300, 20 * 2 ** attempt)
            log(f"{n}/{total} tile {bbox}: {e}; retrying in {wait}s")
            time.sleep(wait)
    else:
        if small:
            raise SystemExit(f"Overpass failed for tile {bbox}; run again to resume")
        s, w, nn, e = tile
        mlat, mlon = (s + nn) / 2, (w + e) / 2
        log(f"{n}/{total} tile {bbox}: splitting in four")
        elements = []
        for q in ((s, w, mlat, mlon), (s, mlon, mlat, e),
                  (mlat, w, nn, mlon), (mlat, mlon, nn, e)):
            elements += fetch_tile(url, q, cache, n, total, attempts)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(elements, f)
        return elements
    elements = [{"ref": e["tags"].get("ref"), "geometry":
                 [(round(g["lat"], 5), round(g["lon"], 5)) for g in e["geometry"]]}
                for e in elements if e.get("geometry") and e.get("tags")]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(elements, f)
    log(f"{n}/{total} tile {bbox}: {len(elements)} ways in {time.time() - t0:.0f}s")
    time.sleep(5)
    return elements


def chain(lines):
    """Join lines that meet end to end into longer lines."""
    lines = [list(l) for l in lines if len(l) >= 2]
    ends = {}
    for i, l in enumerate(lines):
        ends.setdefault(l[0], set()).add(i)
        ends.setdefault(l[-1], set()).add(i)
    alive = set(range(len(lines)))
    out = []
    while alive:
        i = alive.pop()
        line = lines[i]
        for end in (-1, 0):
            while True:
                p = line[end]
                nxt = next((j for j in ends.get(p, ()) if j in alive), None)
                if nxt is None:
                    break
                alive.discard(nxt)
                other = lines[nxt]
                if end == -1:
                    line = line + (other[1:] if other[0] == p else other[::-1][1:])
                else:
                    line = (other[:-1] if other[-1] == p else other[::-1][:-1]) + line
        out.append(line)
    return out


def simplify(points, tol_m=TOLERANCE_M):
    """Douglas-Peucker in local metres."""
    if len(points) <= 2:
        return points
    k = math.cos(math.radians(points[0][0]))
    xy = [(p[1] * 111320 * k, p[0] * 110540) for p in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        far, idx = 0.0, None
        for i in range(a + 1, b):
            px, py = xy[i]
            if length == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * (px - ax) - dx * (py - ay)) / length
            if d > far:
                far, idx = d, i
        if idx is not None and far > tol_m:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k_ in zip(points, keep) if k_]


def encode(points):
    q = [(round(lat * 1e5), round(lon * 1e5)) for lat, lon in points]
    parts = ["%d,%d" % q[0]]
    parts += ["%d,%d" % (b[0] - a[0], b[1] - a[1]) for a, b in zip(q, q[1:])]
    return ";".join(parts)


def main():
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        root, "geodata", "roads-se.tsv.gz")
    url = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
    cache = os.path.join(root, ".gazetteer-cache", "roads")
    os.makedirs(cache, exist_ok=True)
    log(f"Overpass: {url}; cache: {cache}")

    by_key = {}
    seen = set()
    for n, tile in enumerate(TILES, 1):
        for el in fetch_tile(url, tile, cache, n, len(TILES)):
            geom = [tuple(p) for p in el["geometry"]]
            sig = (el["ref"], geom[0], geom[-1], len(geom))
            if sig in seen:          # a way crossing a tile edge comes twice
                continue
            seen.add(sig)
            for key in parse_ref(el["ref"]):
                by_key.setdefault(key, []).append(geom)

    rows, points = [], 0
    for n, ((key, county), lines) in enumerate(sorted(by_key.items()), 1):
        for line in chain(lines):
            line = simplify(line)
            points += len(line)
            rows.append(f"{key}\t{county}\t{encode(line)}")
        if n % 200 == 0:
            log(f"joined {n}/{len(by_key)} roads, {len(rows)} lines, {points} points")

    with gzip.open(out, "wt", encoding="utf-8") as f:
        f.write("# RDS Guard road registry. Data (c) OpenStreetMap contributors, "
                "ODbL 1.0, https://www.openstreetmap.org/copyright\n")
        f.write(f"# built {time.strftime('%Y-%m-%d')} by tools/build_roads.py\n")
        f.write("# key\tcounty\tlat,lon;dlat,dlon;... (1e-5 degrees)\n")
        for row in rows:
            f.write(row + "\n")
    log(f"Wrote {len(rows)} lines ({points} points) for {len(by_key)} roads "
        f"to {out} ({os.path.getsize(out) // 1024} kB)")


if __name__ == "__main__":
    main()
