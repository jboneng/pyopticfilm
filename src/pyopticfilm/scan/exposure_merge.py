# SPDX-License-Identifier: GPL-3.0-or-later
"""Multi-exposure SNR / IVW merge in linear film-negative scanner space."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from pyopticfilm.pass_align import align_pass_to_reference

_FULL_SCALE = 65535.0

# Soft confidence (fraction of full scale). Clip starts earlier than a hard
# 16-bit rail so CCD knee / pre-sat nonlinearity does not bleed into IVW.
_SNR_FLOOR = 0.002 * _FULL_SCALE
_SNR_CLIP_START = 0.80 * _FULL_SCALE
_SNR_CLIP_END = 0.95 * _FULL_SCALE
# Provisional Poisson-Gaussian DN² model (override via alpha/beta kwargs or
# :func:`estimate_pg_noise_params` from flat fields).
_SNR_ALPHA = 1.0
_SNR_BETA = 4096.0  # ~64 DN read noise
_SNR_Z_LO = 3.0
_SNR_Z_HI = 5.0

# Row bands for IVW merge — avoids several full-frame float32 planes at 3600+ dpi.
_MERGE_CHUNK_ROWS = 128
_STATS_MAX_SIDE = 1024
# Luma disagreement (short scale) above which IVW is suppressed (misregistration guard).
_LUMA_DISAGREE_TAU = 300.0
_IVW_CHANNEL_SPREAD_TAU = 150.0


@dataclass(frozen=True)
class FusionStats:
    """Mean short/long IVW weights and related diagnostics."""

    mean_short_weight: float
    mean_long_weight: float
    zero_weight_pixels: int
    total_pixels: int
    mean_residual_confidence: float | None = None
    exposure_ratio_used: float | None = None

    @property
    def zero_weight_fraction(self) -> float:
        return self.zero_weight_pixels / self.total_pixels if self.total_pixels else 0.0


@dataclass(frozen=True)
class MergeResult:
    rgb: np.ndarray
    fusion_stats: FusionStats | None = None


def estimate_pg_noise_params(
    flats: list[np.ndarray],
    *,
    patch: int = 32,
) -> tuple[float, float]:
    """Estimate Poisson–Gaussian ``α``, ``β`` from flat (or near-flat) frames.

    For each frame, tiles of ``patch×patch`` yield (mean, variance) pairs; a
    robust line fit gives ``var ≈ α·mean + β``. Returns ``(_SNR_ALPHA, _SNR_BETA)``
    if too few samples.
    """
    means: list[float] = []
    vars_: list[float] = []
    for flat in flats:
        arr = np.asarray(flat, dtype=np.float64)
        if arr.ndim == 3:
            arr = arr.mean(axis=2)
        h, w = arr.shape[:2]
        if h < patch or w < patch:
            continue
        for y in range(0, h - patch + 1, patch):
            for x in range(0, w - patch + 1, patch):
                tile = arr[y : y + patch, x : x + patch]
                means.append(float(tile.mean()))
                vars_.append(float(tile.var()))
    if len(means) < 8:
        return _SNR_ALPHA, _SNR_BETA
    m = np.asarray(means, dtype=np.float64)
    v = np.asarray(vars_, dtype=np.float64)
    # Drop empty / saturated tiles.
    ok = (m > 50.0) & (m < 0.9 * _FULL_SCALE) & np.isfinite(v)
    if int(np.count_nonzero(ok)) < 8:
        return _SNR_ALPHA, _SNR_BETA
    m, v = m[ok], v[ok]
    # Least squares: [mean, 1] @ [α, β] = var
    a = np.column_stack([m, np.ones_like(m)])
    coef, _, _, _ = np.linalg.lstsq(a, v, rcond=None)
    alpha = float(max(coef[0], 1e-6))
    beta = float(max(coef[1], 1.0))
    return alpha, beta


def merge_exposures(
    short: np.ndarray,
    long: np.ndarray,
    *,
    exposure_short: int = 14000,
    exposure_long: int = 42000,
    align_shift: tuple[float, float] | tuple[int, int] | None = None,
    alpha: float = _SNR_ALPHA,
    beta: float = _SNR_BETA,
) -> np.ndarray:
    """Fuse short and long RGB negatives into one uint16 frame (SNR / IVW)."""
    return merge_exposures_result(
        short,
        long,
        exposure_short=exposure_short,
        exposure_long=exposure_long,
        align_shift=align_shift,
        alpha=alpha,
        beta=beta,
    ).rgb


def merge_exposures_result(
    short: np.ndarray,
    long: np.ndarray,
    *,
    exposure_short: int = 14000,
    exposure_long: int = 42000,
    align_shift: tuple[float, float] | tuple[int, int] | None = None,
    alpha: float = _SNR_ALPHA,
    beta: float = _SNR_BETA,
) -> MergeResult:
    """Like :func:`merge_exposures` but returns weight stats."""
    short_u = np.asarray(short, dtype=np.uint16)
    long_u = np.asarray(long, dtype=np.uint16)
    if short_u.shape != long_u.shape or short_u.ndim != 3 or short_u.shape[2] != 3:
        raise ValueError(
            f"expected matching HxWx3 arrays, got {short_u.shape} and {long_u.shape}"
        )
    if exposure_long <= 0 or exposure_short <= 0:
        raise ValueError("exposure values must be positive")

    long_u, _ = align_pass_to_reference(short_u, long_u, shift=align_shift)
    usb_ratio = exposure_long / float(exposure_short)

    rgb, stats = _merge_snr(
        short_u,
        long_u,
        usb_ratio=usb_ratio,
        alpha=alpha,
        beta=beta,
        floor=_SNR_FLOOR,
        clip_start=_SNR_CLIP_START,
        clip_end=_SNR_CLIP_END,
    )
    return MergeResult(rgb=rgb, fusion_stats=stats)


def _subsample_for_stats(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Stride down large frames for global ratio / median estimates."""
    ha, wa = a.shape[:2]
    sy = max(1, ha // _STATS_MAX_SIDE)
    sx = max(1, wa // _STATS_MAX_SIDE)
    if sy == 1 and sx == 1:
        return a, b
    return a[::sy, ::sx], b[::sy, ::sx]


def _smoothstep01(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _smooth_confidence(
    raw: np.ndarray, *, floor: float, clip_start: float, clip_end: float
) -> np.ndarray:
    """Noise-floor ramp × soft saturation ramp on raw (unnormalized) DN.

    ``raw`` may be HxW or HxWxC — confidence is computed element-wise.
    """
    floor_w = np.clip((raw - floor) / max(floor, 1e-12), 0.0, 1.0)
    t = (raw - clip_start) / max(clip_end - clip_start, 1e-12)
    clip_w = 1.0 - _smoothstep01(t)
    return floor_w * clip_w


def _residual_confidence(z: np.ndarray, *, z_lo: float = _SNR_Z_LO, z_hi: float = _SNR_Z_HI) -> np.ndarray:
    """1 for |z|<=z_lo, smooth decay to 0 by z_hi."""
    az = np.abs(z)
    conf = np.ones_like(az, dtype=np.float32)
    mid = (az > z_lo) & (az < z_hi)
    conf[az >= z_hi] = 0.0
    conf[mid] = (z_hi - az[mid]) / max(z_hi - z_lo, 1e-12)
    return conf


def _estimate_exposure_ratio(
    a: np.ndarray,
    b_raw: np.ndarray,
    *,
    usb_ratio: float,
    full_scale: float = _FULL_SCALE,
) -> float:
    """Robust median long/short ratio on mid-tone pixels; fall back to USB ratio."""
    lo = 0.01 * full_scale
    hi = 0.85 * full_scale
    valid = (a > lo) & (a < hi) & (b_raw > lo) & (b_raw < hi)
    if int(np.count_nonzero(valid)) >= 1000:
        ratios = b_raw[valid] / np.maximum(a[valid], 1e-12)
        return float(np.median(ratios))
    la = a.mean(axis=2)
    lb = b_raw.mean(axis=2)
    valid2 = (la > lo) & (la < hi) & (lb > lo) & (lb < hi)
    if int(np.count_nonzero(valid2)) < 100:
        return float(usb_ratio)
    ratios = lb[valid2] / np.maximum(la[valid2], 1e-12)
    return float(np.median(ratios))


def _estimate_z_median(
    short_u: np.ndarray,
    long_u: np.ndarray,
    *,
    r: float,
    alpha: float,
    beta: float,
    floor: float,
    clip_start: float,
    clip_end: float,
) -> float:
    """Global residual-gate median on a strided subsample (full frame is OOM at high dpi)."""
    a_s, b_s = _subsample_for_stats(short_u, long_u)
    a = a_s.astype(np.float32)
    b_raw = b_s.astype(np.float32)
    xb = b_raw / r
    lum_xa = a.mean(axis=2)
    lum_xb = xb.mean(axis=2)
    lum_b_raw = b_raw.mean(axis=2)
    va_lum = alpha * np.maximum(lum_xa, 0.0) + beta
    vb_lum = (alpha * np.maximum(lum_b_raw, 0.0) + beta) / (r * r)
    z = (lum_xa - lum_xb) / np.sqrt(np.maximum(va_lum + vb_lum, 1e-12))
    return float(np.median(z))


def _merge_snr_rows(
    short_rows: np.ndarray,
    long_rows: np.ndarray,
    *,
    r: float,
    z_median: float,
    alpha: float,
    beta: float,
    floor: float,
    clip_start: float,
    clip_end: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """IVW merge for one row band; returns uint16 chunk and stat accumulators."""
    a = short_rows.astype(np.float32)
    b_raw = long_rows.astype(np.float32)
    xa = a
    xb = b_raw / r

    ca = _smooth_confidence(a, floor=floor, clip_start=clip_start, clip_end=clip_end)
    cb = _smooth_confidence(b_raw, floor=floor, clip_start=clip_start, clip_end=clip_end)

    va = alpha * np.maximum(xa, 0.0) + beta
    vb = (alpha * np.maximum(b_raw, 0.0) + beta) / (r * r)

    lum_xa = xa.mean(axis=2)
    lum_xb = xb.mean(axis=2)
    lum_b_raw = b_raw.mean(axis=2)
    va_lum = alpha * np.maximum(lum_xa, 0.0) + beta
    vb_lum = (alpha * np.maximum(lum_b_raw, 0.0) + beta) / (r * r)
    z = (lum_xa - lum_xb) / np.sqrt(np.maximum(va_lum + vb_lum, 1e-12))
    z_local = z - z_median
    c_res = _residual_confidence(z_local)
    gate = np.minimum(ca, cb).mean(axis=2)
    c_res_eff = 1.0 - gate * (1.0 - c_res)

    wa = ca / np.maximum(va, 1e-12)
    wb = cb / np.maximum(vb, 1e-12)
    denom = wa + wb
    both_zero = (ca + cb) <= 1e-6
    ivw = (wa * xa + wb * xb) / np.maximum(denom, 1e-12)

    prefer = np.where(wa >= wb, xa, xb)
    merged = c_res_eff[..., np.newaxis] * ivw + (1.0 - c_res_eff[..., np.newaxis]) * prefer
    # Misregistered edges: per-channel IVW causes R/G/B fringes; fall back to short scale
    # only when luma AND channel spread both disagree (dense shadow keeps long recovery).
    lum_diff = np.abs(lum_xa - lum_xb)
    ivw_spread = np.max(ivw, axis=2) - np.min(ivw, axis=2)
    misaligned = (lum_diff > _LUMA_DISAGREE_TAU) & (ivw_spread > _IVW_CHANNEL_SPREAD_TAU)
    out = np.where(misaligned[..., np.newaxis], xa, merged)
    out = np.where(both_zero, 0.0, out)

    both_zero_pix = np.all(both_zero, axis=2)
    chunk = np.clip(out, 0, 65535).astype(np.uint16)
    return chunk, wa, wb, both_zero_pix, c_res_eff


def _merge_snr(
    short_u: np.ndarray,
    long_u: np.ndarray,
    *,
    usb_ratio: float,
    alpha: float,
    beta: float,
    floor: float,
    clip_start: float,
    clip_end: float,
) -> tuple[np.ndarray, FusionStats]:
    """Clipping-aware inverse-variance fusion in short-exposure radiometric scale.

    Processed in row bands so peak memory stays O(chunk) not O(full frame float32).
    """
    a_sub, b_sub = _subsample_for_stats(short_u, long_u)
    r = max(
        _estimate_exposure_ratio(
            a_sub.astype(np.float32),
            b_sub.astype(np.float32),
            usb_ratio=usb_ratio,
        ),
        1e-12,
    )
    z_median = _estimate_z_median(
        short_u,
        long_u,
        r=r,
        alpha=alpha,
        beta=beta,
        floor=floor,
        clip_start=clip_start,
        clip_end=clip_end,
    )

    h = short_u.shape[0]
    out = np.empty_like(short_u)
    wa_sum = 0.0
    wb_sum = 0.0
    n_weights = 0
    zero_count = 0
    c_res_sum = 0.0
    total_pixels = int(short_u.shape[0] * short_u.shape[1])

    for y0 in range(0, h, _MERGE_CHUNK_ROWS):
        y1 = min(h, y0 + _MERGE_CHUNK_ROWS)
        chunk, wa, wb, both_zero_pix, c_res_eff = _merge_snr_rows(
            short_u[y0:y1],
            long_u[y0:y1],
            r=r,
            z_median=z_median,
            alpha=alpha,
            beta=beta,
            floor=floor,
            clip_start=clip_start,
            clip_end=clip_end,
        )
        out[y0:y1] = chunk
        wa_sum += float(wa.sum())
        wb_sum += float(wb.sum())
        n_weights += int(wa.size)
        zero_count += int(np.count_nonzero(both_zero_pix))
        c_res_sum += float(c_res_eff.sum())

    stats = FusionStats(
        mean_short_weight=wa_sum / max(n_weights, 1),
        mean_long_weight=wb_sum / max(n_weights, 1),
        zero_weight_pixels=zero_count,
        total_pixels=total_pixels,
        mean_residual_confidence=c_res_sum / max(total_pixels, 1),
        exposure_ratio_used=float(r),
    )
    return out, stats


def _subsample_for_stats_n(frames: list[np.ndarray]) -> list[np.ndarray]:
    """N-frame generalization of :func:`_subsample_for_stats` (stride shared
    across all frames, derived from ``frames[0]``'s shape)."""
    h, w = frames[0].shape[:2]
    sy = max(1, h // _STATS_MAX_SIDE)
    sx = max(1, w // _STATS_MAX_SIDE)
    if sy == 1 and sx == 1:
        return frames
    return [f[::sy, ::sx] for f in frames]


def _estimate_z_median_n(
    frames: list[np.ndarray],
    exposures: list[int],
    *,
    alpha: float,
    beta: float,
) -> list[float]:
    """Per-bracket global residual-gate median vs ``frames[0]``, generalizing
    :func:`_estimate_z_median` to N brackets (one median per non-reference
    bracket, same subsampled-luma z statistic)."""
    subs = _subsample_for_stats_n(frames)
    lum_ref = subs[0].astype(np.float32).mean(axis=2)
    va_lum = alpha * np.maximum(lum_ref, 0.0) + beta
    e0 = float(exposures[0])
    medians: list[float] = []
    for raw, e in zip(subs[1:], exposures[1:], strict=True):
        raw_f = raw.astype(np.float32)
        r = float(e) / e0
        lum_raw = raw_f.mean(axis=2)
        xb_lum = lum_raw / r
        vb_lum = (alpha * np.maximum(lum_raw, 0.0) + beta) / (r * r)
        z = (lum_ref - xb_lum) / np.sqrt(np.maximum(va_lum + vb_lum, 1e-12))
        medians.append(float(np.median(z)))
    return medians


def merge_n_exposures(
    frames: list[np.ndarray],
    exposures: list[int],
    *,
    alpha: float = _SNR_ALPHA,
    beta: float = _SNR_BETA,
) -> MergeResult:
    """N-bracket generalization of :func:`merge_exposures_result`'s IVW fusion.

    Reduces exactly to the pairwise formula at ``len(frames) == 2`` — the
    per-pixel weight ``w_i = c_i / v_i`` and merged value
    ``sum(w_i * x_i) / sum(w_i)`` are algebraically identical to
    :func:`_merge_snr_rows`'s ``wa``/``wb``/``ivw`` when there are only two
    brackets, since bracket 0 is always the reference scale (``r_0 = 1``).
    The residual-disagreement gate and misalignment fallback below are the
    same generalization: at N=2 there is exactly one non-reference bracket,
    so ``c_res_eff``/``prefer``/``misaligned`` collapse to
    :func:`_merge_snr_rows`'s formulas exactly.

    Residual-disagreement gate: for each non-reference bracket ``i``, a
    z-score of its luma vs ``frames[0]`` (bias-corrected by a global,
    subsampled per-bracket median — see :func:`_estimate_z_median_n`) yields
    a per-pixel confidence ``c_res_i``. The pixel's overall confidence is the
    *worst* (minimum) across all brackets — one bracket disagreeing sharply
    with the reference is enough to distrust the pure IVW blend there. Where
    confidence is low, the output blends toward ``prefer``: the single
    bracket with the highest individual weight at that pixel (the N-way
    equivalent of the pairwise "pick short or long, whichever is more
    trusted" fallback).

    Misalignment edge fallback: when the worst per-bracket luma disagreement
    exceeds its threshold, the pixel falls back to ``frames[0]`` verbatim.
    Deliberately luma-only — no cross-channel-spread requirement, unlike the
    2-way merge's ``misaligned`` gate (which ANDs in a spread check — see
    :func:`_merge_snr_rows`, kept as-is to preserve the ``n_brackets == 2``
    production path's byte-identical guarantee). Measured empirically on
    both directions of this tradeoff:

    - Requiring cross-channel spread too (the 2-way gate's AND) misses real
      Y-axis drift on flat/neutral-toned content (skin, cream fabric — see
      jboneng/pyopticfilm#33 for independently measured Y-only drift on
      this hardware): misregistered-but-neutral pixels disagree badly in
      luminance but stay close to grey either way, so the AND never fires
      and the ghosting blends straight through — the original real-hardware
      bug this function's fallback is meant to catch.
    - OR-ing spread in instead (rather than dropping it) is *worse*: any
      saturated, well-aligned color patch has ``max(R,G,B)-min(R,G,B)``
      far above ``_IVW_CHANNEL_SPREAD_TAU`` from real scene color alone —
      verified on a synthetic well-aligned RGB-patch scene, where OR-gating
      made every pixel fall back to ``frames[0]``, i.e. no fusion at all.
    - Luma disagreement alone reproduced neither failure in the same
      checks: ~0% false-trigger on a well-aligned saturated-color scene and
      a well-aligned sharp achromatic edge, ~100% correct-trigger on a
      genuinely 5px-drifted neutral gradient.

    So N=2 through this function is *not* bit-for-bit against
    :func:`_merge_snr_rows` on frames where the dropped spread condition
    would have mattered — the N=2 equivalence documented above is, in
    general, only for the confidence/IVW arithmetic itself (see
    ``test_merge_n_exposures_two_frames_matches_pairwise_ivw``).

    Frames must already be pairwise-aligned to ``frames[0]`` by the caller
    (e.g. via repeated :func:`pyopticfilm.pass_align.align_pass_to_reference`
    calls) — this function does no alignment of its own.

    Args:
        frames: N uint16 HxWx3 arrays, ascending exposure order, already
            aligned to ``frames[0]``.
        exposures: N positive exposure values, same order as ``frames``.

    Returns:
        MergeResult with the fused uint16 HxWx3 array and FusionStats
        (``mean_short_weight``/``mean_long_weight`` report bracket 0 / the
        last bracket specifically, for compatibility with the 2-way stats
        shape; ``exposure_ratio_used`` is ``exposures[-1] / exposures[0]``;
        ``mean_residual_confidence`` is the mean of the per-pixel overall
        ``c_res_eff`` across the frame, same meaning as the 2-way stat).

    Raises:
        ValueError: fewer than 2 frames, mismatched lengths/shapes, or a
            non-positive exposure.
    """
    if len(frames) != len(exposures):
        raise ValueError(
            f"frames ({len(frames)}) and exposures ({len(exposures)}) length mismatch"
        )
    if len(frames) < 2:
        raise ValueError(f"merge_n_exposures needs >= 2 frames, got {len(frames)}")
    if any(e <= 0 for e in exposures):
        raise ValueError(f"all exposures must be positive, got {exposures}")
    ref = np.asarray(frames[0], dtype=np.uint16)
    if ref.ndim != 3 or ref.shape[2] != 3:
        raise ValueError(f"expected HxWx3 arrays, got {ref.shape}")
    for i, f in enumerate(frames[1:], 1):
        fa = np.asarray(f, dtype=np.uint16)
        if fa.shape != ref.shape:
            raise ValueError(
                f"frame {i} shape {fa.shape} does not match frame 0 shape {ref.shape}"
            )

    e0 = float(exposures[0])
    ratios = [float(e) / e0 for e in exposures]
    h, w = ref.shape[:2]
    out = np.empty((h, w, 3), dtype=np.uint16)
    w0_sum = 0.0
    wn_sum = 0.0
    n_weights = 0
    zero_count = 0
    c_res_sum = 0.0
    total_pixels = int(h * w)

    z_medians = _estimate_z_median_n(frames, exposures, alpha=alpha, beta=beta)

    for y0 in range(0, h, _MERGE_CHUNK_ROWS):
        y1 = min(h, y0 + _MERGE_CHUNK_ROWS)
        raw_fs: list[np.ndarray] = []
        xs: list[np.ndarray] = []
        cs: list[np.ndarray] = []
        weights: list[np.ndarray] = []
        for raw, r in zip(frames, ratios, strict=True):
            raw_f = np.asarray(raw[y0:y1], dtype=np.float32)
            x = raw_f / r
            c = _smooth_confidence(
                raw_f, floor=_SNR_FLOOR, clip_start=_SNR_CLIP_START, clip_end=_SNR_CLIP_END
            )
            v = (alpha * np.maximum(raw_f, 0.0) + beta) / (r * r)
            weight = c / np.maximum(v, 1e-12)
            raw_fs.append(raw_f)
            xs.append(x)
            cs.append(c)
            weights.append(weight)

        acc = weights[0] * xs[0]
        w_sum = weights[0].copy()
        all_zero_conf = cs[0] <= 1e-6
        for x, c, weight in zip(xs[1:], cs[1:], weights[1:], strict=True):
            acc += weight * x
            w_sum += weight
            all_zero_conf &= c <= 1e-6
        ivw = acc / np.maximum(w_sum, 1e-12)

        lum_ref = xs[0].mean(axis=2)
        va_lum = alpha * np.maximum(lum_ref, 0.0) + beta
        c_res_eff = np.ones_like(lum_ref, dtype=np.float32)
        lum_diff_max = np.zeros_like(lum_ref, dtype=np.float32)
        for raw_f, x, c, r, z_median in zip(
            raw_fs[1:], xs[1:], cs[1:], ratios[1:], z_medians, strict=True
        ):
            lum_raw = raw_f.mean(axis=2)
            lum_x = x.mean(axis=2)
            vb_lum = (alpha * np.maximum(lum_raw, 0.0) + beta) / (r * r)
            z = (lum_ref - lum_x) / np.sqrt(np.maximum(va_lum + vb_lum, 1e-12))
            z_local = z - z_median
            c_res_i = _residual_confidence(z_local)
            gate_i = np.minimum(cs[0], c).mean(axis=2)
            c_res_eff_i = 1.0 - gate_i * (1.0 - c_res_i)
            c_res_eff = np.minimum(c_res_eff, c_res_eff_i)
            lum_diff_max = np.maximum(lum_diff_max, np.abs(lum_ref - lum_x))

        weight_stack = np.stack(weights, axis=0)
        x_stack = np.stack(xs, axis=0)
        best_idx = np.argmax(weight_stack, axis=0)
        prefer = np.take_along_axis(x_stack, best_idx[np.newaxis, ...], axis=0)[0]

        merged = c_res_eff[..., np.newaxis] * ivw + (1.0 - c_res_eff[..., np.newaxis]) * prefer
        # Luma disagreement alone — no cross-channel spread requirement, see
        # the misalignment-edge-fallback docstring above.
        misaligned = lum_diff_max > _LUMA_DISAGREE_TAU
        chunk = np.where(misaligned[..., np.newaxis], xs[0], merged)
        chunk = np.where(all_zero_conf, 0.0, chunk)

        out[y0:y1] = np.clip(chunk, 0, 65535).astype(np.uint16)
        w0_sum += float(weights[0].sum())
        wn_sum += float(weights[-1].sum())
        n_weights += int(weights[0].size)
        zero_count += int(np.count_nonzero(np.all(all_zero_conf, axis=-1)))
        c_res_sum += float(c_res_eff.sum())

    stats = FusionStats(
        mean_short_weight=w0_sum / max(n_weights, 1),
        mean_long_weight=wn_sum / max(n_weights, 1),
        zero_weight_pixels=zero_count,
        total_pixels=total_pixels,
        mean_residual_confidence=c_res_sum / max(total_pixels, 1),
        exposure_ratio_used=float(exposures[-1]) / e0,
    )
    return MergeResult(rgb=out, fusion_stats=stats)
