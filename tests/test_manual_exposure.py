# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual (Scan Lab / debug) exposure overrides: validation, GL128 single-pass,
and GL128 multi-exposure (ME) short/long behavior.

Automatic/derived exposure keeps the existing adaptive envelope and hardware
clamp; an explicitly supplied value is written to ``REG_EXPOSURE`` verbatim.
"""

from __future__ import annotations

import pytest

from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.select import create_asic
from pyopticfilm.scan.exposure_override import MAX_EXPOSURE_REGISTER, validate_manual_exposure
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.scan.session_gl128 import Gl128ScanSession
from pyopticfilm.scanner import Scanner
from pyopticfilm.usb.fake import MockScannerTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol

_TINY = (0.0, 0.0, 0.08, 0.08)


def _reg_exposure(usb: MockScannerTransport) -> int:
    return (
        (usb.registers.get(0x7D, 0) << 16)
        | (usb.registers.get(0x7E, 0) << 8)
        | usb.registers.get(0x7F, 0)
    )


def _mock_gl128_session() -> tuple[Gl128ScanSession, MockScannerTransport]:
    """For direct ``_configure()`` calls — motor moves off (as in model_lock ME configure)."""
    usb = MockScannerTransport()
    asic = create_asic(GenesysUsbProtocol(usb), MODEL_8200I_SE)
    asic._motor_moves_enabled = False
    asic.init()
    return Gl128ScanSession(asic, MODEL_8200I_SE), usb


def _mock_gl128_session_armed() -> tuple[Gl128ScanSession, MockScannerTransport]:
    """For full ``run()`` calls — motor moves armed, required by the run() gate."""
    usb = MockScannerTransport()
    asic = create_asic(GenesysUsbProtocol(usb), MODEL_8200I_SE)
    asic._motor_moves_enabled = True
    asic.init()
    return Gl128ScanSession(asic, MODEL_8200I_SE), usb


# --- validation ----------------------------------------------------------


def test_validate_manual_exposure_none_is_allowed():
    assert validate_manual_exposure(None, label="x") is None


def test_validate_manual_exposure_accepts_24bit_max():
    assert validate_manual_exposure(MAX_EXPOSURE_REGISTER, label="x") == MAX_EXPOSURE_REGISTER


@pytest.mark.parametrize("value", [0, -1, -100000])
def test_validate_manual_exposure_rejects_non_positive(value):
    with pytest.raises(ValueError):
        validate_manual_exposure(value, label="x")


def test_validate_manual_exposure_rejects_above_24bit_max():
    with pytest.raises(ValueError):
        validate_manual_exposure(MAX_EXPOSURE_REGISTER + 1, label="x")


# --- single-pass: register-writing decision -------------------------------


def test_single_pass_manual_below_hardware_max_reaches_register():
    session, usb = _mock_gl128_session()
    geo = compute_geometry(1200, model=MODEL_8200I_SE)
    session._pass_exposure = 50000
    session._pass_manual = True
    session._configure(geo)
    assert _reg_exposure(usb) == 50000


def test_single_pass_manual_above_hardware_max_is_not_clamped():
    session, usb = _mock_gl128_session()
    assert MODEL_8200I_SE.me_hardware_max_exposure == 85000
    geo = compute_geometry(1200, model=MODEL_8200I_SE)
    session._pass_exposure = 100000
    session._pass_manual = True
    session._configure(geo)
    assert _reg_exposure(usb) == 100000


def test_single_pass_default_none_keeps_hardware_clamp():
    """No override (``_pass_manual`` stays False): the existing clamp still fires."""
    session, usb = _mock_gl128_session()
    geo = compute_geometry(1200, model=MODEL_8200I_SE)
    session._pass_exposure = 100000
    session._pass_long_exposure = False
    session._pass_manual = False
    session._configure(geo)
    assert _reg_exposure(usb) == MODEL_8200I_SE.me_hardware_max_exposure


def test_single_pass_manual_state_resets_between_scans():
    """A manual pass must not leak into a later default-exposure scan."""
    usb = MockScannerTransport()
    scanner = Scanner.open_fake(MODEL_8200I_SE, usb)
    try:
        scanner.scan(resolution=150, area=_TINY, apply_calib=False, single_pass_exposure=99999)
        assert _reg_exposure(usb) == 99999

        scanner.scan(resolution=150, area=_TINY, apply_calib=False)
        assert _reg_exposure(usb) == MODEL_8200I_SE.exposure_short
    finally:
        scanner.close()


# --- ME short: register-writing decision ----------------------------------


def test_me_short_manual_below_hardware_max_reaches_register():
    session, usb = _mock_gl128_session()
    geo = compute_geometry(1800, model=MODEL_8200I_SE)
    session._pass_exposure = 30000
    session._pass_long_exposure = False
    session._pass_manual = True
    session._configure(geo)
    assert _reg_exposure(usb) == 30000


def test_me_short_manual_above_hardware_max_is_not_clamped():
    session, usb = _mock_gl128_session()
    geo = compute_geometry(1800, model=MODEL_8200I_SE)
    session._pass_exposure = 100000
    session._pass_long_exposure = False
    session._pass_manual = True
    session._configure(geo)
    assert _reg_exposure(usb) == 100000


def test_me_short_default_none_uses_model_derived_value():
    session, _usb = _mock_gl128_session_armed()
    session.run(resolution=1800, area=_TINY, apply_calib=False, multi_exposure=True)
    assert session.last_me_debug.exposure_short == MODEL_8200I_SE.exposure_short


# --- ME long: adaptive/DPI/hardware clamps bypassed end-to-end ------------


def test_me_long_manual_below_normal_limits_not_raised_to_adaptive_floor():
    session, _usb = _mock_gl128_session_armed()
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        me_long_exposure=5000,  # below the 14000 floor / 42000 adaptive minimum
    )
    assert session.last_me_debug.exposure_long == 5000


def test_me_long_manual_skips_adaptive_clamp_and_reaches_register():
    """ME long is normally capped at 64000; manual override reaches the true
    16-bit AHB exposure table ceiling (65535) verbatim."""
    session, usb = _mock_gl128_session_armed()
    captures: list[tuple[bool, bool, int]] = []
    original_configure = session._configure

    def spy(geometry):
        original_configure(geometry)
        captures.append((session._pass_long_exposure, session._pass_manual, _reg_exposure(usb)))

    session._configure = spy  # type: ignore[method-assign]

    session.run(
        resolution=7200,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        me_long_exposure=65535,
    )

    long_captures = [c for c in captures if c[0]]
    assert long_captures, "expected a long pass to run"
    _long_pass, manual, reg_value = long_captures[-1]
    assert manual is True
    assert reg_value == 65535
    assert session.last_me_debug.exposure_long == 65535
    assert session.last_me_debug.exposure_reason == "manual-override"
    assert session.last_me_debug.exposure_proposed is None


def test_me_long_manual_above_16bit_table_raises():
    """A manual override at oversample=1 (7200 dpi) above the AHB per-channel
    exposure table's 16-bit width fails loudly instead of silently uploading a
    wrapped table value inconsistent with REG_EXPOSURE."""
    session, _usb = _mock_gl128_session_armed()
    with pytest.raises(ValueError, match="16-bit"):
        session.run(
            resolution=7200,
            area=_TINY,
            apply_calib=False,
            multi_exposure=True,
            me_long_exposure=150000,
        )


def test_me_long_manual_overrides_adaptive_selection():
    """``me_long_exposure=120000`` -> 120000, not adaptive."""
    session, _usb = _mock_gl128_session_armed()
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        me_long_exposure=120000,
    )
    assert session.last_me_debug.exposure_long == 120000
    assert session.last_me_debug.exposure_reason == "manual-override"


def test_me_long_default_none_uses_adaptive_selection():
    session, _usb = _mock_gl128_session_armed()
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
    )
    debug = session.last_me_debug
    assert debug.exposure_reason != "manual-override"
    assert debug.exposure_proposed is not None
    assert 14000 <= debug.exposure_long <= MODEL_8200I_SE.me_hardware_max_exposure


def test_scanner_scan_rejects_invalid_manual_exposure_before_opening_asic():
    scanner = Scanner.open_fake(MODEL_8200I_SE)
    try:
        with pytest.raises(ValueError):
            scanner.scan(resolution=150, area=_TINY, apply_calib=False, me_long_exposure=-5)
        assert not scanner._asic._initialized
    finally:
        scanner.close()


# --- base session compatibility (non-GL128) -------------------------------


def test_scanner_scan_single_pass_exposure_not_implemented_for_non_gl128():
    scanner = Scanner.open_fake(MODEL_8200I)
    try:
        with pytest.raises(NotImplementedError):
            scanner.scan(resolution=900, area=_TINY, apply_calib=False, single_pass_exposure=5000)
    finally:
        scanner.close()
