"""Automatic gain and frequency correction (ppm) for each RTL-SDR and Airspy.

FM broadcasters are strong: too much gain overloads the tuner and every
station on the dongle decodes worse, too little leaves RDS in the noise, and
the tuner's own AGC ("-1") is often wrong. So each dongle is measured where
it stands, with what counts in the end: decoded RDS.

  1. ppm: the FM discriminator's mean is each carrier's offset from where
     the dongle thinks it is; the median over the stations, in ppm.
  2. A sweep over every gain step the tuner allows (about 1 s each): ADC
     clipping, and per station the SNR of the 19 kHz pilot and the 57 kHz
     RDS subcarrier in the demodulated multiplex.
  3. The 4 steps with the best *worst* RDS SNR (no clipping) are decoded
     with redsea for 8 s per station: groups per second and block errors.
  4. The winner has the best worst-station result (no station is given up
     for another); within 5 % the lower gain wins, for headroom.

It runs at the first start of a dongle (or when its frequencies change),
when a station's decoding stays below 60 % of what the calibration measured
for 15 minutes (at most every 6 hours), and when the aggregator's owner asks
(Admin -> Noder -> Kalibrera om). Never during a broadcast on that dongle;
the other dongles keep running. gain:/ppm: in receivers.yml override it.

    docker compose exec rds-guard python3 -m gain_tuner --serial 00000101

runs it by hand (with the satellite stopped, or the dongle is busy) and
prints a line per step; --save stores the result.
"""

import json
import logging
import subprocess
import sys
import threading
import time

import numpy as np

import config
import dongles
import sdr

log = logging.getLogger("rds-guard")

FS = config.RTL_SAMPLE_RATE
MPX_RATE = 171_000
START_GAIN = 8.7             # dB, until the first calibration (and for the ppm capture):
                             # low enough not to clip on a strong station
CLIP_LIMIT = 0.0005           # fraction of ADC samples at 0 or 255
VERIFY_STEPS = 4
VERIFY_SEC = 8
SWEEP_SEC = 1.0
PPM_SEC = 3.0
PPM_LIMIT = 150
HEARD_PILOT_DB = 20           # a station counts as heard with a pilot this clear
TIE = 0.05                    # within 5 %: the lower gain
LOW_QUALITY = 0.6             # of the calibrated rate …
LOW_FOR_SEC = 15 * 60         # … for this long: calibrate again
MIN_BETWEEN_SEC = 6 * 3600
RETRY_AFTER_FAIL_SEC = 3 * 3600
FALLBACK_GAINS = [0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7,
                  20.7, 22.9, 25.4, 28.0, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2,
                  42.1, 43.4, 43.9, 44.5, 48.0, 49.6]     # R820T


def say(msg):
    log.info("%s", msg)


# ---------------------------------------------------------------------------
# Measurements (pure functions on samples)
# ---------------------------------------------------------------------------

def clip_fraction(raw):
    """Share of the 8-bit ADC samples at either end (the ADC overloads)."""
    return sdr.RtlSdr.clip_fraction(raw)


def level_dbfs(z):
    p = float(np.mean(np.abs(z) ** 2)) if len(z) else 0.0
    return 10 * np.log10(p) if p > 0 else -120.0


def ppm_from_mpx(mpx, freq_hz):
    """The dongle's frequency error from one station's multiplex: its mean
    is the carrier's offset (radians per sample at 171 kHz)."""
    if not len(mpx):
        return None
    offset_hz = float(np.mean(mpx)) * MPX_RATE / (2 * np.pi)
    return -offset_hz / freq_hz * 1e6


def _psd(mpx, nfft=8192):
    """Averaged power spectrum of the multiplex (one-sided)."""
    if len(mpx) < nfft:
        nfft = 1 << int(np.log2(max(len(mpx), 256)))
    segs = len(mpx) // nfft
    if segs == 0:
        return None, None
    win = np.hanning(nfft).astype(np.float32)
    x = mpx[:segs * nfft].reshape(segs, nfft) * win
    p = np.mean(np.abs(np.fft.rfft(x, axis=1)) ** 2, axis=0)
    f = np.fft.rfftfreq(nfft, 1 / MPX_RATE)
    return f, p


def _band(f, p, lo, hi):
    m = (f >= lo) & (f <= hi)
    return float(np.mean(p[m])) if m.any() else 0.0


def tone_snr_db(mpx):
    """(pilot SNR, RDS SNR) in dB from the multiplex spectrum: the 19 kHz
    pilot and the 57 kHz RDS subcarrier against the empty bands next to
    them."""
    f, p = _psd(mpx)
    if f is None:
        return 0.0, 0.0
    floor_pilot = (_band(f, p, 16_500, 18_500) + _band(f, p, 19_500, 21_500)) / 2
    floor_rds = _band(f, p, 61_000, 70_000)
    pilot = _band(f, p, 18_950, 19_050)
    rds = _band(f, p, 55_500, 58_500)

    def db(a, b):
        return 10 * np.log10(a / b) if a > 0 and b > 0 else 0.0
    return round(db(pilot, floor_pilot), 1), round(db(rds, floor_rds), 1)


def redsea_quality(pcm, seconds, run=None):
    """Decode a station's PCM with redsea: (groups per second, BLER %)."""
    run = run or (lambda data: subprocess.run(
        ["redsea", "-r", "171k", "-E"], input=data, capture_output=True,
        timeout=seconds * 4 + 20).stdout.decode("utf-8", "replace"))
    out = run(pcm.tobytes())
    groups, bler = 0, None
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("pi"):
            groups += 1
        if isinstance(d.get("bler"), (int, float)):
            bler = d["bler"]
    return round(groups / seconds, 2), bler


def choose(results):
    """The gain to use from the verified steps:
    results = [{"gain", "stations": {freq: gps}}]. Best worst station; within
    TIE the lower gain. A station that decodes at none of the steps does not
    count (it would make every step score 0). Returns (gain, dead stations),
    or None when no station decodes at all."""
    freqs = {f for r in results for f in r["stations"]}
    dead = sorted(f for f in freqs
                  if not any((r["stations"].get(f) or 0) > 0 for r in results))
    live = [f for f in freqs if f not in dead]
    if not live:
        return None
    scored = [(min(r["stations"].get(f) or 0.0 for f in live), r["gain"]) for r in results]
    scored = [x for x in scored if x[0] > 0]
    if not scored:
        return None
    best = max(x[0] for x in scored)
    return min(g for x, g in scored if x >= best * (1 - TIE)), dead


# ---------------------------------------------------------------------------
# A calibration run
# ---------------------------------------------------------------------------

def capture(index, center_hz, gain, ppm, seconds):
    """IQ bytes from rtl_sdr (the first quarter second dropped: settling)."""
    return sdr.capture(sdr.RtlSdr(index), center_hz, gain, ppm, seconds,
                       run=subprocess.run)


def calibrate(receiver, index, gains, capture_fn=None, quality_fn=redsea_quality,
              progress=say, want_gain=True, want_ppm=True, gain=None, ppm=None,
              radio=None):
    """Measure ppm and gain for one receiver. Returns a dict: gain, ppm,
    stations ({freq: groups/s at the chosen gain}), steps (the sweep).
    radio: sdr.RtlSdr (default, at index) or sdr.Airspy (no ppm)."""
    from channelizer import WIDE_FROM, demod_many

    radio = radio or sdr.RtlSdr(index)
    # At 6 MS/s and more a capture is decoded while it comes in (8 s at
    # 10 MS/s would be 320 MB of IQ)
    streaming = capture_fn is None and radio.fs >= WIDE_FROM
    if capture_fn is None:
        capture_fn = (lambda i, c, g, p, s: sdr.capture(radio, c, g, p, s,
                                                        run=subprocess.run))
    start_gain = radio.default_gain
    if not radio.has_ppm:
        want_ppm, ppm = False, 0
    freqs = list(zip(receiver.frequencies, receiver.freqs_hz))
    center = receiver.center_hz
    rid = receiver.id

    def mpx_of(raw):
        z = radio.iq(raw)
        return dict(zip(dict(freqs), demod_many(z, [hz - center for _, hz in freqs],
                                                 radio.fs)))

    def measure(g, p, seconds):
        """(clip fraction, level dBFS, {freq: MPX}) of a capture."""
        if streaming:
            clip, level, mpxs = sdr.capture_measure(radio, center, g, p, seconds,
                                                     [hz - center for _, hz in freqs])
            return clip, level, dict(zip(dict(freqs), mpxs))
        raw = capture_fn(index, center, g, p, seconds)
        return (radio.clip_fraction(raw), round(level_dbfs(radio.iq(raw)), 1),
                mpx_of(raw))

    def ppm_estimates(mpx, label):
        """(freq, ppm) for every station clear enough to measure on."""
        out = []
        for f, m in mpx.items():
            pilot = tone_snr_db(m)[0]
            v = ppm_from_mpx(m, dict(freqs)[f])
            if v is not None and pilot >= HEARD_PILOT_DB:
                out.append((f, v))
        return out

    def settle_ppm(estimates):
        """The median of all estimates (steps × stations), rounded."""
        for f, _ in freqs:
            if not any(e[0] == f for e in estimates):
                progress(f"[{rid}] {f}: pilot never above {HEARD_PILOT_DB} dB, too weak to "
                         "measure ppm on")
        values = sorted(v for _, v in estimates)
        k = len(values)
        if not k:
            progress(f"[{rid}] no station clear enough to measure ppm on: using 0")
            return 0
        measured = values[k // 2] if k % 2 else (values[k // 2 - 1] + values[k // 2]) / 2
        if abs(measured) > PPM_LIMIT:
            progress(f"[{rid}] ppm {measured:.0f} is not believable: using 0")
            return 0
        per = {}
        for f, v in estimates:
            per.setdefault(f, []).append(v)
        progress(f"[{rid}] ppm {int(round(measured))} (median of {k} measurements; per station: "
                 + ", ".join(f"{f} {sorted(v)[len(v) // 2]:.1f}" for f, v in per.items()) + ")")
        return int(round(measured))

    # Gain fixed, ppm automatic: measure ppm at that gain, and stop
    if not want_gain:
        if want_ppm:
            ppm = settle_ppm(ppm_estimates(measure(gain or start_gain, 0, PPM_SEC)[2],
                                           "fixed gain"))
        return {"gain": gain, "ppm": ppm or 0, "stations": {}, "steps": []}

    # 1. sweep, without ppm correction unless ppm is fixed (rtl_sdr takes
    # -g 0 as the tuner's automatic gain: never). The frequency error is
    # measured on the way, at every step that does not clip, on every
    # station heard clearly: far steadier than one capture at a guessed
    # gain, and it costs nothing extra.
    sweep_ppm = 0 if want_ppm else (ppm or 0)
    gains = [g for g in gains if g > 0 or not radio.zero_is_auto] or [start_gain]
    if radio.stages:
        # With LNA/Mixer/VGA partly by hand, several linearity steps give the
        # same three values: each is measured once (the lowest step)
        seen = set()
        gains = [g for g in sorted(gains)
                 if not (radio.stage_values(g) in seen or seen.add(radio.stage_values(g)))]
    steps = []
    estimates = []
    for i, g in enumerate(gains, 1):
        clip, level, mpx = measure(g, sweep_ppm, SWEEP_SEC)
        snrs = {f: tone_snr_db(m) for f, m in mpx.items()}
        if want_ppm and clip <= CLIP_LIMIT:
            estimates += ppm_estimates(mpx, g)
        steps.append({"gain": g, "clip": clip, "snr": snrs, "level": level})
        progress(f"[{rid}] {i}/{len(gains)} gain {g:4.1f} dB: clip {clip * 100:.3f} %, "
                 + ", ".join(f"{f} pilot {s[0]:.0f} dB RDS {s[1]:.0f} dB"
                             for f, s in snrs.items()))

    # 2. ppm from the sweep
    if want_ppm:
        ppm = settle_ppm(estimates)
    ppm = ppm or 0

    # 3. verify the best with redsea. Ranked by the worst station that is
    # heard at all (a clear pilot at some step): a station too far away to
    # hear would make every step equally bad
    heard = [f for f, _ in freqs
             if max(s["snr"][f][0] for s in steps) >= HEARD_PILOT_DB]
    if not heard:
        # Nothing on the air for this dongle: decoding would find nothing
        # either. Say so plainly instead of guessing a gain.
        unclipped = [s["gain"] for s in steps if s["clip"] <= CLIP_LIMIT]
        chosen = max(unclipped) if unclipped else min(gains)
        progress(f"[{rid}] no FM station heard on this dongle at any gain (pilot below "
                 f"{HEARD_PILOT_DB} dB on every frequency): antenna, FM filter or driver? "
                 f"Using {chosen:g} dB")
        return {"gain": chosen, "ppm": ppm, "stations": {}, "dead": list(dict(freqs)),
                "silent": True,
                "steps": [{"gain": s["gain"], "clip": s["clip"]} for s in steps]}
    for s in steps:
        s["worst_rds"] = min(s["snr"][f][1] for f in heard)
    clean = [s for s in steps if s["clip"] <= CLIP_LIMIT] or steps
    best = sorted(clean, key=lambda s: (-s["worst_rds"], s["gain"]))[:VERIFY_STEPS]
    results = []
    for s in sorted(best, key=lambda s: s["gain"]):
        from channelizer import to_pcm
        stations = {}
        for f, m in measure(s["gain"], ppm, VERIFY_SEC)[2].items():
            gps, bler = quality_fn(to_pcm(m), VERIFY_SEC)
            stations[f] = gps
        results.append({"gain": s["gain"], "stations": stations})
        progress(f"[{rid}] check gain {s['gain']:4.1f} dB: "
                 + ", ".join(f"{f} {v:.1f} grp/s" for f, v in stations.items()))

    picked = choose(results)
    dead = []
    if picked is None:
        # Nothing decodes (antenna? frequencies?): the most sensitive step
        # that does not overload
        unclipped = [s["gain"] for s in steps if s["clip"] <= CLIP_LIMIT]
        chosen = max(unclipped) if unclipped else min(gains)
        progress(f"[{rid}] no station decoded at any gain (antenna? frequencies?): "
                 f"using {chosen:g} dB, the highest that does not clip")
        stations = {}
    else:
        chosen, dead = picked
        for f in dead:
            progress(f"[{rid}] {f}: no RDS at any gain - gain chosen for the others")
        stations = next(r["stations"] for r in results if r["gain"] == chosen)
        progress(f"[{rid}] chosen: gain {radio.describe_gain(chosen)}, ppm {ppm} ("
                 + ", ".join(f"{f} {v:.1f} grp/s" for f, v in stations.items()) + ")")
    return {"gain": chosen, "ppm": ppm, "stations": stations, "dead": dead,
            "steps": [{"gain": s["gain"], "clip": s["clip"], "worst_rds": s["worst_rds"]}
                      for s in steps]}


# ---------------------------------------------------------------------------
# On the satellite: when to calibrate, and what to use meanwhile
# ---------------------------------------------------------------------------

class Tuner:
    """Keeps every receiver's gain and ppm right."""

    def __init__(self, sat, calibrate_fn=None, now=time.time):
        self.sat = sat
        self._calibrate = calibrate_fn or self._calibrate_dongle
        self._now = now
        self._requests = {}          # receiver id -> why
        self._seen_commands = set()
        self._low_since = {}         # (receiver id, freq) -> time
        self._busy = {}              # receiver id -> status text
        self._failed = {}            # receiver id -> (time, error)
        self._lock = threading.Lock()

    # -- what the pipeline uses ---------------------------------------------

    def _stored(self, r):
        import event_store
        cal = event_store.get_calibration(r.serial)
        result = (cal or {}).get("result") or {}
        if cal and sorted(cal["frequencies"]) == sorted(r.frequencies) and \
                (result.get("stages") or {}) == _stages(r) and \
                result.get("sample_rate") == getattr(r, "sample_rate", None):
            # (an Airspy's stages by hand and sample rate: the same ones; at
            # 10 MS/s the ADC sees far more of the band than at 2.5)
            return cal
        return None

    def settings(self, r, radio=None):
        """(gain, ppm) for the receiver's pipeline (radio: its sdr.RtlSdr or
        sdr.Airspy, for the gain to use before the calibration)."""
        cal = self._stored(r)
        start = radio.default_gain if radio is not None else START_GAIN
        gain = r.gain if r.gain != "auto" else (cal["gain"] if cal and cal["gain"] is not None
                                                else start)
        ppm = r.ppm if r.ppm != "auto" else (cal["ppm"] if cal and cal["ppm"] is not None else 0)
        return gain, ppm

    def describe(self, p):
        """The receiver's calibration, for the heartbeat."""
        r = p.receiver
        cal = self._stored(r)
        gain, ppm = self.settings(r)
        if getattr(p, "gain", None) is not None:      # what the pipeline runs with
            gain, ppm = p.gain, p.ppm
        result = (cal or {}).get("result") or {}
        return {
            "gain": gain, "gain_mode": "manual" if _gain_fixed(r) else "auto",
            "ppm": ppm, "ppm_mode": "manual" if r.ppm != "auto" else "auto",
            "calibrated_at": cal["calibrated_at"] + "Z" if cal else None,
            "calibration": self._busy.get(r.id) or (
                f"misslyckades: {self._failed[r.id][1]}"[:64] if r.id in self._failed
                else f"ingen RDS: {', '.join(result['dead'])}"[:64] if result.get("dead")
                else None if cal or not self._auto(r) else "väntar"),
            "quality": result.get("stations") or {},
        }

    @staticmethod
    def _auto(r):
        # An Airspy has no ppm to measure: only its gain
        return (not _gain_fixed(r)
                or (r.ppm == "auto" and not dongles.is_airspy_serial(r.serial)))

    # -- requests -----------------------------------------------------------

    def on_commands(self, commands):
        """The aggregator's commands from a heartbeat answer."""
        for c in commands:
            if not isinstance(c, dict) or c.get("type") != "recalibrate":
                continue
            cid = c.get("id")
            if cid in self._seen_commands:
                continue
            self._seen_commands.add(cid)
            target = str(c.get("receiver") or "")
            for p in self.sat.pipelines:
                if target in (p.receiver.id, p.receiver.serial):
                    with self._lock:
                        self._requests[p.receiver.id] = "asked for in Admin"
                    log.info("[%s] calibration asked for by the aggregator", p.receiver.id)

    def due(self, p):
        """Why this receiver should be calibrated now, or None."""
        r = p.receiver
        if not self._auto(r):
            return None
        with self._lock:
            if r.id in self._requests:
                return self._requests[r.id]
        failed = self._failed.get(r.id)
        if failed and self._now() - failed[0] < RETRY_AFTER_FAIL_SEC:
            return None              # keep the default; try again later
        cal = self._stored(r)
        if cal is None:
            return "first start" if not self._any_stored(r) else "new frequencies"
        # Decoding well below what the calibration measured, for a while
        now = self._now()
        expected = (cal.get("result") or {}).get("stations") or {}
        last = _epoch(cal["calibrated_at"])
        low = False
        for s in p.stations:
            st = s["station"]
            want = expected.get(st.frequency)
            if not want:
                continue
            have = st.snapshot()["groups_per_sec"]
            key = (r.id, st.frequency)
            if have < want * LOW_QUALITY and p.status.snapshot()["state"] == "running":
                self._low_since.setdefault(key, now)
                if now - self._low_since[key] >= LOW_FOR_SEC:
                    low = True
            else:
                self._low_since.pop(key, None)
        if low and (last is None or now - last >= MIN_BETWEEN_SEC):
            return "decoding dropped"
        return None

    def _any_stored(self, r):
        import event_store
        return event_store.get_calibration(r.serial) is not None

    # -- running -----------------------------------------------------------

    def run(self, stop_event):
        stop_event.wait(20)          # let the pipelines start
        while not stop_event.is_set():
            for p in list(self.sat.pipelines):
                self.step(p)
            stop_event.wait(10)

    def step(self, p):
        """Calibrate this receiver now if it is due and nothing is on air.
        Returns what it did: None, 'waiting' or 'calibrated'."""
        rid = p.receiver.id
        why = self.due(p)
        if not why:
            if self._busy.get(rid) == "väntar på sändning":
                self._busy.pop(rid, None)
            return None
        if any(s["station"].engine.busy() for s in p.stations):
            self._busy[rid] = "väntar på sändning"
            return "waiting"
        self.calibrate_pipeline(p, why)
        return "calibrated"

    def calibrate_pipeline(self, p, why):
        import event_store
        r = p.receiver
        log.info("[%s] calibrating gain and ppm (%s): about a minute and a half "
                 "without this receiver", r.id, why)
        self._busy[r.id] = "kalibreras"
        try:
            if not p.pause("calibrating"):
                log.warning("[%s] the dongle did not come free; calibrating anyway", r.id)
            res = self._calibrate(r, want_gain=not _gain_fixed(r), want_ppm=r.ppm == "auto")
            if res.get("silent"):
                # Nothing heard: keep what it had, say why, and try again later
                log.warning("[%s] nothing heard on this dongle: keeping the earlier settings "
                            "and calibrating again in %d hours", r.id, RETRY_AFTER_FAIL_SEC // 3600)
                self._failed[r.id] = (self._now(), "ingen FM-signal hörs (antenn, filter, drivrutin?)")
                return
            event_store.save_calibration(
                r.serial, r.frequencies,
                res["gain"] if not _gain_fixed(r) else None,
                res["ppm"] if r.ppm == "auto" else None,
                {"stations": res.get("stations") or {}, "steps": res.get("steps") or [],
                 "stages": _stages(r), "sample_rate": getattr(r, "sample_rate", None),
                 "dead": res.get("dead") or []},
                why, at=self._now())
            log.info("[%s] calibrated: gain %s, ppm %s", r.id, res.get("gain"), res.get("ppm"))
            self._failed.pop(r.id, None)
        except Exception as e:
            log.exception("[%s] calibration failed; keeping the earlier settings and "
                          "trying again in %d hours", r.id, RETRY_AFTER_FAIL_SEC // 3600)
            self._failed[r.id] = (self._now(), str(e) or type(e).__name__)
        finally:
            with self._lock:
                self._requests.pop(r.id, None)
            self._low_since = {k: v for k, v in self._low_since.items() if k[0] != r.id}
            self._busy.pop(r.id, None)
            p.resume()

    def _calibrate_dongle(self, r, want_gain=True, want_ppm=True):
        radio = sdr.for_receiver(r)
        radio.prepare()
        gains = radio.gains or FALLBACK_GAINS
        fixed_gain = r.gain if r.gain != "auto" else None
        fixed_ppm = r.ppm if r.ppm != "auto" else None
        return calibrate(r, radio.index, gains, want_gain=want_gain, want_ppm=want_ppm,
                         gain=fixed_gain, ppm=fixed_ppm, radio=radio)


def _stages(r):
    return dict(getattr(r, "stages", None) or {})


def _gain_fixed(r):
    """gain: in receivers.yml, or an Airspy's LNA, Mixer and VGA all by hand."""
    return r.gain != "auto" or len(getattr(r, "stages", None) or {}) == 3


def _epoch(ts):
    import calendar
    try:
        return calendar.timegm(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return None


def main(argv=None):
    import argparse
    import receivers

    ap = argparse.ArgumentParser(prog="python3 -m gain_tuner",
                                 description="Find the best gain and ppm for a dongle")
    ap.add_argument("--serial", required=True, help="the dongle's serial number")
    ap.add_argument("--frequencies", help="comma-separated (default: from receivers.yml)")
    ap.add_argument("--save", action="store_true",
                    help="store the result for the satellite (else only show it)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    try:
        rs = {r.serial: r for r in receivers.load()}
    except receivers.ReceiverError:
        rs = {}
    if args.frequencies:
        r = receivers.parse({"receivers": [{"serial": args.serial,
                                            "frequencies": args.frequencies.split(",")}]})[0]
    elif args.serial in rs:
        r = rs[args.serial]
    else:
        sys.exit(f"Serial {args.serial} is not in receivers.yml: give --frequencies")
    try:
        radio = sdr.for_receiver(r)
        radio.prepare()
        gains = radio.gains or FALLBACK_GAINS
    except dongles.DongleError as e:
        sys.exit(str(e))
    print(f"{radio.model}, serial {radio.serial}", file=sys.stderr, flush=True)
    res = calibrate(r, radio.index, gains, radio=radio,
                    progress=lambda m: print(m, file=sys.stderr, flush=True))
    print(json.dumps({k: res[k] for k in ("gain", "ppm", "stations")}, ensure_ascii=False))
    if args.save:
        import event_store
        event_store.init_db(config.DB_PATH)
        event_store.save_calibration(r.serial, r.frequencies, res["gain"], res["ppm"],
                                     {"stations": res["stations"], "steps": res["steps"],
                                      "stages": _stages(r),
                                      "sample_rate": getattr(r, "sample_rate", None)},
                                     "by hand")
        print("Saved: the satellite uses it from its next start.", file=sys.stderr)


if __name__ == "__main__":
    main()
