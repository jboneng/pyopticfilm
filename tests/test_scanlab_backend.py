# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan Lab backend: mock checkbox vs real USB (no display)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pyopticfilm.device.model_7400 import MODEL_8100
from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.select import KNOWN_MODELS
from pyopticfilm.exceptions import DeviceNotFoundError
from tools.scanlab.backend import (
    LabTarget,
    apply_lab_decode_layout,
    apply_lab_hw_override,
    apply_lab_motor_acoustic,
    device_banner,
    format_crop_status,
    format_scan_window_log,
    format_scan_window_note,
    lab_crop_scan_meta,
    lab_scan_kwargs,
    lab_scan_needs_motor_warning,
    list_lab_targets,
    nonse_safe_area,
    nonse_safe_y_fraction,
    open_lab_scanner,
    resolve_ir_pass_and_n_passes,
    usb_log_divider,
    usb_log_section_key,
    with_hw_override,
    with_mock_mode,
    with_motor_acoustic,
    with_usb_planar,
)


def _empty_devices():
    return []


def test_list_lab_targets_without_usb(monkeypatch):
    monkeypatch.setattr("tools.scanlab.backend.list_devices", _empty_devices)
    targets = list_lab_targets()
    assert {t.model.name for t in targets} == {m.name for m in KNOWN_MODELS}
    assert all(t.mock and t.device_id is None for t in targets)


def test_with_mock_mode_selects_real():
    target = LabTarget(label="se", model=MODEL_8200I, device_id="plustek:usb:07b3:1825:001:002")
    real = with_mock_mode(target, False)
    assert real.mock is False
    assert real.device_id == target.device_id
    assert with_mock_mode(real, True).mock is True


def test_with_hw_override_sets_flag():
    target = LabTarget(label="x", model=MODEL_8200I, device_id="id")
    assert target.allow_unvalidated is False
    unlocked = with_hw_override(with_mock_mode(target, False), True)
    assert unlocked.mock is False
    assert unlocked.allow_unvalidated is True
    assert with_hw_override(unlocked, False).allow_unvalidated is False


def test_device_banner_override_vs_locked():
    locked = with_mock_mode(
        LabTarget(label="x", model=MODEL_8200I, device_id="id"),
        False,
    )
    text = device_banner(locked)
    assert "refuse to scan" in text
    assert "OVERRIDDEN" not in text

    unlocked = with_hw_override(locked, True)
    text = device_banner(unlocked)
    assert "OVERRIDDEN" in text
    assert "refuse to scan" not in text


def test_apply_lab_hw_override_real_only():
    scanner = MagicMock()
    scanner._allow_unvalidated_scan = False
    mock_target = LabTarget(label="m", model=MODEL_8200I, allow_unvalidated=True)
    apply_lab_hw_override(scanner, mock_target)
    assert scanner._allow_unvalidated_scan is False

    real_locked = with_mock_mode(
        LabTarget(label="r", model=MODEL_8200I, device_id="id"),
        False,
    )
    apply_lab_hw_override(scanner, real_locked)
    assert scanner._allow_unvalidated_scan is False

    real_unlocked = with_hw_override(real_locked, True)
    apply_lab_hw_override(scanner, real_unlocked)
    assert scanner._allow_unvalidated_scan is True
    # Flatbed GL128 / other models: the override never arms motor moves.
    scanner.arm_bringup_motor.assert_not_called()


def test_apply_lab_hw_override_arms_film_loader_motor():
    from pyopticfilm.device.model_135i import MODEL_135I

    scanner = MagicMock()
    scanner._allow_unvalidated_scan = False
    locked = with_mock_mode(LabTarget(label="r", model=MODEL_135I, device_id="id"), False)
    apply_lab_hw_override(scanner, locked)
    scanner.arm_bringup_motor.assert_not_called()
    apply_lab_hw_override(scanner, with_mock_mode(with_hw_override(locked, True), True))
    scanner.arm_bringup_motor.assert_not_called()

    apply_lab_hw_override(scanner, with_hw_override(locked, True))
    assert scanner._allow_unvalidated_scan is True
    scanner.arm_bringup_motor.assert_called_once_with()
    assert "loader motor ARMED" in device_banner(with_hw_override(locked, True))


def test_apply_lab_decode_layout_sets_planar():
    scanner = MagicMock()
    asic = MagicMock()
    scanner._asic = asic
    target = with_usb_planar(LabTarget(label="x", model=MODEL_8100), True)
    apply_lab_decode_layout(scanner, target)
    assert asic.usb_planar_rgb is True
    apply_lab_decode_layout(scanner, with_usb_planar(target, False))
    assert asic.usb_planar_rgb is False


def test_format_crop_status_reports_adjustment():
    from pyopticfilm.scan.bringup import crop_adjustment_message

    meta = {
        "requested_area": (0.1, 0.2, 0.9, 0.8),
        "effective_area": (0.1, 0.2, 0.85, 0.8),
    }
    note = format_crop_status(meta)
    assert note == crop_adjustment_message(meta["requested_area"], meta["effective_area"])


def test_lab_crop_scan_meta_se():
    meta = lab_crop_scan_meta(
        MODEL_8200I_SE,
        dpi=2400,
        crop_norm=(0.1, 0.2, 0.9, 0.8),
    )
    assert meta is not None
    assert meta.get("effective_area") is not None


def test_apply_lab_motor_acoustic_flags():
    scanner = MagicMock()
    asic = MagicMock()
    scanner._asic = asic
    target = with_motor_acoustic(
        LabTarget(label="x", model=MODEL_8200I_SE),
        slow_image_slope=True,
    )
    apply_lab_motor_acoustic(scanner, target)
    assert asic.image_slope_slow is True
    apply_lab_motor_acoustic(
        scanner,
        with_motor_acoustic(target, slow_image_slope=False),
    )
    assert asic.image_slope_slow is False


def test_open_real_without_connected_device_raises():
    target = with_mock_mode(LabTarget(label="x", model=MODEL_8200I), False)
    with pytest.raises(DeviceNotFoundError, match="connected"):
        open_lab_scanner(target)


def test_format_scan_window_note_crop_vs_full():
    assert format_scan_window_note(crop_norm=None, width=2568, height=1801) == (
        "full window 2568×1801"
    )
    assert format_scan_window_note(
        crop_norm=(0.1, 0.2, 0.9, 0.8), width=1272, height=804
    ) == "crop applied 1272×804"


def test_format_scan_window_log_includes_geometry():
    kwargs = lab_scan_kwargs(
        MODEL_8200I_SE,
        dpi=1800,
        kind="scan",
        crop_norm=(0.1, 0.1, 0.75, 0.85),
    )
    line = format_scan_window_log((0.1, 0.1, 0.75, 0.85), kwargs)
    geo = kwargs["geometry"]
    assert line.startswith("crop (0.100,0.100,0.750,0.850)")
    assert f"str={geo.pixel_startx}" in line
    assert f"end={geo.pixel_endx}" in line
    assert format_scan_window_log(None, lab_scan_kwargs(
        MODEL_8200I_SE, dpi=1800, kind="scan", crop_norm=None
    )).startswith("full window")


def test_lab_non_se_prescan_uses_short_area():
    kwargs = lab_scan_kwargs(MODEL_8100, dpi=600, kind="prescan", crop_norm=None)
    assert "geometry" not in kwargs
    area = kwargs["area"]
    assert area is not None
    x1, y1, x2, y2 = area
    assert x1 == 0.0 and x2 == 1.0
    assert y2 - y1 <= nonse_safe_y_fraction(MODEL_8100) + 1e-9
    assert area == nonse_safe_area(MODEL_8100)


def test_lab_non_se_uncropped_scan_clamps_not_full_ta():
    kwargs = lab_scan_kwargs(MODEL_8100, dpi=3600, kind="scan", crop_norm=None)
    area = kwargs["area"]
    assert area is not None
    assert (area[3] - area[1]) <= nonse_safe_y_fraction(MODEL_8100) + 1e-9


def test_lab_non_se_tall_crop_is_clamped():
    kwargs = lab_scan_kwargs(
        MODEL_8100,
        dpi=1200,
        kind="scan",
        crop_norm=(0.0, 0.0, 1.0, 1.0),
    )
    area = kwargs["area"]
    assert area is not None
    assert (area[3] - area[1]) <= nonse_safe_y_fraction(MODEL_8100) + 1e-9


def test_lab_scan_needs_motor_warning_high_ppi():
    assert lab_scan_needs_motor_warning(MODEL_8100, dpi=3600, crop_norm=None) is True
    assert lab_scan_needs_motor_warning(MODEL_8100, dpi=600, crop_norm=None) is False
    assert lab_scan_needs_motor_warning(MODEL_8200I_SE, dpi=3600, crop_norm=None) is False


def test_usb_log_section_key_from_divider():
    assert usb_log_section_key(usb_log_divider("PRESCAN 1200 dpi")) == "PRESCAN"
    assert usb_log_section_key(usb_log_divider("SCAN 1800 dpi")) == "SCAN"
    assert usb_log_section_key(usb_log_divider("IR 1800 dpi")) == "IR"
    assert usb_log_section_key("control_write type=0x40") is None


def test_resolve_ir_pass_checked_forces_n_passes_to_one():
    assert resolve_ir_pass_and_n_passes(
        ir_pass_checked=True, n_passes=4, changed="ir_pass"
    ) == (True, 1)


def test_resolve_n_passes_above_one_unchecks_ir_pass():
    assert resolve_ir_pass_and_n_passes(
        ir_pass_checked=True, n_passes=3, changed="n_passes"
    ) == (False, 3)


def test_resolve_n_passes_at_one_leaves_ir_pass_untouched():
    assert resolve_ir_pass_and_n_passes(
        ir_pass_checked=True, n_passes=1, changed="n_passes"
    ) == (True, 1)


def test_resolve_ir_pass_unchecked_leaves_n_passes_untouched():
    assert resolve_ir_pass_and_n_passes(
        ir_pass_checked=False, n_passes=5, changed="ir_pass"
    ) == (False, 5)


def test_film_loader_lab_kwargs_and_mock(tmp_path, monkeypatch):
    from pyopticfilm.device.model_135i import MODEL_135I
    from pyopticfilm.usb.fake import MockFilmLoaderTransport
    from tools.scanlab.backend import (
        LabTarget,
        is_film_loader,
        lab_crop_scan_meta,
        lab_scan_kwargs,
        multi_pass_allowed_at,
        open_lab_scanner,
    )

    assert is_film_loader(MODEL_135I) and not is_film_loader(MODEL_8200I_SE)
    # Prescan: the whole frame slot of the chosen frame; no flatbed bring-up geometry.
    assert lab_scan_kwargs(MODEL_135I, dpi=1200, kind="prescan", crop_norm=None, frame=2) == {
        "resolution": 1200,
        "area": None,
        "frame": 2,
    }
    # Scan crop: display X is mirrored back to the slot.
    kw = lab_scan_kwargs(MODEL_135I, dpi=1200, kind="scan", crop_norm=(0.0, 0.2, 0.25, 0.6))
    assert kw["area"] == pytest.approx((0.75, 0.2, 1.0, 0.6))
    assert lab_crop_scan_meta(MODEL_135I, dpi=1200, crop_norm=(0.0, 0.2, 0.25, 0.6)) is None
    assert lab_scan_kwargs(MODEL_135I, dpi=600, kind="prescan", crop_norm=None, frame=None)["frame"] is None
    assert multi_pass_allowed_at(MODEL_135I, 1200) is True
    assert multi_pass_allowed_at(MODEL_135I, 7200) is False

    monkeypatch.setattr("pyopticfilm.scanner.default_cache_path", lambda: tmp_path / "calib.json")
    scanner, rec = open_lab_scanner(LabTarget(label="135i", model=MODEL_135I))
    try:
        assert isinstance(rec.inner, MockFilmLoaderTransport)
        assert scanner.holder_present()
    finally:
        scanner.close()
