# SPDX-License-Identifier: GPL-3.0-or-later
"""Pass registration for multi-pass Plustek USB scans.

The carriage re-homes (AGOHOME) between colour, IR, and ME passes, so secondary
frames can land a few pixels off the reference. Uses the native NumPy
registration engine (:mod:`pyopticfilm.registration`).
"""

from __future__ import annotations

import numpy as np

from pyopticfilm.registration.engine import (
    apply_transform,
    estimate_banded,
    estimate_row_drift,
    estimate_shift_field,
    estimate_translation,
)
from pyopticfilm.registration.models import band_shift_profile
from pyopticfilm.registration.phase import phase_correlate_shift
from pyopticfilm.registration.resample import (
    quadratic_field,
    row_shift_touch_mask,
    row_shift_var_scale,
    warp_integer,
    warp_row_shifts,
    warp_x_field,
    x_field_var_scale,
)
from pyopticfilm.registration.resample import warp_shift as _warp_shift_impl
from pyopticfilm.registration.types import (
    FieldWarpTransform,
    LineWarpTransform,
    RegistrationMode,
    RegistrationOptions,
    Transform,
    TranslationTransform,
)

_ALIGN_PROBE_WIDTH = 1024
_REFINE_ROI_SIDE = 512
_REFINE_MAX_RESIDUAL = 2.0
_ALIGN_MIN_RESPONSE = 0.03
_ALIGN_SANITY_MAX_FRAC = 0.5
_ALIGN_BAND_COUNT = 8
_ALIGN_BAND_MIN_HEIGHT = 256
_ALIGN_BAND_OUTLIER_PX = 8.0
# ME long-pass kernel. Row drift sweeps every sub-pixel phase down the frame, so
# softness at half-pixel rows shows as bands of detail: on a real 1800 dpi pair
# fine detail there was 64% of the short with bilinear, 75% cubic, 86% lanczos4.
_ME_RESAMPLER = "lanczos4"
# Samples around a clipped reading that count as ringing, in px. Past the two taps
# at the reading the error is tens of DN against 150-300 of short-pass noise there.
_ME_RING_REACH = 0

Shift2D = tuple[float, float]


def native_align_available() -> bool:
    """Return True — NumPy registration is always available."""
    return True


def warn_if_align_unavailable(context: str = "multi-pass") -> bool:
    """Always available; kept for call-site compatibility. Returns True."""
    del context
    return True


def _default_options() -> RegistrationOptions:
    return RegistrationOptions(
        coarse_target_px=_ALIGN_PROBE_WIDTH,
        refine_roi_side=_REFINE_ROI_SIDE,
        refine_max_residual=_REFINE_MAX_RESIDUAL,
        min_response=_ALIGN_MIN_RESPONSE,
        sanity_max_frac=_ALIGN_SANITY_MAX_FRAC,
        band_count=_ALIGN_BAND_COUNT,
        band_min_height=_ALIGN_BAND_MIN_HEIGHT,
        band_outlier_px=_ALIGN_BAND_OUTLIER_PX,
    )


def _phase_correlate_shift(ref: np.ndarray, mov: np.ndarray, *, scale: float) -> tuple[float, float, float]:
    """Returns ``(dx, dy, response)`` — peak-sharpness trust signal."""
    return phase_correlate_shift(ref, mov, scale=scale)


def _warp_shift(mov: np.ndarray, dx: float, dy: float) -> np.ndarray:
    """Translate ``mov`` by ``(dx, dy)`` (bilinear / integer gather)."""
    return _warp_shift_impl(mov, dx, dy, method="linear")


def _band_shift_profile(
    reference: np.ndarray, moving: np.ndarray, *, n_bands: int = _ALIGN_BAND_COUNT
) -> tuple[float, np.ndarray] | None:
    """Per-row-band profile; see :func:`pyopticfilm.registration.models.band_shift_profile`."""
    opt = _default_options()
    if n_bands != opt.band_count:
        opt = RegistrationOptions(
            coarse_target_px=opt.coarse_target_px,
            refine_roi_side=opt.refine_roi_side,
            refine_max_residual=opt.refine_max_residual,
            min_response=opt.min_response,
            sanity_max_frac=opt.sanity_max_frac,
            band_count=n_bands,
            band_min_height=opt.band_min_height,
            band_outlier_px=opt.band_outlier_px,
        )
    return band_shift_profile(
        reference,
        moving,
        options=opt,
        phase_fn=_phase_correlate_shift,
    )


def estimate_pass_shift(reference: np.ndarray, moving: np.ndarray) -> Shift2D:
    """Estimate ``(dx, dy)`` (sub-pixel) for ``moving`` → ``reference``."""
    result = estimate_translation(
        reference,
        moving,
        RegistrationMode.MULTI_EXPOSURE,
        options=_default_options(),
    )
    if not result.accepted:
        return (0.0, 0.0)
    return result.shift


def estimate_me_transform(reference: np.ndarray, moving: np.ndarray) -> Transform:
    """Registration of an ME pass: translation, then row drift and a shift field if any."""
    options = _default_options()
    result = estimate_translation(
        reference,
        moving,
        RegistrationMode.MULTI_EXPOSURE,
        options=options,
    )
    if not result.accepted:
        return TranslationTransform(0.0, 0.0)
    drift = estimate_row_drift(reference, moving, result.shift, options=options)
    return _add_shift_field(reference, moving, result.transform if drift is None else drift, options)


def _add_shift_field(
    reference: np.ndarray, moving: np.ndarray, transform: Transform, options: RegistrationOptions
) -> Transform:
    """``transform`` with the smooth shift left after it, or as it is when there is none.

    Measured on green after the warp, so tiles report what the warp leaves. A dy
    that varies along a row would need a second vertical pass; dy down the frame
    covers it on real pairs, so the field's dy is taken at mid-column.
    """
    ref = np.asarray(reference)
    mov = np.asarray(moving)
    h, w = ref.shape[:2]
    if isinstance(transform, LineWarpTransform):
        dx, dy = float(transform.dx), np.asarray(transform.dy_per_row, dtype=np.float64)
    else:
        dx, dy = float(transform.dx), np.full(h, float(transform.dy))
    plane = (lambda a: a[..., 1]) if ref.ndim == 3 else (lambda a: a)
    aligned = warp_row_shifts(plane(mov), dx, dy, method=_ME_RESAMPLER)
    field = estimate_shift_field(plane(ref), aligned, options=options)
    if field is None:
        return transform
    coef_x, coef_y = field
    rows = np.arange(h, dtype=np.float64)
    return FieldWarpTransform(
        dx=dx,
        dy_per_row=dy + quadratic_field(coef_y, np.asarray((w - 1) / 2.0), rows, w, h),
        dx_coef=coef_x,
    )


def transform_mean_shift(transform: Transform) -> Shift2D:
    """Representative ``(dx, dy)`` of a transform, for logs and debug fields."""
    if isinstance(transform, LineWarpTransform):
        return float(transform.dx), float(np.mean(transform.dy_per_row))
    return float(transform.dx), float(transform.dy)


def apply_me_transform(
    moving: np.ndarray,
    transform: Transform,
    *,
    clip_level: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Warp an ME pass with :data:`_ME_RESAMPLER`.

    Returns ``(aligned, var_scale)``: the warp and its per-row noise variance
    factor (see :func:`~pyopticfilm.registration.resample.row_shift_var_scale`).

    The kernel rings at a hard clip edge, which can pull a clipped reading below
    the clip and pass it as valid. With ``clip_level``, anything within the
    kernel's reach of a reading at that level is written at full scale, so the
    merge's clip confidence rejects it.
    """
    mov = np.asarray(moving)
    h, w = mov.shape[:2]
    if isinstance(transform, FieldWarpTransform):
        dx, dy = float(transform.dx), np.asarray(transform.dy_per_row, dtype=np.float64)
        aligned = warp_x_field(
            warp_row_shifts(mov, 0.0, dy, method=_ME_RESAMPLER), dx, transform.dx_coef, method=_ME_RESAMPLER
        )
        var_scale = row_shift_var_scale(0.0, dy, method=_ME_RESAMPLER) * x_field_var_scale(
            dx, transform.dx_coef, w, h, method=_ME_RESAMPLER
        )
        if clip_level is not None and np.issubdtype(mov.dtype, np.integer):
            clipped = mov >= clip_level
            if np.any(clipped):
                grid = quadratic_field(transform.dx_coef, np.linspace(0.0, w - 1, 17), np.linspace(0.0, h - 1, 17)[:, None], w, h)
                mid = dx + float(np.median(grid))
                touched = row_shift_touch_mask(clipped, mid, dy, reach=_ME_RING_REACH)
                aligned[touched] = np.iinfo(mov.dtype).max
        return aligned, var_scale
    if isinstance(transform, LineWarpTransform):
        dx, dy = float(transform.dx), np.asarray(transform.dy_per_row, dtype=np.float64)
    else:
        dx, dy = float(transform.dx), np.full(h, float(transform.dy))
        if abs(dx - round(dx)) < 1e-6 and abs(dy[0] - round(dy[0])) < 1e-6:
            return warp_integer(mov, round(dx), round(float(dy[0]))), np.ones(h)
    aligned = warp_row_shifts(mov, dx, dy, method=_ME_RESAMPLER)
    var_scale = row_shift_var_scale(dx, dy, method=_ME_RESAMPLER)
    if clip_level is not None and np.issubdtype(mov.dtype, np.integer):
        clipped = mov >= clip_level
        if np.any(clipped):
            touched = row_shift_touch_mask(clipped, dx, dy, reach=_ME_RING_REACH)
            aligned[touched] = np.iinfo(mov.dtype).max
    return aligned, var_scale


def align_pass_to_reference(
    reference: np.ndarray,
    moving: np.ndarray,
    shift: tuple[float, float] | tuple[int, int] | None = None,
) -> tuple[np.ndarray, Shift2D]:
    """Align ``moving`` onto ``reference``; return aligned array and shift used."""
    ref = np.asarray(reference)
    mov = np.asarray(moving)
    if mov.size == 0 or ref.shape[:2] != mov.shape[:2]:
        return mov, (0.0, 0.0)
    if shift is None:
        shift = estimate_pass_shift(ref, mov)
    dx, dy = float(shift[0]), float(shift[1])
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return mov, (0.0, 0.0)
    return _warp_shift(mov, dx, dy), (dx, dy)


def align_pass_to_reference_banded(reference: np.ndarray, moving: np.ndarray) -> tuple[np.ndarray, Shift2D]:
    """Row-banded Multi-Pass alignment (production default)."""
    ref = np.asarray(reference)
    mov = np.asarray(moving)
    if mov.size == 0 or ref.shape[:2] != mov.shape[:2]:
        return mov, (0.0, 0.0)
    result = estimate_banded(
        ref,
        mov,
        options=_default_options(),
        phase_fn=_phase_correlate_shift,
    )
    if not result.accepted:
        return align_pass_to_reference(ref, mov)
    aligned = apply_transform(mov, result.transform, method="linear")
    return aligned, result.shift


def align_ir_to_rgb(rgb: np.ndarray, ir: np.ndarray) -> np.ndarray:
    """IR→RGB alignment via structural / luminance registration."""
    return align_ir_to_rgb_result(rgb, ir)[0]


def align_ir_to_rgb_result(rgb: np.ndarray, ir: np.ndarray) -> tuple[np.ndarray, Shift2D]:
    """Like :func:`align_ir_to_rgb` but also returns the shift actually applied."""
    result = estimate_translation(
        rgb,
        ir,
        RegistrationMode.RGB_IR,
        options=_default_options(),
    )
    if not result.accepted:
        # Fall back to plain luminance translation (historical path).
        return align_pass_to_reference(rgb, ir)
    return apply_transform(ir, result.transform, method="linear"), result.shift
