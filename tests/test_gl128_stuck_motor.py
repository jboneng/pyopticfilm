# SPDX-License-Identifier: GPL-3.0-or-later
"""B8: a scanner stuck with MOTORENB set and motor power off must stop the scan.

Seen on an 8200i SE after a colour pass: every stationary strip START hangs with
status ``0xc9`` (PWR | BUFEMPTY | HOME | MOTORENB) and ``0x02=0x00`` until a
power-cycle. The driver used to warn, carry on into positioning feeds, and wait
60 s for a park that could not happen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from pyopticfilm.asic import gl128 as gl128_mod
from pyopticfilm.asic.gl128 import Gl128
from pyopticfilm.asic.registers import Gl845Registers
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.select import create_asic
from pyopticfilm.exceptions import ScanError, ScannerStuckError
from pyopticfilm.scan.calibrate import Calibrator
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.scanner import Scanner
from pyopticfilm.usb.fake import MockScannerTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol

_R = Gl845Registers()
_TINY = (0.0, 0.0, 0.08, 0.08)


@dataclass
class StuckMock(MockScannerTransport):
    """Mock whose motor flag can stick on, as on the hardware."""

    stuck: bool = False
    #: Keep ``0x02`` reading back with AGOHOME armed (the older ``0xcd`` case).
    reg02_armed: bool = False
    #: Report the carriage away from home while stuck.
    away: bool = False

    def _status_byte(self) -> int:
        if not self.stuck:
            return super()._status_byte()
        v = _R.STATUS_PWRBIT | _R.STATUS_BUFEMPTY | _R.STATUS_MOTORENB
        if not self.away:
            v |= _R.STATUS_HOMESNR
        return v

    def _tick_motion(self) -> None:
        if not self.stuck:
            super()._tick_motion()

    def _before_register_read(self, addr: int) -> None:
        super()._before_register_read(addr)
        if addr == 0x02 and self.reg02_armed:
            self.registers[0x02] = 0x30  # MTRPWR | AGOHOME


@pytest.fixture(autouse=True)
def _short_idle_wait(monkeypatch):
    monkeypatch.setattr(gl128_mod, "_STATIONARY_IDLE_WAIT_S", 0.1)


def _asic(usb: StuckMock) -> Gl128:
    asic = create_asic(GenesysUsbProtocol(usb), MODEL_8200I_SE)
    asic.init()
    return asic


def test_reset_raises_when_motor_stuck_with_power_off():
    usb = StuckMock()
    asic = _asic(usb)
    usb.stuck = True
    with pytest.raises(ScannerStuckError, match=r"status 0xc9.*Switch the scanner off and on"):
        asic._reset_stationary_scan_engine(where="AFE strip")


def test_reset_is_quiet_on_a_healthy_scanner():
    asic = _asic(StuckMock())
    asic._reset_stationary_scan_engine(where="AFE strip")  # no exception


def test_reset_only_warns_when_agohome_is_still_armed(caplog):
    """The older 0xcd case (AGOHOME left armed) is not the stuck signature."""
    usb = StuckMock()
    asic = _asic(usb)
    usb.stuck = True
    usb.reg02_armed = True
    with caplog.at_level(logging.WARNING, logger="pyopticfilm"):
        asic._reset_stationary_scan_engine(where="AFE strip")
    assert any("scan engine not idle" in r.getMessage() for r in caplog.records)


def test_reset_only_warns_when_carriage_is_not_at_home(caplog):
    usb = StuckMock()
    asic = _asic(usb)
    usb.stuck = True
    usb.away = True
    with caplog.at_level(logging.WARNING, logger="pyopticfilm"):
        asic._reset_stationary_scan_engine(where="AFE strip")
    assert any("scan engine not idle" in r.getMessage() for r in caplog.records)


def test_scan_stops_before_any_feed_when_stuck(tmp_path: Path, monkeypatch):
    usb = StuckMock(stuck=True)
    scanner = Scanner.open_fake(MODEL_8200I_SE, usb, calib_cache=tmp_path / "c.json")
    positioned: list[int] = []
    monkeypatch.setattr(
        Gl128, "position_for_full_frame_scan", lambda self, **kw: positioned.append(1)
    )
    try:
        with pytest.raises(ScannerStuckError):
            scanner.scan(resolution=600, area=_TINY)
    finally:
        scanner.close()
    assert positioned == []


def test_ir_calibration_does_not_swallow_a_stuck_scanner(tmp_path: Path):
    """It used to log a warning and return False, and the scan then started feeding."""
    usb = StuckMock()
    asic = _asic(usb)
    asic._motor_moves_enabled = True
    cal = Calibrator(asic, cache_path=tmp_path / "c.json", model=MODEL_8200I_SE)
    usb.stuck = True
    with pytest.raises(ScannerStuckError):
        cal.measure_ir_host_flatten(compute_geometry(600, model=MODEL_8200I_SE, area=_TINY))


def test_stuck_error_is_a_scan_error():
    """Callers that already catch ScanError keep working."""
    assert issubclass(ScannerStuckError, ScanError)


# --- root cause: re-initialise before stationary strips after an image pass ---


def _boots(usb: StuckMock) -> int:
    """Boot-blob uploads (``write_ahb`` to ``0x000fff00``) seen so far."""
    return sum(
        1
        for t in usb.transactions
        if t.operation == "control_write" and t.value == 0x82 and t.index == 0x01
        and t.data and int.from_bytes(bytes(t.data[0:4]), "little") == 0x000FFF00
    )


def test_reinit_is_a_no_op_without_an_image_pass():
    usb = StuckMock()
    asic = _asic(usb)
    before = _boots(usb)
    asic.reinit_before_stationary_strips()
    assert _boots(usb) == before


def test_reinit_after_image_pass_boots_once_and_keeps_host_state():
    from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE
    from pyopticfilm.scan.calib_gl128 import AfeFrontend

    usb = StuckMock()
    asic = create_asic(GenesysUsbProtocol(usb), MODEL_8300I_SE)
    asic.init()
    afe = AfeFrontend(gains=(30, 25, 30), offsets=(16, 22, 20))
    asic.last_afe = afe
    asic.asic_shading_ready = True
    asic.image_pass_since_init = True
    before = _boots(usb)

    asic.reinit_before_stationary_strips()
    asic.reinit_before_stationary_strips()  # second call: nothing left to do

    assert _boots(usb) == before + 1
    assert asic.image_pass_since_init is False
    assert asic.last_afe is afe  # AFE codes still valid; re-applied by the strips
    assert asic.asic_shading_ready is False  # boot may wipe the table RAM
    clock = [t.data[0] for t in usb.transactions if t.operation == "control_write" and t.request == 0x0C and t.value == 0x8C]
    # Boot leaves the boot clock (0x0E), which runs the 8300i SE motor too fast.
    assert clock[-1] == MODEL_8300I_SE.asic_clock_scan_0x8c


def test_image_configure_flags_the_asic_and_shading_configure_does_not():
    from dataclasses import replace

    from pyopticfilm.scan.session_gl128 import Gl128ScanSession

    usb = StuckMock()
    asic = _asic(usb)
    asic._motor_moves_enabled = False  # configure only, no feeds
    session = Gl128ScanSession(asic, MODEL_8200I_SE)
    geometry = compute_geometry(600, model=MODEL_8200I_SE, area=_TINY)
    session._configure(replace(geometry, disable_buffer_full_move=True))
    assert asic.image_pass_since_init is False
    session._configure(geometry)
    assert asic.image_pass_since_init is True


@pytest.mark.parametrize("entry", ["search_afe", "run_asic_shading"])
def test_stationary_entry_points_reinit_first(monkeypatch, entry):
    usb = StuckMock()
    asic = _asic(usb)
    asic._motor_moves_enabled = False
    calls: list[str] = []
    monkeypatch.setattr(asic, "reinit_before_stationary_strips", lambda: calls.append("reinit"))
    monkeypatch.setattr(asic, "acquire_afe_strip", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("strip")))
    monkeypatch.setattr(asic, "_require_carriage_at_home", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("strip")))
    with pytest.raises(RuntimeError, match="strip"):
        getattr(asic, entry)(resolution=600)
    assert calls == ["reinit"]  # before any strip
