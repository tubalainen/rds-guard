"""Satellite web API: search, calendar, push rules and dispatch."""

import pytest

import config
import event_store
import notifier
import rds_guard
import web_server


@pytest.fixture
async def web(aiohttp_client, store):
    notifier.init()
    return await aiohttp_client(web_server.create_app())


def add_event(store, rt, started, typ="traffic", transcription=None, pi="E203"):
    eid = store.insert_event(typ, "warning", "start", pi, {}, station_ps="P4 Sthlm",
                             frequency="103.3M", radiotext=rt, started_at=started)
    store.end_event(eid, started, 60, radiotext_list=rt)
    if transcription:
        store.update_event_transcription(eid, transcription)
    return eid


async def test_default_rules_are_seeded_once(web, store):
    resp = await web.get("/api/notify")
    data = await resp.json()
    assert [r["name"] for r in data["rules"]] == ["VMA", "Trafik"]
    assert data["mqtt"]["enabled"] is False
    # a deleted default does not come back on the next start
    store.delete_rule(data["rules"][1]["id"])
    notifier.init()
    assert [r["name"] for r in store.list_rules()] == ["VMA"]


async def test_search_is_case_insensitive_for_swedish_letters(web, store):
    add_event(store, ["Olycka vid Häggvik"], "2026-11-24T06:48:00")
    add_event(store, ["Halka på väg 73"], "2026-11-24T14:40:00",
              transcription="Ishalka söder om Handen")
    add_event(store, ["Vägarbete Essingeleden"], "2026-11-20T10:00:00")

    async def search(**params):
        resp = await web.get("/api/events", params=params)
        return [e["radiotext"][0] for e in (await resp.json())["events"]]

    assert await search(q="HÄGGVIK") == ["Olycka vid Häggvik"]
    assert await search(q="ishalka") == ["Halka på väg 73"]
    assert await search(q="väg halka") == ["Halka på väg 73"]
    assert await search(q="100%") == []
    assert len(await search(**{"from": "2026-11-24T00:00:00",
                               "to": "2026-11-25T00:00:00"})) == 2


async def test_event_detail_and_calendar(web, store):
    eid = add_event(store, ["Kö på E4"], "2026-11-24T06:48:00")
    resp = await web.get(f"/api/events/{eid}")
    ev = await resp.json()
    assert ev["radiotext"] == ["Kö på E4"] and ev["notify"] == []
    assert (await web.get("/api/events/999")).status == 404
    resp = await web.get("/api/calendar")
    data = await resp.json()
    assert [e["id"] for e in data["events"]] == [eid]
    assert data["events"][0]["pushed"] is False


async def test_rule_crud_and_validation(web):
    rule = {"name": "Pendling", "event_types": ["traffic"],
            "keywords": ["E4"], "days": [0, 1, 2, 3, 4]}
    resp = await web.post("/api/notify/rules", json=rule)
    assert resp.status == 201
    saved = await resp.json()
    assert saved["topic"] == "pendling"

    resp = await web.put(f"/api/notify/rules/{saved['id']}",
                         json=dict(rule, enabled=False))
    assert (await resp.json())["enabled"] is False

    resp = await web.post("/api/notify/rules", json={"name": "", "event_types": []})
    assert resp.status == 422
    assert (await web.put("/api/notify/rules/999", json=rule)).status == 404
    assert (await web.delete(f"/api/notify/rules/{saved['id']}",
                             json={})).status == 200


async def test_writes_need_json_and_same_origin(web):
    rule = {"name": "x", "event_types": ["traffic"]}
    resp = await web.post("/api/notify/rules", data="name=x")
    assert resp.status == 415
    resp = await web.post("/api/notify/rules", json=rule,
                          headers={"Origin": "https://evil.example"})
    assert resp.status == 403


async def test_preview_runs_rule_on_history(web, store):
    add_event(store, ["Kö Essingeleden"], "2026-11-24T16:12:00")
    add_event(store, ["Halka väg 73"], "2026-11-24T14:40:00")
    resp = await web.post("/api/notify/preview", json={
        "name": "Pendling", "event_types": ["traffic"],
        "keywords": ["essingeleden"], "enabled": False})
    data = await resp.json()
    assert data["total"] == 2 and data["matched"] == 1
    assert data["samples"][0]["radiotext"] == ["Kö Essingeleden"]


async def test_test_message_without_mqtt(web):
    resp = await web.post("/api/notify/test", json={
        "name": "Pendling", "event_types": ["traffic"]})
    data = await resp.json()
    assert data["status"] == "mqtt_off"
    assert data["topic"] == f"{config.MQTT_TOPIC_PREFIX}/notify/pendling"
    assert data["payload"]["test"] is True
    log = (await (await web.get("/api/notify/log")).json())["log"]
    assert log[0]["test"] is True


def test_dispatch_publishes_matching_rules(store):
    notifier.init()
    store.create_rule({"name": "Pendling", "enabled": True,
                       "event_types": ["traffic"], "stations": [], "nodes": [],
                       "keywords": ["E4"], "days": [], "windows": [],
                       "priority": "high", "topic": "pendling"})
    eid = add_event(store, ["Olycka på E4"], "2026-11-24T06:48:00")
    sent = []
    statuses = notifier.dispatch(eid, lambda t, p: sent.append((t, p)) or True)
    # the default 'Trafik' rule and 'Pendling' both match
    assert sorted(t for t, _ in sent) == ["notify/pendling", "notify/trafik"]
    assert statuses == ["sent", "sent"]
    assert {l["topic"] for l in store.notify_for_events([eid])[eid]} == {
        "notify/pendling", "notify/trafik"}

    other = add_event(store, ["VMA"], "2026-11-24T06:48:00", typ="emergency")
    for rule in store.list_rules():
        store.delete_rule(rule["id"])
    assert notifier.dispatch(other, lambda t, p: True) == []
    assert store.notify_for_events([other])[other][0]["status"] == "no_match"


def test_notify_publish_is_none_without_mqtt(monkeypatch):
    monkeypatch.setattr(config, "MQTT_ENABLED", False)
    assert rds_guard._notify_publish("notify/x", {}) is None
