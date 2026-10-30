# SPDX-License-Identifier: GPL-3.0-or-later
"""Multi-exposure SNR / IVW merge in linear film-negative scanner space."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from pyopticfilm.pass_align import align_pass_to_reference, apply_me_transform, estimate_me_transform
from pyopticfilm.registration.types import Transform

_FULL_SCALE = 65535.0

# Soft confidence (fraction of full scale). Clip starts earlier than a hard
# 16-bit rail so CCD knee / pre-sat nonlinearity does not bleed into IVW.
_SNR_FLOOR = 0.002 * _FULL_SCALE
_SNR_CLIP_START = 0.80 * _FULL_SCALE
_SNR_CLIP_END = 0.95 * _FULL_SCALE
# Poisson-Gaussian DN² model. ME merges fit it per scan from the pass pair
# (:func:`_fit_pg_noise_from_pair`); these are the fallback when the fit fails.
_SNR_ALPHA = 1.0
_SNR_BETA = 4096.0  # ~64 DN read noise
# Pass-pair noise fit: equal-count level bins, robust spread per bin.
_NOISE_FIT_BINS = 24
_NOISE_FIT_MIN_PER_BIN = 200
_NOISE_FIT_MIN_BINS = 4
# Plausible fitted range; outside it the channel keeps the fallback model.
_NOISE_FIT_ALPHA_MIN = 1e-3
_NOISE_FIT_ALPHA_MAX = 100.0
_NOISE_FIT_BETA_MIN = 1.0
_NOISE_FIT_BETA_MAX = 1e7
# Long-vs-short transfer curve: log-spaced bins over the two passes' mean level, so
# the dense end gets knots of its own however rare it is on the frame.
_TRANSFER_BINS = 32
_TRANSFER_MIN_PIXELS = 4000
_TRANSFER_MIN_BIN_PIXELS = 100
# Below this level span (max/min bin) there is no curve to see; keep the ratio.
_TRANSFER_MIN_SPAN = 2.0
# Upper share of the knots whose ratio is reported as the gain.
_TRANSFER_GAIN_SHARE = 1.0 / 3.0
# Per-pixel residual gate: only for single-pixel events (dust, hot pixels). It has
# to sit well above the noise's tails, or it keeps exactly the noisiest readings.
_SNR_Z_LO = 5.0
_SNR_Z_HI = 8.0

# Row bands for IVW merge — avoids several full-frame float32 planes at 3600+ dpi.
# Each band holds about 15 float32 planes, so wide frames take shorter bands to keep
# the peak bounded across workers. NumPy drops the GIL in these loops; past 8
# workers memory bandwidth limits the gain.
_MERGE_CHUNK_ROWS = 128
_MERGE_BAND_PIXELS = 512_000
_MERGE_WORKERS = min(8, os.cpu_count() or 1)
_STATS_MAX_PIXELS = 500_000
# Multi-Pass luma disagreement (DN) above which a repeat is dropped as an outlier.
_LUMA_DISAGREE_TAU = 300.0
# ME disagreement guard: residual energy over a (2r+1)² box, as a multiple of what
# the noise model predicts. Noise sits near 1 and cannot trip it, unlike a per-pixel
# threshold, which threw out the long on a third of a real frame. Energy rather than
# the mean, so detail the resampled long softened counts too: its residual changes
# sign and would average away. Falling back there costs little, as texture hides
# noise. Transfer-model error is tolerated as a fraction of the level.
_GUARD_RADIUS = 3
_GUARD_ENERGY_LO = 1.5
_GUARD_ENERGY_HI = 3.0
_GUARD_LEVEL_TOLERANCE = 0.005


@dataclass(frozen=True)
class FusionStats:
    """Mean short/long IVW weights and related diagnostics."""

    mean_short_weight: float
    mean_long_weight: float
    zero_weight_pixels: int
    total_pixels: int
    mean_residual_confidence: float | None = None
    #: Mean of :attr:`exposure_ratio_per_channel`.
    exposure_ratio_used: float | None = None
    #: Long/short ratio in the upper tones for each of R, G, B; it falls towards
    #: black, so the merge uses a level curve and this is only its top end.
    exposure_ratio_per_channel: tuple[float, ...] | None = None
    #: Poisson-Gaussian ``α`` / ``β`` used for each of R, G, B (short-pass DN).
    noise_alpha: tuple[float, ...] | None = None
    noise_beta: tuple[float, ...] | None = None
    #: Per channel: ``True`` when ``α``/``β`` were fitted from this pass pair.
    noise_fitted: tuple[bool, ...] | None = None

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
    align_transform: Transform | None = None,
    alpha: float | None = None,
    beta: float | None = None,
) -> np.ndarray:
    """Fuse short and long RGB negatives into one uint16 frame (SNR / IVW).

    ``align_transform`` registers the long (see
    :func:`~pyopticfilm.pass_align.estimate_me_transform`, which runs when
    neither it nor ``align_shift`` is given); ``align_shift`` is a plain
    bilinear translation. With ``alpha`` and ``beta`` both given, they set the
    noise model; otherwise it is fitted from this pass pair (see
    :func:`_fit_pg_noise_from_pair`).
    """
    return merge_exposures_result(
        short,
        long,
        exposure_short=exposure_short,
        exposure_long=exposure_long,
        align_shift=align_shift,
        align_transform=align_transform,
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
    align_transform: Transform | None = None,
    alpha: float | None = None,
    beta: float | None = None,
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

    if align_shift is None and align_transform is None:
        align_transform = estimate_me_transform(short_u, long_u)
    if align_transform is not None:
        long_u, var_scale = apply_me_transform(long_u, align_transform, clip_level=_SNR_CLIP_END)
    else:
        long_u, shift_used = align_pass_to_reference(short_u, long_u, shift=align_shift)
        var_scale = np.full(short_u.shape[0], _linear_shift_var_scale(shift_used))
    usb_ratio = exposure_long / float(exposure_short)

    rgb, stats = _merge_snr(
        short_u,
        long_u,
        usb_ratio=usb_ratio,
        long_var_scale=var_scale,
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
    stride = int(np.ceil(np.sqrt(ha * wa / _STATS_MAX_PIXELS)))
    if stride <= 1:
        return a, b
    return a[::stride, ::stride], b[::stride, ::stride]


def _linear_shift_var_scale(shift: tuple[float, float]) -> float:
    """Variance factor of white noise after a bilinear shift by ``shift``.

    Linear interpolation at fraction ``t`` averages two samples with weights
    ``1-t``, ``t``, scaling variance by ``(1-t)² + t²`` per axis — down to a
    quarter at a half-pixel shift on both axes.
    """
    scale = 1.0
    for s in shift:
        t = float(s) - np.floor(float(s))
        scale *= (1.0 - t) ** 2 + t**2
    return scale


def _fit_pg_noise_from_pair(
    a: np.ndarray,
    b_raw: np.ndarray,
    xb: np.ndarray,
    *,
    r: np.ndarray,
    long_var_scale: float,
    floor: float,
    clip_start: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-channel Poisson-Gaussian ``α``, ``β`` from the aligned pass pair.

    Both passes see the same scene, so ``d = short - xb`` is noise plus
    leftover misregistration, which the robust spread rejects. At true level
    ``x`` (short scale), with ``s`` the long's resampling variance factor::

        var(d) = α·x·(1 + s/r) + β·(1 + s/r²)

    Binning ``d`` by level (read from the long, the less noisy pass) and
    fitting that line gives ``α``, ``β``. Grain and fixed-pattern noise common
    to both passes cancel in ``d``; that is right for the weights, which only
    need the noise that averages down between passes.

    Returns ``(alpha, beta, fitted)`` per channel; NaN where the fit fails.
    """
    n_ch = a.shape[2]
    alpha = np.full(n_ch, np.nan)
    beta = np.full(n_ch, np.nan)
    for c in range(n_ch):
        ac = a[..., c]
        bc = b_raw[..., c]
        rc = float(r[c])
        # Select on the long's level, not the short's reading: cutting the
        # short at the floor trims its noise there and biases the fit.
        scaled = xb[..., c]
        valid = (scaled > floor) & (bc > floor) & (bc < clip_start) & (ac < clip_start)
        if int(np.count_nonzero(valid)) < _NOISE_FIT_MIN_BINS * _NOISE_FIT_MIN_PER_BIN:
            continue
        level = scaled[valid]
        d = ac[valid] - level
        edges = np.quantile(level, np.linspace(0.0, 1.0, _NOISE_FIT_BINS + 1))
        idx = np.clip(np.searchsorted(edges, level, side="right") - 1, 0, _NOISE_FIT_BINS - 1)
        xs: list[float] = []
        vs: list[float] = []
        ns: list[int] = []
        for k in range(_NOISE_FIT_BINS):
            in_bin = idx == k
            n = int(np.count_nonzero(in_bin))
            if n < _NOISE_FIT_MIN_PER_BIN:
                continue
            dk = d[in_bin]
            mad = float(np.median(np.abs(dk - np.median(dk))))
            xs.append(float(np.median(level[in_bin])))
            vs.append((1.4826 * mad) ** 2)
            ns.append(n)
        if len(xs) < _NOISE_FIT_MIN_BINS:
            continue
        x = np.asarray(xs)
        v = np.asarray(vs)
        n = np.asarray(ns, dtype=np.float64)
        design = np.column_stack([x, np.ones_like(x)])
        # A bin's variance estimate has spread ∝ var/√n: reweight by the
        # predicted variance (start from the measured one).
        pred = np.maximum(v, 1e-6)
        coef = np.zeros(2)
        for _ in range(3):
            sw = np.sqrt(n) / pred
            coef, *_ = np.linalg.lstsq(design * sw[:, None], v * sw, rcond=None)
            pred = np.maximum(design @ coef, 1e-6)
        a_fit = float(coef[0]) / (1.0 + long_var_scale / rc)
        b_fit = float(coef[1]) / (1.0 + long_var_scale / (rc * rc))
        if not (np.isfinite(a_fit) and np.isfinite(b_fit)):
            continue
        if not (_NOISE_FIT_ALPHA_MIN <= a_fit <= _NOISE_FIT_ALPHA_MAX):
            continue
        if b_fit > _NOISE_FIT_BETA_MAX:
            continue
        alpha[c] = a_fit
        # A slightly negative intercept is fit noise around a tiny read noise.
        beta[c] = max(b_fit, _NOISE_FIT_BETA_MIN)
    return alpha, beta, np.isfinite(alpha)


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


def _confidence_lut(*, floor: float, clip_start: float, clip_end: float) -> np.ndarray:
    """:func:`_smooth_confidence` for every uint16 reading; a lookup beats the ramps."""
    return _smooth_confidence(
        np.arange(65536, dtype=np.float32), floor=floor, clip_start=clip_start, clip_end=clip_end
    )


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
) -> np.ndarray:
    """Robust per-channel median long/short ratio on mid-tone pixels.

    One shared ratio leaves any per-channel gain mismatch in the merge, which
    then shifts hue with tone as the weight moves from long to short near the
    long clip. A channel with too few mid-tones takes the luma ratio, or the
    USB ratio when luma has too few as well.
    """
    lo = 0.01 * full_scale
    hi = 0.85 * full_scale
    la = a.mean(axis=2)
    lb = b_raw.mean(axis=2)
    valid_lum = (la > lo) & (la < hi) & (lb > lo) & (lb < hi)
    if int(np.count_nonzero(valid_lum)) >= 100:
        fallback = float(np.median(lb[valid_lum] / np.maximum(la[valid_lum], 1e-12)))
    else:
        fallback = float(usb_ratio)
    ratios = np.full(a.shape[2], fallback, dtype=np.float32)
    for c in range(a.shape[2]):
        ac = a[..., c]
        bc = b_raw[..., c]
        valid = (ac > lo) & (ac < hi) & (bc > lo) & (bc < hi)
        if int(np.count_nonzero(valid)) >= 1000:
            ratios[c] = np.median(bc[valid] / np.maximum(ac[valid], 1e-12))
    return ratios


def _fit_transfer(a: np.ndarray, b_raw: np.ndarray, ratio: float, floor: float) -> tuple[float, np.ndarray]:
    """One channel's gain and uint16 LUT taking a long reading to short scale.

    Long over short is not a constant: on every real film measured it falls
    towards black, from 6.3x in the mid-tones to 5.5x near it, while opaque film
    reads above zero in the long. No gain and offset fits both ends, and fits of
    one flipped on and off between repeats of a frame, moving dense blue by 3.5%.
    So the curve is taken from the pair, as measured. Bins follow the mean of both
    passes because binning on either one alone selects on its noise and bends the
    curve at the ends. Too little to go on keeps the plain ratio.
    """
    readings = np.arange(65536, dtype=np.float64)
    plain = (ratio, (readings / ratio).astype(np.float32))
    hi = 0.85 * _FULL_SCALE
    level = 0.5 * (a + b_raw / ratio)
    # A long at the floor says nothing: dense film can sit censored at its zero.
    valid = (level > floor) & (b_raw > floor) & (a < hi) & (b_raw < hi)
    if int(np.count_nonzero(valid)) < _TRANSFER_MIN_PIXELS:
        return plain
    lv, av, bv = level[valid], a[valid], b_raw[valid]
    edges = np.geomspace(float(lv.min()), float(lv.max()) * 1.0001, _TRANSFER_BINS + 1)
    idx = np.clip(np.searchsorted(edges, lv, side="right") - 1, 0, _TRANSFER_BINS - 1)
    n = np.bincount(idx, minlength=_TRANSFER_BINS).astype(np.float64)
    ok = n >= _TRANSFER_MIN_BIN_PIXELS
    if int(np.count_nonzero(ok)) < 4:
        return plain
    x = np.bincount(idx, weights=av, minlength=_TRANSFER_BINS)[ok] / n[ok]
    y = np.bincount(idx, weights=bv, minlength=_TRANSFER_BINS)[ok] / n[ok]
    if x.min() <= 0.0 or x.max() / x.min() < _TRANSFER_MIN_SPAN:
        return plain
    y = np.maximum.accumulate(y)
    top = max(1, round(x.size * _TRANSFER_GAIN_SHARE))
    gain = float(np.median(y[-top:] / x[-top:]))
    if gain <= 0.0:
        return plain
    # Past the last knot the top end's ratio carries on; below the first, to zero.
    knots_y = np.concatenate(([0.0], y, [_FULL_SCALE + 1.0]))
    knots_x = np.concatenate(([0.0], x, [x[-1] + (_FULL_SCALE + 1.0 - y[-1]) / gain]))
    return gain, np.interp(readings, knots_y, knots_x).astype(np.float32)


def _estimate_transfer(
    a: np.ndarray,
    b_raw: np.ndarray,
    *,
    usb_ratio: float,
    floor: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-channel gain and ``(65536, C)`` long-to-short LUT; see :func:`_fit_transfer`."""
    ratios = np.maximum(_estimate_exposure_ratio(a, b_raw, usb_ratio=usb_ratio), 1e-12)
    fits = [_fit_transfer(a[..., c], b_raw[..., c], float(ratios[c]), floor) for c in range(a.shape[2])]
    return np.asarray([f[0] for f in fits], dtype=np.float32), np.stack([f[1] for f in fits], axis=1)


def _long_to_short(long_u: np.ndarray, curve: np.ndarray) -> np.ndarray:
    """Long readings in short scale through the transfer LUT."""
    return np.stack([curve[long_u[..., c], c] for c in range(long_u.shape[2])], axis=2)


def _pass_variances(
    xa: np.ndarray,
    xb: np.ndarray,
    cb: np.ndarray,
    *,
    r: np.ndarray,
    alpha: np.ndarray,
    beta: np.ndarray,
    long_var_scale: float | np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-pixel noise variance of the short and of the long in short scale.

    Both come from one level estimate, the long where it is confident. Taken
    from each pass's own reading, a reading that noise pushed low would get a
    low variance, so a high weight, and the merge would be biased dark.
    """
    level = np.maximum(xa + cb * (xb - xa), 0.0)
    va = alpha * level + beta
    vb = (alpha * level * r + beta) * (long_var_scale / (r * r))
    return va, vb


def _confident_luma(
    conf: np.ndarray,
    xa: np.ndarray,
    xb: np.ndarray,
    va: np.ndarray,
    vb: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Luma of both passes and its variances over channels confident in both.

    A channel clipped in the long (often the clear red under the orange mask)
    reads far below the short there; in a plain mean it made every pixel with
    one clipped channel look misregistered, and the guards then dropped the
    long for the other channels too. ``conf`` is ``min(ca, cb)`` per channel.
    Returns ``(lum_xa, lum_xb, va_lum, vb_lum, has)``; ``has`` is False where
    no channel is confident in both passes. Channel noise is independent, so the
    luma variances are those of a weighted average, not the average variance.
    """
    wsum = conf.sum(axis=2)
    has = wsum > 1e-6
    wsafe = np.maximum(wsum, 1e-6)
    w2 = conf * conf
    w2sum = wsafe * wsafe
    return (
        (conf * xa).sum(axis=2) / wsafe,
        (conf * xb).sum(axis=2) / wsafe,
        (w2 * va).sum(axis=2) / w2sum,
        (w2 * vb).sum(axis=2) / w2sum,
        has,
    )


def _estimate_z_median(
    short_u: np.ndarray,
    long_u: np.ndarray,
    *,
    r: np.ndarray,
    curve: np.ndarray,
    alpha: np.ndarray,
    beta: np.ndarray,
    long_var_scale: float,
    floor: float,
    clip_start: float,
    clip_end: float,
) -> float:
    """Global residual-gate median on a strided subsample (full frame is OOM at high dpi)."""
    a_s, b_s = _subsample_for_stats(short_u, long_u)
    a = a_s.astype(np.float32)
    b_raw = b_s.astype(np.float32)
    xb = _long_to_short(b_s, curve)
    cb = _smooth_confidence(b_raw, floor=floor, clip_start=clip_start, clip_end=clip_end)
    va, vb = _pass_variances(a, xb, cb, r=r, alpha=alpha, beta=beta, long_var_scale=long_var_scale)
    conf = np.minimum(_smooth_confidence(a, floor=floor, clip_start=clip_start, clip_end=clip_end), cb)
    lum_xa, lum_xb, va_lum, vb_lum, has = _confident_luma(conf, a, xb, va, vb)
    if not np.any(has):
        return 0.0
    z = (lum_xa - lum_xb) / np.sqrt(np.maximum(va_lum + vb_lum, 1e-12))
    return float(np.median(z[has]))


def _box_sum(a: np.ndarray, radius: int) -> np.ndarray:
    """Sum over a ``(2r+1)²`` box; the frame edge repeats outward."""
    n = 2 * radius + 1
    c = np.cumsum(np.pad(a, ((radius + 1, radius), (0, 0)), mode="edge"), axis=0, dtype=np.float64)
    rows = c[n:] - c[:-n]
    c = np.cumsum(np.pad(rows, ((0, 0), (radius + 1, radius)), mode="edge"), axis=1)
    return c[:, n:] - c[:, :-n]


def _agreement_confidence(residual: np.ndarray, variance: np.ndarray, level: np.ndarray) -> np.ndarray:
    """1 where the pass residual is noise, falling to 0 where its energy exceeds noise."""
    # Variance and level vary slowly, so their value here stands in for the box's.
    energy = _box_sum(residual * residual, _GUARD_RADIUS) / (2 * _GUARD_RADIUS + 1) ** 2
    tolerance = _GUARD_LEVEL_TOLERANCE * level
    ratio = energy / np.maximum(variance + tolerance * tolerance, 1e-12)
    return np.clip((_GUARD_ENERGY_HI - ratio) / (_GUARD_ENERGY_HI - _GUARD_ENERGY_LO), 0.0, 1.0).astype(np.float32)


def _merge_snr_rows(
    short_rows: np.ndarray,
    long_rows: np.ndarray,
    *,
    keep: slice,
    r: np.ndarray,
    curve: np.ndarray,
    z_median: float,
    alpha: np.ndarray,
    beta: np.ndarray,
    long_var_scale: np.ndarray,
    conf_lut: np.ndarray,
    floor: float,
    clip_start: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """IVW merge for one row band; returns uint16 chunk and stat accumulators.

    The band carries :data:`_GUARD_RADIUS` extra rows on inner edges so the box
    filter does not see the band split; ``keep`` selects the rows to return.
    """
    a = short_rows.astype(np.float32)
    b_raw = long_rows.astype(np.float32)
    xa = a
    xb = _long_to_short(long_rows, curve)

    ca = conf_lut[short_rows]
    cb = conf_lut[long_rows]

    va, vb = _pass_variances(xa, xb, cb, r=r, alpha=alpha, beta=beta, long_var_scale=long_var_scale)

    lum_xa, lum_xb, va_lum, vb_lum, has = _confident_luma(np.minimum(ca, cb), xa, xb, va, vb)
    var_lum = va_lum + vb_lum
    residual = np.where(has, lum_xa - lum_xb, 0.0)
    z_local = residual / np.sqrt(np.maximum(var_lum, 1e-12)) - z_median
    agreement = _agreement_confidence(residual, np.where(has, var_lum, 0.0), lum_xb)
    # No channel confident in both: nothing to compare, and IVW is one-sided anyway.
    c_res = np.where(has, np.minimum(_residual_confidence(z_local), agreement), 1.0)[keep]

    a, b_raw, xa, xb, ca, cb, va, vb = (x[keep] for x in (a, b_raw, xa, xb, ca, cb, va, vb))

    wa = ca / np.maximum(va, 1e-12)
    wb = cb / np.maximum(vb, 1e-12)
    both_zero = (ca + cb) <= 1e-6
    ivw = (wa * xa + wb * xb) / np.maximum(wa + wb, 1e-12)

    # Passes disagree: keep the short, the reference geometry, for all channels so
    # no colour fringes appear. Only as far as it has a reading, though: at its
    # noise floor or clip the long is all there is.
    c = c_res[..., np.newaxis]
    out = c * ivw + (1.0 - c) * (ca * xa + (1.0 - ca) * ivw)
    # No confident input. A short clipped at the top keeps its reading — 0 there
    # turned clear film / slide highlights black. Below the noise floor an
    # unclipped long is still the best estimate — 0 there crushed the densest
    # areas to black one DN under the floor. Only a short at the floor against
    # a clipped long (no usable reading) goes to 0. Test against clip_start:
    # confidence already reads as zero a few DN below clip_end, and the noise
    # floor is nowhere near it. Under the curve's first knot the long means little,
    # so the larger reading stands: a long at zero must not blank a short above it.
    fallback = np.where(a >= clip_start, xa, np.where(b_raw < clip_start, np.maximum(xa, xb), 0.0))
    out = np.where(both_zero, fallback, out)

    both_zero_pix = np.all(both_zero, axis=2)
    chunk = np.rint(np.clip(out, 0, 65535)).astype(np.uint16)
    return chunk, wa, wb, both_zero_pix, c_res


def _merge_snr(
    short_u: np.ndarray,
    long_u: np.ndarray,
    *,
    usb_ratio: float,
    long_var_scale: np.ndarray,
    alpha: float | None,
    beta: float | None,
    floor: float,
    clip_start: float,
    clip_end: float,
) -> tuple[np.ndarray, FusionStats]:
    """Clipping-aware inverse-variance fusion in short-exposure radiometric scale.

    Processed in row bands so peak memory stays O(chunk) not O(full frame float32).
    ``alpha``/``beta`` both given set the noise model; otherwise it is fitted
    per channel from the pair, with the module defaults where the fit fails.
    ``long_var_scale`` is the long's resampling variance factor per row.
    """
    a_sub, long_sub = _subsample_for_stats(short_u, long_u)
    a_sub = a_sub.astype(np.float32)
    b_sub = long_sub.astype(np.float32)
    r, curve = _estimate_transfer(a_sub, b_sub, usb_ratio=usb_ratio, floor=floor)
    n_ch = short_u.shape[2]
    mean_var_scale = float(np.mean(long_var_scale))
    if alpha is not None and beta is not None:
        alpha_c = np.full(n_ch, float(alpha))
        beta_c = np.full(n_ch, float(beta))
        fitted = np.zeros(n_ch, dtype=bool)
    else:
        alpha_c, beta_c, fitted = _fit_pg_noise_from_pair(
            a_sub,
            b_sub,
            _long_to_short(long_sub, curve),
            r=r,
            long_var_scale=mean_var_scale,
            floor=floor,
            clip_start=clip_start,
        )
        alpha_c = np.where(fitted, alpha_c, _SNR_ALPHA)
        beta_c = np.where(fitted, beta_c, _SNR_BETA)
    alpha_c = alpha_c.astype(np.float32)
    beta_c = beta_c.astype(np.float32)
    z_median = _estimate_z_median(
        short_u,
        long_u,
        r=r,
        curve=curve,
        alpha=alpha_c,
        beta=beta_c,
        long_var_scale=mean_var_scale,
        floor=floor,
        clip_start=clip_start,
        clip_end=clip_end,
    )

    h, w = short_u.shape[:2]
    out = np.empty_like(short_u)
    total_pixels = int(h * w)
    conf_lut = _confidence_lut(floor=floor, clip_start=clip_start, clip_end=clip_end)
    band_rows = max(32, min(_MERGE_CHUNK_ROWS, _MERGE_BAND_PIXELS // max(w, 1)))

    def merge_band(y0: int) -> tuple[float, float, int, int, float]:
        y1 = min(h, y0 + band_rows)
        pad_y0 = max(0, y0 - _GUARD_RADIUS)
        pad_y1 = min(h, y1 + _GUARD_RADIUS)
        chunk, wa, wb, both_zero_pix, c_res_eff = _merge_snr_rows(
            short_u[pad_y0:pad_y1],
            long_u[pad_y0:pad_y1],
            keep=slice(y0 - pad_y0, y1 - pad_y0),
            r=r,
            curve=curve,
            z_median=z_median,
            alpha=alpha_c,
            beta=beta_c,
            long_var_scale=long_var_scale[pad_y0:pad_y1, np.newaxis, np.newaxis].astype(np.float32),
            conf_lut=conf_lut,
            floor=floor,
            clip_start=clip_start,
        )
        out[y0:y1] = chunk
        return (
            float(wa.sum()),
            float(wb.sum()),
            int(wa.size),
            int(np.count_nonzero(both_zero_pix)),
            float(c_res_eff.sum()),
        )

    with ThreadPoolExecutor(max_workers=_MERGE_WORKERS) as pool:
        bands = list(pool.map(merge_band, range(0, h, band_rows)))
    wa_sum, wb_sum, n_weights, zero_count, c_res_sum = (sum(column) for column in zip(*bands, strict=True))

    stats = FusionStats(
        mean_short_weight=wa_sum / max(n_weights, 1),
        mean_long_weight=wb_sum / max(n_weights, 1),
        zero_weight_pixels=zero_count,
        total_pixels=total_pixels,
        mean_residual_confidence=c_res_sum / max(total_pixels, 1),
        exposure_ratio_used=float(r.mean()),
        exposure_ratio_per_channel=tuple(float(v) for v in r),
        noise_alpha=tuple(float(v) for v in alpha_c),
        noise_beta=tuple(float(v) for v in beta_c),
        noise_fitted=tuple(bool(v) for v in fitted),
    )
    return out, stats


def merge_n_passes(
    frames: list[np.ndarray],
    *,
    alpha: float | None = None,
    beta: float | None = None,
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
    alpha = _SNR_ALPHA if alpha is None else float(alpha)
    beta = _SNR_BETA if beta is None else float(beta)
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
        # No confident frame (below the noise floor or clipped in every
        # frame): the plain mean keeps the reading — 0 here crushed the
        # densest areas to black one DN under the floor.
        if np.any(all_zero_conf):
            plain_mean = np.mean(np.stack(xs, axis=0), axis=0)
            merged = np.where(all_zero_conf, plain_mean, merged)

        out[y0:y1] = np.clip(merged, 0, 65535).astype(np.uint16)
        conf_mean_pixel = np.mean(np.stack(cs, axis=0), axis=0)
        conf_sum += float(conf_mean_pixel.sum())
        n_conf += int(conf_mean_pixel.size)
        zero_count += int(np.count_nonzero(np.all(no_weight, axis=-1)))
        outlier_count += int(np.count_nonzero(outlier_any))

    stats = PassMergeStats(
        n_frames=len(frames),
        total_pixels=total_pixels,
        mean_confidence=conf_sum / max(n_conf, 1),
        outlier_pixels=outlier_count,
        zero_weight_pixels=zero_count,
    )
    return MergeResult(rgb=out, fusion_stats=stats)
