# SPDX-License-Identifier: GPL-3.0-or-later
"""OpticFilm 8200i (GL845) values pinned against the SilverFast capture reference."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from scanners.setup_8200i import run_8200i_setup
from scanners.trace_compare import extract_register_writes

from pyopticfilm.asic.motor import create_fast_slope_table
from pyopticfilm.device.model_8200i import MODEL_8200I, Model8200i
from pyopticfilm.device.protocol import DEFAULT_GL845_MOTOR
from pyopticfilm.device.select import MODEL_7600I_V2, create_asic
from pyopticfilm.exceptions import MotorTimeoutError
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.scan.session import create_session
from pyopticfilm.usb.fake import MockScannerTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol

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


def _register_writes(usb) -> list[tuple[int, int]]:
    """Ordered ``(address, value)`` register writes."""
    out = []
    for txn in usb.transactions:
        if txn.operation == "control_write" and txn.request == 0x04 and txn.value == 0x83:
            data = txn.data or b""
            out.extend((data[i], data[i + 1]) for i in range(0, len(data) - 1, 2))
    return out


def _scan(model, resolution: int = 900, **kwargs):
    usb = MockScannerTransport()
    asic = create_asic(GenesysUsbProtocol(usb), model)
    image = create_session(asic, model).run(resolution=resolution, apply_calib=False, **kwargs)
    return usb, asic, image


def _motor_starts(writes) -> list[dict[int, int]]:
    """Register state at each ``0x0F = 1``."""
    regs: dict[int, int] = {}
    starts = []
    for addr, value in writes:
        regs[addr] = value
        if addr == 0x0F and value == 0x01:
            starts.append(dict(regs))
    return starts


def test_fast_feed_then_auto_home_image_pass():
    """One FASTFED move with SCAN off, then the image pass with AGOHOME, as SilverFast."""
    usb, asic, image = _scan(MODEL_8200I)
    geo = compute_geometry(900, model=MODEL_8200I)
    assert image.rgb.shape[:2] == (geo.lines, geo.pixels)

    feed, scan = _motor_starts(_register_writes(usb))
    vendor = _image_pass("06_900ppi_color.pcapng")["regs"]

    assert feed[0x02] == 0x18  # MTRPWR | FASTFED
    assert not feed[0x01] & 0x01  # SCAN off
    feedl = (feed[0x3D] << 16) | (feed[0x3E] << 8) | feed[0x3F]
    assert feedl == (geo.starty - MODEL_8200I.prefeed_margin_steps) << 2
    assert feed[0x6A] == 0xFF

    assert scan[0x02] == vendor["0x02"] == 0x30  # MTRPWR | AGOHOME
    assert scan[0x01] & 0x01
    # The image pass feeds only the margin (less its own slope).
    image_feedl = (scan[0x3D] << 16) | (scan[0x3E] << 8) | scan[0x3F]
    assert 0 < image_feedl <= MODEL_8200I.prefeed_margin_steps << 2

    assert asic.read_status().is_at_home
    assert asic.is_motor_stopped()
    assert not usb.registers[0x01] & 0x01


def test_fast_feed_is_skipped_for_short_moves():
    usb = MockScannerTransport()
    asic = create_asic(GenesysUsbProtocol(usb), MODEL_8200I)
    asic.init()
    session = create_session(asic, MODEL_8200I)
    geo = replace(compute_geometry(900, model=MODEL_8200I), starty=450)
    assert session._fast_feed(geo) is geo
    assert all(regs[0x02] != 0x18 for regs in _motor_starts(_register_writes(usb)))


def test_fast_feed_stops_the_motor_when_the_feed_stalls(monkeypatch):
    from pyopticfilm.scan import session as session_mod

    usb = MockScannerTransport()
    asic = create_asic(GenesysUsbProtocol(usb), MODEL_8200I)
    asic.init()
    session = create_session(asic, MODEL_8200I)
    monkeypatch.setattr(session_mod, "FEED_STALL_S", 0.0)
    monkeypatch.setattr(usb, "_tick_motion", lambda: None)  # the feed never finishes
    try:
        session._fast_feed(compute_geometry(900, model=MODEL_8200I))
    except MotorTimeoutError:
        pass
    else:
        raise AssertionError("expected MotorTimeoutError")
    assert not usb.registers[0x02] & 0x10  # MTRPWR dropped


def test_flags_off_keeps_the_prefeed_and_home_seek_path():
    plain = replace(MODEL_8200I, fast_feed=False, scan_auto_home=False)
    usb, _asic, _image = _scan(plain)
    starts = _motor_starts(_register_writes(usb))
    assert all(regs[0x02] not in (0x18, 0x30) for regs in starts)


def test_7600i_v2_does_not_fast_feed_or_auto_home():
    assert not MODEL_7600I_V2.fast_feed and not MODEL_7600I_V2.scan_auto_home
    usb, _asic, _image = _scan(MODEL_7600I_V2)
    starts = _motor_starts(_register_writes(usb))
    assert all(regs[0x02] not in (0x18, 0x30) for regs in starts)


def test_fast_feed_writes_the_captured_motor_setup_from_a_fresh_init():
    """With a cached calibration nothing is configured before the feed."""
    usb = MockScannerTransport()
    asic = create_asic(GenesysUsbProtocol(usb), MODEL_8200I)
    asic.init()
    create_session(asic, MODEL_8200I)._fast_feed(compute_geometry(1800, model=MODEL_8200I))

    (feed,) = _motor_starts(_register_writes(usb))
    vendor = REFERENCE["fast_feed"]
    for name, value in vendor["regs"].items():
        addr = int(name, 16)
        if addr == 0x01:
            assert not feed[addr] & 0x01  # SCAN off; DVDSET differs by design
            continue
        assert feed[addr] == value, name
    assert feed[0x61] == feed[0x62] == feed[0x64] == feed[0x65] == 0

    uploads = {}
    preamble = None
    for txn in usb.transactions:
        if txn.operation == "control_write" and txn.value == 0x82:
            preamble = int.from_bytes(txn.data[:4], "little")
        elif txn.operation == "bulk_write" and preamble is not None:
            uploads[preamble] = txn.data
            preamble = None
    start = b"".join(v.to_bytes(2, "little") for v in vendor["scan_slot_table"])
    fast = b"".join(v.to_bytes(2, "little") for v in REFERENCE["fast_table"])
    assert uploads[0x10000000] == uploads[0x10004000] == uploads[0x10008000] == start
    assert uploads[0x1000C000] == uploads[0x10010000] == fast


def test_sensor_sampling_point_matches_capture():
    usb, _geo = run_8200i_setup(dpi=1800)
    regs = extract_register_writes(usb.transactions)
    assert (regs[0x58], regs[0x59]) == (0x2A, 0x72)
    # The SANE tables (and the 7600i v2 built from them) keep their values.
    assert (MODEL_7600I_V2.sensor_custom_regs[0x58], MODEL_7600I_V2.sensor_custom_regs[0x59]) == (0x31, 0x79)


def test_lamp_watchdog_is_off_for_the_image_pass_only():
    plain = replace(MODEL_8200I, fast_feed=False)  # 1800 dpi then runs a pre-feed capture
    usb, _asic, _image = _scan(plain, resolution=1800)
    starts = _motor_starts(_register_writes(usb))
    image = [regs for regs in starts if regs[0x02] == 0x30]
    others = [regs for regs in starts if regs[0x02] != 0x30]
    vendor = _image_pass("06_900ppi_color.pcapng")["regs"]
    assert image and others
    assert all(not regs[0x03] & 0x80 for regs in image)
    assert image[-1][0x03] & 0xF0 == vendor["0x03"] & 0xF0 == 0x30
    assert all(regs[0x03] & 0x80 for regs in others)


def test_home_sensor_gpio_stays_at_the_boot_value():
    usb, _asic, _image = _scan(MODEL_8200I)
    values = {value for addr, value in _register_writes(usb) if addr == 0x6C}
    assert values == {0x4C}


def test_7600i_v2_keeps_the_watchdog_and_gpio_behaviour():
    usb, _asic, _image = _scan(MODEL_7600I_V2)
    writes = _register_writes(usb)
    assert 0x4D in {value for addr, value in writes if addr == 0x6C}
    assert all(regs[0x03] & 0x80 for regs in _motor_starts(writes))


def _calibrated_scan(model, tmp_path):
    from pyopticfilm.scanner import Scanner

    usb = MockScannerTransport()
    scanner = Scanner.open_fake(model, usb, calib_cache=tmp_path / "calib.json")
    scanner.scan(resolution=900, mode="color")
    return usb, scanner


def test_calibrated_scan_never_drives_in_reverse(tmp_path):
    """Every return home is an AGOHOME capture; SilverFast never sets MTRREV."""
    usb, scanner = _calibrated_scan(MODEL_8200I, tmp_path)
    starts = _motor_starts(_register_writes(usb))
    homes = [regs for regs in starts if regs[0x02] == 0x30 and regs[0x01] & 0x01 and regs[0x27] == 3]
    assert homes, "expected forward home captures after the calibration strips"
    assert all(not regs[0x02] & 0x04 for regs in starts)  # MTRREV
    for regs in homes:
        assert (regs[0x3D], regs[0x3E], regs[0x3F]) == (0, 0, 1)
    assert scanner.asic.read_status().is_at_home


def test_7600i_v2_still_homes_in_reverse(tmp_path):
    usb, _scanner = _calibrated_scan(MODEL_7600I_V2, tmp_path)
    assert any(regs[0x02] == 0xB4 for regs in _motor_starts(_register_writes(usb)))


def test_fast_feed_homes_first_when_not_at_home():
    usb = MockScannerTransport()
    asic = create_asic(GenesysUsbProtocol(usb), MODEL_8200I)
    asic.init()
    usb.at_home = False
    create_session(asic, MODEL_8200I)._fast_feed(compute_geometry(900, model=MODEL_8200I))
    home, feed = _motor_starts(_register_writes(usb))
    assert home[0x02] == 0x30 and feed[0x02] == 0x18


def test_fast_feed_does_not_start_when_homing_fails(monkeypatch):
    usb = MockScannerTransport()
    asic = create_asic(GenesysUsbProtocol(usb), MODEL_8200I)
    asic.init()
    usb.at_home = False

    def no_home(**_kwargs):
        raise MotorTimeoutError("Head did not reach home")

    monkeypatch.setattr(asic, "home", no_home)
    try:
        create_session(asic, MODEL_8200I)._fast_feed(compute_geometry(900, model=MODEL_8200I))
    except MotorTimeoutError:
        pass
    else:
        raise AssertionError("expected MotorTimeoutError")
    assert all(regs[0x02] != 0x18 for regs in _motor_starts(_register_writes(usb)))


def test_fast_feed_waits_for_the_motor_to_go_idle(monkeypatch):
    usb = MockScannerTransport()
    asic = create_asic(GenesysUsbProtocol(usb), MODEL_8200I)
    asic.init()
    waited = []
    monkeypatch.setattr(asic, "wait_motor_idle", lambda *a, **k: waited.append(usb.registers[0x02]))
    create_session(asic, MODEL_8200I)._fast_feed(compute_geometry(900, model=MODEL_8200I))
    assert waited == [0x08]  # after MTRPWR is dropped


def test_wait_motor_idle_times_out():
    usb = MockScannerTransport()
    asic = create_asic(GenesysUsbProtocol(usb), MODEL_8200I)
    usb.motor_enabled = True
    usb._tick_motion = lambda: None
    try:
        asic.wait_motor_idle(timeout_s=0.05)
    except MotorTimeoutError:
        pass
    else:
        raise AssertionError("expected MotorTimeoutError")
