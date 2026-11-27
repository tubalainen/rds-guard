"""The satellite's receivers: one per RTL-SDR dongle, from receivers.yml.

    receivers:
      - serial: "00000101"          # required: the dongle's serial number
        name: tak                   # optional, shown on the aggregator
        frequencies: [103.5M, 102.9M]
        # gain: auto (default) or e.g. 28.0 to override the calibration
        # ppm: auto (default) or e.g. 12

Without the file and with one dongle, FM_FREQUENCY / FM_FREQUENCIES are used
on it (as before v1.0). With several dongles the file is needed.
"""

import os
from dataclasses import dataclass, field

import config

MAX_STATIONS = 4
MAX_SPAN_HZ = 2_000_000          # usable bandwidth at 2.394 MS/s
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
    warnings: list = field(default_factory=list)

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


def check(receiver, where):
    hz = receiver.freqs_hz
    if not hz:
        raise ReceiverError(f"{where}: no frequencies")
    if len(hz) > MAX_STATIONS:
        raise ReceiverError(f"{where}: {len(hz)} frequencies, at most {MAX_STATIONS} "
                            "per dongle")
    if max(hz) - min(hz) > MAX_SPAN_HZ:
        raise ReceiverError(f"{where}: the frequencies span "
                            f"{(max(hz) - min(hz)) / 1e6:.2f} MHz; one dongle covers "
                            "2 MHz. Put the others on another dongle")
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
        serial = str(item.get("serial") or "").strip()
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
                     ppm=_auto_or(item.get("ppm"), int, f"{where}, ppm"))
        check(r, where)
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
    serial = config.RTL_DEVICE_SERIAL
    if not serial:
        if dongles is None:
            import dongles as dg
            dongles = dg.find_all()
        if not dongles:
            raise ReceiverError("No RTL-SDR dongle found. Check that it is plugged in, "
                                "passed to the container (devices: /dev/bus/usb) and "
                                "that the DVB driver is blacklisted on the host")
        if len(dongles) > 1:
            raise ReceiverError(
                f"{len(dongles)} RTL-SDR dongles are connected ("
                + ", ".join(d.serial for d in dongles)
                + "): say which listens to what in receivers.yml (see "
                "receivers.example.yml)")
        serial = dongles[0].serial
    r = Receiver(serial=serial, frequencies=[_freq(f, "FM_FREQUENCY") for f in freqs],
                 gain=_auto_or(config.RTL_GAIN or None, float, "RTL_GAIN"),
                 ppm=_auto_or(config.PPM_CORRECTION or None, int, "PPM_CORRECTION"))
    check(r, "FM_FREQUENCIES")
    if config.RTL_GAIN:
        r.warnings.append("RTL_GAIN is read as a fixed gain for this dongle; remove "
                          "it to let RDS Guard find the best gain, or move it to "
                          "receivers.yml")
    return [r]
