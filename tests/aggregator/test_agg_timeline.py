"""Aggregator: every broadcast about an incident, with node and audio (v0.7)."""

import gzip

import pytest

import geo_service
import geolocate as geo
from aggregator import api, store as agg
from aggregator.pipeline import Pipeline


def pl(kind, evidence):
    return geo.Placement(lat=59.379, lon=13.43247, radius_m=500, road="E18",
                         label="E18 Bergviksmotet", what="Krock",
                         kind=kind, evidence=evidence)


UID_A = "44444444-4444-4444-8444-444444444444"
UID_B = "55555555-5555-4555-8555-555555555555"


@pytest.fixture
def node(agg_store):
    n, _ = agg_store.create_node("Kalle")
    for uid, at in ((UID_A, "2026-11-25T10:00:00Z"), (UID_B, "2026-11-25T10:30:00Z")):
        agg_store.upsert_event(n["id"], uid, {
            "rev": 1, "type": "traffic", "state": "end", "pi": "0xE824",
            "station_ps": "P4 Värml", "long_ps": None, "frequency": "103.5M",
            "prog_type": None, "radiotext": [], "started_at": at,
            "ended_at": at, "duration_sec": 60, "sent_at": at})
    conn = agg_store._conn()
    conn.execute("UPDATE events SET audio_path = 'x.ogg' WHERE event_uid = ?", (UID_A,))
    conn.commit()
    return n


@pytest.fixture
def geo_svc(tmp_path):
    path = tmp_path / "gaz.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("Karlstad\tcity\t59.38091\t13.50276\tS\t\n")
    return geo_service.GeoService(geo.create_geolocator("rules"),
                                  gazetteer_path=str(path))


async def test_aggregator_api_gives_each_broadcast_its_node_and_audio(
        aiohttp_client, agg_store, node, geo_svc):
    agg_store.save_geolocation(UID_A, geo.GeoResult("claude", [pl("new", "Krock.")]),
                               "done", at="2026-11-25T10:00:00Z")
    agg_store.save_geolocation(UID_B, geo.GeoResult("claude", [pl("update", "Avstängt.")]),
                               "done", at="2026-11-25T10:30:00Z")
    web = await aiohttp_client(api.create_web_app(
        Pipeline(transcriber=None, mqtt=None, geo=geo_svc)))
    [inc] = (await (await web.get("/api/v1/incidents?history=1")).json())["incidents"]
    assert [(t["event_id"], t["kind"], t["node_name"]) for t in inc["timeline"]] == [
        (UID_A, "new", "Kalle"), (UID_B, "update", "Kalle")]
    assert inc["timeline"][0]["audio_url"] == f"/api/v1/audio/{UID_A}.ogg"
    assert inc["timeline"][1]["audio_url"] is None
    one = await (await web.get(f"/api/v1/incidents/{inc['id']}")).json()
    assert one["timeline"] == inc["timeline"] and one["nodes"] == ["Kalle"]
    assert (await web.get("/api/v1/incidents/999")).status == 404
    assert agg.get_incident(999) is None
