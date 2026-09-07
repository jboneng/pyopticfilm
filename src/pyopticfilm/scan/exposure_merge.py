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
class PassMergeStats:
    """Diagnostics for :func:`merge_n_passes` (same-exposure frame stacking)."""

    n_frames: int
    total_pixels: int
    mean_confidence: float
    outlier_pixels: int
    zero_weight_pixels: int

    @property
    def zero_weight_fraction(self) -> float:
        return self.zero_weight_pixels / self.total_pixels if self.total_pixels else 0.0

    @property
    def outlier_fraction(self) -> float:
        return self.outlier_pixels / self.total_pixels if self.total_pixels else 0.0


@dataclass(frozen=True)
class MergeResult:
    rgb: np.ndarray
    fusion_stats: FusionStats | PassMergeStats | None = None


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


def merge_n_passes(
    frames: list[np.ndarray],
    *,
    alpha: float = _SNR_ALPHA,
    beta: float = _SNR_BETA,
) -> MergeResult:
    """Stack N same-exposure captures for an SNR gain (no exposure fusion).

    Unlike :func:`merge_exposures_result`, every frame here is nominally the
    *same* exposure/radiometric scale (repeated Multi-Pass captures), so
    there is no exposure-ratio rescale and no residual-disagreement z-score
    gate — that machinery exists specifically to fuse *different* exposure
    levels. Any per-pixel disagreement between aligned repeats is either
    capture noise or leftover misalignment, not an exposure-fusion problem.

    Per-pixel Poisson-Gaussian inverse-variance weighting (the same noise
    model and :func:`_smooth_confidence` clip/floor ramp as the pairwise
    merge) reduces, for same-exposure frames, to a confidence-weighted mean.
    A lightweight outlier guard additionally zeroes a frame's weight at any
    pixel where its luma disagrees with the per-pixel median luma across all
    frames by more than ``_LUMA_DISAGREE_TAU`` — luma-only, per PR #52's
    empirically-checked finding that luma alone (not ANDed/ORed with
    cross-channel spread) is the correct signal for real pass-to-pass drift
    on flat/neutral content, without false-triggering on well-aligned
    saturated color. This guard needs at least 3 frames to be effective: with
    exactly 2, the median is their mean, so both frames sit equidistant from
    it and either both get flagged (falling back to a plain mean — the same
    result as no guard) or neither does.

    Frames must already be pairwise-aligned to a common reference (e.g. via
    :func:`pyopticfilm.pass_align.align_pass_to_reference_banded`) — this
    function does no alignment of its own.

    Args:
        frames: >=1 uint16 HxWx3 arrays of the same shape and nominal
            exposure, already aligned.

    Returns:
        MergeResult with the stacked uint16 HxWx3 array and PassMergeStats.

    Raises:
        ValueError: no frames, or mismatched shapes.
    """
    if not frames:
        raise ValueError("merge_n_passes needs >= 1 frame, got 0")
    ref = np.asarray(frames[0], dtype=np.uint16)
    if ref.ndim != 3 or ref.shape[2] != 3:
        raise ValueError(f"expected HxWx3 arrays, got {ref.shape}")
    for i, f in enumerate(frames[1:], 1):
        fa = np.asarray(f, dtype=np.uint16)
        if fa.shape != ref.shape:
            raise ValueError(
                f"frame {i} shape {fa.shape} does not match frame 0 shape {ref.shape}"
            )

    total_pixels = int(ref.shape[0] * ref.shape[1])
    if len(frames) == 1:
        return MergeResult(
            rgb=ref.copy(),
            fusion_stats=PassMergeStats(
                n_frames=1,
                total_pixels=total_pixels,
                mean_confidence=1.0,
                outlier_pixels=0,
                zero_weight_pixels=0,
            ),
        )

    h, w = ref.shape[:2]
    out = np.empty((h, w, 3), dtype=np.uint16)
    conf_sum = 0.0
    n_conf = 0
    zero_count = 0
    outlier_count = 0

    for y0 in range(0, h, _MERGE_CHUNK_ROWS):
        y1 = min(h, y0 + _MERGE_CHUNK_ROWS)
        xs = [np.asarray(f[y0:y1], dtype=np.float32) for f in frames]
        cs = [
            _smooth_confidence(x, floor=_SNR_FLOOR, clip_start=_SNR_CLIP_START, clip_end=_SNR_CLIP_END)
            for x in xs
        ]
        lumas = [x.mean(axis=2) for x in xs]
        luma_median = np.median(np.stack(lumas, axis=0), axis=0)

        weights: list[np.ndarray] = []
        outlier_any = np.zeros((y1 - y0, w), dtype=bool)
        for x, c, lum in zip(xs, cs, lumas, strict=True):
            variance = alpha * np.maximum(x, 0.0) + beta
            w_i = c / np.maximum(variance, 1e-12)
            is_outlier = np.abs(lum - luma_median) > _LUMA_DISAGREE_TAU
            w_i = np.where(is_outlier[..., np.newaxis], 0.0, w_i)
            outlier_any |= is_outlier
            weights.append(w_i)

        acc = weights[0] * xs[0]
        w_sum = weights[0].copy()
        all_zero_conf = cs[0] <= 1e-6
        for x, c, w_i in zip(xs[1:], cs[1:], weights[1:], strict=True):
            acc += w_i * x
            w_sum += w_i
            all_zero_conf &= c <= 1e-6
        merged = acc / np.maximum(w_sum, 1e-12)
        # Every frame's weight collapsed to zero at a pixel (e.g. all
        # flagged outlier) — fall back to the plain mean rather than a
        # near-zero denominator.
        no_weight = w_sum <= 1e-9
        if np.any(no_weight):
            plain_mean = np.mean(np.stack(xs, axis=0), axis=0)
            merged = np.where(no_weight, plain_mean, merged)
        merged = np.where(all_zero_conf, 0.0, merged)

        out[y0:y1] = np.clip(merged, 0, 65535).astype(np.uint16)
        conf_mean_pixel = np.mean(np.stack(cs, axis=0), axis=0)
        conf_sum += float(conf_mean_pixel.sum())
        n_conf += int(conf_mean_pixel.size)
        zero_count += int(np.count_nonzero(np.all(all_zero_conf, axis=-1)))
        outlier_count += int(np.count_nonzero(outlier_any))

    stats = PassMergeStats(
        n_frames=len(frames),
        total_pixels=total_pixels,
        mean_confidence=conf_sum / max(n_conf, 1),
        outlier_pixels=outlier_count,
        zero_weight_pixels=zero_count,
    )
    return MergeResult(rgb=out, fusion_stats=stats)
