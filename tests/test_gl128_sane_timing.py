# SPDX-License-Identifier: GPL-3.0-or-later
"""SilverFast timings taken from the SANE GL128 driver."""

from __future__ import annotations

from pyopticfilm.asic.gl128 import Gl128
from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.scan.calib_gl128 import parity_dark_from_columns
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.scan.session_gl128 import _LINE_PERIOD_TO_SECONDS, Gl128ScanSession
from pyopticfilm.usb.fake import MockScannerTransport
from pyopticfilm.usb.protocol import VALUE_BUF_ENDACCESS, GenesysUsbProtocol


def test_se_7200_width_program_matches_silverfast_rows():
    narrow = MODEL_8200I_SE.timing_for_native_width(7200, 1416)
    assert narrow is not None
    lperiod, image, dark, white = narrow
    assert lperiod == 11559
    assert image == (0x03, 0x01, 0x01)
    assert dark == (0x04, 0x01, 0x30)
    assert white == (0x02, 0x02, 0x02)
    assert MODEL_8200I_SE.timing_for_native_width(7200, 10200)[0] == 15951
    assert MODEL_8200I_SE.timing_for_native_width(7200, 10201) is None
    assert MODEL_8200I_SE.timing_for_native_width(1800, 1416) is None
    assert MODEL_8200I_SE.line_period_for(7200) == 15963


def test_v2_7200_line_period_stays_capture_value():
    assert MODEL_8100_V2.line_period_for(7200) == 16035
    assert not hasattr(MODEL_8100_V2, "timing_for_native_width")


def test_se_configure_uses_width_program_at_7200(monkeypatch):
    monkeypatch.setattr("pyopticfilm.asic.gl128.time.sleep", lambda _seconds: None)
    usb = MockScannerTransport()
    asic = Gl128(GenesysUsbProtocol(usb), MODEL_8200I_SE)
    asic._motor_moves_enabled = False
    session = Gl128ScanSession(asic, MODEL_8200I_SE)
    geo = compute_geometry(7200, model=MODEL_8200I_SE, area=(0.40, 0.40, 0.42, 0.45))
    width = geo.pixel_endx - geo.pixel_startx
    program = MODEL_8200I_SE.timing_for_native_width(7200, width)
    assert program is not None
    session._configure(geo)
    lperiod = (usb.registers[0x28] << 16) | (usb.registers[0x29] << 8) | usb.registers[0x2A]
    assert lperiod == program[0]
    assert usb.registers[0x2B] == program[1][0]
    assert usb.registers[0xA5] == program[1][1]
    assert usb.registers[0xAB] == program[1][2]


def test_v2_configure_keeps_7200_capture_lperiod(monkeypatch):
    monkeypatch.setattr("pyopticfilm.asic.gl128.time.sleep", lambda _seconds: None)
    usb = MockScannerTransport()
    asic = Gl128(GenesysUsbProtocol(usb), MODEL_8100_V2)
    asic._motor_moves_enabled = False
    session = Gl128ScanSession(asic, MODEL_8100_V2)
    geo = compute_geometry(7200, model=MODEL_8100_V2, area=(0.40, 0.40, 0.42, 0.45))
    session._configure(geo)
    lperiod = (usb.registers[0x28] << 16) | (usb.registers[0x29] << 8) | usb.registers[0x2A]
    assert lperiod == 16035


def test_pace_falls_back_when_window_ends_are_not_ints():
    session = Gl128ScanSession(object(), MODEL_8200I_SE)

    class _Ends:
        resolution = 7200
        pixel_startx = object()
        pixel_endx = object()

    assert session._line_interval_s(_Ends()) == 15963 * _LINE_PERIOD_TO_SECONDS


def test_host_stagger_at_odd_column_steps():
    assert compute_geometry(7200, model=MODEL_8200I_SE, area=(0.4, 0.4, 0.5, 0.5)).stagger_y == (4, 0)
    assert compute_geometry(2400, model=MODEL_8200I_SE, area=(0.4, 0.4, 0.5, 0.5)).stagger_y == (2, 0)
    assert compute_geometry(1440, model=MODEL_8200I_SE, area=(0.4, 0.4, 0.5, 0.5)).stagger_y == (1, 0)
    assert compute_geometry(1800, model=MODEL_8200I_SE, area=(0.4, 0.4, 0.5, 0.5)).stagger_y == ()
    assert compute_geometry(7200, model=MODEL_8100_V2, area=(0.4, 0.4, 0.5, 0.5)).stagger_y == (4, 0)


def test_parity_dark_keeps_even_and_odd_means():
    columns = [(800, 979, 895), (1134, 1285, 1221), (802, 981, 897), (1130, 1281, 1219)]
    rows = parity_dark_from_columns(columns)
    assert rows[0] == rows[2] == (801, 980, 896)
    assert rows[1] == rows[3] == (1132, 1283, 1220)


def test_boot_sends_clock_requests_and_both_lamp_trains(monkeypatch):
    monkeypatch.setattr("pyopticfilm.asic.gl128.time.sleep", lambda _seconds: None)
    usb = MockScannerTransport()
    asic = Gl128(GenesysUsbProtocol(usb), MODEL_8200I_SE)
    seen: list[tuple[int, int]] = []
    real = asic._write

    def _record(addr: int, value: int) -> None:
        seen.append((int(addr), int(value)))
        real(addr, value)

    asic._write = _record
    asic.asic_boot()
    lamp = [value for addr, value in seen if addr == 0x03]
    assert lamp[-6:] == [0x10, 0x00, 0x20, 0x30, 0x20, 0x30]
    clocks = [
        (txn.index, txn.data)
        for txn in usb.transactions
        if txn.operation == "control_write" and txn.value == VALUE_BUF_ENDACCESS
    ]
    assert clocks == [(0x10, b"\x0c"), (0x13, b"\x0c"), (0x10, b"\x0c"), (0x13, b"\x0c")]
    assert usb.registers[0x13] == 0x08
    assert asic._lamp_boot_sequence_pending

    seen.clear()
    asic._second_lamp_train()
    lamp = [value for addr, value in seen if addr == 0x03]
    assert lamp == [0x20, 0x00, 0x20, 0x20, 0x30, 0x20, 0x30]
    assert asic._lamp_boot_sequence_pending is False
    seen.clear()
    asic._second_lamp_train()
    assert seen == []
