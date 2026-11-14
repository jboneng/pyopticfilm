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

from unittest.mock import MagicMock

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


def test_ir_combo_keeps_headroom_cap_byte_identical_to_main():
    """n_passes=1 is documented as structurally byte-identical to main for
    every existing call shape (plain, ME, IR-combo) — that must include the
    expose_film_base headroom cap, which main always set True on the
    _run_multi_pass path (color+IR combo routes there even at n_passes=1).
    Only the new n_passes>1-without-ME stacking mode is allowed to relax it."""
    session, _usb = _mock_gl128_session_armed()
    seen: list[bool] = []
    orig = session.pipeline.expose_film_base

    def spy(*args, **kwargs):
        seen.append(kwargs.get("preserve_headroom"))
        return orig(*args, **kwargs)

    session.pipeline.expose_film_base = spy
    session.run(resolution=1800, area=_TINY, apply_calib=False, infrared=True, n_passes=1)
    assert seen == [True]


def test_multi_pass_without_me_relaxes_headroom_cap():
    """Genuine plain Multi-Pass stacking (n_passes>1, no ME) is the one case
    intended to stretch highlights like Single-Pass rather than cap them."""
    session, _usb = _mock_gl128_session_armed()
    seen: list[bool] = []
    orig = session.pipeline.expose_film_base

    def spy(*args, **kwargs):
        seen.append(kwargs.get("preserve_headroom"))
        return orig(*args, **kwargs)

    session.pipeline.expose_film_base = spy
    session.run(resolution=1800, area=_TINY, apply_calib=False, n_passes=3)
    assert seen == [False]


def test_invalid_manual_long_exposure_raises_before_carriage_moves():
    """A manual me_long_exposure that overflows the 16-bit AHB exposure table
    (bypassing the soft hardware-max clamp, same as issue #66's real jam)
    must raise before position_for_full_frame_scan moves the carriage — not
    after, which would strand it mid-window."""
    session, _usb = _mock_gl128_session_armed()
    moved: list[bool] = []
    orig = session.asic.position_for_full_frame_scan

    def spy(*args, **kwargs):
        moved.append(True)
        return orig(*args, **kwargs)

    session.asic.position_for_full_frame_scan = spy
    with pytest.raises(ValueError):
        session.run(
            resolution=3600,
            area=_TINY,
            apply_calib=False,
            multi_exposure=True,
            # 3600 dpi halves the table word: 140000 / 2 > 0xFFFF. Manual
            # overrides bypass me_hardware_max_exposure.
            me_long_exposure=140000,
        )
    # The short pass (valid exposure) legitimately moves once; the long
    # pass's own invalid exposure must raise before ITS motor move, so the
    # carriage must not move a second time for the pass that's about to fail.
    assert moved == [True]


def test_colour_ir_scan_calibrates_ir_between_colour_and_ir_passes():
    session, _usb = _mock_gl128_session_armed()
    order: list[str] = []
    calibrator = MagicMock()
    calibrator.ensure_colour_asic_shading.side_effect = lambda _geo: order.append("colour_calib")
    calibrator.measure_ir_host_flatten.side_effect = lambda _geo: (order.append("ir_calib") or True)
    session.calibrator = calibrator
    orig = session.acquire_raw

    def spy(geometry, *, method, **kwargs):
        order.append(f"acquire_{method}")
        return orig(geometry, method=method, **kwargs)

    session.acquire_raw = spy  # type: ignore[method-assign]
    session.run(resolution=1800, area=_TINY, infrared=True, n_passes=1)

    assert order == ["colour_calib", "acquire_transparency", "ir_calib", "acquire_infrared"]
    calibrator.find_for_scan.assert_not_called()


@pytest.mark.parametrize("kwargs", [{"infrared": True}, {"mode": "infrared"}])
def test_uncalibrated_ir_scan_does_not_flatten_with_an_earlier_profile(monkeypatch, kwargs):
    """apply_calib=False: a profile left by an earlier scan fits another window."""
    from pyopticfilm.scan import calib_gl128

    session, _usb = _mock_gl128_session_armed()
    session.asic.last_ir_host_white = [40000] * 6000
    session.asic.ir_host_flatten_ready = True
    calls: list[int] = []
    orig = calib_gl128.flatten_ir_columns

    def spy(plane, white, **kw):
        calls.append(len(white))
        return orig(plane, white, **kw)

    monkeypatch.setattr(calib_gl128, "flatten_ir_columns", spy)
    image = session.run(resolution=1800, area=_TINY, apply_calib=False, n_passes=1, **kwargs)

    assert image.ir is not None
    assert calls == []
    assert session.asic.ir_host_flatten_ready is False
    assert session.asic.last_ir_host_white is None


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


def test_single_pass_exposure_ignored_for_adaptive_multi_pass_combo():
    """single_pass_exposure must also stay ignored when n_passes>1 AND
    multi_exposure=True (Adaptive Multi-Pass) — it's a fallback for the
    plain Multi-Pass mode only, not a substitute for me_short_exposure."""
    session, _usb = _mock_gl128_session_armed()
    session.run(
        resolution=1800,
        area=_TINY,
        apply_calib=False,
        multi_exposure=True,
        n_passes=3,
        single_pass_exposure=30000,
    )
    assert session.last_me_debug.exposure_short == MODEL_8200I_SE.exposure_short
