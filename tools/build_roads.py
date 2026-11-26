"""Build the road network used to place traffic messages on the right road.

Fetches the numbered roads in Sweden (E-roads, riksvägar, länsvägar) from
OpenStreetMap and writes their lines to geodata/roads-se.json.gz, simplified
to about 25 m. The file is committed and bundled in the Docker images, so
nothing is downloaded while RDS Guard runs.

    python3 tools/build_roads.py [OUTPUT]

Sweden is fetched from the Overpass API in tiles (OVERPASS_URL, default
overpass-api.de); finished tiles are cached in .roads-cache/ so an
interrupted run continues where it stopped. Roads outside Sweden are left
out (the county borders come from tools/build_gazetteer.py's cache or
Nominatim). It prints a line per tile as it goes.

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_gazetteer import TILES, CountyFinder, county_borders, http  # noqa: E402

QUERY = """[out:json][timeout:600];
way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified|motorway_link|trunk_link)$"]["ref"]({bbox});
out geom qt;
"""
TOLERANCE_M = 25


def road_refs(ref):
    """'E 18;E 20' -> ['E18', 'E20']; '61' -> ['Rv 61']; '734' -> ['Lv 734']."""
    out = []
    for part in re.split(r"[;,/]", ref or ""):
        part = part.strip()
        m = re.fullmatch(r"E\s?(\d{1,2})", part, re.IGNORECASE)
        if m:
            out.append(f"E{int(m.group(1))}")
            continue
        if re.fullmatch(r"\d{1,4}", part):
            n = int(part)
            out.append(f"Rv {n}" if n < 100 else f"Lv {n}")
    return out


def _xy(p, lat0):
    """Metres on a local flat projection."""
    return (p[1] * 111320 * math.cos(math.radians(lat0)), p[0] * 110540)


def simplify(points, tol=TOLERANCE_M):
    """Douglas-Peucker on (lat, lon) points."""
    if len(points) < 3:
        return points
    lat0 = points[0][0]
    xy = [_xy(p, lat0) for p in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = xy[a], xy[b]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy or 1e-9
        best, far = -1, 0.0
        for i in range(a + 1, b):
            px, py = xy[i]
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
            d = math.hypot(px - ax - t * dx, py - ay - t * dy)
            if d > far:
                best, far = i, d
        if far > tol:
            keep[best] = True
            stack += [(a, best), (best, b)]
    return [p for p, k in zip(points, keep) if k]


def fetch_tile(url, tile, cache):
    path = os.path.join(cache, "roads_%d_%d.json" % tile[:2])
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f), True
    bbox = "%d,%d,%d,%d" % tile
    data = urllib.parse.urlencode({"data": QUERY.format(bbox=bbox)}).encode()
    for attempt in range(8):
        try:
            elements = http(url, data)["elements"]
            break
        except Exception as e:  # noqa: BLE001 - retry any network error
            wait = min(300, 20 * 2 ** attempt)
            print(f"  tile {bbox}: {e}; retrying in {wait}s", file=sys.stderr, flush=True)
            time.sleep(wait)
    else:
        raise SystemExit(f"Overpass failed for tile {bbox}; run again to resume")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(elements, f)
    time.sleep(2)
    return elements, False


def main():
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        root, "geodata", "roads-se.json.gz")
    url = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
    cache = os.path.join(root, ".roads-cache")
    os.makedirs(cache, exist_ok=True)
    # County borders: the gazetteer's cache when it is there
    gaz_cache = os.path.join(root, ".gazetteer-cache")
    finder = CountyFinder(county_borders(gaz_cache if os.path.isdir(gaz_cache) else cache))

    tiles = [t for t in TILES if any(
        x0 <= t[3] and x1 >= t[1] and y0 <= t[2] and y1 >= t[0]
        for _, _, x0, x1, y0, y1 in finder.parts)]
    roads, seen, points_in, points_out = {}, set(), 0, 0
    t0 = time.monotonic()
    for n, tile in enumerate(tiles, 1):
        elements, cached = fetch_tile(url, tile, cache)
        kept = 0
        for el in elements:
            if el.get("type") != "way" or el["id"] in seen:
                continue   # tiles overlap at their edges
            seen.add(el["id"])
            refs = road_refs(el.get("tags", {}).get("ref"))
            geom = [(g["lat"], g["lon"]) for g in el.get("geometry") or []]
            if not refs or len(geom) < 2:
                continue
            mid = geom[len(geom) // 2]
            if not finder.county(*mid):
                continue   # outside Sweden
            line = simplify(geom)
            points_in += len(geom)
            points_out += len(line)
            flat = [round(v, 5) for p in line for v in p]
            for r in refs:
                roads.setdefault(r, []).append(flat)
            kept += 1
        print(f"tile {n:3}/{len(tiles)} {tile}: {len(elements)} ways, {kept} kept"
              f"{' (cached)' if cached else ''} · {len(roads)} roads so far"
              f" · {time.monotonic() - t0:.0f} s", flush=True)

    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    doc = {"about": "RDS Guard road network. Data (c) OpenStreetMap contributors, "
                    "ODbL 1.0, https://www.openstreetmap.org/copyright",
           "built": time.strftime("%Y-%m-%d"),
           "format": "roads: {ref: [[lat, lon, lat, lon, ...], ...]}",
           "roads": dict(sorted(roads.items()))}
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(doc, f, separators=(",", ":"))
    size = os.path.getsize(out) / 1e6
    print(f"Wrote {len(roads)} roads, {points_out} points (from {points_in}) "
          f"to {out} ({size:.1f} MB)")


if __name__ == "__main__":
    main()
