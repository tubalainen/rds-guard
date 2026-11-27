"""The RTL-SDR dongles on this host, from rtl_test.

    docker compose exec rds-guard python3 -m dongles

lists them: index, name and serial number, and with --gains the gain steps
each tuner allows. A dongle is always chosen by its serial number, never by
its index: the index changes with the USB order.
"""

import re
import subprocess
import sys
from dataclasses import dataclass, field

_DEVICE_RE = re.compile(r"^\s*(\d+):\s*(.*?),\s*(.*?),\s*SN:\s*(\S*)\s*$", re.MULTILINE)
_GAINS_RE = re.compile(r"Supported gain values \(\d+\):\s*([-\d.\s]+)")
_TUNER_RE = re.compile(r"Found (.+?) tuner")


@dataclass
class Dongle:
    index: int
    vendor: str
    product: str
    serial: str
    gains: list = field(default_factory=list)
    tuner: str = ""


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


def find_all(with_gains=False):
    """The dongles on this host. Raises DongleError when rtl_test fails."""
    text = _rtl_test(("-t",), timeout=10)
    dongles = parse_devices(text)
    if with_gains:
        for d in dongles:
            info = _rtl_test(("-d", str(d.index), "-t"), timeout=10)
            d.gains = parse_gains(info)
            m = _TUNER_RE.search(info)
            d.tuner = m.group(1) if m else ""
    return dongles


def index_of(serial, dongles=None):
    """The current index of the dongle with this serial number."""
    dongles = find_all() if dongles is None else dongles
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
    dongles = find_all() if dongles is None else dongles
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
        print("No RTL-SDR dongles found.")
        return
    for d in dongles:
        print(f"index {d.index}  serial {d.serial:<12} {d.vendor}, {d.product}"
              + (f"  tuner {d.tuner}" if d.tuner else "")
              + (f"\n          gains: {' '.join(f'{g:g}' for g in d.gains)}" if d.gains else ""))
    for serial in duplicates(dongles):
        print("\nWARNING: " + duplicate_help(dongles, serial))


if __name__ == "__main__":
    main()
