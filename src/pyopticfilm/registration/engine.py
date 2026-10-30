# SPDX-License-Identifier: GPL-3.0-or-later
"""Native registration engine — estimate + apply transforms."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from pyopticfilm.logging import get_logger
from pyopticfilm.registration.models import (
    band_shift_profile,
    line_warp_from_profile,
    shift_is_pathological,
)
from pyopticfilm.registration.phase import phase_correlate_shift
from pyopticfilm.registration.preprocess import (
    coarse_luma_registration_pair,
    coarse_registration_pair,
    roi_registration_plane,
)
from pyopticfilm.registration.regions import consensus_translation, estimate_regional_shifts
from pyopticfilm.registration.resample import warp_row_shifts, warp_shift
from pyopticfilm.registration.types import (
    LineWarpTransform,
    RegistrationContext,
    RegistrationFailure,
    RegistrationMode,
    RegistrationOptions,
    RegistrationQuality,
    RegistrationResult,
    TranslationTransform,
)
from pyopticfilm.registration.validate import classify_failure, should_snap_to_integer

logger = get_logger(__name__)

# When gradient and luma coarse disagree by more than this (px), prefer the
# smaller-|shift| axis value — the larger one is usually a cross-modal lobe.
_RGB_IR_SEED_AGREE_PX = 0.5
_RGB_IR_REFINE_KEEP_SEED_PX = 0.25


def _rgb_ir_merge_seed_axes(grad_dx: float, grad_dy: float, luma_dx: float, luma_dy: float) -> tuple[float, float]:
    """Per-axis merge of gradient coarse and luma coarse for RGB↔IR."""

    def one(g: float, l: float) -> float:
        if abs(l - g) <= _RGB_IR_SEED_AGREE_PX:
            return l
        return g if abs(g) <= abs(l) else l

    return one(grad_dx, luma_dx), one(grad_dy, luma_dy)


def _rgb_ir_keep_seed_axes(seed_dx: float, seed_dy: float, ref_dx: float, ref_dy: float) -> tuple[float, float]:
    """Keep seed on an axis if refine jumped farther from zero by > margin."""

    def one(s: float, r: float) -> float:
        if abs(r) > abs(s) + _RGB_IR_REFINE_KEEP_SEED_PX:
            return s
        return r

    return one(seed_dx, ref_dx), one(seed_dy, ref_dy)


def _zero_result(
    mode: RegistrationMode,
    *,
    reason: str,
    failure: RegistrationFailure,
) -> RegistrationResult:
    return RegistrationResult(
        transform=TranslationTransform(0.0, 0.0),
        mode=mode,
        accepted=False,
        integer_exact=True,
        quality=RegistrationQuality(),
        reason=reason,
        failure=failure,
    )


def estimate_translation(
    reference: np.ndarray,
    moving: np.ndarray,
    mode: RegistrationMode,
    *,
    options: RegistrationOptions | None = None,
    context: RegistrationContext | None = None,
) -> RegistrationResult:
    """Hierarchical phase-correlation translation (ME / RGB↔IR / MP fallback)."""
    opt = options or RegistrationOptions()
    ref = np.asarray(reference)
    mov = np.asarray(moving)
    if ref.size == 0 or mov.size == 0 or ref.shape[:2] != mov.shape[:2]:
        return _zero_result(mode, reason="shape mismatch", failure=RegistrationFailure.SHAPE_MISMATCH)

    full_h, full_w = ref.shape[:2]
    coarse_ref, coarse_mov, scale = coarse_registration_pair(ref, mov, mode, target_w=opt.coarse_target_px)

    dx, dy, resp = phase_correlate_shift(coarse_ref, coarse_mov, scale=scale, upsample=1)

    if not np.isfinite(dx) or not np.isfinite(dy):
        return _zero_result(mode, reason="non-finite shift", failure=RegistrationFailure.LOW_TEXTURE)

    if resp < opt.min_response:
        logger.warning(
            "registration: shift (%.2f, %.2f) low response (%.3f) — reject",
            dx,
            dy,
            resp,
        )
        return _zero_result(
            mode,
            reason=f"low response {resp:.3f}",
            failure=RegistrationFailure.LOW_TEXTURE,
        )
    if shift_is_pathological(dx, dy, full_w, full_h, max_frac=opt.sanity_max_frac):
        return _zero_result(
            mode,
            reason="pathological shift",
            failure=RegistrationFailure.PATHLOGICAL_SHIFT,
        )

    # ROI refine via integer-slice pre-shift — never full-frame warp.
    # RGB↔IR: when gradient coarse is already small, merge coarse-luma per axis
    # (agree → luma; disagree → smaller |shift| to avoid a 1–2 px cross-modal lobe).
    seed_dx, seed_dy = float(dx), float(dy)
    used_rgb_ir_seed = False
    if mode is RegistrationMode.RGB_IR and max(abs(dx), abs(dy)) <= opt.refine_max_residual + 0.5:
        luma_ref, luma_mov, luma_scale = coarse_luma_registration_pair(ref, mov, target_w=opt.coarse_target_px)
        ldx, ldy, lresp = phase_correlate_shift(luma_ref, luma_mov, scale=luma_scale, upsample=1)
        if (
            np.isfinite(ldx)
            and np.isfinite(ldy)
            and lresp >= opt.min_response
            and not shift_is_pathological(ldx, ldy, full_w, full_h, max_frac=opt.sanity_max_frac)
        ):
            seed_dx, seed_dy = _rgb_ir_merge_seed_axes(seed_dx, seed_dy, float(ldx), float(ldy))
            used_rgb_ir_seed = True
    dx, dy = _refine_translation(ref, mov, (seed_dx, seed_dy), mode, opt)
    if used_rgb_ir_seed:
        # Multi-ROI refine often hops to a competing ~1 px lobe; only keep it
        # as a small polish around the merged coarse seed.
        if abs(dx - seed_dx) > 0.25 or abs(dy - seed_dy) > 0.25:
            dx, dy = seed_dx, seed_dy
        else:
            dx, dy = _rgb_ir_keep_seed_axes(seed_dx, seed_dy, dx, dy)

    if shift_is_pathological(dx, dy, full_w, full_h, max_frac=opt.sanity_max_frac):
        return _zero_result(
            mode,
            reason="refined pathological shift",
            failure=RegistrationFailure.PATHLOGICAL_SHIFT,
        )

    regions: list = []
    regional_mad = 0.0
    inliers = 0
    consensus = None
    if opt.enable_regional:
        # Regional on the cheap coarse plane (already built).
        regions = estimate_regional_shifts(
            coarse_ref,
            coarse_mov,
            min_response=opt.min_response,
            patch=min(128, coarse_ref.shape[0], coarse_ref.shape[1]),
        )
        # Scale regional shifts back to full-res pixels.
        for i, r in enumerate(regions):
            if r.accepted:
                regions[i] = type(r)(
                    r.center_x * scale,
                    r.center_y * scale,
                    r.dx * scale,
                    r.dy * scale,
                    r.response,
                    r.accepted,
                )
        consensus = consensus_translation(regions)
        if consensus is not None:
            cdx, cdy, regional_mad, inliers = consensus
            if regional_mad < 1.5 and inliers >= 3 and abs(cdx - dx) < 2.0 and abs(cdy - dy) < 2.0:
                dx = 0.5 * (dx + cdx)
                dy = 0.5 * (dy + cdy)

    if context is not None:
        if context.max_abs_dx is not None and abs(dx) > context.max_abs_dx:
            return _zero_result(mode, reason="outside prior dx", failure=RegistrationFailure.OUTSIDE_SCANNER_PRIOR)
        if context.max_abs_dy is not None and abs(dy) > context.max_abs_dy:
            return _zero_result(mode, reason="outside prior dy", failure=RegistrationFailure.OUTSIDE_SCANNER_PRIOR)

    # RGB↔IR: never integer-snap — cross-modal peaks often sit ~0.5–1 px off an
    # integer and snapping pulls X/Y onto the wrong lobe (e.g. −1.5 → −2).
    integer_exact = False
    if mode is not RegistrationMode.RGB_IR:
        integer_exact = should_snap_to_integer(dx, dy, eps=opt.snap_frac_eps)
        if integer_exact:
            dx, dy = float(round(dx)), float(round(dy))

    failure = classify_failure(
        response=resp,
        min_response=opt.min_response,
        pathological=False,
        regional_mad=regional_mad if consensus else None,
        shape_ok=True,
        mode=mode,
    )
    # Production: accept unless pathological / low response already returned.
    # Regional disagreement only hard-rejects when regional was enabled.
    accepted = failure in (RegistrationFailure.NONE, RegistrationFailure.REGIONAL_DISAGREEMENT)
    if opt.enable_regional and failure is RegistrationFailure.REGIONAL_DISAGREEMENT and regional_mad > 4.0:
        accepted = False

    quality = RegistrationQuality(
        response=float(resp),
        regional_inliers=inliers,
        regional_total=len(regions),
        regional_mad_px=float(regional_mad),
        residual_before=0.0,
        overlap_fraction=max(0.0, 1.0 - abs(dx) / full_w) * max(0.0, 1.0 - abs(dy) / full_h),
    )
    return RegistrationResult(
        transform=TranslationTransform(dx, dy),
        mode=mode,
        accepted=accepted,
        integer_exact=integer_exact,
        quality=quality,
        regions=tuple(regions),
        reason="ok" if accepted else failure.value,
        failure=failure if not accepted else RegistrationFailure.NONE,
    )


def _roi_residual(
    reference: np.ndarray,
    moving: np.ndarray,
    y0: int,
    x0: int,
    rh: int,
    rw: int,
    idx: int,
    idy: int,
    mode: RegistrationMode,
    *,
    plane_force: str | None,
    upsample: int,
) -> tuple[float, float, float] | None:
    """Phase-correlate one integer-presifted ROI; ``None`` if out of bounds.

    Remap convention: sample moving at ``(y0 - idy, x0 - idx)`` so the ROI
    residual is the true fractional part (not ~2× the shift).
    """
    h, w = reference.shape[:2]
    my0 = y0 - idy
    mx0 = x0 - idx
    ty0, tx0 = y0, x0
    if my0 < 0:
        ty0 -= my0
        my0 = 0
    if mx0 < 0:
        tx0 -= mx0
        mx0 = 0
    th = min(rh, h - ty0, h - my0)
    tw = min(rw, w - tx0, w - mx0)
    if th < 64 or tw < 64:
        return None
    ref_roi = roi_registration_plane(reference[ty0 : ty0 + th, tx0 : tx0 + tw], mode, force=plane_force)
    mov_roi = roi_registration_plane(moving[my0 : my0 + th, mx0 : mx0 + tw], mode, force=plane_force)
    ddx, ddy, resp = phase_correlate_shift(ref_roi, mov_roi, scale=1.0, upsample=upsample)
    return float(ddx), float(ddy), float(resp)


def _refine_translation(
    reference: np.ndarray,
    moving: np.ndarray,
    coarse: tuple[float, float],
    mode: RegistrationMode,
    opt: RegistrationOptions,
    *,
    plane_force: str | None = None,
    force_guizar: bool | None = None,
    second_pass: bool = False,
) -> tuple[float, float]:
    """Refine on a center ROI using integer-slice pre-shift (no full-frame warp).

    Does **not** bilinear-warp the fractional coarse remainder into the tile —
    the ROI phase correlate absorbs the full residual, and the result is
    ``(round(dx0) + ddx, round(dy0) + ddy)``.

    RGB↔IR uses luma + 5×5 centroid (no Guizar — ordinary-spectrum Guizar
    disagrees on cross-modal peaks); multi-ROI median avoids a bad vertical
    lobe. Guizar remains available for ME/MP when enabled.
    """
    h, w = reference.shape[:2]
    if h * w < 512 * 512:
        return coarse
    dx0, dy0 = coarse
    rh = min(opt.refine_roi_side, h)
    rw = min(opt.refine_roi_side, w)
    idx = round(dx0)
    idy = round(dy0)

    # Guizar on ME/MP only: RGB↔IR cross-modal peaks favor phase-only centroid.
    guizar_ok = (
        opt.enable_subpixel
        and opt.enable_guizar_refine
        and mode is not RegistrationMode.RGB_IR
        and max(rh, rw) <= opt.guizar_max_roi_side
    )
    if force_guizar is not None:
        guizar_ok = bool(force_guizar) and max(rh, rw) <= opt.guizar_max_roi_side
    upsample = opt.subpixel_factor if guizar_ok else 1

    cy = max(0, (h - rh) // 2)
    cx = max(0, (w - rw) // 2)
    # Multi-ROI for RGB↔IR luma (cardinals + diagonals); ME/MP: center only.
    if mode is RegistrationMode.RGB_IR and plane_force in (None, "luma"):
        half_y = rh // 2
        half_x = rw // 2
        origins = (
            (cy, cx),
            (cy - half_y, cx),
            (cy + half_y, cx),
            (cy, cx - half_x),
            (cy, cx + half_x),
            (cy - half_y, cx - half_x),
            (cy - half_y, cx + half_x),
            (cy + half_y, cx - half_x),
            (cy + half_y, cx + half_x),
        )
    else:
        origins = ((cy, cx),)

    samples: list[tuple[float, float, float]] = []
    for y0, x0 in origins:
        if y0 < 0 or x0 < 0 or y0 + rh > h or x0 + rw > w:
            continue
        hit = _roi_residual(
            reference,
            moving,
            y0,
            x0,
            rh,
            rw,
            idx,
            idy,
            mode,
            plane_force=plane_force,
            upsample=upsample,
        )
        if hit is None:
            continue
        ddx, ddy, resp = hit
        if resp < opt.min_response or max(abs(ddx), abs(ddy)) > opt.refine_max_residual:
            continue
        samples.append((ddx, ddy, resp))

    if not samples:
        return coarse

    if len(samples) == 1:
        ddx, ddy, _ = samples[0]
    else:
        # High-response tiles, then prefer those near the coarse seed (rejects
        # a competing ~1 px X/Y lobe that still has decent response).
        resp_thr = float(np.median([s[2] for s in samples]))
        kept = [s for s in samples if s[2] >= resp_thr] or samples
        seed_ddx = float(dx0) - float(idx)
        seed_ddy = float(dy0) - float(idy)
        near = [s for s in kept if abs(s[0] - seed_ddx) <= 0.75 and abs(s[1] - seed_ddy) <= 0.75]
        use = near if len(near) >= 2 else kept
        ddx = float(np.median([s[0] for s in use]))
        ddy = float(np.median([s[1] for s in use]))

    dx = float(idx) + ddx
    dy = float(idy) + ddy

    # Optional second pass (off by default for RGB↔IR cross-modal stability).
    if second_pass and opt.enable_subpixel:
        y0, x0 = cy, cx
        my0, mx0 = y0 - idy, x0 - idx
        if my0 < 0:
            y0 -= my0
            my0 = 0
        if mx0 < 0:
            x0 -= mx0
            mx0 = 0
        th = min(rh, h - y0, h - my0)
        tw = min(rw, w - x0, w - mx0)
        if th >= 64 and tw >= 64:
            mov_tile = moving[my0 : my0 + th, mx0 : mx0 + tw]
            # Remap-apply the residual so the tile is near-aligned for remeasure.
            mov_aligned = warp_shift(mov_tile, ddx, ddy, method="linear", chunk_rows=th)
            ref_roi = roi_registration_plane(reference[y0 : y0 + th, x0 : x0 + tw], mode, force=plane_force)
            mov_roi2 = roi_registration_plane(mov_aligned, mode, force=plane_force)
            ddx2, ddy2, resp2 = phase_correlate_shift(ref_roi, mov_roi2, scale=1.0, upsample=1)
            if resp2 >= opt.min_response and max(abs(ddx2), abs(ddy2)) <= opt.refine_max_residual:
                dx += ddx2
                dy += ddy2

    return dx, dy


def _fit_row_line(rows: np.ndarray, dys: np.ndarray, outlier_px: float) -> tuple[float, float, np.ndarray] | None:
    """Least-squares ``dy = slope * row + intercept``, dropping outlier tiles."""
    keep = np.ones(rows.shape, dtype=bool)
    slope = intercept = 0.0
    for _ in range(4):
        if int(np.count_nonzero(keep)) < 3:
            return None
        slope, intercept = np.polyfit(rows[keep], dys[keep], 1)
        resid = np.abs(dys - (slope * rows + intercept))
        spread = 1.4826 * float(np.median(resid[keep]))
        next_keep = resid <= max(outlier_px, 3.0 * spread)
        if np.array_equal(next_keep, keep):
            break
        keep = next_keep
    return float(slope), float(intercept), keep


def estimate_row_drift(
    reference: np.ndarray,
    moving: np.ndarray,
    seed: tuple[float, float],
    *,
    options: RegistrationOptions | None = None,
) -> LineWarpTransform | None:
    """Fit ``dy(row)`` for an ME pass pair; ``None`` when a translation is enough.

    The long pass feeds at a slightly different rate from the short, so a single
    translation only registers the frame centre. Full-resolution tiles around the
    integer ``seed`` give the sub-pixel residual; a robust line through them gives
    the profile. A downsampled band correlate cannot resolve drift this small.
    """
    opt = options or RegistrationOptions()
    ref = np.asarray(reference)
    mov = np.asarray(moving)
    h, w = ref.shape[:2]
    side = opt.drift_tile_side
    if ref.shape[:2] != mov.shape[:2] or h < 2 * side or w < side:
        return None
    idx, idy = round(seed[0]), round(seed[1])
    origins = [
        (int(y0), int(x0))
        for y0 in np.linspace(0, h - side, opt.drift_tile_rows)
        for x0 in np.linspace(0, w - side, opt.drift_tile_cols + 2)[1:-1]
    ]

    def measure(origin: tuple[int, int]) -> tuple[float, float, float, float] | None:
        y0, x0 = origin
        hit = _roi_residual(
            ref,
            mov,
            y0,
            x0,
            side,
            side,
            idx,
            idy,
            RegistrationMode.MULTI_EXPOSURE,
            plane_force=None,
            upsample=opt.subpixel_factor,
        )
        if hit is None:
            return None
        ddx, ddy, resp = hit
        if resp < opt.drift_min_response or max(abs(ddx), abs(ddy)) > opt.drift_max_residual:
            return None
        return y0 + side / 2.0, idx + ddx, idy + ddy, resp

    with ThreadPoolExecutor(max_workers=min(len(origins), os.cpu_count() or 1)) as pool:
        tiles = [t for t in pool.map(measure, origins) if t is not None]
    if len(tiles) < opt.drift_min_tiles:
        return None
    arr = np.asarray(tiles, dtype=np.float64)
    fit = _fit_row_line(arr[:, 0], arr[:, 2], opt.drift_outlier_px)
    if fit is None:
        return None
    slope, intercept, keep = fit
    if int(np.count_nonzero(keep)) < opt.drift_min_tiles:
        return None
    kept_rows = arr[keep, 0]
    # Two thirds of the frame must be covered, or the slope extrapolates too far.
    if float(kept_rows.max() - kept_rows.min()) < 2.0 * h / 3.0 - side:
        return None
    dy_per_row = slope * np.arange(h, dtype=np.float64) + intercept
    if float(np.ptp(dy_per_row)) < opt.drift_min_range_px:
        return None
    dx = float(np.median(arr[keep, 1]))
    if should_snap_to_integer(dx, 0.0, eps=opt.snap_frac_eps):
        dx = float(round(dx))
    logger.info(
        "registration: ME row drift dx=%.2f dy(row)=%.2f..%.2f from %d/%d tiles",
        dx,
        float(dy_per_row[0]),
        float(dy_per_row[-1]),
        int(np.count_nonzero(keep)),
        len(origins),
    )
    return line_warp_from_profile(dx, dy_per_row)


def estimate_banded(
    reference: np.ndarray,
    moving: np.ndarray,
    *,
    options: RegistrationOptions | None = None,
    phase_fn=None,
) -> RegistrationResult:
    """Multi-Pass row-banded registration (production default)."""
    mode = RegistrationMode.MULTI_PASS
    opt = options or RegistrationOptions()
    ref = np.asarray(reference)
    mov = np.asarray(moving)
    if mov.size == 0 or ref.shape[:2] != mov.shape[:2]:
        return _zero_result(mode, reason="shape mismatch", failure=RegistrationFailure.SHAPE_MISMATCH)

    profile = band_shift_profile(ref, mov, options=opt, phase_fn=phase_fn)
    if profile is None:
        return estimate_translation(ref, mov, mode, options=opt)

    dx, dy_per_row = profile
    if abs(dx) < 1e-9 and float(np.max(np.abs(dy_per_row))) < 1e-9:
        return RegistrationResult(
            transform=line_warp_from_profile(0.0, dy_per_row * 0.0),
            mode=mode,
            accepted=True,
            integer_exact=True,
            reason="zero",
        )

    transform = line_warp_from_profile(dx, dy_per_row)
    logger.info(
        "registration: banded dx=%.2f dy(row)=%.2f..%.2f (slope=%.4f px/row)",
        dx,
        float(dy_per_row[0]),
        float(dy_per_row[-1]),
        (float(dy_per_row[-1]) - float(dy_per_row[0])) / max(1, ref.shape[0] - 1),
    )
    return RegistrationResult(
        transform=transform,
        mode=mode,
        accepted=True,
        integer_exact=False,
        quality=RegistrationQuality(overlap_fraction=1.0),
        reason="banded",
    )


def register(
    reference: np.ndarray,
    moving: np.ndarray,
    mode: RegistrationMode,
    *,
    context: RegistrationContext | None = None,
    options: RegistrationOptions | None = None,
    banded: bool | None = None,
) -> RegistrationResult:
    """Estimate a transform mapping ``moving`` → ``reference``."""
    use_banded = banded if banded is not None else (mode is RegistrationMode.MULTI_PASS)
    if use_banded:
        return estimate_banded(reference, moving, options=options)
    return estimate_translation(reference, moving, mode, options=options, context=context)


def apply_transform(
    moving: np.ndarray,
    transform: TranslationTransform | LineWarpTransform,
    *,
    method: str = "linear",
    chunk_rows: int = 512,
) -> np.ndarray:
    """Apply ``transform`` once to ``moving`` (never mutates input)."""
    mov = np.asarray(moving)
    if isinstance(transform, LineWarpTransform):
        return warp_row_shifts(mov, transform.dx, transform.dy_per_row, method=method, chunk_rows=chunk_rows)
    dx, dy = float(transform.dx), float(transform.dy)
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return mov
    return warp_shift(mov, dx, dy, method=method, chunk_rows=chunk_rows)


def register_and_align(
    reference: np.ndarray,
    moving: np.ndarray,
    mode: RegistrationMode,
    *,
    options: RegistrationOptions | None = None,
    banded: bool | None = None,
) -> tuple[np.ndarray, RegistrationResult]:
    """Estimate + apply; on reject returns ``moving`` unchanged."""
    result = register(reference, moving, mode, options=options, banded=banded)
    if not result.accepted:
        return np.asarray(moving), result
    chunk = (options or RegistrationOptions()).warp_chunk_rows
    aligned = apply_transform(moving, result.transform, method="linear", chunk_rows=chunk)
    return aligned, result
