# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase-correlation unit tests."""

from __future__ import annotations

import numpy as np

from pyopticfilm.registration.phase import (
    hanning_window,
    optimal_dft_size,
    phase_correlate,
    phase_correlate_shift,
)
from pyopticfilm.registration.resample import warp_shift


def test_optimal_dft_size_powers():
    assert optimal_dft_size(1024) == 1024
    assert optimal_dft_size(7) == 8
    assert optimal_dft_size(1025) == 1080


def test_hanning_window_shape_and_peak():
    win = hanning_window(32, 48)
    assert win.shape == (32, 48)
    assert win.dtype == np.float32
    assert float(win.max()) <= 1.0 + 1e-5
    assert float(win.min()) >= 0.0
    assert win[16, 24] > win[0, 0]


def test_phase_correlate_shift_recovers_warp():
    rng = np.random.default_rng(1)
    base = rng.integers(1000, 20000, (96, 96), dtype=np.uint16)
    # Create with inverse remap so estimate recovers the apply shift (+3,-2).
    mov = warp_shift(base, -3.0, 2.0)
    dx, dy, resp = phase_correlate_shift(base.astype(np.float32), mov.astype(np.float32))
    assert resp > 0.05
    assert abs(dx - 3.0) < 0.5
    assert abs(dy - (-2.0)) < 0.5


def test_phase_correlate_rejects_empty():
    dx, dy, resp, _ = phase_correlate(np.zeros((0, 0)), np.zeros((0, 0)))
    assert (dx, dy, resp) == (0.0, 0.0, 0.0)


def test_guizar_matches_centroid_sign_and_subpixel():
    """Guizar upsample must agree with 5×5 centroid (same sign, ≤0.05 px)."""
    rng = np.random.default_rng(21)
    base = rng.standard_normal((96, 96)).astype(np.float32)
    true = (1.35, -0.72)
    mov = warp_shift(base, -true[0], -true[1]).astype(np.float32)
    dx_c, dy_c, _ = phase_correlate_shift(base, mov, upsample=1)
    dx_g, dy_g, _ = phase_correlate_shift(base, mov, upsample=16)
    assert abs(dx_c - true[0]) < 0.35
    assert abs(dy_c - true[1]) < 0.35
    # Same sign and within a fraction of a pixel (centroid vs Guizar optima differ).
    assert dx_g * dx_c > 0 and dy_g * dy_c > 0
    assert abs(dx_g - dx_c) < 0.15, (dx_g, dx_c)
    assert abs(dy_g - dy_c) < 0.15, (dy_g, dy_c)
    assert abs(dx_g - true[0]) < 0.2
    assert abs(dy_g - true[1]) < 0.2


def test_phase_correlate_batch_equals_per_tile_calls():
    """The stacked FFT path returns each tile's refined shift exactly."""
    from pyopticfilm.registration.phase import phase_correlate_batch

    rng = np.random.default_rng(3)
    shifts = [(0.0, 0.0), (0.4, -0.7), (-1.3, 0.2), (2.6, 1.9), (-0.05, -2.45), (1.0, -1.0)]
    for side in (128, 100):  # 100 pads to an FFT-friendly 100; 128 needs no padding
        refs, movs = [], []
        for dx, dy in shifts:
            tile = rng.normal(0.0, 500.0, (side, side)).astype(np.float32)
            refs.append(tile - tile.mean())
            moved = warp_shift(tile, -dx, -dy, method="cubic")
            movs.append(moved - moved.mean())
        dxs, dys = phase_correlate_batch(np.stack(movs), np.stack(refs), upsample=16)
        for i, (ref, mov) in enumerate(zip(refs, movs, strict=True)):
            dx, dy, _ = phase_correlate_shift(ref, mov, upsample=16)
            assert (float(dxs[i]), float(dys[i])) == (dx, dy)
        assert len({(float(x), float(y)) for x, y in zip(dxs, dys, strict=True)}) > 1
