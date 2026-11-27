"""Aggregator map: node location, geolocation before the alert, incidents API."""

import gzip

import pytest

import geo_service
import geolocate
from aggregator import api, config
from aggregator.pipeline import Pipeline, alert_payload

UID = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def geo(tmp_path):
    path = tmp_path / "gaz.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("Karlstad\tcity\t59.38091\t13.50276\tS\t\n"
                "Bergviksmotet\tjunction\t59.379\t13.43247\tS\t\n"
                "Grums\ttown\t59.35189\t13.10568\tS\t\n")
    return geo_service.GeoService(geolocate.create_geolocator("rules"),
                                  gazetteer_path=str(path))


@pytest.fixture
def event(agg_store):
    node, _ = agg_store.create_node("Kalle")
    agg_store.record_heartbeat(node["id"], "Kalle", "1.0",
                               {"location": "Karlstad, Värmlands län"}, 0)
    agg_store.upsert_event(node["id"], UID, {
        "rev": 1, "type": "traffic", "state": "end", "pi": "0xE824",
        "station_ps": "P4 Värml", "long_ps": None, "frequency": "103.5M",
        "prog_type": None, "radiotext": ["Trafikmeddelande"],
        "started_at": "2026-11-25T10:00:00Z", "ended_at": "2026-11-25T10:01:00Z",
        "duration_sec": 60, "sent_at": "2026-11-25T10:01:00Z"})
    return node


TEXT = ("E18 i Karlstad i höjd med Bergviksmotet i riktning mot Grums. "
        "Olycka med lastbil.")


def test_message_is_placed_before_it_is_marked_done(agg_store, event, geo):
    pipe = Pipeline(transcriber=None, mqtt=None, geo=geo)
    row = agg_store.get_event(UID)
    pipe._on_transcribed(row["id"], TEXT, None, 2.0)
    row = agg_store.get_event(UID)
    assert row["transcription_status"] == "done" and row["geo_status"] == "done"
    [inc] = agg_store.incidents_for_events([UID])[UID]
    assert inc["label"] == "E18 Bergviksmotet" and inc["radius_m"] == 500
    payload = alert_payload(row, "done", [inc])
    assert payload["locations"][0]["label"] == "E18 Bergviksmotet"


def test_unknown_node_location_is_recorded(agg_store, event, geo):
    agg_store.record_heartbeat(event["id"], "Kalle", "1.0", {"location": None}, 0)
    pipe = Pipeline(transcriber=None, mqtt=None, geo=geo)
    row = agg_store.get_event(UID)
    pipe._on_transcribed(row["id"], TEXT, None, 2.0)
    assert agg_store.get_event(UID)["geo_status"] == "no_location"


def test_hints_follow_the_node(agg_store, event, geo):
    pipe = Pipeline(transcriber=None, mqtt=None, geo=geo)
    hints = pipe._hints(agg_store.get_event(UID))
    assert "Bergviksmotet" in hints["keyterms"]
    assert hints["prompt"].startswith("Trafikmeddelande.")


async def test_incidents_api(aiohttp_client, agg_store, event, geo, monkeypatch):
    pipe = Pipeline(transcriber=None, mqtt=None, geo=geo)
    pipe._on_transcribed(agg_store.get_event(UID)["id"], TEXT, None, 2.0)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "hemligt")
    web = await aiohttp_client(api.create_web_app(pipe))
    # The map is public; the node list needs the admin login
    data = await (await web.get("/api/v1/incidents?hours=100000")).json()
    [inc] = data["incidents"]
    assert inc["nodes"] == ["Kalle"] and inc["latest"]["id"] == UID
    assert data["nodes"] == [{"name": "Kalle", "lat": 59.38091, "lon": 13.50276}]
    ev = await (await web.get(f"/api/v1/events/{UID}")).json()
    assert ev["incidents"][0]["label"] == "E18 Bergviksmotet"
    assert (await web.get("/api/v1/nodes")).status == 401
    await web.post("/api/v1/admin/login", json={"password": "hemligt"})
    node = (await (await web.get("/api/v1/nodes")).json())["nodes"][0]
    assert node["location"] == "Karlstad, Värmlands län"
    assert node["located"]["name"] == "Karlstad"


def _wait(pipe):
    import time
    for _ in range(200):
        if not pipe.backfill_status()["running"]:
            return
        time.sleep(0.01)
    raise AssertionError("backfill did not finish")


async def test_older_messages_are_placed_for_the_history(
        aiohttp_client, agg_store, event, geo, monkeypatch):
    """Messages stored before the map existed: placed once, as at the time
    they were broadcast, and only shown with ?history=1."""
    monkeypatch.setattr(geo_service.Backfill, "PAUSE_SEC", 0)
    agg_store.set_transcription(UID, TEXT, "done")      # no geo_status yet
    pipe = Pipeline(transcriber=None, mqtt=None, geo=geo)
    web = await aiohttp_client(api.create_web_app(pipe))

    data = await (await web.get("/api/v1/incidents?history=1")).json()
    assert data["hours"] is None and data["incidents"] == []
    assert data["backfill"]["pending"] == 1 and data["backfill"]["runnable"]
    resp = await web.post("/api/v1/incidents/backfill", json={})
    assert resp.status == 202
    _wait(pipe)

    data = await (await web.get("/api/v1/incidents?history=1")).json()
    [inc] = data["incidents"]
    assert inc["label"] == "E18 Bergviksmotet" and inc["nodes"] == ["Kalle"]
    assert inc["first_seen"] == "2026-11-25T10:00:00Z"
    assert data["backfill"]["placed"] == 1 and not data["backfill"]["runnable"]
    assert agg_store.get_event(UID)["geo_status"] == "done"
    assert (await (await web.get("/api/v1/incidents")).json())["incidents"] == []


def test_messages_of_a_node_without_location_wait(agg_store, event, geo, monkeypatch):
    monkeypatch.setattr(geo_service.Backfill, "PAUSE_SEC", 0)
    agg_store.record_heartbeat(event["id"], "Kalle", "1.0", {"location": None}, 0)
    agg_store.set_transcription(UID, TEXT, "done")
    pipe = Pipeline(transcriber=None, mqtt=None, geo=geo)
    assert pipe.start_backfill()
    _wait(pipe)
    st = pipe.backfill_status()
    assert st["skipped"] == 1 and st["pending"] == 0 and not st["runnable"]
    assert agg_store.get_event(UID)["geo_status"] is None
    # Once the node reports where it is, its messages can be placed
    agg_store.record_heartbeat(event["id"], "Kalle", "1.0",
                               {"location": "Karlstad, Värmlands län"}, 0)
    assert pipe.backfill_status()["runnable"]
    assert pipe.start_backfill()
    _wait(pipe)
    assert agg_store.get_event(UID)["geo_status"] == "done"


async def test_area_warnings_reach_the_map(aiohttp_client, agg_store, event, geo):
    place = geo.locate_node("Karlstad, Värmlands län")
    result = geo.locate("Det är mycket halt på många vägar i länet.", place.point)
    agg_store.save_geolocation(UID, result, "done")
    web = await aiohttp_client(api.create_web_app(Pipeline(transcriber=None, mqtt=None, geo=geo)))
    [area] = (await (await web.get("/api/v1/incidents")).json())["areas"]
    assert area["events"] == [UID] and "halt" in area["evidence"]


async def test_rebuild_places_everything_again(aiohttp_client, agg_store, event,
                                              geo, monkeypatch):
    monkeypatch.setattr(geo_service.Backfill, "PAUSE_SEC", 0)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "hemligt")
    pipe = Pipeline(transcriber=None, mqtt=None, geo=geo)
    pipe._on_transcribed(agg_store.get_event(UID)["id"], TEXT, None, 2.0)
    [before] = agg_store.incidents_for_events([UID])[UID]

    web = await aiohttp_client(api.create_web_app(pipe))
    assert (await web.get("/api/v1/incidents/rebuild")).status == 401
    assert (await web.post("/api/v1/admin/login",
                           json={"password": "hemligt"})).status == 200
    info = await (await web.get("/api/v1/incidents/rebuild")).json()
    assert (info["messages"], info["engine"], info["cost_ore"]) == (1, "rules", 0)
    resp = await web.post("/api/v1/incidents/rebuild", json={})
    assert resp.status == 202
    _wait(pipe)
    [after] = agg_store.incidents_for_events([UID])[UID]
    assert after["id"] != before["id"] and after["label"] == "E18 Bergviksmotet"
    assert agg_store.get_event(UID)["geo_status"] == "done"


def test_location_set_in_admin_overrides_the_reported_one(agg_store, event, geo):
    pipe = Pipeline(transcriber=None, mqtt=None, geo=geo)
    agg_store.record_heartbeat(event["id"], "Kalle", "1.0", {"location": None}, 0)
    assert pipe.node_place(event["id"]) is None
    assert agg_store.set_node_location(event["id"], "Grums, Värmlands län")
    assert pipe.node_place(event["id"]).name == "Grums"
    # The reported location no longer wins once the owner set one
    agg_store.record_heartbeat(event["id"], "Kalle", "1.0",
                               {"location": "Karlstad, Värmlands län"}, 0)
    assert pipe.node_place(event["id"]).name == "Grums"
    # Cleared: back to what the node reports
    agg_store.set_node_location(event["id"], None)
    assert pipe.node_place(event["id"]).name == "Karlstad"


async def test_location_can_be_edited_in_admin(aiohttp_client, agg_store, event,
                                               geo, monkeypatch):
    pipe = Pipeline(transcriber=None, mqtt=None, geo=geo)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "hemligt")
    web = await aiohttp_client(api.create_web_app(pipe))
    r = await web.patch(f"/api/v1/nodes/{event['id']}", json={"location": "Grums"})
    assert r.status == 401
    await web.post("/api/v1/admin/login", json={"password": "hemligt"})
    r = await web.patch(f"/api/v1/nodes/{event['id']}",
                        json={"location": "  Grums,   Värmlands län "})
    node = (await r.json())["node"]
    assert node["location"] == node["location_set"] == "Grums, Värmlands län"
    assert node["location_reported"] == "Karlstad, Värmlands län"
    assert node["located"]["name"] == "Grums"
    assert node["enabled"] is True
    for bad in ({"location": 5}, {"location": "x" * 81}, {}):
        r = await web.patch(f"/api/v1/nodes/{event['id']}", json=bad)
        assert r.status == 422, bad
    r = await web.patch(f"/api/v1/nodes/{event['id']}", json={"location": None})
    assert (await r.json())["node"]["location"] == "Karlstad, Värmlands län"
    # Messages that waited for a location are placed after it was set
    agg_store.record_heartbeat(event["id"], "Kalle", "1.0", {"location": None}, 0)
    row = agg_store.get_event(UID)
    pipe._on_transcribed(row["id"], TEXT, None, 2.0)
    assert agg_store.get_event(UID)["geo_status"] == "no_location"
    await web.patch(f"/api/v1/nodes/{event['id']}", json={"location": "Karlstad, Värmlands län"})
    assert pipe._placeable_count() == 1


def test_place_falls_back_to_another_node_that_heard_it(agg_store, event, geo):
    """The transcribed node has no place; another that heard it has one."""
    agg_store.record_heartbeat(event["id"], "Kalle", "1.0", {"location": None}, 0)
    lisa, _ = agg_store.create_node("Lisa")
    agg_store.record_heartbeat(lisa["id"], "Lisa", "1.0",
                               {"location": "Grums, Värmlands län"}, 0)
    agg_store.upsert_event(lisa["id"], "44444444-4444-4444-8444-444444444444", {
        "rev": 1, "type": "traffic", "state": "end", "pi": "0xE824",
        "station_ps": "P4 Värml", "long_ps": None, "frequency": "103.5M",
        "prog_type": None, "radiotext": [], "started_at": "2026-11-25T10:00:03Z",
        "ended_at": "2026-11-25T10:01:00Z", "duration_sec": 57,
        "sent_at": "2026-11-25T10:01:00Z"})
    pipe = Pipeline(transcriber=None, mqtt=None, geo=geo)
    row = agg_store.get_event(UID)
    assert row["node_name"] == "Kalle" and row["node_names"] == ["Kalle", "Lisa"]
    place, location = pipe.place_of(row)
    assert place.name == "Grums" and location == "Grums, Värmlands län"
    pipe._on_transcribed(row["id"], TEXT, None, 2.0)
    assert agg_store.get_event(UID)["geo_status"] != "no_location"
