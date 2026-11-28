"""A synthetic FM broadcast with RDS, for end-to-end tests without a radio.

RdsEncoder turns RDS groups (PI, PS, PTY, TP/TA, RadioText) into the
1187.5 bit/s biphase signal on 57 kHz; Station adds audio and the 19 kHz
pilot and FM-modulates the multiplex at an offset in a complex baseband,
so several stations can share one I/Q stream as from an Airspy.

Everything is computed in blocks and keeps its phase between blocks, so a
stream can run for hours. State (TA, PI, PTY, RadioText) can be changed
between blocks; it takes effect from the next group.
"""

import numpy as np

# --- RDS group coding (IEC 62106) -------------------------------------------

_POLY = 0x5B9                     # x^10+x^8+x^7+x^5+x^4+x^3+1
OFFSETS = {"A": 0x0FC, "B": 0x198, "C": 0x168, "Cp": 0x350, "D": 0x1B4}
BITRATE = 1187.5

PTY_CODES = {"None": 0, "News": 1, "Pop music": 10, "Phone-in": 21,
             "Alarm test": 30, "Alarm": 31}


# The RDS character set (EN 50067 annex E) differs from Latin-1 above 0x7F
_RDS_CHARS = {"á": 0x80, "à": 0x81, "é": 0x82, "è": 0x83, "ä": 0x91, "ö": 0x97,
              "ü": 0x99, "Ä": 0xD1, "Ö": 0xD7, "Ü": 0xD9, "Å": 0xE1, "å": 0xF1}


def rds_char(c):
    return _RDS_CHARS.get(c, ord(c) if ord(c) < 0x80 else ord("?"))


def checkword(data16):
    reg = data16 << 10
    for i in range(25, 9, -1):
        if reg & (1 << i):
            reg ^= _POLY << (i - 10)
    return reg & 0x3FF


def block_bits(data16, offset):
    word = (data16 << 10) | (checkword(data16) ^ OFFSETS[offset])
    return [(word >> (25 - i)) & 1 for i in range(26)]


class RdsState:
    def __init__(self, pi=0xE224, ps="P4 SR", pty="Phone-in", tp=True, ta=False,
                 rt="", corrupt_next_ta=0):
        self.pi, self.ps, self.pty, self.tp, self.ta, self.rt = pi, ps, pty, tp, ta, rt
        self.corrupt_next_ta = corrupt_next_ta   # this many 0A groups with TA flipped
        self.silent = False                      # no RDS (and no carrier) at all


class RdsEncoder:
    """Endless bit stream of groups 0A (PS, TA) and 2A (RadioText), in the
    ratio a P4 transmitter roughly sends them."""

    def __init__(self, state):
        self.state = state
        self._ps_seg = 0
        self._rt_seg = 0
        self._rt_ab = 0
        self._last_rt = None
        self._n = 0
        self._bits = []

    def _group_0a(self):
        s = self.state
        ta = s.ta
        if s.corrupt_next_ta > 0:
            s.corrupt_next_ta -= 1
            ta = not ta
        seg = self._ps_seg
        self._ps_seg = (seg + 1) % 4
        ps = (s.ps + " " * 8)[:8]
        b = (0 << 12) | (0 << 11) | (int(s.tp) << 10) | (PTY_CODES.get(s.pty, 0) << 5) \
            | (int(ta) << 4) | (1 << 3) | ((1 if seg == 3 else 0) << 2) | seg  # M/S=1, DI
        c = (224 << 8) | 205      # AF: none follow, filler
        d = (rds_char(ps[seg * 2]) << 8) | rds_char(ps[seg * 2 + 1])
        return [(s.pi, "A"), (b, "B"), (c, "C"), (d, "D")]

    def _group_2a(self):
        s = self.state
        if s.rt != self._last_rt:
            self._last_rt = s.rt
            self._rt_ab ^= 1
            self._rt_seg = 0
        text = (s.rt + "\r").ljust(64)[:64] if len(s.rt) < 64 else s.rt[:64]
        nseg = min(16, (len(s.rt) + 1 + 3) // 4) or 1
        seg = self._rt_seg
        self._rt_seg = (seg + 1) % nseg
        b = (2 << 12) | (0 << 11) | (int(s.tp) << 10) | (PTY_CODES.get(s.pty, 0) << 5) \
            | (self._rt_ab << 4) | seg
        ch = text[seg * 4: seg * 4 + 4].ljust(4)
        c = (rds_char(ch[0]) << 8) | rds_char(ch[1])
        d = (rds_char(ch[2]) << 8) | rds_char(ch[3])
        return [(s.pi, "A"), (b, "B"), (c, "C"), (d, "D")]

    def next_group_bits(self):
        self._n += 1
        g = self._group_2a() if (self.state.rt and self._n % 3 == 0) else self._group_0a()
        bits = []
        for data, off in g:
            bits += block_bits(data & 0xFFFF, off)
        return bits

    def bits(self, n):
        while len(self._bits) < n:
            self._bits += self.next_group_bits()
        out, self._bits = self._bits[:n], self._bits[n:]
        return out


# --- modulation ---------------------------------------------------------------

class Station:
    """One FM station at an offset (Hz) from the stream's centre."""

    def __init__(self, offset_hz, fs, state, audio_fn=None, level=0.3):
        self.offset = offset_hz
        self.fs = fs
        self.rds = RdsEncoder(state)
        self.state = state
        self.audio_fn = audio_fn          # (n, fs, t0) -> float array in [-1, 1]
        self.level = level
        self._t = 0                       # samples so far
        self._fm_phase = 0.0
        self._spb = fs / BITRATE          # samples per bit
        self._bit_pos = 0.0               # samples into the first queued bit
        self._levels = np.empty(0)        # differentially coded bits, queued
        self._diff = 0

    def _fill(self, k):
        while len(self._levels) < k:
            lv = []
            for b in self.rds.next_group_bits():
                self._diff ^= b
                lv.append(1.0 if self._diff else -1.0)
            self._levels = np.concatenate([self._levels, lv])

    def _rds_baseband(self, n):
        """Differentially coded biphase symbols, n samples."""
        pos = self._bit_pos + np.arange(n)
        idx = (pos // self._spb).astype(int)
        self._fill(idx[-1] + 1)
        frac = (pos % self._spb) / self._spb
        sym = self._levels[idx] * np.where(frac < 0.5, 1.0, -1.0)
        used = int((self._bit_pos + n) // self._spb)
        self._levels = self._levels[used:]
        self._bit_pos = self._bit_pos + n - used * self._spb
        return sym

    def mpx(self, n):
        """The station's multiplex (audio, pilot, RDS), n samples at fs."""
        t = (self._t + np.arange(n)) / self.fs
        audio = self.audio_fn(n, self.fs, self._t) if self.audio_fn else np.zeros(n)
        pilot = np.sin(2 * np.pi * 19000 * t)
        sym = self._rds_baseband(n)
        k = max(1, int(self._spb / 8))
        sym = np.convolve(sym, np.ones(k) / k, mode="same")
        rds = sym * np.sin(2 * np.pi * 57000 * t)
        self._t += n
        return 0.8 * audio + 0.09 * pilot + 0.05 * rds

    def block(self, n):
        if self.state.silent:
            self._t += n
            return np.zeros(n, dtype=np.complex64)
        mpx = self.mpx(n)
        inst = 2 * np.pi * (self.offset + 75000.0 * mpx) / self.fs
        ph = self._fm_phase + np.cumsum(inst)
        self._fm_phase = float(ph[-1] % (2 * np.pi))
        return (self.level * np.exp(1j * ph)).astype(np.complex64)


def tone_audio(freq=440.0, amp=0.3):
    def fn(n, fs, t0):
        t = (t0 + np.arange(n)) / fs
        return amp * np.sin(2 * np.pi * freq * t)
    return fn
