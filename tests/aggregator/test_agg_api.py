"""Aggregator LAN web server: read API, node admin, write guard."""

import pytest

from aggregator import api, config

UID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
async def web(aiohttp_client, agg_store):
    return await aiohttp_client(api.create_web_app())


@pytest.fixture
def event(agg_store, tmp_path):
    node, _ = agg_store.create_node("Kalle")
    agg_store.upsert_event(node["id"], UID, {
        "rev": 1, "type": "traffic", "state": "end", "pi": "0x9E04",
        "station_ps": "P4 Test", "long_ps": None, "frequency": "103.3M",
        "prog_type": "Information", "radiotext": ["Kö"],
        "started_at": "2026-11-25T10:00:00Z", "ended_at": "2026-11-25T10:01:00Z",
        "duration_sec": 60, "sent_at": "2026-11-25T10:01:00Z"})
    audio = tmp_path / "audio"
    audio.mkdir(exist_ok=True)
    (audio / f"{UID}.ogg").write_bytes(b"OggS1234")
    agg_store.set_event_audio(node["id"], UID, f"{UID}.ogg", None)
    agg_store.set_transcription(UID, "Olycka", "done", 1.5)
    return node


JSON = {"Content-Type": "application/json"}


async def test_events_list_and_filters(web, event):
    data = await (await web.get("/api/v1/events")).json()
    assert data["total"] == 1
    ev = data["events"][0]
    assert ev["node_name"] == "Kalle"
    assert ev["transcription"] == "Olycka"
    assert ev["audio_url"] == f"/api/v1/audio/{UID}.ogg"
    assert "node_id" not in ev and "audio_path" not in ev
    for q, n in (("type=emergency", 0), ("pi=0x9E04", 1), ("node=Kalle", 1),
                 ("node=Other", 0), ("since=2026-11-26T00:00:00Z", 0),
                 ("active=0", 1), ("active=1", 0)):
        assert (await (await web.get(f"/api/v1/events?{q}")).json())["total"] == n, q
    assert (await (await web.get("/api/v1/events?limit=abc")).json())["total"] == 1


async def test_single_event_and_audio(web, event):
    r = await web.get(f"/api/v1/events/{UID}")
    assert (await r.json())["event_uid"] == UID
    assert (await web.get("/api/v1/events/nope")).status == 404
    r = await web.get(f"/api/v1/audio/{UID}.ogg")
    assert r.status == 200
    assert r.headers["Content-Type"] == "audio/ogg"
    assert await r.read() == b"OggS1234"
    assert (await web.get("/api/v1/audio/..%2Fx.ogg")).status == 404


async def test_stations_and_status(web, event):
    st = (await (await web.get("/api/v1/stations")).json())["stations"]
    assert st[0]["pi"] == "0x9E04" and st[0]["nodes"] == ["Kalle"]
    s = await (await web.get("/api/v1/status")).json()
    assert s["events"] == 1 and s["nodes"] == 1
    assert s["mqtt"]["enabled"] is False


async def test_node_admin_flow(web, agg_store, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "https://rds.example.com")
    r = await web.post("/api/v1/nodes", json={"name": "Lisa, Luleå"})
    assert r.status == 201
    res = await r.json()
    assert res["snippet"].startswith("UPLINK_URL=https://rds.example.com\n")
    assert agg_store.authenticate(res["token"])["name"] == "Lisa, Luleå"
    node_id = res["node"]["id"]

    nodes = (await (await web.get("/api/v1/nodes")).json())["nodes"]
    assert nodes[0]["name"] == "Lisa, Luleå"
    assert "token" not in nodes[0] and "token_hash" not in nodes[0]

    r = await web.post(f"/api/v1/nodes/{node_id}/rotate", json={})
    new_token = (await r.json())["token"]
    assert agg_store.authenticate(res["token"]) is None
    assert agg_store.authenticate(new_token) is not None

    r = await web.patch(f"/api/v1/nodes/{node_id}", json={"enabled": False})
    assert (await r.json())["node"]["enabled"] is False

    r = await web.delete(f"/api/v1/nodes/{node_id}", json={})
    assert r.status == 200
    assert agg_store.list_nodes() == []


async def test_node_admin_validation(web, agg_store):
    assert (await web.post("/api/v1/nodes", json={"name": ""})).status == 422
    assert (await web.post("/api/v1/nodes", json=[1])).status == 422
    await web.post("/api/v1/nodes", json={"name": "A"})
    assert (await web.post("/api/v1/nodes", json={"name": "A"})).status == 422
    assert (await web.patch("/api/v1/nodes/999", json={"enabled": True})).status == 404
    assert (await web.patch("/api/v1/nodes/1", json={"enabled": "no"})).status == 422


async def test_write_guard_blocks_forms_and_cross_origin(web, agg_store):
    # A plain HTML form cannot send JSON …
    r = await web.post("/api/v1/nodes", data={"name": "evil"})
    assert r.status == 415
    # … and a cross-site fetch with JSON is refused by origin
    r = await web.post("/api/v1/nodes", json={"name": "evil"},
                       headers={"Origin": "https://evil.example"})
    assert r.status == 403
    assert agg_store.list_nodes() == []
    # Same origin is fine
    host = f"{web.server.host}:{web.server.port}"
    r = await web.post("/api/v1/nodes", json={"name": "ok"},
                       headers={"Origin": f"http://{host}"})
    assert r.status == 201


async def test_ui_served_with_security_headers(web):
    r = await web.get("/")
    assert r.status == 200
    assert "RDS Guard Aggregator" in await r.text()
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    for path in ("/static/css/base.css", "/static/css/aggregator.css",
                 "/static/js/app.js", "/static/js/events.js", "/static/js/nodes.js"):
        assert (await web.get(path)).status == 200, path
