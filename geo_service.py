"""Geolocation service shared by the satellite and the aggregator.

Holds the place-name registry for each node's area and the engine
(rules, or Claude with rules as fallback). Storage is left to the caller:
the satellite and the aggregator keep incidents in their own databases
with the helpers in geolocate.py.
"""

import logging
import threading

import gazetteer as gz
import geolocate

log = logging.getLogger("rds-guard")

AREA_KM = 250          # registry kept in memory around each node


class GeoService:
    def __init__(self, geolocator, keyterms_auto=True, prompt_auto=True,
                 area_km=geolocate.DEFAULT_AREA_KM, gazetteer_path=None):
        self._geo = geolocator
        self.keyterms_auto = keyterms_auto
        self.prompt_auto = prompt_auto
        self.area_km = area_km
        self._path = gazetteer_path
        self._lock = threading.Lock()
        self._located = {}      # location text -> Place or None
        self._areas = {}        # rounded center -> Gazetteer

    @property
    def engine(self):
        return self._geo.engine if self._geo else "none"

    # -- registry -----------------------------------------------------------

    def locate_node(self, text):
        """'Örebro, Örebro län' -> Place (cached), or None if not found."""
        key = (text or "").strip().lower()
        if not key:
            return None
        with self._lock:
            if key in self._located:
                return self._located[key]
        try:
            place = gz.locate_in_file(text, self._path)
        except OSError as e:
            log.error("Place-name registry could not be read: %s", e)
            place = None
        if place:
            log.info("Node location '%s' -> %s (%s, %s) %.4f, %.4f", text,
                     place.name, place.kind,
                     gz.COUNTIES.get(place.county, place.county),
                     place.lat, place.lon)
        else:
            log.warning("Node location '%s' was not found in the place-name "
                        "registry: write it as 'Ort, Län', e.g. 'Örebro, "
                        "Örebro län'", text)
        with self._lock:
            self._located[key] = place
        return place

    def area(self, center):
        """Registry for names within AREA_KM of center (loaded once)."""
        key = (round(center[0], 1), round(center[1], 1))
        with self._lock:
            reg = self._areas.get(key)
        if reg is None:
            reg = gz.Gazetteer.load(self._path, centers=[center],
                                    radius_km=AREA_KM)
            with self._lock:
                self._areas[key] = reg
        return reg

    # -- transcription hints ------------------------------------------------

    def hints(self, center, history=()):
        """Words for the transcriber to listen for near center."""
        if center is None:
            return None
        out = {}
        reg = self.area(center)
        if self.keyterms_auto:
            out["keyterms"] = reg.word_list(center, limit=300, history=history)
        if self.prompt_auto:
            # Whisper reads a short prompt only: earlier names first
            names = reg.word_list(center, limit=40, history=history)
            out["prompt"] = "Trafikmeddelande. " + ", ".join(names) + "."
        return out or None

    # -- reading a message ----------------------------------------------------

    def locate(self, text, center, location_label="", radiotext=None,
               open_incidents=()):
        """Place the incidents in a transcription. Never raises."""
        if self._geo is None or center is None or not (text or "").strip():
            return None
        try:
            return self._geo.locate(text, self.area(center), center,
                                    location_label, radiotext, open_incidents)
        except Exception:  # noqa: BLE001 - geolocation must not break alerts
            log.exception("Geolocation failed")
            return geolocate.GeoResult("rules", [], None, "internal error")


def create_service(engine, api_key="", model="claude-haiku-5-5", timeout=60,
                   place_names=True, area_km=geolocate.DEFAULT_AREA_KM):
    """GeoService for GEO_ENGINE. With "none" it still supplies place names
    to the transcriber (place_names), but places nothing on the map."""
    geo = geolocate.create_geolocator(engine, api_key=api_key, model=model,
                                      timeout=timeout, area_km=area_km)
    return GeoService(geo, keyterms_auto=place_names, prompt_auto=place_names,
                      area_km=area_km)


# ---------------------------------------------------------------------------
# Placing older messages: the first time the history map is shown
# ---------------------------------------------------------------------------

class Backfill:
    """Places the messages stored before the map existed, oldest first, in
    a background thread. Each one is placed as at the time it was broadcast,
    so it joins the incidents of that day, not today's.

    pending(): the messages waiting, oldest first.
    place(item): places one; returns True if it was put on the map, False
        if no place was found, None if it cannot be placed now (left as
        pending, e.g. its node has no location).
    count(): how many pending messages can be placed now.
    """

    PAUSE_SEC = 0.1        # between messages, not to crowd out live ones

    def __init__(self, pending, place, count):
        self._pending = pending
        self._place = place
        self._count = count
        self._lock = threading.Lock()
        self._thread = None
        self._state = {"running": False, "total": 0, "done": 0, "placed": 0,
                       "skipped": 0, "finished": False}

    def status(self, available=True, reason=None):
        with self._lock:
            st = dict(self._state)
        try:
            st["pending"] = self._count()
        except Exception:  # noqa: BLE001 - status must not fail the map
            log.exception("Could not count unplaced messages")
            st["pending"] = 0
        st["available"] = bool(available)
        if reason:
            st["reason"] = reason
        # count() only counts messages that can be placed now
        st["runnable"] = (st["available"] and not st["running"]
                          and st["pending"] > 0)
        return st

    def start(self):
        """Start a run unless one is running. Returns True if started."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._state.update(running=True, total=0, done=0, placed=0,
                               skipped=0, finished=False)
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="geo-backfill")
            self._thread.start()
        return True

    def _run(self):
        import time
        try:
            items = list(self._pending())
            with self._lock:
                self._state["total"] = len(items)
            log.info("Placing %d older messages on the map", len(items))
            for item in items:
                try:
                    res = self._place(item)
                except Exception:  # noqa: BLE001 - one bad message, go on
                    log.exception("Could not place an older message")
                    res = False
                with self._lock:
                    self._state["done"] += 1
                    if res:
                        self._state["placed"] += 1
                    elif res is None:
                        self._state["skipped"] += 1
                time.sleep(self.PAUSE_SEC)
            with self._lock:
                st = dict(self._state)
            log.info("Older messages placed: %d of %d on the map, %d left "
                     "(no receiver location)", st["placed"], st["total"],
                     st["skipped"])
        except Exception:
            log.exception("Placing older messages failed")
        finally:
            with self._lock:
                self._state.update(running=False, finished=True)
