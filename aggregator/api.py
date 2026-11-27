"""Web UI, read API and node admin — served on the LAN port only.

Only the admin pages (push rules, nodes) have a login, see admin_auth and
ADMIN_PASSWORD. The rest has none: keep this port off the internet (a reverse proxy or
tunnel must only ever forward to the ingest port, never to this one). Write
requests must be JSON and same-origin, so a web page on another site
cannot drive the admin endpoints through the owner's browser.
"""

import asyncio
import functools
import pathlib
from urllib.parse import urlsplit

from aiohttp import web

import admin_auth
import notify_rules

import geolocate
import trafikverket

from . import config, nodes, notifier, store
from .ingest import valid_uid

STATIC_DIR = pathlib.Path(__file__).parent / "static"
# The web UI code and styles shared with the satellite (static/shared/)
SHARED_DIR = pathlib.Path(__file__).resolve().parent.parent / "static" / "shared"

ADMIN = admin_auth.AdminAuth(lambda: config.ADMIN_PASSWORD,
                             protected=("/api/v1/notify", "/api/v1/nodes",
                                        "/api/v1/incidents/rebuild"),
                             cookie_name="rdsguard_agg_admin")

PIPELINE = web.AppKey("pipeline", object)
MQTT = web.AppKey("mqtt", object)

_PUBLIC_EVENT_FIELDS = (
    "event_uid", "node_name", "type", "state", "pi", "station_ps", "long_ps",
    "frequency", "prog_type", "radiotext", "started_at", "ended_at",
    "duration_sec", "received_at", "transcription", "transcription_status",
    "transcription_duration_sec", "alert_status", "transcription_corrected",
    "geo_status",
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
    """Public events with their push log and map incidents attached."""
    uids = [r["event_uid"] for r in rows]
    log_rows = store.notify_for_events(uids)
    places = store.incidents_for_events(uids)
    out = []
    for r in rows:
        ev = public_event(r)
        ev["notify"] = log_rows.get(r["event_uid"], [])
        ev["incidents"] = places.get(r["event_uid"], [])
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
        "location": store.node_location(node),
        "location_set": node.get("location"),
        "location_reported": status.get("location"),
        "located": _located(node, pipeline),
        "local_processing": status.get("local_processing"),
    }


def _located(node, pipeline):
    """Where the node's NODE_LOCATION was found, for the Nodes view."""
    place = pipeline.node_place(node["id"]) if pipeline is not None and hasattr(
        pipeline, "node_place") else None
    if place is None:
        return None
    return {"name": place.name, "lat": place.lat, "lon": place.lon}


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
        "trafikverket": _trv_snapshot(pipeline),
        **counts,
    })


def _trv_snapshot(pipeline):
    feed = getattr(pipeline, "trv_feed", None)
    if feed is None:
        return {"enabled": False,
                "reason": "no_key" if not config.TRAFIKVERKET_API_KEY else "off"}
    return feed.snapshot()


async def handle_trafikverket(request):
    """GET /api/v1/trafikverket — Trafikverket's deviations near the nodes:
    current ones and those changed in the last hours, with their versions."""
    hours = _int_param(request, "hours", 12, 1, 24 * 7)
    if request.query.get("layer") in ("1", "true"):
        rows = await _run(store.trv.layer, hours)
    else:
        rows = await _run(lambda: store.trv.list(hours, versions=True))
    return web.json_response({
        "deviations": rows, "hours": hours,
        "trafikverket": _trv_snapshot(request.app[PIPELINE])})


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
    def work():
        out = _with_notify([row])[0]
        trafikverket.attach(store.trv, out.get("incidents") or [])
        return out
    return web.json_response(await _run(work))


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


def _decorate_incidents(rows):
    """Add to incidents what the map needs: the latest message (popup),
    each broadcast's time, node and audio (timeline), Trafikverket's data."""
    trafikverket.attach(store.trv, rows)
    briefs = store.event_briefs(
        t["event_key"] for inc in rows for t in inc.get("timeline", []))
    for inc in rows:
        ev = store.get_event(inc["events"][0]) if inc["events"] else None
        inc["latest"] = {
            "id": ev["event_uid"],
            "started_at": ev.get("started_at"),
            "station_ps": ev.get("station_ps"),
            "node_name": ev.get("node_name"),
            "transcription": ev.get("transcription_corrected")
            or ev.get("transcription"),
            "audio_url": (f"/api/v1/audio/{ev['event_uid']}.ogg"
                          if ev.get("audio_path") else None),
        } if ev else None
        for t in inc.get("timeline", []):
            b = briefs.get(str(t["event_key"]))
            t["event_id"] = b["event_uid"] if b else None
            t["started_at"] = b.get("started_at") if b else None
            t["station_ps"] = b.get("station_ps") if b else None
            t["node_name"] = b.get("node_name") if b else None
            t["audio_url"] = (f"/api/v1/audio/{b['event_uid']}.ogg"
                              if b and b.get("audio_path") else None)
    return rows


async def handle_incident(request):
    """GET /api/v1/incidents/{id} — one incident with every broadcast about
    it (time, node, kind, the words, audio), oldest first."""
    try:
        inc_id = int(request.match_info["incident_id"])
    except (TypeError, ValueError):
        return _error(400, "bad_id")

    def work():
        inc = store.get_incident(inc_id)
        return _decorate_incidents([inc])[0] if inc else None

    inc = await _run(work)
    if inc is None:
        return _error(404, "not_found")
    return web.json_response(inc)


async def handle_incidents(request):
    """GET /api/v1/incidents — incidents for the map (default: 12 hours)."""
    hours = _int_param(request, "hours", 12, 1, 24 * 7)
    history = request.query.get("history") in ("1", "true")
    if history:
        hours = None   # every incident still stored
    pipeline = request.app[PIPELINE]

    def work():
        rows = _decorate_incidents(store.list_incidents(hours))
        nodes = []
        for n in store.list_nodes():
            place = (pipeline.node_place(n["id"])
                     if pipeline is not None and hasattr(pipeline, "node_place")
                     else None)
            if place:
                nodes.append({"name": n["name"], "lat": place.lat,
                              "lon": place.lon})
        return rows, nodes

    rows, nodes = await _run(work)
    # Warnings for an area (slippery roads ...): the current ones only
    areas = await _run(store.list_areas, hours or geolocate.INCIDENT_HOURS)
    out = {
        "incidents": rows, "areas": areas, "hours": hours, "nodes": nodes,
        "geo": {"enabled": config.GEO_ENGINE != "none",
                "engine": ("claude" if config.GEO_ENGINE == "claude"
                           and config.ANTHROPIC_API_KEY else
                           "none" if config.GEO_ENGINE == "none" else "rules")},
    }
    if history and hasattr(pipeline, "backfill_status"):
        out["backfill"] = await _run(pipeline.backfill_status)
    return web.json_response(out)


async def handle_incidents_backfill(request):
    """POST /api/v1/incidents/backfill — place the messages stored before
    the map existed (once; later messages are placed as they come)."""
    pipeline = request.app[PIPELINE]
    if not hasattr(pipeline, "start_backfill"):
        return _error(503, "unavailable", "no processing pipeline")
    started = await _run(pipeline.start_backfill)
    st = await _run(pipeline.backfill_status)
    return web.json_response({"started": started, "backfill": st},
                             status=202 if started else 200)


async def handle_incidents_rebuild_info(request):
    """GET /api/v1/incidents/rebuild (admin) — what rebuilding the map would
    do: how many messages, which engine, the cost, how far a run has come."""
    pipeline = request.app[PIPELINE]
    if not hasattr(pipeline, "rebuild_status"):
        return _error(503, "unavailable", "no processing pipeline")
    return web.json_response(await _run(pipeline.rebuild_status))


async def handle_incidents_rebuild(request):
    """POST /api/v1/incidents/rebuild (admin) — delete every incident and
    place all stored messages again, oldest first, with the current engine."""
    pipeline = request.app[PIPELINE]
    if not hasattr(pipeline, "start_rebuild"):
        return _error(503, "unavailable", "no processing pipeline")
    started = await _run(pipeline.start_rebuild)
    st = await _run(pipeline.rebuild_status)
    return web.json_response({"started": started, "rebuild": st},
                             status=202 if started else 409)


async def handle_stations(request):
    return web.json_response({"stations": await _run(store.list_stations)})


# ---------------------------------------------------------------------------
# Node admin
# ---------------------------------------------------------------------------

async def handle_nodes(request):
    pipeline = request.app[PIPELINE]

    def work():
        # public_node looks up each node's location in the place-name
        # registry, which reads a file the first time: not on the loop
        return [public_node(n, pipeline) for n in store.list_nodes()]

    return web.json_response({"nodes": await _run(work)})


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
    enabled = body.get("enabled") if body else None
    has_location = bool(body) and "location" in body
    location = body.get("location") if has_location else None
    if (body is None or (enabled is None and not has_location)
            or (enabled is not None and not isinstance(enabled, bool))
            or (location is not None and not isinstance(location, str))):
        return _error(422, "invalid",
                      "expected {\"enabled\": true|false} and/or "
                      "{\"location\": \"Ort, Län\"|null}")
    if location is not None:
        location = " ".join(location.split())
        if len(location) > 80:
            return _error(422, "invalid", "location is at most 80 characters")
    node = await _run(store.get_node, node_id) if node_id else None
    if node is None:
        return _error(404, "not_found")
    if enabled is not None:
        await _run(store.set_node_enabled, node_id, enabled)
    if has_location:
        await _run(store.set_node_location, node_id, location)
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
    # strict-origin-when-cross-origin, not same-origin: tile.openstreetmap.org
    # answers 403 "Access blocked" to tile requests without a Referer. This
    # sends only the origin (scheme, host, port) cross-origin, never the path.
    resp.headers.setdefault("Referrer-Policy",
                            "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault(
        "Content-Security-Policy",
        # blob: media for the audio player (wavesurfer.js); inline styles
        # for the player's waveform and the UI's layout attributes
        # img-src: map tiles from OpenStreetMap for the map view
        "default-src 'self'; media-src 'self' blob:; "
        "img-src 'self' data: https://tile.openstreetmap.org; "
        "style-src 'self' 'unsafe-inline'; object-src 'none'; "
        "frame-ancestors 'none'")
    return resp


def create_web_app(pipeline=None, mqtt=None):
    app = web.Application(middlewares=[security_headers, write_guard,
                                       ADMIN.middleware],
                          client_max_size=64 * 1024)
    app[PIPELINE] = pipeline
    app[MQTT] = mqtt
    r = app.router
    r.add_get("/api/v1/status", handle_status)
    r.add_get("/api/v1/events", handle_events)
    r.add_get("/api/v1/events/active", handle_events_active)
    r.add_get("/api/v1/events/{uid}", handle_event)
    r.add_get("/api/v1/calendar", handle_calendar)
    ADMIN.add_routes(r, "/api/v1")
    r.add_get("/api/v1/notify", handle_notify)
    r.add_get("/api/v1/notify/log", handle_notify_log)
    r.add_post("/api/v1/notify/rules", handle_create_rule)
    r.add_put("/api/v1/notify/rules/{rule_id}", handle_update_rule)
    r.add_delete("/api/v1/notify/rules/{rule_id}", handle_delete_rule)
    r.add_post("/api/v1/notify/preview", handle_preview_rule)
    r.add_post("/api/v1/notify/test", handle_test_rule)
    r.add_get("/api/v1/audio/{uid}.ogg", handle_audio)
    r.add_get("/api/v1/stations", handle_stations)
    r.add_get("/api/v1/incidents", handle_incidents)
    r.add_post("/api/v1/incidents/backfill", handle_incidents_backfill)
    r.add_get("/api/v1/incidents/rebuild", handle_incidents_rebuild_info)
    r.add_post("/api/v1/incidents/rebuild", handle_incidents_rebuild)
    r.add_get("/api/v1/incidents/{incident_id:\\d+}", handle_incident)
    r.add_get("/api/v1/trafikverket", handle_trafikverket)
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
