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
    """ME long is normally capped; manual override reaches the true 16-bit AHB
    exposure table ceiling verbatim (131070 at 3600 dpi = table word 65535)."""
    session, usb = _mock_gl128_session_armed()
    captures: list[tuple[bool, bool, int]] = []
    original_configure = session._configure

    def spy(geometry):
        original_configure(geometry)
        captures.append((session._pass_long_exposure, session._pass_manual, _reg_exposure(usb)))

    session._configure = spy  # type: ignore[method-assign]

    session.run(
        resolution=3600,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        me_long_exposure=131070,
    )

    long_captures = [c for c in captures if c[0]]
    assert long_captures, "expected a long pass to run"
    _long_pass, manual, reg_value = long_captures[-1]
    assert manual is True
    assert reg_value == 131070
    assert session.last_me_debug.exposure_long == 131070
    assert session.last_me_debug.exposure_reason == "manual-override"
    assert session.last_me_debug.exposure_proposed is None


def test_me_long_manual_above_16bit_table_raises():
    """A manual override whose AHB scan motor table word (exposure / oversample)
    exceeds 16 bits fails loudly instead of silently uploading a wrapped table
    value inconsistent with REG_EXPOSURE."""
    session, _usb = _mock_gl128_session_armed()
    with pytest.raises(ValueError, match="16-bit"):
        session.run(
            resolution=3600,
            area=_TINY,
            apply_calib=False,
            multi_exposure=True,
            me_long_exposure=140000,
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


def test_me_adaptive_long_meters_film_not_holder_border():
    """A near-black holder border (~5 % of the default frame) must not set the long exposure."""
    import numpy as np

    session, _usb = _mock_gl128_session_armed()
    frame = np.zeros((200, 200, 3), dtype=np.uint16)
    frame[:, :] = (20000, 8000, 2500)  # film: blue p05 2500 -> 14000 * 10000 / 2500 = 56000
    frame[:5], frame[-5:], frame[:, :5], frame[:, -5:] = 100, 100, 100, 100  # holder
    session.pipeline.assemble = lambda *_a, **_k: frame.copy()  # type: ignore[method-assign]
    session.run(resolution=1800, area=_TINY, apply_calib=False, multi_exposure=True)
    debug = session.last_me_debug
    assert debug.exposure_reason == "adaptive"
    assert debug.exposure_long == 56000


def test_single_pass_exposure_applies_to_colour_plus_ir():
    """``infrared=True`` routes through the multi-pass path; the override must not be dropped."""
    session, _usb = _mock_gl128_session_armed()
    passes: list[tuple[str, int | None, bool]] = []
    configure = session._configure

    def spy(geometry):
        if not geometry.disable_buffer_full_move:
            passes.append((session.asic._scan_method, session._pass_exposure, session._pass_manual))
        return configure(geometry)

    session._configure = spy  # type: ignore[method-assign]
    session.run(resolution=150, area=_TINY, apply_calib=False, infrared=True, single_pass_exposure=20000)
    assert passes == [("transparency", 20000, True), ("infrared", 20000, True)]


# --- input types: NumPy integers in, bool out -------------------------------


@pytest.mark.parametrize("value", [True, False])
def test_validate_manual_exposure_rejects_bool(value):
    """``True`` used to pass as exposure 1."""
    with pytest.raises(TypeError, match="must be an int or None, got bool"):
        validate_manual_exposure(value, label="x")


@pytest.mark.parametrize("value", [1.0, 20000.0, "20000"])
def test_validate_manual_exposure_still_rejects_non_integers(value):
    with pytest.raises(TypeError):
        validate_manual_exposure(value, label="x")


def test_validate_manual_exposure_accepts_numpy_integers():
    import numpy as np

    for dtype in (np.int32, np.int64, np.uint32):
        result = validate_manual_exposure(dtype(20000), label="x")
        assert result == 20000 and type(result) is int
    with pytest.raises(ValueError, match="exceeds the 24-bit"):
        validate_manual_exposure(np.int64(MAX_EXPOSURE_REGISTER + 1), label="x")


def test_validate_n_passes_accepts_numpy_integers_and_rejects_bool():
    import numpy as np

    from pyopticfilm.scan.exposure_override import validate_n_passes

    result = validate_n_passes(np.int64(3))
    assert result == 3 and type(result) is int
    with pytest.raises(TypeError):
        validate_n_passes(True)


def test_scanner_scan_numpy_exposure_reaches_register():
    import numpy as np

    usb = MockScannerTransport()
    scanner = Scanner.open_fake(MODEL_8200I_SE, usb)
    try:
        scanner.scan(resolution=150, area=_TINY, apply_calib=False, single_pass_exposure=np.int64(20000))
    finally:
        scanner.close()
    assert _reg_exposure(usb) == 20000


def test_scanner_scan_rejects_bool_exposure_before_usb():
    usb = MockScannerTransport()
    scanner = Scanner.open_fake(MODEL_8200I_SE, usb)
    try:
        with pytest.raises(TypeError, match="single_pass_exposure must be an int or None, got bool"):
            scanner.scan(resolution=150, area=_TINY, apply_calib=False, single_pass_exposure=True)
        assert usb.transactions == []
    finally:
        scanner.close()
