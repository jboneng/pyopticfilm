# SPDX-License-Identifier: GPL-3.0-or-later
"""Multi-exposure merge and alignment tests."""

from __future__ import annotations

import numpy as np

from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.pass_align import align_pass_to_reference
from pyopticfilm.scan.exposure_merge import merge_exposures, merge_exposures_result


def test_model_me_exposure_constants():
    assert MODEL_8200I_SE.exposure_short == 14000
    assert MODEL_8200I_SE.exposure_long == 42000
    assert MODEL_8200I_SE.multi_exposure_factor == 3
    assert MODEL_8200I_SE.channel_exposure_for(1800, exposure=42000) == 42000 // 4


def test_model_pixel_clock_long_at_1800():
    assert MODEL_8200I_SE.pixel_clock_for_image(1800, long_exposure=False) == 0x02
    assert MODEL_8200I_SE.pixel_clock_for_image(1800, long_exposure=True) == 0x01


def test_align_pass_zero_shift_is_identity():
    arr = np.arange(64 * 64 * 3, dtype=np.uint16).reshape(64, 64, 3)
    aligned, shift = align_pass_to_reference(arr, arr, shift=(0, 0))
    assert shift == (0, 0)
    assert np.array_equal(aligned, arr)


def _luma_mean(rgb: np.ndarray) -> float:
    a = rgb.astype(np.float64)
    return float((0.2126 * a[:, :, 0] + 0.7152 * a[:, :, 1] + 0.0722 * a[:, :, 2]).mean())


def test_assemble_expose_base_false_preserves_me_ratio():
    """Per-plane film-base makeup collapses a 3× bracket; expose_base=False keeps it."""
    from pyopticfilm.scan.geometry import compute_geometry
    from pyopticfilm.scan.pipeline import ImagePipeline

    pipe = ImagePipeline(MODEL_8200I_SE)
    geometry = compute_geometry(1800, model=MODEL_8200I_SE)
    h, w = 64, 64
    short = np.full((h, w, 3), 8000, dtype=np.uint16)
    long = np.full((h, w, 3), 24000, dtype=np.uint16)  # 3×

    # Bypass decode: feed through assemble after mocking decode_rgb.
    pipe.decode_rgb = lambda raw, **_k: (  # type: ignore[method-assign]
        long.copy() if raw == b"L" else short.copy()
    )
    # Avoid oversample/shift changing levels for this tiny stub geometry.
    pipe.reduce_y_oversample = lambda rgb, _g: rgb  # type: ignore[method-assign]
    pipe.apply_line_shifts = lambda rgb, _g: rgb  # type: ignore[method-assign]
    pipe.apply_y_stagger = lambda rgb, _g: rgb  # type: ignore[method-assign]
    pipe.apply_host_downsample = lambda rgb, _g: rgb  # type: ignore[method-assign]

    out_s = pipe.assemble(b"S", geometry, dark=None, white=None, expose_base=False)
    out_l = pipe.assemble(b"L", geometry, dark=None, white=None, expose_base=False)
    ratio_linear = _luma_mean(out_l) / max(_luma_mean(out_s), 1.0)
    assert 2.9 <= ratio_linear <= 3.1

    out_s_ex = pipe.assemble(b"S", geometry, dark=None, white=None, expose_base=True)
    out_l_ex = pipe.assemble(b"L", geometry, dark=None, white=None, expose_base=True)
    ratio_exposed = _luma_mean(out_l_ex) / max(_luma_mean(out_s_ex), 1.0)
    # Independent peak stretch toward 0xF000 collapses the bracket.
    assert ratio_exposed < 1.5


def test_locked_usb_end_drop_allows_me_merge_when_edges_differ():
    """ME short/long with unequal dark END columns merge when drop is locked."""
    from pyopticfilm.scan.geometry import compute_geometry
    from pyopticfilm.scan.pipeline import ImagePipeline

    pipe = ImagePipeline(MODEL_8200I_SE)
    geometry = compute_geometry(3600, model=MODEL_8200I_SE, area=(0.15, 0.2, 0.7, 0.6))
    assert geometry.usb_end_drop == 48
    h = max(4, geometry.lines)
    w = geometry.pixels
    short = np.full((h, w, 3), 12_000, dtype=np.uint16)
    short[:, -24:, :] = 100
    long = np.full((h, w, 3), 36_000, dtype=np.uint16)

    pipe.reduce_y_oversample = lambda rgb, _g: rgb  # type: ignore[method-assign]
    pipe.apply_line_shifts = lambda rgb, _g: rgb  # type: ignore[method-assign]
    pipe.apply_y_stagger = lambda rgb, _g: rgb  # type: ignore[method-assign]
    pipe.apply_host_downsample = lambda rgb, _g: rgb  # type: ignore[method-assign]

    pipe.decode_rgb = lambda *_a, **_k: short.copy()  # type: ignore[method-assign]
    out_s = pipe.assemble(b"S", geometry, dark=None, white=None, expose_base=False)
    locked = pipe.last_usb_end_drop
    assert locked == 24

    pipe.decode_rgb = lambda *_a, **_k: long.copy()  # type: ignore[method-assign]
    out_l_unlocked = pipe.assemble(b"L", geometry, dark=None, white=None, expose_base=False)
    assert out_l_unlocked.shape[1] != out_s.shape[1]

    out_l = pipe.assemble(
        b"L", geometry, dark=None, white=None, expose_base=False, usb_end_drop=locked
    )
    assert out_s.shape == out_l.shape
    result = merge_exposures_result(
        out_s, out_l, exposure_short=14000, exposure_long=42000
    )
    assert result.rgb.shape == out_s.shape


def test_assemble_expose_base_false_skips_makeup_hooks():
    from pyopticfilm.scan.geometry import compute_geometry
    from pyopticfilm.scan.pipeline import ImagePipeline

    pipe = ImagePipeline(MODEL_8200I_SE)
    geometry = compute_geometry(1800, model=MODEL_8200I_SE)
    rgb = np.full((32, 32, 3), 10000, dtype=np.uint16)
    seen: list[str] = []
    pipe.decode_rgb = lambda *_a, **_k: rgb  # type: ignore[method-assign]
    pipe.reduce_y_oversample = lambda a, _g: a  # type: ignore[method-assign]
    pipe.apply_line_shifts = lambda a, _g: a  # type: ignore[method-assign]
    pipe.apply_y_stagger = lambda a, _g: a  # type: ignore[method-assign]
    pipe.apply_host_downsample = lambda a, _g: a  # type: ignore[method-assign]
    pipe.expose_film_base = lambda a, **_kw: (seen.append("expose") or a)  # type: ignore[method-assign]
    pipe.clamp_border_highlights = lambda a, **_kw: (seen.append("clamp") or a)  # type: ignore[method-assign]

    pipe.assemble(b"", geometry, dark=None, white=None, expose_base=False)
    assert seen == []
    pipe.assemble(b"", geometry, dark=None, white=None, expose_base=True)
    assert seen == ["expose", "clamp"]


def test_merge_snr_prefers_short_when_long_clipped():
    short = np.full((4, 4, 3), 50000, dtype=np.uint16)
    long = np.full((4, 4, 3), 65000, dtype=np.uint16)
    result = merge_exposures_result(
        short, long, exposure_short=14000, exposure_long=42000
    )
    assert np.allclose(result.rgb, short, atol=1)
    assert result.fusion_stats is not None
    assert result.fusion_stats.mean_long_weight < result.fusion_stats.mean_short_weight
    assert result.fusion_stats.zero_weight_pixels == 0
    assert result.fusion_stats.mean_residual_confidence is not None


def test_merge_snr_midtones_favor_long():
    """After normalization, long has lower variance → higher IVW weight."""
    short = np.full((8, 8, 3), 8000, dtype=np.uint16)
    # Underlying X≈8000 on short scale → long raw ≈ 24000 at 3×
    long = np.full((8, 8, 3), 24000, dtype=np.uint16)
    result = merge_exposures_result(
        short, long, exposure_short=14000, exposure_long=42000
    )
    assert result.fusion_stats is not None
    assert result.fusion_stats.mean_long_weight > result.fusion_stats.mean_short_weight
    assert result.fusion_stats.zero_weight_pixels == 0
    # Fused near the common radiometric level
    assert abs(float(result.rgb.mean()) - 8000.0) < 50.0


def test_merge_snr_both_zero_stays_black():
    short = np.full((4, 4, 3), 50, dtype=np.uint16)
    long = np.full((4, 4, 3), 65000, dtype=np.uint16)
    result = merge_exposures_result(short, long)
    assert np.all(result.rgb == 0)
    assert result.fusion_stats is not None
    assert result.fusion_stats.zero_weight_fraction == 1.0


def test_merge_snr_scale_mismatch_does_not_black_out():
    """USB ratio ≠ effective gain: image-fit ratio; residual must not zero the frame."""
    short = np.full((32, 32, 3), 8000, dtype=np.uint16)
    # True 3× would be 24000; 30000 is a systematic scale error → fitted r=3.75.
    long = np.full((32, 32, 3), 30000, dtype=np.uint16)
    result = merge_exposures_result(
        short, long, exposure_short=14000, exposure_long=42000
    )
    assert result.fusion_stats is not None
    assert result.fusion_stats.zero_weight_fraction == 0.0
    assert result.fusion_stats.exposure_ratio_used is not None
    assert abs(result.fusion_stats.exposure_ratio_used - 3.75) < 0.05
    assert abs(float(result.rgb.mean()) - 8000.0) < 50.0
    assert result.fusion_stats.mean_long_weight > result.fusion_stats.mean_short_weight


def test_merge_snr_uses_long_in_dense_film():
    """Dense (dark) short + brighter long → image-fit ratio, long dominates IVW."""
    short = np.full((32, 32, 3), 1500, dtype=np.uint16)
    long = np.full((32, 32, 3), 9000, dtype=np.uint16)  # effective r≈6
    result = merge_exposures_result(
        short, long, exposure_short=14000, exposure_long=42000
    )
    assert result.fusion_stats is not None
    assert result.fusion_stats.exposure_ratio_used is not None
    assert abs(result.fusion_stats.exposure_ratio_used - 6.0) < 0.1
    assert result.fusion_stats.mean_long_weight > result.fusion_stats.mean_short_weight
    assert abs(float(result.rgb.mean()) - 1500.0) < 80.0


def test_merge_snr_differs_from_short_when_long_adds_signal():
    """Half-frame: short clipped-low in one region that long recovers."""
    short = np.full((32, 32, 3), 8000, dtype=np.uint16)
    long = np.full((32, 32, 3), 24000, dtype=np.uint16)
    # Dense corner: short near floor, long has recoverable signal at 3×.
    short[:16, :16, :] = 400
    long[:16, :16, :] = 6000  # → ~2000 on short scale after r=3
    result = merge_exposures_result(
        short, long, exposure_short=14000, exposure_long=42000, align_shift=(0, 0)
    )
    # Dense corner should be brighter than short's 400 (long contributes).
    assert float(result.rgb[:16, :16].mean()) > 800.0
    assert not np.allclose(result.rgb, short, atol=50)


def test_merge_guard_limits_channel_split_at_shifted_edge():
    """Misregistered long at an edge should not split R/G/B after IVW."""
    h, w = 64, 64
    level_lo = 8000
    level_hi = 15000
    short = np.full((h, w, 3), level_lo, dtype=np.uint16)
    long = np.full((h, w, 3), level_lo * 3, dtype=np.uint16)
    short[: h // 2, :, :] = level_hi
    long[: h // 2, :, :] = level_hi * 3
    long_shifted = np.roll(long, 2, axis=0)

    result = merge_exposures_result(
        short,
        long_shifted,
        exposure_short=14000,
        exposure_long=42000,
        align_shift=(0.0, 0.0),
    )
    row = h // 2
    px = result.rgb[row, w // 2].astype(np.float64)
    spread = float(px.max() - px.min())
    assert spread < 400.0


def test_merge_exposures_large_shape_chunked():
    """Regression: 3600 dpi-class frames must not need full-frame float32 planes."""
    h, w = 3603, 5184
    short = np.full((h, w, 3), 8000, dtype=np.uint16)
    long = np.full((h, w, 3), 24000, dtype=np.uint16)
    result = merge_exposures_result(
        short, long, exposure_short=14000, exposure_long=42000, align_shift=(0, 0)
    )
    assert result.rgb.shape == (h, w, 3)
    assert result.rgb.dtype == np.uint16
    assert result.fusion_stats is not None
    assert abs(float(result.rgb.mean()) - 8000.0) < 50.0


def test_merge_snr_reduces_noise_vs_short_only():
    """Synthetic PG noise: fused frame closer to truth than noisy short alone."""
    rng = np.random.default_rng(42)
    truth = np.full((64, 64, 3), 6000.0)
    r = 3.0
    short = np.clip(truth + rng.normal(0, 80, truth.shape), 0, 65535).astype(np.uint16)
    long_raw = np.clip(truth * r + rng.normal(0, 80, truth.shape), 0, 65535).astype(np.uint16)
    fused = merge_exposures(
        short,
        long_raw,
        exposure_short=14000,
        exposure_long=42000,
        align_shift=(0, 0),
    )
    err_short = float(np.mean((short.astype(np.float64) - truth) ** 2))
    err_fused = float(np.mean((fused.astype(np.float64) - truth) ** 2))
    assert err_fused < err_short * 0.85


def test_merge_snr_per_channel_clip_pulls_r_from_short():
    """Only R clipped on long → merged R near short; G/B still use long."""
    short = np.full((32, 32, 3), 20000, dtype=np.uint16)
    long = np.clip(short.astype(np.int32) * 3, 0, 65535).astype(np.uint16)
    long[:, :, 0] = 65535
    result = merge_exposures_result(
        short, long, exposure_short=14000, exposure_long=42000, align_shift=(0, 0)
    )
    # Crushed long/r for R would be ~21845; short R is 20000 — prefer short.
    assert abs(float(result.rgb[:, :, 0].mean()) - 20000.0) < 500.0
    # G still near short-scale long (20000).
    assert abs(float(result.rgb[:, :, 1].mean()) - 20000.0) < 200.0


def test_estimate_pg_noise_params_from_synthetic_flats():
    from pyopticfilm.scan.exposure_merge import estimate_pg_noise_params

    rng = np.random.default_rng(0)
    flats = []
    for mean in (2000.0, 8000.0, 20000.0, 35000.0):
        # var = 1.5*mean + 2500
        std = np.sqrt(1.5 * mean + 2500.0)
        flats.append(
            np.clip(mean + rng.normal(0, std, (128, 128, 3)), 0, 65535).astype(np.uint16)
        )
    alpha, beta = estimate_pg_noise_params(flats, patch=32)
    assert 0.3 < alpha < 3.0
    assert 500.0 < beta < 8000.0


def test_expose_film_base_preserve_headroom_caps_gain():
    from pyopticfilm.scan.pipeline import (
        HOST_CALIB_HIGHLIGHT_CEILING,
        ImagePipeline,
    )

    pipe = ImagePipeline(MODEL_8200I_SE)
    # Peak p99.7 low enough to trigger makeup, but p99.9 already near ceiling.
    rgb = np.full((64, 64, 3), 20000, dtype=np.uint16)
    rgb[20:44, 20:44, :] = 50000  # bright patch → high p99.9
    out = pipe.expose_film_base(
        rgb, source="test", preserve_headroom=True
    )
    hi = float(np.percentile(out, 99.9))
    # Without headroom, gain≈61440/50000≈1.23 → hi≈61500; with cap stay ≤ ceiling+tol.
    assert hi <= HOST_CALIB_HIGHLIGHT_CEILING + 50


def test_align_pass_subpixel_shift_when_opencv_available():
    try:
        import cv2  # noqa: F401
    except ImportError:
        return
    from pyopticfilm.pass_align import align_pass_to_reference, estimate_pass_shift

    rng = np.random.default_rng(1)
    base = rng.integers(1000, 20000, (96, 96, 3), dtype=np.uint16)
    # Apply known shift to moving; estimate should recover it.
    shifted, _ = align_pass_to_reference(base, base, shift=(3.0, -2.0))
    dx, dy = estimate_pass_shift(base, shifted)
    assert abs(dx - 3.0) < 0.6
    assert abs(dy - (-2.0)) < 0.6


def test_align_pass_tall_crop_accepts_large_dy_within_height_guard():
    """A tall/narrow crop window (e.g. a multi-frame strip scan) can have a
    real, correctable dy that is small relative to frame height but large
    relative to frame width — the guard must judge each axis against its
    own dimension, not reject a real height-scale shift using a
    width-derived threshold (regression for the 1096x6700 ghosting seen on
    real 8100 V2 hardware, where a real dy=-195.81 was ~9x a width-only
    guard but only ~3% of the frame's height)."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        return
    from pyopticfilm.pass_align import align_pass_to_reference, estimate_pass_shift

    rng = np.random.default_rng(2)
    # Narrow, tall crop window, same aspect ratio ballpark as the repro
    # (1096x6700). Height-derived guard = max(16, 0.02*1340) = 26.8;
    # width-derived guard (the pre-fix behavior) = max(16, 0.02*220) = 16.
    base = rng.integers(1000, 20000, (1340, 220, 3), dtype=np.uint16)
    # dy=20 clears the height guard (26.8) but would be rejected by a
    # width-only guard (16) — the bug this test guards against.
    shifted, _ = align_pass_to_reference(base, base, shift=(0.0, 20.0))
    _dx, dy = estimate_pass_shift(base, shifted)
    assert abs(dy - 20.0) < 1.0, f"real height-scale dy was rejected: got dy={dy}"


def test_align_pass_to_reference_banded_recovers_progressive_drift():
    """A tall pass with drift that grows along the feed axis (not a constant
    offset) — the real-hardware shape of the bug: mid-frame content near
    wherever the whole-frame estimate anchored came out sharp, while
    top/bottom (far from it) still ghosted even after applying that single
    global shift. Row-banded alignment should track the profile and leave
    a small residual at both ends, not just in the middle."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        return
    from pyopticfilm.pass_align import align_pass_to_reference_banded

    rng = np.random.default_rng(5)
    h, w = 2400, 300
    # Strong texture throughout so every band has a trustworthy peak.
    base = rng.integers(1000, 40000, (h, w, 3), dtype=np.uint16).astype(np.float64)

    # True per-row drift: linear from 0px (top) to 40px (bottom) — a
    # progressive feed slip, not one rigid shift.
    true_dy = np.linspace(0.0, 40.0, h)
    row_idx = np.clip(np.arange(h) - np.round(true_dy).astype(int), 0, h - 1)
    moving = base[row_idx].astype(np.uint16)
    reference = base.astype(np.uint16)

    warped, (dx, dy_center) = align_pass_to_reference_banded(reference, moving)
    assert abs(dx) < 1.0
    # Profile's midpoint should be ~half the total 40px drift (sign follows
    # this module's existing shift convention, verified by the residual
    # checks below rather than assumed here).
    assert abs(abs(dy_center) - 20.0) < 3.0

    # Compare against the whole-frame rigid alignment on the same pair — it
    # can only fit one number for the entire frame, so it necessarily
    # favors wherever that number happens to be closest to correct (the
    # real-hardware bug: sharp near the anchor, still ghosted far from it).
    whole_frame, _ = align_pass_to_reference(reference, moving)

    def _residual(a, b, y0, y1):
        return np.abs(
            a[y0:y1].astype(np.float64) - b[y0:y1].astype(np.float64)
        ).mean()

    band = 200
    top_whole = _residual(reference, whole_frame, 0, band)
    bottom_whole = _residual(reference, whole_frame, h - band, h)
    top_banded = _residual(reference, warped, 0, band)
    bottom_banded = _residual(reference, warped, h - band, h)

    # Row-banded must beat whole-frame rigid at BOTH extremes. (This
    # particular profile is symmetric around the frame's midpoint, so a
    # single global shift lands close to the average and is similarly
    # wrong at both ends rather than trading one off against the other —
    # banded still tracks the true per-row drift and clearly outperforms.)
    assert top_banded < 0.75 * top_whole, (top_whole, top_banded)
    assert bottom_banded < 0.75 * bottom_whole, (bottom_whole, bottom_banded)


def test_band_shift_profile_refit_excludes_two_outlier_bands(monkeypatch):
    """Two bad bands (not just one) must both be excluded from the fitted
    per-row drift line — dropping only the single worst residual and
    refitting once can leave a second bad band's ~100px error still
    dominating the fit, well past the outlier threshold."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        return
    from pyopticfilm import pass_align

    h, w = 2048, 64
    n_bands = pass_align._ALIGN_BAND_COUNT
    # True drift is 0 everywhere; bands 3 and 6 are corrupted +100px readings
    # (e.g. locked onto low-texture/aliased content) — both must be dropped.
    per_band_dy = [0.0, 0.0, 0.0, 100.0, 0.0, 0.0, 100.0, 0.0]
    assert len(per_band_dy) == n_bands

    call_index = {"i": -1}

    def fake_phase_correlate_shift(ref, mov, *, scale):
        call_index["i"] += 1
        i = call_index["i"] % n_bands
        return (0.0, per_band_dy[i], 1.0)  # response=1.0, always trusted

    monkeypatch.setattr(pass_align, "_phase_correlate_shift", fake_phase_correlate_shift)

    reference = np.zeros((h, w, 3), dtype=np.uint16)
    moving = np.zeros((h, w, 3), dtype=np.uint16)
    result = pass_align._band_shift_profile(reference, moving, n_bands=n_bands)
    assert result is not None
    _, dy_per_row = result
    # A single-drop refit leaves one +100px band in the fit, skewing the
    # line's range across the frame well past the 8px outlier threshold;
    # excluding both should collapse the fit back to ~0 everywhere.
    assert float(np.max(dy_per_row) - np.min(dy_per_row)) < 8.0
