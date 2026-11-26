"""Build the place-name registry (gazetteer) used to geolocate traffic messages.

Fetches named places, motorway junctions, roundabouts, tunnels, bridges,
rest areas and lakes in Sweden from OpenStreetMap and writes them to
geodata/gazetteer-se.tsv.gz. The file is committed and bundled in the Docker
images, so nothing is downloaded while RDS Guard runs.

Run it again now and then to pick up new names:

    python3 tools/build_gazetteer.py [OUTPUT]

Sweden is fetched from the Overpass API in tiles (OVERPASS_URL, default
overpass-api.de); finished tiles are cached in .gazetteer-cache/ so an
interrupted run continues where it stopped. Each name gets its county from
the county borders (Nominatim, simplified), which also drops names in
Norway, Finland and Denmark.

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

COUNTIES = {
    "AB": "Stockholms län", "C": "Uppsala län", "D": "Södermanlands län",
    "E": "Östergötlands län", "F": "Jönköpings län", "G": "Kronobergs län",
    "H": "Kalmar län", "I": "Gotlands län", "K": "Blekinge län",
    "M": "Skåne län", "N": "Hallands län", "O": "Västra Götalands län",
    "S": "Värmlands län", "T": "Örebro län", "U": "Västmanlands län",
    "W": "Dalarnas län", "X": "Gävleborgs län", "Y": "Västernorrlands län",
    "Z": "Jämtlands län", "AC": "Västerbottens län", "BD": "Norrbottens län",
}

UA = {"User-Agent": "rds-guard-gazetteer (+https://github.com/tubalainen/rds-guard)"}
PLACES = "city|town|village|hamlet|suburb|quarter|neighbourhood|isolated_dwelling|locality"
QUERY = """[out:json][timeout:300];
(
  node["place"~"^({places})$"]["name"]({bbox});
  node["highway"="motorway_junction"]["name"]({bbox});
  nw["junction"="roundabout"]["name"]({bbox});
  way["tunnel"]["tunnel:name"]({bbox});
  way["tunnel"]["name"~"tunneln$"]({bbox});
  way["bridge"]["bridge:name"]({bbox});
  way["bridge"]["name"~"bron$"]({bbox});
  nwr["highway"~"^(rest_area|services)$"]["name"]({bbox});
  nwr["aeroway"="aerodrome"]["name"]({bbox});
  wr["natural"="water"]["name"]({bbox});
);
out center tags qt;
"""
# Sweden: 55.3-69.1 N, 10.9-24.2 E, in tiles of 1 x 2 degrees
TILES = [(lat, lon, lat + 1, lon + 2)
         for lat in range(55, 70) for lon in range(10, 25, 2)]


def kind_and_name(tags):
    if tags.get("place"):
        return tags["place"], tags.get("name")
    if tags.get("highway") == "motorway_junction":
        return "junction", tags.get("name")
    if tags.get("junction") == "roundabout":
        return "roundabout", tags.get("name")
    if tags.get("tunnel"):
        return "tunnel", tags.get("tunnel:name") or tags.get("name")
    if tags.get("bridge"):
        return "bridge", tags.get("bridge:name") or tags.get("name")
    if tags.get("highway") in ("rest_area", "services"):
        return "rest_area", tags.get("name")
    if tags.get("aeroway") == "aerodrome":
        return "airport", tags.get("name")
    if tags.get("natural") == "water":
        return "water", tags.get("name")
    return None, None


def airport_alias(name):
    """'Karlstad Airport' -> 'Karlstad flygplats', as the radio says it."""
    m = re.match(r"^(.+?)\s+(?:Airport|flygplats|flygfält)$", name, re.IGNORECASE)
    if not m:
        return None
    alias = f"{m.group(1)} flygplats"
    return None if alias == name else alias


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 12742 * math.asin(math.sqrt(h))


def http(url, data=None, timeout=900):
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch_tile(url, tile, cache):
    path = os.path.join(cache, "tile_%d_%d.json" % tile[:2])
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    bbox = "%d,%d,%d,%d" % tile
    data = urllib.parse.urlencode(
        {"data": QUERY.format(places=PLACES, bbox=bbox)}).encode()
    for attempt in range(8):
        try:
            elements = http(url, data)["elements"]
            break
        except Exception as e:  # noqa: BLE001 - retry any network error
            wait = min(300, 20 * 2 ** attempt)
            print(f"  tile {bbox}: {e}; retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    else:
        raise SystemExit(f"Overpass failed for tile {bbox}; run again to resume")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(elements, f)
    time.sleep(2)
    return elements


def county_borders(cache):
    """{code: [rings]} from Nominatim, simplified to ~1 km."""
    path = os.path.join(cache, "counties.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    borders = {}
    for code, name in COUNTIES.items():
        q = urllib.parse.urlencode({
            "q": name, "countrycodes": "se", "format": "jsonv2",
            "polygon_geojson": 1, "polygon_threshold": 0.005, "limit": 3})
        res = http("https://nominatim.openstreetmap.org/search?" + q, timeout=60)
        hit = next(r for r in res if r.get("addresstype") == "county"
                   or r.get("type") == "administrative")
        g = hit["geojson"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        borders[code] = [ring for poly in polys for ring in poly]
        print(f"county border {len(borders):2}/{len(COUNTIES)} {name}", flush=True)
        time.sleep(1.2)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(borders, f)
    return borders


def inside(lon, lat, ring):
    hit = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


class CountyFinder:
    def __init__(self, borders):
        self.parts = []
        for code, rings in borders.items():
            for ring in rings:
                xs, ys = [p[0] for p in ring], [p[1] for p in ring]
                self.parts.append((code, ring, min(xs), max(xs), min(ys), max(ys)))
        self.cells = {}

    def county(self, lat, lon):
        # One lookup per ~3 km cell keeps this fast; near a border a name
        # may get the neighbouring county, which is harmless here
        cell = (round(lat * 30), round(lon * 15))
        if cell not in self.cells:
            clat, clon = cell[0] / 30, cell[1] / 15
            found = ""
            for code, ring, x0, x1, y0, y1 in self.parts:
                if x0 <= clon <= x1 and y0 <= clat <= y1 and inside(clon, clat, ring):
                    found = code
                    break
            self.cells[cell] = found
        return self.cells[cell]


def main():
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        root, "geodata", "gazetteer-se.tsv.gz")
    url = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
    cache = os.path.join(root, ".gazetteer-cache")
    os.makedirs(cache, exist_ok=True)

    finder = CountyFinder(county_borders(cache))
    kept, rows = {}, []
    tiles = [t for t in TILES if any(
        x0 <= t[3] and x1 >= t[1] and y0 <= t[2] and y1 >= t[0]
        for _, _, x0, x1, y0, y1 in finder.parts)]
    for n, tile in enumerate(tiles, 1):
        elements = fetch_tile(url, tile, cache)
        added = 0
        for el in elements:
            kind, name = kind_and_name(el.get("tags", {}))
            if not kind or not name or len(name) > 60:
                continue
            lat = el.get("lat", (el.get("center") or {}).get("lat"))
            lon = el.get("lon", (el.get("center") or {}).get("lon"))
            if lat is None or lon is None:
                continue
            code = finder.county(lat, lon)
            if not code:
                continue  # outside Sweden
            # Roads in tunnels, bridges and roundabouts are split into many
            # ways with the same name: keep one entry per name, kind and km.
            # Tiles overlap at their edges, so this also drops duplicates.
            same = kept.setdefault((name, kind), [])
            if any(km((lat, lon), p) < 1.5 for p in same):
                continue
            same.append((lat, lon))
            ref = el["tags"].get("ref", "") if kind == "junction" else ""
            rows.append((name, kind, f"{lat:.5f}", f"{lon:.5f}", code,
                         ref.replace("\t", " ")))
            added += 1
            alias = airport_alias(name) if kind == "airport" else None
            if alias:
                rows.append((alias, kind, f"{lat:.5f}", f"{lon:.5f}", code, ""))
        print(f"tile {n:3}/{len(tiles)} {tile}: {added} names", flush=True)

    rows.sort(key=lambda r: (r[4], r[0], r[1]))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as f:
        f.write("# RDS Guard place-name registry. Data (c) OpenStreetMap "
                "contributors, ODbL 1.0, https://www.openstreetmap.org/copyright\n")
        f.write(f"# built {time.strftime('%Y-%m-%d')} by tools/build_gazetteer.py\n")
        f.write("# name\tkind\tlat\tlon\tcounty\tref\n")
        for r in rows:
            f.write("\t".join(r) + "\n")
    print(f"Wrote {len(rows)} names to {out}")


if __name__ == "__main__":
    main()
