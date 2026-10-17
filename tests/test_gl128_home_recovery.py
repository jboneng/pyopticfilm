# SPDX-License-Identifier: GPL-3.0-or-later
"""GL128 carriage recovery: reverse seek, back-off, reverse seek."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

import pyopticfilm.asic.gl128 as gl128_mod
from pyopticfilm.asic.gl128 import Gl128
from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE
from pyopticfilm.device.select import create_asic
from pyopticfilm.exceptions import AsicError, MotorTimeoutError
from pyopticfilm.scan.calibrate import Calibrator
from pyopticfilm.scanner import Scanner
from pyopticfilm.usb.fake import MockScannerTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol

GL128_MODELS = [MODEL_8200I_SE, MODEL_8100_V2, MODEL_8300I_SE]
REVERSE_SEEK = 0x1C  # MTRPWR | FASTFED | MTRREV
FORWARD_FEED = 0x18  # MTRPWR | FASTFED


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(gl128_mod.time, "sleep", lambda *_a, **_k: None)


def _asic(model, usb: MockScannerTransport) -> tuple[Gl128, list[tuple[str, int]]]:
    asic = create_asic(GenesysUsbProtocol(usb), model)
    asic._motor_moves_enabled = True
    asic._initialized = True
    log: list[tuple[str, int]] = []
    r = asic.registers
    orig_write = asic._write
    orig_u24 = asic.protocol.write_u24
    orig_slopes = asic._upload_fast_slopes

    def _write(address: int, value: int) -> None:
        if address == r.REG_0x02:
            log.append(("0x02", value))
        elif address == r.REG_START:
            log.append(("start", value))
        orig_write(address, value)

    def _write_u24(address: int, value: int) -> None:
        if address == r.REG_FEEDL:
            log.append(("feedl", value))
        orig_u24(address, value)

    def _slopes(*, use_slow: bool = False) -> None:
        log.append(("slopes", 1 if use_slow else 0))
        orig_slopes(use_slow=use_slow)

    asic._write = _write
    asic.protocol.write_u24 = _write_u24
    asic._upload_fast_slopes = _slopes
    return asic, log


def _arm_sequence(log: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Only the arming writes (feed length, motor mode, ramp, start)."""
    return [
        e
        for e in log
        if (e[0] == "feedl" and e[1] != 1) or e[0] in ("slopes", "start") or e in (("0x02", REVERSE_SEEK), ("0x02", FORWARD_FEED))
    ]


@pytest.mark.parametrize("model", GL128_MODELS, ids=lambda m: m.model)
def test_at_home_with_a_good_park_does_not_move(model):
    usb = MockScannerTransport()
    asic, log = _asic(model, usb)

    assert asic.recover_home() is False

    assert not any(kind == "start" for kind, _ in log)


@pytest.mark.parametrize("model", GL128_MODELS, ids=lambda m: m.model)
def test_away_from_home_seeks_backs_off_and_seeks_again(model):
    usb = MockScannerTransport(at_home=False)
    asic, log = _asic(model, usb)

    assert asic.recover_home() is True

    seek = [
        ("feedl", model.home_seek_max_steps),
        ("0x02", REVERSE_SEEK),
        ("slopes", 1),
        ("start", 1),
    ]
    backoff = [
        ("feedl", model.home_backoff_steps),
        ("0x02", FORWARD_FEED),
        ("slopes", 1),
        ("start", 1),
    ]
    assert _arm_sequence(log) == seek + backoff + seek
    assert usb.at_home
    assert asic._park_ok


def test_recovery_reboots_registers_before_moving_but_not_when_home():
    usb = MockScannerTransport(at_home=False)
    asic, log = _asic(MODEL_8200I_SE, usb)
    boots: list[int] = []
    orig_boot = asic.asic_boot

    def _boot() -> None:
        boots.append(len(log))
        orig_boot()

    asic.asic_boot = _boot

    asic.recover_home()

    assert len(boots) == 1
    assert ("0x02", REVERSE_SEEK) not in log[: boots[0]]
    assert asic.recover_home() is False
    assert len(boots) == 1


def test_at_home_after_a_failed_park_backs_off_then_seeks_once():
    usb = MockScannerTransport()
    asic, log = _asic(MODEL_8200I_SE, usb)
    asic._park_ok = False

    assert asic.recover_home() is True

    starts = [v for kind, v in log if kind == "0x02" and v in (REVERSE_SEEK, FORWARD_FEED)]
    assert starts == [FORWARD_FEED, REVERSE_SEEK]
    assert asic._park_ok


def test_backoff_that_stays_on_the_sensor_skips_the_second_seek():
    usb = MockScannerTransport()
    asic, log = _asic(MODEL_8200I_SE, usb)
    asic._park_ok = False
    orig_feed = asic._feed_capture

    def _feed_inside_zone(*args, **kwargs):
        orig_feed(*args, **kwargs)
        usb.at_home = True
        usb._sync_status()

    asic._feed_capture = _feed_inside_zone

    assert asic.recover_home() is True

    assert ("0x02", REVERSE_SEEK) not in log
    assert asic._park_ok


def test_motor_still_running_on_the_sensor_gets_a_software_stop(monkeypatch):
    monkeypatch.setattr(gl128_mod, "_HOME_SEEK_SELF_STOP_GRACE_S", 0.0)
    usb = MockScannerTransport(at_home=False, reverse_seek_self_stops=False)
    asic, log = _asic(MODEL_8200I_SE, usb)

    assert asic.recover_home() is True

    first_seek = log.index(("0x02", REVERSE_SEEK))
    after = [v for kind, v in log[first_seek + 1 :] if kind == "0x02"]
    assert after[0] == asic.registers.FASTFED
    assert usb.at_home
    assert not usb.motor_enabled


def test_a_seek_that_never_reaches_home_times_out_and_blocks_scans():
    usb = MockScannerTransport(at_home=False, reverse_seek_reaches_home=False)
    asic, log = _asic(MODEL_8200I_SE, usb)

    with pytest.raises(MotorTimeoutError, match="did not reach the home sensor"):
        asic.recover_home(timeout_s=0.05)

    assert not asic._park_ok
    assert log[-1] == ("feedl", 1)
    assert not usb.motor_enabled
    assert log.count(("0x02", REVERSE_SEEK)) == 1


def test_disabled_model_refuses_recovery_and_positioning():
    model = dataclasses.replace(MODEL_8200I_SE, home_recovery_enabled=False)
    usb = MockScannerTransport(at_home=False)
    asic, log = _asic(model, usb)

    with pytest.raises(AsicError, match="recovery is disabled"):
        asic.recover_home()
    with pytest.raises(AsicError, match="recovery is disabled"):
        asic.position_for_full_frame_scan(scan_steps=13128)
    assert not any(kind == "start" for kind, _ in log)


@pytest.mark.parametrize("model", GL128_MODELS, ids=lambda m: m.model)
def test_positioning_away_from_home_recovers_first(model):
    usb = MockScannerTransport(at_home=False)
    asic, log = _asic(model, usb)

    asic.position_for_full_frame_scan(scan_steps=13128)

    modes = [v for kind, v in log if kind == "0x02" and v in (REVERSE_SEEK, FORWARD_FEED)]
    assert modes == [REVERSE_SEEK, FORWARD_FEED, REVERSE_SEEK, FORWARD_FEED, FORWARD_FEED]
    assert ("feedl", model.feed_to_reference_steps) in log
    assert not usb.at_home


def test_positioning_after_a_failed_park_recovers_first():
    usb = MockScannerTransport()
    asic, log = _asic(MODEL_8200I_SE, usb)
    asic._park_ok = False

    asic.position_for_full_frame_scan(scan_steps=13128)

    assert ("0x02", REVERSE_SEEK) in log
    assert asic._park_ok


def test_scanner_recover_carriage(tmp_path):
    usb = MockScannerTransport(at_home=False)
    with Scanner.open_fake(MODEL_8200I_SE, usb, calib_cache=tmp_path / "c.json") as scanner:
        scanner.asic._initialized = True
        assert scanner.recover_carriage() is True
        assert usb.at_home
        assert scanner.recover_carriage() is False


def test_calibrator_rearms_the_motor_only_for_the_recovery():
    calls: list[bool] = []
    asic = SimpleNamespace(_motor_moves_enabled=False, is_at_home=lambda: False)
    asic.home = lambda: calls.append(asic._motor_moves_enabled)

    Calibrator._ensure_at_home(SimpleNamespace(asic=asic))  # type: ignore[arg-type]

    assert calls == [True]
    assert asic._motor_moves_enabled is False
