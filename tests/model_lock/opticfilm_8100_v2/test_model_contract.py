# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen 8100 V2 model flags — do not retarget to match new code."""

from __future__ import annotations

from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
from pyopticfilm.scan.bringup import bringup_scan_geometry


def test_v2_identity_and_scan_flags():
    assert MODEL_8100_V2.asic == "GL128"
    assert MODEL_8100_V2.usb_vendor_id == 0x07B3
    assert MODEL_8100_V2.usb_product_id == 0x1824
    assert MODEL_8100_V2.scan_ready is True
    assert MODEL_8100_V2.supports_infrared is False
    assert MODEL_8100_V2.mirror_x is True
    assert MODEL_8100_V2.default_gl128_prime is False
    assert MODEL_8100_V2.strpixel_native_units is True
    assert MODEL_8100_V2.optical_end_inactive_native == 96
    assert MODEL_8100_V2.feed_to_scan_steps == 13128
    assert MODEL_8100_V2.ladder_feed2_steps == 13128
    assert MODEL_8100_V2.lperiod_by_dpi[7200] == 16035


def test_v2_ta_window_and_full_frame_is_window_top():
    assert MODEL_8100_V2.x_offset_ta_mm == 0.43
    assert MODEL_8100_V2.x_size_ta_mm == 36.58
    assert MODEL_8100_V2.feed_to_scan_top_steps == 13128
    assert MODEL_8100_V2.feed_to_scan_steps == MODEL_8100_V2.feed_to_scan_top_steps


def test_full_window_str_is_338_at_1200_and_1800():
    g1200, _ = bringup_scan_geometry(MODEL_8100_V2, 1200, profile="preview_safe")
    g1800, _ = bringup_scan_geometry(MODEL_8100_V2, 1800, profile="preview_safe")
    assert g1200.pixel_startx == 338
    assert g1800.pixel_startx == 338
    assert g1200.pixel_startx == g1800.pixel_startx
