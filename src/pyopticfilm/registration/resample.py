# SPDX-License-Identifier: GPL-3.0-or-later
"""Apply registration transforms (integer-exact, separable linear/cubic)."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

_LANCZOS_TAPS = {"lanczos3": 3, "lanczos4": 4}


def _warp_workers() -> int:
    return min(8, os.cpu_count() or 1)


@dataclass(frozen=True)
class Overlap:
    src_y: slice
    src_x: slice
    dst_y: slice
    dst_x: slice


def translation_overlap(h: int, w: int, dx: int, dy: int) -> Overlap | None:
    """Common overlap for integer translation of moving → reference frame."""
    if dx >= 0:
        dst_x0, src_x0 = dx, 0
        width = w - dx
    else:
        dst_x0, src_x0 = 0, -dx
        width = w + dx
    if dy >= 0:
        dst_y0, src_y0 = dy, 0
        height = h - dy
    else:
        dst_y0, src_y0 = 0, -dy
        height = h + dy
    if width <= 0 or height <= 0:
        return None
    return Overlap(
        src_y=slice(src_y0, src_y0 + height),
        src_x=slice(src_x0, src_x0 + width),
        dst_y=slice(dst_y0, dst_y0 + height),
        dst_x=slice(dst_x0, dst_x0 + width),
    )


def fill_shift_border(out: np.ndarray, dx: float, dy: float) -> np.ndarray:
    """Replace OOB strips with deeper interior (IR-safe; no edge-column widen).

    Remap with ``+dx`` exposes the left strip; the first valid column often
    still sampled the extreme source edge (hot IR padding). Copy from
    ``~2*|dx|`` when available so that edge is not painted into the strip.
    """
    h, w = out.shape[:2]
    bx = int(np.ceil(abs(float(dx))))
    by = int(np.ceil(abs(float(dy))))
    if bx <= 0 and by <= 0:
        return out
    result = np.array(out, copy=True)
    if bx > 0 and bx < w:
        if dx > 0:
            src_c = min(w - 1, 2 * bx)
            result[:, :bx] = result[:, src_c : src_c + 1]
        elif dx < 0:
            src_c = max(0, w - 1 - 2 * bx)
            result[:, w - bx :] = result[:, src_c : src_c + 1]
    if by > 0 and by < h:
        if dy > 0:
            src_r = min(h - 1, 2 * by)
            result[:by] = result[src_r : src_r + 1]
        elif dy < 0:
            src_r = max(0, h - 1 - 2 * by)
            result[h - by :] = result[src_r : src_r + 1]
    return result


def fill_row_shift_border(out: np.ndarray, dx: float, dy_per_row: np.ndarray) -> np.ndarray:
    """Per-row generalization of :func:`fill_shift_border`."""
    h, w = out.shape[:2]
    result = np.array(out, copy=True)
    bx = int(np.ceil(abs(float(dx))))
    if 0 < bx < w:
        if dx > 0:
            src_c = min(w - 1, 2 * bx)
            result[:, :bx] = result[:, src_c : src_c + 1]
        else:
            src_c = max(0, w - 1 - 2 * bx)
            result[:, w - bx :] = result[:, src_c : src_c + 1]
    src_y = np.arange(h, dtype=np.float64) - dy_per_row
    valid_rows = np.flatnonzero((src_y >= 0) & (src_y <= h - 1))
    if valid_rows.size and valid_rows.size < h:
        first_valid, last_valid = int(valid_rows[0]), int(valid_rows[-1])
        if first_valid > 0:
            result[:first_valid] = result[first_valid]
        if last_valid < h - 1:
            result[last_valid + 1 :] = result[last_valid]
    return result


def warp_integer(mov: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """Integer remap translate: ``out[y, x] = src[y - dy, x - dx]``.

    Matches ``warp_row_shifts`` / phase-correlate apply: positive ``(dx, dy)``
    moves content right/down. OOB samples are zeroed then IR-safe border-filled.
    """
    if dx == 0 and dy == 0:
        return mov
    # Remap via gather at (-dx, -dy); zeros for OOB (not edge-clamp).
    out = _affine_ishift_slices(np.asarray(mov), -int(dx), -int(dy))
    return fill_shift_border(out, float(dx), float(dy))


def _keys_cubic_weights(t):
    """Keys cubic convolution weights for fractional offset ``t`` in [0, 1) (scalar or array)."""
    a = -0.5
    t2 = t * t
    t3 = t2 * t
    w0 = a * (t3 - 2 * t2 + t)
    w1 = (a + 2) * t3 - (a + 3) * t2 + 1
    w2 = -(a + 2) * t3 + (2 * a + 3) * t2 - a * t
    w3 = a * (-t3 + t2)
    return w0, w1, w2, w3


def _linear_weights(shift: float, n: int) -> tuple[np.ndarray, np.ndarray, float, float]:
    """Indices and weights for dst[i] = (1-f)*src[i+base] + f*src[i+base+1]."""
    base = int(np.floor(shift))
    frac = float(shift - base)
    idx = np.arange(n, dtype=np.int64)
    i0 = np.clip(idx + base, 0, n - 1)
    i1 = np.clip(idx + base + 1, 0, n - 1)
    return i0, i1, 1.0 - frac, frac


def _sample_1d_linear(plane: np.ndarray, axis: int, shift: float) -> np.ndarray:
    """Separable bilinear tap along one axis (float32)."""
    if abs(shift) < 1e-12:
        return plane.astype(np.float32, copy=False)
    n = plane.shape[axis]
    i0, i1, w0, w1 = _linear_weights(shift, n)
    if axis == 0:
        a = plane[i0].astype(np.float32, copy=False)
        b = plane[i1].astype(np.float32, copy=False)
    else:
        a = plane[:, i0].astype(np.float32, copy=False)
        b = plane[:, i1].astype(np.float32, copy=False)
    return w0 * a + w1 * b


def _sample_1d_cubic(plane: np.ndarray, axis: int, shift: float) -> np.ndarray:
    """Separable cubic translate along one axis (constant shift)."""
    if abs(shift) < 1e-12:
        return plane.astype(np.float32, copy=False)
    n = plane.shape[axis]
    base = int(np.floor(shift))
    frac = float(shift - base)
    kw0, kw1, kw2, kw3 = _keys_cubic_weights(frac)
    idx = np.arange(n)
    i0 = np.clip(idx + base - 1, 0, n - 1)
    i1 = np.clip(idx + base, 0, n - 1)
    i2 = np.clip(idx + base + 1, 0, n - 1)
    i3 = np.clip(idx + base + 2, 0, n - 1)

    def take(indices: np.ndarray) -> np.ndarray:
        if axis == 0:
            return plane[indices].astype(np.float32, copy=False)
        return plane[:, indices].astype(np.float32, copy=False)

    return kw0 * take(i0) + kw1 * take(i1) + kw2 * take(i2) + kw3 * take(i3)


def _finalize_warp(out: np.ndarray, dtype: np.dtype, dx: float, dy: float) -> np.ndarray:
    if np.issubdtype(dtype, np.integer):
        out = np.clip(np.rint(out), 0, np.iinfo(dtype).max).astype(dtype)
    else:
        out = out.astype(dtype, copy=False)
    return fill_shift_border(out, dx, dy)


def warp_cubic(mov: np.ndarray, dx: float, dy: float) -> np.ndarray:
    """Separable Keys cubic remap: ``out = src(x - dx, y - dy)``."""
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return mov
    if abs(dx - round(dx)) < 1e-6 and abs(dy - round(dy)) < 1e-6:
        return warp_integer(mov, round(dx), round(dy))

    arr = np.asarray(mov)
    dtype = arr.dtype
    # Separable helper uses dst[i]=src[i+s]; remap needs s = -dx / -dy.
    sx, sy = -float(dx), -float(dy)

    def warp_plane(p: np.ndarray) -> np.ndarray:
        tmp = _sample_1d_cubic(p, axis=1, shift=sx) if abs(sx) >= 1e-12 else p.astype(np.float32)
        return _sample_1d_cubic(tmp, axis=0, shift=sy) if abs(sy) >= 1e-12 else tmp

    if arr.ndim == 3:
        out = np.stack([warp_plane(arr[:, :, c]) for c in range(arr.shape[2])], axis=2)
    else:
        out = warp_plane(arr)
    return _finalize_warp(out, dtype, dx, dy)


def _affine_ishift_slices(arr: np.ndarray, sx: int, sy: int) -> np.ndarray:
    """Gather helper: ``out[y, x] = arr[y + sy, x + sx]`` on in-bounds overlap."""
    h, w = arr.shape[:2]
    out = np.zeros_like(arr)
    y0 = max(0, -sy)
    y1 = min(h, h - sy)
    x0 = max(0, -sx)
    x1 = min(w, w - sx)
    if y1 > y0 and x1 > x0:
        out[y0:y1, x0:x1, ...] = arr[y0 + sy : y1 + sy, x0 + sx : x1 + sx, ...]
    return out


def warp_linear(
    mov: np.ndarray,
    dx: float,
    dy: float,
    *,
    chunk_rows: int = 512,
) -> np.ndarray:
    """Bilinear remap: ``out[y, x] = src[y - dy, x - dx]``.

    Implemented via gather slices with negated offsets. ``chunk_rows`` is
    accepted for API compatibility and unused.
    """
    del chunk_rows
    if not np.isfinite(dx) or not np.isfinite(dy):
        return mov
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return mov
    if abs(dx - round(dx)) < 1e-6 and abs(dy - round(dy)) < 1e-6:
        return warp_integer(mov, round(dx), round(dy))

    arr = np.asarray(mov)
    dtype = arr.dtype
    # Remap (dx, dy) ⇒ gather at (-dx, -dy); bilinear in gather space.
    gx, gy = -float(dx), -float(dy)
    ix = int(np.floor(gx))
    iy = int(np.floor(gy))
    fx = float(gx - ix)
    fy = float(gy - iy)

    c00 = _affine_ishift_slices(arr, ix, iy)
    if abs(fx) < 1e-6 and abs(fy) < 1e-6:
        out = c00.astype(np.float32, copy=False)
    elif abs(fx) < 1e-6:
        c01 = _affine_ishift_slices(arr, ix, iy + 1)
        out = (1.0 - fy) * c00.astype(np.float32) + fy * c01.astype(np.float32)
    elif abs(fy) < 1e-6:
        c10 = _affine_ishift_slices(arr, ix + 1, iy)
        out = (1.0 - fx) * c00.astype(np.float32) + fx * c10.astype(np.float32)
    else:
        c10 = _affine_ishift_slices(arr, ix + 1, iy)
        c01 = _affine_ishift_slices(arr, ix, iy + 1)
        c11 = _affine_ishift_slices(arr, ix + 1, iy + 1)
        c00f = c00.astype(np.float32)
        out = (
            (1.0 - fx) * (1.0 - fy) * c00f
            + fx * (1.0 - fy) * c10.astype(np.float32)
            + (1.0 - fx) * fy * c01.astype(np.float32)
            + fx * fy * c11.astype(np.float32)
        )
    return _finalize_warp(out, dtype, dx, dy)


def warp_shift(
    mov: np.ndarray,
    dx: float,
    dy: float,
    *,
    method: str = "linear",
    chunk_rows: int = 512,
) -> np.ndarray:
    """Remap-translate ``mov`` by ``(dx, dy)`` — linear or cubic."""
    if method == "cubic":
        return warp_cubic(mov, dx, dy)
    return warp_linear(mov, dx, dy, chunk_rows=chunk_rows)


def _lanczos_weights(frac: np.ndarray, a: int) -> tuple[np.ndarray, ...]:
    """Normalized Lanczos-``a`` taps at offsets ``1-a .. a`` from ``floor(pos)``."""
    dist = np.arange(1 - a, a + 1, dtype=np.float64)[:, None] - frac
    w = np.sinc(dist) * np.sinc(dist / a)
    return tuple(w / w.sum(axis=0))


def _row_tap_weights(frac: np.ndarray, method: str) -> tuple[int, tuple[np.ndarray, ...]]:
    """First tap offset from ``floor(pos)`` and the per-row tap weights."""
    if method in _LANCZOS_TAPS:
        a = _LANCZOS_TAPS[method]
        return 1 - a, tuple(w.astype(np.float32) for w in _lanczos_weights(frac, a))
    if method == "cubic":
        return -1, tuple(np.asarray(w, dtype=np.float32) for w in _keys_cubic_weights(frac))
    return 0, ((1.0 - frac).astype(np.float32), frac.astype(np.float32))


def row_shift_var_scale(dx: float, dy_per_row: np.ndarray, *, method: str = "linear") -> np.ndarray:
    """Per-row variance factor of white noise through :func:`warp_row_shifts`.

    A resampling tap averages neighbours, scaling variance by the sum of its
    squared weights, on each axis. Rows at an integer offset keep their noise.
    """
    dy = np.asarray(dy_per_row, dtype=np.float64)
    h = dy.shape[0]
    src_y = np.arange(h, dtype=np.float64) - dy
    _, weights = _row_tap_weights(src_y - np.floor(src_y), method)
    vertical = np.sum(np.stack(weights).astype(np.float64) ** 2, axis=0)
    t = -float(dx) - np.floor(-float(dx))
    if t < 1e-12:
        return vertical
    _, horizontal = _row_tap_weights(np.asarray([t]), method)
    return vertical * float(np.sum(np.stack(horizontal).astype(np.float64) ** 2))


def quadratic_field(coef: np.ndarray, x: np.ndarray, y: np.ndarray | float, w: int, h: int) -> np.ndarray:
    """``c0 + c1 v + c2 v^2 + u (c3 + c4 v) + c5 u^2``, with ``u, v`` in [-1, 1] over the frame."""
    u = 2.0 * np.asarray(x, dtype=np.float64) / max(w - 1, 1) - 1.0
    v = 2.0 * np.asarray(y, dtype=np.float64) / max(h - 1, 1) - 1.0
    return coef[0] + coef[1] * v + coef[2] * v * v + u * (coef[3] + coef[4] * v) + coef[5] * u * u


def x_field_var_scale(dx: float, coef: np.ndarray, w: int, h: int, *, method: str = "linear") -> float:
    """Mean variance factor of white noise through :func:`warp_x_field`."""
    xs = np.linspace(0.0, w - 1, 33)
    ys = np.linspace(0.0, h - 1, 33)[:, None]
    pos = -(float(dx) + quadratic_field(coef, xs, ys, w, h)).ravel()
    _, weights = _row_tap_weights(pos - np.floor(pos), method)
    return float(np.mean(np.sum(np.stack(weights).astype(np.float64) ** 2, axis=0)))


def warp_x_field(mov: np.ndarray, dx: float, coef: np.ndarray, *, method: str = "linear") -> np.ndarray:
    """``dst(x, y) = src(x - dx - field(x, y), y)``; see :func:`quadratic_field`.

    The field is held constant over row bands short enough that it moves by
    under 0.02 px across one.
    """
    arr = np.asarray(mov)
    h, w = arr.shape[:2]
    dtype = arr.dtype
    integer = np.issubdtype(dtype, np.integer)
    top = np.iinfo(dtype).max if integer else 0
    per_row = 2.0 * (abs(coef[1]) + 2.0 * abs(coef[2]) + abs(coef[4])) / max(h - 1, 1)
    band_rows = int(np.clip(0.02 / max(per_row, 1e-9), 1, 64))
    # One taps table: computing weights per pixel is most of the cost otherwise.
    phases = 1024
    first, table = _row_tap_weights(np.arange(phases + 1, dtype=np.float64) / phases, method)
    xs = np.arange(w, dtype=np.float64)
    trail = (None,) * (arr.ndim - 2)
    out = np.empty(arr.shape, dtype=dtype)

    def warp_band(r0: int) -> None:
        r1 = min(h, r0 + band_rows)
        pos = xs - dx - quadratic_field(coef, xs, 0.5 * (r0 + r1 - 1), w, h)
        base = np.floor(pos).astype(np.int64)
        phase = np.rint((pos - base) * phases).astype(np.int64)
        src = arr[r0:r1].astype(np.float32)
        acc = np.zeros(src.shape, dtype=np.float32)
        for i, taps in enumerate(table):
            acc += taps[phase][(None, slice(None), *trail)] * src[:, np.clip(base + first + i, 0, w - 1)]
        out[r0:r1] = np.clip(np.rint(acc), 0, top) if integer else acc

    with ThreadPoolExecutor(max_workers=_warp_workers()) as pool:
        list(pool.map(warp_band, range(0, h, band_rows)))
    return out


def warp_row_shifts(
    mov: np.ndarray,
    dx: float,
    dy_per_row: np.ndarray,
    *,
    method: str = "linear",
    chunk_rows: int = 512,
) -> np.ndarray:
    """Per-row dy + constant dx without HxW map arrays (remap convention).

    Remap convention ``dst(x,y) = src(x - dx, y - dy[y])``. ``method`` is
    ``"linear"``, ``"cubic"`` or ``"lanczos3"`` / ``"lanczos4"``, on both axes.
    Works in row chunks, so no full-frame float32 plane is held.
    """
    if method not in ("linear", "cubic", *_LANCZOS_TAPS):
        raise ValueError(f"unknown resampling method {method!r}")
    arr = np.asarray(mov)
    h, _w = arr.shape[:2]
    dy = np.asarray(dy_per_row, dtype=np.float64)
    if dy.shape[0] != h:
        raise ValueError("dy_per_row length must match image height")

    dtype = arr.dtype
    # Horizontal: dst(x) = src(x - dx). The separable helpers use dst(x)=src(x+s) ⇒ s = -dx.
    shift_x = -float(dx)
    base_x = int(np.floor(shift_x))
    first_x, weights_x = _row_tap_weights(np.asarray([shift_x - base_x]), method)
    cols = np.arange(arr.shape[1])
    tap_cols = [np.clip(cols + base_x + first_x + i, 0, arr.shape[1] - 1) for i in range(len(weights_x))]

    def horiz(p: np.ndarray) -> np.ndarray:
        if abs(shift_x) < 1e-12:
            return p.astype(np.float32, copy=False)
        acc = float(weights_x[0][0]) * p[:, tap_cols[0]].astype(np.float32)
        for w_i, c in zip(weights_x[1:], tap_cols[1:], strict=True):
            acc += float(w_i[0]) * p[:, c].astype(np.float32)
        return acc

    # Vertical: dst(y) = src(y - dy[y]) ⇒ sample index sy = y - dy[y]
    src_y = np.arange(h, dtype=np.float64) - dy
    y0 = np.floor(src_y).astype(np.int64)
    first_tap, weights = _row_tap_weights(src_y - y0, method)
    tap_rows = [np.clip(y0 + first_tap + i, 0, h - 1) for i in range(len(weights))]
    trail = (None,) * (arr.ndim - 1)

    integer = np.issubdtype(dtype, np.integer)
    top = np.iinfo(dtype).max if integer else 0
    out = np.empty(arr.shape, dtype=dtype)
    chunk = max(32, int(chunk_rows))

    def warp_band(r0: int) -> None:
        r1 = min(h, r0 + chunk)
        lo = int(min(t[r0:r1].min() for t in tap_rows))
        hi = int(max(t[r0:r1].max() for t in tap_rows)) + 1
        slab = horiz(arr[lo:hi])
        acc = weights[0][r0:r1][(slice(None), *trail)] * slab[tap_rows[0][r0:r1] - lo]
        for w_i, rows in zip(weights[1:], tap_rows[1:], strict=True):
            acc += w_i[r0:r1][(slice(None), *trail)] * slab[rows[r0:r1] - lo]
        out[r0:r1] = np.clip(np.rint(acc), 0, top) if integer else acc

    with ThreadPoolExecutor(max_workers=_warp_workers()) as pool:
        list(pool.map(warp_band, range(0, h, chunk)))
    return fill_row_shift_border(out, dx, dy)


def row_shift_touch_mask(mask: np.ndarray, dx: float, dy_per_row: np.ndarray, *, reach: int) -> np.ndarray:
    """Where :func:`warp_row_shifts` output lies within ``reach`` px of a set ``mask`` sample."""
    h, w = mask.shape[:2]
    src_y = np.floor(np.arange(h, dtype=np.float64) - np.asarray(dy_per_row, dtype=np.float64)).astype(np.int64)
    out = np.zeros(mask.shape, dtype=bool)
    for offset in range(-reach, reach + 2):
        out |= mask[np.clip(src_y + offset, 0, h - 1)]
    base_x = int(np.floor(-float(dx)))
    if abs(float(dx) - round(float(dx))) >= 1e-6:
        cols = np.arange(w)
        wide = np.zeros(mask.shape, dtype=bool)
        for offset in range(-reach, reach + 2):
            wide |= out[:, np.clip(cols + base_x + offset, 0, w - 1)]
        return wide
    return out[:, np.clip(np.arange(w) + base_x, 0, w - 1)]


def valid_mask_translation(h: int, w: int, dx: float, dy: float) -> np.ndarray:
    """Boolean mask of pixels that sample inside the moving image (remap)."""
    yy = np.arange(h, dtype=np.float64)[:, None]
    xx = np.arange(w, dtype=np.float64)[None, :]
    sx = xx - dx
    sy = yy - dy
    return (sx >= 0) & (sx <= w - 1) & (sy >= 0) & (sy <= h - 1)
