"""Road registry: where the numbered roads run, for placing traffic messages.

Built by tools/build_roads.py from OpenStreetMap and bundled in the Docker
image (geodata/roads-se.tsv.gz), so lookups run locally without any network
access. Without the file everything works as before, only without roads.

It is used to:
  - tell apart places with the same name: "Rv 61 vid X" means the X next to
    Rv 61
  - put the pin on the road the message is about, not beside it
  - point the direction arrow along the road ("i riktning mot Grums")

Data (c) OpenStreetMap contributors, ODbL.
"""

import gzip
import logging
import math
import os
import re
from array import array

log = logging.getLogger("rds-guard")

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "geodata", "roads-se.tsv.gz")
CELL_DEG = 0.05         # grid cell for the lookup index, about 5 x 3 km


def road_key(road):
    """'E18' -> 'E18'; 'Rv 61', 'Lv 236', 'Väg 61' -> '61'; else None."""
    m = re.match(r"^\s*(?:(E)\s*(\d{1,2})|[^\d]*?(\d{1,4}))\s*$", road or "")
    if not m:
        return None
    return f"E{int(m.group(2))}" if m.group(1) else str(int(m.group(3)))


def _decode(text):
    lats, lons = array("d"), array("d")
    lat = lon = 0
    for i, pair in enumerate(text.split(";")):
        a, b = pair.split(",")
        lat, lon = (int(a), int(b)) if i == 0 else (lat + int(a), lon + int(b))
        lats.append(lat / 1e5)
        lons.append(lon / 1e5)
    return lats, lons


class Roads:
    """Lines per road key, with a grid index of their segments."""

    def __init__(self, lines=()):
        # lines: [(key, county, [(lat, lon), ...])]
        self.lines = []             # (key, county, lats, lons)
        self._grid = {}             # (key, cell) -> [(line, seg)]
        for key, county, pts in lines:
            self._add(key, county, array("d", (p[0] for p in pts)),
                      array("d", (p[1] for p in pts)))

    def __len__(self):
        return len(self.lines)

    def __bool__(self):
        return bool(self.lines)

    def _add(self, key, county, lats, lons):
        if len(lats) < 2:
            return
        li = len(self.lines)
        self.lines.append((key, county, lats, lons))
        for s in range(len(lats) - 1):
            for cell in _cells(lats[s], lons[s], lats[s + 1], lons[s + 1]):
                self._grid.setdefault((key, cell), []).append((li, s))

    @classmethod
    def load(cls, path=None, centers=None, radius_km=250):
        """Read the registry, keeping lines with a point within radius_km of
        any center. A missing file gives an empty registry."""
        path = path or os.environ.get("ROADS_PATH") or DEFAULT_PATH
        reg = cls()
        if not os.path.exists(path):
            log.info("Roads: no road registry at %s, placing without roads", path)
            return reg
        centers = [c for c in (centers or []) if c]
        boxes = [(c[0] - radius_km / 111.0, c[0] + radius_km / 111.0,
                  c[1] - radius_km / (111.0 * math.cos(math.radians(c[0]))),
                  c[1] + radius_km / (111.0 * math.cos(math.radians(c[0]))))
                 for c in centers]
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 3:
                    continue
                lats, lons = _decode(parts[2])
                if boxes and not any(
                        any(y0 <= la <= y1 and x0 <= lo <= x1
                            for la, lo in zip(lats, lons))
                        for y0, y1, x0, x1 in boxes):
                    continue
                reg._add(parts[0], parts[1], lats, lons)
        log.info("Roads: %d road lines loaded from %s", len(reg), path)
        return reg

    def has(self, road):
        key = road_key(road)
        return key is not None and any(k == key for k, *_ in self.lines)

    def nearest(self, road, point, max_km=10):
        """The point on the road nearest to point, within max_km.

        Returns (lat, lon, km, bearing) where bearing is the road's direction
        there (one of its two directions), or None.
        """
        key = road_key(road)
        if key is None:
            return None
        lat0, lon0 = point
        k = math.cos(math.radians(lat0))
        reach = int(math.ceil(max_km / (CELL_DEG * 111 * k))) + 1
        ci, cj = _cell(lat0, lon0)
        best = None
        seen = set()
        for di in range(-reach, reach + 1):
            for dj in range(-reach, reach + 1):
                for li, s in self._grid.get((key, (ci + di, cj + dj)), ()):
                    if (li, s) in seen:
                        continue
                    seen.add((li, s))
                    _, _, lats, lons = self.lines[li]
                    ay, ax = lats[s], lons[s]
                    by_, bx = lats[s + 1], lons[s + 1]
                    # local metres-ish frame (degrees of latitude)
                    px, py = (lon0 - ax) * k, lat0 - ay
                    vx, vy = (bx - ax) * k, by_ - ay
                    length = vx * vx + vy * vy
                    t = 0.0 if length == 0 else max(0.0, min(1.0, (px * vx + py * vy) / length))
                    dx, dy = px - t * vx, py - t * vy
                    d = math.hypot(dx, dy) * 111.2
                    if best is None or d < best[2]:
                        best = (ay + t * (by_ - ay), ax + t * (bx - ax), d,
                                (math.degrees(math.atan2(vx, vy)) + 360) % 360)
        if best is None or best[2] > max_km:
            return None
        return best

    def distance_km(self, road, point, max_km=10):
        """km from point to the road, or None if the road is not within max_km."""
        hit = self.nearest(road, point, max_km)
        return None if hit is None else hit[2]


def _cell(lat, lon):
    return (int(math.floor(lat / CELL_DEG)), int(math.floor(lon / CELL_DEG)))


def _cells(lat1, lon1, lat2, lon2):
    i1, j1 = _cell(min(lat1, lat2), min(lon1, lon2))
    i2, j2 = _cell(max(lat1, lat2), max(lon1, lon2))
    return [(i, j) for i in range(i1, i2 + 1) for j in range(j1, j2 + 1)]


def along(bearing_road, bearing_target):
    """The road's direction (bearing_road or its opposite) that goes most
    toward bearing_target."""
    def diff(a, b):
        return abs((a - b + 180) % 360 - 180)
    rev = (bearing_road + 180) % 360
    return bearing_road if diff(bearing_road, bearing_target) <= diff(rev, bearing_target) else rev
