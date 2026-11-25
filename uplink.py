"""Uplink — forwards events, audio and status to an RDS Guard aggregator.

Only active in satellite mode (UPLINK_URL set).  Everything is sent as
outbound HTTPS requests, so it works from behind NAT without opening ports.

What is sent (see docs/uplink-protocol.md):
  - event snapshots: RDS context, RadioText and timestamps of each traffic
    announcement / emergency broadcast, as it starts, updates and ends
  - the recorded audio of each event (OGG/Opus)
  - a status heartbeat (stations, decode rate, pipeline health)

What is never sent: transcriptions.  The aggregator gets raw data only.

Delivery is store-and-forward.  event_store writes an outbox row in the same
transaction as every relevant event change; the worker thread here drains
that queue in order and retries with backoff, so an outage at either end
only delays delivery.
"""

import json
import logging
import re
import threading
import time
from pathlib import Path

import event_store

log = logging.getLogger("rds-guard")

PROTOCOL_VERSION = "1"
PROTOCOL_HEADER = "X-RDS-Guard-Protocol"

# Outcome of one delivery attempt
OK = "ok"                      # delivered (or nothing left to deliver)
DROP = "drop"                  # aggregator rejected it for good — discard
RETRY = "retry"                # transient problem — try again later
UNAUTHORIZED = "unauthorized"  # token rejected — keep queue, try again later
INCOMPATIBLE = "incompatible"  # aggregator speaks another protocol version

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
        "sent_at": _now_z(),
    }


class Uplink:
    """Outbox worker + heartbeat sender."""

    def __init__(self, base_url, token, audio_dir, node_name="",
                 version="dev", status_fn=None, heartbeat_sec=30,
                 max_age_hours=72, http=None):
        self._base = base_url.rstrip("/") + "/api/v1/ingest"
        self._token = token
        self._audio_dir = Path(audio_dir)
        self._node_name = node_name
        self._version = version
        self._status_fn = status_fn
        self._heartbeat_sec = heartbeat_sec
        self._max_age_hours = max_age_hours
        self._http = http  # requests-compatible module/session (tests inject)

        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._state = "starting"
        self._last_ok_at = None
        self._last_error = None
        self._threads = []

    # -- public API ---------------------------------------------------------

    def notify(self):
        """Wake the worker — something was queued."""
        self._wake.set()

    def start(self, stop_event):
        """Start the worker and heartbeat threads."""
        for target, name in ((self._run_worker, "uplink-worker"),
                             (self._run_heartbeat, "uplink-heartbeat")):
            t = threading.Thread(target=target, args=(stop_event,),
                                 daemon=True, name=name)
            t.start()
            self._threads.append(t)
        log.info("Uplink started → %s", self._base)

    def snapshot(self):
        """Uplink health for /api/status."""
        with self._lock:
            snap = {
                "enabled": True,
                "state": self._state,
                "last_ok_at": self._last_ok_at,
                "last_error": self._last_error,
            }
        try:
            snap["queue_depth"] = event_store.outbox_depth()
        except Exception:
            snap["queue_depth"] = None
        return snap

    # -- delivery -----------------------------------------------------------

    def process_one(self):
        """Try to deliver the oldest queued item.

        Returns None if the queue is empty, otherwise one of the outcome
        constants.  On OK/DROP the item has been removed from the queue.
        """
        item = event_store.outbox_head()
        if item is None:
            return None

        kind = item["kind"]
        if kind == event_store.OUTBOX_EVENT:
            outcome = self._send_event(item)
        elif kind == event_store.OUTBOX_AUDIO:
            outcome = self._send_audio(item)
        elif kind == event_store.OUTBOX_DELETE:
            outcome = self._send_delete(item)
        else:
            log.warning("Uplink: unknown outbox kind '%s', dropping", kind)
            outcome = DROP

        if outcome in (OK, DROP) and kind != event_store.OUTBOX_EVENT:
            event_store.outbox_ack(item["id"])
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
        return self._request("POST", "/heartbeat", json_body=body,
                             timeout=(10, 15))

    def _request(self, method, path, json_body=None, data=None,
                 content_type=None, timeout=(10, 30), not_found=DROP):
        """Perform one HTTP request and classify the response."""
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
            resp = http.request(
                method, self._base + path, json=json_body, data=data,
                headers=headers, timeout=timeout, allow_redirects=False)
        except Exception as e:
            self._set_state("offline", f"{type(e).__name__}: {e}")
            return RETRY

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

    # -- threads ------------------------------------------------------------

    def _run_worker(self, stop_event):
        backoff = _BACKOFF_MIN
        last_purge = 0.0
        while not stop_event.is_set():
            try:
                now = time.time()
                if now - last_purge >= 3600:
                    event_store.outbox_purge(self._max_age_hours)
                    last_purge = now

                outcome = self.process_one()
            except Exception:
                log.exception("Uplink worker error")
                outcome = RETRY

            if outcome is None:
                # Queue empty — sleep until something is queued
                self._wake.wait(timeout=60)
                self._wake.clear()
            elif outcome in (OK, DROP):
                backoff = _BACKOFF_MIN
            elif outcome == RETRY:
                stop_event.wait(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)
            else:  # UNAUTHORIZED / INCOMPATIBLE
                stop_event.wait(_HOLD_SEC)

    def _run_heartbeat(self, stop_event):
        # First heartbeat soon after start so the node shows up quickly
        stop_event.wait(3)
        while not stop_event.is_set():
            try:
                self.send_heartbeat()
            except Exception:
                log.exception("Uplink heartbeat error")
            stop_event.wait(self._heartbeat_sec)
