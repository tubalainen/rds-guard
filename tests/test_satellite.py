"""The headless satellite: its database and the move from events.db, the
purge, the health file, its settings, receivers.yml and the dongles."""

import json
import os
import subprocess
import sys
import time

import pytest

import config
import dongles
import receivers
import satellite_health

ROOT = os.path.join(os.path.dirname(__file__), "..")


# --------------------------------------------------------- move from events.db

def test_identity_and_undelivered_events_move_over(store, legacy_db, tmp_path):
    old_audio = tmp_path / "audio"
    old_audio.mkdir()
    legacy_db.set_setting("uplink_token", "a" * 43)
    legacy_db.set_setting("uplink_join_id", "11111111-2222-4333-8444-555555555555")
    legacy_db.set_setting("uplink_joined", "1")
    done = legacy_db.insert_event("traffic", "warning", "end", "0x1", {},
                                  started_at="2026-11-20T10:00:00")      # delivered
    owed = legacy_db.insert_event("traffic", "warning", "start", "0x1", {},
                                  started_at="2026-11-27T10:00:00", queued=True)
    legacy_db.end_event(owed, "2026-11-27T10:01:00", 60)
    (old_audio / f"{owed}.ogg").write_bytes(b"OggS-owed")
    legacy_db.update_event_audio(owed, f"/data/audio/{owed}.ogg", queued=True)
    spool = tmp_path / "spool"

    assert store.migrate_from(legacy_db.path, old_audio, spool) == 1
    assert store.get_setting("uplink_token") == "a" * 43
    assert store.get_setting("uplink_joined") == "1"
    rows, total = store.query_events()
    assert total == 1 and rows[0]["state"] == "end"
    assert (spool / f"{owed}.ogg").read_bytes() == b"OggS-owed"
    kinds = [r["kind"] for r in store._conn().execute("SELECT kind FROM outbox ORDER BY id")]
    assert kinds == ["event", "audio"]
    # Once only; the old database is left as it was
    assert store.migrate_from(legacy_db.path, old_audio, spool) == 0
    assert legacy_db._conn().execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    assert done


def test_an_event_open_in_the_old_database_is_ended_and_sent(store, legacy_db, tmp_path):
    """Its start reached the aggregator; its end never did."""
    open_id = legacy_db.insert_event("traffic", "warning", "start", "0x1", {},
                                     started_at="2026-11-27T10:00:00")
    store.migrate_from(legacy_db.path, tmp_path, tmp_path / "spool")
    store.enable_outbox()
    assert store.close_stale_events() == 1
    [row], _ = store.query_events()
    assert row["state"] == "end"
    assert [r["kind"] for r in store._conn().execute("SELECT kind FROM outbox")] == ["event", "event"]
    assert open_id


def test_no_old_database_is_fine(store, tmp_path):
    assert store.migrate_from(str(tmp_path / "nope.db"), tmp_path, tmp_path / "spool") == 0


def test_purge_keeps_what_is_owed(store, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    store.enable_outbox()
    old = store.insert_event("traffic", "warning", "start", "0x1", {})
    owed = store.insert_event("traffic", "warning", "start", "0x1", {})
    for eid in (old, owed):
        (spool / f"{eid}.ogg").write_bytes(b"OggS")
        store.update_event_audio(eid, f"{eid}.ogg")
    conn = store._conn()
    conn.execute("UPDATE events SET created_at = '2020-01-01T00:00:00'")
    conn.execute("DELETE FROM outbox WHERE event_id = ?", (old,))   # delivered
    conn.commit()
    store.mark_audio_delivered(old)
    assert store.purge(7, spool) == 1
    assert store.get_event(old) is None and not (spool / f"{old}.ogg").exists()
    assert store.get_event(owed) and (spool / f"{owed}.ogg").exists()


# ------------------------------------------------------------------ health

def test_health_file(tmp_path):
    path = str(tmp_path / "health.json")
    ok, msg = satellite_health.check(path)
    assert not ok and "no health file" in msg
    satellite_health.write(path, {"receivers": [
        {"state": "running", "stations": [{"groups_per_sec": 11.2}]},
        {"state": "error", "stations": []}], "pipeline": {"error": "x"}})
    ok, msg = satellite_health.check(path)
    assert ok and "1/2 receivers running" in msg
    ok, msg = satellite_health.check(path, now=time.time() + 120)
    assert not ok and "old" in msg
    satellite_health.write(path, {"receivers": [{"state": "paused", "stations": []}]})
    assert satellite_health.check(path)[0]            # calibrating its gain
    satellite_health.write(path, {"receivers": [], "pipeline": {"error": "No RTL-SDR dongle found"}})
    ok, msg = satellite_health.check(path)
    assert not ok and msg == "No RTL-SDR dongle found"


# ---------------------------------------------------------------- settings

def _run(code, **env):
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("UPLINK_", "AGGREGATOR_", "MQTT_", "FM_", "RTL_"))}
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**clean, **env},
                          capture_output=True, text=True)


def test_without_an_aggregator_it_refuses_to_start():
    out = _run("import config; print(config.problems())")
    assert "needs an aggregator" in out.stdout
    out = _run("import config; print(config.problems())", AGGREGATOR_URL="ftp://x")
    assert "must start with http" in out.stdout
    out = _run("import config; print(config.problems(), config.UPLINK_URL)",
               UPLINK_URL="http://aggregator:8081/")
    assert out.stdout.strip() == "[] http://aggregator:8081"


def test_main_exits_with_the_reason(tmp_path):
    out = _run("import rds_guard; rds_guard.main()", DATA_DIR=str(tmp_path))
    assert out.returncode == 1 and "needs an aggregator" in out.stderr


def test_retired_settings_are_named(monkeypatch):
    monkeypatch.setenv("PUBLISH_MODE", "all")
    monkeypatch.setenv("LOCAL_PROCESSING", "false")
    monkeypatch.setenv("MQTT_ENABLED", "true")       # the aggregator's, in a shared .env
    named = set(config.retired_in_use())
    assert {"PUBLISH_MODE", "LOCAL_PROCESSING"} <= named and "MQTT_ENABLED" not in named


def test_recording_is_capped():
    out = _run("import config; print(config.MAX_RECORDING_SEC)", MAX_RECORDING_SEC="5000")
    assert out.stdout.strip() == "1200"


# ---------------------------------------------------------------- receivers

def test_receivers_yml():
    rs = receivers.parse({"receivers": [
        {"serial": "00000101", "name": "tak", "frequencies": ["103.5M", 102.9]},
        {"serial": 102, "frequencies": "99.1M", "gain": 19.7, "ppm": 12}]})
    tak, other = rs
    assert tak.id == "tak" and tak.frequencies == ["103.5M", "102.9M"]
    assert tak.gain == "auto" and tak.ppm == "auto"
    assert tak.center_hz == 103_200_000
    assert other.id == "102" and other.gain == 19.7 and other.ppm == 12
    assert other.center_hz == 99_100_000 + receivers.SINGLE_OFFSET_HZ


@pytest.mark.parametrize("doc,words", [
    ({}, "'receivers:' list"),
    ({"receivers": [{"frequencies": ["103.5M"]}]}, "'serial' is required"),
    ({"receivers": [{"serial": "1", "frequencies": ["103.5M", "105.9M"]}]}, "2 MHz"),
    ({"receivers": [{"serial": "1", "frequencies": ["88M", "88.2M", "88.4M", "88.6M", "88.8M"]}]},
     "at most 4"),
    ({"receivers": [{"serial": "1", "frequencies": ["103.5M"]},
                    {"serial": "1", "frequencies": ["99.1M"]}]}, "listed twice"),
    ({"receivers": [{"serial": "1", "frequencies": ["1234M"]}]}, "not an FM"),
    ({"receivers": [{"serial": "1", "frequencies": ["103.5M"], "gain": "hög"}]}, "'auto' or a number"),
])
def test_receivers_yml_mistakes(doc, words):
    with pytest.raises(receivers.ReceiverError, match=words):
        receivers.parse(doc)


def test_same_frequency_on_two_dongles_warns():
    rs = receivers.parse({"receivers": [{"serial": "1", "frequencies": ["103.5M"]},
                                        {"serial": "2", "frequencies": ["103.5M"]}]})
    assert "also on receiver 1" in rs[1].warnings[0]


def test_without_receivers_yml_one_dongle_uses_fm_frequency(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FM_FREQUENCIES", ["103.5M", "102.9M"])
    monkeypatch.setattr(config, "RTL_DEVICE_SERIAL", "")
    monkeypatch.setattr(config, "RTL_GAIN", "")
    one = [dongles.Dongle(0, "Realtek", "RTL2838UHIDIR", "00000001")]
    [r] = receivers.load(str(tmp_path / "none.yml"), dongles=one)
    assert r.serial == "00000001" and r.frequencies == ["103.5M", "102.9M"]
    two = one + [dongles.Dongle(1, "Realtek", "RTL2838UHIDIR", "00000002")]
    with pytest.raises(receivers.ReceiverError, match="receivers.yml"):
        receivers.load(str(tmp_path / "none.yml"), dongles=two)
    with pytest.raises(receivers.ReceiverError, match="No RTL-SDR"):
        receivers.load(str(tmp_path / "none.yml"), dongles=[])
    monkeypatch.setattr(config, "RTL_GAIN", "8")
    [r] = receivers.load(str(tmp_path / "none.yml"), dongles=one)
    assert r.gain == "auto" and "not used any more" in r.warnings[0]


def test_receivers_file_is_read(tmp_path):
    path = tmp_path / "receivers.yml"
    path.write_text('receivers:\n  - serial: "00000101"\n    frequencies: [103.5M]\n',
                    encoding="utf-8")
    [r] = receivers.load(str(path))
    assert r.serial == "00000101"
    path.write_text("receivers: [", encoding="utf-8")
    with pytest.raises(receivers.ReceiverError, match="not valid YAML"):
        receivers.load(str(path))


# ---------------------------------------------------------------- dongles

RTL_TEST = """Found 2 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: 00000001
  1:  Realtek, RTL2838UHIDIR, SN: 00000001

Using device 0: Generic RTL2832U OEM
Found Rafael Micro R820T tuner
Supported gain values (29): 0.0 0.9 1.4 2.7 3.7 7.7 8.7 12.5 14.4 15.7 16.6 19.7 20.7 22.9 25.4 28.0 29.7 32.8 33.8 36.4 37.2 38.6 40.2 42.1 43.4 43.9 44.5 48.0 49.6
"""


def test_rtl_test_is_read():
    ds = dongles.parse_devices(RTL_TEST)
    assert [(d.index, d.serial) for d in ds] == [(0, "00000001"), (1, "00000001")]
    gains = dongles.parse_gains(RTL_TEST)
    assert len(gains) == 29 and gains[0] == 0.0 and gains[-1] == 49.6
    assert dongles.nearest(gains, 20) == 19.7


def test_duplicate_serials_are_explained():
    ds = dongles.parse_devices(RTL_TEST)
    assert dongles.duplicates(ds) == ["00000001"]
    with pytest.raises(dongles.DongleError, match="rtl_eeprom"):
        dongles.index_of("00000001", ds)
    with pytest.raises(dongles.DongleError, match="No RTL-SDR with serial number 9"):
        dongles.index_of("9", ds[:1])
    assert dongles.index_of("00000001", ds[:1]) == 0


# ------------------------------------------------------------- the satellite

def test_two_dongles_with_the_same_station_keep_their_own_state(store):
    """The same PI on two receivers: each its own TA state and event."""
    import rds_guard as rg
    store.enable_outbox()
    a, b = rg.Station("tak", "103.5M"), rg.Station("källare", "103.5M")
    for st in (a, b):
        rg.process_group(st, {"pi": "0xE824", "ta": False, "timestamp": "2026-11-27T10:00:00"})
    rg.process_group(a, {"pi": "0xE824", "ta": True, "timestamp": "2026-11-27T10:00:05"})
    rg.process_group(b, {"pi": "0xE824", "ta": True, "timestamp": "2026-11-27T10:00:06"})
    rows, total = store.query_events()
    assert total == 2 and sorted(r["receiver"] for r in rows) == ["källare", "tak"]
    assert a.engine.busy() and b.engine.busy()


def test_first_ta_on_starts_but_first_ta_off_ends_nothing(store):
    import rds_guard as rg
    st = rg.Station("tak", "103.5M")
    rg.process_group(st, {"pi": "0x1", "ta": False, "timestamp": "2026-11-27T10:00:00"})
    assert store.query_events()[1] == 0
    st2 = rg.Station("tak", "99.1M")
    rg.process_group(st2, {"pi": "0x2", "ta": True, "timestamp": "2026-11-27T10:00:00"})
    assert store.query_events()[1] == 1


def test_heartbeat_status_lists_receivers_and_stations(store):
    import rds_guard as rg
    from pipeline import ReceiverPipeline
    r = receivers.parse({"receivers": [{"serial": "1", "name": "tak",
                                        "frequencies": ["103.5M"]}]})[0]
    st = rg.Station("tak", "103.5M")
    rg.process_group(st, {"pi": "0xE824", "ps": "P4 Värml", "tp": True,
                          "timestamp": "2026-11-27T10:00:00"})
    sat = rg.Satellite()
    sat.pipelines = [ReceiverPipeline(r, [{"frequency": "103.5M", "station": st,
                                          "callback": st.on_line}], lambda: (20.0, 0))]
    snap = sat.status()
    [rec] = snap["receivers"]
    assert rec["id"] == "tak" and rec["serial"] == "1"
    assert rec["stations"][0]["ps"] == "P4 Värml" and rec["stations"][0]["tp"] is True
    assert snap["stations"][0]["pi"] == "0xE824"
    json.dumps(snap)
