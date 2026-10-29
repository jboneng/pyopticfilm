# SPDX-License-Identifier: GPL-3.0-or-later
"""The IR white-strip profile (USB column order) must line up with the mirrored image."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

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
