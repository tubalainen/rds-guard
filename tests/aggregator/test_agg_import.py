"""Importing a standalone satellite's history into the aggregator."""

import json

import pytest

import event_store
import geolocate
import notify_rules
from aggregator import import_satellite as imp


def _placement(label="E18 Bergviksmotet"):
    return geolocate.Placement(
        lat=59.379, lon=13.432, radius_m=500, road="E18", label=label,
        what="olycka", level="junction", names=[label],
        evidence="Olycka vid Bergviksmotet.", kind="new")


def _area():
    m = geolocate.Mention(what="halka")
    m.area, m.evidence = "Värmland", "Halt."
    return m


@pytest.fixture
def satellite(store, tmp_path):
    """A satellite database the way the satellite itself writes it."""
    audio = tmp_path / "sat-audio"
    audio.mkdir()
    # 1: traffic with audio, transcription, a map incident and an area warning
    a = store.insert_event("traffic", "warning", "start", "0xE824",
                           {"station": {"long_ps": "P4 Värmland"}, "prog_type": "News"},
                           station_ps="P4 Värml", frequency="103.5M",
                           radiotext=["Trafik"], started_at="2026-11-20T10:00:00")
    store.end_event(a, "2026-11-20T10:01:00", 60)
    (audio / f"event_{a}.ogg").write_bytes(b"OggS" + b"\0" * 40)
    (audio / f"event_{a}.wav").write_bytes(b"RIFF")
    store.update_event_audio(a, f"/data/audio/event_{a}.ogg")
    store.update_event_transcription(a, "Olycka vid Bergviksmotet.", "done", 3.0)
    result = geolocate.GeoResult("rules", [_placement()],
                                 areas=[_area()])
    store.save_geolocation(a, result, "done", at="2026-11-20T10:01:00")
    store.log_notification(a, "sent", rule={"id": 1, "name": "Trafik"},
                           topic="trafik", title="Olycka")
    # 2: emergency without audio, transcription never finished
    b = store.insert_event("emergency", "critical", "active", "0xE824", {},
                           station_ps="P4 Värml", frequency="103.5M",
                           started_at="2026-11-21T08:00:00")
    store.end_event(b, "2026-11-21T08:02:00", 120)
    # 3: audio file missing on disk, transcription stuck at "transcribing"
    c = store.insert_event("traffic", "warning", "start", "0xE824", {},
                           station_ps="P4 Värml", started_at="2026-11-22T07:00:00")
    store.end_event(c, "2026-11-22T07:01:00", 60)
    store.update_event_audio(c, f"/data/audio/event_{c}.ogg")
    store.update_event_transcription_status(c, "transcribing")
    # A push rule of its own besides the defaults
    store.seed_rules_once(notify_rules.DEFAULT_RULES)
    store.create_rule(notify_rules.validate(
        {"name": "Nattlarm", "enabled": True, "event_types": ["emergency"],
         "priority": "critical", "topic": "natt"}))
    conn = event_store._conn()
    conn.commit()
    uids = {r["id"]: r["event_uid"] for r in conn.execute("SELECT id, event_uid FROM events")}
    return {"db": str(tmp_path / "events.db"), "audio": audio, "ids": (a, b, c), "uids": uids}


def _run(satellite, node, dry_run=False):
    src = imp.open_source(satellite["db"])
    return imp.Importer(src, satellite["audio"], node, dry_run=dry_run).run()


def test_imports_events_audio_text_and_map(agg_store, satellite, tmp_path):
    node, _ = agg_store.create_node("docker01")
    counts = _run(satellite, node)
    assert counts["events"] == 3 and counts["skipped"] == 0
    assert counts["audio"] == 1 and counts["audio_missing"] == 1
    a, b, c = satellite["ids"]
    uid_a = satellite["uids"][a]

    ev = agg_store.get_event(uid_a)
    assert ev["node_name"] == "docker01"
    assert ev["state"] == "end" and ev["started_at"] == "2026-11-20T10:00:00Z"
    assert ev["long_ps"] == "P4 Värmland" and ev["prog_type"] == "News"
    assert ev["transcription"] == "Olycka vid Bergviksmotet."
    assert ev["transcription_status"] == "done" and ev["geo_status"] == "done"
    assert ev["audio_path"] == f"{uid_a}.ogg"
    assert (tmp_path / "audio" / f"{uid_a}.ogg").read_bytes().startswith(b"OggS")
    # Never alerted, never transcribed again
    assert agg_store.alert_candidates() == []
    assert agg_store.pending_transcriptions() == []
    uid_c = satellite["uids"][c]
    assert agg_store.get_event(uid_c)["audio_path"] is None
    assert agg_store.get_event(satellite["uids"][b])["type"] == "emergency"

    # The map: incident and area warning keyed by the event's uid
    places = agg_store.incidents_for_events([uid_a])
    assert places[uid_a][0]["label"] == "E18 Bergviksmotet"
    conn = agg_store._conn()
    [area] = conn.execute("SELECT * FROM area_warnings").fetchall()
    assert area["event_key"] == uid_a and area["seen_at"].endswith("Z")
    # The push log of the message, and the satellite's extra rule
    assert agg_store.notify_for_events([uid_a])[uid_a][0]["topic"] == "trafik"
    names = sorted(r["name"] for r in agg_store.list_rules())
    assert names == ["Nattlarm", "Trafik", "VMA"]


def test_running_twice_adds_nothing(agg_store, satellite):
    node, _ = agg_store.create_node("docker01")
    _run(satellite, node)
    again = _run(satellite, node)
    assert again["events"] == 0 and again["skipped"] == 3
    assert again["incidents"] == 0 and again["rules"] == 0
    conn = agg_store._conn()
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM notify_rules").fetchone()[0] == 3


def test_events_the_uplink_delivered_are_skipped_with_their_map(agg_store, satellite):
    """An event already sent by the uplink is kept as the aggregator has it,
    and its map incident is not added a second time."""
    node, _ = agg_store.create_node("docker01")
    a = satellite["ids"][0]
    agg_store.upsert_event(node["id"], satellite["uids"][a], {
        "rev": 3, "type": "traffic", "state": "end", "pi": "0xE824",
        "station_ps": "P4 Värml", "long_ps": None, "frequency": "103.5M",
        "prog_type": None, "radiotext": [], "started_at": "2026-11-20T10:00:00Z",
        "ended_at": "2026-11-20T10:01:00Z", "duration_sec": 60,
        "sent_at": "2026-11-20T10:01:00Z"})
    counts = _run(satellite, node)
    assert counts["events"] == 2 and counts["skipped"] == 1
    assert counts["incidents"] == 0 and counts["areas"] == 0
    assert agg_store.get_event(satellite["uids"][a])["rev"] == 3


def test_events_without_uid_get_a_stable_one(agg_store, satellite):
    conn = event_store._conn()
    conn.execute("UPDATE events SET event_uid = NULL")
    conn.commit()
    node, _ = agg_store.create_node("docker01")
    _run(satellite, node)
    again = _run(satellite, node)
    assert again["skipped"] == 3 and again["events"] == 0
    # Map links follow the derived uid
    assert agg_store._conn().execute(
        "SELECT COUNT(*) FROM incident_events").fetchone()[0] == 1


def test_own_rules_are_kept(agg_store, satellite):
    agg_store.create_rule(notify_rules.validate(
        {"name": "Min regel", "enabled": True, "event_types": ["traffic"],
         "priority": "normal", "topic": "min"}))
    node, _ = agg_store.create_node("docker01")
    _run(satellite, node)
    names = sorted(r["name"] for r in agg_store.list_rules())
    # The aggregator's own rule stays; the satellite's rules are added
    assert names == ["Min regel", "Nattlarm", "Trafik", "VMA"]


def test_dry_run_writes_nothing(agg_store, satellite, tmp_path):
    node, _ = agg_store.create_node("docker01")
    counts = _run(satellite, node, dry_run=True)
    assert counts["events"] == 3
    conn = agg_store._conn()
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert not (tmp_path / "audio").exists() or not any((tmp_path / "audio").iterdir())


def test_command_line(agg_store, satellite, monkeypatch, capsys):
    monkeypatch.setattr("aggregator.privileges.drop_privileges", lambda: False)
    monkeypatch.setattr("aggregator.privileges.require_writable", lambda: None)
    imp.main([satellite["db"], str(satellite["audio"]), "--node", "docker01"])
    out = capsys.readouterr()
    assert "UPLINK_TOKEN=" in out.out          # the new node's setup lines
    assert "1/3 #" in out.err and "Done: 3 events imported" in out.err
    assert agg_store.get_node_by_name("docker01") is not None
    json.dumps(agg_store.list_nodes())          # serialisable as before


def test_events_older_than_the_retention_are_skipped(agg_store, satellite, monkeypatch):
    from aggregator import config
    conn = event_store._conn()
    a = satellite["ids"][0]
    conn.execute("UPDATE events SET created_at = '2020-01-01T00:00:00' WHERE id = ?", (a,))
    conn.commit()
    monkeypatch.setattr(config, "EVENT_RETENTION_DAYS", 90)
    node, _ = agg_store.create_node("docker01")
    counts = _run(satellite, node)
    assert counts["too_old"] == 1 and counts["events"] == 2
    assert agg_store.get_event(satellite["uids"][a]) is None
    # Its incident is not brought either
    assert counts["incidents"] == 0


def test_imported_events_have_their_reception(agg_store, satellite):
    node, _ = agg_store.create_node("docker01")
    _run(satellite, node)
    conn = agg_store._conn()
    assert conn.execute("SELECT COUNT(*) FROM receptions").fetchone()[0] == 3
    a = satellite["ids"][0]
    ev = agg_store.get_event(satellite["uids"][a])
    assert ev["node_names"] == ["docker01"]
    assert agg_store.get_reception(satellite["uids"][a])["audio_path"] == ev["audio_path"]


def test_an_event_merged_into_another_broadcast_is_not_imported_again(agg_store, satellite):
    """The uplink delivered it as a reception of a broadcast another node
    started: its uid is a reception's, not the broadcast's."""
    node, _ = agg_store.create_node("docker01")
    other, _ = agg_store.create_node("Lisa")
    a = satellite["ids"][0]
    base = {"rev": 1, "type": "traffic", "state": "end", "pi": "0xE824",
            "station_ps": "P4 Värml", "long_ps": None, "frequency": "103.5M",
            "prog_type": None, "radiotext": [], "started_at": "2026-11-20T10:00:00Z",
            "ended_at": "2026-11-20T10:01:00Z", "duration_sec": 60,
            "sent_at": "2026-11-20T10:01:00Z"}
    agg_store.upsert_event(other["id"], "99999999-9999-4999-8999-999999999999", base)
    agg_store.upsert_event(node["id"], satellite["uids"][a], dict(base, started_at="2026-11-20T10:00:05Z"))
    counts = _run(satellite, node)
    assert counts["skipped"] == 1 and counts["events"] == 2

def test_deleting_the_node_takes_its_map_along(agg_store, satellite):
    """Delete the node and import again: one copy of the map, not two."""
    node, _ = agg_store.create_node("docker01")
    _run(satellite, node)
    agg_store.delete_node(node["id"])
    conn = agg_store._conn()
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM area_warnings").fetchone()[0] == 0
    node, _ = agg_store.create_node("docker01")
    counts = _run(satellite, node)
    assert counts["events"] == 3 and counts["incidents"] == 1
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM area_warnings").fetchone()[0] == 1


def test_messages_already_on_the_map_are_not_added_again(agg_store, satellite):
    """Events gone but their map left behind (as before v0.10.3): a new
    import does not add the incidents a second time."""
    node, _ = agg_store.create_node("docker01")
    _run(satellite, node)
    conn = agg_store._conn()
    conn.execute("DELETE FROM events")
    conn.commit()
    counts = _run(satellite, node)
    assert counts["events"] == 3
    assert counts["incidents"] == 0 and counts["areas"] == 0
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM area_warnings").fetchone()[0] == 1


def test_deleting_a_node_keeps_the_other_nodes_map(agg_store, satellite):
    node, _ = agg_store.create_node("docker01")
    _run(satellite, node)
    other, _ = agg_store.create_node("annan")
    agg_store.delete_node(other["id"])
    assert agg_store._conn().execute(
        "SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
