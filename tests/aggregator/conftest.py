"""Aggregator fixtures: temporary store and audio directory."""

import pytest

from aggregator import config
from aggregator import store as agg_store_mod


@pytest.fixture
def agg_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIO_DIR", str(tmp_path / "audio"))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "agg.db"))
    agg_store_mod.close()
    agg_store_mod.init_db(str(tmp_path / "agg.db"))
    yield agg_store_mod
    agg_store_mod.close()


class RadioStore:
    """The aggregator's store, with the satellite-era helpers the map and
    Trafikverket tests were written with (an event is its uid here)."""

    def __init__(self, store_mod):
        self._s = store_mod
        self._node = None
        self._rev = {}

    def __getattr__(self, name):
        return getattr(self._s, name)

    @staticmethod
    def _z(ts):
        return (str(ts).replace(" ", "T")[:19] + "Z") if ts else None

    def _node_id(self):
        if self._node is None:
            self._node = (self._s.get_node_by_name("Satellit")
                          or self._s.create_node("Satellit")[0])["id"]
        return self._node

    def insert_event(self, event_type, severity, state, pi, data_payload,
                     station_ps=None, frequency=None, radiotext=None,
                     started_at=None, ended_at=None):
        import uuid
        uid = str(uuid.uuid4())
        self._rev[uid] = 1
        self._s.upsert_event(self._node_id(), uid, {
            "rev": 1, "type": event_type, "state": state, "pi": pi,
            "station_ps": station_ps, "long_ps": None, "frequency": frequency,
            "prog_type": None, "radiotext": radiotext or [],
            "started_at": self._z(started_at), "ended_at": self._z(ended_at),
            "duration_sec": None, "sent_at": self._z(started_at) or self._s.now_iso()},
            30, 120)
        return uid

    def end_event(self, uid, ended_at, duration_sec, radiotext_list=None):
        row = self._s.get_event(uid)
        self._rev[uid] += 1
        self._s.upsert_event(self._node_id(), uid, {
            "rev": self._rev[uid], "type": row["type"], "state": "end", "pi": row["pi"],
            "station_ps": row["station_ps"], "long_ps": None, "frequency": row["frequency"],
            "prog_type": None, "radiotext": radiotext_list or row["radiotext"],
            "started_at": row["started_at"], "ended_at": self._z(ended_at),
            "duration_sec": duration_sec, "sent_at": self._z(ended_at)}, 30, 120)

    def update_event_transcription(self, uid, text, status="done", duration_sec=None):
        self._s.set_transcription(uid, text, status, duration_sec)


@pytest.fixture
def store(agg_store):
    """The map and Trafikverket tests' store: the aggregator's."""
    return RadioStore(agg_store)
