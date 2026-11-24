# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen 8200i model flags and hardware-tested overrides — do not retarget to match new code."""

from __future__ import annotations

from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.device.select import MODEL_7600I_V2, model_for_device


def test_identity_and_scan_flags():
    assert MODEL_8200I.name == "plustek-opticfilm-8200i"
    assert MODEL_8200I.asic == "GL845"
    assert (MODEL_8200I.usb_vendor_id, MODEL_8200I.usb_product_id) == (0x07B3, 0x130D)
    assert MODEL_8200I.scan_ready is True
    assert MODEL_8200I.supports_infrared is True
    assert MODEL_8200I.supports_multi_exposure is True
    assert MODEL_8200I.resolutions_dpi == (7200, 3600, 1800, 900)
    assert MODEL_8200I.multi_exposure_resolutions_dpi == (7200, 3600, 1800, 900)
    assert MODEL_8200I.motor_base_ydpi == 3600
    assert (MODEL_8200I.x_size_ta_mm, MODEL_8200I.y_size_ta_mm) == (36.33, 25.0)


def test_usb_id_routes_to_the_8200i():
    # The hardware-tested unit reports bcdDevice 0x0605.
    assert model_for_device(0x130D, 0x0605) is MODEL_8200I
    assert model_for_device(0x0C3B, 0x0605) is MODEL_7600I_V2


def test_calibration_overrides():
    assert MODEL_8200I.calib_white_home_y_mm == 1.5
    assert MODEL_8200I.afe_white_target == 60000
    assert MODEL_8200I.calib_dark_lines == 1024


def test_motion_overrides():
    # Fast feed to the film, AGOHOME return on the image pass, reverse home
    # seek, and dark frames with motor power off: the combination that ran
    # cleanly on hardware at 900 / 1800 / 3600 / 7200 dpi.
    assert MODEL_8200I.fast_feed is True
    assert MODEL_8200I.fast_feed_offset_steps == 0
    assert MODEL_8200I.scan_auto_home is True
    assert MODEL_8200I.forward_home is False
    assert MODEL_8200I.stationary_motor_off is True
    assert MODEL_8200I.prefeed_margin_steps == 400
    assert MODEL_8200I.image_pass_lamp_watchdog_off is True
    assert MODEL_8200I.home_sensor_gpio_mask == 0x40


def test_exposure_overrides():
    assert MODEL_8200I.exposure_lperiod == 14000
    assert MODEL_8200I.auto_exposure_target is None
    assert MODEL_8200I.exposure_long == 42000
    assert MODEL_8200I.me_long_clamp_min == 28000
    assert MODEL_8200I.me_long_clamp_max == 42000
    assert MODEL_8200I.me_max_exposure_ratio == 3.0


def test_motor_profile():
    mp = MODEL_8200I.motor_profile
    assert len(mp.fast_table) == 256
    assert (mp.fast_table[0], mp.fast_table[-1]) == (64102, 625)
    assert mp.reg_overrides == ((0x5F, 0x01), (0x67, 0x80), (0x68, 0x80), (0x6A, 0xFF))
    assert mp.feed_regs == ((0x21, 0x02), (0x22, 0x01), (0x23, 0x01), (0x24, 0x02), (0x69, 0x01))
    assert len(mp.feed_start_table) == 256
    assert mp.feed_start_table[:2] == (25252, 14000)
