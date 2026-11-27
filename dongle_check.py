"""A check of each receiver's first seconds: does the dongle really receive?

Opening the dongle and starting the capture only shows that it is there.
The channelizer hands the first seconds of IQ to judge(), which says
whether data comes at the rate it should, whether the samples move at all,
how strong the band is and which of the receiver's stations it hears:

  error    too little data, or samples that never change: the receiver
           does not run (it is retried like any other failure)
  warning  it receives, but no station is heard at the present gain (the
           calibration still tries every gain)
  ok       it hears at least one of its stations
"""

import numpy as np

PROBE_SEC = 4.0         # how long the first look lasts
SETTLE_SEC = 0.3        # the tuner's first samples are not representative
KEEP_SEC = 0.5          # IQ kept for the look at the stations
MIN_RATE = 0.5          # of the sample rate it should give: less is an error
LOW_RATE = 0.8          # less than this: a warning (the dongle, USB, or a busy CPU)
FLAT_STD = 1 / 256      # samples moving less than this (full scale = 1) do not change


def judge(iq, nbytes, elapsed, radio, freqs_hz, center_hz, gain=None):
    """(level, message, heard): level 'ok', 'warning' or 'error'."""
    from channelizer import demod_all
    import gain_tuner

    rate = nbytes / radio.bytes_per_sample / elapsed if elapsed > 0 else 0.0
    if rate < MIN_RATE * radio.fs:
        return ("error", f"too little data from the dongle ({rate / 1e6:.2f} of "
                         f"{radio.fs / 1e6:.2f} MS/s): USB, power, or a CPU too busy?", [])
    slow = (f" Only {rate / 1e6:.2f} of {radio.fs / 1e6:.2f} MS/s arrive (USB, or a busy "
            "CPU?)." if rate < LOW_RATE * radio.fs else "")
    if len(iq) == 0 or float(np.std(iq)) < FLAT_STD:
        return ("error", "the dongle's samples do not change: driver or tuner?", [])
    level = gain_tuner.level_dbfs(iq)
    heard, pilots = [], {}
    for hz in freqs_hz:
        mpx = demod_all(iq, hz - center_hz, radio.fs)
        pilot = gain_tuner.tone_snr_db(mpx)[0] if len(mpx) else 0.0
        name = f"{hz / 1e6:g}M"
        pilots[name] = pilot
        if pilot >= gain_tuner.HEARD_PILOT_DB:
            heard.append(name)
    at = f" at gain {gain:g}" if gain is not None else ""
    summary = (f"{radio.model}, {rate / 1e6:.2f} MS/s, level {level:.0f} dBFS, "
               + ", ".join(f"{n} pilot {p:.0f} dB" for n, p in pilots.items()))
    if not heard:
        return ("warning", f"no FM station heard on {', '.join(pilots)}{at} "
                           f"({summary}): the calibration tries every gain.{slow}", [])
    if slow:
        return ("warning", f"{summary}; heard {', '.join(heard)}.{slow}", heard)
    return ("ok", f"OK: {summary}; heard {', '.join(heard)}", heard)


class Probe:
    """Collects the first seconds for judge(); called by the channelizer
    with every block it reads."""

    def __init__(self, fs, on_done, clock=None):
        import time
        self._clock = clock or time.monotonic
        self.fs = fs
        self.on_done = on_done
        self.done = False
        self._t0 = None
        self._nbytes = 0
        self._kept = []
        self._kept_n = 0

    def feed(self, nbytes, z):
        if self.done:
            return
        now = self._clock()
        if self._t0 is None:
            self._t0 = now
        self._nbytes += nbytes
        elapsed = now - self._t0
        if elapsed >= SETTLE_SEC and self._kept_n < KEEP_SEC * self.fs:
            self._kept.append(np.array(z, copy=True))
            self._kept_n += len(z)
        if elapsed >= PROBE_SEC:
            self.finish(elapsed)

    def finish(self, elapsed=None):
        """Also called when the stream ends early."""
        if self.done:
            return
        self.done = True
        if elapsed is None:
            elapsed = (self._clock() - self._t0) if self._t0 is not None else 0.0
        iq = np.concatenate(self._kept) if self._kept else np.zeros(0, np.complex64)
        self.on_done(iq, self._nbytes, elapsed)
