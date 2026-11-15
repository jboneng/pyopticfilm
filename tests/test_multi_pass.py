# SPDX-License-Identifier: GPL-3.0-or-later
"""merge_n_passes (same-exposure Multi-Pass stacking) tests."""

from __future__ import annotations

import numpy as np
import pytest

from pyopticfilm.scan.exposure_merge import merge_n_passes


def test_merge_n_passes_single_frame_returns_copy():
    frame = np.full((4, 4, 3), 8000, dtype=np.uint16)
    result = merge_n_passes([frame])
    assert np.array_equal(result.rgb, frame)
    assert result.rgb is not frame  # copy, not the same array
    assert result.fusion_stats is not None
    assert result.fusion_stats.n_frames == 1
    assert result.fusion_stats.outlier_pixels == 0
    assert result.fusion_stats.zero_weight_pixels == 0


def test_merge_n_passes_reduces_to_plain_mean_when_no_clip_no_outlier():
    """Mid-range, near-identical frames: roughly-equal confidence/variance
    per frame means the IVW-style weighting collapses to ~the plain mean."""
    frames = [
        np.full((16, 16, 3), 8000, dtype=np.uint16),
        np.full((16, 16, 3), 8100, dtype=np.uint16),
        np.full((16, 16, 3), 7900, dtype=np.uint16),
    ]
    result = merge_n_passes(frames)
    assert abs(float(result.rgb.mean()) - 8000.0) < 5.0
    assert result.fusion_stats is not None
    assert result.fusion_stats.n_frames == 3
    assert result.fusion_stats.mean_confidence > 0.9
    assert result.fusion_stats.outlier_pixels == 0


def test_merge_n_passes_clip_aware_weighting_downweights_clipped_frame():
    """A frame that clipped high (sensor rail) must not pull the average up —
    its confidence collapses to ~0 via the same floor/ceiling ramp used by
    the pairwise merge."""
    frames = [
        np.full((8, 8, 3), 8000, dtype=np.uint16),
        np.full((8, 8, 3), 8000, dtype=np.uint16),
        np.full((8, 8, 3), 65000, dtype=np.uint16),  # clipped
    ]
    result = merge_n_passes(frames)
    assert abs(float(result.rgb.mean()) - 8000.0) < 50.0


def test_merge_n_passes_outlier_guard_excludes_misaligned_frame():
    """A residual-misalignment ghost (one frame disagrees sharply in luma at
    a region, unrelated to clipping) must be excluded there, not blended in."""
    good_a = np.full((32, 32, 3), 8000, dtype=np.uint16)
    good_b = np.full((32, 32, 3), 8000, dtype=np.uint16)
    ghosted = good_a.copy()
    ghosted[8:24, 8:24, :] = 20000  # simulated misaligned edge content
    result = merge_n_passes([good_a, good_b, ghosted])
    region = result.rgb[8:24, 8:24, :].astype(np.float64)
    assert abs(region.mean() - 8000.0) < 20.0
    assert result.fusion_stats is not None
    assert result.fusion_stats.outlier_pixels >= 16 * 16


def test_merge_n_passes_validates_frame_count():
    with pytest.raises(ValueError):
        merge_n_passes([])


def test_merge_n_passes_validates_matching_shapes():
    a = np.zeros((4, 4, 3), dtype=np.uint16)
    b = np.zeros((4, 5, 3), dtype=np.uint16)
    with pytest.raises(ValueError):
        merge_n_passes([a, b])


def test_merge_n_passes_zero_weight_pixels_counts_all_outlier_fallback():
    """A pixel where every frame gets luma-outlier-flagged against the others
    (no single frame anchors the median) falls back to the plain mean via
    ``no_weight`` even though no frame's confidence is anywhere near zero —
    ``zero_weight_pixels`` must count that fallback, not just near-zero-
    confidence pixels (``all_zero_conf``), or it silently undercounts."""
    a = np.full((4, 4, 3), 5000, dtype=np.uint16)
    b = np.full((4, 4, 3), 5000, dtype=np.uint16)
    c = np.full((4, 4, 3), 25000, dtype=np.uint16)
    d = np.full((4, 4, 3), 25000, dtype=np.uint16)
    result = merge_n_passes([a, b, c, d])
    assert result.fusion_stats is not None
    assert result.fusion_stats.zero_weight_pixels == 4 * 4
    # Sanity: none of these mid-range, unclipped values collapse confidence
    # to ~0, so the old (wrong) all_zero_conf-based count would have been 0.
    assert result.fusion_stats.mean_confidence > 0.5


def test_merge_n_passes_all_frames_black_stays_black():
    frames = [np.zeros((4, 4, 3), dtype=np.uint16) for _ in range(3)]
    result = merge_n_passes(frames)
    assert np.all(result.rgb == 0)
    assert result.fusion_stats is not None
    assert result.fusion_stats.zero_weight_fraction == 1.0


def test_merge_n_passes_below_floor_keeps_mean():
    """Every repeat under the noise floor (131 DN): plain mean, not black."""
    frames = [np.full((4, 4, 3), v, dtype=np.uint16) for v in (100, 120, 110)]
    result = merge_n_passes(frames)
    assert np.all(result.rgb == 110)


def test_merge_n_passes_all_frames_clipped_keep_clip_level():
    """Clipped in every repeat: no confident frame, but the pixel stays bright."""
    frames = [np.full((4, 4, 3), 65535, dtype=np.uint16) for _ in range(3)]
    frames[1][:, :, 0] = 63000
    result = merge_n_passes(frames)
    assert np.all(result.rgb[:, :, 1:] == 65535)
    assert np.all(result.rgb[:, :, 0] == round((65535 * 2 + 63000) / 3))


def test_merge_n_passes_just_below_clip_end_is_not_black():
    """Confidence is already ~0 a few DN below the clip end; 62256 must keep its level."""
    frames = [np.full((4, 4, 3), 62256, dtype=np.uint16) for _ in range(3)]
    result = merge_n_passes(frames)
    assert np.all(result.rgb == 62256)


def _reference_merge_n_passes(frames: list[np.ndarray]) -> tuple[np.ndarray, tuple[float, int, int]]:
    """The single-threaded stack-and-median merge that :func:`merge_n_passes` must reproduce."""
    from pyopticfilm.scan import exposure_merge as em

    h, w = frames[0].shape[:2]
    out = np.empty((h, w, 3), dtype=np.uint16)
    conf_sum, n_conf, zero_count, outlier_count = 0.0, 0, 0, 0
    for y0 in range(0, h, em._MERGE_CHUNK_ROWS):
        y1 = min(h, y0 + em._MERGE_CHUNK_ROWS)
        xs = [np.asarray(f[y0:y1], dtype=np.float32) for f in frames]
        cs = [
            em._smooth_confidence(x, floor=em._SNR_FLOOR, clip_start=em._SNR_CLIP_START, clip_end=em._SNR_CLIP_END)
            for x in xs
        ]
        lumas = [x.mean(axis=2) for x in xs]
        luma_median = np.median(np.stack(lumas, axis=0), axis=0)
        weights = []
        outlier_any = np.zeros((y1 - y0, w), dtype=bool)
        for x, c, lum in zip(xs, cs, lumas, strict=True):
            w_i = c / np.maximum(em._SNR_ALPHA * np.maximum(x, 0.0) + em._SNR_BETA, 1e-12)
            is_outlier = np.abs(lum - luma_median) > em._LUMA_DISAGREE_TAU
            weights.append(np.where(is_outlier[..., np.newaxis], 0.0, w_i))
            outlier_any |= is_outlier
        acc = weights[0] * xs[0]
        w_sum = weights[0].copy()
        all_zero_conf = cs[0] <= 1e-6
        for x, c, w_i in zip(xs[1:], cs[1:], weights[1:], strict=True):
            acc += w_i * x
            w_sum += w_i
            all_zero_conf &= c <= 1e-6
        merged = acc / np.maximum(w_sum, 1e-12)
        no_weight = w_sum <= 1e-9
        plain_mean = np.mean(np.stack(xs, axis=0), axis=0)
        merged = np.where(no_weight | all_zero_conf, plain_mean, merged)
        out[y0:y1] = np.clip(merged, 0, 65535).astype(np.uint16)
        conf_mean_pixel = np.mean(np.stack(cs, axis=0), axis=0)
        conf_sum += float(conf_mean_pixel.sum())
        n_conf += int(conf_mean_pixel.size)
        zero_count += int(np.count_nonzero(np.all(no_weight, axis=-1)))
        outlier_count += int(np.count_nonzero(outlier_any))
    return out, (conf_sum / n_conf, outlier_count, zero_count)


@pytest.mark.parametrize("n_frames", [2, 3, 4, 5])
def test_merge_n_passes_matches_reference_merge(n_frames):
    """Banded threads, the confidence lookup and the median selections change no bit."""
    rng = np.random.default_rng(n_frames)
    h, w = 300, 96  # three row bands, the last one short
    base = rng.integers(0, 65536, (h, w, 3)).astype(np.float64)
    base[:40] = rng.integers(0, 300, (40, w, 3))  # around the noise floor
    base[40:80] = rng.integers(50000, 65536, (40, w, 3))  # through the clip ramp
    frames = [np.clip(base + rng.normal(0.0, 150.0, base.shape), 0, 65535).astype(np.uint16) for _ in range(n_frames)]
    frames[-1][100:140] = 65535 - frames[-1][100:140]  # one frame disagrees
    for f in frames:
        f[200:220] = rng.integers(0, 65536, (20, w, 3))  # every frame disagrees

    result = merge_n_passes(frames)
    expected_rgb, (mean_conf, outliers, zero_weight) = _reference_merge_n_passes(frames)

    assert np.array_equal(result.rgb, expected_rgb)
    stats = result.fusion_stats
    assert stats.mean_confidence == mean_conf
    assert (stats.outlier_pixels, stats.zero_weight_pixels) == (outliers, zero_weight)
    assert outliers > 0
