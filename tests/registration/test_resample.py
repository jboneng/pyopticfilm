# SPDX-License-Identifier: GPL-3.0-or-later
"""Integer / cubic / banded resample tests."""

from __future__ import annotations

import numpy as np
import pytest

from pyopticfilm.registration.resample import (
    fill_shift_border,
    quadratic_field,
    row_shift_var_scale,
    warp_cubic,
    warp_integer,
    warp_row_shifts,
    warp_shift,
    warp_x_field,
    x_field_var_scale,
)


def test_integer_warp_exact_overlap():
    rng = np.random.default_rng(3)
    src = rng.integers(0, 65535, (32, 40, 3), dtype=np.uint16)
    dx, dy = 3, -2
    out = warp_integer(src, dx, dy)
    # Remap: out[y,x] = src[y-dy, x-dx] away from IR-safe border-fill strips.
    # fill uses ~2*|shift| as the donor, so keep a deeper interior crop.
    y0, y1 = 2 * abs(dy), 32 - 2 * abs(dy)
    x0, x1 = 2 * abs(dx), 40 - 2 * abs(dx)
    assert np.array_equal(out[y0:y1, x0:x1], src[y0 - dy : y1 - dy, x0 - dx : x1 - dx])


def test_cubic_preserves_constant_field():
    src = np.full((48, 48), 12345, dtype=np.uint16)
    out = warp_cubic(src, 1.3, -0.7)
    assert out.dtype == np.uint16
    assert int(out[10:40, 10:40].min()) >= 12345 - 1
    assert int(out[10:40, 10:40].max()) <= 12345 + 1


def test_cubic_clips_to_uint16_rails():
    src = np.full((32, 32), 65535, dtype=np.uint16)
    out = warp_cubic(src, 0.5, 0.5)
    assert out.max() <= 65535
    assert out.min() >= 0


def test_fill_shift_border_uses_interior():
    out = np.zeros((10, 10), dtype=np.uint16)
    out[:, 2:] = 100
    out[:, 4] = 50  # donor at 2*|dx|
    filled = fill_shift_border(out, dx=2.0, dy=0.0)
    assert np.all(filled[:, :2] == 50)


def _row_wave(h: int, w: int, offset: np.ndarray | float = 0.0) -> np.ndarray:
    rows = np.arange(h, dtype=np.float64)[:, None] + (offset[:, None] if isinstance(offset, np.ndarray) else offset)
    return (20000.0 + 8000.0 * np.sin(2.0 * np.pi * rows / 23.0)) * np.ones((h, w))


@pytest.mark.parametrize("method", ["cubic", "lanczos3", "lanczos4"])
def test_warp_row_shifts_undoes_row_drift(method: str):
    h = 240
    dy = np.linspace(-2.0, 2.0, h)
    src = np.rint(_row_wave(h, 8)).astype(np.uint16)
    out = warp_row_shifts(src, 0.0, dy, method=method)
    # dst(y) = src(y - dy[y])
    err = np.abs(out[8:-8].astype(np.float64) - _row_wave(h, 8, -dy)[8:-8])
    assert err.max() < 50.0


def test_warp_row_shifts_rejects_unknown_method():
    with pytest.raises(ValueError):
        warp_row_shifts(np.zeros((4, 4), dtype=np.uint16), 0.0, np.zeros(4), method="nearest")


def test_warp_row_shifts_chunking_does_not_change_output():
    src = (np.arange(80 * 16 * 3) % 4093).reshape(80, 16, 3).astype(np.uint16)
    dy = np.linspace(-1.3, 2.2, 80)
    one = warp_row_shifts(src, 0.4, dy, method="lanczos4", chunk_rows=80)
    many = warp_row_shifts(src, 0.4, dy, method="lanczos4", chunk_rows=32)
    assert np.array_equal(one, many)


@pytest.mark.parametrize("method", ["linear", "cubic", "lanczos4"])
def test_row_shift_var_scale_matches_measured_noise(method: str):
    rng = np.random.default_rng(3)
    src = rng.normal(0.0, 1.0, size=(400, 400)).astype(np.float32)
    dy = np.full(400, 0.5)
    out = warp_row_shifts(src, 0.25, dy, method=method)
    predicted = row_shift_var_scale(0.25, dy, method=method)
    assert predicted[0] == pytest.approx(float(out[16:-16, 16:-16].var()), rel=0.05)


def test_row_shift_var_scale_is_one_on_integer_rows():
    assert np.allclose(row_shift_var_scale(0.0, np.full(8, 2.0), method="lanczos4"), 1.0)


def test_warp_row_shifts_identity():
    rng = np.random.default_rng(4)
    src = rng.integers(0, 1000, (64, 32), dtype=np.uint16)
    dy = np.zeros(64, dtype=np.float64)
    out = warp_row_shifts(src, 0.0, dy)
    assert np.array_equal(out, src)


def test_warp_shift_zero_is_identity():
    arr = np.arange(16, dtype=np.uint16).reshape(4, 4)
    assert np.array_equal(warp_shift(arr, 0.0, 0.0), arr)


def test_register_apply_round_trip_reduces_residual():
    """warp_shift create with -S; estimate ≈ S; apply(+S) aligns (remap)."""
    from pyopticfilm.registration import RegistrationMode, register_and_align
    from pyopticfilm.registration.validate import luma_residual

    rng = np.random.default_rng(9)
    ref = rng.integers(2000, 40000, (256, 320, 3), dtype=np.uint16)
    true = (4.0, -3.0)
    # Inverse of the apply shift: mis is what apply(+true) undoes.
    mis = warp_shift(ref, -true[0], -true[1])
    before = luma_residual(ref, mis)
    aligned, result = register_and_align(ref, mis, RegistrationMode.MULTI_EXPOSURE)
    assert result.accepted
    assert abs(result.shift[0] - true[0]) < 0.5
    assert abs(result.shift[1] - true[1]) < 0.5
    after = luma_residual(ref, aligned)
    assert after < 0.25 * before, (before, after)


def _sines(xs: np.ndarray, ys: np.ndarray, seed: int = 3, n: int = 60) -> np.ndarray:
    """Band-limited texture that can be sampled at any position."""
    rng = np.random.default_rng(seed)
    fx, fy = rng.uniform(-0.25, 0.25, (2, n))
    phase = rng.uniform(0.0, 2.0 * np.pi, n)
    out = np.zeros(np.broadcast(xs, ys).shape)
    for a, b, p in zip(fx, fy, phase, strict=True):
        out += np.sin(2.0 * np.pi * (a * xs + b * ys) + p)
    return 20000.0 + 900.0 * out


def test_quadratic_field_terms():
    coef = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    assert quadratic_field(coef, np.array(0.0), 0.0, 11, 21) == pytest.approx(1.0 - 2.0 + 3.0 - 4.0 + 5.0 + 6.0)
    assert quadratic_field(coef, np.array(5.0), 10.0, 11, 21) == pytest.approx(1.0)
    assert quadratic_field(coef, np.array(10.0), 20.0, 11, 21) == pytest.approx(21.0)


def test_warp_x_field_constant_matches_row_warp():
    yy, xx = np.mgrid[0:64, 0:96].astype(np.float64)
    img = np.rint(_sines(xx, yy)).astype(np.uint16)
    field = warp_x_field(img, 0.3, np.zeros(6), method="lanczos4")
    rows = warp_row_shifts(img, 0.3, np.zeros(64), method="lanczos4")
    assert np.abs(field[:, 8:-8].astype(int) - rows[:, 8:-8].astype(int)).max() <= 3


def test_warp_x_field_follows_the_field():
    yy, xx = np.mgrid[0:256, 0:256].astype(np.float64)
    coef = np.array([0.1, 0.2, 0.0, -0.4, 0.3, 0.1])
    shift = quadratic_field(coef, xx, yy, 256, 256)
    mov = np.rint(_sines(xx + shift, yy)).astype(np.uint16)
    out = warp_x_field(mov, 0.0, coef, method="lanczos4")
    err = out[:, 12:-12].astype(np.float64) - _sines(xx, yy)[:, 12:-12]
    assert float(np.abs(err).mean()) < 25.0


def test_x_field_var_scale_is_one_on_whole_pixels():
    assert x_field_var_scale(2.0, np.zeros(6), 64, 64, method="lanczos4") == pytest.approx(1.0)
    assert x_field_var_scale(0.5, np.zeros(6), 64, 64, method="lanczos4") < 1.0
