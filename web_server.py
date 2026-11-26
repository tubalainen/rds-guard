"""Web server for RDS Guard — REST API + WebSocket + static files.

Uses aiohttp to serve:
  GET  /              → static web UI (index.html)
  GET  /api/events    → query events (type, q, from, to, pi, limit, offset)
  GET  /api/events/active → in-progress announcements
  GET  /api/events/:id → one event
  GET  /api/calendar  → light rows for the archive calendar
  GET  /api/incidents → incidents for the map (?history=1: all stored)
  GET  /api/incidents/{id} → one incident with every broadcast about it
  POST /api/incidents/backfill → place messages stored before the map existed
  GET  /api/admin/session, POST /api/admin/login|logout → admin login
  (every /api/notify* route below needs a logged-in admin, see admin_auth)
  GET  /api/notify    → push rules + MQTT connection
  POST/PUT/DELETE /api/notify/rules[/:id] → edit push rules
  POST /api/notify/preview → try a rule on the stored history
  POST /api/notify/test → send a test message
  GET  /api/notify/log → recent push messages
  GET  /api/status    → decoder status from memory
  DELETE /api/events  → clear all events
  WS   /ws/console    → live decoded message stream
"""

import asyncio
import functools
import json
import logging
import pathlib
from urllib.parse import urlsplit

from aiohttp import web

import admin_auth
import event_store
import notifier
import notify_rules

log = logging.getLogger("rds-guard")

STATIC_DIR = pathlib.Path(__file__).parent / "static"

# References set by run_server()
_stats = None
_station_info = None
_mqtt_connected = None
_pipeline_status = None


# ---------------------------------------------------------------------------
# REST API handlers
# ---------------------------------------------------------------------------

def _public_row(row):
    """An events row as the API returns it."""
    if row.get("radiotext") and isinstance(row["radiotext"], str):
        try:
            row["radiotext"] = json.loads(row["radiotext"])
        except (json.JSONDecodeError, TypeError):
            pass
    if row.get("data") and isinstance(row["data"], str):
        try:
            row["data"] = json.loads(row["data"])
        except (json.JSONDecodeError, TypeError):
            pass
    row["audio_url"] = (f"/api/audio/{row['audio_path']}"
                        if row.get("audio_path") else None)
    return row


def _attach_notify(rows):
    log_rows = event_store.notify_for_events([r["id"] for r in rows])
    places = event_store.incidents_for_events([r["id"] for r in rows])
    for row in rows:
        row["notify"] = log_rows.get(row["id"], [])
        row["incidents"] = places.get(str(row["id"]), [])
    return rows


async def _run(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


async def handle_events(request):
    """GET /api/events — query events with optional filters.

    type, since, limit, offset, plus q (free text), from/to (UTC, on when
    the broadcast started) and pi.
    """
    query = request.query
    try:
        limit = min(int(query.get("limit", 50)), 200)
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = max(0, int(query.get("offset", 0)))
    except (ValueError, TypeError):
        offset = 0

    def work():
        rows, total = event_store.query_events(
            query.get("type"), query.get("since"), limit, offset,
            q=query.get("q"), start=query.get("from"), end=query.get("to"),
            pi=query.get("pi"))
        return _attach_notify([_public_row(r) for r in rows]), total

    rows, total = await _run(work)
    return web.json_response({"events": rows, "total": total})


async def handle_event(request):
    """GET /api/events/{id} — one event."""
    try:
        event_id = int(request.match_info["event_id"])
    except (TypeError, ValueError):
        return web.json_response({"error": "not_found"}, status=404)

    def work():
        import trafikverket
        row = event_store.get_event(event_id)
        if not row:
            return None
        row = _attach_notify([_public_row(row)])[0]
        trafikverket.attach(event_store.trv, row.get("incidents") or [])
        return row

    row = await _run(work)
    if row is None:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response(row)


async def handle_events_active(request):
    """GET /api/events/active — in-progress announcements."""
    rows = await _run(event_store.get_active_events)
    return web.json_response({"events": [_public_row(r) for r in rows]})


def _since_days(days):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S",
                         time.gmtime(time.time() - days * 86400))


async def handle_calendar(request):
    """GET /api/calendar — light rows for the whole retention period."""
    import config as cfg

    def work():
        rows = event_store.calendar_events(_since_days(cfg.EVENT_RETENTION_DAYS + 1))
        log_rows = event_store.notify_for_events([r["id"] for r in rows])
        for r in rows:
            r["pushed"] = any(l["status"] == event_store.NOTIFY_SENT
                              for l in log_rows.get(r["id"], []))
        return rows

    rows = await _run(work)
    return web.json_response({
        "events": rows,
        "retention_days": cfg.EVENT_RETENTION_DAYS,
        "timezone": cfg.NOTIFY_TIMEZONE,
    })


def _decorate_incidents(rows):
    """Add to incidents what the map needs: the latest message (popup),
    each broadcast's time and audio (timeline) and Trafikverket's data."""
    latest = {}
    for inc in rows:
        for key in inc["events"][:1]:
            try:
                ev = event_store.get_event(int(key))
            except (TypeError, ValueError):
                ev = None
            if ev:
                latest[inc["id"]] = {
                    "id": ev["id"],
                    "started_at": ev.get("started_at"),
                    "station_ps": ev.get("station_ps"),
                    "transcription": ev.get("transcription_corrected")
                    or ev.get("transcription"),
                    "audio_url": (f"/api/audio/{ev['audio_path']}"
                                  if ev.get("audio_path") else None),
                }
    briefs = event_store.event_briefs(
        t["event_key"] for inc in rows for t in inc.get("timeline", []))
    for inc in rows:
        for t in inc.get("timeline", []):
            ev = briefs.get(int(t["event_key"])) if str(t["event_key"]).isdigit() else None
            t["event_id"] = ev["id"] if ev else None
            t["started_at"] = ev.get("started_at") if ev else None
            t["station_ps"] = ev.get("station_ps") if ev else None
            t["audio_url"] = (f"/api/audio/{ev['audio_path']}"
                              if ev and ev.get("audio_path") else None)
    import trafikverket
    trafikverket.attach(event_store.trv, rows)
    for inc in rows:
        inc["latest"] = latest.get(inc["id"])
        inc["events"] = [int(k) for k in inc["events"] if str(k).isdigit()]
    return rows


async def handle_incident(request):
    """GET /api/incidents/{id} — one incident with every broadcast about it
    (time, kind, the words, audio), oldest first."""
    try:
        inc_id = int(request.match_info["incident_id"])
    except (TypeError, ValueError):
        return _error(400, "bad_id")

    def work():
        inc = event_store.get_incident(inc_id)
        return _decorate_incidents([inc])[0] if inc else None

    inc = await _run(work)
    if inc is None:
        return _error(404, "not_found")
    return web.json_response(inc)


async def handle_incidents(request):
    """GET /api/incidents — incidents for the map (default: last 12 hours).

    ?history=1 gives every incident still stored (EVENT_RETENTION_DAYS),
    plus how far placing the older messages has come (backfill).
    """
    history = request.query.get("history") in ("1", "true")
    try:
        hours = max(1, min(int(request.query.get("hours", 12)), 24 * 7))
    except (TypeError, ValueError):
        hours = 12
    if history:
        hours = None

    import rds_guard as rg
    rows = await _run(lambda: _decorate_incidents(event_store.list_incidents(hours)))
    out = {"incidents": rows, "hours": hours, "geo": rg.geo_snapshot()}
    if history:
        out["backfill"] = await _run(rg.backfill_status)
    return web.json_response(out)


async def handle_incidents_backfill(request):
    """POST /api/incidents/backfill — place the messages stored before the
    map existed (once; later messages are placed as they come)."""
    import rds_guard as rg
    started = await _run(rg.start_backfill)
    st = await _run(rg.backfill_status)
    return web.json_response({"started": started, "backfill": st},
                             status=202 if started else 200)


async def handle_trafikverket(request):
    """GET /api/trafikverket — Trafikverket's deviations near the receiver:
    current ones and those changed in the last hours (default 12), each with
    its versions for the timeline. layer=1: only those for the map layer
    (current accidents, obstacles and the like no radio incident is linked
    to)."""
    try:
        hours = max(1, min(int(request.query.get("hours", 12)), 24 * 7))
    except (TypeError, ValueError):
        hours = 12
    import rds_guard as rg
    if request.query.get("layer") in ("1", "true"):
        rows = await _run(event_store.trv.layer, hours)
    else:
        rows = await _run(lambda: event_store.trv.list(hours, versions=True))
    return web.json_response({"deviations": rows, "hours": hours,
                              "trafikverket": rg.trv_snapshot()})


async def handle_status(request):
    """GET /api/status — decoder status."""
    import status
    return web.json_response(status.build_status())


async def handle_delete_events(request):
    """DELETE /api/events — clear all events."""
    count = await asyncio.get_event_loop().run_in_executor(
        None, event_store.delete_all_events
    )
    return web.json_response({"deleted": count})


async def handle_audio(request):
    """GET /api/audio/{filename} — serve audio file.

    Serves the complete file in a single response with Accept-Ranges: none.
    Range requests on OGG/Opus files cause playback to stop at 5-10 seconds
    in some browsers: the browser requests bytes 0-65535, then makes a second
    range request starting at byte 65536, which may land in the middle of an
    OGG page.  The browser's Opus decoder cannot resync from an arbitrary byte
    offset (unlike VLC), so playback silently stops.  Serving the full file
    avoids this entirely.
    """
    import config as cfg

    filename = request.match_info["filename"]

    # Security: prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        return web.Response(status=400, text="Invalid filename")

    audio_dir = pathlib.Path(cfg.AUDIO_DIR)
    file_path = audio_dir / filename

    if not file_path.exists():
        return web.Response(status=404, text="Audio file not found")

    # Determine content type
    suffix = file_path.suffix.lower()
    content_types = {
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".opus": "audio/opus",
    }
    content_type = content_types.get(suffix, "application/octet-stream")

    body = await asyncio.get_event_loop().run_in_executor(
        None, file_path.read_bytes
    )
    return web.Response(
        body=body,
        content_type=content_type,
        headers={
            "Content-Length": str(len(body)),
            "Accept-Ranges": "none",
            "Cache-Control": "no-store",
        },
    )



# ---------------------------------------------------------------------------
# Admin login and push notification rules
# ---------------------------------------------------------------------------

def _admin_password():
    import config as cfg
    return cfg.ADMIN_PASSWORD


ADMIN = admin_auth.AdminAuth(_admin_password, protected=("/api/notify",),
                             cookie_name="rdsguard_admin")


def _error(status, code, detail=""):
    return web.json_response({"error": code, "detail": detail}, status=status)


@web.middleware
async def write_guard(request, handler):
    """Admin writes (login, rule edits) must be JSON and same-origin.

    On top of the SameSite cookie, this keeps a page on another site from
    logging in or changing the rules through the owner's browser.
    """
    if (request.path.startswith(("/api/notify", "/api/admin", "/api/incidents"))
            and request.method not in ("GET", "HEAD", "OPTIONS")):
        if request.content_type != "application/json":
            return _error(415, "json_required")
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            return _error(403, "cross_origin")
    return await handler(request)


async def _rule_body(request):
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise notify_rules.RuleError("ogiltig JSON")
    return notify_rules.validate(body)


def _rule_id(request):
    try:
        return int(request.match_info["rule_id"])
    except (TypeError, ValueError):
        return None


async def handle_notify(request):
    """GET /api/notify — rules plus the MQTT connection they go out on."""
    import config as cfg
    import rds_guard

    rules = await _run(event_store.list_rules)
    return web.json_response({
        "rules": rules,
        "mqtt": {
            "enabled": bool(cfg.MQTT_ENABLED and cfg.MQTT_HOST),
            "connected": rds_guard.mqtt_connected.is_set(),
            "host": cfg.MQTT_HOST or None,
            "port": cfg.MQTT_PORT,
            "client_id": cfg.MQTT_CLIENT_ID,
            "prefix": cfg.MQTT_TOPIC_PREFIX,
        },
        "web_ui_url": cfg.WEB_UI_URL or None,
        "timezone": cfg.NOTIFY_TIMEZONE,
        "retention_days": cfg.EVENT_RETENTION_DAYS,
    })


async def handle_create_rule(request):
    try:
        rule = await _rule_body(request)
    except notify_rules.RuleError as e:
        return _error(422, "invalid", str(e))
    return web.json_response(await _run(event_store.create_rule, rule), status=201)


async def handle_update_rule(request):
    rule_id = _rule_id(request)
    try:
        rule = await _rule_body(request)
    except notify_rules.RuleError as e:
        return _error(422, "invalid", str(e))
    saved = await _run(event_store.update_rule, rule_id, rule) if rule_id else None
    if saved is None:
        return _error(404, "not_found")
    return web.json_response(saved)


async def handle_delete_rule(request):
    rule_id = _rule_id(request)
    if not rule_id or not await _run(event_store.delete_rule, rule_id):
        return _error(404, "not_found")
    return web.json_response({"deleted": True})


async def handle_preview_rule(request):
    """POST /api/notify/preview — try an (unsaved) rule on the history."""
    import config as cfg
    try:
        rule = await _rule_body(request)
    except notify_rules.RuleError as e:
        return _error(422, "invalid", str(e))

    def work():
        events = event_store.events_for_preview(
            _since_days(cfg.EVENT_RETENTION_DAYS + 1))
        res = notify_rules.preview(rule, events, notifier.zone())
        res["samples"] = [{k: e.get(k) for k in (
            "id", "type", "pi", "station_ps", "radiotext", "started_at",
            "duration_sec")} for e in res["samples"]]
        return res

    return web.json_response(await _run(work))


async def handle_test_rule(request):
    """POST /api/notify/test — send a made-up message for a rule."""
    import rds_guard
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    try:
        rule = notify_rules.validate(body)
    except notify_rules.RuleError as e:
        return _error(422, "invalid", str(e))
    if isinstance(body, dict) and isinstance(body.get("id"), int):
        rule["id"] = body["id"]
    status, topic, payload = await _run(
        notifier.send_test, rule, rds_guard._notify_publish)
    import config as cfg
    return web.json_response({
        "status": status,
        "topic": f"{cfg.MQTT_TOPIC_PREFIX}/{topic}",
        "payload": payload,
    })


async def handle_notify_log(request):
    try:
        limit = max(1, min(100, int(request.query.get("limit", 30))))
    except (TypeError, ValueError):
        limit = 30
    return web.json_response({"log": await _run(event_store.notify_log_recent, limit)})


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def handle_ws_console(request):
    """WS /ws/console — live message stream."""
    import time
    import rds_guard

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    rds_guard.register_ws(ws)
    client_count = len(rds_guard._ws_clients)
    log.info("WebSocket client connected (%d total)", client_count)

    # Send a welcome message so the browser can confirm the connection works
    try:
        await ws.send_str(json.dumps({
            "topic": "system/connected",
            "payload": {"message": "WebSocket connected", "clients": client_count},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        }))
    except Exception:
        log.warning("Failed to send WebSocket welcome message")

    try:
        async for msg in ws:
            # Console is push-only; ignore client messages
            pass
    finally:
        rds_guard.unregister_ws(ws)
        log.info("WebSocket client disconnected (%d remaining)",
                 len(rds_guard._ws_clients))
    return ws


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------

async def handle_index(request):
    """GET / → serve index.html."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return web.FileResponse(index_path)
    return web.Response(text="RDS Guard — static files not found", status=404)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app():
    app = web.Application(middlewares=[write_guard, ADMIN.middleware],
                          client_max_size=64 * 1024)

    # API routes
    app.router.add_get("/api/events", handle_events)
    app.router.add_get("/api/events/active", handle_events_active)
    app.router.add_get("/api/events/{event_id}", handle_event)
    app.router.add_get("/api/calendar", handle_calendar)
    app.router.add_get("/api/incidents", handle_incidents)
    app.router.add_post("/api/incidents/backfill", handle_incidents_backfill)
    app.router.add_get("/api/incidents/{incident_id:\\d+}", handle_incident)
    app.router.add_get("/api/trafikverket", handle_trafikverket)
    ADMIN.add_routes(app.router, "/api")
    app.router.add_get("/api/notify", handle_notify)
    app.router.add_get("/api/notify/log", handle_notify_log)
    app.router.add_post("/api/notify/rules", handle_create_rule)
    app.router.add_put("/api/notify/rules/{rule_id}", handle_update_rule)
    app.router.add_delete("/api/notify/rules/{rule_id}", handle_delete_rule)
    app.router.add_post("/api/notify/preview", handle_preview_rule)
    app.router.add_post("/api/notify/test", handle_test_rule)
    app.router.add_get("/api/status", handle_status)
    app.router.add_delete("/api/events", handle_delete_events)
    app.router.add_get("/api/audio/{filename}", handle_audio)

    # WebSocket
    app.router.add_get("/ws/console", handle_ws_console)

    # Static files
    if STATIC_DIR.exists():
        app.router.add_static("/static/", STATIC_DIR, show_index=False)

    # Root → index.html
    app.router.add_get("/", handle_index)

    return app


# ---------------------------------------------------------------------------
# Server entry point (called from bridge thread)
# ---------------------------------------------------------------------------

def run_server(port, stats_obj, station_info_obj, mqtt_connected_obj, pipeline_status_obj):
    """Start the aiohttp server — runs in its own thread with its own event loop."""
    global _stats, _station_info, _mqtt_connected, _pipeline_status

    _stats = stats_obj
    _station_info = station_info_obj
    _mqtt_connected = mqtt_connected_obj
    _pipeline_status = pipeline_status_obj

    try:
        import rds_guard
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        rds_guard._event_loop = loop

        app = create_app()
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "0.0.0.0", port)
        loop.run_until_complete(site.start())
        log.info("Web server bound to 0.0.0.0:%d", port)
        loop.run_forever()
    except Exception:
        log.exception("Web server failed to start")
