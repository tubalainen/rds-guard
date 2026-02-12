"""Web server for RDS Guard — REST API + WebSocket + static files.

Uses aiohttp to serve:
  GET  /              → static web UI (index.html)
  GET  /api/events    → query events from SQLite
  GET  /api/events/active → in-progress announcements
  GET  /api/status    → decoder status from memory
  DELETE /api/events  → clear all events
  WS   /ws/console    → live decoded message stream
"""

import asyncio
import json
import logging
import pathlib

from aiohttp import web

import event_store

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

async def handle_events(request):
    """GET /api/events — query events with optional filters."""
    event_type = request.query.get("type")
    since = request.query.get("since")
    try:
        limit = min(int(request.query.get("limit", 50)), 200)
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = int(request.query.get("offset", 0))
    except (ValueError, TypeError):
        offset = 0

    rows, total = await asyncio.get_event_loop().run_in_executor(
        None, lambda: event_store.query_events(event_type, since, limit, offset)
    )

    # Parse radiotext JSON strings back to arrays for the response
    for row in rows:
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
        # Add audio URL if audio_path exists
        if row.get("audio_path"):
            row["audio_url"] = f"/api/audio/{row['audio_path']}"

    return web.json_response({"events": rows, "total": total})


async def handle_events_active(request):
    """GET /api/events/active — in-progress announcements."""
    rows = await asyncio.get_event_loop().run_in_executor(
        None, event_store.get_active_events
    )
    for row in rows:
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
        if row.get("audio_path"):
            row["audio_url"] = f"/api/audio/{row['audio_path']}"

    return web.json_response({"events": rows})


async def handle_status(request):
    """GET /api/status — decoder status."""
    import time
    snap = _stats.snapshot()
    snap["mqtt_connected"] = _mqtt_connected.is_set() if _mqtt_connected else False

    import config as cfg
    snap["frequency"] = cfg.FM_FREQUENCY

    si = _station_info.snapshot()
    if si:
        primary = next(iter(si.values()))
        snap["station"] = primary
    else:
        snap["station"] = None

    if _pipeline_status:
        snap["pipeline"] = _pipeline_status.snapshot()
    else:
        snap["pipeline"] = {"state": "unknown"}

    snap["version"] = cfg.BUILD_VERSION
    snap["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    return web.json_response(snap)


async def handle_delete_events(request):
    """DELETE /api/events — clear all events."""
    count = await asyncio.get_event_loop().run_in_executor(
        None, event_store.delete_all_events
    )
    return web.json_response({"deleted": count})


async def handle_audio(request):
    """GET /api/audio/{filename} — serve audio file."""
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

    return web.FileResponse(file_path, headers={
        "Content-Type": content_type,
        "Cache-Control": "public, max-age=86400",
    })


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
    app = web.Application()

    # API routes
    app.router.add_get("/api/events", handle_events)
    app.router.add_get("/api/events/active", handle_events_active)
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
