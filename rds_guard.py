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
SILENT_END_SEC = 180        # no RDS this long: an open broadcast ends
RADIOTEXT_STALE_SEC = 60    # no RDS this long: the status report drops the RadioText
STATION_KEY = "station"     # TA/RT/PTY state is per station, not per PI
TA_CONFIRM_GROUPS = 3       # TA changes when this many groups in a row agree
PTY_CONFIRM_GROUPS = 3      # so does the programme type (Alarm: a VMA push)


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


_INFO_KEYS = ("ps", "long_ps", "prog_type", "tp", "ta", "country", "radiotext",
              "af", "af_regional")


def _mhz(value):
    """An AF as redsea writes it (kHz, or MHz in older versions) -> '103.5M'."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    mhz = v / 1000 if v > 1000 else v
    return f"{mhz:g}M" if 64 <= mhz <= 110 else None


def alternative_frequencies(data):
    """(same programme, regional variants) from a redsea group: the AF list
    the transmitter sends (method A: a list; method B: an object with the
    tuned frequency and, per frequency, whether it is the same programme or a
    regional variant). Lists of '103.5M', possibly empty."""
    same, regional = [], []
    for key, value in data.items():
        if not key.startswith("alt_"):
            continue
        if isinstance(value, dict):
            for sub, freqs in value.items():
                target = regional if "regional" in sub else same
                for f in (freqs if isinstance(freqs, list) else [freqs]):
                    m = _mhz(f)
                    if m:
                        target.append(m)
        elif isinstance(value, list):
            same += [m for m in (_mhz(f) for f in value) if m]
    return same, regional


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
        self.ta = None               # the confirmed TA flag (None: not yet)
        self._ta_run = None          # [value, first group, count] not yet confirmed
        self.pty = None              # the confirmed programme type
        self._pty_run = None
        self.ta_blips = 0            # single groups that disagreed, since the last log
        self._blip_logged = 0.0
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
            elif "partial_ps" in data and "ps" not in self.info \
                    and data["partial_ps"].strip():
                # A PS not yet complete; blanks are no name ("Locked on to  ")
                self.info["ps"] = data["partial_ps"].strip()
            for key in ("long_ps", "radiotext"):
                if key in data:
                    self.info[key] = str(data[key]).strip()
            for key in ("prog_type", "tp", "ta", "country"):
                if key in data:
                    self.info[key] = data[key]
            # The AF list: the frequencies this transmitter says carry the
            # same programme (and its regional variants). It tells regions
            # apart even when they all send the shared PI 0xE224
            same, regional = alternative_frequencies(data)
            for key, found in (("af", same), ("af_regional", regional)):
                if found:
                    have = set(self.info.get(key) or ())
                    if not set(found) <= have:
                        self.info[key] = sorted(have | set(found),
                                                key=lambda f: float(f[:-1]))
            if not had_ps and "ps" in self.info:
                log.info("[%s %s] Locked on to %s (PI %s, PTY %s, TP %s)",
                         self.receiver_id, self.frequency, self.info["ps"], pi,
                         self.info.get("prog_type", "?"),
                         "yes" if self.info.get("tp") else "no")

    def confirm_ta(self, ta, data):
        """A TA flag from one group. It changes only when TA_CONFIRM_GROUPS
        TA-bearing groups in a row say so: a single corrupted group (weak
        signal) is no announcement, and does not end one either. Returns
        (previous value, first group of the run) when it changes, else None."""
        ta = bool(ta)
        if ta == self.ta:
            run, self._ta_run = self._ta_run, None
            if run:
                self._blip(run)
            return None
        run = self._ta_run
        if run and run[0] == ta:
            run[2] += 1
        else:
            run = self._ta_run = [ta, data, 1]
        if run[2] < TA_CONFIRM_GROUPS:
            return None
        self._ta_run = None
        prev, self.ta = self.ta, ta
        return prev, run[1]

    def confirm_pty(self, pty, data):
        """The programme type from one group, confirmed like TA: a single
        corrupted group must not start a VMA (its push goes out at once).
        Returns (previous, first group of the run) when it changes."""
        if pty == self.pty:
            run, self._pty_run = self._pty_run, None
            if run and run[0] in ALERT_PTY:
                log.info("[%s %s] PTY %s in %d group(s) only, ignored (group %s, BLER %s)",
                         self.receiver_id, self.frequency, run[0], run[2],
                         run[1].get("group", "?"), run[1].get("bler", "?"))
            return None
        run = self._pty_run
        if run and run[0] == pty:
            run[2] += 1
        else:
            run = self._pty_run = [pty, data, 1]
        if run[2] < PTY_CONFIRM_GROUPS:
            return None
        self._pty_run = None
        prev, self.pty = self.pty, pty
        return prev, run[1]

    def reset_ta(self):
        """Heard again after silence: the first TA only says where we are."""
        self.ta = None
        self._ta_run = None
        self.pty = None
        self._pty_run = None

    def _blip(self, run):
        self.ta_blips += 1
        now = time.time()
        if now - self._blip_logged >= 60:
            self._blip_logged = now
            first = run[1]
            log.info("[%s %s] TA %s in %d group(s) only, ignored (group %s, BLER %s)",
                     self.receiver_id, self.frequency, "on" if run[0] else "off",
                     run[2], first.get("group", "?"), first.get("bler", "?"))

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
            blips, self.ta_blips = self.ta_blips, 0
            log.info("[%s %s] %s: %d groups, %.1f grp/s%s", self.receiver_id,
                     self.frequency, s.get("ps") or s.get("pi") or "?",
                     s["groups_total"], s["groups_per_sec"],
                     f", {blips} TA blip(s) ignored" if blips else "")


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

    One station (one frequency) has at most one announcement and one
    emergency broadcast at a time, whatever PI it sends: a P4 transmitter
    switches between the shared PI 0xE224 (national programme) and its
    regional PI, and an announcement can start on one and end on the other.
    The event keeps the PI it started on.
    """

    def __init__(self, recorder=None, record_event_types=None, frequency=None,
                 receiver=None):
        self.lock = threading.Lock()
        self._active = None          # {event_id, pi, since, radiotext}
        self._active_emergency = None  # {event_id, pi, since}
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
                if self._active:
                    return
                event_id = event_store.insert_event(
                    event_type="traffic", severity="warning", state="start", pi=pi,
                    data_payload={"prog_type": data.get("prog_type", "")},
                    station_ps=self._ps(pi), frequency=self.frequency,
                    started_at=ts, receiver=self.receiver)
                self._active = {"event_id": event_id, "pi": pi, "since": ts,
                                "radiotext": [], "ta_on": 0, "ta_off": 0}
                self._record("traffic", event_id)
                log.info("EVENT traffic start on %s %s (#%d; group %s, BLER %s)", pi,
                         self.frequency, event_id, data.get("group", "?"),
                         data.get("bler", "?"))
                return
            ann, self._active = self._active, None
            if not ann:
                return
            self._stop_recording()
            duration = self._duration(ann["since"], ts)
            groups = f"TA on in {ann['ta_on']} of {ann['ta_on'] + ann['ta_off']} groups"
            if duration < MIN_EVENT_SEC:
                event_store.delete_event(ann["event_id"])
                log.info("Discarded short traffic event #%d on %s %s (%ds, %s)",
                         ann["event_id"], ann["pi"], self.frequency, duration, groups)
                return
            event_store.end_event(event_id=ann["event_id"], ended_at=ts,
                                  duration_sec=duration,
                                  radiotext_list=ann["radiotext"])
            log.info("EVENT traffic end on %s %s (%ds, %d RadioText, %s)%s", ann["pi"],
                     self.frequency, duration, len(ann["radiotext"]), groups,
                     "" if pi == ann["pi"] else f" (ended on PI {pi})")

    def on_radiotext(self, pi, rt, data):
        with self.lock:
            ann = self._active
            if not ann:
                return
            if not ann["radiotext"] or ann["radiotext"][-1] != rt:
                ann["radiotext"].append(rt)
            event_store.update_event_radiotext(ann["event_id"], list(ann["radiotext"]))
            log.info("EVENT traffic update on %s %s: %s", pi, self.frequency, rt[:80])

    def on_pty_alert(self, pi, pty, data):
        ts = msg_ts(data)
        with self.lock:
            if self._active_emergency:
                return
            event_id = event_store.insert_event(
                event_type="emergency", severity="critical", state="active", pi=pi,
                data_payload={"prog_type": pty}, station_ps=self._ps(pi),
                frequency=self.frequency, started_at=ts, receiver=self.receiver)
            self._active_emergency = {"event_id": event_id, "pi": pi, "since": ts}
        self._record("emergency", event_id)
        log.warning("EVENT emergency (PTY %s) on %s %s (#%d)", pty, pi,
                    self.frequency, event_id)

    def on_pty_normal(self, pi, pty, data):
        ts = msg_ts(data)
        with self.lock:
            em, self._active_emergency = self._active_emergency, None
        if not em:
            return
        self._stop_recording()
        duration = self._duration(em["since"], ts)
        if duration < MIN_EVENT_SEC:
            event_store.delete_event(em["event_id"])
            log.info("Discarded short emergency event #%d on %s %s (%ds)",
                     em["event_id"], em["pi"], self.frequency, duration)
            return
        event_store.end_event(event_id=em["event_id"], ended_at=ts, duration_sec=duration)
        log.info("EVENT emergency end on %s %s (PTY %s, %ds)", em["pi"],
                 self.frequency, pty, duration)

    def count_ta(self, ta):
        """One TA-bearing group during the announcement (for the end log)."""
        with self.lock:
            if self._active:
                self._active["ta_on" if ta else "ta_off"] += 1

    def end_silent(self, age_sec):
        """The station has sent nothing for age_sec (signal or decoder
        lost): end what is going on rather than leave it open."""
        with self.lock:
            open_ = bool(self._active or self._active_emergency)
        if not open_:
            return False
        log.warning("[%s %s] no RDS for %ds: ending the open broadcast",
                    self.receiver, self.frequency, age_sec)
        # It ended when the station was last heard
        data = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S",
                                           time.gmtime(time.time() - age_sec))}
        with self.lock:
            ann = self._active
        if ann:
            self.on_ta_change(ann["pi"], False, data)
        with self.lock:
            em = self._active_emergency
        if em:
            self.on_pty_normal(em["pi"], "(no RDS)", data)
        return True

    def is_active(self, pi=None):
        with self.lock:
            return self._active is not None

    def is_emergency_active(self, pi=None):
        with self.lock:
            return self._active_emergency is not None

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
    """Route one decoded RDS group through the station's rules.

    TA, RadioText and PTY are followed per station (frequency), not per
    PI: P4 switches PI between its national and regional programme, also
    in the middle of an announcement."""
    pi = data.get("pi")
    if not pi:
        return
    pi = str(pi)
    station.stats.inc()
    station.update_info(pi, data)
    state, engine = station.state, station.engine
    group = data.get("group", "")
    here = STATION_KEY

    # 1-3: traffic announcements. TA changes when a few groups in a row
    # agree (Station.confirm_ta), from the first of them. The first TA
    # value (start-up) only says where we are: off ends nothing, on starts
    # the event.
    if "ta" in data:
        engine.count_ta(bool(data["ta"]))
        change = station.confirm_ta(data["ta"], data)
        if change:
            prev, first = change
            ta = station.ta
            if prev is not None or ta:
                engine.on_ta_change(str(first.get("pi") or pi), ta, first)
                if ta:
                    # A new announcement takes the RadioText on air now,
                    # even if the same text was sent before
                    state.forget(here, "rt")

    if group in ("2A", "2B"):
        rt = data.get("radiotext")      # complete RadioText only
        if rt and state.changed(here, "rt", rt) and engine.is_active():
            engine.on_radiotext(pi, rt.strip(), data)

    # 4: emergency broadcasts
    # The programme type changes when a few groups in a row agree, like TA
    pty = data.get("prog_type")
    change = station.confirm_pty(pty, data) if pty else None
    if change:
        prev, first = change
        first_pi = str(first.get("pi") or pi)
        if pty in ALERT_PTY:
            engine.on_pty_alert(first_pi, pty, first)
        elif engine.is_emergency_active():
            engine.on_pty_normal(first_pi, pty, first)


# ---------------------------------------------------------------------------
# The satellite
# ---------------------------------------------------------------------------

def on_air(snap):
    """A station snapshot for the status report: its RadioText says what is
    on air now, so it is left out once the station has been silent for
    RADIOTEXT_STALE_SEC (it is only cleared on a PI switch otherwise)."""
    age = snap.get("last_group_age_sec")
    if age is None or age >= RADIOTEXT_STALE_SEC:
        snap = dict(snap, radiotext=None)
    return snap


class Satellite:
    """The receivers, their pipelines and stations, and the uplink."""

    def __init__(self):
        self.started = time.time()
        self.receivers = []          # receivers.Receiver
        self.pipelines = []          # pipeline.ReceiverPipeline
        self.error = None            # why no receiver runs (no dongle …)
        self.uplink = None           # to the own aggregator (AGGREGATOR_URL)
        self.also = []               # to ALSO_REPORT_TO
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
                "check": (p.check[1][:64] if getattr(p, "check", None) else None),
                **cal,
                "stations": [on_air(s["station"].snapshot()) for s in p.stations],
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
                          ("frequency", "pi", "ps", "long_ps", "prog_type", "radiotext",
                           "tp", "ta", "groups_per_sec", "groups_total")}
                         for st in stations],
        }


def _housekeeping(sat, stop_event):
    import satellite_health
    last_purge = 0.0
    while not stop_event.is_set():
        now = time.time()
        try:
            snap = sat.status()
            snap["uplink"] = sat.uplink.snapshot() if sat.uplink else None
            snap["also_report_to"] = [u.snapshot() for u in sat.also]
            satellite_health.write(config.HEALTH_FILE, snap)
        except Exception:
            log.debug("Could not write the health file", exc_info=True)
        for st in sat.stations():
            try:
                age = st.stats.snapshot().get("last_group_age_sec")
                if age is not None and age >= SILENT_END_SEC:
                    if st.engine.end_silent(age):
                        # When it is heard again, its first TA says where we are
                        st.reset_ta()
            except Exception:
                log.exception("Could not check %s for silence", st.frequency)
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
    for url in config.ALSO_REPORT_TO:
        log.info("  Also to:    %s", url)
    log.info("  Node name:  %s", config.NODE_NAME or "(not set)")
    log.info("════════════════════════════════════════════")
    if config.ENV_COMMENTS_DROPPED:
        log.warning("In .env a comment stood where the value goes (KEY=  # …), taken as "
                    "empty: %s. Put the comment on a line of its own.",
                    ", ".join(config.ENV_COMMENTS_DROPPED))
    retired = config.retired_in_use()
    if retired:
        log.warning("Not used by the satellite any more (the aggregator does this): %s",
                    ", ".join(retired))

    import receivers as receivers_mod
    from uplink import Uplink

    event_store.init_db(config.DB_PATH)
    event_store.migrate_from(config.OLD_DB_PATH, config.OLD_AUDIO_DIR, config.SPOOL_DIR)

    sat = Satellite()
    common = dict(audio_dir=config.SPOOL_DIR, node_name=config.NODE_NAME,
                  version=config.BUILD_VERSION, status_fn=sat.status,
                  heartbeat_sec=config.UPLINK_HEARTBEAT_SEC,
                  max_age_hours=config.UPLINK_MAX_AGE_HOURS,
                  suggested_location=config.NODE_LOCATION or None)
    sat.uplink = Uplink(base_url=config.UPLINK_URL, token=config.UPLINK_TOKEN,
                        join_key=config.JOIN_KEY, **common)
    # Other aggregators: their own token and approval, never our JOIN_KEY
    sat.also = [Uplink(base_url=url, token="", join_key="", target=url, **common)
                for url in config.ALSO_REPORT_TO]
    links = [sat.uplink, *sat.also]
    event_store.enable_outbox(lambda: [u.notify() for u in links],
                              targets=[event_store.PRIMARY, *config.ALSO_REPORT_TO])
    event_store.close_stale_events()

    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    for u in links:
        u.start(stop_event)
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
