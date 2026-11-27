"""tools/iq_side.py: its 'normal' result means the channelizer's convention
(station at station - centre), and its ppm has the gain tuner's sign."""

import sys

import numpy as np
import pytest

sys.path.insert(0, "tools")
import iq_side  # noqa: E402

import gain_tuner as gt  # noqa: E402
from channelizer import demod_all  # noqa: E402
from tests.test_gain_tuner import fm_station  # noqa: E402

F = 103_500_000


def dongle(true_ppm, mirror=False, p_sign=1):
    """A fake rtl_sdr: the station where a dongle off by true_ppm puts it,
    less the correction it was given with -p."""
    def capture(center, gain, index, seconds=1.0, ppm=0):
        off = (F - center) - (true_ppm - p_sign * ppm) * F / 1e6
        z = fm_station(int(iq_side.FS * 0.5), off)
        return np.conj(z) if mirror else z
    return capture


@pytest.mark.parametrize("true_ppm", [-30, 12])
def test_normal_result_and_ppm_match_the_satellite(monkeypatch, capsys, true_ppm):
    monkeypatch.setattr(iq_side, "capture", dongle(true_ppm))
    monkeypatch.setitem(sys.modules, "dongles", None)     # no rtl_test here
    assert iq_side.main(["103.5M", "--index", "0"]) == 0
    out = capsys.readouterr().out
    assert "RESULT: normal I/Q" in out
    assert "the sign is right" in out
    est = float(out.split("ppm estimate: ")[1].split()[0])
    assert est == pytest.approx(true_ppm, abs=1.5)
    # The same signal, as the satellite demodulates it
    center = F + iq_side.OFFSET
    z = dongle(true_ppm)(center, 20, 0)
    mpx = demod_all(z, F - center)
    assert gt.ppm_from_mpx(mpx, F) == pytest.approx(true_ppm, abs=2)


def test_mirrored_iq_is_reported(monkeypatch, capsys):
    monkeypatch.setattr(iq_side, "capture", dongle(12, mirror=True))
    monkeypatch.setitem(sys.modules, "dongles", None)
    iq_side.main(["103.5M"])
    assert "MIRRORED" in capsys.readouterr().out


def test_a_correction_that_doubles_the_offset_is_reported(monkeypatch, capsys):
    monkeypatch.setattr(iq_side, "capture", dongle(20, p_sign=-1))
    monkeypatch.setitem(sys.modules, "dongles", None)
    iq_side.main(["103.5M"])
    assert "the sign is WRONG" in capsys.readouterr().out
