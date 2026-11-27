"""Aggregator ingest API (protocol v1)."""

import uuid

import pytest

from aggregator import config, ingest

H = ingest.PROTOCOL_HEADER


class RecordingHooks(ingest.Hooks):
    def __init__(self):
        self.calls = []

    def on_event(self, row, prev_state):
        self.calls.append(("event", row["event_uid"], prev_state))

    def on_audio(self, row):
        self.calls.append(("audio", row["event_uid"]))

    def on_delete(self, row):
        self.calls.append(("delete", row["event_uid"]))

    def on_heartbeat(self, node):
        self.calls.append(("heartbeat", node["name"]))


@pytest.fixture
def hooks():
    return RecordingHooks()


@pytest.fixture
async def client(aiohttp_client, agg_store, hooks):
    return await aiohttp_client(ingest.create_ingest_app(hooks))


@pytest.fixture
def node(agg_store):
    n, token = agg_store.create_node("Kalle")
    return {"node": n, "token": token,
            "headers": {"Authorization": f"Bearer {token}", H: "2"}}


def _event(rev=1, state="start", **kw):
    body = {"rev": rev, "type": "traffic", "state": state, "pi": "0x9E04",
            "station_ps": "P4 Test", "frequency": "103.3M",
            "prog_type": "Information", "radiotext": [],
            "started_at": "2026-11-25T10:00:00Z", "ended_at": None,
            "duration_sec": None, "sent_at": "2026-11-25T10:00:01Z"}
    body.update(kw)
    return body


def _url(uid, suffix=""):
    return f"/api/v1/ingest/events/{uid}{suffix}"


OGG = b"OggS" + b"\0" * 200


async def test_every_response_has_protocol_header(client, node):
    resp = await client.put(_url(uuid.uuid4()), json=_event())
    assert resp.status == 426
    assert resp.headers[H] == "2"
    resp = await client.get("/nope", headers=node["headers"])
    assert resp.status in (404, 405)
    assert resp.headers[H] == "2"


@pytest.mark.parametrize("method,path", [
    ("GET", "/"),
    ("GET", "/index.html"),
    ("GET", "/static/js/app.js"),
    ("GET", "/api/v1/status"),
    ("GET", "/api/v1/events"),
    ("GET", "/api/v1/nodes"),
    ("POST", "/api/v1/nodes"),
    ("DELETE", "/api/v1/nodes/1"),
    ("GET", "/api/v1/audio/x.ogg"),
])
async def test_ingest_port_serves_nothing_but_ingest(client, node, method, path):
    """This port is the one put on the internet, without a filter in front:
    the web UI, the read API and node admin must not exist on it."""
    r = await client.request(method, path, headers=node["headers"], json={})
    assert r.status == 404
    assert r.headers[H] == "2"
    # … and without a token the caller does not even learn that
    r = await client.request(method, path)
    assert r.status == 426


async def test_reachability_check_from_readme(client):
    """aggregator/README.md, "Check that it works": a plain curl gets a 426
    that is recognisably from the aggregator."""
    r = await client.get("/api/v1/ingest/heartbeat")
    assert r.status == 426
    assert r.headers[H] == "2"
    assert (await r.json())["error"] == "unsupported_protocol"


async def test_auth_required(client):
    resp = await client.put(_url(uuid.uuid4()), json=_event(), headers={H: "2"})
    assert resp.status == 401
    resp = await client.put(_url(uuid.uuid4()), json=_event(),
                            headers={H: "2", "Authorization": "Bearer wrong"})
    assert resp.status == 401


async def test_disabled_node_rejected(client, node, agg_store):
    agg_store.set_node_enabled(node["node"]["id"], False)
    resp = await client.put(_url(uuid.uuid4()), json=_event(),
                            headers=node["headers"])
    assert resp.status == 403


async def test_event_lifecycle_and_rev_order(client, node, hooks, agg_store):
    uid = str(uuid.uuid4())
    h = node["headers"]
    r = await client.put(_url(uid), json=_event(rev=1), headers=h)
    assert (r.status, (await r.json())["status"]) == (200, "stored")

    end = _event(rev=5, state="end", radiotext=["Olycka på E4"],
                 ended_at="2026-11-25T10:01:00Z", duration_sec=60)
    r = await client.put(_url(uid), json=end, headers=h)
    assert (await r.json())["status"] == "stored"

    # Replays and late arrivals of older snapshots change nothing
    for rev in (5, 3):
        r = await client.put(_url(uid), json=_event(rev=rev), headers=h)
        assert (await r.json())["status"] == "stale"

    row = agg_store.get_event(uid)
    assert row["state"] == "end"
    assert row["radiotext"] == ["Olycka på E4"]
    assert row["node_name"] == "Kalle"
    assert row["end_received_at"]
    assert hooks.calls == [("event", uid, None), ("event", uid, "start")]


async def test_other_node_cannot_touch_event(client, node, agg_store):
    uid = str(uuid.uuid4())
    await client.put(_url(uid), json=_event(), headers=node["headers"])
    _, token2 = agg_store.create_node("Eve")
    h2 = {"Authorization": f"Bearer {token2}", H: "2"}
    r = await client.put(_url(uid), json=_event(rev=9), headers=h2)
    assert r.status == 409
    r = await client.put(_url(uid, "/audio"), data=OGG, headers=h2)
    assert r.status == 404
    r = await client.delete(_url(uid), headers=h2)
    assert r.status == 404
    assert agg_store.get_event(uid)["rev"] == 1


@pytest.mark.parametrize("patch", [
    {"type": "weather"},
    {"state": "bogus"},
    {"pi": ""},
    {"pi": "x" * 17},
    {"rev": -1},
    {"rev": "1"},
    {"radiotext": ["x" * 129]},
    {"radiotext": ["a"] * 51},
    {"radiotext": "not a list"},
    {"started_at": "2026-11-25 10:00:00"},
    {"duration_sec": 100000},
    {"sent_at": None},
])
async def test_invalid_event_rejected(client, node, patch):
    r = await client.put(_url(uuid.uuid4()), json=_event(**patch),
                         headers=node["headers"])
    assert r.status == 422


async def test_bad_uid_and_bad_json(client, node):
    r = await client.put(_url("not-a-uuid"), json=_event(), headers=node["headers"])
    assert r.status == 422
    r = await client.put(_url(str(uuid.uuid4()).upper()), json=_event(),
                         headers=node["headers"])
    assert r.status == 422
    r = await client.put(_url(uuid.uuid4()), data=b"{nope",
                         headers=node["headers"])
    assert r.status == 422


async def test_oversized_json_rejected(client, node):
    body = _event()
    body["padding"] = "x" * (config.MAX_JSON_BYTES + 10)
    r = await client.put(_url(uuid.uuid4()), json=body, headers=node["headers"])
    assert r.status == 413
    assert r.headers[H] == "2"


async def test_audio_upload(client, node, hooks, agg_store, tmp_path):
    uid = str(uuid.uuid4())
    h = node["headers"]
    r = await client.put(_url(uid, "/audio"), data=OGG, headers=h)
    assert r.status == 404                      # event must exist first
    await client.put(_url(uid), json=_event(), headers=h)

    r = await client.put(_url(uid, "/audio"), data=b"RIFFxxxx", headers=h)
    assert r.status == 415
    r = await client.put(_url(uid, "/audio"), data=OGG, headers=h)
    assert r.status == 200

    row = agg_store.get_event(uid)
    assert row["audio_path"] == f"{uid}.ogg"
    assert row["transcription_status"] == "queued"
    assert (tmp_path / "audio" / f"{uid}.ogg").read_bytes() == OGG
    assert hooks.calls[-1] == ("audio", uid)


async def test_audio_too_large(client, node, monkeypatch):
    monkeypatch.setattr(config, "MAX_AUDIO_BYTES", 1000)
    uid = str(uuid.uuid4())
    await client.put(_url(uid), json=_event(), headers=node["headers"])
    r = await client.put(_url(uid, "/audio"), data=b"OggS" + b"\0" * 2000,
                         headers=node["headers"])
    assert r.status == 413


async def test_delete_removes_event_and_audio(client, node, agg_store, tmp_path):
    uid = str(uuid.uuid4())
    h = node["headers"]
    await client.put(_url(uid), json=_event(), headers=h)
    await client.put(_url(uid, "/audio"), data=OGG, headers=h)
    r = await client.delete(_url(uid), headers=h)
    assert r.status == 200
    assert agg_store.get_event(uid) is None
    assert not (tmp_path / "audio" / f"{uid}.ogg").exists()
    r = await client.delete(_url(uid), headers=h)
    assert r.status == 404


async def test_heartbeat_is_cleaned_and_stored(client, node, agg_store, hooks):
    body = {"node_name": "Kalle " + "x" * 200, "version": "1.0",
            "sent_at": "2026-11-25T10:00:00Z", "uptime_sec": 5,
            "pipeline": {"state": "running", "error": None, "evil": "x"},
            "stations": [{"pi": "0x9E04", "ps": "P4", "tp": True,
                          "ta": "yes", "groups_per_sec": 11.2,
                          "extra": "dropped"}] * 20,
            "queue_depth": 3, "unknown": "ignored"}
    r = await client.post("/api/v1/ingest/heartbeat", json=body,
                          headers=node["headers"])
    assert r.status == 200
    assert (await r.json())["server_time"].endswith("Z")
    n = agg_store.get_node(node["node"]["id"])
    assert len(n["reported_name"]) == 64
    assert n["version"] == "1.0"
    st = n["status"]
    assert len(st["stations"]) == 8
    assert st["stations"][0]["ta"] is None        # not a bool → dropped
    assert "extra" not in st["stations"][0]
    assert "evil" not in st["pipeline"]
    assert st["queue_depth"] == 3
    assert n["clock_offset_sec"] is not None
    assert hooks.calls[-1] == ("heartbeat", "Kalle")


async def test_rate_limit(aiohttp_client, agg_store, node):
    client = await aiohttp_client(ingest.create_ingest_app(
        limiter=ingest.RateLimiter(per_minute=1, burst=2)))
    statuses = []
    for _ in range(3):
        r = await client.post("/api/v1/ingest/heartbeat", json={},
                              headers=node["headers"])
        statuses.append(r.status)
    assert statuses == [200, 200, 429]


async def test_heartbeat_carries_the_node_location(client, node, agg_store):
    body = {"node_name": "Kalle", "version": "1.0", "sent_at": "2026-11-25T10:00:00Z",
            "location": "Karlstad, Värmlands län " + "x" * 100,
            "local_processing": "no"}
    r = await client.post("/api/v1/ingest/heartbeat", json=body,
                          headers=node["headers"])
    assert r.status == 200
    st = agg_store.get_node(node["node"]["id"])["status"]
    assert st["location"].startswith("Karlstad, Värmlands län")
    assert len(st["location"]) == 80
    assert st["local_processing"] is None          # not a bool → dropped
