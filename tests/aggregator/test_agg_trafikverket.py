"""Trafikverket's deviations on the aggregator: storage, API, linking to
the radio's incidents and push messages for updates."""

import gzip
import time

import pytest

import geo_service
import geolocate
import trafikverket
from aggregator import api, notifier
from aggregator.pipeline import Pipeline, alert_payload


def situation():
    return {"Id": "S1", "Deviation": [{
        "Id": "D1", "MessageType": "Olycka", "RoadNumber": "E18",
        "StartTime": "2026-11-26T13:02:00.000+01:00",
        "Geometry": {"Point": {"WGS84": "POINT (13.43247 59.379 0)"}}}]}


async def test_aggregator_api_and_status(aiohttp_client, agg_store):
    agg_store.trv.apply([situation()])
    pipe = Pipeline(transcriber=None, mqtt=None, geo=None)
    client = await aiohttp_client(api.create_web_app(pipe))
    data = await (await client.get("/api/v1/trafikverket")).json()
    assert data["deviations"][0]["id"] == "D1"
    assert data["deviations"][0]["start_time"] == "2026-11-26T12:02:00Z"
    status = await (await client.get("/api/v1/status")).json()
    assert status["trafikverket"]["enabled"] is False


# ---------------------------------------------------------------- linking

UID = "44444444-4444-4444-8444-444444444444"


def _now(offset_min=0):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                         time.gmtime(time.time() + offset_min * 60))


def accident(message="Olycka med lastbil.", deleted=False):
    s = {"Id": "S2", "Deviation": [{
        "Id": "A1", "MessageType": "Olycka", "MessageCode": "Olycka",
        "Message": message, "RoadNumber": "E18", "SeverityCode": 4,
        "SeverityText": "Stor påverkan", "CreationTime": _now(-5),
        "StartTime": _now(-6), "EndTime": _now(120),
        "Geometry": {"Point": {"WGS84": "POINT (13.4352 59.3791 0)"}}}]}
    if deleted:
        s["Deleted"] = True
    return s


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
def node_event(agg_store):
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


def test_message_is_linked_and_updates_are_pushed(agg_store, node_event, geo):
    agg_store.trv.apply([accident()])
    pipe = Pipeline(transcriber=None, mqtt=None, geo=geo)
    pipe._on_transcribed(agg_store.get_event(UID)["id"], TEXT, None, 2.0)
    [inc] = agg_store.incidents_for_events([UID])[UID]
    assert inc["trv_id"] == "A1" and inc["radius_m"] == 300
    # The alert carries Trafikverket's deviation
    trafikverket.attach(agg_store.trv, [inc], versions=False)
    payload = alert_payload(agg_store.get_event(UID), "done", [inc])
    assert payload["trafikverket"][0]["id"] == "A1"
    assert "line" not in payload["trafikverket"][0]

    # The message was pushed by the rule "Trafik": the update goes there too
    notifier.init()
    rule = next(r for r in agg_store.list_rules() if r["name"] == "Trafik")
    agg_store.log_notification(UID, agg_store.NOTIFY_SENT, rule=rule,
                               topic="notify/trafik", title="Trafik: …")
    sent = []
    pipe.notify_publish = lambda t, p: sent.append((t, p)) or True
    before = agg_store.trv.last_version_id()
    ids = agg_store.trv.apply([accident("Bärgning pågår.")])
    pipe.trv_changed(ids, before)
    assert sent and sent[0][0] == f"notify/{rule['topic']}"
    assert sent[0][1]["message"] == "Bärgning pågår."
    assert sent[0][1]["event_id"] == UID


async def test_incidents_api_and_layer(aiohttp_client, agg_store, node_event, geo):
    agg_store.trv.apply([accident()])
    pipe = Pipeline(transcriber=None, mqtt=None, geo=geo)
    pipe._on_transcribed(agg_store.get_event(UID)["id"], TEXT, None, 2.0)
    client = await aiohttp_client(api.create_web_app(pipe))
    data = await (await client.get("/api/v1/incidents")).json()
    assert data["incidents"][0]["trafikverket"]["id"] == "A1"
    layer = await (await client.get("/api/v1/trafikverket?layer=1")).json()
    assert layer["deviations"] == []        # linked: shown as the incident
    ev = await (await client.get(f"/api/v1/events/{UID}")).json()
    assert ev["incidents"][0]["trafikverket"]["id"] == "A1"
