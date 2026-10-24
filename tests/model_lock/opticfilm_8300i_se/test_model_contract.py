# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen 8300i SE model flags — do not retarget to match new code."""

from __future__ import annotations

from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE
from pyopticfilm.scan.bringup import bringup_scan_geometry


def test_8300i_identity_and_scan_flags():
    assert MODEL_8300I_SE.asic == "GL128"
    assert MODEL_8300I_SE.usb_vendor_id == 0x07B3
    assert MODEL_8300I_SE.usb_product_id == 0x181F
    assert MODEL_8300I_SE.scan_ready is True
    assert MODEL_8300I_SE.supports_infrared is True
    assert MODEL_8300I_SE.mirror_x is True
    assert MODEL_8300I_SE.strpixel_native_units is True
    assert MODEL_8300I_SE.optical_end_inactive_native == 0
    assert MODEL_8300I_SE.feed_to_scan_steps == 13128
    assert MODEL_8300I_SE.ladder_feed2_steps == 13128
    assert MODEL_8300I_SE.exposure_short == 15000
    assert MODEL_8300I_SE.exposure_lperiod == 15000
    assert MODEL_8300I_SE.lperiod_by_dpi[7200] == 16035
    assert MODEL_8300I_SE.dummy_by_dpi[1800] == 0x08
    assert MODEL_8300I_SE.pixel_clock_by_dpi[1800] == 0x08
    assert MODEL_8300I_SE.stagger_y_for(1440) == (1, 0)
    assert MODEL_8300I_SE.stagger_y_for(2400) == (1, 0)
    assert MODEL_8300I_SE.stagger_y_for(1800) == ()


def test_8300i_ta_window_and_full_frame_fallback():
    assert MODEL_8300I_SE.x_offset_ta_mm == 0.43
    assert MODEL_8300I_SE.x_size_ta_mm == 36.58
    assert MODEL_8300I_SE.feed_to_scan_top_steps == 13128
    assert MODEL_8300I_SE.feed_to_scan_steps == MODEL_8300I_SE.feed_to_scan_top_steps


def test_full_window_str_is_242_at_1200_and_1800():
    g1200, _ = bringup_scan_geometry(MODEL_8300I_SE, 1200, profile="preview_safe")
    g1800, _ = bringup_scan_geometry(MODEL_8300I_SE, 1800, profile="preview_safe")
    assert g1200.pixel_startx == 242
    assert g1800.pixel_startx == 242
    assert g1200.pixel_startx == g1800.pixel_startx


def test_colour_short_feed2_is_ppi_dependent():
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(600) == 13040
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(1200) == 13112
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(1800) == 13122
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(7200) == 13128
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(1200, long_exposure=True) == 13128
