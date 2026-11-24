# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen 8200i image and calibration-strip geometry — do not retarget to match new code."""

from __future__ import annotations

import pytest

from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.scan.geometry import compute_calib_geometry, compute_geometry


@pytest.mark.parametrize(
    ("dpi", "pixels", "lines", "lincnt"),
    [
        (900, 1286, 885, 891),
        (1800, 2560, 1771, 1783),
        (3600, 5136, 3543, 3567),
        (7200, 10288, 7086, 7138),
    ],
)
def test_full_frame(dpi, pixels, lines, lincnt):
    # Pixels, lines and starty match the hardware full-frame scans (issue #181).
    g = compute_geometry(dpi, model=MODEL_8200I)
    assert (g.pixels, g.lines) == (pixels, lines)
    assert g.lincnt_register == lincnt
    assert g.starty == 4039
    assert g.exposure_lperiod == 14000
    assert g.register_dpiset == dpi // 6


@pytest.mark.parametrize(
    ("dpi", "lines"),
    [(900, 70), (1800, 141), (3600, 283), (7200, 566)],
)
def test_calib_strip_near_home(dpi, lines):
    g = compute_calib_geometry(dpi, model=MODEL_8200I)
    assert g.starty == 212  # 1.5 mm from home at 3600 steps/inch
    assert g.lines == lines
