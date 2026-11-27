"""Which side of the tuned frequency a station lands on, and the dongle's ppm.

Run it with the satellite stopped (it needs the dongle). It tunes 250 kHz
above and 250 kHz below the station, captures one second each with
rtl_sdr at a fixed gain, and prints where the station's power is and how
far its carrier is off. It changes nothing.

    python3 iq_side.py 103.5M [--gain 20] [--serial 00000001]
"""

import argparse
import subprocess
import sys

import numpy as np

FS = 2_394_000
OFFSET = 250_000


def hz(text):
    t = text.strip().upper()
    mult = 1e6 if t.endswith("M") else 1e3 if t.endswith("K") else 1.0
    return int(round(float(t.rstrip("MK")) * mult))


def capture(center, gain, index, seconds=1.0):
    n = int(FS * seconds) * 2
    cmd = ["rtl_sdr", "-f", str(center), "-s", str(FS), "-g", str(gain),
           "-n", str(n // 2), "-d", str(index), "-"]
    print(f"  capturing: {' '.join(cmd)}", flush=True)
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         timeout=30, check=False).stdout
    raw = np.frombuffer(out, dtype=np.uint8).astype(np.float32)
    raw = (raw - 127.5) / 127.5
    return raw[0::2] + 1j * raw[1::2]


def band_power(z, f0, width=100_000):
    """Power within ±width/2 of f0 (Hz, baseband), in dB."""
    spec = np.abs(np.fft.fftshift(np.fft.fft(z[: 1 << 20]))) ** 2
    freqs = np.fft.fftshift(np.fft.fftfreq(min(len(z), 1 << 20), 1 / FS))
    sel = np.abs(freqs - f0) < width / 2
    return 10 * np.log10(spec[sel].mean() + 1e-20)


def carrier_offset(z, f0):
    """Mean FM discriminator output after moving f0 to 0 Hz: how far (Hz)
    the carrier is from f0."""
    n = np.arange(len(z))
    y = z * np.exp(-2j * np.pi * f0 * n / FS)
    k = 14                                     # 2.394 MS/s → 171 kS/s
    y = y[: len(y) // k * k].reshape(-1, k).mean(axis=1)
    d = np.angle(y[1:] * np.conj(y[:-1]))
    return float(np.mean(d) * (FS / k) / (2 * np.pi))


def analyse(z, label):
    lo, hi = band_power(z, -OFFSET), band_power(z, +OFFSET)
    side = "BELOW centre (-250 kHz)" if lo > hi else "ABOVE centre (+250 kHz)"
    print(f"  {label}: power at -250 kHz {lo:6.1f} dB, at +250 kHz {hi:6.1f} dB"
          f" -> the station is {side} ({abs(lo - hi):.1f} dB difference)")
    return -OFFSET if lo > hi else OFFSET


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("frequency", help="a station you receive well, e.g. 103.5M")
    ap.add_argument("--gain", type=float, default=20.0)
    ap.add_argument("--index", type=int, default=0, help="rtl_sdr device index")
    args = ap.parse_args(argv)
    f = hz(args.frequency)
    print(f"Station {f / 1e6:.3f} MHz, gain {args.gain} dB", flush=True)
    results = {}
    for label, center in (("tuned 250 kHz ABOVE the station", f + OFFSET),
                          ("tuned 250 kHz BELOW the station", f - OFFSET)):
        print(f"{label} ({center / 1e6:.3f} MHz):", flush=True)
        z = capture(center, args.gain, args.index)
        if len(z) < FS // 2:
            print("  rtl_sdr gave too little data: is the satellite stopped?")
            return 1
        found = analyse(z, label)
        expected = f - center                   # where it is if I/Q is as assumed
        off = carrier_offset(z, found)
        results[label] = (found == expected, off, center)
        print(f"  carrier {off:+.0f} Hz from where it should be", flush=True)
    normal = all(ok for ok, _, _ in results.values())
    mirrored = not any(ok for ok, _, _ in results.values())
    print()
    if normal:
        print("RESULT: normal I/Q - the station is at (station - centre), as the channelizer assumes")
    elif mirrored:
        print("RESULT: MIRRORED I/Q - the station is at (centre - station)")
    else:
        print("RESULT: unclear (the two captures disagree) - try a stronger station or another gain")
    ppms = [-off / f * 1e6 for _, off, _ in results.values()]
    print(f"ppm estimate: {np.median(ppms):+.1f} (from {', '.join(f'{p:+.1f}' for p in ppms)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
