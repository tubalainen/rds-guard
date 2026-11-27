"""The satellite's receivers: one per RTL-SDR dongle or Airspy, from
receivers.yml.

    receivers:
      - serial: "00000101"          # required: the dongle's serial number
                                    # (an Airspy's: "0x644064DC2B1C2E4F")
        name: tak                   # optional, shown on the aggregator
        frequencies: [103.5M, 102.9M]
        # gain: auto (default) or e.g. 28.0 to override the calibration
        # ppm: auto (default) or e.g. 12
        # An Airspy only, each optional: lna: 0-14, mixer: 0-15, vga: 0-15
        # An Airspy only, optional: sample_rate: 10M (R2) or 6M (Mini), for
        # stations up to ~7.8 / ~4.6 MHz apart and up to 8 of them

Without the file and with one dongle, FM_FREQUENCY / FM_FREQUENCIES are used
on it (as before v1.0). With several dongles the file is needed.
"""

import os
from dataclasses import dataclass, field

import config

MAX_STATIONS = 4
MAX_SPAN_HZ = 2_000_000          # usable bandwidth at 2.394 MS/s
# An Airspy's sample_rate: (R2: 2.5M, 10M; Mini: 3M, 6M). From 6 MS/s the
# span is 80 % of the rate less a channel, and up to 8 stations
AIRSPY_RATES = (2_500_000, 3_000_000, 6_000_000, 10_000_000)
WIDE_RATE = 6_000_000
MAX_STATIONS_WIDE = 8
# A single station is captured off-centre: away from the dongle's DC spike
SINGLE_OFFSET_HZ = 250_000


class ReceiverError(Exception):
    pass


@dataclass
class Receiver:
    serial: str
    frequencies: list                  # e.g. ["103.5M", "102.9M"]
    name: str = ""
    gain: object = "auto"              # "auto" or dB (float)
    ppm: object = "auto"               # "auto" or int
    # An Airspy's stages by hand (None: from the linearity step)
    lna: object = None
    mixer: object = None
    vga: object = None
    sample_rate: object = None         # an Airspy's, in S/s (None: the lowest that fits)
    warnings: list = field(default_factory=list)

    @property
    def max_span_hz(self):
        return span_for(self.sample_rate)

    @property
    def max_stations(self):
        return MAX_STATIONS_WIDE if (self.sample_rate or 0) >= WIDE_RATE else MAX_STATIONS

    @property
    def stages(self):
        """The Airspy stages set by hand: {"lna": 4, …} (may be empty)."""
        return {k: v for k, v in (("lna", self.lna), ("mixer", self.mixer),
                                  ("vga", self.vga)) if v is not None}

    @property
    def gain_fixed(self):
        """No gain to calibrate: gain: given, or all three stages by hand."""
        return self.gain != "auto" or len(self.stages) == 3

    @property
    def id(self):
        """How the receiver is named to the aggregator."""
        return self.name or self.serial

    @property
    def freqs_hz(self):
        return [config._parse_freq_hz(f) for f in self.frequencies]

    @property
    def center_hz(self):
        hz = self.freqs_hz
        if len(hz) == 1:
            return hz[0] + SINGLE_OFFSET_HZ
        return min(hz) + (max(hz) - min(hz)) // 2


def _freq(value, where):
    text = str(value).strip()
    try:
        hz = config._parse_freq_hz(text if text[-1:].upper() in "MK" or float(text) > 1e4
                                   else text + "M")
    except (ValueError, IndexError):
        raise ReceiverError(f"{where}: '{value}' is not a frequency (e.g. 103.5M)")
    if not 64_000_000 <= hz <= 110_000_000:
        raise ReceiverError(f"{where}: {value} is not an FM broadcast frequency")
    mhz = hz / 1e6
    return f"{mhz:g}M"


def _auto_or(value, kind, where):
    if value is None or str(value).strip().lower() in ("", "auto"):
        return "auto"
    try:
        return kind(value)
    except (TypeError, ValueError):
        raise ReceiverError(f"{where}: must be 'auto' or a number (got '{value}')")


STAGE_MAX = {"lna": 14, "mixer": 15, "vga": 15}


def span_for(rate):
    """How far apart a receiver's stations may be at this sample rate."""
    if not rate or rate < WIDE_RATE:
        return MAX_SPAN_HZ
    return int(0.8 * rate) - 200_000


def _rate(value, where):
    if value is None or str(value).strip().lower() in ("", "auto"):
        return None
    text = str(value).strip().upper().replace("MS/S", "M").replace("MSPS", "M")
    try:
        hz = float(text.rstrip("M")) * (1e6 if text.endswith("M") or float(text.rstrip("M")) < 100
                                        else 1)
    except ValueError:
        hz = -1
    hz = int(round(hz))
    if hz not in AIRSPY_RATES:
        raise ReceiverError(f"{where}: sample_rate must be one of "
                            + ", ".join(f"{r / 1e6:g}M" for r in AIRSPY_RATES)
                            + f" (got '{value}')")
    return hz


def _stage(value, name, where):
    if value is None or str(value).strip().lower() in ("", "auto"):
        return None
    try:
        v = int(str(value).strip())
    except ValueError:
        raise ReceiverError(f"{where}: {name} must be a whole number 0-{STAGE_MAX[name]} "
                            f"or left out (got '{value}')")
    if not 0 <= v <= STAGE_MAX[name]:
        raise ReceiverError(f"{where}: {name} must be 0-{STAGE_MAX[name]} (got {v})")
    return v


def check(receiver, where):
    hz = receiver.freqs_hz
    if not hz:
        raise ReceiverError(f"{where}: no frequencies")
    most = getattr(receiver, "max_stations", MAX_STATIONS)
    span = getattr(receiver, "max_span_hz", MAX_SPAN_HZ)
    if len(hz) > most:
        raise ReceiverError(f"{where}: {len(hz)} frequencies, at most {most} "
                            "per dongle" + ("" if most > MAX_STATIONS else
                                            " (an Airspy at sample_rate: 10M or 6M takes 8)"))
    if max(hz) - min(hz) > span:
        raise ReceiverError(f"{where}: the frequencies span "
                            f"{(max(hz) - min(hz)) / 1e6:.2f} MHz; one dongle covers "
                            f"{span / 1e6:g} MHz. Put the others on another dongle"
                            + ("" if span > MAX_SPAN_HZ else
                               " (or an Airspy at sample_rate: 10M: 7.8 MHz)"))
    if len(set(hz)) != len(hz):
        raise ReceiverError(f"{where}: a frequency is listed twice")


def parse(data):
    """The YAML document (as Python data) -> [Receiver]."""
    if not isinstance(data, dict) or not isinstance(data.get("receivers"), list):
        raise ReceiverError("receivers.yml must have a 'receivers:' list")
    out = []
    for i, item in enumerate(data["receivers"], 1):
        where = f"receivers.yml, receiver {i}"
        if not isinstance(item, dict):
            raise ReceiverError(f"{where}: must be a mapping")
        import dongles
        serial = dongles.normalize_serial(item.get("serial") or "")
        if not serial:
            raise ReceiverError(f"{where}: 'serial' is required (the dongle's serial "
                                "number; 'python3 -m dongles' lists them)")
        freqs = item.get("frequencies")
        if isinstance(freqs, (str, int, float)):
            freqs = [freqs]
        if not isinstance(freqs, list):
            raise ReceiverError(f"{where}: 'frequencies' must be a list")
        r = Receiver(serial=serial, name=str(item.get("name") or "").strip()[:32],
                     frequencies=[_freq(f, where) for f in freqs],
                     gain=_auto_or(item.get("gain"), float, f"{where}, gain"),
                     ppm=_auto_or(item.get("ppm"), int, f"{where}, ppm"),
                     sample_rate=_rate(item.get("sample_rate"), where),
                     **{k: _stage(item.get(k), k, where) for k in STAGE_MAX})
        airspy = dongles.is_airspy_serial(serial)
        if r.sample_rate and not airspy:
            r.warnings.append("sample_rate: is for an Airspy; an RTL-SDR runs at 2.4 MS/s")
            r.sample_rate = None
        check(r, where)
        if airspy and r.ppm != "auto":
            r.warnings.append("ppm: is not used for an Airspy (its clock is a TCXO)")
        if r.stages and not airspy:
            r.warnings.append("lna:/mixer:/vga: are for an Airspy; an RTL-SDR uses gain:")
            r.lna = r.mixer = r.vga = None
        elif len(r.stages) == 3 and r.gain != "auto":
            r.warnings.append("gain: is not used: lna:, mixer: and vga: are all set")
        out.append(r)
    if not out:
        raise ReceiverError("receivers.yml lists no receivers")
    serials = [r.serial for r in out]
    for s in set(serials):
        if serials.count(s) > 1:
            raise ReceiverError(f"receivers.yml: serial number {s} is listed twice")
    ids = [r.id for r in out]
    for n in set(ids):
        if ids.count(n) > 1:
            raise ReceiverError(f"receivers.yml: the name '{n}' is used twice")
    seen = {}
    for r in out:
        for f in r.frequencies:
            if f in seen:
                r.warnings.append(f"{f} is also on receiver {seen[f]}: the same "
                                  "broadcast will be heard twice (the aggregator "
                                  "keeps it as one)")
            seen[f] = r.id
    return out


def load(path=None, dongles=None):
    """The receivers to run. dongles: the connected ones (dongles.find_all),
    used when there is no receivers.yml."""
    path = path or config.RECEIVERS_FILE
    if os.path.isfile(path):
        import yaml
        with open(path, encoding="utf-8") as f:
            try:
                data = yaml.safe_load(f)
            except yaml.YAMLError as e:
                raise ReceiverError(f"{path} is not valid YAML: {e}")
        return parse(data)
    # No file: one dongle, the frequencies from .env
    freqs = config.FM_FREQUENCIES or [config.FM_FREQUENCY]
    import dongles as dg
    serial = dg.normalize_serial(config.RTL_DEVICE_SERIAL)
    if not serial:
        if dongles is None:
            dongles = dg.find_all()
        if not dongles:
            raise ReceiverError("No RTL-SDR dongle or Airspy found. Check that it is "
                                "plugged in, passed to the container (devices: "
                                "/dev/bus/usb) and, for an RTL-SDR, that the DVB driver "
                                "is blacklisted on the host")
        if len(dongles) > 1:
            raise ReceiverError(
                f"{len(dongles)} receivers are connected ("
                + ", ".join(d.serial for d in dongles)
                + "): say which listens to what in receivers.yml (see "
                "receivers.example.yml)")
        serial = dongles[0].serial
    r = Receiver(serial=serial, frequencies=[_freq(f, "FM_FREQUENCY") for f in freqs])
    check(r, "FM_FREQUENCIES")
    if config.RTL_GAIN or config.PPM_CORRECTION:
        r.warnings.append("RTL_GAIN and PPM_CORRECTION in .env are not used any more: "
                          "RDS Guard finds the best gain and ppm itself. To fix them, "
                          "put gain:/ppm: in receivers.yml (see receivers.example.yml)")
    return [r]
