"""Aggregator admin login: ADMIN_PASSWORD guards the push rules and nodes."""

import pytest

import admin_auth
from aggregator import api as agg_api
from aggregator import config as agg_config
from aggregator import notifier


@pytest.fixture
async def web(aiohttp_client, agg_store, monkeypatch):
    monkeypatch.setattr(agg_config, "ADMIN_PASSWORD", "hemligt")
    monkeypatch.setattr(admin_auth, "FAIL_DELAY_SEC", 0)
    agg_api.ADMIN._sessions.clear()
    agg_api.ADMIN._fails.clear()
    notifier.init()
    return await aiohttp_client(agg_api.create_web_app())


async def test_rules_and_nodes_need_login(web, agg_store):
    assert (await web.get("/api/v1/notify")).status == 401
    assert (await web.get("/api/v1/nodes")).status == 401
    assert (await web.post("/api/v1/nodes", json={"name": "x"})).status == 401
    assert (await web.post("/api/v1/notify/rules", json={"name": "x"})).status == 401
    assert agg_store.list_nodes() == []
    # the status, stations, archive and calendar stay public
    for path in ("/api/v1/status", "/api/v1/stations", "/api/v1/events", "/api/v1/calendar"):
        assert (await web.get(path)).status == 200, path
    assert await (await web.get("/api/v1/admin/session")).json() == {
        "enabled": True, "logged_in": False}


async def test_login_logout(web):
    assert (await web.post("/api/v1/admin/login", json={"password": "fel"})).status == 401
    resp = await web.post("/api/v1/admin/login", json={"password": "hemligt"})
    assert resp.status == 200
    cookie = resp.cookies["rdsguard_agg_admin"]
    assert cookie["httponly"] and cookie["samesite"] == "Strict"
    assert (await web.get("/api/v1/notify")).status == 200
    assert (await web.get("/api/v1/nodes")).status == 200
    assert (await (await web.get("/api/v1/admin/session")).json())["logged_in"]
    await web.post("/api/v1/admin/logout", json={})
    assert (await web.get("/api/v1/notify")).status == 401


async def test_new_password_ends_sessions(web, monkeypatch):
    await web.post("/api/v1/admin/login", json={"password": "hemligt"})
    assert (await web.get("/api/v1/notify")).status == 200
    monkeypatch.setattr(agg_config, "ADMIN_PASSWORD", "nytt")
    assert (await web.get("/api/v1/notify")).status == 401


async def test_forged_cookie_is_refused(web):
    web.session.cookie_jar.update_cookies({"rdsguard_agg_admin": "påhittad"})
    assert (await web.get("/api/v1/notify")).status == 401


async def test_lockout_after_repeated_failures(web):
    for _ in range(admin_auth.MAX_FAILS):
        assert (await web.post("/api/v1/admin/login", json={"password": "x"})).status == 401
    # even the right password waits out the lockout
    assert (await web.post("/api/v1/admin/login", json={"password": "hemligt"})).status == 429


async def test_login_must_be_json_and_same_origin(web):
    resp = await web.post("/api/v1/admin/login", data="password=hemligt",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert resp.status == 415
    resp = await web.post("/api/v1/admin/login", json={"password": "hemligt"},
                          headers={"Origin": "https://evil.example"})
    assert resp.status == 403


async def test_no_password_switches_admin_off(web, monkeypatch):
    monkeypatch.setattr(agg_config, "ADMIN_PASSWORD", "")
    assert await (await web.get("/api/v1/admin/session")).json() == {
        "enabled": False, "logged_in": False}
    assert (await web.post("/api/v1/admin/login", json={"password": ""})).status == 403
    assert (await web.get("/api/v1/notify")).status == 403
