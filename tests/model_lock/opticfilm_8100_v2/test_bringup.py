# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen 8100 V2 preview_safe / full-window geometry — do not retarget to match new code."""

from __future__ import annotations

import pytest

from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
from pyopticfilm.scan.bringup import (
    bringup_scan_geometry,
    crop_scan_geometry,
    preview_safe_scan_area,
)
from pyopticfilm.scan.geometry import compute_geometry


def test_preview_safe_area_1200_uses_window_top_feed2():
    area, meta = preview_safe_scan_area(MODEL_8100_V2, 1200, y1=0.0)
    assert area[0] == 0.0 and area[2] == 1.0
    assert meta["feed2"] == MODEL_8100_V2.feed_to_scan_top_steps == 13128
    assert meta["target_lincnt"] == 4836
    assert meta["max_lincnt"] == 4836


def test_bringup_preview_safe_passes_motor_gate_at_1200():
    geometry, meta = bringup_scan_geometry(MODEL_8100_V2, 1200, profile="preview_safe")
    feed2 = int(meta["feed2"])
    assert feed2 == 13128
    assert geometry.lincnt_register == 4836
    max_lc = MODEL_8100_V2.max_lincnt_for(feed2, 1200)
    assert geometry.lincnt_register <= max_lc


def test_default_full_frame_feed2_is_window_top_and_fits_at_1200():
    """V2 full-frame is feed2=13128 (TA top), unlike SE's 13704 which trips the gate."""
    geometry = compute_geometry(1200, model=MODEL_8100_V2, area=None)
    feed2 = MODEL_8100_V2.feed_to_scan_steps_for_area(None)
    assert feed2 == 13128
    max_lc = MODEL_8100_V2.max_lincnt_for(feed2, 1200)
    assert geometry.lincnt_register <= max_lc


def test_full_window_1800_stays_2592_even():
    geometry, _ = bringup_scan_geometry(MODEL_8100_V2, 1800, profile="preview_safe")
    assert geometry.pixels == 2592
    assert geometry.pixels % 2 == 0
    assert geometry.pixel_endx - geometry.pixel_startx == geometry.optical_pixels
    assert geometry.pixel_startx == 242
    assert geometry.pixel_endx == 10610
    assert geometry.usb_end_drop == 0


def test_crop_widths_at_1800_are_even():
    for i in range(80):
        x1 = (i % 40) / 100
        x2 = min(1.0, x1 + 0.25 + (i % 20) / 100)
        g = compute_geometry(1800, model=MODEL_8100_V2, area=(x1, 0.1, x2, 0.75))
        assert g.pixels % 2 == 0, g.pixels
        assert g.pixel_endx - g.pixel_startx == g.optical_pixels
        assert g.optical_pixels % 4 == 0


def test_crop_scan_geometry_clamps_lincnt():
    geometry, meta = crop_scan_geometry(MODEL_8100_V2, 1800, (0.1, 0.0, 0.9, 1.0))
    assert meta["profile"] == "crop"
    assert geometry.pixels % 2 == 0
    assert geometry.lincnt_register <= int(meta["max_lincnt"])
    assert geometry.lincnt_register == int(meta["target_lincnt"])
    assert "effective_area" in meta
    assert "requested_area" in meta


def test_lperiod_7200_is_v2_capture_value():
    assert MODEL_8100_V2.line_period_for(7200) == 16035
    dummy, clk_a, clk_b = MODEL_8100_V2.shading_strip_clocks(7200, dvdset=True)
    assert dummy == 0x10
    assert clk_a == 0x01
    assert clk_b == 0x01


def test_effective_scan_area_narrows_x_when_aligned():
    from pyopticfilm.scan.bringup import effective_scan_area

    requested = (0.1, 0.2, 0.9, 0.8)
    geometry = compute_geometry(2400, model=MODEL_8100_V2, area=requested)
    effective = effective_scan_area(MODEL_8100_V2, geometry, requested)
    assert effective[0] == pytest.approx(0.1)
    assert effective[1] == pytest.approx(0.2)
    assert effective[2] <= requested[2] + 1e-9
    # Host Y-unstagger spare at 2400 can nudge y2 a fraction above the request.
    assert effective[3] == pytest.approx(requested[3], abs=1e-3)
