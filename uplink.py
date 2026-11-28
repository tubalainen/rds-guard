"""Uplink — forwards events, audio and status to an RDS Guard aggregator.

Only active in satellite mode (UPLINK_URL set).  Everything is sent as
outbound HTTP(S) requests, so it works from behind NAT without opening ports.

Joining (protocol 2): without UPLINK_TOKEN the satellite makes its own token
once, keeps it in its database and asks to join (POST /join).  The
aggregator's owner approves it under Admin -> Noder (or a shared JOIN_KEY
approves it at once).  Nothing secret travels back, so a lost answer only
means asking again.  Events are recorded and queued meanwhile.

What is sent (see docs/uplink-protocol.md):
  - event snapshots: RDS context, RadioText and timestamps of each traffic
    announcement / emergency broadcast, as it starts, updates and ends
  - the recorded audio of each event (OGG/Opus)
  - a status heartbeat (stations, decode rate, pipeline health)

What is never sent: transcriptions.  The aggregator gets raw data only.

Delivery is store-and-forward.  event_store writes an outbox row in the same
transaction as every relevant event change.  Two worker threads drain it:
one for event snapshots and deletes, one for audio, so a new broadcast is
reported while an earlier recording is still uploading.  An audio upload
waits until its event has been delivered.  Both retry with backoff, cut
short when something new is queued, so an outage at either end only delays
delivery.
"""

import hashlib
import json
import logging
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

import event_store

log = logging.getLogger("rds-guard")

PROTOCOL_VERSION = "2"
PROTOCOL_HEADER = "X-RDS-Guard-Protocol"

# Outcome of one delivery attempt
OK = "ok"                      # delivered (or nothing left to deliver)
DROP = "drop"                  # aggregator rejected it for good — discard
RETRY = "retry"                # transient problem — try again later
UNAUTHORIZED = "unauthorized"  # token rejected — keep queue, try again later
INCOMPATIBLE = "incompatible"  # aggregator speaks another protocol version
WAIT = "wait"                  # audio waits for its event to be delivered

# The two queues
EVENTS = (event_store.OUTBOX_EVENT, event_store.OUTBOX_DELETE)
AUDIO = (event_store.OUTBOX_AUDIO,)

_JOIN_EVERY = 15               # seconds between join requests while pending

_BACKOFF_MIN = 5
_BACKOFF_MAX = 300
_HOLD_SEC = 300                # wait after unauthorized / incompatible

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def utc_z(ts):
    """Normalise a stored timestamp ('YYYY-MM-DDTHH:MM:SS', UTC) to ISO 'Z'."""
    if not ts or not isinstance(ts, str) or not _TS_RE.match(ts):
        return None
    return ts[:19] + "Z"


def _now_z():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_event_payload(row, rev):
    """Build the wire snapshot of an event row.  No transcription fields."""
    try:
        data = json.loads(row.get("data") or "{}")
    except (TypeError, ValueError):
        data = {}
    try:
        radiotext = json.loads(row.get("radiotext") or "[]")
    except (TypeError, ValueError):
        radiotext = []
    if not isinstance(radiotext, list):
        radiotext = []
    station = data.get("station") if isinstance(data.get("station"), dict) else {}

    return {
        "rev": rev,
        "type": row.get("type"),
        "state": row.get("state"),
        "pi": row.get("pi"),
        "station_ps": row.get("station_ps"),
        "long_ps": station.get("long_ps"),
        "frequency": row.get("frequency"),
        "prog_type": data.get("prog_type") or None,
        "radiotext": [str(rt) for rt in radiotext],
        "started_at": utc_z(row.get("started_at")),
        "ended_at": utc_z(row.get("ended_at")),
        "duration_sec": row.get("duration_sec"),
        "receiver": "0",       # one receiver per satellite until v1.0
        "sent_at": _now_z(),
    }


def verify_code(join_id):
    """The short code shown in the satellite's log and in the aggregator's
    Admin, so the owner approves the right node (same formula there)."""
    digest = hashlib.sha256(join_id.encode("utf-8")).digest()
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    chars = [alphabet[b % len(alphabet)] for b in digest[:6]]
    return "".join(chars[:3]) + "-" + "".join(chars[3:])


class Uplink:
    """Join, two outbox workers and the heartbeat."""

    def __init__(self, base_url, token, audio_dir, node_name="",
                 version="dev", status_fn=None, heartbeat_sec=30,
                 max_age_hours=72, http=None, join_key="",
                 suggested_location=None):
        self._base = base_url.rstrip("/") + "/api/v1/ingest"
        self._audio_dir = Path(audio_dir)
        self._node_name = node_name
        self._version = version
        self._status_fn = status_fn
        self._heartbeat_sec = heartbeat_sec
        self._max_age_hours = max_age_hours
        self._http = http  # requests-compatible module/session (tests inject)
        self._join_key = join_key or ""
        self._suggested_location = suggested_location or None

        # A configured token is used as it is; otherwise the satellite's own
        self._configured = bool(token)
        if token:
            self._token = token
            self._join_id = None
        else:
            self._token, self._join_id = self._own_identity()
        self._ready = threading.Event()        # token accepted: may send
        if self._configured or event_store.get_setting("uplink_joined") == "1":
            self._ready.set()

        self._wakes = [threading.Event(), threading.Event()]
        self._lock = threading.Lock()
        self._state = "starting" if self._ready.is_set() else "joining"
        self._last_ok_at = None
        self._last_error = None
        self._verify = verify_code(self._join_id) if self._join_id else None
        self._threads = []
        # Called with the aggregator's commands[] from a heartbeat answer
        # (e.g. recalibrate a receiver); set by the satellite
        self.on_commands = None

    @staticmethod
    def _own_identity():
        """This satellite's own token and join id, made once and kept."""
        token = event_store.get_setting("uplink_token")
        join_id = event_store.get_setting("uplink_join_id")
        if not token or not join_id:
            token = secrets.token_urlsafe(32)
            join_id = str(uuid.uuid4())
            event_store.set_setting("uplink_token", token)
            event_store.set_setting("uplink_join_id", join_id)
            event_store.set_setting("uplink_joined", None)
        return token, join_id

    # -- public API ---------------------------------------------------------

    def notify(self):
        """Wake the workers — something was queued."""
        for w in self._wakes:
            w.set()

    def start(self, stop_event):
        """Start the join, worker and heartbeat threads."""
        targets = [(self._run_lane, (stop_event, EVENTS, self._wakes[0]), "uplink-events"),
                   (self._run_lane, (stop_event, AUDIO, self._wakes[1]), "uplink-audio"),
                   (self._run_heartbeat, (stop_event,), "uplink-heartbeat")]
        if not self._configured:
            targets.insert(0, (self._run_join, (stop_event,), "uplink-join"))
        for target, args, name in targets:
            t = threading.Thread(target=target, args=args, daemon=True, name=name)
            t.start()
            self._threads.append(t)
        log.info("Uplink started → %s", self._base)
        if not self._configured and not self._ready.is_set():
            log.info("Uplink: asking to join the aggregator as '%s' (code %s). "
                     "Approve it under Admin → Noder, or set the same JOIN_KEY "
                     "on both.", self._node_name or "(no NODE_NAME)", self._verify)

    def snapshot(self):
        """Uplink health for /api/status."""
        with self._lock:
            snap = {
                "enabled": True,
                "state": self._state,
                "last_ok_at": self._last_ok_at,
                "last_error": self._last_error,
                "verify": self._verify if not self._ready.is_set() else None,
            }
        try:
            snap["queue_depth"] = event_store.outbox_depth()
        except Exception:
            snap["queue_depth"] = None
        return snap

    # -- joining ------------------------------------------------------------

    def join_once(self):
        """Ask to join once. Returns 'approved', 'pending', 'rejected' or an
        outcome constant (RETRY, INCOMPATIBLE)."""
        body = {
            "join_id": self._join_id,
            "node_name": self._node_name,
            "version": self._version,
            "suggested_location": self._suggested_location,
            "sent_at": _now_z(),
        }
        if self._join_key:
            body["join_key"] = self._join_key
        if self._status_fn is not None:
            try:
                body["stations"] = self._status_fn().get("stations") or []
            except Exception:
                pass
        resp, outcome = self._send("POST", "/join", json_body=body, timeout=(10, 15))
        if resp is None:
            return outcome
        if resp.status_code == 200:
            event_store.set_setting("uplink_joined", "1")
            self._ready.set()
            self.notify()
            self._set_state("connected", None, ok=True)
            log.info("Uplink: approved by the aggregator — sending events")
            return "approved"
        if resp.status_code == 202:
            self._set_state("pending", f"waiting for approval (code {self._verify})")
            return "pending"
        if resp.status_code == 403:
            self._set_state("rejected", "the aggregator's owner rejected this node "
                                        "(or JOIN_KEY does not match)")
            return "rejected"
        return self._classify(resp, "POST", "/join")

    def _run_join(self, stop_event):
        wake = threading.Event()
        while not stop_event.is_set():
            if self._ready.is_set():
                stop_event.wait(30)
                continue
            try:
                result = self.join_once()
            except Exception:
                log.exception("Uplink join error")
                result = RETRY
            delay = {"approved": 0, "pending": _JOIN_EVERY,
                     "rejected": _HOLD_SEC, INCOMPATIBLE: _HOLD_SEC}.get(result, _JOIN_EVERY)
            if delay:
                _wait(stop_event, wake, delay)

    # -- delivery -----------------------------------------------------------

    def process_one(self, lane=None):
        """Try to deliver the oldest queued item of one queue (lane None:
        events first, then audio).

        Returns None if that queue is empty, WAIT when its head is audio
        whose event is not delivered yet, otherwise an outcome constant.
        On OK/DROP the item has been removed from the queue.
        """
        if lane is None:
            outcome = self.process_one(EVENTS)
            return outcome if outcome is not None else self.process_one(AUDIO)
        item = event_store.outbox_head(lane)
        if item is None:
            return None

        kind = item["kind"]
        if kind == event_store.OUTBOX_EVENT:
            outcome = self._send_event(item)
        elif kind == event_store.OUTBOX_AUDIO:
            if event_store.outbox_event_pending(item["event_id"], item["id"]):
                return WAIT
            outcome = self._send_audio(item)
        elif kind == event_store.OUTBOX_DELETE:
            outcome = self._send_delete(item)
        else:
            log.warning("Uplink: unknown outbox kind '%s', dropping", kind)
            outcome = DROP

        if outcome in (OK, DROP) and kind != event_store.OUTBOX_EVENT:
            event_store.outbox_ack(item["id"])
            if outcome == OK and kind == event_store.OUTBOX_AUDIO and item.get("event_id"):
                # Kept SATELLITE_KEEP_DAYS, then removed
                event_store.mark_audio_delivered(item["event_id"])
        return outcome

    def _send_event(self, item):
        event_id = item["event_id"]
        # Read the watermark first, then the row: the snapshot then covers
        # every queued change of this event up to `rev`.
        rev = event_store.outbox_latest_event_rev(event_id)
        row = event_store.get_event(event_id)
        if row is None or not row.get("event_uid"):
            # Deleted since it was queued (a 'delete' item follows if the
            # aggregator needs to know).
            event_store.outbox_ack_event(event_id, rev)
            return OK
        payload = build_event_payload(row, rev)
        outcome = self._request(
            "PUT", f"/events/{row['event_uid']}", json_body=payload)
        if outcome in (OK, DROP):
            event_store.outbox_ack_event(event_id, rev)
        return outcome

    def _send_audio(self, item):
        row = event_store.get_event(item["event_id"])
        if row is None or not row.get("audio_path"):
            return OK  # event gone — nothing to upload
        name = Path(row["audio_path"]).name
        path = self._audio_dir / name
        try:
            body = path.read_bytes()
        except OSError:
            log.warning("Uplink: audio file %s missing, skipping upload", name)
            return OK
        return self._request(
            "PUT", f"/events/{item['event_uid']}/audio",
            data=body, content_type="audio/ogg", timeout=(10, 120),
            not_found=DROP)

    def _send_delete(self, item):
        # 404 = the aggregator never heard of it, which is the desired state
        return self._request(
            "DELETE", f"/events/{item['event_uid']}", not_found=OK)

    def send_heartbeat(self):
        """Send one status heartbeat.  Returns an outcome constant."""
        body = {
            "node_name": self._node_name,
            "version": self._version,
            "sent_at": _now_z(),
            "suggested_location": self._suggested_location,
        }
        if self._status_fn is not None:
            try:
                body.update(self._status_fn())
            except Exception:
                log.debug("Uplink: status snapshot failed", exc_info=True)
        try:
            body["queue_depth"] = event_store.outbox_depth()
        except Exception:
            pass
        resp, outcome = self._send("POST", "/heartbeat", json_body=body,
                                   timeout=(10, 15))
        if resp is None:
            return outcome
        outcome = self._classify(resp, "POST", "/heartbeat")
        if outcome == OK and self.on_commands is not None:
            try:
                commands = (resp.json() or {}).get("commands") or []
            except Exception:
                commands = []
            if commands:
                try:
                    self.on_commands(commands)
                except Exception:
                    log.exception("Uplink: could not run the aggregator's commands")
        return outcome

    def _send(self, method, path, json_body=None, data=None,
              content_type=None, timeout=(10, 30)):
        """Perform one HTTP request. Returns (response, None), or (None,
        RETRY) when nothing answered."""
        http = self._http
        if http is None:
            import requests
            http = requests
        headers = {
            "Authorization": f"Bearer {self._token}",
            PROTOCOL_HEADER: PROTOCOL_VERSION,
            "User-Agent": f"rds-guard/{self._version}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        try:
            return http.request(
                method, self._base + path, json=json_body, data=data,
                headers=headers, timeout=timeout, allow_redirects=False), None
        except Exception as e:
            self._set_state("offline", f"{type(e).__name__}: {e}")
            return None, RETRY

    def _request(self, method, path, json_body=None, data=None,
                 content_type=None, timeout=(10, 30), not_found=DROP):
        """Perform one HTTP request and classify the response."""
        resp, outcome = self._send(method, path, json_body, data, content_type,
                                   timeout)
        if resp is None:
            return outcome
        return self._classify(resp, method, path, not_found)

    def _classify(self, resp, method, path, not_found=DROP):
        status = resp.status_code
        from_aggregator = PROTOCOL_HEADER in resp.headers

        if 200 <= status < 300:
            self._set_state("connected", None, ok=True)
            return OK

        detail = f"HTTP {status} on {method} {path}"
        if not from_aggregator:
            # A reverse proxy, firewall or wrong URL answered — not the
            # aggregator.  Never discard data because of that.
            self._set_state(
                "offline", detail + " (response did not come from an "
                "RDS Guard aggregator — check UPLINK_URL / proxy)")
            return RETRY
        if status in (401, 403):
            if not self._configured and status == 401 and self._ready.is_set():
                # Our own token is no longer known (the node was removed):
                # ask to join again
                log.warning("Uplink: the aggregator no longer knows this node — "
                            "asking to join again (code %s)", self._verify)
                event_store.set_setting("uplink_joined", None)
                self._ready.clear()
                self._set_state("joining", detail)
                return UNAUTHORIZED
            self._set_state("unauthorized", detail + " — check UPLINK_TOKEN")
            return UNAUTHORIZED
        if status == 426:
            self._set_state(
                "incompatible", detail + " — this RDS Guard version is too "
                "old or too new for the aggregator")
            return INCOMPATIBLE
        if status == 404:
            if not_found == OK:
                self._set_state("connected", None, ok=True)
            return not_found
        if status in (408, 425, 429) or status >= 500:
            self._set_state("offline", detail)
            return RETRY
        # Remaining 4xx: the aggregator understood and refused this item
        log.warning("Uplink: aggregator rejected item (%s): %s",
                    detail, resp.text[:200])
        self._set_state("connected", None, ok=True)
        return DROP

    def _set_state(self, state, error, ok=False):
        with self._lock:
            changed = state != self._state
            self._state = state
            self._last_error = error
            if ok:
                self._last_ok_at = _now_z()
        if changed:
            if error:
                log.warning("Uplink %s: %s", state, error)
            else:
                log.info("Uplink %s", state)
            if state == "connected":
                # Back after an outage (e.g. the heartbeat got through): send
                # what is queued now, not after the lanes' backoff (up to 5 min)
                self.notify()

    # -- threads ------------------------------------------------------------

    def _run_lane(self, stop_event, lane, wake):
        backoff = _BACKOFF_MIN
        last_purge = 0.0
        while not stop_event.is_set():
            if not self._ready.is_set():
                # Not approved yet: keep everything queued
                _wait(stop_event, wake, 5)
                continue
            try:
                now = time.time()
                if lane is EVENTS and now - last_purge >= 3600:
                    event_store.outbox_purge(self._max_age_hours)
                    last_purge = now

                outcome = self.process_one(lane)
            except Exception:
                log.exception("Uplink worker error")
                outcome = RETRY

            if outcome is None:
                # Queue empty — sleep until something is queued
                _wait(stop_event, wake, 60)
            elif outcome == WAIT:
                # Audio whose event the other worker has not delivered yet
                _wait(stop_event, wake, 2)
            elif outcome in (OK, DROP):
                backoff = _BACKOFF_MIN
            elif outcome == RETRY:
                # Cut short when something new is queued: a new broadcast
                # is tried at once
                _wait(stop_event, wake, backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
            else:  # UNAUTHORIZED / INCOMPATIBLE
                _wait(stop_event, wake, _HOLD_SEC)

    def _run_heartbeat(self, stop_event):
        # First heartbeat soon after start so the node shows up quickly
        stop_event.wait(3)
        while not stop_event.is_set():
            if not self._ready.is_set():
                stop_event.wait(5)
                continue
            try:
                self.send_heartbeat()
            except Exception:
                log.exception("Uplink heartbeat error")
            stop_event.wait(self._heartbeat_sec)


def _wait(stop_event, wake, seconds):
    """Sleep up to `seconds`; return early when woken or stopping."""
    deadline = time.monotonic() + seconds
    while not stop_event.is_set():
        left = deadline - time.monotonic()
        if left <= 0:
            return
        if wake.wait(timeout=min(left, 1.0)):
            wake.clear()
            return
