"""Admin login: ADMIN_PASSWORD guards the push rules on both apps."""

import pytest

import admin_auth
import config
import notifier
import web_server


@pytest.fixture
async def sat(aiohttp_client, store, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "hemligt")
    monkeypatch.setattr(admin_auth, "FAIL_DELAY_SEC", 0)
    web_server.ADMIN._sessions.clear()
    web_server.ADMIN._fails.clear()
    notifier.init()
    return await aiohttp_client(web_server.create_app())


async def test_rules_need_login(sat):
    assert (await sat.get("/api/notify")).status == 401
    assert (await sat.post("/api/notify/rules", json={"name": "x"})).status == 401
    # the public read API stays open
    assert (await sat.get("/api/calendar")).status == 200
    assert await (await sat.get("/api/admin/session")).json() == {
        "enabled": True, "logged_in": False}


async def test_login_logout(sat):
    resp = await sat.post("/api/admin/login", json={"password": "fel"})
    assert resp.status == 401
    resp = await sat.post("/api/admin/login", json={"password": "hemligt"})
    assert resp.status == 200
    cookie = resp.cookies["rdsguard_admin"]
    assert cookie["httponly"] and cookie["samesite"] == "Strict"
    assert (await sat.get("/api/notify")).status == 200
    assert (await (await sat.get("/api/admin/session")).json())["logged_in"]

    await sat.post("/api/admin/logout", json={})
    assert (await sat.get("/api/notify")).status == 401


async def test_new_password_ends_sessions(sat, monkeypatch):
    await sat.post("/api/admin/login", json={"password": "hemligt"})
    assert (await sat.get("/api/notify")).status == 200
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "nytt")
    assert (await sat.get("/api/notify")).status == 401


async def test_forged_cookie_is_refused(sat):
    sat.session.cookie_jar.update_cookies({"rdsguard_admin": "påhittad"})
    assert (await sat.get("/api/notify")).status == 401


async def test_lockout_after_repeated_failures(sat):
    for _ in range(admin_auth.MAX_FAILS):
        assert (await sat.post("/api/admin/login", json={"password": "x"})).status == 401
    # even the right password waits out the lockout
    assert (await sat.post("/api/admin/login", json={"password": "hemligt"})).status == 429


async def test_login_must_be_json_and_same_origin(sat):
    resp = await sat.post("/api/admin/login", data="password=hemligt",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert resp.status == 415
    resp = await sat.post("/api/admin/login", json={"password": "hemligt"},
                          headers={"Origin": "https://evil.example"})
    assert resp.status == 403


async def test_no_password_switches_admin_off(sat, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    assert await (await sat.get("/api/admin/session")).json() == {
        "enabled": False, "logged_in": False}
    assert (await sat.post("/api/admin/login", json={"password": ""})).status == 403
    assert (await sat.get("/api/notify")).status == 403

