#!/usr/bin/env python3
"""RDS Guard — RDS decoder bridge with event store and web UI.

Supervisor process that manages:
  1. Web server (aiohttp) — REST API + WebSocket + static files
  2. MQTT client (optional, non-blocking background connection)
  3. Radio pipeline (rtl_fm | redsea as subprocesses via pipeline.py)

Each component starts independently — the web UI is available immediately,
regardless of whether the radio pipeline or MQTT broker are working.
"""

import asyncio
import json
import logging
import signal
import threading
import time

import config
import event_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("rds-guard")

# ---------------------------------------------------------------------------
# WebSocket broadcast hub — all connected console clients
# ---------------------------------------------------------------------------

_ws_clients = set()
_ws_lock = threading.Lock()
_event_loop = None  # set by web server on start
_ws_msg_count = 0


def register_ws(ws):
    with _ws_lock:
        _ws_clients.add(ws)


def unregister_ws(ws):
    with _ws_lock:
        _ws_clients.discard(ws)


def broadcast_ws(message):
    """Send a message dict to all WebSocket console clients (non-blocking)."""
    global _ws_msg_count
    if not _ws_clients or _event_loop is None:
        return
    text = json.dumps(message)
    with _ws_lock:
        clients = set(_ws_clients)
    for ws in clients:
        try:
            asyncio.run_coroutine_threadsafe(ws.send_str(text), _event_loop)
        except Exception:
            log.debug("Failed to send WS message to client: %s", ws)
    _ws_msg_count += 1
    if _ws_msg_count == 1:
        log.info("WebSocket: first message sent to %d client(s)", len(clients))
    elif _ws_msg_count % 500 == 0:
        log.info("WebSocket: %d messages sent so far to %d client(s)",
                 _ws_msg_count, len(clients))


# ---------------------------------------------------------------------------
# State tracking for deduplication
# ---------------------------------------------------------------------------

class StationState:
    """Tracks last-published values per PI code to suppress duplicates."""

    def __init__(self):
        self._state = {}

    def changed(self, pi, key, value):
        full_key = f"{pi}/{key}"
        if self._state.get(full_key) == value:
            return False
        self._state[full_key] = value
        return True

    def is_known(self, pi, key):
        """Return True if a value has been recorded for this key before."""
        return f"{pi}/{key}" in self._state


state = StationState()

# ---------------------------------------------------------------------------
# Station info accumulator — latest decoded values for status messages
# ---------------------------------------------------------------------------

class StationInfo:
    """Accumulates the latest decoded RDS fields from the stream."""

    def __init__(self):
        self.lock = threading.Lock()
        self._info = {}
        self._identified = set()   # PI codes we've already logged as "locked on"
        self._ps_logged = set()    # PI codes whose PS name we've logged

    def update(self, pi, data):
        with self.lock:
            new_pi = pi not in self._info
            if new_pi:
                self._info[pi] = {}
            info = self._info[pi]
            info["pi"] = pi

            had_ps = "ps" in info
            if "ps" in data:
                info["ps"] = data["ps"].strip()
            elif "partial_ps" in data and "ps" not in info:
                info["ps"] = data["partial_ps"].strip()
            if "long_ps" in data:
                info["long_ps"] = data["long_ps"].strip()
            if "prog_type" in data:
                info["prog_type"] = data["prog_type"]
            if "tp" in data:
                info["tp"] = data["tp"]
            if "ta" in data:
                info["ta"] = data["ta"]
            if "is_music" in data:
                info["is_music"] = data["is_music"]
            if "di" in data:
                info["di"] = data["di"]
            if "country" in data:
                info["country"] = data["country"]
            if "language" in data:
                info["language"] = data["language"]
            if "radiotext" in data:
                info["radiotext"] = data["radiotext"].strip()
            for af_key in ("alt_frequencies_a", "alt_frequencies_b"):
                if af_key in data:
                    info["alt_frequencies"] = data[af_key]
            if "radiotext_plus" in data:
                rtp = data["radiotext_plus"]
                for tag in rtp.get("tags", []):
                    ct = tag.get("content-type", "")
                    if ct == "item.title":
                        info["now_title"] = tag["data"]
                    elif ct == "item.artist":
                        info["now_artist"] = tag["data"]

            # --- Station identification logging ---
            if new_pi and pi not in self._identified:
                self._identified.add(pi)
                log.info("╔══ New PI code detected: %s on %s", pi, config.FM_FREQUENCY)

            # Log when PS (station name) first resolves for this PI
            if not had_ps and "ps" in info and pi not in self._ps_logged:
                self._ps_logged.add(pi)
                ps_name = info["ps"]
                parts = [f"Station: {ps_name} (PI: {pi})"]
                if "prog_type" in info:
                    parts.append(f"PTY: {info['prog_type']}")
                if "tp" in info:
                    parts.append(f"TP: {'yes' if info['tp'] else 'no'}")
                if "country" in info:
                    parts.append(f"Country: {info['country']}")
                log.info("╔══════════════════════════════════════════")
                log.info("║ Locked on to: %s", " | ".join(parts))
                log.info("╚══════════════════════════════════════════")

    def snapshot(self):
        with self.lock:
            return {pi: dict(info) for pi, info in self._info.items()}

    def primary_summary(self):
        """Return a short summary string of the primary station for log messages."""
        with self.lock:
            if not self._info:
                return None
            primary = next(iter(self._info.values()))
            parts = []
            ps = primary.get("ps")
            if ps:
                parts.append(ps)
            else:
                parts.append(f"PI:{primary.get('pi', '?')}")
            pty = primary.get("prog_type")
            if pty:
                parts.append(pty)
            rt = primary.get("radiotext")
            if rt:
                parts.append(f'RT: "{rt[:60]}"')
            elif primary.get("now_artist") and primary.get("now_title"):
                parts.append(f'Now: {primary["now_artist"]} - {primary["now_title"]}')
            return " | ".join(parts)


station_info = StationInfo()

# ---------------------------------------------------------------------------
# Rules Engine — replaces AlertTracker, writes to SQLite + MQTT + WebSocket
# ---------------------------------------------------------------------------

ALERT_PTY = {"Alarm", "Alarm - Loss of radio"}


class RulesEngine:
    """Evaluates RDS data against event rules and writes to all outputs.

    Outputs: SQLite (event_store), MQTT (if connected), WebSocket broadcast.

    Rules (hardcoded — defined by RDS standard):
      1. TA flag → true:  traffic event (start)
      2. TA flag → false: traffic event (end, with collected RT + duration)
      3. RT change during active TA: update existing traffic event
      4. PTY → Alarm type: emergency event
      5. EON TA (group 14A): eon_traffic event
    """

    def __init__(self, recorder=None, record_event_types=None):
        self.lock = threading.Lock()
        # Per-PI active traffic announcement: pi -> {event_id, since, radiotext, prog_type}
        self._active = {}
        # Per-PI active emergency: pi -> {event_id, since}
        self._active_emergency = {}
        # Audio recorder (may be None if not initialized yet)
        self._recorder = recorder
        self._record_types = set(
            t.strip() for t in (record_event_types or "traffic,emergency").split(",")
        )

    def _station_context(self, pi):
        si = station_info.snapshot()
        info = si.get(pi, {})
        ctx = {"pi": pi}
        for key in ("ps", "long_ps", "prog_type", "country"):
            if key in info:
                ctx[key] = info[key]
        return ctx

    def on_ta_change(self, mqtt_client, pi, ta, data):
        """TA flag changed state."""
        ts = msg_ts(data)
        ctx = self._station_context(pi)
        ps = ctx.get("ps")
        freq = config.FM_FREQUENCY

        with self.lock:
            if ta:
                # --- Traffic announcement START ---
                payload = {
                    "type": "traffic",
                    "state": "start",
                    "station": ctx,
                    "frequency": freq,
                    "prog_type": data.get("prog_type", ""),
                    "timestamp": ts,
                }
                event_id = event_store.insert_event(
                    event_type="traffic",
                    severity="warning",
                    state="start",
                    pi=pi,
                    data_payload=payload,
                    station_ps=ps,
                    frequency=freq,
                    started_at=ts,
                )
                payload["event_id"] = event_id
                self._active[pi] = {
                    "event_id": event_id,
                    "since": ts,
                    "radiotext": [],
                    "prog_type": data.get("prog_type", ""),
                }

                # Start audio recording
                if self._recorder and "traffic" in self._record_types:
                    self._recorder.start(event_id)
                    event_store.update_event_transcription_status(
                        event_id, "recording")

                log.info("EVENT traffic start on %s (event #%d)", pi, event_id)
                _mqtt_pub(mqtt_client, "alert", payload)
                broadcast_ws({"topic": "alert", "payload": payload, "timestamp": ts})

            else:
                # --- Traffic announcement END ---
                ann = self._active.pop(pi, {})
                event_id = ann.get("event_id")

                # Stop audio recording
                has_audio = False
                if self._recorder and event_id:
                    has_audio = self._recorder.stop()
                    if has_audio:
                        event_store.update_event_transcription_status(
                            event_id, "saving")

                duration = self._duration(ann.get("since"), ts)
                payload = {
                    "type": "traffic",
                    "state": "end",
                    "station": ctx,
                    "frequency": freq,
                    "started": ann.get("since", ""),
                    "ended": ts,
                    "duration_sec": duration,
                    "radiotext": ann.get("radiotext", []),
                    "prog_type": data.get("prog_type", ""),
                    "audio_available": has_audio,
                    "transcription_status": "saving" if has_audio else "none",
                    "event_id": event_id,
                    "timestamp": ts,
                }
                if event_id:
                    event_store.end_event(
                        event_id=event_id,
                        ended_at=ts,
                        duration_sec=duration,
                        radiotext_list=ann.get("radiotext", []),
                        data_payload=payload,
                    )
                log.info("EVENT traffic end on %s (%d RT messages)",
                         pi, len(payload["radiotext"]))
                _mqtt_pub(mqtt_client, "alert", payload)
                broadcast_ws({"topic": "alert", "payload": payload, "timestamp": ts})

    def on_radiotext(self, mqtt_client, pi, rt, data):
        """RadioText changed during active TA — update event."""
        ts = msg_ts(data)
        with self.lock:
            if pi not in self._active:
                return
            collected = self._active[pi]["radiotext"]
            if not collected or collected[-1] != rt:
                collected.append(rt)
            event_id = self._active[pi]["event_id"]
            event_store.update_event_radiotext(event_id, list(collected))
            payload = {
                "type": "traffic",
                "state": "update",
                "station": self._station_context(pi),
                "frequency": config.FM_FREQUENCY,
                "radiotext": rt,
                "all_radiotext": list(collected),
                "started": self._active[pi]["since"],
                "timestamp": ts,
            }
            log.info("EVENT traffic update on %s: %s", pi, rt[:80])
            _mqtt_pub(mqtt_client, "alert", payload)
            broadcast_ws({"topic": "alert", "payload": payload, "timestamp": ts})

    def on_pty_alert(self, mqtt_client, pi, pty, data):
        """PTY changed to an alarm type."""
        ts = msg_ts(data)
        ctx = self._station_context(pi)
        payload = {
            "type": "emergency",
            "state": "active",
            "station": ctx,
            "frequency": config.FM_FREQUENCY,
            "prog_type": pty,
            "timestamp": ts,
        }
        event_id = event_store.insert_event(
            event_type="emergency",
            severity="critical",
            state="active",
            pi=pi,
            data_payload=payload,
            station_ps=ctx.get("ps"),
            frequency=config.FM_FREQUENCY,
            started_at=ts,
        )
        payload["event_id"] = event_id

        # Track active emergency for end detection
        with self.lock:
            self._active_emergency[pi] = {
                "event_id": event_id,
                "since": ts,
            }

        # Start audio recording
        if self._recorder and "emergency" in self._record_types:
            self._recorder.start(event_id)
            event_store.update_event_transcription_status(
                event_id, "recording")

        log.warning("EVENT emergency PTY alarm on %s: %s", pi, pty)
        _mqtt_pub(mqtt_client, "alert", payload)
        broadcast_ws({"topic": "alert", "payload": payload, "timestamp": ts})

    def on_pty_normal(self, mqtt_client, pi, pty, data):
        """PTY changed away from alarm — end emergency recording."""
        ts = msg_ts(data)
        with self.lock:
            em = self._active_emergency.pop(pi, None)
        if not em:
            return

        event_id = em["event_id"]

        # Stop audio recording
        has_audio = False
        if self._recorder:
            has_audio = self._recorder.stop()
            if has_audio:
                event_store.update_event_transcription_status(
                    event_id, "saving")

        duration = self._duration(em.get("since"), ts)
        event_store.end_event(
            event_id=event_id,
            ended_at=ts,
            duration_sec=duration,
        )

        ctx = self._station_context(pi)
        payload = {
            "type": "emergency",
            "state": "end",
            "station": ctx,
            "frequency": config.FM_FREQUENCY,
            "started": em.get("since", ""),
            "ended": ts,
            "duration_sec": duration,
            "audio_available": has_audio,
            "transcription_status": "saving" if has_audio else "none",
            "event_id": event_id,
            "timestamp": ts,
        }
        log.info("EVENT emergency end on %s (PTY → %s)", pi, pty)
        _mqtt_pub(mqtt_client, "alert", payload)
        broadcast_ws({"topic": "alert", "payload": payload, "timestamp": ts})

    def is_emergency_active(self, pi):
        """Check if an emergency broadcast is active for the given PI."""
        with self.lock:
            return pi in self._active_emergency

    def on_eon_ta(self, mqtt_client, pi, other_pi, ta, data):
        """Linked station TA via EON.

        EON TA is informational only — indicates a linked station on a
        different frequency has an active TA.  Published to MQTT / WS for
        Home Assistant but NOT stored in the database (no audio or
        transcription is available for another frequency).
        """
        ts = msg_ts(data)
        on = data.get("other_network", {})
        ctx = self._station_context(pi)
        payload = {
            "type": "eon_traffic",
            "state": "received",
            "ta_active": ta,
            "station": ctx,
            "linked_station": {
                "pi": other_pi,
                "ps": (on.get("ps") or "").strip(),
                "kilohertz": on.get("kilohertz"),
            },
            "frequency": config.FM_FREQUENCY,
            "timestamp": ts,
        }
        label = "active" if ta else "ended"
        log.info("EON traffic %s on linked %s via %s", label, other_pi, pi)
        _mqtt_pub(mqtt_client, "alert", payload)
        broadcast_ws({"topic": "alert", "payload": payload, "timestamp": ts})

    def is_active(self, pi):
        with self.lock:
            return pi in self._active

    @staticmethod
    def _duration(start, end):
        try:
            fmt = "%Y-%m-%dT%H:%M:%S"
            s = time.strptime(start[:19], fmt)
            e = time.strptime(end[:19], fmt)
            return max(0, int(time.mktime(e) - time.mktime(s)))
        except Exception:
            return 0


rules_engine = RulesEngine()


# ---------------------------------------------------------------------------
# Transcription completion callback — called from transcriber thread
# ---------------------------------------------------------------------------

def _on_transcription_complete(event_id, text, error, duration_sec=None):
    """Handle transcription result — update DB, publish MQTT + WS."""
    if error:
        event_store.update_event_transcription(event_id, None, status="error")
        payload = {
            "type": "transcription_update",
            "event_id": event_id,
            "transcription_status": "error",
            "transcription_error": str(error),
        }
        _mqtt_pub(mqtt_client, "alert", {
            "state": "transcription_failed",
            "event_id": event_id,
            "transcription_status": "error",
            "transcription_error": str(error),
            "audio_available": True,
            "timestamp": now_iso(),
        })
        broadcast_ws({"topic": "transcription_error", "event_id": event_id,
                       "error": str(error), "timestamp": now_iso()})
        return

    event_store.update_event_transcription(event_id, text, status="done",
                                           duration_sec=duration_sec)

    # Fetch the full event for the MQTT payload
    try:
        rows, _ = event_store.query_events(limit=1, offset=0)
        event = None
        for r in rows:
            if r.get("id") == event_id:
                event = r
                break
        if not event:
            # Direct lookup
            import sqlite3
            conn = event_store._conn()
            row = conn.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            event = dict(row) if row else {}
    except Exception:
        event = {}

    ts = now_iso()
    pi = event.get("pi", "")
    evt_type = event.get("type", "traffic")

    # Parse station context from stored data
    station = {"pi": pi}
    if event.get("station_ps"):
        station["ps"] = event["station_ps"]

    import json as _json
    radiotext = event.get("radiotext", "[]")
    if isinstance(radiotext, str):
        try:
            radiotext = _json.loads(radiotext)
        except Exception:
            radiotext = []

    # Publish to rds/alert with state=transcribed
    alert_payload = {
        "type": evt_type,
        "state": "transcribed",
        "event_id": event_id,
        "station": station,
        "frequency": event.get("frequency", ""),
        "started": event.get("started_at", ""),
        "ended": event.get("ended_at", ""),
        "duration_sec": event.get("duration_sec"),
        "radiotext": radiotext,
        "transcription": text,
        "transcription_status": "done",
        "transcription_duration_sec": duration_sec,
        "audio_available": True,
        "timestamp": ts,
    }
    _mqtt_pub(mqtt_client, "alert", alert_payload)

    # Publish to rds/{pi}/{type}/transcription (retained)
    if pi:
        transcription_payload = {
            "event_id": event_id,
            "station": station,
            "transcription": text,
            "language": config.TRANSCRIPTION_LANGUAGE,
            "duration_sec": event.get("duration_sec"),
            "radiotext": radiotext,
            "timestamp": ts,
        }
        pub(mqtt_client, f"{pi}/{evt_type}/transcription",
            transcription_payload, qos=1, retain=True)

    # WebSocket broadcast
    broadcast_ws({
        "topic": "transcription",
        "event_id": event_id,
        "transcription": text,
        "transcription_status": "done",
        "transcription_duration_sec": duration_sec,
        "timestamp": ts,
    })

# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

class Stats:
    def __init__(self):
        self.start_time = time.time()
        self.groups_total = 0
        self.lock = threading.Lock()

    def inc(self):
        with self.lock:
            self.groups_total += 1

    def snapshot(self):
        with self.lock:
            elapsed = time.time() - self.start_time
            gps = self.groups_total / elapsed if elapsed > 0 else 0.0
            return {
                "uptime_sec": int(elapsed),
                "groups_total": self.groups_total,
                "groups_per_sec": round(gps, 1),
            }


stats = Stats()

# ---------------------------------------------------------------------------
# MQTT setup (optional — only if MQTT_HOST is configured)
# ---------------------------------------------------------------------------

mqtt_connected = threading.Event()
_mqtt_available = False


def _mqtt_pub(client, topic, payload):
    """Publish to MQTT if available.

    Takes a local client reference to avoid race conditions with the
    background MQTT thread setting the module-level mqtt_client.
    """
    c = client  # snapshot the reference
    if not _mqtt_available or c is None:
        return
    try:
        full_topic = f"{config.MQTT_TOPIC_PREFIX}/{topic}"
        msg = json.dumps(payload)
        c.publish(full_topic, msg, qos=1, retain=False)
        log.debug("PUB %s %s", full_topic, msg[:120])
    except Exception:
        pass  # client may have disconnected during shutdown


def pub(client, topic, payload, qos=None, retain=False):
    """Publish a JSON payload to an MQTT topic (for regular topic publishing).

    Takes a local client reference to avoid race conditions.
    """
    c = client  # snapshot the reference
    if not _mqtt_available or c is None:
        return
    if qos is None:
        qos = config.MQTT_QOS
    try:
        full_topic = f"{config.MQTT_TOPIC_PREFIX}/{topic}"
        msg = json.dumps(payload)
        c.publish(full_topic, msg, qos=qos, retain=retain)
        log.debug("PUB %s %s", full_topic, msg[:120])
    except Exception:
        pass  # client may have disconnected during shutdown


def create_mqtt_client():
    import paho.mqtt.client as mqtt

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("MQTT connected to %s:%s (client_id=%s)",
                     config.MQTT_HOST, config.MQTT_PORT, config.MQTT_CLIENT_ID)
            mqtt_connected.set()
        else:
            log.error("MQTT connection refused by broker: reason_code=%s "
                      "(check credentials and broker config)", reason_code)
            mqtt_connected.clear()

    def on_disconnect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("MQTT disconnected cleanly")
        else:
            log.warning("MQTT lost connection (reason_code=%s) — reconnecting...", reason_code)
        mqtt_connected.clear()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=config.MQTT_CLIENT_ID,
        protocol=mqtt.MQTTv311,
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    if config.MQTT_USER:
        client.username_pw_set(config.MQTT_USER, config.MQTT_PASSWORD)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def msg_ts(data):
    return data.get("timestamp", now_iso())


# ---------------------------------------------------------------------------
# Group handlers — extended topic publishers (used in "all" mode)
# ---------------------------------------------------------------------------

def handle_pin(client, pi, data):
    ts = msg_ts(data)
    payload = {"timestamp": ts}
    if "prog_item_number" in data:
        payload["prog_item_number"] = data["prog_item_number"]
    if "prog_item_started" in data:
        payload["prog_item_started"] = data["prog_item_started"]
    if len(payload) > 1 and state.changed(pi, "programme/pin", payload):
        pub(client, f"{pi}/programme/pin", payload, qos=0, retain=False)
    country = data.get("country")
    if country and state.changed(pi, "system/country", country):
        pub(client, f"{pi}/system/country",
            {"country": country, "language": data.get("language", ""),
             "timestamp": ts},
            qos=0, retain=config.MQTT_RETAIN_STATE)


def handle_oda(client, pi, data):
    ts = msg_ts(data)
    oda = data.get("open_data_app")
    if oda and state.changed(pi, "system/oda", oda):
        pub(client, f"{pi}/system/oda", {"open_data_app": oda, "timestamp": ts},
            qos=0, retain=config.MQTT_RETAIN_STATE)


def handle_clock(client, pi, data):
    ts = msg_ts(data)
    ct = data.get("clock_time")
    if ct:
        pub(client, f"{pi}/clock", {"clock_time": ct, "timestamp": ts},
            qos=0, retain=False)


def handle_rt_plus(client, pi, data):
    ts = msg_ts(data)
    rtp = data.get("radiotext_plus")
    if rtp and state.changed(pi, "programme/rt_plus", rtp):
        payload = {
            "item_running": rtp.get("item_running"),
            "tags": rtp.get("tags", []),
            "timestamp": ts,
        }
        pub(client, f"{pi}/programme/rt_plus", payload, qos=1, retain=False)


def handle_eon(client, pi, data):
    ts = msg_ts(data)
    on = data.get("other_network")
    if not on:
        return
    other_pi = on.get("pi", "unknown")
    payload = {
        "pi": other_pi,
        "ps": (on.get("ps") or "").strip(),
        "tp": on.get("tp"),
        "ta": on.get("ta"),
        "kilohertz": on.get("kilohertz"),
        "timestamp": ts,
    }
    eon_key = f"eon/{other_pi}"
    if state.changed(pi, eon_key, payload):
        pub(client, f"{pi}/eon/{other_pi}", payload,
            qos=0, retain=config.MQTT_RETAIN_STATE)
    if on.get("ta") is not None:
        ta_key = f"eon/{other_pi}/ta"
        if state.changed(pi, ta_key, on["ta"]):
            pub(client, f"{pi}/eon/{other_pi}/ta",
                {"active": on["ta"], "timestamp": ts},
                qos=1, retain=config.MQTT_RETAIN_STATE)


def handle_long_ps(client, pi, data):
    ts = msg_ts(data)
    lps = data.get("long_ps")
    if lps and state.changed(pi, "station/long_ps", lps):
        pub(client, f"{pi}/station/long_ps",
            {"long_ps": lps.strip(), "timestamp": ts},
            qos=1, retain=config.MQTT_RETAIN_STATE)


def handle_ert(client, pi, data):
    ts = msg_ts(data)
    ert = data.get("enhanced_radiotext")
    if ert and state.changed(pi, "programme/ert", ert):
        pub(client, f"{pi}/programme/ert",
            {"enhanced_radiotext": ert.strip(), "timestamp": ts},
            qos=1, retain=False)


def handle_bler(client, pi, data):
    ts = msg_ts(data)
    bler = data.get("bler")
    if bler is not None:
        pub(client, f"{pi}/system/bler",
            {"bler_pct": bler, "timestamp": ts},
            qos=0, retain=False)


def handle_pi_topic(client, pi, data):
    ts = msg_ts(data)
    if state.changed(pi, "station/pi", pi):
        pub(client, f"{pi}/station/pi", {"pi": pi, "timestamp": ts},
            qos=1, retain=config.MQTT_RETAIN_STATE)


# ---------------------------------------------------------------------------
# Field-change logger — confirms active decoding in docker logs
# ---------------------------------------------------------------------------

_logged_fields = {}  # pi/key → last logged value


def _log_field_changes(pi, data):
    """Log notable field changes to confirm the decoder is working."""
    # RadioText change — only log complete texts, not partial fragments
    rt = data.get("radiotext")
    if rt:
        rt = rt.strip()
        key = f"{pi}/rt"
        if _logged_fields.get(key) != rt:
            _logged_fields[key] = rt
            log.info("RadioText [%s]: %s", pi, rt)

    # RT+ now playing
    rtp = data.get("radiotext_plus")
    if rtp:
        for tag in rtp.get("tags", []):
            ct = tag.get("content-type", "")
            if ct in ("item.title", "item.artist"):
                tag_key = f"{pi}/rtp/{ct}"
                val = tag.get("data", "")
                if _logged_fields.get(tag_key) != val:
                    _logged_fields[tag_key] = val
                    label = "Title" if ct == "item.title" else "Artist"
                    log.info("RT+ %s [%s]: %s", label, pi, val)

    # PTY change
    pty = data.get("prog_type")
    if pty:
        key = f"{pi}/pty"
        if _logged_fields.get(key) != pty:
            _logged_fields[key] = pty
            log.info("PTY [%s]: %s", pi, pty)

    # Long PS
    lps = data.get("long_ps")
    if lps:
        lps = lps.strip()
        key = f"{pi}/long_ps"
        if _logged_fields.get(key) != lps:
            _logged_fields[key] = lps
            log.info("Long PS [%s]: %s", pi, lps)


# ---------------------------------------------------------------------------
# Main group dispatcher
# ---------------------------------------------------------------------------

def process_group(client, data):
    """Route a single redsea JSON object through rules engine and MQTT topics.

    The rules engine ALWAYS runs (writes to SQLite + WebSocket).
    MQTT topic publishing is controlled by PUBLISH_MODE (essential/all)
    and only runs if MQTT is configured.
    """
    pi = data.get("pi")
    if not pi:
        return

    stats.inc()
    pi = str(pi)

    # Always accumulate decoded fields for the status message
    station_info.update(pi, data)

    # Log notable field changes that confirm active decoding
    _log_field_changes(pi, data)

    group = data.get("group", "")
    ts = msg_ts(data)

    # --- Broadcast raw group to WebSocket console ---
    topic_hint = group.lower() if group else "unknown"
    broadcast_ws({"topic": f"{pi}/{topic_hint}", "payload": data, "timestamp": ts})

    # --- Rules engine: evaluate all rules ---

    # Rule 1-2: Traffic Announcement (TA flag change)
    if "ta" in data and state.changed(pi, "traffic/ta", data["ta"]):
        rules_engine.on_ta_change(client, pi, data["ta"], data)
        # Also publish to MQTT topic
        retain = config.MQTT_RETAIN_STATE
        pub(client, f"{pi}/traffic/ta",
            {"active": data["ta"], "since": ts,
             "prog_type": data.get("prog_type", "")},
            qos=1, retain=retain)

    # TP flag
    if "tp" in data and state.changed(pi, "traffic/tp", data["tp"]):
        pub(client, f"{pi}/traffic/tp", {"tp": data["tp"], "timestamp": ts},
            qos=1, retain=config.MQTT_RETAIN_STATE)

    # Rule 3: RadioText during active TA
    if group in ("2A", "2B"):
        # Complete RadioText — use for events and MQTT
        rt_full = data.get("radiotext")
        # Partial RadioText — publish to MQTT for real-time display, but
        # do NOT use for event tracking (would flood with fragments)
        rt_partial = data.get("partial_radiotext")
        rt = rt_full or rt_partial
        if rt and state.changed(pi, "programme/rt", rt):
            rt_stripped = rt.strip()
            pub(client, f"{pi}/programme/rt",
                {"radiotext": rt_stripped, "partial": rt_full is None, "timestamp": ts},
                qos=1, retain=False)
            # Only update the traffic event with complete RadioText
            if rt_full and rules_engine.is_active(pi):
                rules_engine.on_radiotext(client, pi, rt_stripped, data)

    # Rule 4: PTY alarm
    pty = data.get("prog_type")
    if pty and state.changed(pi, "station/pty", pty):
        pub(client, f"{pi}/station/pty", {"prog_type": pty, "timestamp": ts},
            qos=1, retain=config.MQTT_RETAIN_STATE)
        if pty in ALERT_PTY:
            rules_engine.on_pty_alert(client, pi, pty, data)
        elif rules_engine.is_emergency_active(pi):
            rules_engine.on_pty_normal(client, pi, pty, data)

    # Rule 5: EON Traffic Announcements (group 14A)
    # Only create events for genuine TA state transitions, not for the
    # first observation of a linked station (startup or new PI code).
    if group == "14A":
        on = data.get("other_network")
        if on and on.get("ta") is not None:
            other_pi = on.get("pi", "unknown")
            eon_key = f"eon/{other_pi}/ta"
            was_known = state.is_known(pi, eon_key)
            if state.changed(pi, eon_key, on["ta"]):
                pub(client, f"{pi}/eon/{other_pi}/ta",
                    {"active": on["ta"], "timestamp": ts},
                    qos=1, retain=config.MQTT_RETAIN_STATE)
                if was_known:
                    rules_engine.on_eon_ta(client, pi, other_pi, on["ta"], data)

    # --- Extended topics (only in "all" mode) ---
    publish_all = config.PUBLISH_MODE == "all"
    if not publish_all:
        if config.PUBLISH_RAW:
            pub(client, f"{pi}/system/raw", data, qos=0, retain=False)
        return

    ps = data.get("ps") or data.get("partial_ps")
    if ps and state.changed(pi, "station/ps", ps):
        pub(client, f"{pi}/station/ps", {"ps": ps.strip(), "timestamp": ts},
            qos=1, retain=config.MQTT_RETAIN_STATE)

    handle_pi_topic(client, pi, data)

    if "is_music" in data and state.changed(pi, "programme/music", data["is_music"]):
        pub(client, f"{pi}/programme/music",
            {"is_music": data["is_music"], "timestamp": ts},
            qos=0, retain=config.MQTT_RETAIN_STATE)

    di = data.get("di")
    if di and state.changed(pi, "programme/di", di):
        pub(client, f"{pi}/programme/di", {"di": di, "timestamp": ts},
            qos=0, retain=config.MQTT_RETAIN_STATE)

    for af_key in ("alt_frequencies_a", "alt_frequencies_b", "partial_alt_frequencies"):
        af = data.get(af_key)
        if af and state.changed(pi, "station/af", af):
            pub(client, f"{pi}/station/af",
                {"frequencies_khz": af, "timestamp": ts},
                qos=0, retain=config.MQTT_RETAIN_STATE)

    handle_bler(client, pi, data)

    if group in ("1A", "1B"):
        handle_pin(client, pi, data)
    if group == "3A":
        handle_oda(client, pi, data)
    if group == "4A":
        handle_clock(client, pi, data)
    if group == "14A":
        handle_eon(client, pi, data)
    if group == "15A":
        handle_long_ps(client, pi, data)
    if "radiotext_plus" in data:
        handle_rt_plus(client, pi, data)
    if "enhanced_radiotext" in data:
        handle_ert(client, pi, data)
    if config.PUBLISH_RAW:
        pub(client, f"{pi}/system/raw", data, qos=0, retain=False)


# ---------------------------------------------------------------------------
# Status publisher (periodic)
# ---------------------------------------------------------------------------

def status_publisher(stop_event):
    while not stop_event.is_set():
        stop_event.wait(config.STATUS_INTERVAL)
        if stop_event.is_set():
            break
        snap = stats.snapshot()
        snap["mqtt_connected"] = mqtt_connected.is_set()
        snap["frequency"] = config.FM_FREQUENCY
        snap["timestamp"] = now_iso()
        si = station_info.snapshot()
        if si:
            primary = next(iter(si.values()))
            snap["station"] = primary
        else:
            snap["station"] = None
        pub(mqtt_client, "system/status", snap, qos=0,
            retain=config.MQTT_RETAIN_STATE)


# ---------------------------------------------------------------------------
# Retention purge (hourly)
# ---------------------------------------------------------------------------

def retention_purge(stop_event):
    """Purge old events every hour."""
    days = config.EVENT_RETENTION_DAYS
    while not stop_event.is_set():
        stop_event.wait(3600)
        if stop_event.is_set():
            break
        try:
            event_store.purge_old_events(days)
        except Exception:
            log.exception("Error during retention purge")


# ---------------------------------------------------------------------------
# Module-level MQTT client — set by background thread, read by process_group
# ---------------------------------------------------------------------------

mqtt_client = None


# ---------------------------------------------------------------------------
# MQTT background connector — non-blocking, never delays startup
# ---------------------------------------------------------------------------

def _connect_mqtt_background():
    """Connect to MQTT broker in a background thread.

    Sets module-level mqtt_client and _mqtt_available when ready.
    Never blocks main startup.
    """
    global mqtt_client, _mqtt_available

    if not config.MQTT_HOST:
        log.warning("MQTT enabled but MQTT_HOST is empty — skipping MQTT")
        return

    log.info("MQTT connecting to %s:%s ...", config.MQTT_HOST, config.MQTT_PORT)
    try:
        client = create_mqtt_client()
        client.connect(config.MQTT_HOST, config.MQTT_PORT, keepalive=60)
        client.loop_start()
        _mqtt_available = True
        mqtt_client = client  # atomic ref assignment — safe under GIL
        log.info("MQTT client started (connection in progress)")
    except Exception as e:
        log.error("MQTT connection failed: %s", e)
        log.warning("Continuing without MQTT — web UI and event store still active")
        _mqtt_available = False


# ---------------------------------------------------------------------------
# Pipeline data callback — fed by pipeline.py reader thread
# ---------------------------------------------------------------------------

_line_count = 0
_error_count = 0
_last_stats_log = 0.0
_first_group = True


def _on_pipeline_line(raw_line):
    """Process a single line of ndjson from the redsea pipeline.

    Called by the pipeline thread for each line of redsea output.
    """
    global _line_count, _error_count, _last_stats_log, _first_group

    line = raw_line.decode("utf-8", errors="replace").strip()
    if not line:
        return

    try:
        data = json.loads(line)
        process_group(mqtt_client, data)
        _line_count += 1

        if _first_group:
            pi = data.get("pi", "?")
            log.info("════════════════════════════════════════════")
            log.info("  First RDS group received — decoder is running")
            log.info("  PI: %s  Frequency: %s", pi, config.FM_FREQUENCY)
            log.info("════════════════════════════════════════════")
            _first_group = False

        # Periodic progress log every 60 seconds
        now = time.time()
        if now - _last_stats_log >= 60:
            snap = stats.snapshot()
            summary = station_info.primary_summary()
            if summary:
                log.info("Stats: %d groups, %.1f grp/s, uptime %ds — %s",
                         snap["groups_total"], snap["groups_per_sec"],
                         snap["uptime_sec"], summary)
            else:
                log.info("Stats: %d groups, %.1f grp/s, uptime %ds (no station identified yet)",
                         snap["groups_total"], snap["groups_per_sec"],
                         snap["uptime_sec"])
            _last_stats_log = now

    except json.JSONDecodeError:
        _error_count += 1
        if _error_count <= 10:
            log.warning("Invalid JSON (line %d): %s",
                        _line_count + _error_count, line[:100])
        elif _error_count == 11:
            log.warning("Suppressing further JSON error messages")
    except Exception:
        log.exception("Error processing group")


# ---------------------------------------------------------------------------
# Main — supervisor pattern
# ---------------------------------------------------------------------------

def main():
    global _mqtt_available, _event_loop, _last_stats_log

    log.info("════════════════════════════════════════════")
    log.info("  RDS Guard starting")
    log.info("════════════════════════════════════════════")
    log.info("  Frequency:  %s", config.FM_FREQUENCY)
    log.info("  Publish:    %s (raw: %s)", config.PUBLISH_MODE, config.PUBLISH_RAW)
    log.info("  Retention:  %d days", config.EVENT_RETENTION_DAYS)
    log.info("  Web UI:     port %d", config.WEB_UI_PORT)
    log.info("  MQTT:       %s", "enabled" if config.MQTT_ENABLED else "disabled")
    log.info("  Recording:  always on → %s", config.AUDIO_DIR)
    log.info("  Transcribe: %s", config.TRANSCRIPTION_ENGINE)
    if config.MQTT_ENABLED:
        log.info("  MQTT host:  %s:%s", config.MQTT_HOST or "(empty)", config.MQTT_PORT)
        log.info("  MQTT user:  %s", config.MQTT_USER or "(none)")
        log.info("  MQTT prefix: %s", config.MQTT_TOPIC_PREFIX)

    # --- 1. Initialize event store (fast, ~10ms) ---
    log.info("Initializing event store...")
    event_store.init_db()
    event_store.close_stale_events()

    stop_event = threading.Event()

    # --- 1b. Initialize transcription + recording ---
    from transcriber import create_transcriber
    from audio_recorder import AudioRecorder

    transcriber_instance = create_transcriber(
        engine=config.TRANSCRIPTION_ENGINE,
        language=config.TRANSCRIPTION_LANGUAGE,
        model_size=config.TRANSCRIPTION_MODEL,
        device=config.TRANSCRIPTION_DEVICE,
        remote_url=config.WHISPER_REMOTE_URL,
        remote_timeout=config.WHISPER_REMOTE_TIMEOUT,
    )
    if transcriber_instance:
        transcriber_instance.start()

    recorder = AudioRecorder(
        audio_dir=config.AUDIO_DIR,
        transcriber=transcriber_instance,
        on_transcription_complete=_on_transcription_complete,
        max_duration_sec=config.MAX_RECORDING_SEC,
    )
    log.info("Audio recording enabled → %s", config.AUDIO_DIR)

    # Wire recorder to rules engine
    rules_engine._recorder = recorder
    rules_engine._record_types = set(
        t.strip() for t in config.RECORD_EVENT_TYPES.split(","))

    # --- 2. Start web server FIRST — always available ---
    log.info("Starting web server on port %d...", config.WEB_UI_PORT)
    import web_server
    from pipeline import pipeline_status

    web_thread = threading.Thread(
        target=web_server.run_server,
        args=(config.WEB_UI_PORT, stats, station_info, mqtt_connected, pipeline_status),
        daemon=True,
    )
    web_thread.start()

    # Give the web server a moment to bind
    time.sleep(0.5)
    if web_thread.is_alive():
        log.info("Web UI ready at http://0.0.0.0:%d", config.WEB_UI_PORT)
    else:
        log.error("Web server thread died on startup!")

    # --- 3. MQTT in background (non-blocking, fire-and-forget) ---
    if config.MQTT_ENABLED:
        mqtt_thread = threading.Thread(
            target=_connect_mqtt_background,
            daemon=True,
        )
        mqtt_thread.start()
    else:
        log.info("MQTT disabled (set MQTT_ENABLED=true in .env to activate)")

    # --- 4. Start radio pipeline (with AudioTee + recorder) ---
    from pipeline import run_pipeline

    _last_stats_log = time.time()

    pipeline_thread = threading.Thread(
        target=run_pipeline,
        args=(_on_pipeline_line, pipeline_status, stop_event, recorder),
        daemon=True,
    )
    pipeline_thread.start()

    # --- 5. Housekeeping threads ---

    # MQTT status publisher — uses module-level mqtt_client
    # Start it even if MQTT isn't connected yet; it will be a no-op until
    # _mqtt_available becomes True
    if config.MQTT_ENABLED:
        status_thread = threading.Thread(
            target=status_publisher, args=(stop_event,), daemon=True
        )
        status_thread.start()
        log.info("MQTT status publisher started (every %ds)", config.STATUS_INTERVAL)

    # Retention purge
    purge_thread = threading.Thread(
        target=retention_purge, args=(stop_event,), daemon=True
    )
    purge_thread.start()

    # --- 6. Signal handling + main thread wait ---
    def shutdown(signum, frame):
        log.info("Shutting down (signal %s)...", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info("RDS Guard running — all systems started")

    # Block main thread until shutdown signal
    try:
        stop_event.wait()
    except KeyboardInterrupt:
        pass

    # --- Cleanup ---
    log.info("Cleaning up...")

    if mqtt_client:
        try:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
            log.info("MQTT disconnected")
        except Exception:
            pass

    # Give threads a moment to finish
    pipeline_thread.join(timeout=5)

    log.info("RDS Guard stopped — %d groups decoded, %d JSON errors",
             _line_count, _error_count)


if __name__ == "__main__":
    main()
