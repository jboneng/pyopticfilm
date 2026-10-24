# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen 8300i SE STR/END window oracles — do not retarget to match new code."""

from __future__ import annotations

from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE
from pyopticfilm.scan.bringup import bringup_scan_geometry, crop_scan_geometry

# SilverFast full-frame STR/END at every captured PPI (docs §10.2). No ENDPIXEL
# dummy trim or frame shift: SilverFast's USB lines carry no dummy suffix (#102).
PREVIEW_STR = 242
PREVIEW_END = 10610


def test_no_endpixel_dummy_trim():
    assert MODEL_8300I_SE.optical_end_inactive_native == 0


def test_full_window_str_is_dpi_independent():
    starts = []
    for dpi in (1200, 1800, 3600, 7200):
        g, _ = bringup_scan_geometry(MODEL_8300I_SE, dpi, profile="preview_safe")
        starts.append(g.pixel_startx)
        assert g.pixels % 2 == 0
        assert g.pixel_endx - g.pixel_startx == g.optical_pixels
        assert g.line_bytes == g.pixels * 3 * (g.depth // 8)
        assert g.usb_end_drop == 0
    assert len(set(starts)) == 1
    assert starts[0] == PREVIEW_STR


def test_full_window_matches_sf_str_end_at_1800():
    g, _ = bringup_scan_geometry(MODEL_8300I_SE, 1800, profile="preview_safe")
    assert (g.pixel_startx, g.pixel_endx) == (PREVIEW_STR, PREVIEW_END)
    assert g.pixels == 2592


def test_same_crop_str_matches_at_1800_and_3600():
    area = (0.15, 0.1, 0.72, 0.8)
    g1800, _ = crop_scan_geometry(MODEL_8300I_SE, 1800, area)
    g3600, _ = crop_scan_geometry(MODEL_8300I_SE, 3600, area)
    assert g1800.pixel_startx == g3600.pixel_startx
    assert abs(g1800.pixel_endx - g3600.pixel_endx) < 8
    assert g1800.usb_end_drop == g3600.usb_end_drop == 0


def test_calib_geometry_does_not_drop_usb_end():
    from pyopticfilm.scan.geometry import compute_calib_geometry

    g = compute_calib_geometry(1800, model=MODEL_8300I_SE)
    assert g.disable_buffer_full_move
    assert g.usb_end_drop == 0
    assert g.pixel_startx == 120
