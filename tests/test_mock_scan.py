# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless mock-scanner scans (no PyQt, no hardware)."""

from __future__ import annotations

import threading

import pytest

from pyopticfilm.asic.registers import Gl845Registers
from pyopticfilm.asic.status import ScannerStatus
from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.select import create_asic, model_is_scan_ready
from pyopticfilm.exceptions import AsicError, MotorTimeoutError, ScanCancelled
from pyopticfilm.scan.session import create_session
from pyopticfilm.scanner import Scanner
from pyopticfilm.usb.fake import FakeDeviceHandle, MockScannerTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol
from pyopticfilm.usb.trace import RecordingTransport, UsbTransaction, format_usb_log_line

_TINY = (0.0, 0.0, 0.08, 0.08)


def test_scan_ready_unchanged_by_open_fake():
    assert model_is_scan_ready(MODEL_8200I) is False
    scanner = Scanner.open_fake(MODEL_8200I)
    try:
        assert model_is_scan_ready(scanner.model) is False
        assert scanner._allow_unvalidated_scan is True
    finally:
        scanner.close()


def test_real_path_still_locks_non_se_without_open_fake():
    handle = FakeDeviceHandle.for_model(MODEL_8200I)
    proto = GenesysUsbProtocol(MockScannerTransport())
    scanner = Scanner(handle, proto, model=MODEL_8200I)
    try:
        with pytest.raises(AsicError, match="locked out"):
            scanner.scan(resolution=900, area=_TINY, apply_calib=False)
    finally:
        scanner.close()


def test_open_fake_8200i_scan_pattern_and_progress():
    seen: list[float] = []
    scanner = Scanner.open_fake(MODEL_8200I)
    try:
        image = scanner.scan(
            resolution=900,
            area=_TINY,
            apply_calib=False,
            progress=seen.append,
        )
    finally:
        scanner.close()
    assert image.rgb.ndim == 3 and image.rgb.shape[2] == 3
    assert image.rgb.max() > 0
    assert seen and seen[-1] == pytest.approx(1.0)


def test_open_fake_cancel():
    scanner = Scanner.open_fake(MODEL_8200I)
    cancel = threading.Event()
    cancel.set()
    try:
        with pytest.raises(ScanCancelled):
            scanner.scan(resolution=900, area=_TINY, apply_calib=False, cancel=cancel)
    finally:
        scanner.close()


def test_gl128_session_mock_scan():
    usb = MockScannerTransport()
    asic = create_asic(GenesysUsbProtocol(usb), MODEL_8200I_SE)
    asic._motor_moves_enabled = True
    image = create_session(asic, MODEL_8200I_SE).run(
        resolution=150,
        area=_TINY,
        apply_calib=False,
    )
    assert image.rgb.max() > 0
    assert any(t.operation == "bulk_read" for t in usb.transactions)


def test_recording_transport_listener():
    lines: list[str] = []

    def on_txn(txn: UsbTransaction) -> None:
        lines.append(format_usb_log_line(txn))

    rec = RecordingTransport(MockScannerTransport(), listener=on_txn)
    proto = GenesysUsbProtocol(rec)
    proto.write_register(0x01, 0x22)
    assert rec.transactions
    assert lines and "control_write" in lines[0]


def test_gl845_home_uses_reverse_mini_scan_and_stops():
    from pyopticfilm.device.select import MODEL_7600I_V2

    transport = MockScannerTransport()
    scanner = Scanner.open_fake(MODEL_7600I_V2, transport=transport)
    try:
        scanner.asic.init()
        scanner.lamp_on()
        transport.at_home = False
        transport._sync_status()

        scanner.home(timeout_s=1.0)

        status = scanner.status()
        registers = Gl845Registers()
        assert status.is_at_home
        assert not status.is_motor_enabled
        # Parking between calibration and image passes must not cool the lamp.
        assert status.is_lamp_on
        assert transport.registers[0x02] & (
            registers.AGOHOME | registers.NOTHOME | registers.MTRREV
        ) == registers.AGOHOME | registers.NOTHOME | registers.MTRREV
        assert (transport.registers[0x25], transport.registers[0x26], transport.registers[0x27]) == (
            0,
            0,
            3,
        )
        assert not transport.registers[0x01] & registers.SCAN
        assert not transport.registers[0x40] & (
            registers.DATAENB | registers.MOTMFLG
        )
    finally:
        scanner.close()


def test_gl845_stop_motor_reports_motor_still_enabled():
    from pyopticfilm.device.select import MODEL_7600I_V2

    scanner = Scanner.open_fake(MODEL_7600I_V2)
    try:
        scanner.asic.init()
        read_status = scanner.asic.read_status
        scanner.asic.read_status = lambda: ScannerStatus.from_reg41(
            Gl845Registers().STATUS_MOTORENB
        )
        try:
            with pytest.raises(MotorTimeoutError, match="Could not stop GL845 motor"):
                scanner.asic.stop_motor(timeout_s=0)
        finally:
            scanner.asic.read_status = read_status
    finally:
        scanner.close()


def test_stop_failure_does_not_mask_capture_error():
    from pyopticfilm.exceptions import ScanError

    scanner = Scanner.open_fake(MODEL_8200I)
    try:
        scanner.asic.init()
        session = create_session(scanner.asic, MODEL_8200I)

        def _boom(*_a, **_kw):
            raise ScanError("data timeout")

        def _stuck(**_kw):
            raise MotorTimeoutError("Could not stop GL845 motor")

        session._acquire = _boom
        scanner.asic.stop_motor = _stuck
        with pytest.raises(ScanError, match="data timeout"):
            session.run(resolution=900, area=_TINY, apply_calib=False)
    finally:
        scanner.close()


def test_lamp_settles_only_when_relit(monkeypatch):
    import pyopticfilm.scan.session as session_mod

    sleeps: list[float] = []
    real_sleep = session_mod.time.sleep
    monkeypatch.setattr(
        session_mod.time, "sleep", lambda s: sleeps.append(s) or real_sleep(min(s, 0.01))
    )
    scanner = Scanner.open_fake(MODEL_8200I)
    try:
        scanner.asic.init()
        scanner.lamp_off()
        scanner.scan(resolution=900, area=_TINY, apply_calib=False)
        assert MODEL_8200I.lamp_settle_s in sleeps
        sleeps.clear()
        scanner.scan(resolution=900, area=_TINY, apply_calib=False)
        assert MODEL_8200I.lamp_settle_s not in sleeps
    finally:
        scanner.close()


def test_gl845_dark_calib_capture_keeps_lamp_off(tmp_path):
    from pyopticfilm.device.select import MODEL_7600I_V2
    from pyopticfilm.scan.calibrate import CalibCache

    cache_path = tmp_path / "calib.json"
    scanner = Scanner.open_fake(MODEL_7600I_V2, calib_cache=cache_path)
    try:
        scanner.scan(resolution=900, area=_TINY, apply_calib=True)
    finally:
        scanner.close()

    cache = CalibCache(cache_path)
    assert cache.load()
    entry = next(e for e in cache.entries if e.method == "transparency")
    # Before the fix _configure re-lit the lamp, so dark ≈ white.
    assert float(entry.dark.mean()) < 0.5 * float(entry.white.mean())


def test_feed_wait_reports_stall_quickly(monkeypatch):
    import pyopticfilm.scan.session as session_mod

    monkeypatch.setattr(session_mod, "FEED_STALL_S", 0.05)
    scanner = Scanner.open_fake(MODEL_8200I)
    session = create_session(scanner.asic, MODEL_8200I)
    try:
        session._feedl = 1000
        session._read_feed_steps = lambda: 10
        with pytest.raises(MotorTimeoutError, match="stalled at 10/1000"):
            session._wait_feed(None)
    finally:
        scanner.close()


def test_feed_timeout_scales_with_exposure(monkeypatch):
    import pyopticfilm.scan.session as session_mod

    clock = iter(range(10_000))
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(session_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(session_mod, "FEED_STALL_S", 1e9)
    scanner = Scanner.open_fake(MODEL_8200I)
    session = create_session(scanner.asic, MODEL_8200I)
    steps = iter(range(10_000))
    try:
        session._feedl = 10_000
        session._lperiod = MODEL_8200I.exposure_lperiod * 3
        session._read_feed_steps = lambda: next(steps)
        with pytest.raises(MotorTimeoutError, match="within 180s"):
            session._wait_feed(None)
    finally:
        scanner.close()


def test_gl845_single_pass_failure_parks_head():
    from pyopticfilm.exceptions import ScanError

    scanner = Scanner.open_fake(MODEL_8200I)
    try:
        scanner.asic.init()
        session = create_session(scanner.asic, MODEL_8200I)
        parks: list[int] = []
        scanner.asic.park = lambda **_kw: parks.append(1)

        def _boom(*_a, **_kw):
            raise ScanError("data timeout")

        session._acquire = _boom
        with pytest.raises(ScanError, match="data timeout"):
            session.run(resolution=900, area=_TINY, apply_calib=False)
        assert parks == [1]
    finally:
        scanner.close()


def test_gl845_infrared_calib_measures_dark(tmp_path):
    from pyopticfilm.device.select import MODEL_7600I_V2

    scanner = Scanner.open_fake(MODEL_7600I_V2, calib_cache=tmp_path / "calib.json")
    try:
        scanner.scan(resolution=900, area=_TINY, mode="infrared", apply_calib=True)
        entry = scanner._calibrator._active
        assert entry.method == "infrared"
        # IR LED and lamp off: a real (dim, non-zero) offset, not the old zeros.
        assert 0 < float(entry.dark.mean()) < 0.5 * float(entry.white.mean())
    finally:
        scanner.close()


def test_gl845_init_stops_scan_left_running_by_dead_session():
    from pyopticfilm.device.select import MODEL_7600I_V2

    transport = MockScannerTransport()
    scanner = Scanner.open_fake(MODEL_7600I_V2, transport=transport)
    try:
        regs = Gl845Registers()
        # A previous process died mid-scan: SCAN set, data still buffered.
        transport.registers[0x01] = 0x22 | regs.SCAN
        transport.buffer_empty = False
        transport._sync_status()
        aborts: list[int] = []
        real_abort = scanner.asic.protocol.abort_bulk_stream
        scanner.asic.protocol.abort_bulk_stream = lambda: aborts.append(1) or real_abort()
        resets: list[int] = []
        real_reset = scanner.asic.soft_reset
        scanner.asic.soft_reset = lambda: resets.append(1) or real_reset()

        scanner.asic.init()

        assert aborts == [1] and resets == [1]
        assert not transport.registers[0x01] & regs.SCAN
    finally:
        scanner.close()


def test_gl845_init_leaves_idle_scanner_alone():
    from pyopticfilm.device.select import MODEL_7600I_V2

    scanner = Scanner.open_fake(MODEL_7600I_V2)
    try:
        aborts: list[int] = []
        scanner.asic.protocol.abort_bulk_stream = lambda: aborts.append(1) or 0
        scanner.asic.init()
        assert aborts == []
    finally:
        scanner.close()
