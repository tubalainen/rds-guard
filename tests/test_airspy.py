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
    assert tuner.settings(r) == (gt.DEFAULT_GAIN, 0)


def test_an_airspy_with_a_fixed_gain_is_not_calibrated():
    fixed = receivers.parse({"receivers": [{"serial": "0x644064DC2B1C2E4F",
                                            "frequencies": ["103.5M"], "gain": 10}]})[0]
    assert not gt.Tuner._auto(fixed)                 # ppm: auto means nothing here
    rtl = receivers.parse({"receivers": [{"serial": "1", "frequencies": ["103.5M"],
                                          "gain": 19.7}]})[0]
    assert gt.Tuner._auto(rtl)                       # its ppm is still measured
