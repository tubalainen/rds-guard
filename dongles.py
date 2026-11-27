"""The receivers on this host: RTL-SDR dongles (rtl_test) and Airspy Mini /
R2 (airspy_info).

    docker compose exec rds-guard python3 -m dongles

lists them: index, name and serial number, and with --gains the gain steps
each tuner allows. A dongle is always chosen by its serial number, never by
its index: the index changes with the USB order. An Airspy's serial number
is 64 bits in hex (0x…); airspy_info cannot open an Airspy that is in use,
so one the satellite is running is not listed.
"""

import re
import subprocess
import sys
from dataclasses import dataclass, field

_DEVICE_RE = re.compile(r"^\s*(\d+):\s*(.*?),\s*(.*?),\s*SN:\s*(\S*)\s*$", re.MULTILINE)
_GAINS_RE = re.compile(r"Supported gain values \(\d+\):\s*([-\d.\s]+)")
_TUNER_RE = re.compile(r"Found (.+?) tuner")
_AIRSPY_RE = re.compile(r"Found AirSpy board (\d+)(.*?)(?=Found AirSpy board|\Z)", re.S)
_AIRSPY_SN_RE = re.compile(r"Serial Number:\s*(0x[0-9A-Fa-f]+)")
_AIRSPY_FW_RE = re.compile(r"Firmware Version:\s*(.*)")
_AIRSPY_RATE_RE = re.compile(r"^\s*([\d.]+)\s*MSPS", re.M)
# airspy_rx -g: the "linearity" gain, 0 (least) to 21 (most)
AIRSPY_GAINS = [float(g) for g in range(22)]


@dataclass
class Dongle:
    index: int
    vendor: str
    product: str
    serial: str
    gains: list = field(default_factory=list)
    tuner: str = ""
    kind: str = "rtlsdr"               # rtlsdr | airspy
    rates: list = field(default_factory=list)   # Airspy: its sample rates (S/s)


class DongleError(Exception):
    pass


def _rtl_test(args=(), timeout=10):
    try:
        proc = subprocess.run(["rtl_test", *args], capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        raise DongleError("rtl_test not found")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"") + (e.stderr or b"")
        return out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
    return (proc.stdout or "") + (proc.stderr or "")


def parse_devices(text):
    """rtl_test's device list -> [Dongle]."""
    return [Dongle(int(m.group(1)), m.group(2).strip(), m.group(3).strip(),
                   m.group(4).strip()) for m in _DEVICE_RE.finditer(text)]


def parse_gains(text):
    """'Supported gain values (29): 0.0 0.9 1.4 …' -> [0.0, 0.9, 1.4, …]."""
    m = _GAINS_RE.search(text)
    if not m:
        return []
    return [float(v) for v in m.group(1).split()]


def duplicates(dongles):
    """Serial numbers more than one dongle has."""
    seen, dup = set(), set()
    for d in dongles:
        if d.serial in seen:
            dup.add(d.serial)
        seen.add(d.serial)
    return sorted(dup)


def duplicate_help(dongles, serial):
    idx = [d.index for d in dongles if d.serial == serial]
    return (f"Several RTL-SDR dongles have serial number {serial} (indexes "
            f"{', '.join(map(str, idx))}), so they cannot be told apart. Give each "
            f"its own: stop RDS Guard, plug in one at a time and run e.g. "
            f"'rtl_eeprom -d {idx[-1]} -s 00000102' on the host (then unplug and "
            f"plug it in again).")


def parse_airspy_info(text):
    """airspy_info's output -> [Dongle] (kind airspy)."""
    out = []
    for m in _AIRSPY_RE.finditer(text):
        block = m.group(2)
        sn = _AIRSPY_SN_RE.search(block)
        if not sn:
            continue
        fw = _AIRSPY_FW_RE.search(block)
        firmware = fw.group(1).strip() if fw else ""
        rates = sorted(int(round(float(r) * 1e6)) for r in _AIRSPY_RATE_RE.findall(block))
        out.append(Dongle(index=int(m.group(1)) - 1, vendor="Airspy",
                          product=airspy_model(firmware, rates),
                          serial=normalize_serial(sn.group(1)), gains=list(AIRSPY_GAINS),
                          tuner="R820T2", kind="airspy", rates=rates))
    return out


def airspy_model(firmware, rates):
    """'Airspy Mini' or 'Airspy R2', from the firmware string or the rates
    (Mini: 6 and 3 MS/s; R2: 10 and 2.5 MS/s)."""
    if "MINI" in firmware.upper() or 3_000_000 in rates or 6_000_000 in rates:
        return "Airspy Mini"
    return "Airspy R2"


def normalize_serial(value):
    """A serial number as written in receivers.yml or .env -> as listed.
    An Airspy's is 0x + 16 hex digits (YAML reads an unquoted 0x… as a
    number); an RTL-SDR's is kept as it is."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"0x{value:016X}" if value > 0xFFFFFFFF else str(value)
    text = str(value or "").strip()
    if re.fullmatch(r"0[xX][0-9A-Fa-f]{1,16}", text):
        return f"0x{int(text, 16):016X}"
    return text


def _hex_value(serial):
    text = str(serial).strip()
    if text[:2].lower() == "0x":
        text = text[2:]
    try:
        return int(text, 16) if re.fullmatch(r"[0-9A-Fa-f]{1,16}", text) else None
    except ValueError:
        return None


def is_airspy_serial(serial):
    """Written the way an Airspy's serial number is (0x + up to 16 hex)."""
    return str(serial).strip()[:2].lower() == "0x" and _hex_value(serial) is not None


def same_airspy(a, b):
    va, vb = _hex_value(a), _hex_value(b)
    return va is not None and va == vb


def _airspy_info(timeout=10):
    try:
        proc = subprocess.run(["airspy_info"], capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        return ""
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
        return out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
    return proc.stdout or ""


# Airspys seen by serial: one that is running cannot be listed again
_airspy_seen = {}


def find_airspy():
    """The Airspys airspy_info can open now (also remembered)."""
    found = parse_airspy_info(_airspy_info())
    for d in found:
        _airspy_seen[d.serial] = d
    return found


def find_rtl():
    """The RTL-SDR dongles (rtl_test only). Raises DongleError."""
    return parse_devices(_rtl_test(("-t",), timeout=10))


def find_all(with_gains=False):
    """The dongles on this host. Raises DongleError when rtl_test fails."""
    dongles = find_rtl()
    if with_gains:
        for d in dongles:
            info = _rtl_test(("-d", str(d.index), "-t"), timeout=10)
            d.gains = parse_gains(info)
            m = _TUNER_RE.search(info)
            d.tuner = m.group(1) if m else ""
    return dongles + find_airspy()


def lookup(serial, found=None):
    """The connected dongle (RTL-SDR or Airspy) with this serial number.
    An Airspy seen before is not looked for again: while it runs,
    airspy_info cannot see it, and opening it would disturb it."""
    serial = normalize_serial(serial)
    for s, d in _airspy_seen.items():
        if same_airspy(s, serial):
            return d
    # An RTL-SDR is looked for with rtl_test alone: airspy_info opens every
    # free Airspy, which is only done when it is needed
    rtl_found = None
    if found is None:
        try:
            rtl_found = find_rtl()
        except DongleError:
            rtl_found = []
    rtl = [d for d in (found if found is not None else rtl_found) if d.kind != "airspy"]
    if serial in duplicates(rtl):
        raise DongleError(duplicate_help(rtl, serial))
    for d in rtl:
        if d.serial == serial:
            return d
    if found is None:
        found = rtl_found + find_airspy()
    for d in found:
        if d.kind == "airspy" and same_airspy(d.serial, serial):
            return d
    have = ", ".join(f"{d.serial} ({d.product})" for d in found) or "none"
    raise DongleError(f"No RTL-SDR or Airspy with serial number {serial} is connected "
                      f"(connected: {have})")


def index_of(serial, dongles=None):
    """The current index of the RTL-SDR with this serial number."""
    dongles = find_rtl() if dongles is None else dongles
    dongles = [d for d in dongles if d.kind != "airspy"]
    dup = duplicates(dongles)
    if serial in dup:
        raise DongleError(duplicate_help(dongles, serial))
    for d in dongles:
        if d.serial == serial:
            return d.index
    have = ", ".join(f"{d.serial} ({d.product})" for d in dongles) or "none"
    raise DongleError(f"No RTL-SDR with serial number {serial} is connected "
                      f"(connected: {have})")


def gains_of(serial, dongles=None):
    if is_airspy_serial(serial):
        return list(AIRSPY_GAINS)
    dongles = find_rtl() if dongles is None else dongles
    for d in dongles:
        if d.kind == "airspy" and same_airspy(d.serial, serial):
            return list(AIRSPY_GAINS)
    idx = index_of(serial, dongles)
    return parse_gains(_rtl_test(("-d", str(idx), "-t"), timeout=10))


def nearest(gains, value):
    """The allowed gain step nearest to value."""
    return min(gains, key=lambda g: abs(g - value)) if gains else value


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m dongles", description=__doc__.split("\n")[0])
    ap.add_argument("--gains", action="store_true", help="also list each tuner's gain steps")
    args = ap.parse_args(argv)
    try:
        dongles = find_all(with_gains=args.gains)
    except DongleError as e:
        sys.exit(str(e))
    if not dongles:
        print("No RTL-SDR or Airspy found (an Airspy in use is not listed: stop "
              "the satellite first).")
        return
    for d in dongles:
        if d.kind == "airspy":
            print(f"Airspy    serial {d.serial}  {d.product}"
                  + (f"  rates {', '.join(f'{r / 1e6:g}' for r in d.rates)} MS/s"
                     if d.rates else "")
                  + ("\n          gains: linearity 0-21" if args.gains else ""))
            continue
        print(f"index {d.index}  serial {d.serial:<12} {d.vendor}, {d.product}"
              + (f"  tuner {d.tuner}" if d.tuner else "")
              + (f"\n          gains: {' '.join(f'{g:g}' for g in d.gains)}" if d.gains else ""))
    for serial in duplicates([d for d in dongles if d.kind != "airspy"]):
        print("\nWARNING: " + duplicate_help(dongles, serial))


if __name__ == "__main__":
    main()
