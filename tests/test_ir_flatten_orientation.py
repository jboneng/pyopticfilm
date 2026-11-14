# SPDX-License-Identifier: GPL-3.0-or-later
"""The IR white-strip profile (USB column order) must line up with the mirrored image."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.scan import calib_gl128
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.scan.session import ScanSession


def test_ir_host_flatten_cancels_falloff_on_mirrored_image(monkeypatch):
    model = MODEL_8200I_SE
    assert model.mirror_x
    geometry = compute_geometry(600, model=model)
    x = np.arange(geometry.pixels)
    # IR light falling to half across the strip, in USB order (STRPIXEL first).
    profile = 40000.0 * (1.0 - 0.5 * x / (geometry.pixels - 1))
    line = np.repeat(profile.astype("<u2")[:, None], 3, axis=1).tobytes()

    asic = SimpleNamespace(last_ir_host_white=[int(v) for v in profile], ir_host_flatten_ready=True)
    session = ScanSession(asic, model)  # type: ignore[arg-type]
    rgb = session.pipeline.assemble(line * geometry.optical_line_count, geometry, expose_base=False)
    # Look at the white-strip flatten alone; the per-image flatten would hide an error.
    monkeypatch.setattr(calib_gl128, "flatten_ir_image_columns", lambda plane: plane)

    plane = session._infrared_plane(rgb)
    left, right = float(plane[:, :50].mean()), float(plane[:, -50:].mean())
    assert abs(left - right) / max(left, right) < 0.02


def _shaded_line(geometry, level: float) -> tuple[np.ndarray, bytes]:
    """A tilted dome across the ASIC columns (USB order) and one RGB16 line of it."""
    x = np.linspace(-1.0, 1.0, geometry.pixels)
    shade = 1.0 - 0.25 * x**2 - 0.10 * x
    line = np.repeat((level * shade).astype("<u2")[:, None], 3, axis=1).tobytes()
    return shade, line


@pytest.mark.parametrize("dpi", [150, 300])
def test_ir_host_flatten_uses_the_whole_profile_below_600_dpi(monkeypatch, dpi):
    """150 / 300 dpi scan at 600 and average down: the profile must be averaged too."""
    model = MODEL_8200I_SE
    geometry = compute_geometry(dpi, model=model)
    assert geometry.host_downsample == 600 // dpi
    shade, line = _shaded_line(geometry, 30000.0)

    asic = SimpleNamespace(last_ir_host_white=[int(50000 * v) for v in shade], ir_host_flatten_ready=True)
    session = ScanSession(asic, model)  # type: ignore[arg-type]
    rgb = session.pipeline.assemble(line * geometry.optical_line_count, geometry, expose_base=False)
    assert rgb.shape[1] == geometry.pixels // geometry.host_downsample
    monkeypatch.setattr(calib_gl128, "flatten_ir_image_columns", lambda plane: plane)

    plane = session._infrared_plane(rgb, host_downsample=geometry.host_downsample).astype(np.float64)
    assert plane.max() / plane.min() < 1.03


@pytest.mark.parametrize("dpi", [150, 300])
def test_host_calib_fallback_uses_the_whole_strip_below_600_dpi(dpi):
    """The colour host dark/white fallback has one entry per ASIC column as well."""
    model = MODEL_8200I_SE
    geometry = compute_geometry(dpi, model=model)
    shade, line = _shaded_line(geometry, 40000.0)
    dark = np.zeros((geometry.pixels, 3), dtype=np.uint16)
    white = np.repeat((60000 * shade).astype(np.uint16)[:, None], 3, axis=1)

    session = ScanSession(SimpleNamespace(), model)  # type: ignore[arg-type]
    rgb = session.pipeline.assemble(
        line * geometry.optical_line_count, geometry, dark=dark, white=white, expose_base=False
    ).astype(np.float64)
    assert rgb.shape[1] == geometry.pixels // geometry.host_downsample
    assert rgb.max() / rgb.min() < 1.01
