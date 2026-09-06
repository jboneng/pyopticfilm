# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen 8200i SE model flags and full-window STR — do not retarget to match new code."""

from __future__ import annotations

from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.scan.bringup import bringup_scan_geometry


def test_se_identity_and_scan_flags():
    assert MODEL_8200I_SE.asic == "GL128"
    assert MODEL_8200I_SE.usb_vendor_id == 0x07B3
    assert MODEL_8200I_SE.usb_product_id == 0x1825
    assert MODEL_8200I_SE.scan_ready is True
    assert MODEL_8200I_SE.supports_infrared is True
    assert MODEL_8200I_SE.mirror_x is True
    assert MODEL_8200I_SE.default_gl128_prime is False
    assert MODEL_8200I_SE.strpixel_native_units is True
    assert MODEL_8200I_SE.optical_end_inactive_native == 96


def test_se_ta_window_and_preview_feed2():
    assert MODEL_8200I_SE.x_offset_ta_mm == 0.43
    assert MODEL_8200I_SE.x_size_ta_mm == 36.58
    assert MODEL_8200I_SE.feed_to_scan_top_steps == 13128


def test_full_window_str_is_338_at_1200_and_1800():
    g1200, _ = bringup_scan_geometry(MODEL_8200I_SE, 1200, profile="preview_safe")
    g1800, _ = bringup_scan_geometry(MODEL_8200I_SE, 1800, profile="preview_safe")
    assert g1200.pixel_startx == 338
    assert g1800.pixel_startx == 338
    assert g1200.pixel_startx == g1800.pixel_startx
