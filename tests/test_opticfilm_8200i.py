# SPDX-License-Identifier: GPL-3.0-or-later
"""OpticFilm 8200i (GL845) values pinned against the SilverFast capture reference."""

from __future__ import annotations

import json
from pathlib import Path

from scanners.setup_8200i import run_8200i_setup
from scanners.trace_compare import extract_register_writes

from pyopticfilm.asic.motor import create_fast_slope_table
from pyopticfilm.device.model_8200i import MODEL_8200I, Model8200i
from pyopticfilm.device.protocol import DEFAULT_GL845_MOTOR
from pyopticfilm.device.select import MODEL_7600I_V2

REFERENCE = json.loads(
    (Path(__file__).parent / "data" / "8200i" / "vendor_scan_reference.json").read_text()
)


def _image_pass(capture: str) -> dict:
    return next(p for p in REFERENCE["passes"] if p["capture"] == capture)


def test_fast_table_is_the_captured_one():
    table = create_fast_slope_table(profile=MODEL_8200I).table
    assert table == REFERENCE["fast_table"]
    assert len(table) == 256 and table[0] == 64102 and table[-1] == 625


def test_motor_registers_match_capture():
    usb, _geo = run_8200i_setup(dpi=1800)
    regs = extract_register_writes(usb.transactions)
    vendor = _image_pass("06_1800ppi_color.pcapng")["regs"]
    for addr in (0x5F, 0x67, 0x68, 0x6A, 0x80):
        assert regs[addr] == vendor[f"0x{addr:02x}"], hex(addr)


def test_optical_setup_matches_capture():
    """Exposure and dpiset agree with SilverFast at every shared resolution."""
    for dpi, capture in (
        (900, "06_900ppi_color.pcapng"),
        (1800, "06_1800ppi_color.pcapng"),
        (3600, "06_3600ppi_color.pcapng"),
        (7200, "7200dpi_color.pcapng"),
    ):
        usb, geo = run_8200i_setup(dpi=dpi)
        regs = extract_register_writes(usb.transactions)
        vendor = _image_pass(capture)
        assert geo.register_dpiset == vendor["dpiset"], dpi
        assert (regs[0x38] << 8) | regs[0x39] == vendor["lperiod"], dpi
        assert regs[0x1C] == vendor["regs"]["0x1c"], dpi


def test_multi_exposure_long_pass_matches_capture():
    long_pass = [p for p in REFERENCE["passes"] if p["capture"] == "07_1800ppi_color_me.pcapng"][-1]
    assert long_pass["lperiod"] == MODEL_8200I.exposure_long == 42000


def test_infrared_pass_uses_the_ir_led_gpio():
    ir = next(p for p in REFERENCE["passes"] if p["infrared"])
    assert ir["regs"]["0xa8"] & 0x04  # IR LED on
    assert not ir["regs"]["0x03"] & 0x10  # LAMPPWR off


def test_calibration_follows_the_capture():
    assert MODEL_8200I.calib_white_home_y_mm == 1.5
    assert MODEL_8200I.afe_white_target == 60000
    assert MODEL_8200I.scan_ready is False


def test_7600i_v2_and_sane_defaults_keep_the_sane_motor_profile():
    assert MODEL_7600I_V2.motor_profile is DEFAULT_GL845_MOTOR
    assert Model8200i().motor_profile is DEFAULT_GL845_MOTOR
    assert DEFAULT_GL845_MOTOR.fast_table is None
    assert DEFAULT_GL845_MOTOR.reg_overrides == ()
