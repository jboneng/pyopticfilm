# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen 7600i v2 image and calibration-strip geometry — do not retarget to match new code."""

from __future__ import annotations

import pytest

from pyopticfilm.device.select import MODEL_7600I_V2
from pyopticfilm.scan.geometry import compute_calib_geometry, compute_geometry


@pytest.mark.parametrize(
    ("dpi", "pixels", "lines", "lincnt"),
    [
        (900, 1287, 885, 891),
        (1800, 2560, 1771, 1783),
        (3600, 5136, 3543, 3567),
        (7200, 10288, 7086, 7138),
    ],
)
def test_full_frame(dpi, pixels, lines, lincnt):
    # 900 / 3600 / 7200 match the hardware full-frame scans.
    g = compute_geometry(dpi, model=MODEL_7600I_V2)
    assert (g.pixels, g.lines) == (pixels, lines)
    assert g.lincnt_register == lincnt
    assert g.starty == 4039
    assert g.exposure_lperiod == 14000
    assert g.register_dpiset == dpi // 6


@pytest.mark.parametrize(
    ("dpi", "lines"),
    [(900, 70), (1800, 141), (3600, 283), (7200, 566)],
)
def test_calib_strip_at_open_light_path(dpi, lines):
    g = compute_calib_geometry(dpi, model=MODEL_7600I_V2)
    assert g.starty == 212  # 1.5 mm from home at 3600 steps/inch
    assert g.lines == lines
