"""Aggregator processing: transcription, MQTT alerts, node status, retention.

Ingest handlers call the Hooks methods here; they must return quickly, so
anything slow happens in the transcriber thread or the sweeper thread.

Alerts are driven by the database (alert_published_at), not by in-memory
timers, so a restart neither loses nor repeats an alert.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, notifier, store
from .ingest import Hooks
from .mqtt_out import topic_slug

log = logging.getLogger("rds-aggregator")

EVENT_TYPE_NAMES = {
    "traffic": "traffic_announcement",
    "emergency": "emergency_broadcast",
}

SWEEP_SEC = 5


def _epoch(ts):
    if not ts:
        return None
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def alert_decision(row, now, engine_enabled=True):
    """Decide what to do with an ended event's alert.

    Returns (action, transcription_status) where action is "wait",
    "publish" or "skip".
    """
    ended = _epoch(row.get("ended_at")) or _epoch(row.get("end_received_at"))
    if ended is not None and now - ended > config.ALERT_MAX_DELAY_MIN * 60:
        return "skip", None

    if not row.get("audio_path"):
        end_recv = _epoch(row.get("end_received_at")) or now
        if now - end_recv >= config.ALERT_AUDIO_WAIT_SEC:
            return "publish", "none"
        return "wait", None

    status = row.get("transcription_status")
    if status in ("done", "error"):
        return "publish", status
    if not engine_enabled or status is None:
        return "publish", "none"
    audio_recv = _epoch(row.get("audio_received_at")) or now
    if now - audio_recv >= config.ALERT_TRANSCRIPTION_WAIT_SEC:
        return "publish", "timeout"
    return "wait", None


def alert_payload(row, transcription_status):
    """MQTT alert payload — the satellite's format plus a `node` field."""
    station = {"pi": row["pi"]}
    for src, dst in (("station_ps", "ps"), ("long_ps", "long_ps"),
                     ("prog_type", "prog_type")):
        if row.get(src):
            station[dst] = row[src]
    return {
        "event_type": EVENT_TYPE_NAMES.get(row["type"], row["type"]),
        "state": "end",
        "station": station,
        "frequency": row.get("frequency"),
        "started": row.get("started_at"),
        "ended": row.get("ended_at"),
        "duration_sec": row.get("duration_sec"),
        "radiotext": row.get("radiotext") or [],
        "prog_type": row.get("prog_type") or "",
        "audio_available": bool(row.get("audio_path")),
        "transcribed_text": (row.get("transcription")
                             if transcription_status == "done" else None),
        "transcription_status": transcription_status,
        "event_id": row["event_uid"],
        "node": {"name": row.get("node_name")},
        "timestamp": store.now_iso(),
    }


def remove_audio_files(paths):
    audio_dir = Path(config.AUDIO_DIR)
    for p in paths or []:
        try:
            (audio_dir / Path(p).name).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Could not remove audio %s: %s", p, e)


class Pipeline(Hooks):
    def __init__(self, transcriber=None, mqtt=None):
        self._transcriber = transcriber
        self._mqtt = mqtt
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._online = {}        # node_id -> bool (current state)
        self._synced = {}        # node_id -> state was published to MQTT
        self._last_purge = 0.0

    # -- hooks (event loop thread; must be quick) ---------------------------

    def on_event(self, row, prev_state):
        if (row["type"] == "emergency" and row["state"] in store.ACTIVE_STATES
                and prev_state not in store.ACTIVE_STATES):
            # VMA push goes out at once, not after the broadcast
            notifier.dispatch_async(row, self.notify_publish)
        if row["type"] == "traffic":
            was_active = prev_state in store.ACTIVE_STATES
            is_active = row["state"] in store.ACTIVE_STATES
            if is_active and not was_active:
                self._publish_ta(row, True)
            elif was_active and not is_active:
                self._publish_ta(row, False)
        self._wake.set()

    def on_audio(self, row):
        self.enqueue_transcription(row)
        self._wake.set()

    def on_delete(self, row):
        # A discarded (too short) announcement: clear the TA flag again
        if row["type"] == "traffic" and row["state"] in store.ACTIVE_STATES:
            self._publish_ta(row, False)

    def on_heartbeat(self, node):
        self._set_online(node, True)

    # -- transcription ------------------------------------------------------

    def enqueue_transcription(self, row):
        if self._transcriber is None or not row.get("audio_path"):
            return
        path = Path(config.AUDIO_DIR) / Path(row["audio_path"]).name
        # Transcriber reports by integer id (shared with the satellite code)
        self._transcriber.enqueue(path, row["id"], self._on_transcribed)

    def requeue_pending(self):
        """After a restart, queue audio whose transcription never finished."""
        rows = store.pending_transcriptions()
        for row in rows:
            self.enqueue_transcription(row)
        if rows:
            log.info("Re-queued %d unfinished transcriptions", len(rows))

    def _on_transcribed(self, event_id, text, error, duration_sec=None):
        row = store.get_event_by_id(event_id)
        if row is None:
            return  # deleted meanwhile
        uid = row["event_uid"]
        if error:
            store.set_transcription(uid, None, "error")
        else:
            store.set_transcription(uid, text, "done", duration_sec)
            self._publish(
                f"{row['pi']}/{row['type']}/transcription", {
                    "event_id": uid,
                    "station": {"pi": row["pi"], "ps": row.get("station_ps")},
                    "node": {"name": row.get("node_name")},
                    "transcription": text,
                    "language": config.TRANSCRIPTION_LANGUAGE,
                    "duration_sec": row.get("duration_sec"),
                    "radiotext": row.get("radiotext") or [],
                    "timestamp": store.now_iso(),
                }, retain=True)
        self._wake.set()

    # -- MQTT helpers -------------------------------------------------------

    def _publish(self, topic, payload, retain=False):
        if self._mqtt is None:
            return False
        return self._mqtt.publish(topic, payload, qos=1, retain=retain)

    def notify_publish(self, topic, payload):
        """Publisher for push messages: None when MQTT is not configured."""
        if self._mqtt is None:
            return None
        return self._mqtt.publish(topic, payload, qos=1)

    def _notify_after_alert(self, row):
        # Emergencies were pushed when they started; only one that arrived
        # already ended (node was offline) is pushed here.
        if row["type"] == "emergency" and store.has_notification(row["event_uid"]):
            return
        try:
            notifier.dispatch(row, self.notify_publish)
        except Exception:
            log.exception("Push dispatch failed for %s", row["event_uid"])

    def _publish_ta(self, row, active):
        self._publish(f"{row['pi']}/traffic/ta", {
            "active": active,
            "since": row.get("started_at") if active else row.get("ended_at"),
            "station": {"pi": row["pi"], "ps": row.get("station_ps")},
            "frequency": row.get("frequency"),
            "node": {"name": row.get("node_name")},
            "timestamp": store.now_iso(),
        }, retain=True)

    def _set_online(self, node, online):
        nid = node["id"]
        with self._lock:
            changed = self._online.get(nid) != online
            self._online[nid] = online
            need_publish = changed or not self._synced.get(nid)
        if changed:
            log.info("Node '%s' is %s", node["name"],
                     "online" if online else "offline")
        if need_publish:
            ok = self._publish(f"nodes/{topic_slug(node['name'])}/status", {
                "name": node["name"],
                "online": online,
                "last_seen_at": node.get("last_heartbeat_at"),
                "version": node.get("version"),
            }, retain=True)
            with self._lock:
                self._synced[nid] = ok

    # -- sweeper ------------------------------------------------------------

    def sweep(self, now=None):
        """One pass: alerts, node liveness, retention."""
        now = time.time() if now is None else now
        self._sweep_alerts(now)
        self._sweep_nodes(now)
        if now - self._last_purge >= 3600:
            self._last_purge = now
            remove_audio_files(store.purge_old_events(config.EVENT_RETENTION_DAYS))
            store.purge_notify_log(config.EVENT_RETENTION_DAYS)

    def _sweep_alerts(self, now):
        engine_on = self._transcriber is not None
        for row in store.alert_candidates():
            action, tstatus = alert_decision(row, now, engine_on)
            if action == "wait":
                continue
            if action == "skip":
                store.mark_alert(row["event_uid"], "skipped_late")
                continue
            if self._mqtt is None:
                store.mark_alert(row["event_uid"], "mqtt_disabled")
                self._notify_after_alert(row)
                continue
            if self._publish("alert", alert_payload(row, tstatus)):
                store.mark_alert(row["event_uid"], "published")
                self._notify_after_alert(row)
                log.info("Alert published for %s from %s (%s)",
                         row.get("station_ps") or row["pi"],
                         row.get("node_name"), tstatus)
            # else: MQTT not connected — try again on the next sweep

    def _sweep_nodes(self, now):
        for node in store.list_nodes():
            seen = _epoch(node.get("last_heartbeat_at"))
            online = (node["enabled"] and seen is not None
                      and now - seen < config.NODE_OFFLINE_SEC)
            self._set_online(node, online)

    def run(self, stop_event):
        """Sweeper loop (own thread)."""
        while not stop_event.is_set():
            try:
                self.sweep()
            except Exception:
                log.exception("Sweeper error")
            self._wake.wait(SWEEP_SEC)
            self._wake.clear()

    def node_online(self, node_id):
        with self._lock:
            return bool(self._online.get(node_id))
