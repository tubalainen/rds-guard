"""Ingest API for satellites — the only part of the aggregator on the internet.

Implements docs/uplink-protocol.md (version 2). Every response, errors
included, carries the protocol header so a satellite can tell the
aggregator's answers apart from those of a proxy or a wrong URL.

Everything a satellite sends is untrusted: it is validated, size-limited
and length-capped here, files are named by the server, and a node can only
touch its own events.
"""

import asyncio
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from . import config, store

log = logging.getLogger("rds-aggregator")

PROTOCOL_VERSION = "2"
PROTOCOL_HEADER = "X-RDS-Guard-Protocol"

EVENT_TYPES = ("traffic", "emergency")
EVENT_STATES = ("start", "update", "active", "end")

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


class Hooks:
    """Called after a successful ingest. The pipeline overrides these."""

    def on_event(self, row, prev_state):
        pass

    def on_audio(self, row):
        pass

    def on_delete(self, row):
        pass

    def on_heartbeat(self, node):
        pass


class ValidationError(Exception):
    pass


HOOKS = web.AppKey("hooks", Hooks)
LIMITER = None  # set below, once RateLimiter is defined
NODE = web.RequestKey("node", dict)


async def _run(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


def _error(status, code, detail=""):
    return web.json_response({"error": code, "detail": detail}, status=status)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def valid_uid(value):
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _opt_str(body, key, max_len, required=False):
    val = body.get(key)
    if val is None:
        if required:
            raise ValidationError(f"'{key}' is required")
        return None
    if not isinstance(val, str):
        raise ValidationError(f"'{key}' must be a string")
    val = val.strip()
    if required and not val:
        raise ValidationError(f"'{key}' must not be empty")
    if len(val) > max_len:
        raise ValidationError(f"'{key}' is longer than {max_len} characters")
    return val or None


def _opt_ts(body, key, required=False):
    val = body.get(key)
    if val is None:
        if required:
            raise ValidationError(f"'{key}' is required")
        return None
    if not isinstance(val, str) or not _TS_RE.match(val):
        raise ValidationError(f"'{key}' must be a UTC timestamp ending in Z")
    return val[:19] + "Z"


def _opt_int(body, key, lo, hi, required=False):
    val = body.get(key)
    if val is None:
        if required:
            raise ValidationError(f"'{key}' is required")
        return None
    if isinstance(val, bool) or not isinstance(val, int) or not lo <= val <= hi:
        raise ValidationError(f"'{key}' must be an integer {lo}–{hi}")
    return val


def validate_event(body):
    """Validate a PUT /events/{uid} body. Returns a clean dict."""
    if not isinstance(body, dict):
        raise ValidationError("body must be a JSON object")
    ev = {
        "rev": _opt_int(body, "rev", 0, 2**62, required=True),
        "type": _opt_str(body, "type", 16, required=True),
        "state": _opt_str(body, "state", 16, required=True),
        "pi": _opt_str(body, "pi", 16, required=True),
        "station_ps": _opt_str(body, "station_ps", 64),
        "long_ps": _opt_str(body, "long_ps", 64),
        "frequency": _opt_str(body, "frequency", 16),
        "prog_type": _opt_str(body, "prog_type", 64),
        "started_at": _opt_ts(body, "started_at"),
        "ended_at": _opt_ts(body, "ended_at"),
        "duration_sec": _opt_int(body, "duration_sec", 0, 86400),
        "sent_at": _opt_ts(body, "sent_at", required=True),
        # Which of the satellite's receivers heard it (protocol 2)
        "receiver": _opt_str(body, "receiver", 32) or "0",
    }
    if ev["type"] not in EVENT_TYPES:
        raise ValidationError(f"'type' must be one of {', '.join(EVENT_TYPES)}")
    if ev["state"] not in EVENT_STATES:
        raise ValidationError(f"'state' must be one of {', '.join(EVENT_STATES)}")

    rt = body.get("radiotext", [])
    if rt is None:
        rt = []
    if not isinstance(rt, list) or len(rt) > 50:
        raise ValidationError("'radiotext' must be a list of at most 50 strings")
    clean_rt = []
    for item in rt:
        if not isinstance(item, str) or len(item) > 128:
            raise ValidationError("'radiotext' items must be strings of at "
                                  "most 128 characters")
        clean_rt.append(item.strip())
    ev["radiotext"] = clean_rt
    return ev


def _cap(val, max_len):
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val
    return str(val)[:max_len]


def _num(val):
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return val


# Stations per receiver: 4 on a dongle, 8 on an Airspy at a wide sample
# rate; per node, up to 8 receivers' worth
STATIONS_PER_RECEIVER = 8
STATIONS_PER_NODE = 32


def _clean_stations(stations, limit=STATIONS_PER_NODE):
    out = []
    for st in (stations if isinstance(stations, list) else [])[:limit]:
        if not isinstance(st, dict):
            continue
        out.append({
            "frequency": _cap(st.get("frequency"), 16),
            "pi": _cap(st.get("pi"), 16),
            "ps": _cap(st.get("ps"), 64),
            "long_ps": _cap(st.get("long_ps"), 64),
            "prog_type": _cap(st.get("prog_type"), 64),
            "tp": st.get("tp") if isinstance(st.get("tp"), bool) else None,
            "ta": st.get("ta") if isinstance(st.get("ta"), bool) else None,
            "groups_per_sec": _num(st.get("groups_per_sec")),
            "groups_total": _num(st.get("groups_total")),
        })
    return out


def _clean_receivers(receivers):
    """The satellite's receivers (one per RTL-SDR or Airspy, protocol 2)."""
    out = []
    for r in (receivers if isinstance(receivers, list) else [])[:8]:
        if not isinstance(r, dict):
            continue
        quality = r.get("quality") if isinstance(r.get("quality"), dict) else {}
        out.append({
            "id": _cap(r.get("id"), 32),
            "serial": _cap(r.get("serial"), 32),
            "name": _cap(r.get("name"), 32),
            "state": _cap(r.get("state"), 32),
            "error": _cap(r.get("error"), 256),
            "gain": _num(r.get("gain")),
            "gain_mode": _cap(r.get("gain_mode"), 16),
            "ppm": _num(r.get("ppm")),
            "ppm_mode": _cap(r.get("ppm_mode"), 16),
            "calibrated_at": _cap(r.get("calibrated_at"), 32),
            "calibration": _cap(r.get("calibration"), 64),
            "quality": {_cap(k, 16): _num(v) for k, v in list(quality.items())[:8]},
            "stations": _clean_stations(r.get("stations"), STATIONS_PER_RECEIVER),
        })
    return out


def clean_heartbeat(body):
    """Reduce a heartbeat body to known, length-capped fields."""
    if not isinstance(body, dict):
        raise ValidationError("body must be a JSON object")
    pipe = body.get("pipeline") if isinstance(body.get("pipeline"), dict) else {}
    clean_stations = _clean_stations(body.get("stations"))
    sent_at = body.get("sent_at")
    if not isinstance(sent_at, str) or not _TS_RE.match(sent_at):
        sent_at = None
    name = body.get("node_name")
    version = body.get("version")
    return {
        "node_name": str(name)[:64].strip() if isinstance(name, str) else None,
        "version": str(version)[:64].strip() if isinstance(version, str) else None,
        "sent_at": sent_at,
        "status": {
            "uptime_sec": _num(body.get("uptime_sec")),
            "pipeline": {
                "state": _cap(pipe.get("state"), 32),
                "error": _cap(pipe.get("error"), 256),
            },
            "stations": clean_stations,
            "receivers": _clean_receivers(body.get("receivers")),
            "queue_depth": _num(body.get("queue_depth")),
            # "Ort, Län" the satellite owner gave (NODE_LOCATION); the
            # owner of the aggregator can set another under Admin -> Noder
            "location": _cap(body.get("suggested_location")
                             or body.get("location"), 80),
            "local_processing": (body.get("local_processing")
                                 if isinstance(body.get("local_processing"), bool)
                                 else None),
        },
    }


def clock_offset(sent_at):
    """Seconds the satellite clock is behind (+) or ahead (−) of ours."""
    if not sent_at:
        return None
    try:
        sent = datetime.strptime(sent_at[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None
    return round(time.time() - sent.timestamp(), 1)


# ---------------------------------------------------------------------------
# Rate limiting (per node, in memory)
# ---------------------------------------------------------------------------

class RateLimiter:
    """Token bucket per key."""

    def __init__(self, per_minute, burst):
        self._rate = per_minute / 60.0
        self._burst = float(burst)
        self._buckets = {}

    def allow(self, key):
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (self._burst, now))
        tokens = min(self._burst, tokens + (now - last) * self._rate)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True


LIMITER = web.AppKey("limiter", RateLimiter)
JOIN_LIMITER = web.AppKey("join_limiter", RateLimiter)


def verify_code(join_id):
    """Short code shown in the satellite's log and in Admin (same formula
    as the satellite's uplink.verify_code)."""
    digest = hashlib.sha256(join_id.encode("utf-8")).digest()
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    chars = [alphabet[b % len(alphabet)] for b in digest[:6]]
    return "".join(chars[:3]) + "-" + "".join(chars[3:])


def client_address(request):
    """The client's address; X-Forwarded-For only from a trusted proxy."""
    peer = request.remote or ""
    if peer in config.TRUSTED_PROXIES:
        # The client may write any X-Forwarded-For it likes; each trusted
        # proxy appends the address it got the request from. Take the last
        # address that is not one of ours.
        hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",")
                if h.strip()]
        for hop in reversed(hops):
            if hop not in config.TRUSTED_PROXIES:
                return hop[:64]
    return peer[:64]


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

@web.middleware
async def protocol_middleware(request, handler):
    """Tag every response and turn aiohttp errors into JSON."""
    try:
        resp = await handler(request)
    except web.HTTPRequestEntityTooLarge:
        resp = _error(413, "too_large", "request body is too large")
    except web.HTTPException as e:
        resp = _error(e.status, e.reason.lower().replace(" ", "_"))
    except Exception:
        log.exception("Ingest handler error")
        resp = _error(500, "internal_error")
    resp.headers[PROTOCOL_HEADER] = PROTOCOL_VERSION
    resp.headers["Cache-Control"] = "no-store"
    return resp


@web.middleware
async def auth_middleware(request, handler):
    """Check protocol version, authenticate the node, apply rate limit."""
    if request.headers.get(PROTOCOL_HEADER) != PROTOCOL_VERSION:
        return _error(426, "unsupported_protocol",
                      f"this aggregator speaks protocol {PROTOCOL_VERSION}")
    if request.path == JOIN_PATH:
        return await handler(request)      # authenticates by itself
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    node = await _run(store.authenticate, token) if token else None
    if node is None:
        return _error(401, "unauthorized", "missing or unknown node token")
    if not node["enabled"]:
        return _error(403, "node_disabled",
                      "this node has been disabled by the aggregator's owner")
    if not request.app[LIMITER].allow(node["id"]):
        return _error(429, "rate_limited", "too many requests")
    request[NODE] = node
    return await handler(request)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _uid(request):
    uid = request.match_info["uid"]
    if not valid_uid(uid):
        raise ValidationError("event id must be a lowercase UUID")
    return uid


async def _read_json(request):
    if request.content_length is not None and request.content_length > config.MAX_JSON_BYTES:
        raise web.HTTPRequestEntityTooLarge(
            max_size=config.MAX_JSON_BYTES, actual_size=request.content_length)
    raw = await request.content.read(config.MAX_JSON_BYTES + 1)
    if len(raw) > config.MAX_JSON_BYTES:
        raise web.HTTPRequestEntityTooLarge(
            max_size=config.MAX_JSON_BYTES, actual_size=len(raw))
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValidationError("body is not valid JSON")


async def handle_put_event(request):
    node = request[NODE]
    try:
        uid = _uid(request)
        ev = validate_event(await _read_json(request))
    except ValidationError as e:
        return _error(422, "invalid", str(e))

    result, prev_state, row = await _run(
        store.upsert_event, node["id"], uid, ev, config.MATCH_WINDOW_SEC,
        config.BROADCAST_END_GRACE_SEC)
    await _run(store.touch_node, node["id"])
    if result == store.CONFLICT:
        return _error(409, "conflict", "event id belongs to another node")
    if result == store.STALE:
        return web.json_response({"status": "stale"})
    request.app[HOOKS].on_event(row, prev_state)
    return web.json_response({"status": "stored"})


def _write_audio(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)


async def handle_put_audio(request):
    node = request[NODE]
    try:
        uid = _uid(request)
    except ValidationError as e:
        return _error(422, "invalid", str(e))
    rec = await _run(store.get_reception, uid)
    if rec is None or rec["node_id"] != node["id"]:
        return _error(404, "not_found", "no such event for this node")

    if request.content_length is not None and request.content_length > config.MAX_AUDIO_BYTES:
        return _error(413, "too_large", "audio file is too large")
    body = await request.content.read(config.MAX_AUDIO_BYTES + 1)
    if len(body) > config.MAX_AUDIO_BYTES:
        return _error(413, "too_large", "audio file is too large")
    if not body.startswith(b"OggS"):
        return _error(415, "unsupported_media", "audio must be OGG/Opus")

    filename = f"{uid}.ogg"   # named by the server, never by the client
    await _run(_write_audio, Path(config.AUDIO_DIR) / filename, body)
    row = await _run(store.set_event_audio, node["id"], uid, filename, None,
                     len(body))
    await _run(store.touch_node, node["id"])
    if row is None:   # deleted while we were writing
        await _run(_remove_audio, filename)
        return _error(404, "not_found", "no such event for this node")
    request.app[HOOKS].on_audio(row)
    return web.json_response({"status": "stored"})


def _remove_audio(filename):
    if not filename:
        return
    try:
        (Path(config.AUDIO_DIR) / Path(filename).name).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("Could not remove audio %s: %s", filename, e)


async def handle_delete_event(request):
    node = request[NODE]
    try:
        uid = _uid(request)
    except ValidationError as e:
        return _error(422, "invalid", str(e))
    res = await _run(store.delete_event, node["id"], uid)
    await _run(store.touch_node, node["id"])
    if res is None:
        return _error(404, "not_found", "no such event for this node")
    rec, before, after = res
    await _run(_remove_audio, rec.get("audio_path"))
    if after is None:
        request.app[HOOKS].on_delete(before)     # the broadcast went with it
    else:
        request.app[HOOKS].on_event(after, before["state"])
    return web.json_response({"status": "deleted"})


async def handle_heartbeat(request):
    node = request[NODE]
    try:
        hb = clean_heartbeat(await _read_json(request))
    except ValidationError as e:
        return _error(422, "invalid", str(e))
    await _run(store.record_heartbeat, node["id"], hb["node_name"],
               hb["version"], hb["status"], clock_offset(hb["sent_at"]))
    request.app[HOOKS].on_heartbeat(await _run(store.get_node, node["id"]))
    answer = {"server_time": store.now_iso()}
    commands = await _run(store.take_node_commands, node["id"])
    if commands:
        answer["commands"] = commands
    return web.json_response(answer)


JOIN_PATH = "/api/v1/ingest/join"


def _bearer(request):
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth[:7].lower() == "bearer " else ""


async def handle_join(request):
    """POST /join — a satellite asks to become a node, with a token it made
    itself. Pending until the owner approves it (Admin -> Noder or the
    nodes CLI); a matching JOIN_KEY approves at once. Nothing secret is
    sent back, so the satellite simply asks again until approved."""
    address = client_address(request)
    limiter = request.app[JOIN_LIMITER]
    if not limiter.allow(f"ip:{address}") or not limiter.allow("all"):
        return _error(429, "rate_limited", "too many join requests")
    token = _bearer(request)
    if len(token) < 32 or len(token) > 256:
        return _error(401, "unauthorized", "a join needs the satellite's own "
                                           "token (at least 32 characters)")
    try:
        body = await _read_json(request)
        if not isinstance(body, dict):
            raise ValidationError("body must be a JSON object")
        join_id = body.get("join_id")
        if not valid_uid(join_id):
            raise ValidationError("'join_id' must be a lowercase UUID")
        info = {
            "node_name": _opt_str(body, "node_name", 64),
            "version": _opt_str(body, "version", 64),
            "suggested_location": _opt_str(body, "suggested_location", 80),
            "stations": [
                {"frequency": _cap(st.get("frequency"), 16), "pi": _cap(st.get("pi"), 16),
                 "ps": _cap(st.get("ps"), 64)}
                for st in (body.get("stations") or [])[:STATIONS_PER_NODE]
                if isinstance(st, dict)],
        }
        join_key = body.get("join_key")
        if join_key is not None and not isinstance(join_key, str):
            raise ValidationError("'join_key' must be a string")
    except ValidationError as e:
        return _error(422, "invalid", str(e))

    node = await _run(store.authenticate, token)
    if node is not None:
        if not node["enabled"]:
            return _error(403, "node_disabled",
                          "this node has been disabled by the aggregator's owner")
        return web.json_response({"status": "approved", "node": node["name"]})

    if join_key:
        if not config.JOIN_KEY or not hmac.compare_digest(
                join_key.encode("utf-8"), config.JOIN_KEY.encode("utf-8")):
            return _error(403, "rejected", "join refused")
    verify = verify_code(join_id)
    status, req = await _run(store.record_join_request, token, join_id, verify,
                             info, address, config.JOIN_MAX_PENDING)
    if status == "conflict":
        return _error(409, "conflict", "this join id belongs to another token")
    if status == "full":
        return _error(429, "too_many_pending", "too many satellites are waiting "
                                               "for approval")
    if status == store.JOIN_REJECTED:
        return _error(403, "rejected", "join refused")
    if join_key:   # matches JOIN_KEY: approve at once
        name = await _run(store.free_name, info["node_name"] or f"Nod {verify}")
        node = await _run(store.approve_join, req["id"], name,
                          info["suggested_location"])
        return web.json_response({"status": "approved", "node": node["name"]})
    return web.json_response({"status": "pending", "verify": verify}, status=202)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_ingest_app(hooks=None, limiter=None):
    # Body limit for aiohttp's own reader; handlers enforce the real limits.
    app = web.Application(
        client_max_size=config.MAX_AUDIO_BYTES + 64 * 1024,
        middlewares=[protocol_middleware, auth_middleware],
    )
    app[HOOKS] = hooks or Hooks()
    app[LIMITER] = limiter or RateLimiter(config.INGEST_RATE_PER_MIN,
                                          config.INGEST_BURST)
    app[JOIN_LIMITER] = RateLimiter(config.JOIN_RATE_PER_MIN, config.JOIN_RATE_PER_MIN)
    base = "/api/v1/ingest"
    uid = "{uid}"
    app.router.add_put(f"{base}/events/{uid}", handle_put_event)
    app.router.add_put(f"{base}/events/{uid}/audio", handle_put_audio)
    app.router.add_delete(f"{base}/events/{uid}", handle_delete_event)
    app.router.add_post(f"{base}/heartbeat", handle_heartbeat)
    app.router.add_post(JOIN_PATH, handle_join)
    return app
