"""Aggregator admin login: ADMIN_PASSWORD guards the push rules."""

from aggregator import api as agg_api
from aggregator import config as agg_config


async def test_rules_and_nodes_need_login(aiohttp_client, agg_store, monkeypatch):
    monkeypatch.setattr(agg_config, "ADMIN_PASSWORD", "hemligt")
    client = await aiohttp_client(agg_api.create_web_app())
    assert (await client.get("/api/v1/notify")).status == 401
    assert (await client.get("/api/v1/nodes")).status == 401
    assert (await client.post("/api/v1/nodes", json={"name": "x"})).status == 401
    assert agg_store.list_nodes() == []
    # the status and stations stay public
    assert (await client.get("/api/v1/status")).status == 200
    assert (await client.get("/api/v1/stations")).status == 200
    assert (await client.get("/api/v1/events")).status == 200
    resp = await client.post("/api/v1/admin/login", json={"password": "hemligt"})
    assert resp.status == 200 and "rdsguard_agg_admin" in resp.cookies
    assert (await client.get("/api/v1/notify")).status == 200
    assert (await client.get("/api/v1/nodes")).status == 200
