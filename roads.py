"""The numbered roads (E-roads, riksvägar, länsvägar) for placing messages.

The lines come from OpenStreetMap (tools/build_roads.py) and are bundled in
geodata/roads-se.json.gz, so nothing is fetched while RDS Guard runs. Only
the roads near the receiver are kept in memory.

A traffic message nearly always names the road ("E18", "riksväg 61"). With
the road's line the placing can:
  - pick, among places with the same name, the one by that road,
  - put the pin on the road,
  - tell the reader which roads a candidate place lies by.

Data (c) OpenStreetMap contributors, ODbL.
"""

import gzip
import json
import logging
import math
import os

log = logging.getLogger("rds-guard")

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "geodata", "roads-se.json.gz")


def _xy(lat, lon, lat0):
    return (lon * 111.320 * math.cos(math.radians(lat0)), lat * 110.540)


class Line:
    """One polyline with its bounding box (degrees)."""

    __slots__ = ("pts", "box")

    def __init__(self, flat):
        self.pts = [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]
        lats = [p[0] for p in self.pts]
        lons = [p[1] for p in self.pts]
        self.box = (min(lats), min(lons), max(lats), max(lons))

    def near_box(self, p, km):
        dlat = km / 110.5
        dlon = km / (111.3 * max(0.2, math.cos(math.radians(p[0]))))
        return (self.box[0] - dlat <= p[0] <= self.box[2] + dlat
                and self.box[1] - dlon <= p[1] <= self.box[3] + dlon)

    def nearest(self, p):
        """(km, (lat, lon)) of the point on the line nearest p."""
        px, py = _xy(p[0], p[1], p[0])
        best = (float("inf"), None)
        for a, b in zip(self.pts, self.pts[1:]):
            ax, ay = _xy(a[0], a[1], p[0])
            bx, by = _xy(b[0], b[1], p[0])
            dx, dy = bx - ax, by - ay
            t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, (
                (px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
            d = math.hypot(px - ax - t * dx, py - ay - t * dy)
            if d < best[0]:
                best = (d, (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
        return best


class Roads:
    def __init__(self, roads=None):
        self.roads = roads or {}          # ref -> [Line]

    def __len__(self):
        return len(self.roads)

    def __contains__(self, ref):
        return ref in self.roads

    @classmethod
    def load(cls, path=None, centers=None, radius_km=250):
        """Read the road file, keeping lines that come within radius_km of
        a center. A missing file gives no roads (placing works as before)."""
        if path == "":
            return cls()        # asked for none (tests with their own registry)
        path = path or os.environ.get("ROADS_PATH") or DEFAULT_PATH
        if not os.path.exists(path):
            log.info("Roads: %s not found, placing without the road network", path)
            return cls()
        with gzip.open(path, "rt", encoding="utf-8") as f:
            doc = json.load(f)
        centers = [c for c in (centers or []) if c]
        roads, n = {}, 0
        for ref, lines in (doc.get("roads") or {}).items():
            for flat in lines:
                line = Line(flat)
                if centers and not any(line.near_box(c, radius_km) for c in centers):
                    continue
                roads.setdefault(ref, []).append(line)
                n += 1
        log.info("Roads: %d roads (%d lines) loaded from %s", len(roads), n, path)
        return cls(roads)

    def nearest(self, ref, p, max_km=50):
        """(km, (lat, lon)) of the nearest point on road ref, or None when
        the road is not known or further than max_km."""
        best = None
        for line in self.roads.get(ref, ()):
            if not line.near_box(p, max_km):
                continue
            d = line.nearest(p)
            if best is None or d[0] < best[0]:
                best = d
        return best if best and best[0] <= max_km else None

    def distance(self, ref, p, max_km=50):
        """km from p to road ref; None when unknown or far."""
        hit = self.nearest(ref, p, max_km)
        return hit[0] if hit else None

    def roads_near(self, p, km=1.5, refs=None):
        """The roads (of refs, or all) that pass within km of p."""
        out = []
        for ref in (refs if refs is not None else self.roads):
            d = self.distance(ref, p, km)
            if d is not None:
                out.append(ref)
        return out
