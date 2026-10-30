# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen 7600i v2 model flags and hardware-tuned overrides — do not retarget to match new code."""

from __future__ import annotations

from pyopticfilm.device.model_7600i_v1 import MODEL_7600I_V1
from pyopticfilm.device.select import MODEL_7600I_V2, model_for_device


def test_identity_and_scan_flags():
    assert MODEL_7600I_V2.name == "plustek-opticfilm-7600i-v2"
    assert MODEL_7600I_V2.asic == "GL845"
    assert (MODEL_7600I_V2.usb_vendor_id, MODEL_7600I_V2.usb_product_id) == (0x07B3, 0x0C3B)
    assert MODEL_7600I_V2.scan_ready is True
    assert MODEL_7600I_V2.supports_infrared is True
    assert MODEL_7600I_V2.supports_multi_exposure is True
    assert MODEL_7600I_V2.resolutions_dpi == (7200, 3600, 1800, 900)
    assert MODEL_7600I_V2.multi_exposure_resolutions_dpi == (7200, 3600, 1800, 900)
    assert MODEL_7600I_V2.motor_base_ydpi == 3600
    assert (MODEL_7600I_V2.x_size_ta_mm, MODEL_7600I_V2.y_size_ta_mm) == (36.33, 25.0)


def test_bcd_device_routes_v1_and_v2():
    assert model_for_device(0x0C3B, 0x0605) is MODEL_7600I_V2
    assert model_for_device(0x0C3B, 0x0400) is MODEL_7600I_V1


def test_calibration_overrides():
    # White on the open light path 1.5 mm from home, not the holder edge.
    assert MODEL_7600I_V2.calib_white_home_y_mm == 1.5
    assert MODEL_7600I_V2.afe_white_target == 60000
    assert MODEL_7600I_V2.calib_dark_lines == 1024


def test_exposure_and_feed_overrides():
    assert MODEL_7600I_V2.exposure_lperiod == 14000
    assert MODEL_7600I_V2.auto_exposure_target == 48000
    assert MODEL_7600I_V2.exposure_long == 42000
    assert MODEL_7600I_V2.me_long_clamp_min == 28000
    assert MODEL_7600I_V2.me_long_clamp_max == 65000
    assert MODEL_7600I_V2.me_max_exposure_ratio == 4.6
    assert MODEL_7600I_V2.prefeed_margin_steps == 400
    assert MODEL_7600I_V2.prefeed_offset_steps == 6
