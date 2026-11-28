"""Automatic gain and ppm: measurements on synthetic FM signals, the choice,
a whole calibration with a fake dongle, and when the satellite runs it."""

import numpy as np
import pytest

import gain_tuner as gt
import receivers
from channelizer import demod_all, iq_from_u8

FS = gt.FS
CENTER = 103_200_000


def fm_station(n, offset_hz, rds=True, pilot=True, dev=75_000, seed=0):
    """Complex baseband of one FM station: a tone as 'audio', the 19 kHz
    pilot and a 57 kHz RDS-like subcarrier, at offset_hz from the centre."""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / FS
    mpx = 0.8 * np.sin(2 * np.pi * 1000 * t)
    if pilot:
        mpx += 0.09 * np.sin(2 * np.pi * 19_000 * t)
    if rds:
        bits = np.repeat(rng.choice([-1.0, 1.0], size=n // 2000 + 1), 2000)[:n]
        mpx += 0.05 * bits * np.sin(2 * np.pi * 57_000 * t)
    phase = 2 * np.pi * np.cumsum(dev / 1.0 * mpx) / FS / 1.0
    return np.exp(1j * (2 * np.pi * offset_hz * t + phase))


def to_u8(z, gain_lin, noise=0.01, seed=1):
    """What the dongle's 8-bit ADC makes of it at a linear gain (clipping)."""
    rng = np.random.default_rng(seed)
    z = z * gain_lin + noise * (rng.standard_normal(len(z)) + 1j * rng.standard_normal(len(z)))
    i = np.clip(np.round(z.real * 127.5 + 127.5), 0, 255)
    q = np.clip(np.round(z.imag * 127.5 + 127.5), 0, 255)
    out = np.empty(2 * len(z), dtype=np.uint8)
    out[0::2], out[1::2] = i, q
    return out.tobytes()


def test_the_channelizer_picks_the_station_not_its_mirror():
    """A station above the centre comes out clean; one below it, at the
    mirror frequency, does not leak in."""
    off = 300_000
    above = fm_station(int(FS * 0.2), off, dev=0)
    mpx = demod_all(iq_from_u8(to_u8(above, 0.4)), off)
    assert mpx.std() < 0.1                     # a clean carrier, not noise
    mirror = fm_station(int(FS * 0.2), -off, dev=0)
    assert demod_all(iq_from_u8(to_u8(mirror, 0.4)), off).std() > 1.0


def test_clip_fraction():
    z = fm_station(20_000, 300_000)
    assert gt.clip_fraction(to_u8(z, 0.3)) == 0
    assert gt.clip_fraction(to_u8(z, 3.0)) > 0.5


def test_ppm_from_a_known_carrier_offset():
    freq = 103_500_000
    for true_ppm in (-40, 0, 25):
        residual = -true_ppm * freq / 1e6        # where the dongle sees it
        z = fm_station(int(FS * 0.5), (freq - CENTER) + residual)
        mpx = demod_all(iq_from_u8(to_u8(z, 0.4)), freq - CENTER)
        assert gt.ppm_from_mpx(mpx, freq) == pytest.approx(true_ppm, abs=2)


def test_pilot_and_rds_snr():
    off = 300_000
    good = demod_all(iq_from_u8(to_u8(fm_station(int(FS * 0.5), off), 0.4)), off)
    pilot, rds = gt.tone_snr_db(good)
    assert pilot > 15 and rds > 6
    none = demod_all(iq_from_u8(to_u8(fm_station(int(FS * 0.5), off, rds=False,
                                                 pilot=False), 0.4)), off)
    p2, r2 = gt.tone_snr_db(none)
    assert p2 < pilot - 10 and r2 < rds - 5


def test_choose_best_worst_station_and_lower_gain_on_ties():
    res = [{"gain": 10.0, "stations": {"a": 9.0, "b": 2.0}},     # one station given up
           {"gain": 20.0, "stations": {"a": 8.0, "b": 7.5}},
           {"gain": 30.0, "stations": {"a": 8.1, "b": 7.7}},     # within 5 %: no
           {"gain": 40.0, "stations": {"a": 3.0, "b": 3.0}}]
    assert gt.choose(res) == (20.0, [])
    assert gt.choose([{"gain": 5.0, "stations": {"a": 0.0}}]) is None


def test_a_station_that_never_decodes_does_not_decide():
    """A far, weak station with no RDS at any step: the others choose (it
    would otherwise make every step score 0)."""
    res = [{"gain": 3.0, "stations": {"near": 11.0, "far": 0.0}},
           {"gain": 12.0, "stations": {"near": 9.0, "far": 0.0}},
           {"gain": 16.0, "stations": {"near": 8.0, "far": 0.0}}]
    assert gt.choose(res) == (3.0, ["far"])
    # It decodes somewhere: then it counts, and decides
    res[2]["stations"]["far"] = 2.0
    assert gt.choose(res) == (16.0, [])


def test_redsea_quality_counts_groups_and_bler():
    out = '{"pi":"0xE824","group":"0A"}\n{"pi":"0xE824","bler":12}\nnot json\n'
    assert gt.redsea_quality(np.zeros(10, dtype=np.int16), 2, run=lambda d: out) == (1.0, 12)


class FakeDongle:
    """Captures for a calibration: a clean signal that clips above 30 dB,
    RDS decoding that improves with gain until it overloads."""

    def __init__(self, receiver, true_ppm=12):
        self.r = receiver
        self.true_ppm = true_ppm
        self.calls = []

    def capture(self, index, center, gain, ppm, seconds):
        self.calls.append((gain, ppm, seconds))
        n = int(min(seconds, 0.3) * FS)
        z = sum(fm_station(n, hz - center - (self.true_ppm - ppm) * hz / 1e6, seed=i)
                for i, hz in enumerate(self.r.freqs_hz)) / len(self.r.freqs_hz)
        return to_u8(z, 10 ** ((gain - 20) / 20) * 0.5, noise=0.05 / (1 + gain / 10))

    def quality(self, gain_for):
        def q(pcm, seconds):
            g = gain_for[0]
            return (11.0 if 15 <= g <= 30 else (6.0 if g < 15 else 2.0)), 0
        return q


def test_a_whole_calibration_with_a_fake_dongle():
    r = receivers.parse({"receivers": [{"serial": "1", "frequencies": ["103.5M", "102.9M"]}]})[0]
    dongle = FakeDongle(r)
    current = [None]

    def capture(index, center, gain, ppm, seconds):
        current[0] = gain
        return dongle.capture(index, center, gain, ppm, seconds)

    lines = []
    res = gt.calibrate(r, 0, [0.0, 8.7, 19.7, 28.0, 40.2, 49.6], capture_fn=capture,
                       quality_fn=dongle.quality(current), progress=lines.append)
    assert res["ppm"] == pytest.approx(12, abs=2)
    assert 15 <= res["gain"] <= 30
    assert set(res["stations"]) == {"103.5M", "102.9M"}
    # A line per step (0.0 dB is rtl_sdr's automatic gain: never), the checks
    # and the choice
    assert sum("gain" in line and "clip" in line for line in lines) == 5
    assert any(line.startswith("[1] chosen") for line in lines)
    # ppm was measured during the sweep (no correction then, no separate
    # capture), and the decoding checks ran with it; overloaded steps were
    # not checked
    sweep = [c for c in dongle.calls if c[2] == gt.SWEEP_SEC]
    assert {c[1] for c in sweep} == {0}
    assert not [c for c in dongle.calls if c[2] == gt.PPM_SEC]
    assert {c[1] for c in dongle.calls if c[2] == gt.VERIFY_SEC} == {res["ppm"]}
    checked = [c[0] for c in dongle.calls if c[2] == gt.VERIFY_SEC]
    assert 49.6 not in checked


def test_nothing_decodes_uses_the_highest_gain_that_does_not_clip():
    r = receivers.parse({"receivers": [{"serial": "1", "frequencies": ["103.5M"]}]})[0]
    dongle = FakeDongle(r)
    lines = []
    res = gt.calibrate(r, 0, [0.0, 8.7, 19.7, 28.0, 40.2, 49.6], capture_fn=dongle.capture,
                       quality_fn=lambda pcm, s: (0.0, None), progress=lines.append)
    clean = [s["gain"] for s in res["steps"] if s["clip"] <= gt.CLIP_LIMIT]
    assert clean and max(clean) < 49.6           # the fake clips at the top
    assert res["gain"] == max(clean)
    assert any("no station decoded" in line for line in lines)


def test_a_dead_station_is_reported_and_the_other_decides():
    r = receivers.parse({"receivers": [{"serial": "1", "frequencies": ["103.5M", "102.8M"]}]})[0]
    dongle = FakeDongle(r)
    current, n = [None], [0]

    def capture(index, center, gain, ppm, seconds):
        current[0] = gain
        return dongle.capture(index, center, gain, ppm, seconds)

    def quality(pcm, seconds):              # stations come in order: 103.5M, 102.8M
        n[0] += 1
        return (dongle.quality(current)(pcm, seconds) if n[0] % 2 else (0.0, None))

    lines = []
    res = gt.calibrate(r, 0, [0.0, 8.7, 19.7, 28.0, 40.2, 49.6], capture_fn=capture,
                       quality_fn=quality, progress=lines.append)
    assert 15 <= res["gain"] <= 30            # where 103.5M decodes best
    assert res["dead"] == ["102.8M"]
    assert any("102.8M: no RDS at any gain" in line for line in lines)


def test_ppm_is_measured_only_on_steps_that_do_not_clip():
    """Clipping distorts the carrier; those steps give no ppm estimate."""
    r = receivers.parse({"receivers": [{"serial": "1", "frequencies": ["103.5M"]}]})[0]
    dongle = FakeDongle(r, true_ppm=-3)

    def capture(index, center, gain, ppm, seconds):
        if gain > 30:                    # overload: and a carrier 40 kHz off
            return dongle.capture(index, center + 40_000, gain, ppm, seconds)
        return dongle.capture(index, center, gain, ppm, seconds)

    lines = []
    res = gt.calibrate(r, 0, [8.7, 12.5, 19.7, 40.2, 49.6], capture_fn=capture,
                       quality_fn=lambda pcm, s: (11.0, 0), progress=lines.append)
    assert res["ppm"] == pytest.approx(-3, abs=1)
    assert any("median of" in line for line in lines)


def test_fixed_ppm_is_used_in_the_sweep_and_not_measured():
    r = receivers.parse({"receivers": [{"serial": "1", "frequencies": ["103.5M"], "ppm": 7}]})[0]
    dongle = FakeDongle(r, true_ppm=7)
    res = gt.calibrate(r, 0, [8.7, 19.7], capture_fn=dongle.capture,
                       quality_fn=lambda pcm, s: (11.0, 0), progress=lambda line: None,
                       want_ppm=False, ppm=7)
    assert res["ppm"] == 7
    assert {c[1] for c in dongle.calls} == {7}


def test_fixed_gain_measures_ppm_at_that_gain():
    r = receivers.parse({"receivers": [{"serial": "1", "frequencies": ["103.5M"], "gain": 12.5}]})[0]
    dongle = FakeDongle(r, true_ppm=5)
    res = gt.calibrate(r, 0, [8.7, 12.5, 19.7], capture_fn=dongle.capture,
                       quality_fn=lambda pcm, s: (11.0, 0), progress=lambda line: None,
                       want_gain=False, gain=12.5)
    assert res["ppm"] == pytest.approx(5, abs=1)
    assert [c[0] for c in dongle.calls] == [12.5]


# ------------------------------------------------------------ on the satellite

class FakePipeline:
    def __init__(self, receiver, stations):
        import pipeline
        self.receiver = receiver
        self.stations = [{"station": st, "frequency": st.frequency} for st in stations]
        self.status = pipeline.PipelineStatus()
        self.status.set_running()
        self.paused = self.resumed = 0

    def pause(self, why=None):
        self.paused += 1
        return True

    def resume(self):
        self.resumed += 1


class Sat:
    def __init__(self, pipelines):
        self.pipelines = pipelines


@pytest.fixture
def setup(store):
    import rds_guard as rg
    r = receivers.parse({"receivers": [{"serial": "S1", "name": "tak",
                                        "frequencies": ["103.5M"]}]})[0]
    st = rg.Station("tak", "103.5M")
    p = FakePipeline(r, [st])
    clock = [1_800_000_000.0]
    runs = []

    def fake_calibrate(rec, want_gain=True, want_ppm=True):
        runs.append(rec.id)
        return {"gain": 28.0, "ppm": 7, "stations": {"103.5M": 10.0}, "steps": []}

    tuner = gt.Tuner(Sat([p]), calibrate_fn=fake_calibrate, now=lambda: clock[0])
    return tuner, p, st, clock, runs


def test_first_start_calibrates_then_uses_the_result(setup):
    tuner, p, st, clock, runs = setup
    assert tuner.settings(p.receiver) == (gt.START_GAIN, 0) == (8.7, 0)
    assert tuner.describe(p)["calibration"] == "väntar"
    assert tuner.due(p) == "first start"
    assert tuner.step(p) == "calibrated"
    assert runs == ["tak"] and p.paused == p.resumed == 1
    assert tuner.settings(p.receiver) == (28.0, 7)
    d = tuner.describe(p)
    assert d["gain_mode"] == "auto" and d["calibrated_at"] and d["quality"] == {"103.5M": 10.0}
    assert tuner.step(p) is None


def test_not_during_a_broadcast(setup, store):
    import rds_guard as rg
    tuner, p, st, clock, runs = setup
    for _ in range(rg.TA_CONFIRM_GROUPS):
        rg.process_group(st, {"pi": "0x1", "ta": True, "timestamp": "2026-11-27T10:00:00"})
    assert tuner.step(p) == "waiting" and runs == []
    assert tuner.describe(p)["calibration"] == "väntar på sändning"
    for _ in range(rg.TA_CONFIRM_GROUPS):
        rg.process_group(st, {"pi": "0x1", "ta": False, "timestamp": "2026-11-27T10:01:00"})
    assert tuner.step(p) == "calibrated"


def test_new_frequencies_calibrate_again(setup, store):
    tuner, p, st, clock, runs = setup
    tuner.step(p)
    p.receiver.frequencies = ["99.1M"]
    assert tuner.due(p) == "new frequencies"


def test_manual_gain_and_ppm_are_not_calibrated(setup):
    tuner, p, st, clock, runs = setup
    p.receiver.gain, p.receiver.ppm = 19.7, 3
    assert tuner.due(p) is None
    assert tuner.settings(p.receiver) == (19.7, 3)
    assert tuner.describe(p)["gain_mode"] == "manual"


def test_admin_command_runs_once(setup):
    tuner, p, st, clock, runs = setup
    tuner.step(p)
    cmd = {"id": 5, "type": "recalibrate", "receiver": "tak"}
    tuner.on_commands([cmd])
    assert tuner.due(p) == "asked for in Admin"
    tuner.step(p)
    tuner.on_commands([cmd])              # the same command again: ignored
    assert tuner.due(p) is None and runs == ["tak", "tak"]
    tuner.on_commands([{"id": 6, "type": "recalibrate", "receiver": "S1"}])   # by serial
    assert tuner.due(p) == "asked for in Admin"


def test_a_lasting_drop_calibrates_again_but_not_too_often(setup):
    tuner, p, st, clock, runs = setup
    tuner.step(p)                           # baseline 10 grp/s
    clock[0] += gt.MIN_BETWEEN_SEC + 1
    st.stats.snapshot = lambda: {"groups_per_sec": 3.0, "groups_total": 1, "uptime_sec": 1}
    assert tuner.due(p) is None             # low, but not for long yet
    clock[0] += gt.LOW_FOR_SEC + 1
    assert tuner.due(p) == "decoding dropped"
    tuner.step(p)
    clock[0] += gt.LOW_FOR_SEC + 60         # low again, but calibrated just now
    tuner.due(p)
    clock[0] += gt.LOW_FOR_SEC + 60
    assert tuner.due(p) is None


def test_blocks_keep_the_sample_spacing():
    """A tone demodulates smoothly across the channelizer's blocks."""
    z = fm_station(int(FS * 0.2), 250_000, rds=False, pilot=False, dev=10_000)
    m = demod_all(iq_from_u8(to_u8(z, 0.4, noise=0.0)), 250_000)[10:]
    d = np.abs(np.diff(m))
    assert d.max() < 4 * np.median(d)


def test_gain_zero_is_never_used():
    import pipeline
    assert "-g" in (cmd := pipeline.rtl_sdr_cmd(100_000_000, 0, 0.0, 0)) and cmd[cmd.index("-g") + 1] == "0.9"
    r = receivers.parse({"receivers": [{"serial": "1", "frequencies": ["103.5M"]}]})[0]
    dongle = FakeDongle(r)
    gt.calibrate(r, 0, [0.0, 19.7], capture_fn=dongle.capture,
                 quality_fn=lambda pcm, s: (5.0, 0), progress=lambda m: None)
    assert 0.0 not in {c[0] for c in dongle.calls}


def test_a_failed_calibration_waits_hours(setup):
    tuner, p, st, clock, runs = setup

    def broken(rec, want_gain=True, want_ppm=True):
        raise RuntimeError("rtl_sdr gave no samples")
    tuner._calibrate = broken
    assert tuner.step(p) == "calibrated"            # tried, failed
    assert p.resumed == 1
    assert tuner.due(p) is None                     # not again at once
    assert tuner.describe(p)["calibration"].startswith("misslyckades: rtl_sdr")
    clock[0] += gt.RETRY_AFTER_FAIL_SEC + 1
    assert tuner.due(p) == "first start"


def test_ppm_comes_from_the_stations_that_are_heard():
    """A frequency with no station on it (only noise) must not pull the ppm:
    on docker01 one gave 18.5 next to the real -1.7, and the median of two
    took it."""
    r = receivers.parse({"receivers": [{"serial": "1", "frequencies": ["103.5M", "102.8M"]}]})[0]
    true_ppm = -2

    def capture(index, center, gain, ppm, seconds):
        n = int(min(seconds, 0.3) * FS)
        hz = 103_500_000                      # only 103.5M is on the air
        z = 0.5 * fm_station(n, hz - center - (true_ppm - ppm) * hz / 1e6)
        return to_u8(z, 0.4, noise=0.05)

    lines = []
    res = gt.calibrate(r, 0, [8.7, 19.7], capture_fn=capture,
                       quality_fn=lambda pcm, s: (11.0, 0), progress=lines.append)
    assert res["ppm"] == pytest.approx(true_ppm, abs=1)
    assert any("102.8M: pilot" in line and "too weak" in line for line in lines)


def test_a_dongle_that_hears_nothing_is_said_so_and_not_checked():
    r = receivers.parse({"receivers": [{"serial": "1", "frequencies": ["103.5M"]}]})[0]
    calls = []

    def capture(index, center, gain, ppm, seconds):        # noise only
        calls.append(seconds)
        return to_u8(np.zeros(int(min(seconds, 0.3) * FS), complex), 0.0, noise=0.05)

    lines = []
    res = gt.calibrate(r, 0, [8.7, 19.7, 49.6], capture_fn=capture,
                       quality_fn=lambda pcm, s: (0.0, None), progress=lines.append)
    assert res["silent"] and any("no FM station heard" in line for line in lines)
    assert gt.VERIFY_SEC not in calls                       # no decoding run in vain


def test_a_silent_dongle_keeps_its_settings_and_tries_later(setup, store):
    tuner, p, st, clock, runs = setup
    tuner._calibrate = lambda r, **kw: {"silent": True, "gain": 49.6, "ppm": 0,
                                        "stations": {}, "dead": ["103.5M"], "steps": []}
    tuner.calibrate_pipeline(p, "first start")
    assert tuner.settings(p.receiver) == (gt.START_GAIN, 0)    # nothing saved
    assert "ingen FM-signal" in tuner.describe(p)["calibration"]
    assert p.resumed == 1


def test_stations_without_rds_show_on_the_node_card(setup, store):
    tuner, p, st, clock, runs = setup
    tuner._calibrate = lambda r, **kw: {"gain": 3.7, "ppm": -2, "stations": {"103.5M": 11.0},
                                        "dead": ["102.8M"], "steps": []}
    tuner.calibrate_pipeline(p, "first start")
    assert tuner.describe(p)["calibration"] == "ingen RDS: 102.8M"


def test_a_station_with_a_pilot_but_no_rds_does_not_steer_the_choice():
    """docker01, v1.0.10: 100.3M had a clear pilot (27 dB) but no RDS at any
    gain, so the worst RDS SNR was about 0 at every step and the steps to
    check were picked almost at random; linearity 6, the one step where
    102.8M decoded, got in by luck. Now that station is left out of the
    ranking, and every station's own best step is always checked."""
    def step(g, r1035, r1028, r1003):
        return {"gain": g, "clip": 0.0,
                "snr": {"103.5M": (50, r1035), "102.8M": (36, r1028), "100.3M": (27, r1003)}}
    # 100.3M's RDS SNR is noise around 0, a little higher at the low steps
    steps = [step(0, 10, 0.2, 0.9), step(1, 11, 0.3, 0.8), step(2, 12, 0.1, 0.7),
             step(3, 12, 0.4, 0.6), step(4, 13, 0.2, 0.3), step(5, 13, 1.0, 0.2),
             step(6, 14, 9.0, 0.1), step(7, 14, 0.5, 0.0)]
    best, left_out = gt.pick_steps(steps, ["103.5M", "102.8M", "100.3M"])
    gains = sorted(s["gain"] for s in best)
    assert left_out == ["100.3M"]
    assert 6 in gains                         # 102.8M's only good step
    # The old ranking (worst over all heard) would have checked 0-3 only
    old = sorted(sorted(steps, key=lambda s: (-min(v[1] for v in s["snr"].values()),
                                              s["gain"]))[:gt.VERIFY_STEPS],
                 key=lambda s: s["gain"])
    assert 6 not in [s["gain"] for s in old]


def test_every_station_with_rds_gets_its_own_best_step_checked():
    def step(g, a, b):
        return {"gain": g, "clip": 0.0, "snr": {"A": (40, a), "B": (40, b)}}
    steps = [step(g, 20 - g, 2 + (8 if g == 9 else 0)) for g in range(10)]
    best, left_out = gt.pick_steps(steps, ["A", "B"])
    assert left_out == [] and {0, 9} <= {s["gain"] for s in best}
    assert len(best) == gt.VERIFY_STEPS


def test_the_node_card_gets_the_radio_s_own_gain_text_and_no_ppm_for_an_airspy(setup):
    """docker01's node card showed an Airspy with LNA/Mixer/VGA by hand as
    "gain 8 dB (fast) · 0 ppm": a linearity step read as dB, and a ppm it
    does not have."""
    tuner, p, st, clock, runs = setup
    p.gain_text, p.has_ppm = "LNA 4, Mixer 4, VGA 5 (by hand)", False
    d = tuner.describe(p)
    assert d["gain_text"] == "LNA 4, Mixer 4, VGA 5 (by hand)"
    assert d["ppm"] is None and d["ppm_mode"] is None
