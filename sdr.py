"""The two kinds of receiver the satellite drives, behind one interface.

  RtlSdr  rtl_sdr, 2.394 MS/s, unsigned 8-bit IQ, gain in dB steps, ppm
  Airspy  airspy_rx (Mini 3 MS/s, R2 2.5 MS/s), signed 16-bit IQ, the
          "linearity" gain 0-21, no ppm (its clock is a TCXO)

Both give the channelizer IQ it turns into 171 kHz MPX for redsea, so
everything after the capture (redsea, recording, the calibration) is the
same for both. open(serial) finds which one a serial number is.
"""

import subprocess

import numpy as np

import config
import dongles

# The Airspy is decimated to 250 kHz (3 MS/s / 12, 2.5 MS/s / 10), then
# resampled to 171 kHz
AIRSPY_MID_RATE = 250_000
AIRSPY_DEFAULT_GAIN = 12.0       # linearity, before the calibration
AIRSPY_CLIP = 32_000             # |I| or |Q| this high: the ADC is (near) full


class RtlSdr:
    kind = "rtlsdr"
    tool = "rtl_sdr"
    fmt = "u8"
    bytes_per_sample = 2          # I and Q, one byte each
    has_ppm = True
    zero_is_auto = True           # rtl_sdr -g 0: the tuner's AGC, never used
    default_gain = 20.0

    def __init__(self, index, serial="", product="RTL-SDR"):
        self.index = index
        self.serial = serial
        self.model = f"RTL-SDR ({product})" if product else "RTL-SDR"
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

    def __init__(self, serial, rates=(), model="Airspy"):
        self.index = None
        self.serial = dongles.normalize_serial(serial)
        self.model = model
        self.fs = pick_rate(rates, model)
        self.gains = list(dongles.AIRSPY_GAINS)

    def usable_gain(self, gain):
        g = self.default_gain if gain is None else float(gain)
        return int(round(min(max(g, 0), 21)))

    def _cmd(self, center_hz, gain, ppm):
        # -f in MHz; -a by value (above 1 000 000 it is not an index); -t 2:
        # INT16_IQ; no -b (bias tee) and no -p (packing, not ppm)
        return ["airspy_rx", "-s", self.serial, "-f", f"{center_hz / 1e6:.6f}",
                "-a", str(self.fs), "-t", "2", "-g", str(self.usable_gain(gain))]

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
        """Share of the I/Q values near full scale (12-bit ADC, scaled to 16)."""
        a = np.frombuffer(raw[:len(raw) // 2 * 2], dtype=np.int16)
        if not len(a):
            return 0.0
        return float(np.count_nonzero(np.abs(a.astype(np.int32)) >= AIRSPY_CLIP)) / len(a)


def pick_rate(rates, model=""):
    """The smallest sample rate that is a multiple of 250 kHz and at least
    2.4 MS/s (Mini 3 MS/s, R2 2.5 MS/s)."""
    ok = sorted(r for r in rates or () if r % AIRSPY_MID_RATE == 0 and r >= 2_400_000)
    if ok:
        return ok[0]
    return 3_000_000 if "mini" in str(model).lower() else 2_500_000


def from_dongle(d):
    if d.kind == "airspy":
        return Airspy(d.serial, d.rates, d.product)
    return RtlSdr(d.index, d.serial, d.product)


def open(serial):                  # noqa: A001 - sdr.open(serial) reads well
    """The receiver with this serial number. Raises dongles.DongleError."""
    return from_dongle(dongles.lookup(serial))


def capture(radio, center_hz, gain, ppm, seconds, run=subprocess.run):
    """IQ bytes for a while (the first quarter second dropped: settling)."""
    bps = radio.bytes_per_sample
    samples = int((seconds + 0.25) * radio.fs)
    proc = run(radio.capture_cmd(center_hz, gain, ppm, samples), capture_output=True,
               timeout=seconds + 30)
    raw = proc.stdout
    if len(raw) < radio.fs * bps // 2:   # not even half a second: busy or gone
        raise RuntimeError(f"{radio.tool} gave no samples: "
                           f"{proc.stderr.decode('utf-8', 'replace')[-200:]}")
    return raw[int(0.25 * radio.fs) * bps:]
