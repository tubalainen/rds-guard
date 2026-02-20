"""Channelizer — wideband IQ to N×PCM streams for multi-station RDS decoding.

Reads raw unsigned-8-bit IQ samples from rtl_sdr stdout at 2 394 000 S/s,
extracts each configured station to baseband, and writes 171 kHz signed-16-bit
mono PCM to one os.pipe() per station.

DSP chain per station (numpy only, no scipy):
  U8 IQ → complex64 → frequency shift → LPF (127-tap Blackman sinc)
        → decimate 14 → FM discriminator → s16le → os.pipe()

Key parameters:
  RTL sample rate: 2 394 000 S/s  (= 171 000 × 14, exact integer ratio)
  Block size:      16 384 complex samples per RTL IQ chunk (~6.8 ms latency)
  Decimation:      14  → output 171 000 Hz per station
  LPF cutoff:      80 kHz  (FM channel half-width is 100 kHz)
  Filter length:   127 taps (Blackman-windowed sinc)
"""

import logging
import os
import threading

import numpy as np

log = logging.getLogger("rds-guard")

_FS = 2_394_000           # RTL-SDR sample rate
_OUTPUT_RATE = 171_000    # redsea expected input rate
_DECIMATION = _FS // _OUTPUT_RATE   # = 14
_BLOCK = 16_384           # complex samples per IQ block
_NTAPS = 127              # FIR filter length
_LPF_CUTOFF = 80_000      # LPF cutoff in Hz


def _make_lpf(cutoff_hz: float, fs: float, ntaps: int = _NTAPS) -> np.ndarray:
    """Blackman-windowed sinc low-pass filter (real, symmetric)."""
    fc = cutoff_hz / fs
    n = np.arange(ntaps) - (ntaps - 1) / 2
    h = np.sinc(2 * fc * n) * np.blackman(ntaps)
    return (h / h.sum()).astype(np.float32)


class _StationDSP:
    """Per-station DSP state: phase accumulator, filter overlap, FM prev sample."""

    def __init__(self, delta_f: float, fs: float = _FS):
        # Frequency shift: phasor accumulator
        self._phase = 0.0
        self._phase_inc = 2.0 * np.pi * delta_f / fs

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

        # Output pipe
        r, w = os.pipe()
        self.pipe_r = r
        self._pipe_w = os.fdopen(w, "wb")
        self._dead = False

    def process(self, z: np.ndarray) -> None:
        """Run the full DSP chain on one IQ block and write PCM to the pipe."""
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

        # 3. Decimate by 14
        decimated = filtered[::_DECIMATION]
        if len(decimated) == 0:
            return

        # 4. FM discriminator: instantaneous phase difference
        #    angle(z[n] * conj(z[n-1]))
        z_ext = np.concatenate([[self._prev_z], decimated])
        product = z_ext[1:] * np.conj(z_ext[:-1])
        discriminated = np.angle(product).astype(np.float32)
        self._prev_z = decimated[-1]

        # 5. Scale to s16le and write to output pipe
        #    FM deviation at 2394 kHz SR ≈ ±0.2 rad for ±75 kHz; scale by 1/π
        pcm = (discriminated * (32767.0 / np.pi)).clip(-32768, 32767).astype(np.int16)
        self._write(pcm.tobytes())

    def _write(self, data: bytes) -> None:
        if self._dead:
            return
        try:
            self._pipe_w.write(data)
            self._pipe_w.flush()
        except (BrokenPipeError, OSError):
            self._dead = True
            log.warning("Channelizer: pipe closed for a station (redsea exited?)")

    def close(self) -> None:
        try:
            self._pipe_w.close()
        except Exception:
            pass


class Channelizer(threading.Thread):
    """Daemon thread: reads rtl_sdr IQ, writes 171 kHz PCM to N os.pipe()s.

    Usage::

        ch = Channelizer(rtl_sdr_proc.stdout, [103_500_000, 102_900_000], 103_200_000)
        ch.start()
        # ch.pipe_read_fds[i] is the read end of station i's PCM pipe
    """

    def __init__(self, rtl_sdr_stdout, frequencies_hz: list, center_freq_hz: int):
        super().__init__(daemon=True, name="channelizer")
        self._src = rtl_sdr_stdout
        self._stations = [
            _StationDSP(freq_hz - center_freq_hz)
            for freq_hz in frequencies_hz
        ]

    @property
    def pipe_read_fds(self) -> list:
        """Read-end file descriptors (one per station, in config order)."""
        return [st.pipe_r for st in self._stations]

    def run(self) -> None:
        log.info("Channelizer started (%d station(s))", len(self._stations))
        bytes_per_block = _BLOCK * 2   # 2 bytes per sample (I byte + Q byte)
        try:
            while True:
                raw = self._src.read(bytes_per_block)
                if not raw:
                    break  # rtl_sdr EOF — process exited
                if len(raw) < bytes_per_block:
                    continue  # short read — skip partial block

                # Convert unsigned-8-bit IQ to normalised complex64
                iq = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
                iq = (iq - 127.5) / 127.5
                z = (iq[0::2] + 1j * iq[1::2]).astype(np.complex64)

                for st in self._stations:
                    st.process(z)

        except Exception:
            log.exception("Channelizer: unexpected error in IQ loop")
        finally:
            for st in self._stations:
                st.close()
            log.info("Channelizer stopped")
