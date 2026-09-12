# SPDX-License-Identifier: GPL-3.0-or-later
"""Open real scan-ready or mock scanners with a recording USB wrapper."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from pyopticfilm.device.protocol import FilmModel
from pyopticfilm.device.select import KNOWN_MODELS, model_for_device, model_is_scan_ready
from pyopticfilm.exceptions import DeviceNotFoundError
from pyopticfilm.scan.bringup import (
    PRESCAN_DPI,
    bringup_scan_geometry,
    clamp_area,
    crop_adjustment_message,
    crop_scan_geometry,
    image_crop_to_scan_area,
    is_gl128_opticfilm,
)
from pyopticfilm.scanner import Scanner
from pyopticfilm.usb.device import UsbDeviceInfo, list_devices
from pyopticfilm.usb.fake import MockScannerTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol
from pyopticfilm.usb.trace import RecordingTransport, UsbTransaction, format_usb_log_line

#: Max TA height fraction for unvalidated non-SE Lab scans (motor safety).
NONSE_SAFE_Y_FRACTION = 0.18
#: Absolute cap on TA travel (mm) for non-SE Lab windows.
NONSE_SAFE_Y_MM = 5.0
#: Warn before Scan when PPI is at least this and the crop would be long.
HIGH_PPI_WARN_DPI = 2400

Area = tuple[float, float, float, float]


@dataclass(frozen=True)
class LabTarget:
    """One combo-box entry: a model, optionally bound to a plugged-in USB device."""

    label: str
    model: FilmModel
    mock: bool = True
    device_id: str | None = None
    #: When True on a real open, set ``Scanner._allow_unvalidated_scan``.
    allow_unvalidated: bool = False
    #: Force planar USB RGB decode (``asic.usb_planar_rgb``) for rainbow bring-up.
    usb_planar: bool = False
    #: Sleep between GL128 image bulk chunk announces (quieter creep; default on).
    quiet_usb_pace: bool = True
    #: Upload slow motor slope for image configure (acoustic A/B).
    slow_image_slope: bool = False


def with_mock_mode(target: LabTarget, mock: bool) -> LabTarget:
    """Copy ``target`` with mock/real selected from the UI checkbox."""
    return replace(target, mock=mock)


def with_hw_override(target: LabTarget, allow_unvalidated: bool) -> LabTarget:
    """Copy ``target`` with the lab HW-gate override from the UI checkbox."""
    return replace(target, allow_unvalidated=bool(allow_unvalidated))


def with_usb_planar(target: LabTarget, usb_planar: bool) -> LabTarget:
    """Copy ``target`` with the Lab planar/chunky decode toggle."""
    return replace(target, usb_planar=bool(usb_planar))


def with_motor_acoustic(
    target: LabTarget,
    *,
    quiet_usb_pace: bool = True,
    slow_image_slope: bool = False,
) -> LabTarget:
    """Copy ``target`` with GL128 motor-noise A/B toggles from the UI."""
    return replace(
        target,
        quiet_usb_pace=bool(quiet_usb_pace),
        slow_image_slope=bool(slow_image_slope),
    )


def apply_lab_hw_override(scanner: Scanner, target: LabTarget) -> None:
    """Unlock scan/home/park for real USB when the lab override is on.

    Does not flip ``model.scan_ready``. No-op for mock opens (``open_fake``
    already sets ``_allow_unvalidated_scan``).
    """
    if target.mock or not target.allow_unvalidated:
        return
    scanner._allow_unvalidated_scan = True


def apply_lab_decode_layout(scanner: Scanner, target: LabTarget) -> None:
    """Set ``asic.usb_planar_rgb`` from the Lab planar toggle."""
    asic = getattr(scanner, "_asic", None)
    if asic is None:
        return
    asic.usb_planar_rgb = bool(target.usb_planar)


def apply_lab_motor_acoustic(scanner: Scanner, target: LabTarget) -> None:
    """Apply GL128 image-pass acoustic A/B flags from Scan Lab checkboxes."""
    asic = getattr(scanner, "_asic", None)
    if asic is None:
        return
    from pyopticfilm.scan.session_gl128 import IMAGE_USB_PACE_S

    asic.image_slope_slow = bool(target.slow_image_slope)
    asic.image_usb_pace_s = IMAGE_USB_PACE_S if target.quiet_usb_pace else 0.0


def nonse_safe_y_fraction(model: FilmModel) -> float:
    """Height fraction of TA used for short Lab windows."""
    y_ta = float(getattr(model, "y_size_ta_mm", 25.0) or 25.0)
    frac_from_mm = NONSE_SAFE_Y_MM / max(y_ta, 1e-6)
    return min(NONSE_SAFE_Y_FRACTION, max(0.05, frac_from_mm))


def nonse_safe_area(model: FilmModel, *, y1: float = 0.0) -> Area:
    """Full-width short Y window for unvalidated non-SE bring-up."""
    y1 = max(0.0, min(1.0, float(y1)))
    height = nonse_safe_y_fraction(model)
    y2 = min(1.0, y1 + height)
    if y2 <= y1:
        y1 = max(0.0, y2 - height)
    return clamp_area((0.0, y1, 1.0, y2))


def clamp_nonse_scan_area(model: FilmModel, area: Area) -> Area:
    """Clamp a crop so Y span does not exceed the non-SE safe fraction."""
    x1, y1, x2, y2 = clamp_area(area)
    max_h = nonse_safe_y_fraction(model)
    if (y2 - y1) > max_h + 1e-9:
        y2 = min(1.0, y1 + max_h)
    return clamp_area((x1, y1, x2, y2))


def lab_scan_needs_motor_warning(
    model: FilmModel,
    *,
    dpi: int,
    crop_norm: Area | None,
) -> bool:
    """True when a Scan would have been a long high-PPI move before clamping."""
    if is_gl128_opticfilm(model) or model_is_scan_ready(model):
        return False
    if int(dpi) < HIGH_PPI_WARN_DPI:
        return False
    if crop_norm is None:
        return True
    area = image_crop_to_scan_area(model, crop_norm)
    return (area[3] - area[1]) > nonse_safe_y_fraction(model) + 1e-9


def resolve_ir_pass_and_n_passes(
    *, ir_pass_checked: bool, n_passes: int, changed: str
) -> tuple[bool, int]:
    """Keep IR pass and Multi-Pass N mutually exclusive on GL128.

    `changed` names the control the user just touched ("ir_pass" or
    "n_passes"). session_gl128.Gl128ScanSession.run raises ScanError for
    infrared=True with n_passes>1, so the GUI must not let the two combine —
    mirrors the old me_fixed_long auto-untick pattern.
    """
    if changed == "ir_pass" and ir_pass_checked:
        return True, 1
    if changed == "n_passes" and n_passes > 1:
        return False, n_passes
    return ir_pass_checked, n_passes


def prescan_resolution(model: FilmModel) -> int:
    if is_gl128_opticfilm(model):
        return PRESCAN_DPI
    return min(int(d) for d in model.resolutions_dpi)


def lab_scan_kwargs(
    model: FilmModel,
    *,
    dpi: int,
    kind: str,
    crop_norm: tuple[float, float, float, float] | None,
) -> dict:
    """``Scanner.scan`` kwargs that stay inside a motor-safe window.

    SE uses capture-proven ``bringup_scan_geometry`` / crop clamps. Unvalidated
    non-SE models never get full-TA ``area=None`` — Lab uses a short Y strip
    (and clamps rubber-band crops) so high-PPI Scan cannot grind the carriage.
    """
    if is_gl128_opticfilm(model):
        if kind == "prescan" or crop_norm is None:
            geometry, _ = bringup_scan_geometry(model, dpi, profile="preview_safe")
        else:
            area = image_crop_to_scan_area(model, crop_norm)
            geometry, _ = crop_scan_geometry(model, dpi, area)
        return {"resolution": dpi, "geometry": geometry, "area": None}

    if model_is_scan_ready(model):
        area = None
        if crop_norm is not None and kind == "scan":
            area = image_crop_to_scan_area(model, crop_norm)
        return {"resolution": dpi, "area": area}

    # Unvalidated non-SE: always a bounded area.
    if kind == "scan" and crop_norm is not None:
        area = clamp_nonse_scan_area(model, image_crop_to_scan_area(model, crop_norm))
    else:
        area = nonse_safe_area(model)
    return {"resolution": dpi, "area": area}


def lab_crop_scan_meta(
    model: FilmModel,
    *,
    dpi: int,
    crop_norm: tuple[float, float, float, float],
) -> dict | None:
    """Crop geometry meta for Scan Lab status (GL128 cropped scans only)."""
    if not is_gl128_opticfilm(model):
        return None
    area = image_crop_to_scan_area(model, crop_norm)
    _geometry, meta = crop_scan_geometry(model, dpi, area)
    return meta


def format_crop_status(meta: dict | None) -> str | None:
    if meta is None:
        return None
    requested = meta.get("requested_area")
    effective = meta.get("effective_area")
    if not requested or not effective:
        return None
    return crop_adjustment_message(requested, effective)


def format_scan_window_note(
    *,
    crop_norm: tuple[float, float, float, float] | None,
    width: int,
    height: int,
) -> str:
    """Status-bar fragment: crop applied vs full preview_safe window."""
    if crop_norm is None:
        return f"full window {width}×{height}"
    return f"crop applied {width}×{height}"


def format_scan_window_log(
    crop_norm: tuple[float, float, float, float] | None,
    scan_kw: dict,
) -> str:
    """USB-log line for the Scan window (crop tuple + STR/END when geometry is set)."""
    if crop_norm is None:
        text = "full window"
    else:
        x1, y1, x2, y2 = (float(v) for v in crop_norm)
        text = f"crop ({x1:.3f},{y1:.3f},{x2:.3f},{y2:.3f})"
    geometry = scan_kw.get("geometry")
    if geometry is not None:
        text += (
            f" str={geometry.pixel_startx} end={geometry.pixel_endx} "
            f"pixels={geometry.pixels} lines={geometry.lines} "
            f"lincnt={geometry.lincnt_register}"
        )
    elif scan_kw.get("area") is not None:
        text += f" area={scan_kw['area']}"
    return text


def list_lab_targets() -> list[LabTarget]:
    targets: list[LabTarget] = []
    connected_models: set[str] = set()
    try:
        devices = list_devices()
    except Exception:  # noqa: BLE001
        devices = []
    for info in devices:
        try:
            model = model_for_device(info.product_id, info.bcd_device)
        except Exception:  # noqa: BLE001, S112
            continue
        name = info.product or model.model
        targets.append(
            LabTarget(
                label=f"{name} ({info.device_id}) — connected",
                model=model,
                device_id=info.device_id,
            )
        )
        connected_models.add(model.name)
    for model in KNOWN_MODELS:
        if model.name in connected_models:
            continue
        targets.append(
            LabTarget(
                label=f"{model.model} ({model.asic})",
                model=model,
            )
        )
    return targets


def open_lab_scanner(
    target: LabTarget,
    *,
    on_usb: Callable[[UsbTransaction], None] | None = None,
) -> tuple[Scanner, RecordingTransport]:
    """Open mock USB, or a plugged-in scanner wrapped in a recording transport."""
    listener = on_usb
    if target.mock:
        rec = RecordingTransport(MockScannerTransport(), listener=listener)
        scanner = Scanner.open_fake(target.model, rec)
        apply_lab_decode_layout(scanner, target)
        apply_lab_motor_acoustic(scanner, target)
        return scanner, rec

    from pyopticfilm.usb.device import UsbDeviceHandle

    if not target.device_id:
        raise DeviceNotFoundError("No matching scanner is connected")
    handle = UsbDeviceHandle.open(target.device_id)
    rec = RecordingTransport(handle, listener=listener)
    scanner = Scanner(handle, GenesysUsbProtocol(rec), model=target.model)
    apply_lab_hw_override(scanner, target)
    apply_lab_decode_layout(scanner, target)
    apply_lab_motor_acoustic(scanner, target)
    return scanner, rec


def usb_log_line(txn: UsbTransaction) -> str:
    return format_usb_log_line(txn)


def usb_log_divider(title: str) -> str:
    return f"======== {title} ========"


def usb_log_section_key(line: str) -> str | None:
    """Return ``PRESCAN`` / ``SCAN`` / ``IR`` from a divider line, else ``None``."""
    stripped = line.strip()
    if not (stripped.startswith("======== ") and stripped.endswith(" ========")):
        return None
    inner = stripped.removeprefix("======== ").removesuffix(" ========").strip()
    if not inner:
        return None
    return inner.split()[0]


def device_banner(target: LabTarget, info: UsbDeviceInfo | None = None) -> str:
    if target.mock:
        return (
            f"MOCK hardware — {target.model.model} ({target.model.asic}). "
            "Not a real scan; motors stay locked on physical non-SE devices."
        )
    extra = f" {info.device_id}" if info is not None else ""
    layout = "planar" if target.usb_planar else "chunky"
    if not model_is_scan_ready(target.model):
        if target.allow_unvalidated:
            return (
                f"REAL hardware — {target.model.model}{extra}. "
                "HW gate OVERRIDDEN — unverified pipeline; motors/lamp can run. "
                f"scan_ready stays False. USB RGB={layout}."
            )
        return (
            f"REAL hardware — {target.model.model}{extra}. "
            "scan_ready is False; the library will refuse to scan."
        )
    return f"REAL hardware — {target.model.model}{extra}. USB RGB={layout}."
