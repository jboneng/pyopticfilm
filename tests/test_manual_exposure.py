# SPDX-License-Identifier: GPL-3.0-or-later
"""Manual (Scan Lab / debug) exposure overrides: validation, GL128 single-pass,
and GL128 multi-exposure (ME) short/long behavior.

Automatic/derived exposure keeps the existing adaptive envelope and hardware
clamp; an explicitly supplied value is written to ``REG_EXPOSURE`` verbatim.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
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
    return _mock_gl128_session_armed_for(MODEL_8200I_SE)


def _mock_gl128_session_armed_for(model) -> tuple[Gl128ScanSession, MockScannerTransport]:
    usb = MockScannerTransport()
    asic = create_asic(GenesysUsbProtocol(usb), model)
    asic._motor_moves_enabled = True
    asic.init()
    return Gl128ScanSession(asic, model), usb


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


def test_me_long_manual_skips_dpi_and_hardware_clamp_and_reaches_register():
    """7200 dpi normally caps ME long at 42000; hardware max is 85000 — go above both."""
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
        me_long_exposure=150000,
    )

    long_captures = [c for c in captures if c[0]]
    assert long_captures, "expected a long pass to run"
    _long_pass, manual, reg_value = long_captures[-1]
    assert manual is True
    assert reg_value == 150000
    assert session.last_me_debug.exposure_long == 150000
    assert session.last_me_debug.exposure_reason == "manual-override"
    assert session.last_me_debug.exposure_proposed is None


@pytest.mark.parametrize("me_exposure_mode", ["adaptive", "fixed"])
def test_me_long_manual_overrides_exposure_mode(me_exposure_mode):
    """``me_exposure_mode='adaptive'`` + ``me_long_exposure=120000`` -> 120000, not adaptive."""
    session, _usb = _mock_gl128_session_armed()
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        me_exposure_mode=me_exposure_mode,
        me_long_exposure=120000,
    )
    assert session.last_me_debug.exposure_long == 120000
    assert session.last_me_debug.exposure_reason == "manual-override"


# --- ME target: clamped manual bracket selection (NegPy) ------------------


def test_me_target_exposure_within_envelope_reaches_register_unchanged():
    session, _usb = _mock_gl128_session_armed()
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        me_target_exposure=60000,
    )
    assert session.last_me_debug.exposure_long == 60000
    assert session.last_me_debug.exposure_reason == "manual-target"


def test_me_target_exposure_below_floor_is_clamped_to_exposure_short():
    session, _usb = _mock_gl128_session_armed()
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        me_target_exposure=5000,  # below exposure_short=14000
    )
    assert session.last_me_debug.exposure_long == MODEL_8200I_SE.exposure_short


def test_me_target_exposure_above_dpi_ceiling_is_clamped_at_7200dpi():
    """Unlike me_long_exposure, me_target_exposure stays inside the DPI ceiling."""
    session, _usb = _mock_gl128_session_armed()
    session.run(
        resolution=7200,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        me_target_exposure=150000,
    )
    assert session.last_me_debug.exposure_long == 42000


def test_me_target_exposure_clamped_to_v2_flat_ceiling_off_7200dpi():
    """V2's ceiling is pinned at 42000 for every DPI, not just 7200 (SE's shape)."""
    session, _usb = _mock_gl128_session_armed_for(MODEL_8100_V2)
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        me_target_exposure=80000,
    )
    assert session.last_me_debug.exposure_long == 42000


def test_me_target_exposure_and_me_long_exposure_are_mutually_exclusive():
    scanner = Scanner.open_fake(MODEL_8200I_SE)
    try:
        with pytest.raises(ValueError):
            scanner.scan(
                resolution=150,
                area=_TINY,
                apply_calib=False,
                me_long_exposure=50000,
                me_target_exposure=50000,
            )
        assert not scanner._asic._initialized
    finally:
        scanner.close()


def test_me_long_default_none_uses_adaptive_selection():
    session, _usb = _mock_gl128_session_armed()
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        me_exposure_mode="adaptive",
    )
    debug = session.last_me_debug
    assert debug.exposure_reason != "manual-override"
    assert debug.exposure_proposed is not None
    assert 14000 <= debug.exposure_long <= MODEL_8200I_SE.me_hardware_max_exposure


def test_me_long_default_none_uses_fixed_selection():
    session, _usb = _mock_gl128_session_armed()
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        me_exposure_mode="fixed",
    )
    debug = session.last_me_debug
    assert debug.exposure_reason == "fixed"
    assert debug.exposure_long == 42000


# --- Scanner.scan() wiring: prime pass must not inherit overrides ---------


def test_scanner_scan_threads_manual_exposure_to_session_not_prime(monkeypatch):
    import pyopticfilm.scan.session as session_module

    scanner = Scanner.open_fake(MODEL_8200I_SE)
    sentinel = object()
    runs: list[dict[str, object]] = []

    class FakeSession:
        last_me_debug = None

        def run(self, **kwargs):
            runs.append(kwargs)
            return sentinel

    monkeypatch.setattr(session_module, "create_session", lambda *args: FakeSession())
    monkeypatch.delenv("POF_GL128_PRIME", raising=False)
    try:
        result = scanner.scan(
            resolution=150,
            area=_TINY,
            apply_calib=False,
            single_pass_exposure=12345,
            me_short_exposure=6789,
            me_long_exposure=99999,
            gl128_prime=True,
        )
    finally:
        scanner.close()

    assert result is sentinel
    assert len(runs) == 2
    prime_kwargs, scan_kwargs = runs
    assert "single_pass_exposure" not in prime_kwargs
    assert "me_short_exposure" not in prime_kwargs
    assert "me_long_exposure" not in prime_kwargs
    assert scan_kwargs["single_pass_exposure"] == 12345
    assert scan_kwargs["me_short_exposure"] == 6789
    assert scan_kwargs["me_long_exposure"] == 99999


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


# --- n_brackets (N-way ME) -------------------------------------------------


def test_n_brackets_default_matches_two_bracket_debug():
    session, _usb = _mock_gl128_session_armed()
    session.run(resolution=1800, area=_TINY, apply_calib=False, multi_exposure=True)
    debug = session.last_me_debug
    assert debug.brackets is not None
    assert len(debug.brackets) == 2
    assert debug.brackets[0].exposure == debug.exposure_short
    assert debug.brackets[-1].exposure == debug.exposure_long


def test_two_bracket_v2_routes_through_banded_luma_only_merge():
    """Model8100V2.me_use_banded_alignment routes n_brackets==2 through
    merge_n_exposures (banded alignment + luma-only misalignment gate),
    not merge_exposures_result's whole-frame-shift + AND-gated path."""
    from pyopticfilm.scan import exposure_merge

    session, _usb = _mock_gl128_session_armed_for(MODEL_8100_V2)
    with (
        patch.object(
            exposure_merge, "merge_n_exposures", wraps=exposure_merge.merge_n_exposures
        ) as spy_n,
        patch.object(
            exposure_merge, "merge_exposures_result", wraps=exposure_merge.merge_exposures_result
        ) as spy_pairwise,
    ):
        session.run(resolution=1800, area=_TINY, apply_calib=False, multi_exposure=True)
    assert spy_n.call_count == 1
    assert spy_pairwise.call_count == 0


def test_two_bracket_se_keeps_original_pairwise_merge():
    """SE (me_use_banded_alignment=False) is unaffected — still routes
    n_brackets==2 through merge_exposures_result, byte-identical."""
    from pyopticfilm.scan import exposure_merge

    session, _usb = _mock_gl128_session_armed()  # defaults to MODEL_8200I_SE
    with (
        patch.object(
            exposure_merge, "merge_n_exposures", wraps=exposure_merge.merge_n_exposures
        ) as spy_n,
        patch.object(
            exposure_merge, "merge_exposures_result", wraps=exposure_merge.merge_exposures_result
        ) as spy_pairwise,
    ):
        session.run(resolution=1800, area=_TINY, apply_calib=False, multi_exposure=True)
    assert spy_pairwise.call_count == 1
    assert spy_n.call_count == 0


def test_n_brackets_five_captures_five_ascending_exposures():
    session, _usb = _mock_gl128_session_armed()
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        n_brackets=5,
    )
    debug = session.last_me_debug
    assert debug.brackets is not None
    assert len(debug.brackets) == 5
    exposures = [b.exposure for b in debug.brackets]
    assert exposures == sorted(exposures)
    assert len(set(exposures)) == 5
    assert exposures[0] == debug.exposure_short
    assert exposures[-1] == debug.exposure_long


def test_n_brackets_nine_is_accepted():
    session, _usb = _mock_gl128_session_armed()
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        n_brackets=9,
    )
    assert len(session.last_me_debug.brackets) == 9


@pytest.mark.parametrize("n_brackets", [1, 10])
def test_n_brackets_out_of_range_raises(n_brackets):
    scanner = Scanner.open_fake(MODEL_8200I_SE)
    try:
        with pytest.raises(ValueError):
            scanner.scan(
                resolution=1800,
                area=_TINY,
                apply_calib=False,
                multi_exposure=True,
                n_brackets=n_brackets,
            )
    finally:
        scanner.close()


# --- n_brackets model-aware exposure-mode default --------------------------
#
# n_brackets > 2 with no explicit me_exposure_mode defers to
# Model.me_default_exposure_mode: "fixed" (pinned to the one
# real-hardware-validated exposure) on the 8100 V2, "adaptive" on the SE
# (no dedicated N-bracket hardware validation for SE yet). n_brackets == 2
# is unaffected either way — always "adaptive" by default, unchanged.


def test_n_brackets_v2_defaults_to_fixed_when_unset():
    session, _usb = _mock_gl128_session_armed_for(MODEL_8100_V2)
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        n_brackets=5,
    )
    debug = session.last_me_debug
    assert debug.exposure_reason == "fixed"
    assert debug.exposure_long == 42000


def test_n_brackets_se_defaults_to_adaptive_when_unset():
    session, _usb = _mock_gl128_session_armed_for(MODEL_8200I_SE)
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        n_brackets=5,
    )
    debug = session.last_me_debug
    assert debug.exposure_reason != "fixed"


def test_n_brackets_two_v2_still_defaults_to_adaptive():
    """n_brackets == 2 is unaffected by the model default — unchanged behavior."""
    session, _usb = _mock_gl128_session_armed_for(MODEL_8100_V2)
    session.run(resolution=1800, area=_TINY, apply_calib=False, multi_exposure=True)
    debug = session.last_me_debug
    assert debug.exposure_reason != "fixed"


def test_n_brackets_explicit_mode_overrides_v2_default():
    session, _usb = _mock_gl128_session_armed_for(MODEL_8100_V2)
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        n_brackets=5,
        me_exposure_mode="adaptive",
    )
    debug = session.last_me_debug
    assert debug.exposure_reason != "fixed"
