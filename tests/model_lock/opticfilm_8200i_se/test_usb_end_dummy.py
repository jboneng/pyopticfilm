# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen 8200i SE USB ENDPIXEL handling — do not retarget to match new code.

SilverFast's USB lines carry no ENDPIXEL dummy (sessions 04/13) and live scans
since the GL128 register fixes show the holder edge there, so the model trims
nothing (#102). The content-aware trim itself stays covered via an explicit cap.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.scan.pipeline import (
    ImagePipeline,
    count_usb_end_dummy_columns,
    trim_to_optical_span,
)

AREA = (0.15, 0.2, 0.7, 0.6)


def _capped_geometry(cap: int = 24):
    return replace(compute_geometry(1800, model=MODEL_8200I_SE, area=AREA), usb_end_drop=cap)


def test_trim_to_optical_span_drops_usb_wider_than_pixels():
    """Pcap 7200 URB dummy past STR/END — not live-path heuristic trim."""
    geo = compute_geometry(1800, model=MODEL_8200I_SE, area=(0.2, 0.2, 0.6, 0.5))
    wide = np.zeros((8, geo.pixels + 5, 3), dtype=np.uint16)
    trimmed = trim_to_optical_span(wide, geo)
    assert trimmed.shape[1] == geo.pixels


def _assemble_passthrough(monkeypatch, geo, rgb):
    pipe = ImagePipeline(MODEL_8200I_SE)
    monkeypatch.setattr(pipe, "decode_rgb", lambda *_a, **_k: rgb.copy())
    monkeypatch.setattr(pipe, "reduce_y_oversample", lambda arr, _g: arr)
    monkeypatch.setattr(pipe, "apply_line_shifts", lambda arr, _g: arr)
    monkeypatch.setattr(pipe, "apply_y_stagger", lambda arr, _g: arr)
    monkeypatch.setattr(pipe, "apply_host_downsample", lambda arr, _g: arr)
    return pipe.assemble(b"", geo, dark=None, white=None, expose_base=False)


def test_model_geometry_has_no_usb_end_drop():
    for dpi in (1200, 1800, 3600, 7200):
        assert compute_geometry(dpi, model=MODEL_8200I_SE, area=AREA).usb_end_drop == 0


def test_assemble_keeps_dark_holder_columns(monkeypatch):
    """Dark END columns are the holder edge, not dummy: all columns are kept."""
    geo = compute_geometry(1800, model=MODEL_8200I_SE, area=AREA)
    h = max(4, geo.lines)
    rgb = np.full((h, geo.pixels, 3), 18_000, dtype=np.uint16)
    rgb[:, -24:, :] = 1_400
    out = _assemble_passthrough(monkeypatch, geo, rgb)
    assert out.shape[1] == geo.pixels


def test_count_usb_end_dummy_drops_true_dummy_not_cap():
    rgb = np.full((8, 80, 3), 18_000, dtype=np.uint16)
    rgb[:, -8:, :] = 200
    assert count_usb_end_dummy_columns(rgb, max_drop=24) == 8


def test_count_usb_end_dummy_zero_when_no_dummy():
    rgb = np.full((8, 80, 3), 18_000, dtype=np.uint16)
    assert count_usb_end_dummy_columns(rgb, max_drop=24) == 0


def test_assemble_capped_drop_removes_full_dummy(monkeypatch):
    geo = _capped_geometry()
    h = max(4, geo.lines)
    rgb = np.full((h, geo.pixels, 3), 18_000, dtype=np.uint16)
    rgb[:, -24:, :] = 200
    out = _assemble_passthrough(monkeypatch, geo, rgb)
    assert out.shape[1] == geo.pixels - 24
    assert int(out[:, 0, 0].mean()) > 10_000


def test_assemble_capped_drop_removes_partial_dummy(monkeypatch):
    """8 true dummy columns drop 8, not the 24-column cap."""
    geo = _capped_geometry()
    h = max(4, geo.lines)
    rgb = np.full((h, geo.pixels, 3), 18_000, dtype=np.uint16)
    rgb[:, -8:, :] = 200
    out = _assemble_passthrough(monkeypatch, geo, rgb)
    assert out.shape[1] == geo.pixels - 8
    assert int(out[:, 0, 0].mean()) > 10_000


def test_assemble_capped_drop_keeps_frame_when_no_dummy(monkeypatch):
    geo = _capped_geometry()
    h = max(4, geo.lines)
    rgb = np.full((h, geo.pixels, 3), 18_000, dtype=np.uint16)
    out = _assemble_passthrough(monkeypatch, geo, rgb)
    assert out.shape[1] == geo.pixels
    assert int(out[:, 0, 0].mean()) > 10_000


def test_locked_usb_end_drop_equalizes_me_short_long_widths(monkeypatch):
    """Short discovers dark END columns; long without them must reuse that drop."""
    from pyopticfilm.scan.exposure_merge import merge_exposures_result

    geo = _capped_geometry()
    h = max(4, geo.lines)
    w = geo.pixels

    short_raw = np.full((h, w, 3), 18_000, dtype=np.uint16)
    short_raw[:, -16:, :] = 200  # 16 true dummy columns
    long_raw = np.full((h, w, 3), 40_000, dtype=np.uint16)
    # Long edge is bright — heuristic alone would drop 0.

    pipe = ImagePipeline(MODEL_8200I_SE)
    monkeypatch.setattr(pipe, "reduce_y_oversample", lambda arr, _g: arr)
    monkeypatch.setattr(pipe, "apply_line_shifts", lambda arr, _g: arr)
    monkeypatch.setattr(pipe, "apply_y_stagger", lambda arr, _g: arr)
    monkeypatch.setattr(pipe, "apply_host_downsample", lambda arr, _g: arr)

    monkeypatch.setattr(pipe, "decode_rgb", lambda *_a, **_k: short_raw.copy())
    out_short_heuristic = pipe.assemble(b"", geo, dark=None, white=None, expose_base=False)
    monkeypatch.setattr(pipe, "decode_rgb", lambda *_a, **_k: long_raw.copy())
    out_long_heuristic = pipe.assemble(b"", geo, dark=None, white=None, expose_base=False)
    assert out_short_heuristic.shape[1] != out_long_heuristic.shape[1]

    monkeypatch.setattr(pipe, "decode_rgb", lambda *_a, **_k: short_raw.copy())
    out_short = pipe.assemble(b"", geo, dark=None, white=None, expose_base=False)
    locked = int(pipe.last_usb_end_drop)
    assert locked == 16

    monkeypatch.setattr(pipe, "decode_rgb", lambda *_a, **_k: long_raw.copy())
    out_long = pipe.assemble(
        b"", geo, dark=None, white=None, expose_base=False, usb_end_drop=locked
    )
    assert out_short.shape == out_long.shape
    assert pipe.last_usb_end_drop == locked

    merged = merge_exposures_result(
        out_short, out_long, exposure_short=14000, exposure_long=42000
    )
    assert merged.rgb.shape == out_short.shape
