# SPDX-License-Identifier: GPL-3.0-or-later
"""GL128 full-window / PPI-ladder scan geometry (Lab ``preview_safe`` parity).

Both profiles pin ``LINCNT`` to a value the captures prove safe for the second
feed they use. ``LINCNT`` is in ASIC-dpi units (four per output line), so a
fixed physical height needs a different LINCNT at every PPI.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal

from pyopticfilm.scan.geometry import ScanGeometry, compute_geometry

Area = tuple[float, float, float, float]
GeometryProfile = Literal["preview_safe", "ppi_ladder"]

NATIVE_DPI = 7200
MM_PER_INCH = 25.4
#: Fixed Prescan DPI (full window). Independent of the Scan-tab DPI picker.
PRESCAN_DPI = 1200


def is_gl128_opticfilm(model: Any) -> bool:
    """True for GL128 OpticFilm models (8200i SE, 8100 V2, 8300i SE)."""
    asic = getattr(model, "asic", None)
    pid = getattr(model, "usb_product_id", None)
    if str(asic or "") == "GL128":
        return True
    try:
        return int(pid) in {0x1825, 0x1824, 0x181F}
    except (TypeError, ValueError):
        return False


def _window_end_steps(model: Any) -> int:
    return int(getattr(model, "scan_window_end_steps", 0) or getattr(model, "feed_to_scan_bottom_steps", 0))


def y1_for_feed2(model: Any, feed2: int) -> float:
    """Invert feed_to_scan_steps_for_area so a feed2 maps back to ``y1``."""
    top = int(model.feed_to_scan_top_steps)
    end = _window_end_steps(model)
    if end == top:
        return 0.0
    y1 = (int(feed2) - top) / float(end - top)
    return max(0.0, min(1.0, y1))


def _feed2_for(model: Any, y1: float) -> int:
    feed_fn = getattr(model, "feed_to_scan_steps_for_area", None)
    if callable(feed_fn):
        return int(feed_fn((0.0, y1, 1.0, 1.0)))
    return int(getattr(model, "feed_to_scan_steps", 0) or 0)


def _travel_mm(model: Any, lincnt: int, dpi: int) -> float:
    fn = getattr(model, "travel_mm_for_lincnt", None)
    if callable(fn):
        return float(fn(lincnt, dpi))
    return lincnt * MM_PER_INCH / NATIVE_DPI


def preview_safe_scan_area(
    model: Any,
    dpi: int,
    *,
    y1: float = 0.0,
) -> tuple[Area, dict[str, Any]]:
    """Full-width crop from ``y1`` to the end of the scan window (Lab Full window).

    Default ``y1=0`` maps to preview-top ``feed2`` (13128 on SE): 25.59 mm of
    travel, ``LINCNT=4836`` at 1200 dpi.
    """
    y1 = max(0.0, min(1.0, float(y1)))
    feed2 = _feed2_for(model, y1)

    max_fn = getattr(model, "max_lincnt_for", None)
    max_lincnt = int(max_fn(feed2, dpi)) if callable(max_fn) else 0
    if max_lincnt <= 0:
        max_lincnt = 4836

    travel_mm = _travel_mm(model, max_lincnt, dpi)
    y_size = float(model.y_size_ta_mm)
    y2 = min(1.0, max(y1 + 1e-6, y1 + travel_mm / y_size))
    area: Area = (0.0, y1, 1.0, y2)

    meta = {
        "profile": "preview_safe",
        "feed2": feed2,
        "max_lincnt": max_lincnt,
        "target_lincnt": max_lincnt,
        "oversample": int(model.oversample_for(dpi)),
        "y2": y2,
        "travel_mm": round(travel_mm, 3),
        "dpi": int(dpi),
    }
    return area, meta


def ladder_scan_area(model: Any, dpi: int) -> tuple[Area, dict[str, Any]]:
    """Session-13 PPI-ladder crop (model's own ``ladder_feed2_steps`` / capture LINCNT for ``dpi``)."""
    feed2 = int(getattr(model, "ladder_feed2_steps", 13560))
    lincnt_fn = getattr(model, "ladder_lincnt_for", None)
    if callable(lincnt_fn):
        target_lincnt = int(lincnt_fn(dpi))
    else:
        table = dict(getattr(model, "ladder_lincnt_by_dpi", {}) or {})
        target_lincnt = int(table.get(int(dpi), table.get(1800, 6868)))

    y1 = y1_for_feed2(model, feed2)
    travel_mm = _travel_mm(model, target_lincnt, dpi)
    y_size = float(model.y_size_ta_mm)
    y2 = min(1.0, max(y1 + 1e-6, y1 + travel_mm / y_size))
    area: Area = (0.0, y1, 1.0, y2)

    feed_fn = getattr(model, "feed_to_scan_steps_for_area", None)
    actual_feed2 = int(feed_fn(area)) if callable(feed_fn) else feed2

    max_fn = getattr(model, "max_lincnt_for", None)
    max_lincnt = int(max_fn(actual_feed2, dpi)) if callable(max_fn) else 0

    meta = {
        "profile": "ppi_ladder",
        "feed2": actual_feed2,
        "max_lincnt": max_lincnt or target_lincnt,
        "target_lincnt": target_lincnt,
        "oversample": int(model.oversample_for(dpi)),
        "y2": y2,
        "travel_mm": round(travel_mm, 3),
        "dpi": int(dpi),
    }
    return area, meta


def apply_target_lincnt(geometry: ScanGeometry, target_lincnt: int) -> ScanGeometry:
    """Force image ``LINCNT`` / USB line count to a capture table value."""
    target = int(target_lincnt)
    per_line = max(1, int(geometry.lincnt_per_line or 1))
    halved = bool(geometry.register_lincnt and geometry.optical_line_count * 2 == geometry.register_lincnt)
    return replace(
        geometry,
        register_lincnt=target,
        optical_line_count=target // 2 if halved else target,
        lines=max(1, target // per_line),
    )


def bringup_scan_geometry(
    model: Any,
    dpi: int,
    *,
    profile: GeometryProfile = "preview_safe",
) -> tuple[ScanGeometry, dict[str, Any]]:
    """Build SE geometry for Full window (``preview_safe``) or PPI ladder."""
    if profile == "ppi_ladder":
        area, meta = ladder_scan_area(model, dpi)
    else:
        area, meta = preview_safe_scan_area(model, dpi, y1=0.0)

    # Do not shrink X to fit a shading table. SF Full window at 1800 is 2592 px;
    # clamping it made USB line length disagree with the ASIC and sheared the
    # frame into a diamond. The shading table covers the acquired columns instead.
    # Dummy on the ENDPIXEL side is dropped after decode (``usb_end_drop``).
    geometry = compute_geometry(dpi, model=model, area=area)
    geometry = apply_target_lincnt(geometry, int(meta["target_lincnt"]))

    meta = {
        **meta,
        "geometry_lincnt": geometry.lincnt_register,
        "optical_line_count": geometry.optical_line_count,
        "area": geometry.area,
        "pixels": geometry.pixels,
    }
    return geometry, meta


def _clamp_area_to_shading_table(model: Any, dpi: int, area: Area) -> Area:
    """Deprecated: shrinking the image window to the AHB table shears USB lines.

    Kept for unit tests that assert the old ratio math. The shading table is
    measured and uploaded at the acquire width instead.
    """
    from pyopticfilm.scan.calib_gl128 import (
        shading_acquire_width,
        shading_width_for_resolution,
    )

    probe = compute_geometry(dpi, model=model, area=area)
    table_n = shading_width_for_resolution(dpi)
    n = shading_acquire_width(
        strpixel=probe.pixel_startx,
        endpixel=probe.pixel_endx,
        dpiset=probe.register_dpiset,
        optical_resolution=int(getattr(model, "optical_resolution", 7200)),
    )
    if n <= table_n:
        return area
    x1, y1, x2, y2 = area
    width = max(1e-9, x2 - x1)
    return (x1, y1, x1 + width * (table_n / n), y2)


def clamp_area(area: Area) -> Area:
    """Clamp a normalized TA rect to ``0..1`` with a tiny positive size."""
    x1, y1, x2, y2 = (float(v) for v in area)
    x1 = max(0.0, min(1.0, x1))
    y1 = max(0.0, min(1.0, y1))
    x2 = max(0.0, min(1.0, x2))
    y2 = max(0.0, min(1.0, y2))
    if x2 <= x1:
        x2 = min(1.0, x1 + 1e-3)
    if y2 <= y1:
        y2 = min(1.0, y1 + 1e-3)
    return (x1, y1, x2, y2)


def image_crop_to_scan_area(model: Any, crop_norm: Area) -> Area:
    """Map Prescan crop widget coords → TA ``area`` for ``compute_geometry``.

    Scan Lab shows the assembled image after ``mirror_x``. The rubber-band is
    therefore in display space: flip X back to TA so STR/ENDPIXEL match the
    box the user drew.
    """
    x1, y1, x2, y2 = clamp_area(crop_norm)
    if getattr(model, "mirror_x", False):
        x1, x2 = 1.0 - x2, 1.0 - x1
    return clamp_area((x1, y1, x2, y2))


def scan_area_to_image_crop(model: Any, area: Area) -> Area:
    """Map TA ``area`` → Prescan crop widget coords (same involution as :func:`image_crop_to_scan_area`)."""
    return image_crop_to_scan_area(model, area)


def default_frame_crop_norm(model: Any) -> Area:
    """Centered ~35 mm frame crop (session-13 ladder y-extent, full width).

    Returned in **TA / scan** space. Convert with :func:`scan_area_to_image_crop`
    before drawing on the Prescan preview.
    """
    area, _ = ladder_scan_area(model, PRESCAN_DPI)
    return clamp_area(area)


def effective_scan_area(
    model: Any,
    geometry: ScanGeometry,
    requested: Area,
) -> Area:
    """Normalized TA rect actually covered by ``geometry`` (after span snap / LINCNT clamp)."""
    x1, y1, _x2, _y2 = clamp_area(requested)
    x_size = float(model.x_size_ta_mm)
    y_size = float(model.y_size_ta_mm)
    width_px = max(1, int(geometry.pixels) - int(getattr(geometry, "usb_end_drop", 0) or 0))
    width_mm = width_px * MM_PER_INCH / max(1, geometry.asic_dpi)
    eff_x2 = min(1.0, max(x1 + 1e-6, x1 + width_mm / x_size))
    eff_y2 = min(1.0, max(y1 + 1e-6, y1 + geometry.travel_mm / y_size))
    return clamp_area((x1, y1, eff_x2, eff_y2))


def crop_adjustment_message(requested: Area, effective: Area) -> str | None:
    """Human-readable note when optical alignment narrowed the crop."""
    req = clamp_area(requested)
    eff = clamp_area(effective)
    parts: list[str] = []
    if abs(req[2] - eff[2]) > 1e-4:
        parts.append(f"x2 {req[2]:.3f}→{eff[2]:.3f}")
    if abs(req[3] - eff[3]) > 1e-4:
        parts.append(f"y2 {req[3]:.3f}→{eff[3]:.3f}")
    if not parts:
        return None
    return "Crop adjusted (" + ", ".join(parts) + ")"


def crop_scan_geometry(
    model: Any,
    dpi: int,
    area: Area,
) -> tuple[ScanGeometry, dict[str, Any]]:
    """Build geometry for an arbitrary normalized crop inside the TA window.

    ``LINCNT`` is clamped to :meth:`max_lincnt_for` so the motor cannot overrun
    the scan-window end. Optical span alignment stays in ``compute_geometry``.
    """
    area = clamp_area(area)
    requested_area = area
    feed2 = _feed2_for(model, area[1])
    max_fn = getattr(model, "max_lincnt_for", None)
    max_lincnt = int(max_fn(feed2, dpi)) if callable(max_fn) else 0

    geometry = compute_geometry(dpi, model=model, area=area)
    target_before_clamp = int(geometry.lincnt_register)
    target = target_before_clamp
    if max_lincnt > 0 and target > max_lincnt:
        geometry = apply_target_lincnt(geometry, max_lincnt)
        target = max_lincnt

    effective_area = effective_scan_area(model, geometry, requested_area)
    travel_mm = _travel_mm(model, target, dpi)
    meta = {
        "profile": "crop",
        "feed2": feed2,
        "max_lincnt": max_lincnt or target,
        "target_lincnt": target,
        "oversample": int(model.oversample_for(dpi)),
        "y2": area[3],
        "travel_mm": round(travel_mm, 3),
        "dpi": int(dpi),
        "geometry_lincnt": geometry.lincnt_register,
        "optical_line_count": geometry.optical_line_count,
        "area": geometry.area if geometry.area is not None else area,
        "requested_area": requested_area,
        "effective_area": effective_area,
        "lincnt_clamped": target < target_before_clamp,
        "pixels": geometry.pixels,
    }
    return geometry, meta
