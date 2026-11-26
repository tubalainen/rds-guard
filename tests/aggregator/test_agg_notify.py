"""Aggregator push rules: API, search, and dispatch from the pipeline."""

import time

import pytest

from aggregator import api, config, notifier, pipeline, store



class FakeMqtt:
    connected = True

    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, qos=1, retain=False):
        self.messages.append((topic, payload))
        return True

    def topics(self):
        return [m[0] for m in self.messages]


def _store_event(node, uid="11111111-1111-4111-8111-111111111111",
                 state="end", rev=1, **kw):
    ev = {"rev": rev, "type": "traffic", "state": state, "pi": "0x9E04",
          "station_ps": "P4 Test", "long_ps": None, "frequency": "103.3M",
          "prog_type": "Information", "radiotext": ["Kö på E18"],
          "started_at": "2026-11-25T10:00:00Z",
          "ended_at": "2026-11-25T10:01:00Z" if state == "end" else None,
          "duration_sec": 60 if state == "end" else None,
          "sent_at": "2026-11-25T10:01:01Z"}
    ev.update(kw)
    return store.upsert_event(node["id"], uid, ev)

UID2 = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def node(agg_store):
    notifier.init()
    n, _ = agg_store.create_node("Kalle")
    return n


@pytest.fixture
async def web(aiohttp_client, node, monkeypatch):
    """A client logged in as admin (the push rules need it)."""
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "hemligt")
    client = await aiohttp_client(api.create_web_app(mqtt=FakeMqtt()))
    resp = await client.post("/api/v1/admin/login", json={"password": "hemligt"})
    assert resp.status == 200
    return client


async def test_rules_api_and_test_message(web):
    data = await (await web.get("/api/v1/notify")).json()
    assert [r["name"] for r in data["rules"]] == ["VMA", "Trafik"]
    resp = await web.post("/api/v1/notify/rules", json={
        "name": "Halka", "event_types": ["traffic"], "keywords": ["halka"],
        "nodes": ["Kalle"]})
    assert resp.status == 201
    rule = await resp.json()
    resp = await web.post("/api/v1/notify/test", json=rule)
    data = await resp.json()
    assert data["status"] == "sent"
    assert data["topic"] == "rds-aggregator/notify/halka"
    assert (await web.post("/api/v1/notify/rules", json={"name": "x"})).status == 422
    assert (await web.delete(f"/api/v1/notify/rules/{rule['id']}", json={})).status == 200


async def test_search_preview_and_calendar(web, node):
    _store_event(node, radiotext=["Ishalka på E18"])
    _store_event(node, uid=UID2, radiotext=["Kö vid Älvsjö"],
                 started_at="2026-11-24T10:00:00Z")
    data = await (await web.get("/api/v1/events", params={"q": "älvsjö"})).json()
    assert [e["event_uid"] for e in data["events"]] == [UID2]
    assert data["events"][0]["notify"] == []
    data = await (await web.get("/api/v1/events", params={
        "from": "2026-11-25T00:00:00Z"})).json()
    assert data["total"] == 1
    resp = await web.post("/api/v1/notify/preview", json={
        "name": "Halka", "event_types": ["traffic"], "keywords": ["halka"]})
    res = await resp.json()
    assert res["matched"] == 1 and res["total"] == 2
    cal = await (await web.get("/api/v1/calendar")).json()
    assert len(cal["events"]) == 2 and cal["events"][0]["pushed"] is False


def test_traffic_is_pushed_with_its_alert(node):
    mqtt = FakeMqtt()
    p = pipeline.Pipeline(mqtt=mqtt)
    _store_event(node, ended_at=store.iso_ago(200), end_received_at=None)
    p.sweep(now=time.time() + 120)
    assert "alert" in mqtt.topics()
    assert "notify/trafik" in mqtt.topics()
    uid = "11111111-1111-4111-8111-111111111111"
    assert store.notify_for_events([uid])[uid][0]["status"] == "sent"


def test_emergency_is_pushed_at_start_only_once(node, monkeypatch):
    mqtt = FakeMqtt()
    p = pipeline.Pipeline(mqtt=mqtt)
    monkeypatch.setattr(notifier, "dispatch_async",
                        lambda row, pub: notifier.dispatch(row, pub))
    _, prev, row = _store_event(node, uid=UID2, type="emergency", state="active",
                                radiotext=[])
    p.on_event(row, prev)
    assert mqtt.topics().count("notify/vma") == 1
    _, prev, row = _store_event(node, uid=UID2, type="emergency", rev=2,
                                ended_at=store.iso_ago(200), duration_sec=60,
                                radiotext=[])
    p.on_event(row, prev)
    p.sweep(now=time.time() + 120)
    assert mqtt.topics().count("notify/vma") == 1
