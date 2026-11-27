"""The two kinds of receiver the satellite drives, behind one interface.

  RtlSdr  rtl_sdr, 2.394 MS/s, unsigned 8-bit IQ, gain in dB steps, ppm
  Airspy  airspy_rx (Mini 3 MS/s, R2 2.5 MS/s), signed 16-bit IQ, the
          "linearity" gain 0-21, no ppm (its clock is a TCXO); or LNA,
          Mixer and VGA set by hand (lna:/mixer:/vga: in receivers.yml)

Both give the channelizer IQ it turns into 171 kHz MPX for redsea, so
everything after the capture (redsea, recording, the calibration) is the
same for both. open(serial) finds which one a serial number is.
"""

import subprocess
import time

import numpy as np

import config
import dongles

# The Airspy is decimated to 250 kHz (3 MS/s / 12, 2.5 MS/s / 10), then
# resampled to 171 kHz
AIRSPY_MID_RATE = 250_000
AIRSPY_DEFAULT_GAIN = 8.0        # linearity, before the calibration: well
                                 # below overload, as the RTL-SDR's 8.7 dB
# libairspy shifts the 12-bit ADC to 16 bits and halves it in its real-to-IQ
# step (Q >> 1, the half-band I filter): a full ADC gives about ±16 384
AIRSPY_CLIP = 15_000             # |I| or |Q| this high: the ADC is (near) full

# What airspy_rx -g N sets (libairspy 1.0.10, airspy_set_linearity_gain):
# (LNA, Mixer, VGA) for linearity 0..21, both AGCs off
AIRSPY_LINEARITY = [
    (0, 0, 4), (0, 0, 5), (0, 1, 6), (0, 1, 7), (0, 1, 8), (0, 1, 9), (0, 2, 10),
    (1, 2, 10), (3, 0, 10), (5, 0, 10), (6, 1, 10), (8, 0, 10), (9, 0, 10),
    (8, 5, 10), (9, 6, 10), (9, 6, 11), (10, 7, 11), (12, 8, 11), (13, 9, 11),
    (14, 11, 11), (14, 12, 12), (14, 12, 13)]
# The highest step of each stage (airspy_rx)
AIRSPY_STAGE_MAX = {"lna": 14, "mixer": 15, "vga": 15}


class RtlSdr:
    kind = "rtlsdr"
    tool = "rtl_sdr"
    fmt = "u8"
    bytes_per_sample = 2          # I and Q, one byte each
    has_ppm = True
    zero_is_auto = True           # rtl_sdr -g 0: the tuner's AGC, never used
    default_gain = 8.7            # until calibrated: low enough not to clip

    def __init__(self, index, serial="", product="RTL-SDR"):
        self.index = index
        self.serial = serial
        self.model = f"RTL-SDR ({product})" if product else "RTL-SDR"
        self.stages = {}
        self.fs = config.RTL_SAMPLE_RATE
        self._gains = None

    @property
    def gains(self):
        if self._gains is None:
            try:
                self._gains = dongles.gains_of(self.serial) if self.serial else []
            except dongles.DongleError:
                self._gains = []
        return self._gains

    def prepare(self):
        pass

    def describe_gain(self, gain):
        return f"{self.usable_gain(gain):g} dB"

    def usable_gain(self, gain):
        # -g 0 would be the tuner's own automatic gain, which overloads on FM
        return max(float(gain), 0.9) if gain is not None else self.default_gain

    def _cmd(self, center_hz, gain, ppm):
        cmd = ["rtl_sdr", "-f", str(center_hz), "-s", str(self.fs),
               "-g", f"{self.usable_gain(gain):g}"]
        if ppm:
            cmd += ["-p", str(ppm)]
        return cmd

    def stream_cmd(self, center_hz, gain, ppm):
        return self._cmd(center_hz, gain, ppm) + ["-d", str(self.index), "-"]

    def capture_cmd(self, center_hz, gain, ppm, samples):
        return self._cmd(center_hz, gain, ppm) + ["-d", str(self.index), "-n",
                                                  str(samples), "-"]

    @staticmethod
    def iq(raw):
        from channelizer import iq_from_u8
        return iq_from_u8(raw)

    @staticmethod
    def clip_fraction(raw):
        """Share of the 8-bit ADC samples at either end (the ADC overloads)."""
        a = np.frombuffer(raw, dtype=np.uint8)
        if not len(a):
            return 0.0
        return float(np.count_nonzero((a == 0) | (a == 255))) / len(a)


class Airspy:
    kind = "airspy"
    tool = "airspy_rx"
    fmt = "s16"
    bytes_per_sample = 4          # I and Q, two bytes each
    has_ppm = False
    zero_is_auto = False          # linearity 0 is just the lowest gain
    default_gain = AIRSPY_DEFAULT_GAIN

    def __init__(self, serial, rates=(), model="Airspy", stages=None, rate=None):
        self.index = None
        self.serial = dongles.normalize_serial(serial)
        self.model = model
        if rate and rates and rate not in rates:
            raise dongles.DongleError(
                f"{model} {self.serial} cannot run at {rate / 1e6:g} MS/s (sample_rate: "
                "in receivers.yml); it takes "
                + ", ".join(f"{r / 1e6:g}M" for r in sorted(rates)))
        self.fs = rate or pick_rate(rates, model)
        self.gains = list(dongles.AIRSPY_GAINS)
        # LNA / Mixer / VGA fixed by hand ({"lna": 4, …}); the others follow
        # the linearity step
        self.stages = {k: v for k, v in (stages or {}).items() if v is not None}

    def usable_gain(self, gain):
        g = self.default_gain if gain is None else float(gain)
        return int(round(min(max(g, 0), 21)))

    def stage_values(self, gain):
        """(LNA, Mixer, VGA) at this linearity step, with the fixed ones."""
        lna, mixer, vga = AIRSPY_LINEARITY[self.usable_gain(gain)]
        return (self.stages.get("lna", lna), self.stages.get("mixer", mixer),
                self.stages.get("vga", vga))

    def _cmd(self, center_hz, gain, ppm):
        # -f in MHz; -a by value (above 1 000 000 it is not an index); -t 2:
        # INT16_IQ; no -b (bias tee) and no -p (packing, not ppm)
        cmd = ["airspy_rx", "-s", self.serial, "-f", f"{center_hz / 1e6:.6f}",
               "-a", str(self.fs), "-t", "2"]
        if not self.stages:
            return cmd + ["-g", str(self.usable_gain(gain))]
        # By hand: each stage on its own (airspy_rx sets them only without
        # -g; the AGCs are turned off first, see prepare())
        lna, mixer, vga = self.stage_values(gain)
        return cmd + ["-l", str(lna), "-m", str(mixer), "-v", str(vga)]

    def describe_gain(self, gain):
        lna, mixer, vga = self.stage_values(gain)
        how = "by hand" if self.stages else f"linearity {self.usable_gain(gain)}"
        return f"LNA {lna}, Mixer {mixer}, VGA {vga} ({how})"

    def prepare(self, agc_off=None):
        """Before airspy_rx with the stages by hand: turn both AGCs off. The
        tuner keeps them between programs (until it is unplugged), so an
        AGC another program turned on would override LNA and Mixer; -g
        turns them off by itself, -l/-m/-v do not."""
        if not self.stages:
            return
        (agc_off or _airspy_agc_off)(self.serial)

    def stream_cmd(self, center_hz, gain, ppm):
        return self._cmd(center_hz, gain, ppm) + ["-r", "-"]

    def capture_cmd(self, center_hz, gain, ppm, samples):
        return self._cmd(center_hz, gain, ppm) + ["-n", str(samples), "-r", "-"]

    @staticmethod
    def iq(raw):
        from channelizer import iq_from_s16
        return iq_from_s16(raw)

    @staticmethod
    def clip_fraction(raw):
        """Share of the I/Q values near what a full ADC gives."""
        a = np.frombuffer(raw[:len(raw) // 2 * 2], dtype=np.int16)
        if not len(a):
            return 0.0
        return float(np.count_nonzero(np.abs(a.astype(np.int32)) >= AIRSPY_CLIP)) / len(a)


def _airspy_agc_off(serial):
    """libairspy: open the Airspy, LNA AGC and Mixer AGC off, close."""
    import ctypes
    lib = ctypes.CDLL("libairspy.so.0")
    lib.airspy_open_sn.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint64]
    lib.airspy_set_lna_agc.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
    lib.airspy_set_mixer_agc.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
    lib.airspy_close.argtypes = [ctypes.c_void_p]
    dev = ctypes.c_void_p()
    rc = lib.airspy_open_sn(ctypes.byref(dev), int(serial, 16))
    if rc != 0:
        raise dongles.DongleError(f"Could not open Airspy {serial} to turn its AGC "
                                  f"off (libairspy error {rc}): is it in use?")
    try:
        for name in ("airspy_set_lna_agc", "airspy_set_mixer_agc"):
            rc = getattr(lib, name)(dev, 0)
            if rc != 0:
                raise dongles.DongleError(f"{name} failed on Airspy {serial} "
                                          f"(libairspy error {rc})")
    finally:
        lib.airspy_close(dev)


def pick_rate(rates, model=""):
    """The smallest sample rate that is a multiple of 250 kHz and at least
    2.4 MS/s (Mini 3 MS/s, R2 2.5 MS/s)."""
    ok = sorted(r for r in rates or () if r % AIRSPY_MID_RATE == 0 and r >= 2_400_000)
    if ok:
        return ok[0]
    return 3_000_000 if "mini" in str(model).lower() else 2_500_000


def from_dongle(d, stages=None, rate=None):
    if d.kind == "airspy":
        return Airspy(d.serial, d.rates, d.product, stages, rate)
    return RtlSdr(d.index, d.serial, d.product)


def open(serial, stages=None, rate=None):   # noqa: A001 - sdr.open(serial) reads well
    """The receiver with this serial number. Raises dongles.DongleError.
    stages: an Airspy's LNA/Mixer/VGA by hand ({"lna": 4, …}, optional);
    rate: an Airspy's sample rate (None: the lowest that fits)."""
    return from_dongle(dongles.lookup(serial), stages, rate)


def for_receiver(r):
    """The radio for a receivers.Receiver, with its settings."""
    return open(r.serial, getattr(r, "stages", None), getattr(r, "sample_rate", None))


def capture(radio, center_hz, gain, ppm, seconds, run=subprocess.run, retries=1):
    """IQ bytes for a while (the first quarter second dropped: settling).
    Tried once more after a second: an Airspy can be held for a moment by
    airspy_info (another receiver being looked for)."""
    bps = radio.bytes_per_sample
    samples = int((seconds + 0.25) * radio.fs)
    for attempt in range(retries + 1):
        proc = run(radio.capture_cmd(center_hz, gain, ppm, samples), capture_output=True,
                   timeout=seconds + 30)
        raw = proc.stdout
        if len(raw) >= radio.fs * bps // 2:   # at least half a second
            return raw[int(0.25 * radio.fs) * bps:]
        if attempt < retries:
            time.sleep(1)
    raise RuntimeError(f"{radio.tool} gave no samples: "
                       f"{proc.stderr.decode('utf-8', 'replace')[-200:]}")


def capture_measure(radio, center_hz, gain, ppm, seconds, offsets, popen=subprocess.Popen):
    """A capture decoded while it comes in, for high rates: (clip fraction,
    level dBFS, [MPX per offset]). Only the MPX at 171 kHz is kept."""
    from channelizer import make_stations
    bps = radio.bytes_per_sample
    settle = int(0.25 * radio.fs)
    samples = int((seconds + 0.25) * radio.fs)
    stations, block = make_stations(offsets, radio.fs, pipe=False)
    parts = [[] for _ in stations]
    clipped = power = counted = 0.0
    proc = popen(radio.capture_cmd(center_hz, gain, ppm, samples), stdout=subprocess.PIPE,
                 stderr=subprocess.DEVNULL)
    try:
        skipped = 0
        while skipped < settle:
            chunk = proc.stdout.read(min(settle - skipped, block) * bps)
            if not chunk:
                break
            skipped += len(chunk) // bps
        while True:
            raw = proc.stdout.read(block * bps)
            if len(raw) < block * bps:
                break
            clipped += radio.clip_fraction(raw) * block * 2
            z = radio.iq(raw)
            power += float(np.sum(np.abs(z) ** 2))
            counted += block
            for st, out in zip(stations, parts):
                m = st.demod(z)
                if m is not None:
                    out.append(m)
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
    if counted < radio.fs * min(seconds, 1.0) / 2:
        raise RuntimeError(f"{radio.tool} gave no samples (busy or gone?)")
    level = 10 * np.log10(power / counted) if power > 0 else -120.0
    return (clipped / (counted * 2), round(float(level), 1),
            [np.concatenate(p) if p else np.zeros(0, dtype=np.float32) for p in parts])
