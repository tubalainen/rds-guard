"""Aggregator fixtures: temporary store and audio directory."""

import pytest

from aggregator import config, store


@pytest.fixture
def agg_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIO_DIR", str(tmp_path / "audio"))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "agg.db"))
    store.close()
    store.init_db(str(tmp_path / "agg.db"))
    yield store
    store.close()
