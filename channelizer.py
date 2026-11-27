"""Channelizer — wideband IQ to N×PCM streams for multi-station RDS decoding.

Reads raw unsigned-8-bit IQ samples from rtl_sdr stdout at 2 394 000 S/s,
extracts each configured station to baseband, and writes 171 kHz signed-16-bit
mono PCM to one os.pipe() per station.

DSP chain per station (numpy only, no scipy):
  U8 IQ → complex64 → frequency shift → LPF (127-tap Blackman sinc)
        → decimate 14 → FM discriminator → s16le → os.pipe()

Key parameters:
  RTL sample rate: 2 394 000 S/s  (= 171 000 × 14, exact integer ratio)
  Block size:      16 380 complex samples per RTL IQ chunk (~6.8 ms latency)
  Decimation:      14  → output 171 000 Hz per station
  LPF cutoff:      80 kHz  (FM channel half-width is 100 kHz)
  Filter length:   127 taps (Blackman-windowed sinc)

An Airspy gives signed 16-bit IQ at 3 MS/s (Mini) or 2.5 MS/s (R2), which
171 kHz does not divide: it is decimated to 250 kHz (12 or 10), demodulated
there and the MPX resampled 171/250 (polyphase), scaled to the same
radians per 171 kHz sample as the RTL path. The RTL path has no resampler.

At 6 MS/s and more (Airspy Mini 6 MS/s, R2 10 MS/s: stations up to ~8 MHz
apart) shifting and filtering every station at the full rate would cost too
much. There one FFT per block is shared by all stations (overlap-save): each
takes the bins around its own frequency, applies the low-pass there and
returns to the time domain with a small inverse FFT, already at 250 kHz.
The bin's whole-block phase and the part of the offset finer than a bin are
corrected at 250 kHz, so blocks join without a click and the carrier
offset (ppm) reads as with the other paths.
"""

import logging
import os
import threading

import numpy as np

log = logging.getLogger("rds-guard")

_FS = 2_394_000           # RTL-SDR sample rate
_OUTPUT_RATE = 171_000    # redsea expected input rate
_DECIMATION = _FS // _OUTPUT_RATE   # = 14
# A multiple of the decimation (14), so every block keeps the same 14-sample
# spacing: with 16 384 every block began 4 samples off (a click per block)
_BLOCK = 16_380           # complex samples per IQ block
_NTAPS = 127              # FIR filter length
_LPF_CUTOFF = 80_000      # LPF cutoff in Hz


_MID_RATE = 250_000       # where a non-RTL rate is decimated to
_RS_TAPS = 32             # resampler taps per phase (at the 250 kHz rate)
_RS_CUTOFF = 80_000       # resampler low-pass (MPX ends at 60 kHz)
# From this rate on, the shared-FFT front end (_WideFront)
WIDE_FROM = 4_000_000
_WIDE_TRANSITION = 40_000  # filter taps: about 6 * fs / this (Blackman)


def plan(fs):
    """(decimation, resampler (up, down) or None) to reach 171 kHz."""
    if fs % _OUTPUT_RATE == 0:
        return fs // _OUTPUT_RATE, None
    if fs % _MID_RATE == 0 and fs >= 2 * _MID_RATE:
        from math import gcd
        g = gcd(_OUTPUT_RATE, _MID_RATE)
        return fs // _MID_RATE, (_OUTPUT_RATE // g, _MID_RATE // g)
    raise ValueError(f"sample rate {fs} cannot be brought to 171 kHz")


class Resampler:
    """Polyphase rational resampler for a real stream, up/down, with its
    state carried from block to block (no click at the joins)."""

    def __init__(self, up, down, fs_in, taps=_RS_TAPS, cutoff_hz=_RS_CUTOFF):
        self.up, self.down, self.k = up, down, taps
        n = up * taps
        fc = cutoff_hz / (fs_in * up)
        t = np.arange(n) - (n - 1) / 2
        h = np.sinc(2 * fc * t) * np.kaiser(n, 8.0)
        h *= up / h.sum()
        # bank[p, k] = h[p + k * up]: output with phase p uses x[i - k]
        self._bank = h.reshape(taps, up).T.astype(np.float32)
        self._hist = np.zeros(taps - 1, dtype=np.float32)
        self._u = 0               # next output, in 1/up input samples from block start
        self._k = np.arange(taps)

    def process(self, x):
        x = np.asarray(x, dtype=np.float32)
        n = len(x)
        ext = np.concatenate([self._hist, x])
        end = self.up * n
        u = np.arange(self._u, end, self.down, dtype=np.int64)
        i = u // self.up + (self.k - 1)       # index in ext of x[u // up]
        p = u % self.up
        y = np.einsum("ij,ij->i", ext[i[:, None] - self._k[None, :]], self._bank[p])
        self._u = int(u[-1] + self.down - end) if len(u) else self._u - end
        self._hist = ext[-(self.k - 1):]
        return y.astype(np.float32)


def _make_lpf(cutoff_hz: float, fs: float, ntaps: int = _NTAPS) -> np.ndarray:
    """Blackman-windowed sinc low-pass filter (real, symmetric)."""
    fc = cutoff_hz / fs
    n = np.arange(ntaps) - (ntaps - 1) / 2
    h = np.sinc(2 * fc * n) * np.blackman(ntaps)
    return (h / h.sum()).astype(np.float32)


class _StationDSP:
    """Per-station DSP state: phase accumulator, filter overlap, FM prev sample."""

    def __init__(self, delta_f: float, fs: float = _FS, pipe: bool = True):
        self._decim, rs = plan(int(fs))
        # A resampled MPX is in radians per 250 kHz sample: scaled to per
        # 171 kHz sample, as the RTL path (redsea, recording, ppm alike)
        self._rs = Resampler(*rs, fs_in=fs / self._decim) if rs else None
        self._rs_scale = (fs / self._decim) / _OUTPUT_RATE
        # Frequency shift down by delta_f (the station's offset from the
        # centre): multiply by exp(-j 2 pi delta_f t). (Before v1.0 the sign
        # was +, which picked the mirror frequency centre - delta_f.)
        self._phase = 0.0
        self._phase_inc = -2.0 * np.pi * delta_f / fs

        # FIR filter: precompute FFT of zero-padded impulse response
        lpf = _make_lpf(_LPF_CUTOFF, fs)
        # FFT size: next power of 2 >= (_BLOCK + _NTAPS - 1)
        fft_size = 1
        while fft_size < _BLOCK + _NTAPS - 1:
            fft_size <<= 1
        self._fft_size = fft_size
        h_pad = np.zeros(fft_size, dtype=np.complex64)
        h_pad[:_NTAPS] = lpf
        self._H = np.fft.fft(h_pad)   # frequency-domain filter response

        # Overlap buffer (last _NTAPS-1 samples of the previous input block)
        self._overlap = np.zeros(_NTAPS - 1, dtype=np.complex64)

        # FM discriminator: previous complex sample after decimation
        self._prev_z = np.complex64(0)

        # Output pipe (none when used offline, e.g. by the gain calibration)
        self._open_pipe(pipe)

    def _open_pipe(self, pipe):
        self._dead = False
        if pipe:
            r, w = os.pipe()
            self.pipe_r = r
            self._pipe_w = os.fdopen(w, "wb")
        else:
            self.pipe_r = None
            self._pipe_w = None

    def process(self, z: np.ndarray) -> None:
        """Run the full DSP chain on one IQ block and write PCM to the pipe."""
        discriminated = self.demod(z)
        if discriminated is None:
            return
        # 5. Scale to s16le and write to output pipe
        #    FM deviation at 2394 kHz SR ≈ ±0.2 rad for ±75 kHz; scale by 1/π
        self._write(to_pcm(discriminated).tobytes())

    def demod(self, z: np.ndarray):
        """One IQ block -> the station's FM multiplex at 171 kHz (radians
        per sample), or None for an empty block."""
        n = len(z)

        # 1. Frequency shift to baseband
        angles = self._phase + self._phase_inc * np.arange(n, dtype=np.float64)
        phasors = np.exp(1j * angles).astype(np.complex64)
        zb = z * phasors
        self._phase = (self._phase + self._phase_inc * n) % (2.0 * np.pi)

        # 2. Overlap-save FFT-based FIR LPF
        #    Extend with previous overlap, zero-pad to fft_size, multiply spectra
        extended = np.concatenate([self._overlap, zb])
        x_pad = np.zeros(self._fft_size, dtype=np.complex64)
        x_pad[:len(extended)] = extended
        Y = np.fft.fft(x_pad) * self._H
        y = np.fft.ifft(Y).astype(np.complex64)
        #    Valid output for the current block starts at offset _NTAPS-1
        filtered = y[_NTAPS - 1 : _NTAPS - 1 + n]
        self._overlap = zb[-(_NTAPS - 1):]

        # 3. Decimate (by 14 for the RTL-SDR)
        decimated = filtered[::self._decim]
        if len(decimated) == 0:
            return None

        # 4. FM discriminator: instantaneous phase difference
        #    angle(z[n] * conj(z[n-1]))
        z_ext = np.concatenate([[self._prev_z], decimated])
        product = z_ext[1:] * np.conj(z_ext[:-1])
        discriminated = np.angle(product).astype(np.float32)
        self._prev_z = decimated[-1]
        if self._rs is not None:
            discriminated = self._rs.process(discriminated) * np.float32(self._rs_scale)
            if len(discriminated) == 0:
                return None
        return discriminated

    def _write(self, data: bytes) -> None:
        if self._dead or self._pipe_w is None:
            return
        try:
            self._pipe_w.write(data)
            self._pipe_w.flush()
        except (BrokenPipeError, OSError):
            self._dead = True
            log.warning("Channelizer: pipe closed for a station (redsea exited?)")

    def close(self) -> None:
        try:
            if self._pipe_w is not None:
                self._pipe_w.close()
        except Exception:
            pass


class _WideFront:
    """The shared part at a high rate: one overlap-save FFT per block."""

    def __init__(self, fs):
        self.fs = int(fs)
        self.decim, rs = plan(self.fs)
        self.resampler = rs
        ntaps = int(6 * self.fs / _WIDE_TRANSITION) | 1
        # FFT size: a multiple of the decimation, about 8 filter lengths
        size = self.decim
        while size < 8 * (ntaps - 1):
            size *= 2
        self.size = size
        self.bins = size // self.decim              # per station, at 250 kHz
        self.block = (size - (ntaps - 1)) // self.decim * self.decim
        self.overlap = size - self.block            # >= ntaps - 1, a multiple of decim
        h = np.zeros(size, dtype=np.complex64)
        h[:ntaps] = _make_lpf(_LPF_CUTOFF, self.fs, ntaps)
        self.H = np.fft.fft(h)
        self._hist = np.zeros(self.overlap, dtype=np.complex64)
        self._last = None
        self._X = None
        self.start = -self.overlap                  # absolute index of the FFT input's [0]

    def spectrum(self, z):
        """The FFT of this block with the end of the previous one (computed
        once per block, whatever the number of stations)."""
        if z is not self._last:
            if self._last is not None:
                self.start += len(self._last)
            ext = np.concatenate([self._hist, z])
            self._hist = ext[-self.overlap:]
            self._X = np.fft.fft(ext)
            self._last = z
        return self._X


class _WideStation(_StationDSP):
    """One station from a _WideFront's spectrum: its bins, the low-pass, a
    small inverse FFT to 250 kHz, then as _StationDSP (FM, 171 kHz)."""

    def __init__(self, front, delta_f, pipe=True):
        self._front = front
        fs, size, m = front.fs, front.size, front.bins
        self._k0 = int(round(delta_f * size / fs))
        self._resid = delta_f - self._k0 * fs / size      # finer than a bin
        offs = (np.fft.fftfreq(m) * m).astype(int)
        self._sel = (self._k0 + offs) % size
        self._Hsel = front.H[offs % size] * np.float32(m / size)
        self._first = front.overlap // front.decim        # first valid output
        self._decim = front.decim
        self._rs = Resampler(*front.resampler, fs_in=fs / front.decim) \
            if front.resampler else None
        self._rs_scale = (fs / front.decim) / _OUTPUT_RATE
        self._prev_z = np.complex64(0)
        self._open_pipe(pipe)

    def demod(self, z):
        front = self._front
        X = front.spectrum(z)
        y = np.fft.ifft(X[self._sel] * self._Hsel)[self._first:]
        # Output n is absolute input sample t = start + (first + n) * decim.
        # Taking the bins around k0 shifted each sample by k0 / size cycles
        # per sample of its index within this FFT (t - start), not of t: the
        # difference is k0 * start / size cycles, the same for the block. The
        # part of the offset finer than a bin is shifted here as well.
        t = front.start + (self._first + np.arange(len(y))) * self._decim
        block_cycles = (self._k0 * front.start) % front.size / front.size
        ph = -2.0 * np.pi * (block_cycles + np.mod(self._resid * t / front.fs, 1.0))
        decimated = (y * np.exp(1j * np.mod(ph, 2 * np.pi))).astype(np.complex64)
        if len(decimated) == 0:
            return None
        z_ext = np.concatenate([[self._prev_z], decimated])
        disc = np.angle(z_ext[1:] * np.conj(z_ext[:-1])).astype(np.float32)
        self._prev_z = decimated[-1]
        if self._rs is not None:
            disc = self._rs.process(disc) * np.float32(self._rs_scale)
            if len(disc) == 0:
                return None
        return disc


def make_stations(offsets, fs, pipe=True):
    """The per-station DSP for these offsets (Hz from the centre), and the
    block length (complex samples) they take."""
    if fs >= WIDE_FROM:
        front = _WideFront(fs)
        return [_WideStation(front, df, pipe) for df in offsets], front.block
    return [_StationDSP(df, fs, pipe) for df in offsets], _BLOCK


def to_pcm(discriminated):
    """MPX (radians per sample) -> s16 samples for redsea."""
    return (discriminated * (32767.0 / np.pi)).clip(-32768, 32767).astype(np.int16)


def iq_from_u8(raw):
    """rtl_sdr's unsigned 8-bit IQ bytes -> normalised complex64."""
    iq = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5) / 127.5
    return (iq[0::2] + 1j * iq[1::2]).astype(np.complex64)


def iq_from_s16(raw):
    """airspy_rx's signed 16-bit IQ bytes (-t 2) -> normalised complex64."""
    a = np.frombuffer(raw[:len(raw) // 4 * 4], dtype="<i2").astype(np.float32) / 32768.0
    return (a[0::2] + 1j * a[1::2]).astype(np.complex64)


_FORMATS = {"u8": (2, iq_from_u8), "s16": (4, iq_from_s16)}


def demod_all(z, delta_f, fs=_FS):
    """A whole IQ capture -> one station's MPX at 171 kHz (offline)."""
    return demod_many(z, [delta_f], fs)[0]


def demod_many(z, offsets, fs=_FS):
    """A whole IQ capture -> each station's MPX at 171 kHz (offline; at a
    high rate the FFT is shared, as in the Channelizer)."""
    stations, block = make_stations(offsets, fs, pipe=False)
    parts = [[] for _ in stations]
    for i in range(0, len(z) - block + 1, block):
        zb = z[i:i + block]
        for st, out in zip(stations, parts):
            m = st.demod(zb)
            if m is not None:
                out.append(m)
    return [np.concatenate(p) if p else np.zeros(0, dtype=np.float32) for p in parts]


class Channelizer(threading.Thread):
    """Daemon thread: reads rtl_sdr IQ, writes 171 kHz PCM to N os.pipe()s.

    Usage::

        ch = Channelizer(rtl_sdr_proc.stdout, [103_500_000, 102_900_000], 103_200_000)
        ch.start()
        # ch.pipe_read_fds[i] is the read end of station i's PCM pipe
    """

    def __init__(self, rtl_sdr_stdout, frequencies_hz: list, center_freq_hz: int,
                 fs: int = _FS, fmt: str = "u8", probe=None):
        super().__init__(daemon=True, name="channelizer")
        self._src = rtl_sdr_stdout
        self._probe = probe          # dongle_check.Probe: sees the first seconds
        self._bytes_per_sample, self._to_iq = _FORMATS[fmt]
        self._stations, self._block = make_stations(
            [freq_hz - center_freq_hz for freq_hz in frequencies_hz], fs)

    @property
    def pipe_read_fds(self) -> list:
        """Read-end file descriptors (one per station, in config order)."""
        return [st.pipe_r for st in self._stations]

    def run(self) -> None:
        log.info("Channelizer started (%d station(s))", len(self._stations))
        bytes_per_block = self._block * self._bytes_per_sample
        try:
            while True:
                raw = self._src.read(bytes_per_block)
                if not raw:
                    break  # rtl_sdr EOF — process exited
                if len(raw) < bytes_per_block:
                    continue  # short read — skip partial block

                z = self._to_iq(raw)
                if self._probe is not None:
                    try:
                        self._probe.feed(len(raw), z)
                    except Exception:
                        log.exception("Channelizer: the dongle check failed")
                        self._probe = None

                for st in self._stations:
                    st.process(z)

        except Exception:
            log.exception("Channelizer: unexpected error in IQ loop")
        finally:
            if self._probe is not None and not self._probe.done:
                try:
                    self._probe.finish()
                except Exception:
                    log.exception("Channelizer: the dongle check failed")
            for st in self._stations:
                st.close()
            log.info("Channelizer stopped")
