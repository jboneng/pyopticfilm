# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen 7600i v1 model flags and job geometry — do not retarget to match new code."""

from __future__ import annotations

import pytest

from pyopticfilm.device.model_7600i_v1 import CALIBRATED_SHIFTS, MODEL_7600I_V1
from pyopticfilm.scan.replay_gl843_v1 import prepare_profile


def test_identity_and_scan_flags():
    assert MODEL_7600I_V1.asic == "GL843"
    assert (MODEL_7600I_V1.usb_vendor_id, MODEL_7600I_V1.usb_product_id) == (0x07B3, 0x0C3B)
    assert MODEL_7600I_V1.scan_ready is True
    assert MODEL_7600I_V1.supports_infrared is True
    assert MODEL_7600I_V1.mirror_x is True
    assert MODEL_7600I_V1.resolutions_dpi == (7200, 3600, 1440)
    assert MODEL_7600I_V1.infrared_resolutions_dpi == (7200, 3600)
    assert MODEL_7600I_V1.dummy_lines == "none"
    assert MODEL_7600I_V1.single_sample is True
    assert MODEL_7600I_V1.window_start == 80
    assert MODEL_7600I_V1.stagger_y_by_dpi[7200] == (4, 0)


@pytest.mark.parametrize(
    ("mode", "dpi", "pixels", "lines", "yres", "stop_ms"),
    [
        ("color", 1440, 2050, 2824, 2880, 2571.9),
        ("color", 3600, 5124, 7058, 7200, 2559.7),
        ("color", 7200, 10248, 14122, 14400, 2571.8),
        ("infrared", 3600, 5124, 7058, 7200, 2571.8),
    ],
)
def test_job_frames_and_positioning_stop(mode, dpi, pixels, lines, yres, stop_ms):
    p = MODEL_7600I_V1.replay_profile(mode, dpi)
    main = p["frames"][p["mainFrame"]]
    assert p["mainFrame"] == 7
    assert (main["pixels"], main["lines"], main["bytes"]) == (pixels, lines, pixels * lines * 6)
    assert p["yres"] == yres
    assert next(o["ms"] for o in p["ops"] if o.get("timedStop")) == stop_ms


def test_calibrated_shifts():
    assert CALIBRATED_SHIFTS == {
        2880: (0.0, 9.84, 19.30),
        7200: (0.0, 24.22, 48.21),
        14400: (0.0, 48.44, 96.42),
    }


@pytest.mark.parametrize(
    ("dpi", "setting", "kwargs", "cruise"),
    [
        (3600, "recorded", {}, None),
        (3600, "none", {}, (14000, 7000)),
        (7200, "fewer", {}, (42000, 28000)),
        (7200, "none", {}, (42000, 14000)),
        (3600, "recorded", {"exposure_multiplier": 3}, (14000, 21000)),
    ],
)
def test_main_scan_motor_cruise(dpi, setting, kwargs, cruise):
    p = prepare_profile(MODEL_7600I_V1.replay_profile("color", dpi), dummy_lines=setting, **kwargs)
    got = p.get("motorCruise")
    assert (None if got is None else (got["recorded"], got["used"])) == cruise
