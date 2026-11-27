#!/usr/bin/env python3
"""RDS Guard satellite: receives, records and reports.

For each receiver (RTL-SDR dongle, receivers.yml) a pipeline decodes RDS on
its stations. The rules below turn each station's RDS into events: a traffic
announcement while the TA flag is on, an emergency broadcast while the
programme type is Alarm. Each event and its recording go to the aggregator
at once (uplink.py), which transcribes, maps, alerts and archives them.

No web UI, no MQTT: the aggregator shows this satellite's state from its
heartbeat. A health file (satellite_health.py) is Docker's health check.
"""

import json
import logging
import signal
import sys
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

ALERT_PTY = {"Alarm", "Alarm - Loss of radio"}
MIN_EVENT_SEC = 10          # shorter "events" are RDS noise


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def msg_ts(data):
    return data.get("timestamp") or now_iso()


# ---------------------------------------------------------------------------
# Per station: last values, decoded fields, decode rate
# ---------------------------------------------------------------------------

class StationState:
    """Last value of each tracked field (per PI), to see changes."""

    def __init__(self):
        self._state = {}

    def changed(self, pi, key, value):
        full = f"{pi}/{key}"
        if self._state.get(full) == value:
            return False
        self._state[full] = value
        return True

    def is_known(self, pi, key):
        return f"{pi}/{key}" in self._state

    def forget(self, pi, key):
        self._state.pop(f"{pi}/{key}", None)


class Stats:
    def __init__(self):
        self.start_time = time.time()
        self.groups_total = 0
        self.last_at = None        # time of the latest group
        self.lock = threading.Lock()
        self._window = []          # (time, total) for the recent rate

    def inc(self):
        with self.lock:
            self.groups_total += 1
            self.last_at = time.time()

    def snapshot(self):
        with self.lock:
            now = time.time()
            elapsed = now - self.start_time
            self._window.append((now, self.groups_total))
            self._window = [w for w in self._window if now - w[0] <= 120]
            t0, n0 = self._window[0]
            recent = ((self.groups_total - n0) / (now - t0)) if now - t0 >= 5 else (
                self.groups_total / elapsed if elapsed > 0 else 0.0)
            return {"uptime_sec": int(elapsed), "groups_total": self.groups_total,
                    "groups_per_sec": round(recent, 1),
                    "last_group_age_sec": (int(now - self.last_at)
                                           if self.last_at is not None else None)}


_INFO_KEYS = ("ps", "long_ps", "prog_type", "tp", "ta", "country", "radiotext")


class Station:
    """One frequency on one receiver: its rules engine, decoded fields and
    decode rate, fed line by line from its redsea."""

    def __init__(self, receiver_id, frequency, recorder=None, record_types=None):
        self.receiver_id = receiver_id
        self.frequency = frequency
        self.state = StationState()
        self.stats = Stats()
        self.info = {}
        self.engine = RulesEngine(recorder=recorder, record_event_types=record_types,
                                  frequency=frequency, receiver=receiver_id)
        self.engine.station = self
        self._errors = 0
        self._first = True
        self._last_log = time.time()
        self._lock = threading.Lock()

    def update_info(self, pi, data):
        with self._lock:
            new = self.info.get("pi") != pi
            if new:
                self.info = {"pi": pi}
                log.info("[%s %s] PI %s", self.receiver_id, self.frequency, pi)
            had_ps = "ps" in self.info
            if "ps" in data:
                self.info["ps"] = data["ps"].strip()
            elif "partial_ps" in data and "ps" not in self.info:
                self.info["ps"] = data["partial_ps"].strip()
            for key in ("long_ps", "radiotext"):
                if key in data:
                    self.info[key] = str(data[key]).strip()
            for key in ("prog_type", "tp", "ta", "country"):
                if key in data:
                    self.info[key] = data[key]
            if not had_ps and "ps" in self.info:
                log.info("[%s %s] Locked on to %s (PI %s, PTY %s, TP %s)",
                         self.receiver_id, self.frequency, self.info["ps"], pi,
                         self.info.get("prog_type", "?"),
                         "yes" if self.info.get("tp") else "no")

    def snapshot(self):
        with self._lock:
            info = dict(self.info)
        snap = self.stats.snapshot()
        return {"frequency": self.frequency, "pi": info.get("pi"),
                **{k: info.get(k) for k in _INFO_KEYS},
                "groups_per_sec": snap["groups_per_sec"],
                "groups_total": snap["groups_total"],
                "last_group_age_sec": snap.get("last_group_age_sec")}

    def on_line(self, raw_line):
        """One line of redsea JSON (the station's reader thread)."""
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            return
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            self._errors += 1
            if self._errors <= 5:
                log.warning("[%s %s] invalid JSON: %s", self.receiver_id,
                            self.frequency, line[:100])
            return
        try:
            process_group(self, data)
        except Exception:
            log.exception("[%s %s] error processing a group", self.receiver_id,
                          self.frequency)
            return
        if self._first and data.get("pi"):
            self._first = False
            log.info("[%s %s] first RDS group: decoder running", self.receiver_id,
                     self.frequency)
        now = time.time()
        if now - self._last_log >= 300:
            self._last_log = now
            s = self.snapshot()
            log.info("[%s %s] %s: %d groups, %.1f grp/s", self.receiver_id,
                     self.frequency, s.get("ps") or s.get("pi") or "?",
                     s["groups_total"], s["groups_per_sec"])


# ---------------------------------------------------------------------------
# Rules: RDS -> events
# ---------------------------------------------------------------------------

class RulesEngine:
    """Turns one station's RDS into events (stored and queued for the
    aggregator), and starts and stops its recordings.

      1. TA on:  traffic announcement starts
      2. RadioText while TA is on: the announcement is updated
      3. TA off: it ends (shorter than 10 s: discarded as noise)
      4. PTY Alarm: emergency broadcast starts; PTY back: it ends
    """

    def __init__(self, recorder=None, record_event_types=None, frequency=None,
                 receiver=None):
        self.lock = threading.Lock()
        self._active = {}            # pi -> {event_id, since, radiotext}
        self._active_emergency = {}  # pi -> {event_id, since}
        self._recorder = recorder
        self._record_types = {t.strip() for t in
                              (record_event_types or "traffic,emergency").split(",")}
        self.frequency = frequency
        self.receiver = receiver
        self.station = None

    def _ps(self, pi):
        info = self.station.info if self.station else {}
        return info.get("ps") if info.get("pi") == pi else None

    def _record(self, kind, event_id):
        if self._recorder and kind in self._record_types:
            self._recorder.start(event_id)

    def _stop_recording(self):
        return bool(self._recorder and self._recorder.stop())

    def on_ta_change(self, pi, ta, data):
        ts = msg_ts(data)
        with self.lock:
            if ta:
                event_id = event_store.insert_event(
                    event_type="traffic", severity="warning", state="start", pi=pi,
                    data_payload={"prog_type": data.get("prog_type", "")},
                    station_ps=self._ps(pi), frequency=self.frequency,
                    started_at=ts, receiver=self.receiver)
                self._active[pi] = {"event_id": event_id, "since": ts, "radiotext": []}
                self._record("traffic", event_id)
                log.info("EVENT traffic start on %s %s (#%d)", pi, self.frequency, event_id)
                return
            ann = self._active.pop(pi, None)
            if not ann:
                return
            self._stop_recording()
            duration = self._duration(ann["since"], ts)
            if duration < MIN_EVENT_SEC:
                event_store.delete_event(ann["event_id"])
                log.info("Discarded short traffic event #%d on %s (%ds)",
                         ann["event_id"], pi, duration)
                return
            event_store.end_event(event_id=ann["event_id"], ended_at=ts,
                                  duration_sec=duration,
                                  radiotext_list=ann["radiotext"])
            log.info("EVENT traffic end on %s (%ds, %d RadioText)", pi, duration,
                     len(ann["radiotext"]))

    def on_radiotext(self, pi, rt, data):
        with self.lock:
            ann = self._active.get(pi)
            if not ann:
                return
            if not ann["radiotext"] or ann["radiotext"][-1] != rt:
                ann["radiotext"].append(rt)
            event_store.update_event_radiotext(ann["event_id"], list(ann["radiotext"]))
            log.info("EVENT traffic update on %s: %s", pi, rt[:80])

    def on_pty_alert(self, pi, pty, data):
        ts = msg_ts(data)
        with self.lock:
            if pi in self._active_emergency:
                return
            event_id = event_store.insert_event(
                event_type="emergency", severity="critical", state="active", pi=pi,
                data_payload={"prog_type": pty}, station_ps=self._ps(pi),
                frequency=self.frequency, started_at=ts, receiver=self.receiver)
            self._active_emergency[pi] = {"event_id": event_id, "since": ts}
        self._record("emergency", event_id)
        log.warning("EVENT emergency (PTY %s) on %s %s (#%d)", pty, pi,
                    self.frequency, event_id)

    def on_pty_normal(self, pi, pty, data):
        ts = msg_ts(data)
        with self.lock:
            em = self._active_emergency.pop(pi, None)
        if not em:
            return
        self._stop_recording()
        duration = self._duration(em["since"], ts)
        if duration < MIN_EVENT_SEC:
            event_store.delete_event(em["event_id"])
            log.info("Discarded short emergency event #%d on %s (%ds)",
                     em["event_id"], pi, duration)
            return
        event_store.end_event(event_id=em["event_id"], ended_at=ts, duration_sec=duration)
        log.info("EVENT emergency end on %s (PTY %s, %ds)", pi, pty, duration)

    def is_active(self, pi):
        with self.lock:
            return pi in self._active

    def is_emergency_active(self, pi):
        with self.lock:
            return pi in self._active_emergency

    def busy(self):
        """A broadcast is going on (no calibration now)."""
        with self.lock:
            return bool(self._active or self._active_emergency)

    @staticmethod
    def _duration(start, end):
        try:
            fmt = "%Y-%m-%dT%H:%M:%S"
            return max(0, int(time.mktime(time.strptime(end[:19], fmt))
                              - time.mktime(time.strptime(start[:19], fmt))))
        except Exception:
            return 0


def process_group(station, data):
    """Route one decoded RDS group through the station's rules."""
    pi = data.get("pi")
    if not pi:
        return
    pi = str(pi)
    station.stats.inc()
    station.update_info(pi, data)
    state, engine = station.state, station.engine
    group = data.get("group", "")

    # 1-3: traffic announcements. The first TA value seen (start-up, a new
    # PI) only says where we are: off ends nothing, on starts the event.
    if "ta" in data:
        known = state.is_known(pi, "ta")
        if state.changed(pi, "ta", data["ta"]) and (known or data["ta"]):
            engine.on_ta_change(pi, data["ta"], data)
            if data["ta"]:
                # A new announcement takes the RadioText on air now, even
                # if the same text was sent before
                state.forget(pi, "rt")

    if group in ("2A", "2B"):
        rt = data.get("radiotext")      # complete RadioText only
        if rt and state.changed(pi, "rt", rt) and engine.is_active(pi):
            engine.on_radiotext(pi, rt.strip(), data)

    # 4: emergency broadcasts
    pty = data.get("prog_type")
    if pty and state.changed(pi, "pty", pty):
        if pty in ALERT_PTY:
            engine.on_pty_alert(pi, pty, data)
        elif engine.is_emergency_active(pi):
            engine.on_pty_normal(pi, pty, data)


# ---------------------------------------------------------------------------
# The satellite
# ---------------------------------------------------------------------------

class Satellite:
    """The receivers, their pipelines and stations, and the uplink."""

    def __init__(self):
        self.started = time.time()
        self.receivers = []          # receivers.Receiver
        self.pipelines = []          # pipeline.ReceiverPipeline
        self.error = None            # why no receiver runs (no dongle …)
        self.uplink = None
        self.tuner = None            # gain_tuner.Tuner (set in main)

    def stations(self):
        return [st for p in self.pipelines for st in (s["station"] for s in p.stations)]

    def receiver_status(self):
        out = []
        for p in self.pipelines:
            pipe = p.status.snapshot()
            cal = self.tuner.describe(p) if self.tuner else {}
            out.append({
                "id": p.receiver.id, "serial": p.receiver.serial,
                "name": p.receiver.name or None,
                "state": pipe["state"], "error": pipe["error"],
                **cal,
                "stations": [s["station"].snapshot() for s in p.stations],
            })
        return out

    def status(self):
        """For the heartbeat and the health file."""
        recs = self.receiver_status()
        stations = [st for r in recs for st in r["stations"]]
        states = [r["state"] for r in recs]
        worst = ("error" if "error" in states else
                 "running" if states and all(s == "running" for s in states) else
                 (states[0] if states else "error"))
        errors = [f"{r['id']}: {r['error']}" for r in recs if r.get("error")]
        if self.error:
            errors.insert(0, self.error)
        return {
            "uptime_sec": int(time.time() - self.started),
            "receivers": recs,
            # For aggregators and node cards that read the flat shape
            "pipeline": {"state": worst, "error": "; ".join(errors)[:256] or None},
            "stations": [{k: st.get(k) for k in
                          ("frequency", "pi", "ps", "long_ps", "prog_type", "tp", "ta",
                           "groups_per_sec", "groups_total")} for st in stations],
        }


def _housekeeping(sat, stop_event):
    import satellite_health
    last_purge = 0.0
    while not stop_event.is_set():
        now = time.time()
        try:
            snap = sat.status()
            snap["uplink"] = sat.uplink.snapshot() if sat.uplink else None
            satellite_health.write(config.HEALTH_FILE, snap)
        except Exception:
            log.debug("Could not write the health file", exc_info=True)
        if now - last_purge >= 3600:
            last_purge = now
            try:
                event_store.purge(config.SATELLITE_KEEP_DAYS, config.SPOOL_DIR)
            except Exception:
                log.exception("Purge failed")
        stop_event.wait(10)


def build_pipelines(sat, receivers_list):
    from audio_recorder import AudioRecorder
    from pipeline import ReceiverPipeline

    pipelines = []
    for r in receivers_list:
        for w in r.warnings:
            log.warning("[%s] %s", r.id, w)
        stations = []
        for freq in r.frequencies:
            rec = AudioRecorder(config.SPOOL_DIR, max_duration_sec=config.MAX_RECORDING_SEC)
            st = Station(r.id, freq, recorder=rec, record_types=config.RECORD_EVENT_TYPES)
            stations.append({"frequency": freq, "callback": st.on_line,
                             "recorder": rec, "station": st})
        settings = ((lambda radio=None, r=r: sat.tuner.settings(r, radio)) if sat.tuner
                    else (lambda radio=None, r=r: (
                        r.gain if r.gain != "auto" else getattr(radio, "default_gain", 20.0),
                        r.ppm if r.ppm != "auto" else 0)))
        pipelines.append(ReceiverPipeline(r, stations, settings))
    return pipelines


def main():
    for msg in config.problems():
        log.error(msg)
        sys.exit(1)

    log.info("════════════════════════════════════════════")
    log.info("  RDS Guard satellite %s", config.BUILD_VERSION)
    log.info("  Aggregator: %s", config.UPLINK_URL)
    log.info("  Node name:  %s", config.NODE_NAME or "(not set)")
    log.info("════════════════════════════════════════════")
    retired = config.retired_in_use()
    if retired:
        log.warning("Not used by the satellite any more (the aggregator does this): %s",
                    ", ".join(retired))

    import receivers as receivers_mod
    from uplink import Uplink

    event_store.init_db(config.DB_PATH)
    event_store.migrate_from(config.OLD_DB_PATH, config.OLD_AUDIO_DIR, config.SPOOL_DIR)

    sat = Satellite()
    sat.uplink = Uplink(
        base_url=config.UPLINK_URL, token=config.UPLINK_TOKEN,
        audio_dir=config.SPOOL_DIR, node_name=config.NODE_NAME,
        version=config.BUILD_VERSION, status_fn=sat.status,
        heartbeat_sec=config.UPLINK_HEARTBEAT_SEC,
        max_age_hours=config.UPLINK_MAX_AGE_HOURS, join_key=config.JOIN_KEY,
        suggested_location=config.NODE_LOCATION or None)
    event_store.enable_outbox(sat.uplink.notify)
    event_store.close_stale_events()

    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    sat.uplink.start(stop_event)
    threading.Thread(target=_housekeeping, args=(sat, stop_event), daemon=True,
                     name="housekeeping").start()

    # The receivers: retried until a dongle is there (the heartbeat says why)
    while not stop_event.is_set():
        try:
            found = receivers_mod.load()
            sat.error = None
            break
        except Exception as e:
            sat.error = str(e)
            log.error("%s (trying again in 60 s)", e)
            stop_event.wait(60)
    if stop_event.is_set():
        return

    sat.receivers = found
    try:
        import gain_tuner
        sat.tuner = gain_tuner.Tuner(sat)
    except ImportError:
        sat.tuner = None
    sat.pipelines = build_pipelines(sat, found)
    if sat.tuner:
        sat.uplink.on_commands = sat.tuner.on_commands
    for p in sat.pipelines:
        log.info("Receiver %s (serial %s): %s", p.receiver.id, p.receiver.serial,
                 ", ".join(p.receiver.frequencies))
        threading.Thread(target=p.run, args=(stop_event,), daemon=True,
                         name=f"pipeline-{p.receiver.id}").start()
    if sat.tuner:
        threading.Thread(target=sat.tuner.run, args=(stop_event,), daemon=True,
                         name="gain-tuner").start()

    log.info("RDS Guard satellite running")
    stop_event.wait()
    log.info("Stopping …")
    time.sleep(1)


if __name__ == "__main__":
    sys.modules["rds_guard"] = sys.modules[__name__]
    main()
