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

The file is the only place for the radio. Without it the satellite writes
one: from FM_FREQUENCY in .env once (the move from before v1.1), otherwise
a template with the connected receivers commented out, to be filled in.
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
# Every receiver leaves a weak mirror of each station on the other side of
# the centre (I/Q imbalance, -25 to -45 dB). Two stations placed
# symmetrically (the midpoint of two) see each other's mirror, and when one
# is weak or absent its channel decodes the other's mirror instead: P4
# Värmland on the 102.8 channel. So the centre is chosen where no mirror
# lands within MIRROR_GUARD_HZ of a station and no station within
# DC_GUARD_HZ of the centre.
MIRROR_GUARD_HZ = 250_000
DC_GUARD_HZ = 150_000
CENTER_STEP_HZ = 5_000


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
        return self.placement()[0]

    def placement(self):
        """(centre Hz, True when no station sits on another's mirror)."""
        hz = self.freqs_hz
        if len(hz) == 1:
            return hz[0] + SINGLE_OFFSET_HZ, True
        return place_center(hz, max(self.max_span_hz // 2, MAX_SPAN_HZ // 2))


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


def place_center(hz, half):
    """The centre for these stations: every station within half (Hz) of it,
    no station's mirror within MIRROR_GUARD_HZ of another station, none
    within DC_GUARD_HZ of the centre; the nearest such to the midpoint.
    (centre, ok); when none exists, the one that keeps them furthest apart,
    and ok False."""
    mid = min(hz) + (max(hz) - min(hz)) // 2
    lo, hi = max(hz) - half, min(hz) + half

    def margin(c):
        mirror = min(abs((a - c) + (b - c)) for i, a in enumerate(hz) for b in hz[i + 1:])
        dc = min(abs(a - c) for a in hz)
        return min(mirror / MIRROR_GUARD_HZ, dc / DC_GUARD_HZ)
    steps = range(0, max(0, (hi - lo)) // CENTER_STEP_HZ + 1)
    candidates = sorted({c for k in steps for c in (mid - k * CENTER_STEP_HZ,
                                                    mid + k * CENTER_STEP_HZ)
                         if lo <= c <= hi} or {mid}, key=lambda c: (abs(c - mid), c))
    for c in candidates:
        if margin(c) >= 1:
            return c, True
    return max(candidates, key=lambda c: (margin(c), -abs(c - mid))), False


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


def _mirror_warning(r):
    center, ok = r.placement()
    if not ok:
        r.warnings.append(
            "the stations cannot all be kept off each other's mirror (centre "
            f"{center / 1e6:.3f} MHz): a weak station may decode a neighbour's "
            "mirror instead of itself. Spread them less, or over two receivers")


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
        _mirror_warning(r)
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


# ---------------------------------------------------------------------------
# receivers.yml: the template the satellite writes, and the one-time move
# from FM_FREQUENCY in .env (before v1.1)
# ---------------------------------------------------------------------------

_HEAD = """\
# rds-data/receivers.yml: which receiver listens to which stations.
#
# Remove the # at the start of the lines for your receiver (just the #),
# change the frequencies (and the serial number, if it is an example) and
# save. A satellite waiting for this file picks it up within a minute; a
# running one needs: docker compose restart rds-guard
#
# Serial numbers: the satellite's log lists the receivers it finds, or
#   docker compose exec rds-guard python3 -m dongles
# Frequencies: your local P4 station, e.g. 103.5M (README: "P4 regional
# frequencies"). Up to 4 stations within 2 MHz on one receiver.

receivers:
"""

# Per kind: (heading, an example serial number, example frequencies)
_EXAMPLES = {
    "rtlsdr": ("An RTL-SDR dongle", "00000001", ["103.5M"]),
    "airspy": ("An Airspy Mini or R2", "0x644064DC2B1C2E4F", ["103.5M", "102.9M"]),
}

_TAIL = """
# --- More, all optional. Copy a line under a receiver, below its
# frequencies:, and remove the # at the start ---
#    name: tak           # shown on the aggregator
#    gain: 28.0          # fixed gain in dB (default: calibrated by itself)
#    ppm: 12             # fixed frequency correction (an RTL-SDR only)
#  An Airspy only:
#    sample_rate: 10M    # a wider capture, 10M (R2) or 6M (Mini): up to 8
#                        # stations within 7.8 / 4.6 MHz
#    lna: 4              # its stages by hand: lna 0-14, mixer 0-15,
#    mixer: 3            # vga 0-15 (all three: no gain calibration)
#    vga: 9
#
# Two dongles with the same serial number (00000001 is common) cannot be
# told apart: give one a new number with rtl_eeprom (the log says how).
"""

EMPTY = ("Fill in {path}: remove the # in front of the lines for your receiver "
         "and set its frequencies (the file says how)")
NO_RECEIVER = ("No RTL-SDR dongle or Airspy found. Check that it is plugged in, "
               "passed to the container (devices: /dev/bus/usb) and, for an "
               "RTL-SDR, that the DVB driver is blacklisted on the host")
_ENV_KEYS = "FM_FREQUENCY, FM_FREQUENCIES and RTL_DEVICE_SERIAL"


def _entry(title, serial, freqs, commented):
    c = "#" if commented else ""
    return (f"\n  # --- {title} ---\n"
            f"  {c}- serial: \"{serial}\"\n"
            f"  {c}  frequencies: [{', '.join(freqs)}]\n")


def template(connected=(), active=None):
    """receivers.yml as the satellite writes it. connected: the receivers
    found (dongles.Dongle), first, with their real serial numbers and
    commented out. active: a Receiver written uncommented, as the one in use
    (the move from .env). A kind with nothing connected gets an example."""
    import dongles
    out = [_HEAD]
    kinds = set()
    if active is not None:
        out.append(_entry("In use (moved from FM_FREQUENCY in .env)",
                          active.serial, active.frequencies, False))
        kinds.add("airspy" if dongles.is_airspy_serial(active.serial) else "rtlsdr")
    for d in connected:
        if active is not None and d.serial == active.serial:
            continue
        kind = "airspy" if d.kind == "airspy" else "rtlsdr"
        kinds.add(kind)
        title = "This Airspy" if kind == "airspy" else "This RTL-SDR dongle"
        out.append(_entry(f"{title} (connected now)", d.serial, ["103.5M"], True))
    for kind, (title, serial, freqs) in _EXAMPLES.items():
        if kind not in kinds:
            out.append(_entry(title, serial, freqs, True))
    out.append(_TAIL)
    return "".join(out)


def _write_new(path, text):
    """Write a receivers.yml that is not there yet, owned like its folder so
    that it can be edited without sudo (the container runs as root).
    Returns False when it could not be written."""
    folder = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(folder, exist_ok=True)
        with open(path, "x", encoding="utf-8") as f:
            f.write(text)
    except FileExistsError:
        return True
    except OSError:
        return False
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            st = os.stat(folder)
            os.chown(path, st.st_uid, st.st_gid)
        except OSError:
            pass
    return True


def _from_env():
    """The frequencies FM_FREQUENCY / FM_FREQUENCIES in .env (before v1.1)."""
    return list(config.FM_FREQUENCIES) or ([config.FM_FREQUENCY] if config.FM_FREQUENCY else [])


def load(path=None, dongles=None):
    """The receivers to run, from receivers.yml. dongles: the connected ones
    (dongles.find_all), used only when there is no receivers.yml yet.

    Without the file the satellite writes one: from FM_FREQUENCY in .env
    when that is set (the move from before v1.1), otherwise a template with
    the connected receivers, and says to fill it in (ReceiverError)."""
    path = path or config.RECEIVERS_FILE
    if not os.path.isfile(path):
        _create(path, dongles)
    import yaml
    with open(path, encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ReceiverError(f"{path} is not valid YAML: {e}")
    # Everything still commented out
    if data is None or (isinstance(data, dict) and data.get("receivers") is None):
        raise ReceiverError(EMPTY.format(path=path))
    out = parse(data)
    if _from_env() or config.RTL_DEVICE_SERIAL:
        out[0].warnings.append(f"{_ENV_KEYS} in .env are not used: the receivers "
                               f"are in {path}. Remove them from .env")
    return out


def _create(path, dongles):
    """No receivers.yml: write one (see load). Returns when the file it
    wrote has a receiver in use; raises ReceiverError otherwise."""
    import dongles as dg
    if dongles is None:
        try:
            dongles = dg.find_all()
        except Exception:
            dongles = []
    cannot = (f"Could not write {path}: is the data folder writable? Write it "
              "yourself (see receivers.example.yml)")
    freqs = _from_env()
    if freqs:
        serial = dg.normalize_serial(config.RTL_DEVICE_SERIAL)
        if not serial and len(dongles) == 1:
            serial = dongles[0].serial
        if serial:
            r = Receiver(serial=serial,
                         frequencies=[_freq(f, "FM_FREQUENCY") for f in freqs])
            check(r, "FM_FREQUENCIES")
            if not _write_new(path, template(dongles, active=r)):
                raise ReceiverError(cannot)
            import logging
            logging.getLogger("rds-guard").warning(
                "Moved FM_FREQUENCY from .env to %s (receiver %s: %s). The radio is "
                "set there from now on; remove %s from .env",
                path, serial, ", ".join(r.frequencies), _ENV_KEYS)
            return
        if not dongles:
            # Nothing to write the move for yet: try again when it is there
            raise ReceiverError(NO_RECEIVER)
    listed = (f" Connected now: {', '.join(d.serial for d in dongles)}." if dongles
              else f" {NO_RECEIVER}.")
    if not _write_new(path, template(dongles)):
        raise ReceiverError(cannot + "." + listed)
    raise ReceiverError(EMPTY.format(path=path) + "." + listed)
