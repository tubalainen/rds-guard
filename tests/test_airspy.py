"""Airspy Mini and R2 on the satellite: finding them, the airspy_rx command,
the 16-bit IQ through the channelizer (250 kHz, resampled to 171 kHz) and a
calibration over the linearity gain."""

import os

import numpy as np
import pytest
import yaml

import dongles
import gain_tuner as gt
import receivers
import sdr
from channelizer import Channelizer, Resampler, demod_all, iq_from_s16, iq_from_u8, plan

AIRSPY_INFO = """airspy_lib_version: 1.0.10

Found AirSpy board 1
Board ID Number: 0 (AIRSPY)
Firmware Version: AirSpy MINI v1.0.0-rc10-6-g4008185 2020-05-08
Part ID Number: 0x6906002B 0x00000030
Serial Number: 0x644064DC2B1C2E4F
Supported sample rates:
	6.000000 MSPS
	3.000000 MSPS
Close board 1

Found AirSpy board 2
Board ID Number: 0 (AIRSPY)
Firmware Version: AirSpy NOS v1.0.0-rc10-3-g7120e77 2018-04-28
Part ID Number: 0x6906002B 0x00000030
Serial Number: 0x00000000ABCDEF12
Supported sample rates:
	10.000000 MSPS
	2.500000 MSPS
Close board 2
"""

CENTER = 103_200_000


def fm_station(n, offset_hz, fs, rds=True, pilot=True, dev=75_000, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(n) / fs
    mpx = 0.8 * np.sin(2 * np.pi * 1000 * t)
    if pilot:
        mpx += 0.09 * np.sin(2 * np.pi * 19_000 * t)
    if rds:
        bits = np.repeat(rng.choice([-1.0, 1.0], size=n // 2000 + 1), 2000)[:n]
        mpx += 0.05 * bits * np.sin(2 * np.pi * 57_000 * t)
    phase = 2 * np.pi * np.cumsum(dev * mpx) / fs
    return np.exp(1j * (2 * np.pi * offset_hz * t + phase))


def to_s16(z, gain_lin, noise=0.01, seed=1):
    """What airspy_rx -t 2 gives: interleaved little-endian int16 I/Q."""
    rng = np.random.default_rng(seed)
    z = z * gain_lin + noise * (rng.standard_normal(len(z)) + 1j * rng.standard_normal(len(z)))
    out = np.empty(2 * len(z), dtype="<i2")
    out[0::2] = np.clip(np.round(z.real * 32768), -32768, 32767)
    out[1::2] = np.clip(np.round(z.imag * 32768), -32768, 32767)
    return out.tobytes()


def to_u8(z, gain_lin):
    i = np.clip(np.round(z.real * gain_lin * 127.5 + 127.5), 0, 255)
    q = np.clip(np.round(z.imag * gain_lin * 127.5 + 127.5), 0, 255)
    out = np.empty(2 * len(z), dtype=np.uint8)
    out[0::2], out[1::2] = i, q
    return out.tobytes()


# ------------------------------------------------------------- finding them

def test_airspy_info_is_parsed():
    mini, r2 = dongles.parse_airspy_info(AIRSPY_INFO)
    assert (mini.kind, mini.product, mini.serial) == ("airspy", "Airspy Mini",
                                                      "0x644064DC2B1C2E4F")
    assert mini.rates == [3_000_000, 6_000_000]
    assert (r2.product, r2.serial, r2.rates) == ("Airspy R2", "0x00000000ABCDEF12",
                                                 [2_500_000, 10_000_000])
    assert mini.gains == [float(g) for g in range(22)]


def test_sample_rate_is_a_multiple_of_250_khz():
    assert sdr.pick_rate([3_000_000, 6_000_000]) == 3_000_000
    assert sdr.pick_rate([2_500_000, 10_000_000]) == 2_500_000
    assert sdr.pick_rate([], "Airspy Mini") == 3_000_000
    assert sdr.pick_rate([], "Airspy R2") == 2_500_000
    assert plan(3_000_000) == (12, (171, 250))
    assert plan(2_500_000) == (10, (171, 250))
    assert plan(2_394_000) == (14, None)            # the RTL-SDR: no resampler


def test_serial_numbers_as_written_in_receivers_yml():
    # Unquoted, YAML reads 0x… as a number
    data = yaml.safe_load("receivers:\n  - serial: 0x644064DC2B1C2E4F\n"
                          "    frequencies: [103.5M]\n    ppm: 3\n")
    [r] = receivers.parse(data)
    assert r.serial == "0x644064DC2B1C2E4F"
    assert any("TCXO" in w for w in r.warnings)
    assert dongles.normalize_serial("0x644064dc2b1c2e4f") == "0x644064DC2B1C2E4F"
    assert dongles.normalize_serial("644064dc2b1c2e4f") == "0x644064DC2B1C2E4F"
    assert dongles.normalize_serial("00000101") == "00000101"      # an RTL-SDR's
    assert dongles.same_airspy("644064DC2B1C2E4F", "0x644064dc2b1c2e4f")


def test_lookup_remembers_an_airspy_it_cannot_see_while_running(monkeypatch):
    monkeypatch.setattr(dongles, "_airspy_seen", {})
    monkeypatch.setattr(dongles, "_airspy_listed_at", [0.0])
    calls = []
    monkeypatch.setattr(dongles, "_rtl_test",
                        lambda args=(), timeout=10: "Found 1 device(s):\n"
                        "  0:  Realtek, RTL2838UHIDIR, SN: 00000101\n")
    monkeypatch.setattr(dongles, "_airspy_info",
                        lambda timeout=10: calls.append(1) or AIRSPY_INFO)
    d = dongles.lookup("0x644064DC2B1C2E4F")
    assert d.kind == "airspy" and d.product == "Airspy Mini"
    # Now it runs: airspy_info no longer lists it, and is not asked
    monkeypatch.setattr(dongles, "_airspy_info", lambda timeout=10: "")
    assert dongles.lookup("644064dc2b1c2e4f").serial == "0x644064DC2B1C2E4F"
    assert len(calls) == 1
    # An RTL-SDR is found with rtl_test alone
    monkeypatch.setattr(dongles, "_airspy_info", lambda timeout=10: pytest.fail("opened"))
    assert dongles.lookup("00000101").kind == "rtlsdr"
    monkeypatch.setattr(dongles, "_airspy_info", lambda timeout=10: "")
    # A missing Airspy is looked for again only after a minute
    with pytest.raises(dongles.DongleError, match="RTL-SDR or Airspy"):
        dongles.lookup("0x1")
    assert len(calls) == 1
    monkeypatch.setattr(dongles, "_airspy_listed_at", [1.0])
    monkeypatch.setattr(dongles.time, "monotonic", lambda: 1.0 + dongles.AIRSPY_RELIST_SEC)
    monkeypatch.setattr(dongles, "_airspy_info",
                        lambda timeout=10: calls.append(1) or AIRSPY_INFO)
    with pytest.raises(dongles.DongleError):
        dongles.lookup("0x1")
    assert len(calls) == 2
    radio = sdr.open("0x644064DC2B1C2E4F")
    assert isinstance(radio, sdr.Airspy) and radio.fs == 3_000_000


# ---------------------------------------------------------------- commands

def test_airspy_rx_command():
    radio = sdr.Airspy("0x00000000ABCDEF12", [2_500_000, 10_000_000], "Airspy R2")
    cmd = radio.stream_cmd(103_450_000, 30.0, 12)
    assert cmd[0] == "airspy_rx"
    assert cmd[cmd.index("-s") + 1] == "0x00000000ABCDEF12"
    assert cmd[cmd.index("-f") + 1] == "103.450000"          # MHz
    assert cmd[cmd.index("-a") + 1] == "2500000"             # by value
    assert cmd[cmd.index("-t") + 1] == "2"                   # INT16_IQ
    assert cmd[cmd.index("-g") + 1] == "21"                  # clamped to 0-21
    assert cmd[-2:] == ["-r", "-"]
    # -p is packing and -b the bias tee there, not ppm: never given
    assert "-p" not in cmd and "-b" not in cmd
    cap = radio.capture_cmd(103_450_000, 0, 0, 1000)
    assert cap[cap.index("-g") + 1] == "0" and cap[cap.index("-n") + 1] == "1000"


def test_rtl_sdr_command_is_unchanged():
    import pipeline
    assert pipeline.rtl_sdr_cmd(103_450_000, 2, 28.0, 5) == [
        "rtl_sdr", "-f", "103450000", "-s", "2394000", "-g", "28", "-p", "5", "-d", "2", "-"]


def test_airspy_clip_fraction():
    """A full 12-bit ADC comes out of libairspy at about ±16 384 (halved in
    its real-to-IQ step), not at the int16 limit."""
    z = fm_station(20_000, 300_000, 3_000_000)
    assert sdr.Airspy.clip_fraction(to_s16(z, 0.3)) == 0
    assert sdr.Airspy.clip_fraction(to_s16(z, 0.5)) > 0.2      # ±16 384: full
    assert sdr.Airspy.clip_fraction(to_s16(z, 3.0)) > 0.5


def test_a_capture_is_tried_once_more(monkeypatch):
    import types
    monkeypatch.setattr(sdr.time, "sleep", lambda s: None)
    radio = sdr.Airspy("0x1", [3_000_000])
    answers = [b"", b"\0" * (4 * 3_000_000)]
    run = lambda cmd, **kw: types.SimpleNamespace(stdout=answers.pop(0), stderr=b"busy")
    assert len(sdr.capture(radio, 103_000_000, 10, 0, 0.75, run=run)) == 4 * 2_250_000
    with pytest.raises(RuntimeError, match="airspy_rx gave no samples"):
        sdr.capture(radio, 103_000_000, 10, 0, 0.75,
                    run=lambda cmd, **kw: types.SimpleNamespace(stdout=b"", stderr=b"busy"))


# -------------------------------------------------------------- the signal

@pytest.mark.parametrize("fs", [3_000_000, 2_500_000])
def test_airspy_mpx_matches_the_rtl_path(fs):
    """The same station through the Airspy path (16-bit, resampled) gives the
    same multiplex as through the RTL-SDR path: pilot and RDS clear, the
    level and the carrier offset (ppm) in the same units."""
    off = 300_000
    a = demod_all(iq_from_s16(to_s16(fm_station(int(fs * 0.5), off, fs), 0.4)), off, fs)
    r = demod_all(iq_from_u8(to_u8(fm_station(int(2_394_000 * 0.5), off, 2_394_000), 0.4)),
                  off)
    assert abs(len(a) - len(r)) < 2000                    # both at 171 kHz
    pilot, rds = gt.tone_snr_db(a[500:])
    assert pilot > 15 and rds > 6
    assert a[500:].std() == pytest.approx(r[500:].std(), rel=0.05)
    # A carrier 2 kHz off reads as 2 kHz off (radians per 171 kHz sample)
    b = demod_all(iq_from_s16(to_s16(fm_station(int(fs * 0.5), off + 2000, fs, dev=0), 0.4)),
                  off, fs)
    assert float(np.mean(b[500:])) * 171_000 / (2 * np.pi) == pytest.approx(2000, rel=0.02)


def test_the_resampler_has_no_seams_between_blocks():
    fs = 250_000
    x = np.sin(2 * np.pi * 57_000 * np.arange(fs // 2) / fs).astype(np.float32)
    whole = Resampler(171, 250, fs).process(x)
    rs = Resampler(171, 250, fs)
    pieces = np.concatenate([rs.process(x[i:i + 1365]) for i in range(0, len(x), 1365)])
    assert len(pieces) == len(whole)
    assert np.max(np.abs(pieces - whole)) < 1e-4


def test_the_channelizer_reads_airspy_iq_from_a_pipe():
    fs = 3_000_000
    freqs = [103_000_000, 103_400_000]
    center = 103_200_000
    n = int(fs * 0.3)
    z = sum(fm_station(n, f - center, fs, seed=i) for i, f in enumerate(freqs)) / 2
    raw = to_s16(z, 0.4)
    r, w = os.pipe()
    chan = Channelizer(os.fdopen(r, "rb"), freqs, center, fs, "s16")
    chan.start()
    outs = [bytearray() for _ in freqs]

    import threading

    def drain(fd, buf):
        with os.fdopen(fd, "rb") as f:
            while chunk := f.read(65536):
                buf.extend(chunk)
    readers = [threading.Thread(target=drain, args=(fd, b)) for fd, b in
               zip(chan.pipe_read_fds, outs)]
    for t in readers:
        t.start()
    with os.fdopen(w, "wb") as f:
        f.write(raw)
    chan.join(10)
    for t in readers:
        t.join(10)
    for buf, f in zip(outs, freqs):
        pcm = np.frombuffer(bytes(buf), dtype=np.int16)
        assert len(pcm) == pytest.approx(n / fs * 171_000, rel=0.08)
        pilot, rds = gt.tone_snr_db(pcm[500:].astype(np.float32) / 32767 * np.pi)
        assert pilot > 15 and rds > 6


# -------------------------------------------------------------- calibration

def test_a_calibration_over_the_airspy_linearity_gain():
    r = receivers.parse({"receivers": [{"serial": "0x644064DC2B1C2E4F",
                                        "frequencies": ["103.5M"]}]})[0]
    radio = sdr.Airspy(r.serial, [3_000_000, 6_000_000], "Airspy Mini")
    calls, current = [], [None]

    def capture(index, center, gain, ppm, seconds):
        calls.append((gain, ppm, seconds))
        current[0] = gain
        n = int(min(seconds, 0.3) * radio.fs)
        z = fm_station(n, r.freqs_hz[0] - center, radio.fs)
        return to_s16(z, 10 ** ((gain - 10) / 20) * 0.3, noise=0.02)

    def quality(pcm, seconds):
        return (11.0 - abs(current[0] - 17)), 0      # best at 17

    lines = []
    res = gt.calibrate(r, None, radio.gains, capture_fn=capture, quality_fn=quality,
                       progress=lines.append, radio=radio)
    checked = [c[0] for c in calls if c[2] == gt.VERIFY_SEC]
    assert res["ppm"] == 0 and res["gain"] == min(checked, key=lambda g: abs(g - 17))
    # No ppm step; linearity 0 is a real step (not an AGC)
    assert not any(c[2] == gt.PPM_SEC for c in calls)
    assert sum("clip" in line for line in lines) == 22
    # Overloaded steps (clipping) were not checked
    assert 21.0 not in [c[0] for c in calls if c[2] == gt.VERIFY_SEC]


def test_the_default_gain_before_calibration_follows_the_radio(tmp_path, monkeypatch):
    import event_store
    monkeypatch.setattr(event_store, "get_calibration", lambda serial: None)
    r = receivers.parse({"receivers": [{"serial": "0x644064DC2B1C2E4F",
                                        "frequencies": ["103.5M"]}]})[0]
    tuner = gt.Tuner(sat=None)
    assert tuner.settings(r, sdr.Airspy(r.serial, [3_000_000])) == (sdr.AIRSPY_DEFAULT_GAIN, 0)
    assert tuner.settings(r) == (gt.START_GAIN, 0)
    assert sdr.RtlSdr.default_gain == gt.START_GAIN         # the same start for both paths


def test_an_airspy_with_a_fixed_gain_is_not_calibrated():
    fixed = receivers.parse({"receivers": [{"serial": "0x644064DC2B1C2E4F",
                                            "frequencies": ["103.5M"], "gain": 10}]})[0]
    assert not gt.Tuner._auto(fixed)                 # ppm: auto means nothing here
    rtl = receivers.parse({"receivers": [{"serial": "1", "frequencies": ["103.5M"],
                                          "gain": 19.7}]})[0]
    assert gt.Tuner._auto(rtl)                       # its ppm is still measured


# ------------------------------------------------- LNA / Mixer / VGA by hand

def _airspy_receiver(**extra):
    return receivers.parse({"receivers": [{"serial": "0x62CC68FF3584805B",
                                           "frequencies": ["102.8M"], **extra}]})[0]


def test_the_linearity_table_is_libairspys():
    assert sdr.AIRSPY_LINEARITY[0] == (0, 0, 4)
    assert sdr.AIRSPY_LINEARITY[8] == (3, 0, 10)
    assert sdr.AIRSPY_LINEARITY[21] == (14, 12, 13)
    assert len(sdr.AIRSPY_LINEARITY) == 22


def test_stages_in_receivers_yml_are_optional_and_checked():
    r = _airspy_receiver()
    assert r.stages == {} and not r.gain_fixed
    r = _airspy_receiver(lna=4, mixer=3, vga=9)
    assert r.stages == {"lna": 4, "mixer": 3, "vga": 9} and r.gain_fixed
    r = _airspy_receiver(lna=4)
    assert r.stages == {"lna": 4} and not r.gain_fixed      # the rest is calibrated
    r = _airspy_receiver(lna=4, mixer=3, vga=9, gain=10)
    assert any("gain: is not used" in w for w in r.warnings)
    with pytest.raises(receivers.ReceiverError, match="lna must be 0-14"):
        _airspy_receiver(lna=15)
    with pytest.raises(receivers.ReceiverError, match="vga must be a whole number"):
        _airspy_receiver(vga="hög")
    rtl = receivers.parse({"receivers": [{"serial": "00000101", "frequencies": ["102.8M"],
                                          "lna": 4}]})[0]
    assert rtl.stages == {} and any("for an Airspy" in w for w in rtl.warnings)


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_stages_by_hand_go_to_airspy_rx_instead_of_g():
    all3 = sdr.Airspy("0x62CC68FF3584805B", [2_500_000], "Airspy R2",
                      {"lna": 4, "mixer": 3, "vga": 9})
    cmd = all3.stream_cmd(102_800_000, 12, 0)
    assert "-g" not in cmd
    assert (_arg(cmd, "-l"), _arg(cmd, "-m"), _arg(cmd, "-v")) == ("4", "3", "9")
    # Only LNA by hand: Mixer and VGA follow the linearity step
    lna = sdr.Airspy("0x1", [2_500_000], stages={"lna": 4})
    cmd = lna.capture_cmd(102_800_000, 8, 0, 1000)
    assert (_arg(cmd, "-l"), _arg(cmd, "-m"), _arg(cmd, "-v")) == ("4", "0", "10")
    cmd = lna.capture_cmd(102_800_000, 21, 0, 1000)
    assert (_arg(cmd, "-l"), _arg(cmd, "-m"), _arg(cmd, "-v")) == ("4", "12", "13")
    # None by hand: -g as before
    plain = sdr.Airspy("0x1", [2_500_000])
    assert _arg(plain.stream_cmd(102_800_000, 8, 0), "-g") == "8"
    assert "-l" not in plain.stream_cmd(102_800_000, 8, 0)
    assert plain.describe_gain(8) == "LNA 3, Mixer 0, VGA 10 (linearity 8)"
    assert all3.describe_gain(8) == "LNA 4, Mixer 3, VGA 9 (by hand)"


def test_the_agc_is_turned_off_before_a_run_by_hand():
    seen = []
    sdr.Airspy("0x1", [3_000_000]).prepare(agc_off=seen.append)
    assert seen == []                                    # -g turns it off itself
    sdr.Airspy("0x1", [3_000_000], stages={"vga": 9}).prepare(agc_off=seen.append)
    assert seen == ["0x0000000000000001"]


def test_agc_off_through_libairspy(monkeypatch):
    import ctypes
    calls = []

    class Fn:
        def __init__(self, name, rc=0):
            self.name, self.rc = name, rc

        def __call__(self, *args):
            calls.append((self.name, args))
            return self.rc

    class Lib:
        def __init__(self, path):
            assert path == "libairspy.so.0"
            for n in ("airspy_open_sn", "airspy_set_lna_agc", "airspy_set_mixer_agc",
                      "airspy_close"):
                setattr(self, n, Fn(n))
    monkeypatch.setattr(ctypes, "CDLL", Lib)
    sdr._airspy_agc_off("0x62CC68FF3584805B")
    assert [c[0] for c in calls] == ["airspy_open_sn", "airspy_set_lna_agc",
                                     "airspy_set_mixer_agc", "airspy_close"]
    assert calls[0][1][1] == 0x62CC68FF3584805B
    assert calls[1][1][1] == 0 and calls[2][1][1] == 0

    class Busy(Lib):
        def __init__(self, path):
            super().__init__(path)
            self.airspy_open_sn = Fn("airspy_open_sn", rc=-1000)
    monkeypatch.setattr(ctypes, "CDLL", Busy)
    with pytest.raises(dongles.DongleError, match="AGC"):
        sdr._airspy_agc_off("0x1")


def test_all_three_by_hand_is_not_calibrated_but_one_is():
    assert not gt.Tuner._auto(_airspy_receiver(lna=4, mixer=3, vga=9))
    assert gt.Tuner._auto(_airspy_receiver(lna=4))


def test_a_calibration_keeps_the_stages_set_by_hand():
    r = _airspy_receiver(lna=4)
    radio = sdr.Airspy(r.serial, [2_500_000], "Airspy R2", r.stages)
    cmds = []

    def run(cmd, **kw):
        import types
        cmds.append(cmd)
        n = int(radio.fs * 0.6)
        return types.SimpleNamespace(stdout=to_s16(fm_station(n, 250_000, radio.fs), 0.3),
                                     stderr=b"")
    capture = lambda i, c, g, p, s: sdr.capture(radio, c, g, p, s, run=run)
    gt.calibrate(r, None, [0.0, 8.0, 21.0], capture_fn=capture,
                 quality_fn=lambda pcm, s: (5.0, 0), progress=lambda m: None, radio=radio)
    assert cmds and all(_arg(c, "-l") == "4" and "-g" not in c for c in cmds)
    assert {_arg(c, "-v") for c in cmds} >= {"4", "13"}         # VGA still swept


def test_steps_that_give_the_same_stages_are_swept_once():
    r = _airspy_receiver(lna=4, mixer=3)
    radio = sdr.Airspy(r.serial, [2_500_000], "Airspy R2", r.stages)
    swept = []

    def capture(i, c, g, p, s):
        if s == gt.SWEEP_SEC:
            swept.append(g)
        return to_s16(fm_station(int(radio.fs * 0.3), 250_000, radio.fs), 0.3)
    gt.calibrate(r, None, radio.gains, capture_fn=capture,
                 quality_fn=lambda pcm, s: (5.0, 0), progress=lambda m: None, radio=radio)
    # Only VGA moves: 4..13, each once, at its lowest step
    assert [radio.stage_values(g)[2] for g in swept] == list(range(4, 14))
    assert len(set(radio.stage_values(g) for g in swept)) == len(swept)


def test_changed_stages_calibrate_again(monkeypatch):
    import event_store
    store = {}
    monkeypatch.setattr(event_store, "get_calibration", lambda serial: store.get(serial))
    r = _airspy_receiver(lna=4)
    tuner = gt.Tuner(sat=None)
    store[r.serial] = {"frequencies": ["102.8M"], "gain": 9.0, "ppm": None,
                       "calibrated_at": "2026-11-27T20:00:00",
                       "result": {"stations": {}, "stages": {"lna": 4}}}
    assert tuner._stored(r) is not None
    assert tuner._stored(_airspy_receiver(lna=8)) is None          # pin changed
    assert tuner._stored(_airspy_receiver()) is None               # pin removed
    # A calibration saved before stages existed counts as none by hand
    store[r.serial]["result"] = {"stations": {}}
    assert tuner._stored(_airspy_receiver()) is not None


# ------------------------------------------------------- 10 MS/s and 6 MS/s

def test_the_wide_front_end_fits_its_blocks():
    from channelizer import WIDE_FROM, _WideFront
    for fs in (6_000_000, 10_000_000):
        f = _WideFront(fs)
        assert fs >= WIDE_FROM and fs // f.decim == 250_000
        assert f.size % f.decim == 0 and f.block % f.decim == 0 and f.overlap % f.decim == 0
        assert f.overlap >= int(6 * fs / 40_000)          # longer than the filter
        assert f.bins == f.size // f.decim


@pytest.mark.parametrize("fs, off", [(10_000_000, 300_000), (10_000_000, -3_700_000),
                                     (10_000_000, 2_123_457), (6_000_000, 300_000),
                                     (6_000_000, -2_123_457)])
def test_wide_rates_give_the_same_multiplex(fs, off):
    a = demod_all(iq_from_s16(to_s16(fm_station(int(fs * 0.5), off, fs), 0.4)), off, fs)
    pilot, rds = gt.tone_snr_db(a[500:])
    assert pilot > 15 and rds > 6
    assert len(a) == pytest.approx(0.5 * 171_000, rel=0.03)
    # The carrier offset reads right, and blocks join without a step
    b = demod_all(iq_from_s16(to_s16(fm_station(int(fs * 0.4), off + 2000, fs, dev=0), 0.4)),
                  off, fs)[500:]
    assert float(np.mean(b)) * 171_000 / (2 * np.pi) == pytest.approx(2000, rel=0.01)
    assert b.std() < 0.02


def test_far_apart_stations_do_not_leak_into_each_other():
    from channelizer import demod_many
    fs = 10_000_000
    n = int(fs * 0.4)
    t = np.arange(n) / fs

    def tone_station(off, tone):
        return np.exp(1j * (2 * np.pi * off * t
                            + 2 * np.pi * np.cumsum(75_000 * 0.8 * np.sin(2 * np.pi * tone * t)) / fs))
    z = iq_from_s16(to_s16((tone_station(-3_500_000, 1000) + tone_station(3_500_000, 3000)) / 2,
                           0.4))
    lo, hi = demod_many(z, [-3_500_000, 3_500_000], fs)

    def peak(m):
        sp = np.abs(np.fft.rfft(m[500:] * np.hanning(len(m) - 500)))
        return np.fft.rfftfreq(len(m) - 500, 1 / 171_000)[sp.argmax()]
    assert peak(lo) == pytest.approx(1000, abs=20)
    assert peak(hi) == pytest.approx(3000, abs=20)
    # The mirror of a station is not picked up
    mirror = demod_all(iq_from_s16(to_s16(fm_station(n, -2_000_000, fs, dev=0), 0.4)),
                       2_000_000, fs)
    assert mirror[500:].std() > 1.0


def test_the_channelizer_reads_10_msps_from_a_pipe():
    import threading
    fs = 10_000_000
    freqs = [99_500_000, 106_900_000]
    center = (freqs[0] + freqs[1]) // 2
    n = int(fs * 0.25)
    z = sum(fm_station(n, f - center, fs, seed=i) for i, f in enumerate(freqs)) / 2
    r, w = os.pipe()
    chan = Channelizer(os.fdopen(r, "rb"), freqs, center, fs, "s16")
    chan.start()
    outs = [bytearray() for _ in freqs]

    def drain(fd, buf):
        with os.fdopen(fd, "rb") as f:
            while chunk := f.read(65536):
                buf.extend(chunk)
    readers = [threading.Thread(target=drain, args=(fd, b)) for fd, b in
               zip(chan.pipe_read_fds, outs)]
    for t in readers:
        t.start()
    with os.fdopen(w, "wb") as f:
        f.write(to_s16(z, 0.4))
    chan.join(20)
    for t in readers:
        t.join(20)
    for buf in outs:
        pcm = np.frombuffer(bytes(buf), dtype=np.int16)
        assert len(pcm) == pytest.approx(n / fs * 171_000, rel=0.1)
        pilot, rds = gt.tone_snr_db(pcm[500:].astype(np.float32) / 32767 * np.pi)
        assert pilot > 15 and rds > 6


def test_sample_rate_in_receivers_yml():
    r = _airspy_receiver(sample_rate="10M", frequencies=["99.5M", "102.8M", "106.9M"])
    assert r.sample_rate == 10_000_000 and r.max_span_hz == 7_800_000
    with pytest.raises(receivers.ReceiverError, match="one dongle covers 7.8 MHz"):
        _airspy_receiver(sample_rate="10M", frequencies=["95.0M", "103.0M"])
    with pytest.raises(receivers.ReceiverError, match="sample_rate: 10M"):
        _airspy_receiver(frequencies=["99.5M", "106.9M"])           # 2 MHz without it
    eight = [f"{88 + i * 0.9:.1f}M" for i in range(8)]
    assert len(_airspy_receiver(sample_rate=10_000_000, frequencies=eight).frequencies) == 8
    with pytest.raises(receivers.ReceiverError, match="at most 4"):
        _airspy_receiver(sample_rate="2.5M", frequencies=eight[:5])
    assert _airspy_receiver(sample_rate="6M", frequencies=["99.0M", "103.6M"]).max_span_hz \
        == 4_600_000
    with pytest.raises(receivers.ReceiverError, match="must be one of"):
        _airspy_receiver(sample_rate="5M")
    rtl = receivers.parse({"receivers": [{"serial": "00000101", "frequencies": ["102.8M"],
                                          "sample_rate": "10M"}]})[0]
    assert rtl.sample_rate is None and any("for an Airspy" in w for w in rtl.warnings)


def test_the_sample_rate_must_be_the_devices():
    r2 = sdr.Airspy("0x1", [2_500_000, 10_000_000], "Airspy R2", rate=10_000_000)
    assert r2.fs == 10_000_000 and _arg(r2.stream_cmd(103_000_000, 8, 0), "-a") == "10000000"
    with pytest.raises(dongles.DongleError, match="cannot run at 6 MS/s"):
        sdr.Airspy("0x1", [2_500_000, 10_000_000], "Airspy R2", rate=6_000_000)


def test_a_wide_capture_is_decoded_while_it_comes_in():
    import io
    import types
    radio = sdr.Airspy("0x1", [2_500_000, 10_000_000], "Airspy R2", rate=10_000_000)
    data = to_s16(fm_station(int(radio.fs * 0.75), 300_000, radio.fs), 0.3)
    seen = []

    def popen(cmd, **kw):
        seen.append(cmd)
        return types.SimpleNamespace(stdout=io.BytesIO(data), kill=lambda: None,
                                     wait=lambda: 0)
    clip, level, [mpx] = sdr.capture_measure(radio, 103_000_000, 8, 0, 0.5, [300_000],
                                             popen=popen)
    assert _arg(seen[0], "-n") == str(int(0.75 * radio.fs))
    assert clip == 0 and -20 < level < 0
    assert len(mpx) == pytest.approx(0.5 * 171_000, rel=0.05)
    assert gt.tone_snr_db(mpx[500:])[0] > 15


def test_the_calibration_streams_at_wide_rates(monkeypatch):
    r = _airspy_receiver(sample_rate="10M")
    radio = sdr.Airspy(r.serial, [2_500_000, 10_000_000], "Airspy R2", rate=10_000_000)
    calls = []

    def fake(radio_, center, gain, ppm, seconds, offsets):
        """A clean multiplex: pilot and RDS carrier, no IQ at all."""
        calls.append(gain)
        t = np.arange(int(171_000 * 0.3)) / 171_000
        m = (0.8 * np.sin(2 * np.pi * 1000 * t) + 0.09 * np.sin(2 * np.pi * 19_000 * t)
             + 0.05 * np.sin(2 * np.pi * 57_000 * t)).astype(np.float32) * 2.75
        return 0.0, -10.0, [m for _ in offsets]
    monkeypatch.setattr(sdr, "capture_measure", fake)
    res = gt.calibrate(r, None, radio.gains, quality_fn=lambda pcm, s: (5.0, 0),
                       progress=lambda m: None, radio=radio)
    assert len(calls) >= 22 and res["gain"] in radio.gains
