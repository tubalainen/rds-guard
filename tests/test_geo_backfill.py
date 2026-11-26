"""Placing the messages stored before the map existed (satellite)."""

import gzip
import time

import pytest

import config
import event_store
import geo_service
import geolocate
import rds_guard

TEXT = ("E18 i Karlstad i höjd med Bergviksmotet i riktning mot Grums. "
        "Olycka med lastbil.")


@pytest.fixture
def geo(tmp_path, monkeypatch):
    path = tmp_path / "gaz.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("Karlstad\tcity\t59.38091\t13.50276\tS\t\n"
                "Bergviksmotet\tjunction\t59.379\t13.43247\tS\t\n"
                "Grums\ttown\t59.35189\t13.10568\tS\t\n")
    svc = geo_service.GeoService(geolocate.create_geolocator("rules"),
                                 gazetteer_path=str(path))
    monkeypatch.setattr(rds_guard, "geo_service", svc)
    monkeypatch.setattr(rds_guard, "_node_place", svc.locate_node("Karlstad, Värmlands län"))
    monkeypatch.setattr(rds_guard, "_backfill", None)
    monkeypatch.setattr(config, "NODE_LOCATION", "Karlstad, Värmlands län")
    rds_guard._geo_ready.set()
    monkeypatch.setattr(geo_service.Backfill, "PAUSE_SEC", 0)
    return svc


def old_event(store, started, text=TEXT, geo_status=None):
    eid = store.insert_event("traffic", "info", "end", "0xE824", {},
                             started_at=started, ended_at=started)
    store.update_event_transcription(eid, text, status="done")
    if geo_status:
        store._conn().execute("UPDATE events SET geo_status = ? WHERE id = ?",
                              (geo_status, eid))
        store._conn().commit()
    return eid


def wait(bf):
    for _ in range(200):
        if not bf.status()["running"]:
            return
        time.sleep(0.01)
    raise AssertionError("backfill did not finish")


def test_older_messages_are_placed_once_at_their_own_time(store, geo):
    a = old_event(store, "2026-11-20T08:00:00")
    b = old_event(store, "2026-11-20T09:00:00", geo_status="no_location")
    old_event(store, "2026-11-20T09:30:00", text="Inga platser här alls.")
    placed = old_event(store, "2026-11-20T10:00:00", geo_status="done")
    assert event_store.geo_pending_count() == 3
    st = rds_guard.backfill_status()
    assert st["available"] and st["runnable"] and st["pending"] == 3

    assert rds_guard.start_backfill()
    wait(rds_guard._get_backfill())
    st = rds_guard.backfill_status()
    assert st == {**st, "total": 3, "done": 3, "placed": 2, "skipped": 0,
                  "pending": 0, "runnable": False, "finished": True}

    [inc] = event_store.list_incidents(None)
    assert inc["label"] == "E18 Bergviksmotet"
    assert inc["first_seen"] == "2026-11-20T08:00:00"
    assert inc["last_seen"] == "2026-11-20T09:00:00"
    assert sorted(inc["events"]) == sorted([str(a), str(b)])
    assert str(placed) not in inc["events"]
    # Older than 12 hours: not on the normal map
    assert event_store.list_incidents(12) == []


def test_without_a_receiver_location_nothing_runs(store, geo, monkeypatch):
    old_event(store, "2026-11-20T08:00:00")
    monkeypatch.setattr(rds_guard, "_node_place", None)
    st = rds_guard.backfill_status()
    assert not st["available"] and st["reason"] == "no_location" and not st["runnable"]
    assert not rds_guard.start_backfill()
    assert event_store.geo_pending_count() == 1


def test_without_processing_nothing_runs(store, monkeypatch):
    monkeypatch.setattr(rds_guard, "geo_service", None)
    monkeypatch.setattr(rds_guard, "_backfill", None)
    monkeypatch.setattr(config, "LOCAL_PROCESSING", False)
    assert rds_guard.backfill_status()["reason"] == "off"
    assert not rds_guard.start_backfill()


async def test_history_api(aiohttp_client, store, geo):
    import web_server
    old_event(store, "2026-11-20T08:00:00")
    client = await aiohttp_client(web_server.create_app())
    data = await (await client.get("/api/incidents?history=1")).json()
    assert data["hours"] is None and data["backfill"]["runnable"]
    # Writes must be JSON (same-origin guard)
    assert (await client.post("/api/incidents/backfill", data="x")).status == 415
    resp = await client.post("/api/incidents/backfill", json={})
    assert resp.status == 202 and (await resp.json())["started"]
    wait(rds_guard._get_backfill())
    data = await (await client.get("/api/incidents?history=1")).json()
    assert [i["label"] for i in data["incidents"]] == ["E18 Bergviksmotet"]
    assert data["backfill"]["placed"] == 1 and not data["backfill"]["runnable"]
    # The normal map keeps to the last 12 hours
    data = await (await client.get("/api/incidents")).json()
    assert data["incidents"] == [] and "backfill" not in data
