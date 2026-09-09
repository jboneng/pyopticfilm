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
