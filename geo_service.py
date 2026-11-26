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
            whole = gz.Gazetteer.load(self._path)
            place = whole.locate(text)
        except OSError as e:
            log.error("Place-name registry could not be read: %s", e)
            place = None
        whole = None  # the whole country is only needed for this lookup
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
