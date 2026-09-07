# SPDX-License-Identifier: GPL-3.0-or-later
"""Multi-Pass / Adaptive Multi-Pass session-level (mocked ASIC) tests.

n_passes crossed with multi_exposure gives four modes:

  multi_exposure=False, n_passes=1  -> Single-Pass (not exercised here,
                                        covered by existing session tests)
  multi_exposure=True,  n_passes=1  -> Adaptive Multi-Exposure (today's ME,
                                        must stay byte-identical)
  multi_exposure=False, n_passes>1  -> Multi-Pass
  multi_exposure=True,  n_passes>1  -> Adaptive Multi-Pass
"""

from __future__ import annotations

import pytest

from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.select import create_asic
from pyopticfilm.exceptions import ScanError
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


def _mock_gl128_session_armed() -> tuple[Gl128ScanSession, MockScannerTransport]:
    usb = MockScannerTransport()
    asic = create_asic(GenesysUsbProtocol(usb), MODEL_8200I_SE)
    asic._motor_moves_enabled = True
    asic.init()
    return Gl128ScanSession(asic, MODEL_8200I_SE), usb


def test_n_passes_one_does_not_populate_multi_pass_debug():
    session, _usb = _mock_gl128_session_armed()
    session.run(resolution=1800, area=_TINY, apply_calib=False, n_passes=1)
    assert session.last_multi_pass_debug is None


def test_n_passes_one_with_me_stays_on_todays_path():
    """n_passes=1, multi_exposure=True: today's exact ME path, no Multi-Pass debug."""
    session, _usb = _mock_gl128_session_armed()
    session.run(resolution=1800, area=_TINY, apply_calib=False, multi_exposure=True, n_passes=1)
    assert session.last_me_debug is not None
    assert session.last_multi_pass_debug is None


def test_multi_pass_without_me_stacks_short_only():
    session, _usb = _mock_gl128_session_armed()
    image = session.run(resolution=1800, area=_TINY, apply_calib=False, n_passes=3)
    assert image.rgb is not None
    debug = session.last_multi_pass_debug
    assert debug is not None
    assert debug.short.n_passes == 3
    assert len(debug.short.align_shifts) == 2  # repeats 2 and 3 vs repeat 1
    assert debug.short.stack_stats is not None
    assert debug.short.stack_stats.n_frames == 3
    assert debug.long is None
    assert session.last_me_debug is None


def test_adaptive_multi_pass_stacks_both_slots_then_fuses():
    session, _usb = _mock_gl128_session_armed()
    image = session.run(
        resolution=1800, area=_TINY, apply_calib=False, multi_exposure=True, n_passes=3
    )
    assert image.rgb is not None
    debug = session.last_multi_pass_debug
    assert debug is not None
    assert debug.short.n_passes == 3
    assert debug.long is not None
    assert debug.long.n_passes == 3
    assert debug.long.stack_stats is not None
    assert debug.long.stack_stats.n_frames == 3
    # Cross-exposure fuse still ran on the stacked short/long planes —
    # merge_exposures_result itself is untouched by Multi-Pass.
    assert session.last_me_debug is not None
    assert session.last_me_debug.fusion_stats is not None


def test_infrared_with_n_passes_greater_than_one_rejected():
    session, _usb = _mock_gl128_session_armed()
    with pytest.raises(ScanError):
        session.run(resolution=1800, area=_TINY, apply_calib=False, infrared=True, n_passes=2)


@pytest.mark.parametrize("n_passes", [0, -1, 10])
def test_n_passes_out_of_range_rejected(n_passes):
    session, _usb = _mock_gl128_session_armed()
    with pytest.raises(ValueError):
        session.run(resolution=1800, area=_TINY, apply_calib=False, n_passes=n_passes)


def test_scanner_scan_n_passes_out_of_range_rejected_before_opening_asic():
    scanner = Scanner.open_fake(MODEL_8200I_SE)
    try:
        with pytest.raises(ValueError):
            scanner.scan(resolution=150, area=_TINY, apply_calib=False, n_passes=10)
        assert not scanner._asic._initialized
    finally:
        scanner.close()


def test_scanner_scan_n_passes_greater_than_one_not_implemented_for_non_gl128():
    scanner = Scanner.open_fake(MODEL_8200I)
    try:
        with pytest.raises(NotImplementedError):
            scanner.scan(resolution=900, area=_TINY, apply_calib=False, n_passes=2)
    finally:
        scanner.close()


def test_single_pass_exposure_overrides_short_when_multi_pass_without_me():
    """single_pass_exposure is a fallback short-slot override, but only when
    n_passes>1 — that's the only new surface reaching _run_multi_pass with
    multi_exposure=False."""
    session, usb = _mock_gl128_session_armed()
    captures: list[tuple[bool, bool, int]] = []
    original_configure = session._configure

    def spy(geometry):
        original_configure(geometry)
        captures.append((session._pass_long_exposure, session._pass_manual, _reg_exposure(usb)))

    session._configure = spy  # type: ignore[method-assign]

    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        n_passes=2,
        single_pass_exposure=30000,
    )
    assert len(captures) == 2  # two short-slot repeats, no long pass
    for _long_pass, manual, reg_value in captures:
        assert manual is True
        assert reg_value == 30000


def test_single_pass_exposure_ignored_for_n_passes_one_me_combo():
    """Byte-identical guarantee: at n_passes=1, single_pass_exposure must
    stay ignored for multi_exposure/IR-combo call shapes, exactly as before
    this parameter reached _run_multi_pass."""
    session, _usb = _mock_gl128_session_armed()
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        n_passes=1,
        single_pass_exposure=30000,
    )
    assert session.last_me_debug.exposure_short == MODEL_8200I_SE.exposure_short
