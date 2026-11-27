"""Automatic gain and frequency correction (ppm) for each RTL-SDR.

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

log = logging.getLogger("rds-guard")

FS = config.RTL_SAMPLE_RATE
MPX_RATE = 171_000
DEFAULT_GAIN = 20.0           # dB, when nothing decodes (no antenna …)
CLIP_LIMIT = 0.0005           # fraction of ADC samples at 0 or 255
VERIFY_STEPS = 4
VERIFY_SEC = 8
SWEEP_SEC = 1.0
PPM_SEC = 3.0
PPM_LIMIT = 150
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
    a = np.frombuffer(raw, dtype=np.uint8)
    if not len(a):
        return 0.0
    return float(np.count_nonzero((a == 0) | (a == 255))) / len(a)


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
    TIE the lower gain. None when nothing decodes."""
    scored = [(min(r["stations"].values()) if r["stations"] else 0.0, r["gain"])
              for r in results]
    scored = [s for s in scored if s[0] > 0]
    if not scored:
        return None
    best = max(s[0] for s in scored)
    return min(g for s, g in scored if s >= best * (1 - TIE))


# ---------------------------------------------------------------------------
# A calibration run
# ---------------------------------------------------------------------------

def capture(index, center_hz, gain, ppm, seconds):
    """IQ bytes from rtl_sdr (the first quarter second dropped: settling)."""
    samples = int((seconds + 0.25) * FS)
    cmd = ["rtl_sdr", "-f", str(center_hz), "-s", str(FS), "-g", f"{gain:g}"]
    if ppm:
        cmd += ["-p", str(ppm)]
    cmd += ["-d", str(index), "-n", str(samples), "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=seconds + 30)
    raw = proc.stdout
    if len(raw) < FS:      # not even half a second: the dongle is busy or gone
        raise RuntimeError(f"rtl_sdr gave no samples: "
                           f"{proc.stderr.decode('utf-8', 'replace')[-200:]}")
    return raw[int(0.25 * FS) * 2:]


def calibrate(receiver, index, gains, capture_fn=capture, quality_fn=redsea_quality,
              progress=say, want_gain=True, want_ppm=True, gain=None, ppm=None):
    """Measure ppm and gain for one receiver. Returns a dict: gain, ppm,
    stations ({freq: groups/s at the chosen gain}), steps (the sweep)."""
    from channelizer import demod_all, iq_from_u8

    freqs = list(zip(receiver.frequencies, receiver.freqs_hz))
    center = receiver.center_hz
    rid = receiver.id

    def mpx_of(raw):
        z = iq_from_u8(raw)
        return {f: demod_all(z, hz - center) for f, hz in freqs}

    # 1. ppm, at a middle gain without correction
    if want_ppm:
        mid = dongles.nearest([g for g in gains if g > 0], 20.0) if gains else DEFAULT_GAIN
        raw = capture_fn(index, center, mid, 0, PPM_SEC)
        values = [ppm_from_mpx(m, dict(freqs)[f]) for f, m in mpx_of(raw).items()]
        values = sorted(v for v in values if v is not None)
        measured = values[len(values) // 2] if values else 0.0
        if abs(measured) > PPM_LIMIT:
            progress(f"[{rid}] ppm {measured:.0f} is not believable: using 0")
            ppm = 0
        else:
            ppm = int(round(measured))
            progress(f"[{rid}] ppm {ppm} (per station: "
                     + ", ".join(f"{v:.1f}" for v in values) + ")")
    ppm = ppm or 0
    if not want_gain:
        return {"gain": gain, "ppm": ppm, "stations": {}, "steps": []}

    # 2. sweep (rtl_sdr takes -g 0 as the tuner's automatic gain: never)
    gains = [g for g in gains if g > 0] or [DEFAULT_GAIN]
    steps = []
    for i, g in enumerate(gains, 1):
        raw = capture_fn(index, center, g, ppm, SWEEP_SEC)
        clip = clip_fraction(raw)
        snrs = {f: tone_snr_db(m) for f, m in mpx_of(raw).items()}
        worst_rds = min(s[1] for s in snrs.values())
        steps.append({"gain": g, "clip": clip, "snr": snrs, "worst_rds": worst_rds,
                      "level": round(level_dbfs(iq_from_u8(raw)), 1)})
        progress(f"[{rid}] {i}/{len(gains)} gain {g:4.1f} dB: clip {clip * 100:.3f} %, "
                 + ", ".join(f"{f} pilot {s[0]:.0f} dB RDS {s[1]:.0f} dB"
                             for f, s in snrs.items()))

    # 3. verify the best with redsea
    clean = [s for s in steps if s["clip"] <= CLIP_LIMIT] or steps
    best = sorted(clean, key=lambda s: (-s["worst_rds"], s["gain"]))[:VERIFY_STEPS]
    results = []
    for s in sorted(best, key=lambda s: s["gain"]):
        raw = capture_fn(index, center, s["gain"], ppm, VERIFY_SEC)
        from channelizer import to_pcm
        stations = {}
        for f, m in mpx_of(raw).items():
            gps, bler = quality_fn(to_pcm(m), VERIFY_SEC)
            stations[f] = gps
        results.append({"gain": s["gain"], "stations": stations})
        progress(f"[{rid}] check gain {s['gain']:4.1f} dB: "
                 + ", ".join(f"{f} {v:.1f} grp/s" for f, v in stations.items()))

    chosen = choose(results)
    if chosen is None:
        chosen = dongles.nearest(gains, DEFAULT_GAIN) if gains else DEFAULT_GAIN
        progress(f"[{rid}] no station decoded at any gain (antenna? frequencies?): "
                 f"using {chosen:g} dB")
        stations = {}
    else:
        stations = next(r["stations"] for r in results if r["gain"] == chosen)
        progress(f"[{rid}] chosen: gain {chosen:g} dB, ppm {ppm} ("
                 + ", ".join(f"{f} {v:.1f} grp/s" for f, v in stations.items()) + ")")
    return {"gain": chosen, "ppm": ppm, "stations": stations,
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
        if cal and sorted(cal["frequencies"]) == sorted(r.frequencies):
            return cal
        return None

    def settings(self, r):
        """(gain, ppm) for the receiver's pipeline."""
        cal = self._stored(r)
        gain = r.gain if r.gain != "auto" else (cal["gain"] if cal and cal["gain"] is not None
                                                else DEFAULT_GAIN)
        ppm = r.ppm if r.ppm != "auto" else (cal["ppm"] if cal and cal["ppm"] is not None else 0)
        return gain, ppm

    def describe(self, p):
        """The receiver's calibration, for the heartbeat."""
        r = p.receiver
        cal = self._stored(r)
        gain, ppm = self.settings(r)
        result = (cal or {}).get("result") or {}
        return {
            "gain": gain, "gain_mode": "manual" if r.gain != "auto" else "auto",
            "ppm": ppm, "ppm_mode": "manual" if r.ppm != "auto" else "auto",
            "calibrated_at": cal["calibrated_at"] + "Z" if cal else None,
            "calibration": self._busy.get(r.id) or (
                f"misslyckades: {self._failed[r.id][1]}"[:64] if r.id in self._failed
                else None if cal or not self._auto(r) else "väntar"),
            "quality": result.get("stations") or {},
        }

    @staticmethod
    def _auto(r):
        return r.gain == "auto" or r.ppm == "auto"

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
            res = self._calibrate(r, want_gain=r.gain == "auto", want_ppm=r.ppm == "auto")
            event_store.save_calibration(
                r.serial, r.frequencies,
                res["gain"] if r.gain == "auto" else None,
                res["ppm"] if r.ppm == "auto" else None,
                {"stations": res.get("stations") or {}, "steps": res.get("steps") or []},
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
        found = dongles.find_all()
        index = dongles.index_of(r.serial, found)
        gains = dongles.gains_of(r.serial, found) or FALLBACK_GAINS
        fixed_gain = r.gain if r.gain != "auto" else None
        fixed_ppm = r.ppm if r.ppm != "auto" else None
        return calibrate(r, index, gains, want_gain=want_gain, want_ppm=want_ppm,
                         gain=fixed_gain, ppm=fixed_ppm)


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
        found = dongles.find_all()
        index = dongles.index_of(r.serial, found)
        gains = dongles.gains_of(r.serial, found) or FALLBACK_GAINS
    except dongles.DongleError as e:
        sys.exit(str(e))
    res = calibrate(r, index, gains, progress=lambda m: print(m, file=sys.stderr, flush=True))
    print(json.dumps({k: res[k] for k in ("gain", "ppm", "stations")}, ensure_ascii=False))
    if args.save:
        import event_store
        event_store.init_db(config.DB_PATH)
        event_store.save_calibration(r.serial, r.frequencies, res["gain"], res["ppm"],
                                     {"stations": res["stations"], "steps": res["steps"]},
                                     "by hand")
        print("Saved: the satellite uses it from its next start.", file=sys.stderr)


if __name__ == "__main__":
    main()
