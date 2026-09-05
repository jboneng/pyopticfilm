# SPDX-License-Identifier: GPL-3.0-or-later
"""Pass registration for multi-pass Plustek USB scans.

The carriage re-homes (AGOHOME) between colour, IR, and ME passes, so secondary
frames can land a few pixels off the reference. Uses phase correlation when
OpenCV is available (sub-pixel shift + warp); otherwise returns zero shift.
"""

from __future__ import annotations

import numpy as np

from pyopticfilm.logging import get_logger

logger = get_logger(__name__)

_ALIGN_PROBE_WIDTH = 1024
_REFINE_ROI_SIDE = 2048
_REFINE_MAX_RESIDUAL = 2.0

# Trust gate for a phase-correlation result: cv2.phaseCorrelate's own
# peak-sharpness score, not shift magnitude. Real hardware drift on this
# GL128 platform is routinely 20-40px+ (jboneng/pyopticfilm#33's own
# benchmark measured up to ~42px) — a magnitude-based cutoff rejects real,
# correctable shifts no matter how it's scaled to the frame. A weak/
# ambiguous correlation peak (low response) is what actually indicates an
# untrustworthy result — e.g. the bogus ~46px lock onto low-texture content
# noted in jboneng/pyopticfilm#14 — regardless of the shift's size.
_ALIGN_MIN_RESPONSE = 0.05
# Generous absolute sanity ceiling on top of the response gate — catches
# only a truly pathological result (e.g. a shift larger than the frame
# itself), not a plausible-but-large real one.
_ALIGN_SANITY_MAX_FRAC = 0.5

# Row-banded alignment (see align_pass_to_reference_banded): a tall pass can
# drift progressively along the feed axis rather than by one constant
# offset, so a single whole-frame shift under- or over-corrects depending
# on how far a region sits from wherever the dominant texture anchored the
# estimate.
_ALIGN_BAND_COUNT = 8
_ALIGN_BAND_MIN_HEIGHT = 256  # below this, banding has too little signal per band
_ALIGN_BAND_OUTLIER_PX = 8.0

Shift2D = tuple[float, float]

_cv2_warned = False


def opencv_align_available() -> bool:
    """Return True when sub-pixel pass registration is available."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return True


def warn_if_align_unavailable(context: str = "multi-pass") -> bool:
    """Log once at WARNING when OpenCV is missing; return False."""
    global _cv2_warned
    if opencv_align_available():
        return True
    if not _cv2_warned:
        logger.warning(
            "pass_align: OpenCV unavailable — %s registration disabled (zero shift). "
            "Install opencv-python-headless (lab group) for ME/IR alignment.",
            context,
        )
        _cv2_warned = True
    return False


def _luminance_plane(image: np.ndarray, *, probe_w: int = _ALIGN_PROBE_WIDTH) -> np.ndarray:
    """Downsampled luma for coarse phase correlation (INTER_AREA when OpenCV present)."""
    arr = np.asarray(image)
    h, w = arr.shape[:2]
    scale = max(1.0, w / probe_w)
    if scale <= 1.0:
        if arr.ndim == 3:
            return arr.astype(np.float32).mean(axis=2)
        return arr.astype(np.float32)
    sz = (probe_w, max(1, round(h / scale)))
    try:
        import cv2
    except ImportError:
        sy = max(1, round(h / scale))
        sx = max(1, round(w / scale))
        arr = arr[::sy, ::sx]
        if arr.ndim == 3:
            return arr.astype(np.float32).mean(axis=2)
        return arr.astype(np.float32)
    if arr.ndim == 3:
        small = cv2.resize(arr, sz, interpolation=cv2.INTER_AREA)
        return small.astype(np.float32).mean(axis=2)
    return cv2.resize(arr.astype(np.float32), sz, interpolation=cv2.INTER_AREA)


def _roi_luminance(image: np.ndarray, y0: int, x0: int, rh: int, rw: int) -> np.ndarray:
    roi = np.asarray(image)[y0 : y0 + rh, x0 : x0 + rw]
    if roi.ndim == 3:
        return roi.astype(np.float32).mean(axis=2)
    return roi.astype(np.float32)


def _phase_correlate_shift(
    ref: np.ndarray, mov: np.ndarray, *, scale: float
) -> tuple[float, float, float]:
    """Returns ``(dx, dy, response)`` — response is cv2.phaseCorrelate's own
    peak-sharpness score (0..1ish), the trust signal used by both the
    whole-frame guard and the row-banded per-band filter."""
    try:
        import cv2
    except ImportError:
        return (0.0, 0.0, 0.0)
    if ref.size == 0 or mov.size == 0 or ref.shape != mov.shape:
        return (0.0, 0.0, 0.0)
    h, w = ref.shape[:2]
    win = cv2.createHanningWindow((w, h), cv2.CV_32F)
    (dx, dy), resp = cv2.phaseCorrelate(
        np.ascontiguousarray(mov), np.ascontiguousarray(ref), win
    )
    return float(dx * scale), float(dy * scale), float(resp)


def _refine_pass_shift(
    reference: np.ndarray,
    moving: np.ndarray,
    coarse: Shift2D,
) -> Shift2D:
    """Second phaseCorrelate on a central ROI after coarse alignment."""
    if not opencv_align_available():
        return coarse
    h, w = reference.shape[:2]
    if h * w < 512 * 512:
        return coarse
    dx0, dy0 = coarse
    if abs(dx0) < 1e-9 and abs(dy0) < 1e-9:
        warped = moving
    else:
        warped = _warp_shift(moving, dx0, dy0)
    rh = min(_REFINE_ROI_SIDE, h)
    rw = min(_REFINE_ROI_SIDE, w)
    y0 = max(0, (h - rh) // 2)
    x0 = max(0, (w - rw) // 2)
    ref_lum = _roi_luminance(reference, y0, x0, rh, rw)
    mov_lum = _roi_luminance(warped, y0, x0, rh, rw)
    ddx, ddy, resp = _phase_correlate_shift(ref_lum, mov_lum, scale=1.0)
    if resp < _ALIGN_MIN_RESPONSE or max(abs(ddx), abs(ddy)) > _REFINE_MAX_RESIDUAL:
        return coarse
    return dx0 + ddx, dy0 + ddy


def _shift_is_pathological(dx: float, dy: float, full_w: int, full_h: int) -> bool:
    """Last-resort absolute sanity ceiling — a shift larger than half the
    frame's own dimension isn't a plausible pass-to-pass drift regardless of
    correlation confidence. Judges each axis against its own dimension (a
    tall crop/strip window can have a legitimately large dy relative to its
    narrow width, and vice versa for a wide window)."""
    guard_x = _ALIGN_SANITY_MAX_FRAC * full_w
    guard_y = _ALIGN_SANITY_MAX_FRAC * full_h
    return abs(dx) > guard_x or abs(dy) > guard_y


def estimate_pass_shift(reference: np.ndarray, moving: np.ndarray) -> Shift2D:
    """Estimate ``(dx, dy)`` (sub-pixel when OpenCV is available) for ``moving`` → ``reference``.

    Trusts the phase-correlation peak's own sharpness (response), not shift
    magnitude, to decide whether to use the result — see
    ``_ALIGN_MIN_RESPONSE``. Real hardware drift on this platform is
    routinely large (tens of px); a magnitude-based cutoff rejected real,
    correctable shifts no matter how it was scaled to the frame.
    """
    if not opencv_align_available():
        warn_if_align_unavailable("pass")
        return (0.0, 0.0)
    ref = np.asarray(reference)
    mov = np.asarray(moving)
    if ref.shape[:2] != mov.shape[:2]:
        return (0.0, 0.0)
    full_h, full_w = ref.shape[:2]
    ref_lum = _luminance_plane(ref)
    mov_lum = _luminance_plane(mov)
    scale = max(1.0, full_w / _ALIGN_PROBE_WIDTH)
    dx, dy, resp = _phase_correlate_shift(ref_lum, mov_lum, scale=scale)
    if resp < _ALIGN_MIN_RESPONSE:
        logger.warning(
            "pass_align: shift (%.2f, %.2f) has low peak response (%.3f) — using unaligned",
            dx,
            dy,
            resp,
        )
        return (0.0, 0.0)
    if _shift_is_pathological(dx, dy, full_w, full_h):
        logger.warning(
            "pass_align: shift (%.2f, %.2f) exceeds sanity ceiling — using unaligned",
            dx,
            dy,
        )
        return (0.0, 0.0)
    dx, dy = _refine_pass_shift(ref, mov, (dx, dy))
    if _shift_is_pathological(dx, dy, full_w, full_h):
        logger.warning(
            "pass_align: refined shift (%.2f, %.2f) exceeds sanity ceiling — using unaligned",
            dx,
            dy,
        )
        return (0.0, 0.0)
    return (dx, dy)


def _fill_shift_border(out: np.ndarray, dx: float, dy: float) -> np.ndarray:
    """Replace destination strips that came from out-of-bounds / edge replicate.

    ``BORDER_REPLICATE`` and clipped index gathers copy the extreme edge column
    into a strip of width ``ceil(|dx|)``. When that column is decode padding,
    IR flatten already blew it up — replicate widens it into a bright band.
    Fill from the first fully valid interior column instead.
    """
    h, w = out.shape[:2]
    bx = int(np.ceil(abs(float(dx))))
    by = int(np.ceil(abs(float(dy))))
    if bx <= 0 and by <= 0:
        return out
    result = np.array(out, copy=True)
    if bx > 0 and bx < w:
        if dx > 0:
            # Content shifted right: left strip is invalid.
            src = result[:, bx : bx + 1]
            result[:, :bx] = src
        elif dx < 0:
            src = result[:, w - bx - 1 : w - bx]
            result[:, w - bx :] = src
    if by > 0 and by < h:
        if dy > 0:
            src = result[by : by + 1, :]
            result[:by, :] = src
        elif dy < 0:
            src = result[h - by - 1 : h - by, :]
            result[h - by :, :] = src
    return result


def _warp_shift(mov: np.ndarray, dx: float, dy: float) -> np.ndarray:
    """Translate ``mov`` by ``(dx, dy)`` with sub-pixel resampling when possible."""
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return mov
    h, w = mov.shape[:2]
    # Integer path: cheap index gather (exact, no OpenCV).
    if abs(dx - round(dx)) < 1e-6 and abs(dy - round(dy)) < 1e-6:
        idx = round(dx)
        idy = round(dy)
        x_idx = np.clip(np.arange(w) + idx, 0, w - 1)
        y_idx = np.clip(np.arange(h) + idy, 0, h - 1)
        if mov.ndim == 3:
            out = mov[y_idx][:, x_idx, :]
        else:
            out = mov[y_idx][:, x_idx]
        return _fill_shift_border(out, float(idx), float(idy))
    try:
        import cv2
    except ImportError:
        idx = round(dx)
        idy = round(dy)
        x_idx = np.clip(np.arange(w) + idx, 0, w - 1)
        y_idx = np.clip(np.arange(h) + idy, 0, h - 1)
        if mov.ndim == 3:
            out = mov[y_idx][:, x_idx, :]
        else:
            out = mov[y_idx][:, x_idx]
        return _fill_shift_border(out, float(idx), float(idy))
    # OpenCV: +x is right, +y is down — same as phaseCorrelate / our index convention.
    # CONSTANT+0 then interior fill avoids replicating a hot/dark padding column.
    matrix = np.array([[1.0, 0.0, dx], [0.0, 1.0, dy]], dtype=np.float32)
    if mov.ndim == 3:
        planes = [
            cv2.warpAffine(
                mov[:, :, c],
                matrix,
                (w, h),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
            for c in range(mov.shape[2])
        ]
        out = np.stack(planes, axis=2)
    else:
        out = cv2.warpAffine(
            mov,
            matrix,
            (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
    if np.issubdtype(mov.dtype, np.integer):
        out = np.clip(np.rint(out), 0, np.iinfo(mov.dtype).max).astype(mov.dtype)
    else:
        out = out.astype(mov.dtype, copy=False)
    return _fill_shift_border(out, dx, dy)


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


def _band_shift_profile(
    reference: np.ndarray, moving: np.ndarray, *, n_bands: int = _ALIGN_BAND_COUNT
) -> tuple[float, np.ndarray] | None:
    """Per-row-band ``(dx, dy)`` estimate fit to a per-row ``dy(y)`` line.

    Splits the frame into ``n_bands`` horizontal strips, phase-correlates
    each independently, and fits a line across the trustworthy bands' row
    centers instead of trusting one whole-frame shift. Bands below
    ``_ALIGN_MIN_RESPONSE`` peak sharpness (too little local texture to
    trust) are dropped before fitting; one more outlier band is dropped and
    the line refit if the initial fit's worst residual exceeds
    ``_ALIGN_BAND_OUTLIER_PX``, so a single aliased/low-texture band can't
    skew the whole profile.

    Returns ``(dx, dy_per_row)`` — a single representative dx (this
    hardware's drift is near-pure Y-axis; see the module docstring) and a
    length-``h`` array of the fitted dy for every row — or ``None`` when the
    frame is too short to band usefully, or too few bands produced a
    trustworthy peak to fit a line at all. Callers should fall back to
    :func:`estimate_pass_shift` in that case.
    """
    if not opencv_align_available():
        return None
    ref = np.asarray(reference)
    mov = np.asarray(moving)
    h, w = ref.shape[:2]
    if h < n_bands * _ALIGN_BAND_MIN_HEIGHT:
        return None
    band_h = h // n_bands
    scale = max(1.0, w / _ALIGN_PROBE_WIDTH)
    centers: list[float] = []
    dxs: list[float] = []
    dys: list[float] = []
    for i in range(n_bands):
        y0 = i * band_h
        y1 = h if i == n_bands - 1 else y0 + band_h
        ref_lum = _luminance_plane(ref[y0:y1])
        mov_lum = _luminance_plane(mov[y0:y1])
        dx, dy, resp = _phase_correlate_shift(ref_lum, mov_lum, scale=scale)
        if resp < _ALIGN_MIN_RESPONSE:
            continue  # too little texture in this band to trust its peak
        centers.append((y0 + y1) / 2.0)
        dxs.append(dx)
        dys.append(dy)
    if len(centers) < max(3, n_bands // 2):
        return None
    c = np.asarray(centers, dtype=np.float64)
    d = np.asarray(dys, dtype=np.float64)
    slope, intercept = np.polyfit(c, d, 1)
    resid = np.abs(d - (slope * c + intercept))
    if len(c) > 3 and resid.max() > _ALIGN_BAND_OUTLIER_PX:
        keep = resid < resid.max()
        if keep.sum() >= 3:
            slope, intercept = np.polyfit(c[keep], d[keep], 1)
    dy_per_row = slope * np.arange(h, dtype=np.float64) + intercept
    # Sanity floor: a fitted drift spanning more than half the frame's own
    # height across the pass isn't a physically plausible feed drift — more
    # likely a bad fit through mostly-untrustworthy bands. Fall back rather
    # than apply it.
    if float(np.max(dy_per_row) - np.min(dy_per_row)) > 0.5 * h:
        return None
    return float(np.median(dxs)), dy_per_row


def _fill_row_shift_border(out: np.ndarray, dx: float, dy_per_row: np.ndarray) -> np.ndarray:
    """Per-row generalization of :func:`_fill_shift_border`.

    Any row whose source (``y - dy_per_row[y]``) sampled outside
    ``[0, h-1]`` pulled in ``BORDER_CONSTANT`` padding from
    :func:`_warp_row_shifts` — replace it with the nearest valid row's
    content instead, same rationale as the constant-shift version.
    """
    h, w = out.shape[:2]
    result = np.array(out, copy=True)
    bx = int(np.ceil(abs(float(dx))))
    if 0 < bx < w:
        if dx > 0:
            result[:, :bx] = result[:, bx : bx + 1]
        else:
            result[:, w - bx :] = result[:, w - bx - 1 : w - bx]
    src_y = np.arange(h, dtype=np.float64) - dy_per_row
    valid_rows = np.flatnonzero((src_y >= 0) & (src_y <= h - 1))
    if valid_rows.size and valid_rows.size < h:
        first_valid, last_valid = valid_rows[0], valid_rows[-1]
        if first_valid > 0:
            result[:first_valid] = result[first_valid]
        if last_valid < h - 1:
            result[last_valid + 1 :] = result[last_valid]
    return result


def _warp_row_shifts(mov: np.ndarray, dx: float, dy_per_row: np.ndarray) -> np.ndarray:
    """Like :func:`_warp_shift` but with a per-row dy instead of one constant
    shift for the whole frame — requires OpenCV (callers only reach this
    once :func:`_band_shift_profile` has already required it)."""
    import cv2

    h, w = mov.shape[:2]
    map_x = np.broadcast_to((np.arange(w, dtype=np.float32) - dx), (h, w)).copy()
    map_y = np.broadcast_to(
        (np.arange(h, dtype=np.float32) - dy_per_row.astype(np.float32))[:, np.newaxis], (h, w)
    ).copy()
    if mov.ndim == 3:
        planes = [
            cv2.remap(
                mov[:, :, c],
                map_x,
                map_y,
                interpolation=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
            for c in range(mov.shape[2])
        ]
        out = np.stack(planes, axis=2)
    else:
        out = cv2.remap(
            mov,
            map_x,
            map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
    if np.issubdtype(mov.dtype, np.integer):
        out = np.clip(np.rint(out), 0, np.iinfo(mov.dtype).max).astype(mov.dtype)
    else:
        out = out.astype(mov.dtype, copy=False)
    return _fill_row_shift_border(out, dx, dy_per_row)


def align_pass_to_reference_banded(
    reference: np.ndarray, moving: np.ndarray
) -> tuple[np.ndarray, Shift2D]:
    """Row-banded generalization of :func:`align_pass_to_reference`.

    Fits a per-row ``dy(y)`` profile (see :func:`_band_shift_profile`)
    instead of one whole-frame rigid shift, so progressive/non-rigid drift
    along a tall pass is corrected across the whole frame instead of only
    wherever the dominant texture happened to anchor a single global
    estimate (real hardware repro: mid-frame content near the anchor came
    out sharp while top/bottom — far from it — still ghosted, even after
    accepting a whole-frame shift).

    Falls back to the whole-frame :func:`align_pass_to_reference` (and its
    existing guard) when the frame is too short to band usefully, or when
    too few bands produced a trustworthy peak to fit a line.

    Returns the warped array and ``(dx, dy_at_center)`` — a representative
    single shift pair for logging/debug display, even though the actual
    per-row correction varies along the frame.
    """
    if not opencv_align_available():
        warn_if_align_unavailable("banded pass")
        return align_pass_to_reference(reference, moving)
    ref = np.asarray(reference)
    mov = np.asarray(moving)
    if mov.size == 0 or ref.shape[:2] != mov.shape[:2]:
        return mov, (0.0, 0.0)
    profile = _band_shift_profile(ref, mov)
    if profile is None:
        return align_pass_to_reference(ref, mov)
    dx, dy_per_row = profile
    h = ref.shape[0]
    if abs(dx) < 1e-9 and float(np.max(np.abs(dy_per_row))) < 1e-9:
        return mov, (0.0, 0.0)
    warped = _warp_row_shifts(mov, dx, dy_per_row)
    dy_lo, dy_hi = float(dy_per_row[0]), float(dy_per_row[-1])
    logger.info(
        "pass_align: banded profile dx=%.2f dy(row)=%.2f..%.2f (slope=%.4f px/row)",
        dx,
        dy_lo,
        dy_hi,
        (dy_hi - dy_lo) / max(1, h - 1),
    )
    return warped, (dx, float(dy_per_row[h // 2]))


def align_ir_to_rgb(rgb: np.ndarray, ir: np.ndarray) -> np.ndarray:
    """Backward-compatible IR→RGB alignment."""
    aligned, _ = align_pass_to_reference(rgb, ir)
    return aligned
