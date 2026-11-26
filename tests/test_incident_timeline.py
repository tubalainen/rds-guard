"""Every broadcast about an incident, with its kind, words and audio (v0.7)."""

import pytest

import geolocate as geo
import web_server


def pl(kind, evidence, **kw):
    return geo.Placement(lat=59.379, lon=13.43247, radius_m=500, road="E18",
                         label="E18 Bergviksmotet", what=kw.pop("what", "Krock"),
                         kind=kind, evidence=evidence, **kw)


def add(store, at, audio=None):
    eid = store.insert_event("traffic", "warning", "end", "E824", {},
                             station_ps="P4 Värml", started_at=at)
    if audio:
        conn = store._conn()
        conn.execute("UPDATE events SET audio_path = ? WHERE id = ?", (audio, eid))
        conn.commit()
    return eid


def place(store, eid, at, *placements):
    store.save_geolocation(eid, geo.GeoResult("claude", list(placements)), "done", at=at)


def test_timeline_is_oldest_first_with_kind_and_words(store):
    a = add(store, "2026-11-26T10:00:00")
    b = add(store, "2026-11-26T10:30:00")
    place(store, a, "2026-11-26T10:00:00", pl("new", "Krock vid Bergviksmotet."))
    place(store, b, "2026-11-26T10:30:00", pl("repeat", "Fortfarande köer."))
    [inc] = store.list_incidents(None)
    assert [(t["event_key"], t["kind"], t["evidence"]) for t in inc["timeline"]] == [
        (str(a), "new", "Krock vid Bergviksmotet."),
        (str(b), "repeat", "Fortfarande köer.")]
    assert inc["events"] == [str(b), str(a)]           # newest first, as before
    assert store.get_incident(inc["id"])["timeline"] == inc["timeline"]
    assert store.get_incident(inc["id"] + 1) is None


def test_incidents_stored_before_kinds_have_an_empty_timeline_entry(store):
    a = add(store, "2026-11-26T10:00:00")
    place(store, a, "2026-11-26T10:00:00", pl("new", ""))
    conn = store._conn()
    conn.execute("UPDATE incident_events SET kind = NULL, evidence = NULL")
    conn.commit()
    [t] = store.list_incidents(None)[0]["timeline"]
    assert t["kind"] is None and t["evidence"] == ""


@pytest.fixture
async def client(aiohttp_client, store):
    return await aiohttp_client(web_server.create_app())


async def test_satellite_api_gives_each_broadcast_its_time_and_audio(client, store):
    a = add(store, "2026-11-26T10:00:00Z", audio="a.ogg")
    b = add(store, "2026-11-26T10:30:00Z")
    place(store, a, "2026-11-26T10:00:00", pl("new", "Krock vid Bergviksmotet."))
    place(store, b, "2026-11-26T10:30:00", pl("cleared", "Öppet igen.", cleared=True))
    inc_id = store.list_incidents(None)[0]["id"]
    one = await (await client.get(f"/api/incidents/{inc_id}")).json()
    first, last = one["timeline"]
    assert (first["event_id"], first["started_at"], first["audio_url"]) == (
        a, "2026-11-26T10:00:00Z", "/api/audio/a.ogg")
    assert (last["event_id"], last["kind"], last["audio_url"]) == (b, "cleared", None)
    assert one["latest"]["id"] == b and one["status"] == "cleared"
    assert (await client.get(f"/api/incidents/{inc_id + 5}")).status == 404
