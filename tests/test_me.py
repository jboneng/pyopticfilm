# SPDX-License-Identifier: GPL-3.0-or-later
"""Multi-exposure merge and alignment tests."""

from __future__ import annotations

import numpy as np
import pytest

from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.pass_align import align_pass_to_reference, align_pass_to_reference_banded
from pyopticfilm.scan.exposure_merge import (
    merge_exposures,
    merge_exposures_result,
    merge_n_exposures,
)


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


def test_align_pass_tall_crop_accepts_large_dy():
    """A tall/narrow crop window (e.g. a multi-frame strip scan) can have a
    real, correctable dy that is small relative to frame height but large
    relative to frame width — regression for the 1096x6700 ghosting seen on
    real 8100 V2 hardware, where a real dy=-195.81 was rejected by an
    axis-blind, magnitude-based guard. The current guard is response-based
    (see test_align_pass_accepts_large_real_hardware_drift below for why
    magnitude alone — even judged per-axis — still wasn't enough), so this
    also stands as a same-shape check on a narrow/tall aspect ratio
    specifically."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        return
    from pyopticfilm.pass_align import align_pass_to_reference, estimate_pass_shift

    rng = np.random.default_rng(2)
    base = rng.integers(1000, 20000, (1340, 220, 3), dtype=np.uint16)
    shifted, _ = align_pass_to_reference(base, base, shift=(0.0, 20.0))
    _dx, dy = estimate_pass_shift(base, shifted)
    assert abs(dy - 20.0) < 1.0, f"real height-scale dy was rejected: got dy={dy}"


def test_align_pass_accepts_large_real_hardware_drift():
    """Regression for the second round of ghosting: a completely ordinary
    1712x1201 full-frame 1200dpi scan (not a tall crop) on real 8100 V2
    hardware measured a real ~30px drift that a magnitude-based guard
    rejected even after the per-axis fix above (guard_y = max(16,
    0.02*1201) = 24.02, still below the real ~30px shift). #33's own
    benchmark independently measured real drift up to ~42px on this
    hardware, so any magnitude-based cutoff scaled off frame dimensions
    fights real behavior. The guard is now response-based (trusts the
    phase-correlation peak's own sharpness, not the shift's size) —
    verify a large, but well-correlated, shift on an ordinary-aspect frame
    is no longer rejected."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        return
    from pyopticfilm.pass_align import align_pass_to_reference, estimate_pass_shift

    rng = np.random.default_rng(6)
    base = rng.integers(1000, 20000, (1201, 1712, 3), dtype=np.uint16)
    shifted, _ = align_pass_to_reference(base, base, shift=(-0.09, -29.98))
    dx, dy = estimate_pass_shift(base, shifted)
    # Sign follows this module's existing convention (see other tests in
    # this file) — what matters here is that the real ~30px magnitude
    # survives instead of being rejected down to (0, 0).
    assert abs(dx) < 1.0
    assert abs(abs(dy) - 29.98) < 1.0, f"real large drift was rejected: got dy={dy}"


def test_align_pass_rejects_bogus_lock_on_uncorrelated_content():
    """The response-based guard must reject an untrustworthy peak — the
    exact failure mode a magnitude-only guard can't catch at all (a bogus
    shift can be any size, including one that looks plausible) and the
    reason jboneng/pyopticfilm#14 saw an occasional bogus lock on
    low-texture content. Two fully independent noise frames have nothing
    real to correlate on; phase correlation still reports *some* shift
    (there's always a max in the cross-power spectrum), but with a weak,
    unreliable response — verified directly below — that must be rejected
    regardless of how large or small the reported shift happens to be."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        return
    from pyopticfilm.pass_align import (
        _ALIGN_MIN_RESPONSE,
        _luminance_plane,
        _phase_correlate_shift,
        estimate_pass_shift,
    )

    rng = np.random.default_rng(9)
    ref = rng.integers(0, 65535, (256, 256, 3), dtype=np.uint32).astype(np.uint16)
    mov = rng.integers(0, 65535, (256, 256, 3), dtype=np.uint32).astype(np.uint16)

    _dx, _dy, resp = _phase_correlate_shift(
        _luminance_plane(ref), _luminance_plane(mov), scale=1.0
    )
    assert resp < _ALIGN_MIN_RESPONSE, f"test setup didn't reproduce a weak peak: resp={resp}"

    dx, dy = estimate_pass_shift(ref, mov)
    assert dx == 0.0 and dy == 0.0, (
        f"uncorrelated content's bogus lock should be rejected, got ({dx}, {dy})"
    )


# --- N-bracket merge (merge_n_exposures) ----------------------------------


def test_merge_n_exposures_two_frames_matches_pairwise_ivw():
    """N=2 must reduce exactly to the pairwise merge's confidence/IVW
    arithmetic (there is exactly one non-reference bracket at N=2, so
    merge_n_exposures's per-bracket loop collapses to _merge_snr_rows's
    wa/wb/ivw/c_res_eff/prefer terms directly). The misalignment *fallback*
    is deliberately NOT required to match: merge_n_exposures uses a
    luma-only gate (see its docstring) while _merge_snr_rows still ANDs in
    a cross-channel-spread check, kept as-is for the n_brackets==2
    production path's byte-identical guarantee. Neither of this test's
    synthetic scenes exercises that specific divergence (no real
    misregistration is introduced here), so the two still agree below.

    Not bit-for-bit against merge_exposures_result: that function additionally
    estimates the *actual* image-fit exposure ratio via
    _estimate_exposure_ratio rather than trusting the nominal
    exposure_long/exposure_short, a difference that predates this feature and
    is out of scope here. Both use exp_long/exp_short as the nominal ratio's
    numerator/denominator, so exposure_ratio_used matches; rgb only matches
    when the data-fit ratio equals the nominal one (verified separately by
    the ratio-uniform frames below)."""
    rng = np.random.default_rng(7)
    short = rng.integers(500, 30000, size=(48, 64, 3), dtype=np.uint16)
    long = rng.integers(2000, 60000, size=(48, 64, 3), dtype=np.uint16)
    result = merge_n_exposures([short, long], [14000, 42000])
    assert result.rgb.shape == short.shape
    assert result.rgb.dtype == np.uint16
    assert result.fusion_stats is not None
    assert result.fusion_stats.exposure_ratio_used == 3.0

    # Uniform-ratio frames: the data-fit ratio in merge_exposures_result
    # converges to the nominal ratio, so the two paths must agree exactly.
    truth = rng.uniform(2000.0, 10000.0, size=(48, 64, 1)) * np.ones((1, 1, 3))
    short_u = np.clip(truth, 0, 65535).astype(np.uint16)
    long_u = np.clip(truth * 3.0, 0, 65535).astype(np.uint16)
    result_u = merge_n_exposures([short_u, long_u], [14000, 42000])
    pairwise_u = merge_exposures_result(
        short_u, long_u, exposure_short=14000, exposure_long=42000, align_shift=(0.0, 0.0)
    )
    # Data-fit ratio in merge_exposures_result is ~3.0 but not exactly (float
    # fit noise), so allow a tiny per-pixel tolerance rather than bit-exact.
    assert np.max(
        np.abs(result_u.rgb.astype(np.int32) - pairwise_u.rgb.astype(np.int32))
    ) <= 2
    assert result_u.fusion_stats is not None
    assert pairwise_u.fusion_stats is not None
    assert result_u.fusion_stats.mean_residual_confidence == pytest.approx(
        pairwise_u.fusion_stats.mean_residual_confidence, abs=1e-4
    )


def test_merge_n_exposures_misalign_gate_is_luma_only_not_chroma_spread():
    """Regression for the ghosting seen on real 8100 V2 hardware (skin/cream
    fabric ghosted while the high-chroma striped strap stayed sharp).

    Two real-hardware-shaped failure modes, both wrong, ruled out here:

    - AND-ing in cross-channel spread (the original 2-way gate's shape)
      misses genuine misregistration on neutral/flat-toned content: a real
      luma disagreement there doesn't move R/G/B apart from each other, so
      the AND never fires and the ghost blends straight through.
    - OR-ing spread in instead (rather than dropping it) is worse: any
      well-aligned saturated color patch has max(R,G,B)-min(R,G,B) far
      above the spread threshold from real scene color alone, so OR-gating
      makes *every* pixel fall back to frames[0] — no fusion at all, even
      with zero real misalignment.

    Luma-only avoids both: near-untouched fusion on a well-aligned,
    saturated-color scene, and a near-total fallback-to-frames[0] on a
    genuinely drifted neutral gradient (protecting it from ghosting)."""
    rng = np.random.default_rng(3)
    h, w = 64, 64

    # Well-aligned, saturated RGB patches — must NOT collapse to frames[0].
    truth = np.zeros((h, w, 3), dtype=np.float64)
    truth[:, : w // 3] = [40000, 4000, 4000]
    truth[:, w // 3 : 2 * w // 3] = [4000, 40000, 4000]
    truth[:, 2 * w // 3 :] = [4000, 4000, 40000]
    truth += rng.normal(0, 200, truth.shape)
    short = np.clip(truth / 3.0, 0, 65535).astype(np.uint16)
    long = np.clip(truth, 0, 65535).astype(np.uint16)
    colorful_result = merge_n_exposures([short, long], [14000, 42000])
    assert colorful_result.fusion_stats is not None
    assert colorful_result.fusion_stats.mean_residual_confidence > 0.9, (
        "well-aligned saturated color incorrectly triggered the misalignment fallback"
    )

    # Genuinely drifted (5px), low-chroma (near-neutral) gradient — MUST
    # fall back to frames[0] rather than ghost.
    grad = np.linspace(5000, 50000, h)[:, None, None] * np.ones((1, w, 3))
    grad += rng.normal(0, 100, grad.shape)
    short_n = np.clip(grad / 3.0, 0, 65535).astype(np.uint16)
    long_n = np.roll(np.clip(grad, 0, 65535).astype(np.uint16), 5, axis=0)
    neutral_result = merge_n_exposures([short_n, long_n], [14000, 42000])
    diff_from_short = np.abs(
        neutral_result.rgb.astype(np.float64) - short_n.astype(np.float64)
    ).mean()
    assert diff_from_short < 5.0, (
        f"drifted neutral content was not protected by the misalignment fallback "
        f"(mean diff from frames[0]: {diff_from_short})"
    )


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


def test_merge_n_exposures_validates_frame_count():
    frame = np.zeros((4, 4, 3), dtype=np.uint16)
    with pytest.raises(ValueError):
        merge_n_exposures([frame], [14000])


def test_merge_n_exposures_validates_length_mismatch():
    frame = np.zeros((4, 4, 3), dtype=np.uint16)
    with pytest.raises(ValueError):
        merge_n_exposures([frame, frame], [14000, 20000, 42000])


def test_merge_n_exposures_validates_shape_mismatch():
    a = np.zeros((4, 4, 3), dtype=np.uint16)
    b = np.zeros((5, 5, 3), dtype=np.uint16)
    with pytest.raises(ValueError):
        merge_n_exposures([a, b], [14000, 42000])


def test_merge_n_exposures_validates_nonpositive_exposure():
    frame = np.zeros((4, 4, 3), dtype=np.uint16)
    with pytest.raises(ValueError):
        merge_n_exposures([frame, frame], [14000, 0])


def test_merge_n_exposures_guard_limits_channel_split_at_shifted_bracket():
    """N-bracket generalization of test_merge_guard_limits_channel_split_at_shifted_edge:
    one misregistered non-reference bracket among several must not split
    R/G/B via IVW at the disagreement edge."""
    h, w = 64, 64
    level_lo = 8000
    level_hi = 15000
    schedule = [14000, 24249, 42000]
    frames = []
    for e in schedule:
        r = e / schedule[0]
        frame = np.full((h, w, 3), round(level_lo * r), dtype=np.uint16)
        frame[: h // 2, :, :] = round(level_hi * r)
        frames.append(frame)
    # Misregister only the middle bracket at the edge — same style of shift
    # as the pairwise guard test.
    frames[1] = np.roll(frames[1], 2, axis=0)

    result = merge_n_exposures(frames, schedule)
    row = h // 2
    px = result.rgb[row, w // 2].astype(np.float64)
    spread = float(px.max() - px.min())
    assert spread < 400.0


def test_merge_n_exposures_disagreeing_bracket_lowers_mean_residual_confidence():
    """A bracket that disagrees sharply with the reference *in a spatially
    localized way* should pull the worst-case (min) per-pixel confidence
    down vs all brackets agreeing. (A globally uniform disagreement would be
    fully absorbed by the per-bracket bias-correcting z-median and prove
    nothing — the gate only fires on *local* deviation from that median.)"""
    h, w = 32, 32
    schedule = [14000, 24249, 42000]
    rng = np.random.default_rng(3)

    def make_agreeing():
        truth = rng.uniform(2000.0, 10000.0, size=(h, w, 1)) * np.ones((1, 1, 3))
        return [
            np.clip(truth * (e / schedule[0]), 0, 65535).astype(np.uint16) for e in schedule
        ]

    agreeing = make_agreeing()
    disagreeing = [f.copy() for f in agreeing]
    # Bracket 1: one corner patch spikes far off its expected scale, the rest
    # of the frame stays consistent — a local, not global, disagreement.
    disagreeing[1][: h // 4, : w // 4, :] = 60000

    result_agree = merge_n_exposures(agreeing, schedule)
    result_disagree = merge_n_exposures(disagreeing, schedule)
    assert result_agree.fusion_stats is not None
    assert result_disagree.fusion_stats is not None
    assert (
        result_disagree.fusion_stats.mean_residual_confidence
        < result_agree.fusion_stats.mean_residual_confidence
    )


def test_merge_n_exposures_more_brackets_reduces_noise():
    """Synthetic PG noise: 5 brackets should merge closer to truth than 2."""
    rng = np.random.default_rng(11)
    truth = np.full((64, 64, 3), 6000.0)
    schedule2 = [14000, 42000]
    schedule5 = [14000, 18425, 24249, 31913, 42000]

    def make_frames(schedule):
        frames = []
        for e in schedule:
            r = e / schedule[0]
            noisy = np.clip(
                truth * r + rng.normal(0, 80 * np.sqrt(max(r, 1.0)), truth.shape), 0, 65535
            )
            frames.append(noisy.astype(np.uint16))
        return frames

    fused2 = merge_n_exposures(make_frames(schedule2), schedule2).rgb
    fused5 = merge_n_exposures(make_frames(schedule5), schedule5).rgb
    err2 = float(np.mean((fused2.astype(np.float64) - truth) ** 2))
    err5 = float(np.mean((fused5.astype(np.float64) - truth) ** 2))
    assert err5 < err2


def test_merge_n_exposures_large_shape_chunked():
    """Regression: must not need O(full-frame x N) float32 planes at 7200dpi scale."""
    h, w = 3603, 5184
    schedule = [14000, 18425, 24249, 31913, 42000]
    frames = [np.full((h, w, 3), 8000 * (e / schedule[0]), dtype=np.uint16) for e in schedule]
    result = merge_n_exposures(frames, schedule)
    assert result.rgb.shape == (h, w, 3)
    assert result.rgb.dtype == np.uint16
    assert abs(float(result.rgb.mean()) - 8000.0) < 50.0
