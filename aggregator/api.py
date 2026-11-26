"""Web UI, read API and node admin — served on the LAN port only.

This server has no login: keep it off the internet (the nginx config only
forwards /api/v1/ingest/ to the ingest port, never this one). Write
requests must be JSON and same-origin, so a web page on another site
cannot drive the admin endpoints through the owner's browser.
"""

import asyncio
import functools
import pathlib
from urllib.parse import urlsplit

from aiohttp import web

import notify_rules

from . import config, nodes, notifier, store
from .ingest import valid_uid

STATIC_DIR = pathlib.Path(__file__).parent / "static"
# The web UI code and styles shared with the satellite (static/shared/)
SHARED_DIR = pathlib.Path(__file__).resolve().parent.parent / "static" / "shared"

PIPELINE = web.AppKey("pipeline", object)
MQTT = web.AppKey("mqtt", object)

_PUBLIC_EVENT_FIELDS = (
    "event_uid", "node_name", "type", "state", "pi", "station_ps", "long_ps",
    "frequency", "prog_type", "radiotext", "started_at", "ended_at",
    "duration_sec", "received_at", "transcription", "transcription_status",
    "transcription_duration_sec", "alert_status",
)


async def _run(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


def _error(status, code, detail=""):
    return web.json_response({"error": code, "detail": detail}, status=status)


def public_event(row):
    ev = {k: row.get(k) for k in _PUBLIC_EVENT_FIELDS}
    ev["audio_url"] = (f"/api/v1/audio/{row['event_uid']}.ogg"
                       if row.get("audio_path") else None)
    return ev


def _with_notify(rows):
    """Public events with their push log attached."""
    log_rows = store.notify_for_events([r["event_uid"] for r in rows])
    out = []
    for r in rows:
        ev = public_event(r)
        ev["notify"] = log_rows.get(r["event_uid"], [])
        out.append(ev)
    return out


def public_node(node, pipeline=None):
    status = node.get("status") or {}
    return {
        "id": node["id"],
        "name": node["name"],
        "enabled": node["enabled"],
        "online": bool(pipeline and pipeline.node_online(node["id"])),
        "token_prefix": node["token_prefix"],
        "created_at": node["created_at"],
        "last_seen_at": node.get("last_seen_at"),
        "last_heartbeat_at": node.get("last_heartbeat_at"),
        "reported_name": node.get("reported_name"),
        "version": node.get("version"),
        "clock_offset_sec": node.get("clock_offset_sec"),
        "uptime_sec": status.get("uptime_sec"),
        "pipeline": status.get("pipeline"),
        "stations": status.get("stations") or [],
        "queue_depth": status.get("queue_depth"),
    }


# ---------------------------------------------------------------------------
# Same-origin JSON guard for write requests
# ---------------------------------------------------------------------------

@web.middleware
async def write_guard(request, handler):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.content_type != "application/json":
            return _error(415, "json_required",
                          "write requests must use Content-Type: application/json")
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            return _error(403, "cross_origin", "cross-origin request refused")
    return await handler(request)


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------

def _int_param(request, key, default, lo, hi):
    try:
        return max(lo, min(hi, int(request.query.get(key, default))))
    except (TypeError, ValueError):
        return default


async def handle_status(request):
    mqtt = request.app[MQTT]
    pipeline = request.app[PIPELINE]
    counts = await _run(store.counts)
    node_list = await _run(store.list_nodes)
    online = sum(1 for n in node_list
                 if pipeline is not None and pipeline.node_online(n["id"]))
    return web.json_response({
        "version": config.BUILD_VERSION,
        "server_time": store.now_iso(),
        "public_url": config.PUBLIC_URL or None,
        "transcription_engine": config.TRANSCRIPTION_ENGINE,
        "mqtt": {"enabled": mqtt is not None,
                 "connected": bool(mqtt and mqtt.connected),
                 "prefix": config.MQTT_TOPIC_PREFIX},
        "nodes_online": online,
        **counts,
    })


async def handle_events(request):
    q = request.query
    active = {"1": True, "true": True, "0": False, "false": False}.get(
        q.get("active", "").lower())
    def work():
        rows, total = store.query_events(
            event_type=q.get("type") or None, pi=q.get("pi") or None,
            node=q.get("node") or None, since=q.get("since") or None,
            active=active,
            limit=_int_param(request, "limit", 50, 1, 200),
            offset=_int_param(request, "offset", 0, 0, 10**9),
            q=q.get("q") or None, start=q.get("from") or None,
            end=q.get("to") or None)
        return _with_notify(rows), total

    events, total = await _run(work)
    return web.json_response({"events": events, "total": total})


async def handle_events_active(request):
    rows, _ = await _run(store.query_events, active=True, limit=200)
    return web.json_response({"events": [public_event(r) for r in rows]})


async def handle_event(request):
    uid = request.match_info["uid"]
    row = await _run(store.get_event, uid) if valid_uid(uid) else None
    if row is None:
        return _error(404, "not_found")
    return web.json_response((await _run(_with_notify, [row]))[0])


async def handle_calendar(request):
    """Light rows for the archive calendar over the retention period."""
    def work():
        rows = store.calendar_events(store.iso_ago((config.EVENT_RETENTION_DAYS + 1) * 86400))
        log_rows = store.notify_for_events([r["event_uid"] for r in rows])
        for r in rows:
            r["pushed"] = any(x["status"] == store.NOTIFY_SENT
                              for x in log_rows.get(r["event_uid"], []))
        return rows
    return web.json_response({
        "events": await _run(work),
        "retention_days": config.EVENT_RETENTION_DAYS,
        "timezone": config.NOTIFY_TIMEZONE,
    })


# ---------------------------------------------------------------------------
# Push notification rules
# ---------------------------------------------------------------------------

def _rule_id(request):
    try:
        return int(request.match_info["rule_id"])
    except (TypeError, ValueError):
        return None


async def _rule_body(request):
    body = await _json_body(request)
    return notify_rules.validate(body), body


async def handle_notify(request):
    mqtt = request.app[MQTT]
    return web.json_response({
        "rules": await _run(store.list_rules),
        "mqtt": {"enabled": mqtt is not None,
                 "connected": bool(mqtt and mqtt.connected),
                 "host": config.MQTT_HOST or None, "port": config.MQTT_PORT,
                 "client_id": config.MQTT_CLIENT_ID,
                 "prefix": config.MQTT_TOPIC_PREFIX},
        "web_ui_url": config.WEB_UI_URL or None,
        "timezone": config.NOTIFY_TIMEZONE,
        "retention_days": config.EVENT_RETENTION_DAYS,
    })


async def handle_create_rule(request):
    try:
        rule, _ = await _rule_body(request)
    except notify_rules.RuleError as e:
        return _error(422, "invalid", str(e))
    return web.json_response(await _run(store.create_rule, rule), status=201)


async def handle_update_rule(request):
    rule_id = _rule_id(request)
    try:
        rule, _ = await _rule_body(request)
    except notify_rules.RuleError as e:
        return _error(422, "invalid", str(e))
    saved = await _run(store.update_rule, rule_id, rule) if rule_id else None
    if saved is None:
        return _error(404, "not_found")
    return web.json_response(saved)


async def handle_delete_rule(request):
    rule_id = _rule_id(request)
    if not rule_id or not await _run(store.delete_rule, rule_id):
        return _error(404, "not_found")
    return web.json_response({"deleted": True})


async def handle_preview_rule(request):
    try:
        rule, _ = await _rule_body(request)
    except notify_rules.RuleError as e:
        return _error(422, "invalid", str(e))

    def work():
        events = store.events_for_preview(
            store.iso_ago((config.EVENT_RETENTION_DAYS + 1) * 86400))
        res = notify_rules.preview(rule, events, notifier.zone())
        res["samples"] = [{k: e.get(k) for k in (
            "event_uid", "type", "pi", "station_ps", "radiotext", "started_at",
            "duration_sec", "node_name")} for e in res["samples"]]
        return res

    return web.json_response(await _run(work))


async def handle_test_rule(request):
    try:
        rule, body = await _rule_body(request)
    except notify_rules.RuleError as e:
        return _error(422, "invalid", str(e))
    if isinstance(body.get("id"), int):
        rule["id"] = body["id"]
    mqtt = request.app[MQTT]

    def publish(topic, payload):
        return None if mqtt is None else mqtt.publish(topic, payload, qos=1)

    status, topic, payload = await _run(notifier.send_test, rule, publish)
    return web.json_response({"status": status,
                              "topic": f"{config.MQTT_TOPIC_PREFIX}/{topic}",
                              "payload": payload})


async def handle_notify_log(request):
    limit = _int_param(request, "limit", 30, 1, 100)
    return web.json_response({"log": await _run(store.notify_log_recent, limit)})


async def handle_audio(request):
    """Serve the whole file without range support.

    Same reason as on the satellite: some browsers stop OGG/Opus playback
    when a range request lands in the middle of an OGG page.
    """
    uid = request.match_info["uid"]
    if not valid_uid(uid):
        return _error(404, "not_found")
    row = await _run(store.get_event, uid)
    if row is None or not row.get("audio_path"):
        return _error(404, "not_found")
    path = pathlib.Path(config.AUDIO_DIR) / pathlib.Path(row["audio_path"]).name
    try:
        body = await _run(path.read_bytes)
    except FileNotFoundError:
        return _error(404, "not_found")
    return web.Response(body=body, content_type="audio/ogg", headers={
        "Accept-Ranges": "none", "Cache-Control": "no-store"})


async def handle_stations(request):
    return web.json_response({"stations": await _run(store.list_stations)})


# ---------------------------------------------------------------------------
# Node admin
# ---------------------------------------------------------------------------

async def handle_nodes(request):
    pipeline = request.app[PIPELINE]
    node_list = await _run(store.list_nodes)
    return web.json_response({"nodes": [public_node(n, pipeline) for n in node_list]})


async def _json_body(request):
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _node_id(request):
    try:
        return int(request.match_info["node_id"])
    except (TypeError, ValueError):
        return None


async def handle_create_node(request):
    body = await _json_body(request)
    if body is None:
        return _error(422, "invalid", "body must be a JSON object")
    try:
        name = nodes.clean_name(body.get("name") if isinstance(body.get("name"), str) else "")
        node, token = await _run(store.create_node, name)
    except ValueError as e:
        return _error(422, "invalid", str(e))
    return web.json_response({
        "node": public_node(node, request.app[PIPELINE]),
        "token": token,
        "snippet": nodes.setup_snippet(node["name"], token),
    }, status=201)


async def handle_rotate_node(request):
    node_id = _node_id(request)
    node = await _run(store.get_node, node_id) if node_id else None
    if node is None:
        return _error(404, "not_found")
    token = await _run(store.rotate_token, node_id)
    return web.json_response({
        "token": token,
        "snippet": nodes.setup_snippet(node["name"], token),
    })


async def handle_update_node(request):
    node_id = _node_id(request)
    body = await _json_body(request)
    if body is None or not isinstance(body.get("enabled"), bool):
        return _error(422, "invalid", "expected {\"enabled\": true|false}")
    if not node_id or not await _run(store.set_node_enabled, node_id, body["enabled"]):
        return _error(404, "not_found")
    node = await _run(store.get_node, node_id)
    return web.json_response({"node": public_node(node, request.app[PIPELINE])})


async def handle_delete_node(request):
    node_id = _node_id(request)
    paths = await _run(store.delete_node, node_id) if node_id else None
    if paths is None:
        return _error(404, "not_found")
    await _run(nodes.remove_audio_files, paths)
    return web.json_response({"deleted": True})


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

async def handle_index(request):
    return web.FileResponse(STATIC_DIR / "index.html")


@web.middleware
async def security_headers(request, handler):
    resp = await handler(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault(
        "Content-Security-Policy",
        # blob: media for the audio player (wavesurfer.js); inline styles
        # for the player's waveform and the UI's layout attributes
        "default-src 'self'; media-src 'self' blob:; "
        "style-src 'self' 'unsafe-inline'; object-src 'none'; "
        "frame-ancestors 'none'")
    return resp


def create_web_app(pipeline=None, mqtt=None):
    app = web.Application(middlewares=[security_headers, write_guard],
                          client_max_size=64 * 1024)
    app[PIPELINE] = pipeline
    app[MQTT] = mqtt
    r = app.router
    r.add_get("/api/v1/status", handle_status)
    r.add_get("/api/v1/events", handle_events)
    r.add_get("/api/v1/events/active", handle_events_active)
    r.add_get("/api/v1/events/{uid}", handle_event)
    r.add_get("/api/v1/calendar", handle_calendar)
    r.add_get("/api/v1/notify", handle_notify)
    r.add_get("/api/v1/notify/log", handle_notify_log)
    r.add_post("/api/v1/notify/rules", handle_create_rule)
    r.add_put("/api/v1/notify/rules/{rule_id}", handle_update_rule)
    r.add_delete("/api/v1/notify/rules/{rule_id}", handle_delete_rule)
    r.add_post("/api/v1/notify/preview", handle_preview_rule)
    r.add_post("/api/v1/notify/test", handle_test_rule)
    r.add_get("/api/v1/audio/{uid}.ogg", handle_audio)
    r.add_get("/api/v1/stations", handle_stations)
    r.add_get("/api/v1/nodes", handle_nodes)
    r.add_post("/api/v1/nodes", handle_create_node)
    r.add_post("/api/v1/nodes/{node_id}/rotate", handle_rotate_node)
    r.add_patch("/api/v1/nodes/{node_id}", handle_update_node)
    r.add_delete("/api/v1/nodes/{node_id}", handle_delete_node)
    if SHARED_DIR.exists():
        r.add_static("/static/shared/", SHARED_DIR, show_index=False)
    r.add_static("/static/", STATIC_DIR, show_index=False)
    r.add_get("/", handle_index)
    return app
