# SPDX-License-Identifier: GPL-3.0-or-later
"""``me_long_mode="fixed"``: SilverFast-style ME with a per-model fixed long pass."""

from __future__ import annotations

from dataclasses import replace

import pytest

from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE
from pyopticfilm.device.select import create_asic
from pyopticfilm.scan.me_exposure import ME_LONG_MODES, validate_me_long_mode
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


def _armed_session(model=MODEL_8200I_SE) -> tuple[Gl128ScanSession, MockScannerTransport]:
    usb = MockScannerTransport()
    asic = create_asic(GenesysUsbProtocol(usb), model)
    asic._motor_moves_enabled = True
    asic.init()
    return Gl128ScanSession(asic, model), usb


def _spy_long_passes(session: Gl128ScanSession, usb: MockScannerTransport) -> list[tuple[bool, int]]:
    """Record ``(manual, REG_EXPOSURE)`` for every long-pass configure."""
    captures: list[tuple[bool, int]] = []
    original_configure = session._configure

    def spy(geometry):
        original_configure(geometry)
        if session._pass_long_exposure:
            captures.append((session._pass_manual, _reg_exposure(usb)))

    session._configure = spy  # type: ignore[method-assign]
    return captures


# --- validation ----------------------------------------------------------


def test_validate_me_long_mode_accepts_known_modes():
    for mode in ME_LONG_MODES:
        assert validate_me_long_mode(mode) == mode


@pytest.mark.parametrize("value", ["Fixed", "", None, 1, "manual"])
def test_validate_me_long_mode_rejects_unknown(value):
    with pytest.raises(ValueError, match="me_long_mode"):
        validate_me_long_mode(value)


def test_scanner_scan_rejects_bad_mode_before_opening_asic():
    scanner = Scanner.open_fake(MODEL_8200I_SE)
    try:
        with pytest.raises(ValueError, match="me_long_mode"):
            scanner.scan(resolution=1800, area=_TINY, apply_calib=False, me_long_mode="silverfast")
        assert not scanner._asic._initialized
    finally:
        scanner.close()


# --- per-model values ----------------------------------------------------


def test_fixed_long_exposure_per_model():
    assert MODEL_8200I_SE.me_fixed_long_exposure == 42000
    assert MODEL_8100_V2.me_fixed_long_exposure == 42000
    assert MODEL_8300I_SE.me_fixed_long_exposure == 60000


# --- session behaviour ---------------------------------------------------


@pytest.mark.parametrize("model", [MODEL_8200I_SE, MODEL_8100_V2, MODEL_8300I_SE])
def test_fixed_mode_programs_model_value_through_normal_clamps(model):
    session, usb = _armed_session(model)
    captures = _spy_long_passes(session, usb)
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        me_long_mode="fixed",
    )
    assert captures, "expected a long pass to run"
    manual, reg_value = captures[-1]
    assert manual is False
    assert reg_value == model.me_fixed_long_exposure
    debug = session.last_me_debug
    assert debug.exposure_long == model.me_fixed_long_exposure
    assert debug.exposure_reason == "fixed"
    assert debug.exposure_proposed is None
    assert debug.exposure_short == model.exposure_short


def test_fixed_mode_skips_adaptive_selection(monkeypatch):
    from pyopticfilm.scan import me_exposure

    def boom(*_a, **_k):
        raise AssertionError("adaptive selection must not run in fixed mode")

    monkeypatch.setattr(me_exposure, "select_long_exposure", boom)
    session, _usb = _armed_session()
    session.run(
        resolution=1800, area=_TINY, apply_calib=False, multi_exposure=True, me_long_mode="fixed"
    )
    assert session.last_me_debug.exposure_reason == "fixed"


def test_fixed_mode_value_outside_envelope_is_clamped():
    model = replace(MODEL_8200I_SE, me_fixed_long_exposure=200000)
    session, usb = _armed_session(model)
    captures = _spy_long_passes(session, usb)
    session.run(
        resolution=1800, area=_TINY, apply_calib=False, multi_exposure=True, me_long_mode="fixed"
    )
    assert captures[-1] == (False, model.me_long_clamp_max)
    assert session.last_me_debug.exposure_long == model.me_long_clamp_max
    assert session.last_me_debug.exposure_reason == "fixed (clamped)"


def test_manual_long_exposure_wins_over_fixed_mode():
    session, _usb = _armed_session()
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        me_long_mode="fixed",
        me_long_exposure=50000,
    )
    assert session.last_me_debug.exposure_long == 50000
    assert session.last_me_debug.exposure_reason == "manual-override"


def test_adaptive_default_is_unchanged():
    session, _usb = _armed_session()
    session.run(resolution=1800, area=_TINY, apply_calib=False, multi_exposure=True)
    debug = session.last_me_debug
    assert debug.exposure_reason not in ("fixed", "fixed (clamped)", "manual-override")
    assert debug.exposure_proposed is not None


def test_fixed_mode_with_multi_pass_stacks_fixed_long():
    session, usb = _armed_session()
    captures = _spy_long_passes(session, usb)
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        me_long_mode="fixed",
        n_passes=2,
    )
    assert [reg for _manual, reg in captures] == [42000, 42000]
    assert session.last_multi_pass_debug.long.n_passes == 2


def test_fixed_mode_is_ignored_without_multi_exposure():
    session, usb = _armed_session()
    captures = _spy_long_passes(session, usb)
    session.run(resolution=1800, area=_TINY, apply_calib=False, me_long_mode="fixed")
    assert captures == []
    assert session.last_me_debug is None


def test_scanner_scan_passes_mode_through():
    scanner = Scanner.open_fake(MODEL_8300I_SE)
    try:
        scanner.scan(
            resolution=1800,
            area=_TINY,
            apply_calib=False,
            multi_exposure=True,
            me_long_mode="fixed",
        )
        debug = scanner.last_me_debug
        assert debug.exposure_long == 60000
        assert debug.exposure_reason == "fixed"
    finally:
        scanner.close()
