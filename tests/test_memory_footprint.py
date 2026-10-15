# SPDX-License-Identifier: GPL-3.0-or-later
"""Host memory footprint at high dpi: no full-frame wide-dtype temporaries.

Peaks are measured with ``tracemalloc`` (NumPy reports its buffers to it) on a
cropped SE geometry, so the bounds scale to a 7200 dpi full frame.
"""

from __future__ import annotations

import tracemalloc

import numpy as np
import pytest

from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.scan.pipeline import ImagePipeline


def _peak_bytes(fn):
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        result = fn()
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


def _se_raw(dpi: int, area, *, seed: int = 0):
    geo = compute_geometry(dpi, model=MODEL_8200I_SE, area=area)
    rng = np.random.default_rng(seed)
    words = rng.integers(0, 30000, size=geo.total_bytes // 2, dtype=np.uint16)
    return geo, bytearray(words.tobytes())


def test_assemble_peak_stays_below_two_decoded_frames():
    geo, raw = _se_raw(7200, (0.3, 0.3, 0.5, 0.45))
    assert geo.optical_line_count // geo.lines == 2
    decoded = len(raw)
    pipe = ImagePipeline(MODEL_8200I_SE)
    out, peak = _peak_bytes(lambda: pipe.assemble(raw, geo, expose_base=True))
    assert out.dtype == np.uint16
    # Decoded frame + Y-reduced frame + a couple of uint32 row slabs. The old
    # chain held two decoded copies, then a full uint32 copy plus sums (~4x).
    slab = 256 * geo.pixels * 3 * 4
    assert peak < 1.5 * decoded + 2 * slab, (peak, decoded, slab)


def test_assemble_accepts_bytearray_and_matches_bytes():
    geo, raw = _se_raw(3600, (0.15, 0.2, 0.7, 0.6), seed=3)
    pipe = ImagePipeline(MODEL_8200I_SE)
    a = pipe.assemble(raw, geo, expose_base=False)
    b = ImagePipeline(MODEL_8200I_SE).assemble(bytes(raw), geo, expose_base=False)
    assert np.array_equal(a, b)
    assert a.flags["C_CONTIGUOUS"]


@pytest.mark.parametrize("n", [2, 3, 4])
def test_reduce_y_oversample_matches_integer_mean(n):
    rows, width = 600, 37
    groups = rows // n
    geo = compute_geometry(1800, model=MODEL_8200I_SE)
    from dataclasses import replace

    geo = replace(geo, lines=groups, optical_line_count=groups * n)
    rng = np.random.default_rng(n)
    rgb = rng.integers(0, 65536, size=(rows, width, 3), dtype=np.uint16)
    trimmed = rgb[: groups * n].reshape(groups, n, width, 3).astype(np.uint64)
    expect = ((trimmed.sum(axis=1) + n // 2) // n).astype(np.uint16)
    out = ImagePipeline().reduce_y_oversample(rgb, geo)
    assert out.dtype == np.uint16
    assert np.array_equal(out, expect)


def test_expose_and_clamp_inplace_match_copy():
    rng = np.random.default_rng(5)
    rgb = rng.integers(0, 20000, size=(300, 400, 3), dtype=np.uint16)
    rgb[:8] = 64000
    pipe = ImagePipeline()
    ref = pipe.clamp_border_highlights(
        pipe.expose_film_base(rgb, source="t", preserve_headroom=True)
    )
    work = rgb.copy()
    got = pipe.expose_film_base(work, source="t", preserve_headroom=True, inplace=True)
    got = pipe.clamp_border_highlights(got, inplace=True)
    assert got is work
    assert np.array_equal(got, ref)
    assert not np.array_equal(rgb, work)


def _float64_selection_reference(short, short_exposure, long_exposure, black, q):
    arr = short.astype(np.float64)
    signal = np.maximum(arr[..., :3] - black, 0.0)
    dense = np.percentile(signal.reshape(-1, 3), q, axis=0)
    predicted = black + signal * (long_exposure / short_exposure)
    n = arr.shape[0] * arr.shape[1]
    clips = tuple(float(np.count_nonzero(predicted[..., c] >= 0.95 * 65535.0)) / n for c in range(3))
    return dense, clips


@pytest.mark.parametrize("black", [0.0, 640.0])
def test_me_selection_matches_float64_reference(black):
    from pyopticfilm.scan.me_exposure import dense_percentiles, predicted_clip_fractions

    rng = np.random.default_rng(11)
    short = rng.integers(0, 40000, size=(513, 300, 3), dtype=np.uint16)
    dense_ref, clips_ref = _float64_selection_reference(short, 14000, 60000, black, 5.0)
    dense = dense_percentiles(short, black_level=black, dense_percentile=5.0)
    clips = predicted_clip_fractions(short, 14000, 60000, black_level=black)
    assert dense == tuple(float(v) for v in dense_ref)
    assert clips == clips_ref


def test_me_selection_allocates_no_float64_frame():
    from pyopticfilm.scan.me_exposure import select_long_exposure

    rng = np.random.default_rng(2)
    short = rng.integers(0, 30000, size=(3000, 1600, 3), dtype=np.uint16)
    _decision, peak = _peak_bytes(lambda: select_long_exposure(short, 14000))
    # One float64 copy of the frame alone would be 4x short.nbytes.
    assert peak < short.nbytes, (peak, short.nbytes)


def _per_channel_warp_reference(mov, dx, dy):
    import cv2

    h, w = mov.shape[:2]
    matrix = np.array([[1.0, 0.0, dx], [0.0, 1.0, dy]], dtype=np.float32)
    planes = [
        cv2.warpAffine(
            mov[:, :, c], matrix, (w, h),
            flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0,
        )
        for c in range(3)
    ]
    return np.stack(planes, axis=2)


def test_warp_shift_matches_per_channel_opencv():
    pytest.importorskip("cv2")
    from pyopticfilm.pass_align import _warp_shift

    rng = np.random.default_rng(7)
    mov = rng.integers(0, 65536, size=(320, 410, 3), dtype=np.uint16)
    dx, dy = 3.37, -2.81
    ref = _per_channel_warp_reference(mov, dx, dy)
    got = _warp_shift(mov, dx, dy)
    # Border strips are refilled from the interior; compare the warped body.
    assert got.dtype == np.uint16
    assert np.array_equal(got[4:-4, 4:-4], ref[4:-4, 4:-4])


def test_banded_row_warp_matches_single_remap():
    cv2 = pytest.importorskip("cv2")
    from pyopticfilm import pass_align
    from pyopticfilm.pass_align import _warp_row_shifts

    rng = np.random.default_rng(9)
    h, w = 3 * pass_align._WARP_BAND_ROWS + 17, 300
    mov = rng.integers(0, 65536, size=(h, w, 3), dtype=np.uint16)
    dx = 1.7
    dy = np.linspace(-2.2, 4.6, h)
    map_x = np.broadcast_to(np.arange(w, dtype=np.float32) - dx, (h, w)).copy()
    map_y = np.broadcast_to(
        (np.arange(h, dtype=np.float32) - dy.astype(np.float32))[:, np.newaxis], (h, w)
    ).copy()
    ref = np.stack(
        [
            cv2.remap(
                mov[:, :, c], map_x, map_y,
                interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0,
            )
            for c in range(3)
        ],
        axis=2,
    )
    got = _warp_row_shifts(mov, dx, dy)
    assert np.array_equal(got[5:-5, 2:], ref[5:-5, 2:])
