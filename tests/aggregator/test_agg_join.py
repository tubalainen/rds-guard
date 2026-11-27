"""Joining (protocol 2): a satellite asks with its own token, the owner
approves it in Admin or with the CLI, or a shared JOIN_KEY approves it."""

import secrets
import uuid

import pytest

import uplink
from aggregator import api, config, ingest, nodes

H = ingest.PROTOCOL_HEADER
URL = "/api/v1/ingest/join"


@pytest.fixture
async def client(aiohttp_client, agg_store):
    return await aiohttp_client(ingest.create_ingest_app())


@pytest.fixture
async def web(aiohttp_client, agg_store, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "hemligt")
    c = await aiohttp_client(api.create_web_app())
    assert (await c.post("/api/v1/admin/login", json={"password": "hemligt"})).status == 200
    return c


class Sat:
    """A satellite's identity, as uplink.py makes it."""

    def __init__(self, name="Kalle, Karlstad", location="Karlstad, Värmlands län"):
        self.token = secrets.token_urlsafe(32)
        self.join_id = str(uuid.uuid4())
        self.name, self.location = name, location

    def headers(self):
        return {"Authorization": f"Bearer {self.token}", H: "2"}

    def body(self, **kw):
        return {"join_id": self.join_id, "node_name": self.name, "version": "0.11.0",
                "suggested_location": self.location,
                "stations": [{"frequency": "103.5M", "pi": "0xE824", "ps": "P4 Värml"}],
                **kw}


async def _join(client, sat, **kw):
    return await client.post(URL, json=sat.body(**kw), headers=sat.headers())


async def test_pending_then_approved_in_admin(client, web, agg_store):
    sat = Sat()
    r = await _join(client, sat)
    assert r.status == 202 and r.headers[H] == "2"
    data = await r.json()
    # The same code as the satellite shows in its log
    assert data == {"status": "pending", "verify": uplink.verify_code(sat.join_id)}
    # Not a node yet: its token opens nothing
    hb = await client.post("/api/v1/ingest/heartbeat", json={}, headers=sat.headers())
    assert hb.status == 401

    pending = (await (await web.get("/api/v1/nodes/pending")).json())["pending"]
    assert [p["verify"] for p in pending] == [data["verify"]]
    assert pending[0]["suggested_location"] == "Karlstad, Värmlands län"
    assert pending[0]["stations"][0]["ps"] == "P4 Värml"
    r = await web.post(f"/api/v1/nodes/pending/{pending[0]['id']}/approve",
                       json={"name": "Kalle", "location": "Grums, Värmlands län"})
    node = (await r.json())["node"]
    assert node["name"] == "Kalle" and node["location_set"] == "Grums, Värmlands län"

    # Asking again (a lost answer) says approved; the token now works
    r = await _join(client, sat)
    assert r.status == 200 and (await r.json()) == {"status": "approved", "node": "Kalle"}
    hb = await client.post("/api/v1/ingest/heartbeat", json={}, headers=sat.headers())
    assert hb.status == 200
    assert (await (await web.get("/api/v1/nodes/pending")).json())["pending"] == []


async def test_asking_again_keeps_one_request(client, agg_store):
    sat = Sat()
    for _ in range(3):
        assert (await _join(client, sat)).status == 202
    assert len(agg_store.list_join_requests()) == 1


async def test_rejected(client, web, agg_store):
    sat = Sat()
    await _join(client, sat)
    [req] = agg_store.list_join_requests()
    assert (await web.post(f"/api/v1/nodes/pending/{req['id']}/reject", json={})).status == 200
    assert (await _join(client, sat)).status == 403
    assert agg_store.list_join_requests() == []


async def test_join_key_approves_at_once(client, agg_store, monkeypatch):
    monkeypatch.setattr(config, "JOIN_KEY", "delad-hemlighet")
    agg_store.create_node("Kalle, Karlstad")          # name taken
    sat = Sat()
    r = await _join(client, sat, join_key="delad-hemlighet")
    assert r.status == 200
    assert (await r.json())["node"] == "Kalle, Karlstad (2)"
    node = agg_store.authenticate(sat.token)
    assert node["location"] == "Karlstad, Värmlands län"
    # A wrong key is refused without saying why
    other = Sat("Lisa")
    r = await _join(client, other, join_key="fel")
    assert r.status == 403 and (await r.json())["error"] == "rejected"
    assert agg_store.list_join_requests() == []


async def test_join_key_unset_refuses_any_key(client, agg_store, monkeypatch):
    monkeypatch.setattr(config, "JOIN_KEY", "")
    assert (await _join(client, Sat(), join_key="gissning")).status == 403


async def test_validation_and_old_protocol(client, agg_store):
    sat = Sat()
    r = await client.post(URL, json=sat.body(), headers={**sat.headers(), H: "1"})
    assert r.status == 426
    r = await client.post(URL, json=sat.body(), headers={H: "2"})
    assert r.status == 401                                   # no token
    r = await client.post(URL, json=sat.body(),
                          headers={H: "2", "Authorization": "Bearer short"})
    assert r.status == 401
    assert (await _join(client, sat, join_id="nope")).status == 422
    # The join id belongs to the first token
    thief = Sat()
    thief.join_id = sat.join_id
    await _join(client, sat)
    assert (await _join(client, thief)).status == 409


async def test_limits(client, agg_store, monkeypatch):
    monkeypatch.setattr(config, "JOIN_MAX_PENDING", 2)
    for _ in range(2):
        assert (await _join(client, Sat())).status == 202
    assert (await _join(client, Sat())).status == 429


async def test_rate_limit_per_address(aiohttp_client, agg_store, monkeypatch):
    monkeypatch.setattr(config, "JOIN_RATE_PER_MIN", 3)
    client = await aiohttp_client(ingest.create_ingest_app())
    sat = Sat()
    codes = [(await _join(client, sat)).status for _ in range(5)]
    assert codes[:3] == [202, 202, 202] and codes[-1] == 429


def test_forwarded_for_only_from_trusted_proxies(monkeypatch):
    class Req:
        remote = "10.0.0.2"
        headers = {"X-Forwarded-For": "203.0.113.9, 10.0.0.2"}
    monkeypatch.setattr(config, "TRUSTED_PROXIES", set())
    assert ingest.client_address(Req()) == "10.0.0.2"
    monkeypatch.setattr(config, "TRUSTED_PROXIES", {"10.0.0.2"})
    assert ingest.client_address(Req()) == "203.0.113.9"


async def test_replace_node_keeps_its_history(client, web, agg_store):
    old, _ = agg_store.create_node("docker01")
    sat = Sat("docker01")
    await _join(client, sat)
    [req] = agg_store.list_join_requests()
    r = await web.post(f"/api/v1/nodes/pending/{req['id']}/approve",
                       json={"replace_node_id": old["id"]})
    assert (await r.json())["node"]["id"] == old["id"]
    assert agg_store.authenticate(sat.token)["id"] == old["id"]
    assert len(agg_store.list_nodes()) == 1


async def test_cli_pending_approve_reject(client, agg_store, capsys, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", agg_store._DB_PATH)
    a, b = Sat("Kalle"), Sat("Lisa")
    await _join(client, a)
    await _join(client, b)
    nodes.main(["pending"])
    out = capsys.readouterr().out
    code_a, code_b = uplink.verify_code(a.join_id), uplink.verify_code(b.join_id)
    assert code_a in out and code_b in out
    nodes.main(["approve", code_a.lower(), "--location", "Kil, Värmlands län"])
    assert "now reports as node 'Kalle'" in capsys.readouterr().out
    assert agg_store.authenticate(a.token)["location"] == "Kil, Värmlands län"
    nodes.main(["reject", code_b])
    assert (await _join(client, b)).status == 403


def test_old_requests_expire(agg_store):
    sat = Sat()
    agg_store.record_join_request(sat.token, sat.join_id, "AAA-BBB", {}, "1.2.3.4", 20)
    conn = agg_store._conn()
    conn.execute("UPDATE join_requests SET last_seen_at = '2020-01-01T00:00:00Z'")
    conn.commit()
    assert agg_store.purge_join_requests(24) == 1
