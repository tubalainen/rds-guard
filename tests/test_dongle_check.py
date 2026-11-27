"""The check of a receiver's first seconds: data rate, samples that move,
and whether its stations are heard."""

import io
import os

import numpy as np

import dongle_check as dc
from channelizer import Channelizer, iq_from_u8
from tests.test_gain_tuner import FS, fm_station, to_u8

CENTER = 103_250_000
FREQS = [103_500_000]


class Radio:
    fs = FS
    bytes_per_sample = 2
    model = "RTL-SDR (test)"


def station_iq(seconds=0.5, gain_lin=0.4):
    z = fm_station(int(FS * seconds), FREQS[0] - CENTER)
    return iq_from_u8(to_u8(z, gain_lin))


def nbytes(seconds, rate=1.0):
    return int(FS * Radio.bytes_per_sample * seconds * rate)


def test_a_station_is_heard():
    level, msg, heard = dc.judge(station_iq(), nbytes(4), 4.0, Radio, FREQS, CENTER, gain=8.7)
    assert level == "ok" and heard == ["103.5M"]
    assert "MS/s" in msg and "dBFS" in msg and "heard 103.5M" in msg


def test_only_noise_is_a_warning_not_an_error():
    rng = np.random.default_rng(0)
    n = int(FS * 0.5)
    noise = iq_from_u8(to_u8(np.zeros(n, complex), 0.0, noise=0.05))
    level, msg, heard = dc.judge(noise, nbytes(4), 4.0, Radio, FREQS, CENTER, gain=49.6)
    assert level == "warning" and not heard
    assert "no FM station heard on 103.5M at gain 49.6" in msg


def test_samples_that_never_change_are_an_error():
    flat = np.full(int(FS * 0.5), 0.0039 + 0.0039j, np.complex64)   # one ADC code
    level, msg, _ = dc.judge(flat, nbytes(4), 4.0, Radio, FREQS, CENTER)
    assert level == "error" and "do not change" in msg


def test_too_little_data_is_an_error():
    level, msg, _ = dc.judge(station_iq(), nbytes(4, rate=0.4), 4.0, Radio, FREQS, CENTER)
    assert level == "error" and "too little data" in msg and "0.96 of 2.39 MS/s" in msg


def test_somewhat_low_data_is_only_a_warning():
    level, msg, heard = dc.judge(station_iq(), nbytes(4, rate=0.7), 4.0, Radio, FREQS, CENTER)
    assert level == "warning" and heard == ["103.5M"] and "1.68 of 2.39 MS/s" in msg


def test_airspy_format():
    class Airspy:
        fs = 3_000_000
        bytes_per_sample = 4
        model = "Airspy Mini"
    t = np.arange(int(Airspy.fs * 0.5)) / Airspy.fs
    mpx = 0.8 * np.sin(2 * np.pi * 1000 * t) + 0.09 * np.sin(2 * np.pi * 19_000 * t)
    z = 0.3 * np.exp(1j * (2 * np.pi * (FREQS[0] - CENTER) * t
                           + 2 * np.pi * np.cumsum(75_000 * mpx) / Airspy.fs))
    level, _, heard = dc.judge(z.astype(np.complex64), int(Airspy.fs * 4 * 4), 4.0,
                               Airspy, FREQS, CENTER)
    assert level == "ok" and heard == ["103.5M"]


def test_probe_collects_then_reports_once():
    t = [0.0]
    got = []
    p = dc.Probe(FS, lambda iq, n, el: got.append((len(iq), n, el)), clock=lambda: t[0])
    block = np.zeros(16_380, np.complex64)
    while t[0] < dc.PROBE_SEC + 1:
        p.feed(len(block) * 2, block)
        t[0] += len(block) / FS
    assert len(got) == 1
    kept, n, elapsed = got[0]
    assert dc.KEEP_SEC * FS <= kept < dc.KEEP_SEC * FS + 16_380
    assert elapsed >= dc.PROBE_SEC and n > 0


def test_channelizer_output_is_the_same_with_a_probe():
    raw = to_u8(fm_station(int(FS * 0.3), FREQS[0] - CENTER), 0.4)

    def run(probe):
        ch = Channelizer(io.BytesIO(raw), FREQS, CENTER, probe=probe)
        fd = ch.pipe_read_fds[0]
        ch.start()
        out = b""
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            out += chunk
        ch.join()
        return out

    got = []
    with_probe = run(dc.Probe(FS, lambda *a: got.append(a)))
    assert with_probe == run(None)
    assert len(got) == 1                     # the stream ended: reported anyway


def test_a_dongle_that_gives_flat_samples_stops_the_receiver(monkeypatch):
    """The whole path: capture → channelizer → check → the run ends with
    the reason as its error, and the supervisor retries as for any failure."""
    import sys
    import threading
    import pipeline
    import receivers

    monkeypatch.setattr(dc, "PROBE_SEC", 1.0)
    monkeypatch.setattr(dc, "SETTLE_SEC", 0.1)

    class FlatRadio(Radio):
        kind = "rtlsdr"
        tool = "fake_sdr"
        fmt = "u8"
        has_ppm = True

        def prepare(self):
            pass

        def describe_gain(self, gain):
            return f"{gain:g} dB"

        def stream_cmd(self, center, gain, ppm):
            # Bytes at the right rate, all the same value: a dead tuner
            return [sys.executable, "-c",
                    "import sys,time\n"
                    "b=bytes([128])*65520\n"
                    "while True:\n"
                    "  sys.stdout.buffer.write(b)\n"]

    monkeypatch.setattr(pipeline.sdr, "open", lambda serial, stages=None: FlatRadio())
    monkeypatch.setattr(pipeline, "redsea_cmd", lambda: [sys.executable, "-c",
                        "import sys\nfor _ in sys.stdin.buffer: pass"])
    r = receivers.parse({"receivers": [{"serial": "1", "frequencies": ["103.5M"]}]})[0]
    p = pipeline.ReceiverPipeline(r, [{"frequency": "103.5M", "callback": lambda line: None}],
                                  lambda radio: (8.7, 0))
    ok = p._run_once(threading.Event())
    assert ok is False
    snap = p.status.snapshot()
    assert snap["state"] == "error" and "do not change" in snap["error"]
