# SPDX-License-Identifier: GPL-3.0-or-later
"""OpticFilm 135i loader and RGB+IR session against the VueScan captures (no hardware).

Register values below are the state at each START in the 135i captures
(``insert 35mm carrier``, ``holder eject``, ``64bit rgbi 35mm film preview``,
``64bit rgbi 35mm frame 7200dpi scan``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pytest

from pyopticfilm.asic.gl128_loader import Gl128Loader
from pyopticfilm.asic.registers import Gl128Registers
from pyopticfilm.device.model_135i import MODEL_135I
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.select import create_asic
from pyopticfilm.device.tables_135i import SLOPE_TABLE_FEED, SLOPE_TABLE_LOADER, scan_motor_table
from pyopticfilm.exceptions import AsicError, MotorTimeoutError, ScanError
from pyopticfilm.scan.calib_gl128 import coarse_gains_from_wide_means, unpack_shading_table
from pyopticfilm.scan.calib_gl128_loader import (
    AFE_LINE_GAIN,
    AHB_SHADING_IR,
    AHB_SHADING_VISIBLE,
    SHADING_GAIN_UNITY,
    afe_strip_clocks,
    lit_channel_peaks,
    split_toggle_lines,
)
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.scan.session import create_session
from pyopticfilm.scan.session_gl128_loader import Gl128LoaderScanSession, loader_feed_steps
from pyopticfilm.scanner import Scanner
from pyopticfilm.usb.fake import MockFilmLoaderTransport
from pyopticfilm.usb.protocol import REQUEST_BUFFER, VALUE_BUFFER, GenesysUsbProtocol

R = Gl128Registers()


@dataclass
class _Recorder(MockFilmLoaderTransport):
    """Mock 135i that keeps the register file at every START, AHB uploads and announces."""

    starts: list[dict[int, int]] = field(default_factory=list)
    writes: list[tuple[int, int]] = field(default_factory=list)
    ahb: list[tuple[int, bytes]] = field(default_factory=list)
    announces: list[tuple[int, int]] = field(default_factory=list)
    _pending: list | None = None

    def _after_register_write(self, addr: int, value: int) -> None:
        self.writes.append((addr, value & 0xFF))
        if addr == 0x0F and value & 0xFF == 0x01:
            self.starts.append(dict(self.registers))
        super()._after_register_write(addr, value)

    def _handle_control(self, txn):  # type: ignore[no-untyped-def]
        if txn.operation == "control_write" and txn.request == REQUEST_BUFFER and txn.value == VALUE_BUFFER:
            addr = int.from_bytes(txn.data[:4], "little")
            size = int.from_bytes(txn.data[4:8], "little")
            if txn.index == 0x01:
                self._pending = [addr, size, bytearray()]
            else:
                self.announces.append((txn.index, size))
        return super()._handle_control(txn)

    def bulk_write(self, data, *, timeout_ms=None):  # type: ignore[no-untyped-def]
        n = super().bulk_write(data, timeout_ms=timeout_ms)
        if self._pending is not None:
            self._pending[2] += bytes(data)
            if len(self._pending[2]) >= self._pending[1]:
                self.ahb.append((self._pending[0], bytes(self._pending[2])))
                self._pending = None
        return n


def _u(regs: dict[int, int], addr: int, width: int) -> int:
    out = 0
    for i in range(width):
        out = (out << 8) | regs.get(addr + i, 0)
    return out


def _words(blob: bytes) -> tuple[int, ...]:
    return tuple(int(v) for v in np.frombuffer(blob, dtype="<u2"))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)


@pytest.fixture
def rec() -> _Recorder:
    return _Recorder()


@pytest.fixture
def scanner(rec, tmp_path):
    sc = Scanner.open_fake(MODEL_135I, rec, calib_cache=tmp_path / "calib.json")
    yield sc
    sc.close()


# --- selection ----------------------------------------------------------------


def test_135i_routes_to_loader_driver_and_session():
    asic = create_asic(GenesysUsbProtocol(MockFilmLoaderTransport()), MODEL_135I)
    assert isinstance(asic, Gl128Loader)
    assert isinstance(create_session(asic, MODEL_135I), Gl128LoaderScanSession)
    assert MODEL_135I.film_loader is True
    assert MODEL_8200I_SE.film_loader is False
    assert not isinstance(create_asic(GenesysUsbProtocol(MockFilmLoaderTransport()), MODEL_8200I_SE), Gl128Loader)


# --- loader moves ---------------------------------------------------------------


def test_load_holder_matches_insert_capture(scanner, rec):
    scanner.asic.init()
    rec.starts.clear()
    rec.ahb.clear()
    scanner.load_holder()
    forward, reverse = rec.starts
    # Step 1: 0x32 bit 1 cleared, 02=18, ramp 255, FEEDL 6690.
    assert forward[0x32] & 0x02 == 0
    assert (forward[0x02], forward[0xAE], forward[0xAF], _u(forward, 0x3D, 3)) == (0x18, 0x00, 0xFF, 6690)
    # Step 2: MTRREV seek, FEEDL 71490, 0x1C=0, exposure 30000, 0x8A-0x92 cleared.
    assert (reverse[0x02], _u(reverse, 0x3D, 3), reverse[0x1C], _u(reverse, 0x7D, 3)) == (0x1C, 71490, 0x00, 30000)
    assert all(reverse[a] == 0 for a in range(0x8A, 0x93))
    # Both moves upload the load ramp to both slope windows.
    assert [a for a, _ in rec.ahb] == [R.AHB_SLOPE_SCAN, R.AHB_SLOPE_FAST] * 2
    assert all(_words(blob) == SLOPE_TABLE_LOADER for _, blob in rec.ahb)
    assert scanner.status().is_at_home


def test_load_without_holder_is_refused(scanner, rec):
    rec.holder_present = False
    assert scanner.holder_present() is False
    with pytest.raises(AsicError, match="No film holder"):
        scanner.load_holder()


def test_eject_matches_holder_eject_capture(scanner, rec):
    scanner.asic.init()
    rec.starts.clear()
    rec.writes.clear()
    rec.ahb.clear()
    scanner.eject_holder()
    (move,) = rec.starts
    assert (move[0x09], move[0x02], _u(move, 0x3D, 3), move[0xAF]) == (0x08, 0x18, 3090, 0xFF)
    # Vendor order: holder LED, then the stop disarmed (0x32 bit 1 set), then 09 |= 08.
    assert move[0x32] & 0x02 == 0x02
    led = rec.writes.index((0x36, 0xFC))
    assert rec.writes[led + 1] == (0x33, 0x8E)
    assert led < rec.writes.index((0x09, 0x08))
    assert rec.writes[-1] == (0x09, 0x00)
    assert all(_words(blob) == SLOPE_TABLE_FEED for _, blob in rec.ahb)


def test_eject_off_home_homes_first(scanner, rec):
    scanner.asic.init()
    rec.at_home = False
    rec.starts.clear()
    scanner.eject_holder()
    # No inward inch, and FEEDL covers the whole travel from the end of a strip.
    assert [(_u(s, 0x3D, 3), s[0x02]) for s in rec.starts] == [(MODEL_135I.loader_travel_steps(), 0x1C), (3090, 0x18)]
    reverse, eject = rec.starts
    assert reverse[0x32] & 0x02 == 0
    assert eject[0x32] & 0x02 == 0x02


def test_eject_with_empty_slot_does_not_move(scanner, rec):
    scanner.asic.init()
    rec.holder_present = False
    rec.at_home = False
    rec.starts.clear()
    scanner.eject_holder()
    assert rec.starts == []


def test_eject_button_reads_interrupt_byte(scanner, rec):
    rec.interrupt_queue.append(b"\x48")
    assert scanner.asic.eject_requested() is True
    assert scanner.asic.eject_requested() is False


# --- loader sensors ---------------------------------------------------------------


def test_holder_sensor_reads_disarmed(scanner, rec):
    scanner.asic.init()
    rec.holder_present = False
    # Measured on a 135i after the init table: 0x32 = 0x1b with the slot empty (bit 6 clear).
    assert scanner.protocol.read_register(0x32) == 0x1B
    assert scanner.holder_present() is False
    rec.holder_present = True
    assert scanner.holder_present() is True


def test_holder_seen_off_home_with_the_stop_armed(scanner, rec):
    asic = scanner.asic
    asic.init()
    asic._set_home_stop(armed=True)
    rec.at_home = False
    # Armed at a frame position, 0x32 bit 2 reads 0 as HOMESNR does.
    assert not scanner.protocol.read_register(0x32) & 0x04
    assert scanner.holder_present() is True
    assert not scanner.protocol.read_register(0x32) & 0x02  # still armed


def test_at_home_reads_homesnr_with_the_stop_armed(scanner, rec):
    asic = scanner.asic
    asic.init()
    assert scanner.protocol.read_register(0x32) & 0x02  # init leaves the stop disarmed
    rec.at_home = False
    # Disarmed, HOMESNR reads "holder in the slot"; the loaded position needs the stop armed.
    assert asic.read_status_reliable().is_at_home is True
    assert asic.is_at_home() is False
    assert scanner.protocol.read_register(0x32) & 0x02  # restored
    with pytest.raises(ScanError, match="not at the loader home"):
        asic.require_holder_loaded("scan")


# --- home -------------------------------------------------------------------------


def test_home_refuses_from_an_unknown_position(scanner, rec):
    # After init the driver does not know where an off-home holder is: deep in
    # a strip, an inward inch would pass the loader travel.
    scanner.asic.init()
    rec.at_home = False
    rec.starts.clear()
    with pytest.raises(AsicError, match="position is unknown"):
        scanner.home()
    assert rec.starts == []


def test_home_after_a_frame_feed_seeks_straight_back(scanner, rec):
    asic = scanner.asic
    scanner.load_holder()
    asic.position_for_frame(feed_steps=60198, scan_regs={})
    rec.at_home = False
    rec.starts.clear()
    scanner.home()
    # No inward inch: one reverse seek over the whole travel, stopped by the sensor.
    assert [(_u(s, 0x3D, 3), s[0x02], s[0x32] & 0x02) for s in rec.starts] == [(MODEL_135I.loader_travel_steps(), 0x1C, 0)]
    assert asic.is_at_home() and asic.holder_loaded()


def test_load_refuses_a_holder_inside_the_loader(scanner, rec):
    # Armed HOMESNR = 0 with a holder in the slot: inward of home. A load's
    # inch from there could pass the loader travel (e.g. 73722 + 7200).
    scanner.asic.init()
    rec.at_home = False
    rec.starts.clear()
    with pytest.raises(AsicError, match="inside the loader"):
        scanner.load_holder()
    assert rec.starts == []
    scanner.asic._park_ok = False  # also after a lost park
    with pytest.raises(AsicError, match="inside the loader"):
        scanner.load_holder()
    assert rec.starts == []


def test_home_after_a_lost_park_seeks_straight_back(scanner, rec):
    asic = scanner.asic
    asic.init()
    rec.at_home = False
    asic._park_ok = False
    rec.starts.clear()
    scanner.home()
    assert [(_u(s, 0x3D, 3), s[0x02], s[0x32] & 0x02) for s in rec.starts] == [(MODEL_135I.loader_travel_steps(), 0x1C, 0)]
    assert asic._park_ok is True


def test_load_does_not_seek_while_the_sensor_stays_set(scanner, rec, monkeypatch):
    asic = scanner.asic
    asic.init()
    rec.starts.clear()
    ejected = []

    def feed_onto_sensor(*_a, **_k):
        rec.at_home = True

    monkeypatch.setattr(asic, "_loader_move", feed_onto_sensor)
    monkeypatch.setattr(asic, "eject_holder", lambda **_k: ejected.append(True))
    with pytest.raises(AsicError, match="did not load"):
        asic.load_holder()
    assert rec.starts == [] and ejected == [True]
    assert asic._holder_inward is False


def test_scan_does_not_home_after_init(scanner, rec):
    rec.at_home = False
    rec.starts.clear()
    with pytest.raises(ScanError, match="eject it"):
        scanner.scan(resolution=600)
    assert rec.starts == []


def test_calibrate_refuses_off_home_before_any_strip(scanner, rec):
    rec.at_home = False
    rec.starts.clear()
    with pytest.raises(ScanError, match="eject it"):
        scanner.calibrate(resolution=600)
    assert rec.starts == []


def test_warmup_does_not_home(scanner, rec):
    rec.at_home = False
    rec.starts.clear()
    scanner.warmup(lamp=False)
    assert rec.starts == []


def test_scan_needs_a_load_even_when_the_sensor_reads_home(scanner, rec):
    # Armed, HOMESNR reads 1 for a holder at the entry or ejected (135i run).
    rec.starts.clear()
    assert scanner.holder_present() and scanner.asic.is_at_home() and not scanner.holder_loaded()
    with pytest.raises(ScanError, match="not loaded"):
        scanner.scan(resolution=600)
    with pytest.raises(ScanError, match="not loaded"):
        scanner.calibrate(resolution=600)
    assert rec.starts == []


def test_holder_loaded_lasts_from_the_load_to_the_eject(scanner, rec):
    scanner.load_holder()
    assert scanner.holder_loaded()
    scanner.eject_holder()
    assert not scanner.holder_loaded()
    rec.at_home = True  # armed, an ejected holder reads 1 (135i run)
    with pytest.raises(ScanError, match="not loaded"):
        scanner.scan(resolution=600)


def test_init_removal_and_a_lost_park_clear_holder_loaded(scanner, rec):
    asic = scanner.asic
    scanner.load_holder()
    asic.init(force=True)
    assert not scanner.holder_loaded()

    scanner.load_holder()
    asic._park_ok = False
    assert not scanner.holder_loaded()
    asic._park_ok = True

    assert scanner.holder_loaded()
    asic.poll_loader()
    rec.holder_present = False
    assert asic.poll_loader() == "removed"
    assert not scanner.holder_loaded()


def test_home_without_holder_does_not_move(scanner, rec):
    scanner.asic.init()
    rec.holder_present = False
    rec.at_home = False
    rec.starts.clear()
    scanner.home()
    assert rec.starts == []


def test_already_home_with_the_stop_armed(scanner, rec):
    scanner.asic.init()
    rec.starts.clear()
    scanner.home()
    assert rec.starts == []


# --- motor stop -----------------------------------------------------------------


def test_move_timeout_does_not_claim_the_motor_stopped(scanner, rec):
    asic = scanner.asic
    asic.init()
    rec.writes.clear()
    with pytest.raises(MotorTimeoutError, match="may still be running"):
        asic._wait_move(timeout_s=0, until_home=False, steps=3600)
    assert all(addr != 0x02 for addr, _ in rec.writes)
    assert asic._park_ok is False


def test_close_without_an_image_pass_does_not_park(scanner, rec):
    scanner.asic.init()
    rec.writes.clear()
    rec.starts.clear()
    scanner.close()
    assert all(addr != 0x02 for addr, _ in rec.writes)
    assert rec.starts == []


# --- geometry -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dpi", "pixels", "end", "lperiod", "dpiset", "margin"),
    [(600, 864, 5219, 11023, 200, 5), (1200, 1728, 5219, 11239, 400, 9), (7200, 10368, 10403, 15991, 1200, 53)],
)
def test_frame_geometry_follows_capture_rules(dpi, pixels, end, lperiod, dpiset, margin):
    g = compute_geometry(dpi, model=MODEL_135I)
    assert (g.pixels, g.pixel_startx, g.pixel_endx) == (pixels, 35, end)
    assert (g.exposure_lperiod, g.register_dpiset) == (lperiod, dpiset)
    # One frame slot (the 10752-step pitch) at the ASIC dpi, plus the line margin;
    # LINCNT counts a visible and an IR line per output line.
    assert g.lines == round(10752 * dpi / 7200)
    assert g.lincnt_register == 2 * (g.lines + margin)
    assert g.total_bytes == g.lincnt_register * pixels * 6


def test_crop_and_frame_feed():
    g = compute_geometry(1200, model=MODEL_135I, area=(0.25, 0.5, 0.75, 1.0))
    assert g.pixels == 864
    assert g.pixel_startx == 35 + 432 * 3
    assert g.starty == 5376
    assert loader_feed_steps(MODEL_135I, compute_geometry(1200, model=MODEL_135I), 1) == 17190
    assert loader_feed_steps(MODEL_135I, g, 2) == 27942 + 5376


def test_split_toggle_lines():
    # IR line first, visible second (VueScan's AFE probes).
    raw = bytes([1] * 6 + [2] * 6) * 3
    visible, ir = split_toggle_lines(raw, line_bytes=6, pairs=3)
    assert ir.tobytes() == bytes([1] * 18)
    assert visible.tobytes() == bytes([2] * 18)


# --- scans ----------------------------------------------------------------------


def _common_scan_bits(regs: dict[int, int]) -> None:
    assert (regs[0x04], regs[0x06], regs[0x0C], regs[0x15], regs[0x33], regs[0x3A]) == (0x42, 0xF8, 0x01, 0x90, 0x0E, 0x10)


def test_1200_preview_programs_match_capture(scanner, rec):
    scanner.load_holder()
    rec.starts.clear()
    rec.ahb.clear()
    rec.announces.clear()
    rec.writes.clear()
    img = scanner.scan(resolution=1200, infrared=True)
    probe1, probe2, wide, probe3, probe4, dark, white, feed, image = rec.starts

    for probe in (probe1, probe2, probe3, probe4):
        _common_scan_bits(probe)
        assert (_u(probe, 0x2C, 2), _u(probe, 0x82, 3), _u(probe, 0x85, 3), _u(probe, 0x25, 3)) == (1200, 35, 547, 2)
        assert (_u(probe, 0x28, 3), probe[0x2B], probe[0xA5], probe[0xAB]) == (10935, 0x01, 0x01, 0x30)
        assert [probe[a] for a in (0xA6, 0xA7, 0xA8, 0xA9)] == [0x00, 0x01, 0x00, 0x01]
        assert (probe[0x01], probe[0x02], probe[0x03], probe[0x19], probe[0xAF]) == (0x03, 0x00, 0x30, 0x08, 0x00)
        assert probe[0x32] & 0xA0 == 0xA0
    assert (_u(wide, 0x85, 3), _u(wide, 0x28, 3), wide[0x2B]) == (5219, 12103, 0x04)

    # Dark: lamp, toggle and IR LED off, 256 lines at the scan window.
    assert (_u(dark, 0x2C, 2), _u(dark, 0x85, 3), _u(dark, 0x25, 3), _u(dark, 0x28, 3)) == (400, 5219, 256, 11239)
    assert (dark[0x01], dark[0x03], dark[0x19], dark[0x32] & 0x20, dark[0xA3]) == (0x03, 0x20, 0x00, 0x00, 0x01)
    assert (dark[0x2B], dark[0xA5], dark[0xAB]) == (0x02, 0x01, 0x30)
    # White: DVDSET on the unity tables, lamp + IR LED + toggle on, motor off.
    assert (white[0x01], white[0x02], white[0x03], white[0x19], white[0x32] & 0x20, white[0xA3]) == (0x23, 0x00, 0x30, 0x08, 0x20, 0x00)
    assert (white[0x2B], white[0xA5], white[0xAB]) == (0x0D, 0x0B, 0x0B)

    # Frame 1 feed: FEEDL 6438 under the image registers.
    assert (_u(feed, 0x3D, 3), feed[0x01], feed[0x02], feed[0x05], feed[0xAE], feed[0xAF]) == (6438, 0x22, 0x18, 0x48, 0x00, 0xFF)
    assert [feed[a] for a in (0xA6, 0xA7, 0xA8, 0xA9)] == [0, 0, 0, 0]
    assert (_u(feed, 0x2C, 2), _u(feed, 0x7D, 3), feed[0xA5], feed[0xAB]) == (400, 14000, 0x0B, 0x0B)

    # Image pass: AGOHOME, DVDSET, depth pair untouched.
    _common_scan_bits(image)
    assert (image[0x01], image[0x02], image[0x05], _u(image, 0x3D, 3)) == (0x23, 0x30, 0x40, 1)
    assert (_u(image, 0x2C, 2), _u(image, 0x82, 3), _u(image, 0x85, 3), _u(image, 0x28, 3)) == (400, 35, 5219, 11239)
    assert (image[0x2B], image[0xA5], image[0xAB], image[0xAD], image[0xAF]) == (0x0D, 0x0B, 0x0B, 0x01, 0xFF)
    assert (_u(image, 0x7D, 3), _u(image, 0x90, 3), image[0x3B], image[0x3C]) == (14000, 11000, 0x00, 0x01)
    assert image[0x32] & 0xA0 == 0xA0
    assert _u(image, 0x25, 3) == compute_geometry(1200, model=MODEL_135I).lincnt_register

    # Shading: unity (0x4000) then measured, IR then visible bank, 21064 bytes each.
    shading = [(a, blob) for a, blob in rec.ahb if a in (AHB_SHADING_VISIBLE, AHB_SHADING_IR)]
    assert [a for a, _ in shading] == [0x10014000, 0x10034000] * 2
    assert (AHB_SHADING_IR, AHB_SHADING_VISIBLE) == (0x10014000, 0x10034000)
    assert all(len(blob) == 21064 for _, blob in shading)
    assert _words(shading[0][1])[1] == SHADING_GAIN_UNITY
    # Each bank has its own white target (mock IR white 30000, visible 40000-42000).
    ir_gain = unpack_shading_table(shading[2][1])[100]["gain"]
    vis_gain = unpack_shading_table(shading[3][1])[100]["gain"]
    assert ir_gain == [round(0xF000 * SHADING_GAIN_UNITY / rec.ir_level)] * 3
    assert vis_gain == [round(0x16000 * SHADING_GAIN_UNITY / v) for v in rec.visible_level]
    # Scan motor tables after the feed: the INI ramp clamped at 4666.
    motor = [blob for a, blob in rec.ahb if a in (R.AHB_SCAN_MOTOR_0, R.AHB_SCAN_MOTOR_1, R.AHB_SCAN_MOTOR_2)]
    assert len(motor) == 3 and all(_words(b) == scan_motor_table(4666) for b in motor)

    # Image reads: first announce on the image stream, the rest on RAM.
    image_reads = rec.announces[-(len(rec.announces) - 5 - 2):]
    assert image_reads[0][0] == R.BULK_INDEX_IMAGE
    assert {idx for idx, _ in image_reads[1:]} == {R.BULK_INDEX_RAM}

    # End of pass: lamp pair, clear SCAN, shutter off, AGOHOME, lamp off.
    i = rec.writes.index((0x01, 0x22), len(rec.writes) - 40)
    assert rec.writes[i - 2 : i + 5] == [(0x03, 0x30), (0x03, 0x20), (0x01, 0x22), (0x15, 0x80), (0x02, 0x30), (0x03, 0x10), (0x03, 0x00)]
    assert scanner.status().is_at_home

    assert img.rgb.shape == (1792, 1728, 3)
    assert img.ir is not None and img.ir.shape == (1792, 1728)


def test_second_frame_reuses_shading(scanner, rec):
    scanner.load_holder()
    scanner.scan(resolution=600)
    rec.starts.clear()
    rec.ahb.clear()
    img = scanner.scan(resolution=600, frame=1)
    feed, image = rec.starts
    assert _u(feed, 0x3D, 3) == 17190
    assert image[0x01] == 0x23
    assert not [a for a, _ in rec.ahb if a in (AHB_SHADING_VISIBLE, AHB_SHADING_IR)]
    assert img.ir is None


def test_7200_configure_matches_frame_scan(rec, tmp_path):
    sc = Scanner.open_fake(MODEL_135I, rec, calib_cache=tmp_path / "c.json")
    asic = sc.asic
    asic.init()
    asic._motor_moves_enabled = False
    session = create_session(asic, MODEL_135I)
    geometry = compute_geometry(7200, model=MODEL_135I)
    rec.ahb.clear()
    session._configure(geometry)
    regs = rec.registers
    assert (_u(regs, 0x2C, 2), _u(regs, 0x85, 3), _u(regs, 0x28, 3)) == (1200, 10403, 15991)
    assert (_u(regs, 0x7D, 3), _u(regs, 0x90, 3)) == (28000, 44000)
    assert (regs[0x18], regs[0x2B], regs[0xA5], regs[0xAB], regs[0x32] & 0x80) == (0x10, 0x17, 0x01, 0x01, 0x00)
    motor = [blob for a, blob in rec.ahb if a == R.AHB_SCAN_MOTOR_0]
    assert _words(motor[0]) == (56000,) * 256
    sc.close()


def test_scan_returns_ir_from_the_same_pass(scanner):
    scanner.load_holder()
    img = scanner.scan(resolution=600, infrared=True, apply_calib=False)
    assert img.rgb.shape == (896, 864, 3)
    assert img.ir.shape == (896, 864)
    plain = scanner.scan(resolution=600, apply_calib=False)
    assert plain.ir is None


def test_scan_refusals(scanner, rec):
    with pytest.raises(ScanError, match="no film holder"):
        rec.holder_present = False
        scanner.scan(resolution=600)
    rec.holder_present = True
    scanner.load_holder()
    with pytest.raises(ScanError, match="loader travel"):
        scanner.scan(resolution=7200, frame=6)
    with pytest.raises(ValueError, match="mode='color'"):
        scanner.scan(resolution=600, mode="infrared")


def test_flatbed_rejects_frame(tmp_path):
    with Scanner.open_fake(MODEL_8200I_SE, calib_cache=tmp_path / "c.json") as sc:
        with pytest.raises(ValueError, match="no film loader"):
            sc.scan(resolution=1200, frame=1)
        with pytest.raises(Exception, match="no film loader"):
            sc.load_holder()


# --- strip, travel and multi-exposure -----------------------------------------


def test_loader_travel_is_the_panorama_capture():
    # Panorama preview: feed 6414, LINCNT 11218 (5609 pairs x 12 steps at 600 dpi).
    assert MODEL_135I.loader_travel_steps() == 6414 + 5609 * 12 == 73722


def test_strip_geometry_matches_panorama_capture():
    from pyopticfilm.scan.geometry import compute_strip_geometry

    g = compute_strip_geometry(600, model=MODEL_135I)
    # VueScan read 5550 line pairs of 864 px (111 x 518400 bytes).
    assert (g.lines, g.pixels, g.pixel_startx, g.pixel_endx) == (5550, 864, 35, 5219)
    assert loader_feed_steps(MODEL_135I, g, None) == 6414
    for dpi in MODEL_135I.resolutions_dpi:
        geo = compute_strip_geometry(dpi, model=MODEL_135I)
        from pyopticfilm.scan.session_gl128_loader import loader_travel_end

        assert loader_travel_end(MODEL_135I, geo, None) <= MODEL_135I.loader_travel_steps()


def test_strip_scan_feeds_6414(scanner, rec):
    scanner.load_holder()
    rec.starts.clear()
    img = scanner.scan(resolution=600, frame=None, apply_calib=False)
    feed, image = rec.starts
    assert _u(feed, 0x3D, 3) == 6414
    assert _u(image, 0x25, 3) == 2 * (5550 + 5)
    assert img.rgb.shape == (5550, 864, 3)


def test_last_frame_fits_the_travel(scanner):
    scanner.load_holder()
    # Frame 5 at 7200 dpi: 60198 + 10805 = 71003 steps.
    from pyopticfilm.scan.session_gl128_loader import loader_travel_end

    assert loader_travel_end(MODEL_135I, compute_geometry(7200, model=MODEL_135I), 5) == 71003


def test_multi_exposure_runs_two_fed_passes(scanner, rec):
    scanner.load_holder()
    rec.starts.clear()
    img = scanner.scan(resolution=600, multi_exposure=True, me_long_mode="fixed", infrared=True)
    images = [s for s in rec.starts if s[0x02] == 0x30]
    feeds = [s for s in rec.starts if s[0x02] == 0x18]
    assert [_u(f, 0x3D, 3) for f in feeds] == [6438, 6438]
    # Short 14000 / 0x90 11000; the fixed long 42000 scales both three times.
    assert [(_u(s, 0x7D, 3), _u(s, 0x90, 3)) for s in images] == [(14000, 11000), (42000, 33000)]
    # STEPNO follows each pass's table: 0x23 at period 2333, 0x06 at 7000.
    assert [(s[0xA5], s[0xAB]) for s in images] == [(0x23, 0x23), (0x06, 0x06)]
    assert scanner.last_me_debug.exposure_long == 42000
    assert img.rgb.shape == (896, 864, 3) and img.ir.shape == (896, 864)


def test_afe_gains_come_from_the_visible_line_peaks():
    pixels = 512
    strip = np.zeros((2, pixels, 3), dtype="<u2")
    strip[0] = 60000  # IR line, ignored
    strip[1, :, :] = (20000, 25000, 22000)
    strip[1, 100:104, :] = (25700, 32300, 27240)  # four-pixel peak
    strip[1, 200, :] = 40000  # one hot pixel, averaged away
    strip[1, -20:, :] = 65535  # the wrapped end of a strip
    peaks = lit_channel_peaks(strip.tobytes(), pixels=pixels)
    assert peaks == (25700.0, 32300.0, 27240.0)
    # QuickScan's codes from peaks of this size.
    assert coarse_gains_from_wide_means(peaks, targets=(AFE_LINE_GAIN,) * 3) == (40, 31, 38)


def test_afe_clocks_follow_the_dpi():
    assert afe_strip_clocks(600, wide=False)[0] == 0x03 and afe_strip_clocks(600, wide=True)[0] == 0x1F
    assert afe_strip_clocks(1200, wide=False)[0] == 0x01 and afe_strip_clocks(1200, wide=True)[0] == 0x04


@dataclass
class _NoisyGpio(_Recorder):
    """0x32 reads back bits 3-5 that were not written, as a 135i does with the toggle on."""

    _reads: int = 0
    written_32: list[int] = field(default_factory=list)

    def _after_register_write(self, addr: int, value: int) -> None:
        if addr == 0x32:
            self.written_32.append(value & 0xFF)
        super()._after_register_write(addr, value)

    def _before_register_read(self, addr: int) -> None:
        super()._before_register_read(addr)
        if addr == 0x32:
            self._reads += 1
            self.registers[0x32] = (self.registers[0x32] & ~0x38 & 0xFF) | (self._reads * 8 & 0x38)


def test_0x32_never_takes_bits_from_the_readback(tmp_path):
    rec = _NoisyGpio()
    scanner = Scanner.open_fake(MODEL_135I, rec, calib_cache=tmp_path / "calib.json")
    try:
        scanner.load_holder()
        rec.starts.clear()
        rec.written_32.clear()
        scanner.calibrate(resolution=600)
        assert rec.written_32 and all(v & 0x18 == 0x18 for v in rec.written_32)
        dark, white = [s for s in rec.starts if _u(s, 0x25, 3) == 256]
        assert (rec.written_32.count(0xBD) > 0, rec.written_32.count(0x9D) > 0) == (True, True)
        assert (dark[0x03], white[0x03]) == (0x20, 0x30)
    finally:
        scanner.close()


def test_scan_clock_switch_pulses_the_lamp_and_waits(scanner, rec, monkeypatch):
    asic = scanner.asic
    asic.init()
    order: list = []
    monkeypatch.setattr(asic, "_write_clock", lambda value: order.append(("clock", value)))
    monkeypatch.setattr("time.sleep", lambda s: order.append(("sleep", s)))
    rec.writes.clear()
    asic.enter_scan_clock()
    assert order == [("clock", 0x0D), ("sleep", 0.075), ("clock", 0x0E)]
    assert [v for a, v in rec.writes if a == 0x03] == [0x20, 0x30, 0x20, 0x30]


def test_settles_after_moves_and_before_shading_uploads(scanner, rec, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    scanner.load_holder()
    assert sleeps.count(0.1) >= 2  # the inward inch and the seek
    sleeps.clear()
    scanner.asic.upload_shading_table(bytes(2 * 21064))
    assert sleeps == [0.1]


def test_strips_never_write_start_zero_and_clear_in_steps(scanner, rec):
    scanner.load_holder()
    rec.writes.clear()
    scanner.calibrate(resolution=600)
    assert (0x0F, 0x00) not in rec.writes
    first = rec.writes.index((0x0F, 0x01))
    steps = [v for a, v in rec.writes[:first] if a == 0x0D]
    assert steps == [0x00, 0x01, 0x05, 0x07]


def test_multi_pass_repeats_the_frame(scanner, rec):
    scanner.load_holder()
    rec.starts.clear()
    scanner.scan(resolution=600, n_passes=2, frame=1, apply_calib=False)
    feeds = [_u(s, 0x3D, 3) for s in rec.starts if s[0x02] == 0x18]
    assert feeds == [17190, 17190]


def test_me_long_cap_follows_the_135i_motor_word():
    from pyopticfilm.scan.session_gl128 import clamp_me_long

    # Motor word = exposure * Ydpi / 3600; with step-time shift 1 it may
    # reach 128000 (64000 in the table). 42000 at 7200 dpi is a word of 84000.
    assert clamp_me_long(MODEL_135I, 7200, 42000) == 42000
    assert clamp_me_long(MODEL_135I, 7200, 84000) == 64000
    assert MODEL_135I.channel_exposure_for(7200, exposure=64000) == 128000
    assert MODEL_135I.scan_motor_step_shift(128000) == 1
    assert clamp_me_long(MODEL_135I, 3600, 200000) == 85000
    assert clamp_me_long(MODEL_135I, 1200, 200000) == 85000
    assert clamp_me_long(MODEL_8200I_SE, 3600, 200000) == 85000
    assert clamp_me_long(MODEL_8200I_SE, 7200, 42000) == 42000


def test_7200_long_exposure_is_clamped_before_the_motor_table(rec, tmp_path):
    sc = Scanner.open_fake(MODEL_135I, rec, calib_cache=tmp_path / "c.json")
    asic = sc.asic
    asic.init()
    asic._motor_moves_enabled = False
    session = create_session(asic, MODEL_135I)
    session._pass_exposure = 84000
    rec.ahb.clear()
    session._configure(compute_geometry(7200, model=MODEL_135I))
    regs = rec.registers
    # Clamped to 64000: a word of 128000, halved by step-time shift 1.
    assert _u(regs, 0x7D, 3) == 64000
    assert regs[0x1C] & 0x07 == 1
    motor = [blob for a, blob in rec.ahb if a == R.AHB_SCAN_MOTOR_0]
    assert _words(motor[0]) == (64000,) * 256
    sc.close()


def test_7200_multi_exposure_short_at_timing_set_exposure(scanner, rec):
    """The 7200 short pass runs the single pass's 28000; the long uses the step shift."""
    scanner.load_holder()
    rec.starts.clear()
    scanner.scan(
        resolution=7200,
        multi_exposure=True,
        me_long_mode="fixed",
        area=(0.0, 0.0, 0.1, 0.1),
        apply_calib=False,
    )
    images = [s for s in rec.starts if s[0x02] == 0x30]
    feeds = [s for s in rec.starts if s[0x02] == 0x18]
    assert [(_u(s, 0x7D, 3), _u(s, 0x90, 3)) for s in images] == [(28000, 44000), (42000, 66000)]
    assert [s[0x1C] & 0x07 for s in images] == [0, 1]
    assert [s[0xA5] for s in images] == [0x01, 0x01]
    assert feeds and all(s[0x1C] & 0x07 == 0 for s in feeds)
    assert scanner.last_me_debug.exposure_short == 28000
    assert scanner.last_me_debug.exposure_long == 42000
    # The eject after a shifted pass runs without the shift.
    rec.starts.clear()
    scanner.eject_holder()
    assert rec.starts and all(s[0x1C] & 0x07 == 0 for s in rec.starts)


def test_7200_multi_pass_runs_at_timing_set_exposure(scanner, rec):
    scanner.load_holder()
    rec.starts.clear()
    scanner.scan(resolution=7200, n_passes=2, area=(0.0, 0.0, 0.1, 0.1), apply_calib=False)
    images = [s for s in rec.starts if s[0x02] == 0x30]
    assert [_u(s, 0x7D, 3) for s in images] == [28000, 28000]
    assert [s[0x1C] & 0x07 for s in images] == [0, 0]


def test_scan_motor_step_shift_limits():
    from pyopticfilm.device.tables_135i import scan_motor_step_shift

    assert [scan_motor_step_shift(p) for p in (2333, 4666, 56000, 65535)] == [0, 0, 0, 0]
    assert scan_motor_step_shift(65536) == 1
    assert scan_motor_step_shift(131071) == 1
    with pytest.raises(ValueError, match="step-time shift"):
        scan_motor_step_shift(131072)
    # Shift 0 tables are unchanged (every captured pass).
    assert scan_motor_table(4666) == tuple(max(v, 4666) for v in scan_motor_table(4666))
    assert scan_motor_table(84000) == (42000,) * 256


# --- loader watcher -----------------------------------------------------------


def test_poll_loader_reports_insert_removal_and_eject(scanner, rec):
    asic = scanner.asic
    asic.init()
    rec.holder_present = False
    assert asic.poll_loader() is None  # first sample only records the sensor
    rec.holder_present = True
    assert asic.poll_loader() == "inserted"
    assert asic.poll_loader() is None
    rec.interrupt_queue.append(bytes([0x48]))
    assert asic.poll_loader() == "eject"
    rec.holder_present = False
    assert asic.poll_loader() == "removed"


def test_watch_loader_loads_on_insert_and_ejects_on_button(scanner, rec):
    import threading

    rec.holder_present = False
    stop = threading.Event()
    events: list[str] = []

    def on_event(event: str) -> None:
        events.append(event)
        if event == "loaded":
            rec.interrupt_queue.append(bytes([0x48]))
        if event == "ejected":
            stop.set()

    worker = threading.Thread(
        target=scanner.watch_loader, kwargs={"stop": stop, "on_event": on_event, "interval_s": 0.001}
    )
    worker.start()
    # Insert only after the watcher has sampled the empty loader.
    seen = threading.Event()
    for _ in range(100000):
        if scanner.asic._holder_seen is False:
            seen.set()
            break
        threading.Event().wait(0.001)
    assert seen.is_set()
    rec.holder_present = True
    worker.join(timeout=20)
    stop.set()
    assert not worker.is_alive()
    assert events == ["inserted", "loaded", "eject", "ejected"]
    # The load ran its two moves (6690 forward, 71490 reverse), then the eject (3090).
    feeds = [_u(s, 0x3D, 3) for s in rec.starts if s[0x02] in (0x18, 0x1C)]
    assert feeds == [6690, 71490, 3090]


def test_3600_configure_matches_the_quickscan_image_pass(rec, tmp_path):
    """QuickScan 3600 dpi IR image START: DPISET 1200, a5/ab 03, 2b 1b, 1d 00."""
    sc = Scanner.open_fake(MODEL_135I, rec, calib_cache=tmp_path / "c.json")
    asic = sc.asic
    asic.init()
    asic._motor_moves_enabled = False
    session = create_session(asic, MODEL_135I)
    rec.ahb.clear()
    session._configure(compute_geometry(3600, model=MODEL_135I))
    regs = rec.registers
    assert (_u(regs, 0x2C, 2), _u(regs, 0x7D, 3), regs[0xA5], regs[0xAB], regs[0x2B]) == (1200, 14000, 0x03, 0x03, 0x1B)
    assert regs[0x1D] == 0x00 and regs[0x1C] & 0x07 == 0
    motor = [blob for a, blob in rec.ahb if a == R.AHB_SCAN_MOTOR_0]
    assert _words(motor[0])[:3] == (54484, 16345, 14000)
    sc.close()


def test_3600_calibration_strips_match_quickscan(scanner, rec):
    """QuickScan 3600 dpi IR: AFE probe/wide LPERIOD 11063 / 13399, dark and white 0x1D = 1."""
    scanner.load_holder()
    rec.starts.clear()
    scanner.calibrate(resolution=3600)
    afe = [s for s in rec.starts if s[0x03] == 0x30 and _u(s, 0x25, 3) == 2]
    strips = [s for s in rec.starts if s[0x02] == 0x00 and _u(s, 0x25, 3) == 256]
    dark = [s for s in strips if s[0x03] == 0x20]
    white = [s for s in strips if s[0x01] & 0x20]
    assert sorted({_u(s, 0x28, 3) for s in afe}) == [11063, 13399]
    assert all(s[0x1D] == 0x00 for s in afe)
    assert dark and all((s[0x1D], s[0x2B], s[0xA5], s[0xAB]) == (0x01, 0x04, 0x01, 0x30) for s in dark)
    assert white and all((s[0x1D], s[0x2B], s[0xA5], s[0xAB]) == (0x01, 0x1F, 0x04, 0x04) for s in white)


def test_1200_calibration_strips_unchanged(scanner, rec):
    scanner.load_holder()
    rec.starts.clear()
    scanner.calibrate(resolution=1200)
    strips = [s for s in rec.starts if _u(s, 0x25, 3) in (2, 256)]
    assert strips and all(s[0x1D] == 0x00 for s in strips)
    afe = [s for s in strips if _u(s, 0x25, 3) == 2]
    assert sorted({_u(s, 0x28, 3) for s in afe}) == [10935, 12103]


def test_whole_strip_at_7200_is_refused_before_any_move(scanner, rec):
    scanner.load_holder()
    rec.starts.clear()
    rec.writes.clear()
    with pytest.raises(ScanError, match="per pass"):
        scanner.scan(resolution=7200, frame=None)
    assert rec.starts == [] and rec.writes == []


def test_pass_size_limit_admits_a_7200_frame_and_a_3600_strip():
    from pyopticfilm.scan.geometry import compute_strip_geometry
    from pyopticfilm.scan.session_gl128_loader import MAX_PASS_RAW_BYTES

    assert compute_geometry(7200, model=MODEL_135I).total_bytes < MAX_PASS_RAW_BYTES
    assert compute_strip_geometry(3600, model=MODEL_135I).total_bytes < MAX_PASS_RAW_BYTES
    assert compute_strip_geometry(7200, model=MODEL_135I).total_bytes > MAX_PASS_RAW_BYTES


def test_me_long_pass_reuses_the_calibration(scanner, rec):
    scanner.load_holder()
    rec.starts.clear()
    scanner.scan(resolution=600, multi_exposure=True, me_long_mode="fixed")
    strips = [s for s in rec.starts if s[0x02] == 0x00 and _u(s, 0x25, 3) == 256]
    dark = [s for s in strips if s[0x03] == 0x20]
    white = [s for s in strips if s[0x01] & 0x20]
    images = [s for s in rec.starts if s[0x02] == 0x30]
    assert (len(dark), len(white), len(images)) == (1, 1, 2)
    # Both passes run with the measured tables (DVDSET).
    assert all(s[0x01] & 0x20 for s in images)


def test_7200_geometry_unstaggers_by_sensor_column():
    g = compute_geometry(7200, model=MODEL_135I)
    assert (g.stagger_y, g.num_staggered_lines, g.stagger_phase) == ((4, 0), 4, 35)
    # After the 48-row blue shift the buffer still holds the 4 stagger rows.
    pairs = g.optical_line_count // g.lincnt_per_line
    assert pairs - g.shift_b >= g.lines + g.num_staggered_lines
    for dpi in (600, 1200, 3600):
        assert compute_geometry(dpi, model=MODEL_135I).stagger_y == ()


def test_7200_unstagger_aligns_a_four_row_column_offset():
    """A horizontal edge read 4 rows later on every other sensor column comes out straight."""
    from dataclasses import replace

    from pyopticfilm.scan.session_gl128_loader import LoaderImagePipeline, plane_geometry

    g = compute_geometry(7200, model=MODEL_135I, area=(0.0, 0.0, 0.01, 0.01))
    plane = plane_geometry(g)
    g = replace(g, shift_r=0, shift_g=0, shift_b=0)
    rows, width = plane.optical_line_count, g.pixels
    img = np.full((rows, width, 3), 1000, dtype="<u2")
    edge = 40
    for x in range(width):
        # Sensor column parity = (pixel_startx + x); odd columns lag by 4 rows.
        lag = 4 if (g.pixel_startx + x) % 2 == 0 else 0
        img[edge + lag :, x, :] = 50000
    pipe = LoaderImagePipeline(MODEL_135I)
    out = pipe.assemble(memoryview(img.tobytes()), g, planar=False, expose_base=False)
    col = out[:, :, 1]
    first_bright = [int(np.argmax(col[:, x] > 25000)) for x in range(col.shape[1])]
    assert len(set(first_bright)) == 1
