# SPDX-License-Identifier: GPL-3.0-or-later
"""OpticFilm 135i leaf: tables, register rules and boot against the VueScan captures."""

from __future__ import annotations

import pytest

from pyopticfilm.device.gl128_common import gl128_firmware_variant
from pyopticfilm.device.model_135i import MODEL_135I
from pyopticfilm.device.select import create_asic, model_is_scan_ready
from pyopticfilm.device.tables_135i import (
    FRAME_PITCH_STEPS,
    INIT_REGS_135I,
    LOADER_EJECT_STEPS,
    LOADER_LOAD_FORWARD_STEPS,
    LOADER_LOAD_REVERSE_STEPS,
    SCAN_RAMP,
    SLOPE_TABLE_FEED,
    SLOPE_TABLE_LOADER,
    dpi_subkey,
    feedl_for_move,
    hardware_x_dpi,
    image_line_margin,
    scan_motor_table,
    timing_set_for,
)
from pyopticfilm.usb.fake import MockScannerTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol

# --- motor tables (AHB words uploaded in the captures) -----------------------


def test_loader_ramp_matches_insert_capture():
    # insert 35mm carrier: both load feeds, 0x1000C000 and 0x10010000.
    assert SLOPE_TABLE_LOADER[:8] == (34482, 10256, 6968, 5555, 4739, 4184, 3780, 3472)
    assert SLOPE_TABLE_LOADER[-1] == 470
    assert len(SLOPE_TABLE_LOADER) == len(set(SLOPE_TABLE_LOADER)) == 256


def test_feed_ramp_matches_positioning_and_eject_captures():
    # Every positioning feed (previews, 7200 dpi scan) and holder eject.
    assert SLOPE_TABLE_FEED[:8] == (64817, 19278, 13098, 10442, 8908, 7864, 7106, 6526)
    assert SLOPE_TABLE_FEED[-1] == 884
    assert MODEL_135I.slope_table_fast() is SLOPE_TABLE_FEED
    assert MODEL_135I.slope_table_slow() is SLOPE_TABLE_LOADER


@pytest.mark.parametrize(
    ("period", "distinct", "ramp_entries"),
    [(4666, 11, 10), (2333, 35, 34), (56000, 1, 0)],
)
def test_scan_motor_table_matches_image_pass_captures(period, distinct, ramp_entries):
    # 1200 dpi preview, 600 dpi panorama, 7200 dpi frame scan.
    table = scan_motor_table(period)
    assert len(table) == 256
    assert len(set(table)) == distinct
    assert sum(1 for v in table if v > period) == ramp_entries
    if ramp_entries:
        assert table[:6] == (54484, 16345, 11122, 8887, 7564, 6689)
    assert table[-1] == period
    assert SCAN_RAMP[0] == 54484


def test_scan_motor_period_matches_captures():
    assert MODEL_135I.channel_exposure_for(1200) == 4666
    assert MODEL_135I.channel_exposure_for(600) == 2333
    assert MODEL_135I.channel_exposure_for(7200, exposure=28000) == 56000
    # 7200 dpi runs exposure 28000 (multiplier 2) by default, 0x90 = 44000.
    assert MODEL_135I.channel_exposure_for(7200) == 56000
    assert MODEL_135I.image_exposure_for(1200) == 14000
    regs = MODEL_135I.exposure_regs_for(7200)
    assert (regs[0x7D], regs[0x7E], regs[0x7F]) == (0x00, 0x6D, 0x60)
    assert (regs[0x90], regs[0x91], regs[0x92]) == (0x00, 0xAB, 0xE0)
    assert (MODEL_135I.exposure_regs_for(600)[0x91], MODEL_135I.exposure_regs_for(600)[0x92]) == (0x2A, 0xF8)


@pytest.mark.parametrize(
    ("dpi", "span", "lperiod"),
    [(600, 5184, 11023), (1200, 5184, 11239), (7200, 10368, 15991), (1200, 2592, 11023)],
)
def test_line_period_takes_the_programmed_span(dpi, span, lperiod):
    # END - STR in the timing set's sensor units, as programmed.
    assert MODEL_135I.line_period_for(dpi, span=span) == lperiod


def test_move_split_matches_loader_feeds():
    # FEEDL on the wire: load forward 6690, reverse seek 71490, eject 3090.
    assert feedl_for_move(LOADER_LOAD_FORWARD_STEPS) == 6690
    assert feedl_for_move(LOADER_LOAD_REVERSE_STEPS) == 71490
    assert feedl_for_move(LOADER_EJECT_STEPS) == 3090
    assert feedl_for_move(300) == 1


def test_frame_feeds_match_preview_capture():
    assert [MODEL_135I.frame_feed_steps(i) for i in range(3)] == [6438, 17190, 27942]
    assert FRAME_PITCH_STEPS * 25.4 / MODEL_135I.feed_steps_per_inch == pytest.approx(37.93, abs=0.01)


# --- per-resolution register rules ------------------------------------------


@pytest.mark.parametrize(
    ("y_dpi", "subkey", "timing"),
    [
        (150, 300, "Speed2"),
        (300, 300, "Speed2"),
        (600, 600, "Speed2"),
        (900, 1200, "Speed2"),
        (1200, 1200, "Speed2"),
        (1800, 3600, "Speed2"),
        (3600, 3600, "Speed2"),
        (7200, 7200, "Speed1"),
    ],
)
def test_ini_subkey_and_timing_set(y_dpi, subkey, timing):
    assert dpi_subkey(y_dpi) == subkey
    assert timing_set_for(y_dpi) == timing


def test_listed_resolutions_have_a_native_hardware_rung():
    for dpi in MODEL_135I.resolutions_dpi:
        asic = MODEL_135I.asic_dpi_for(dpi)
        assert hardware_x_dpi(asic, MODEL_135I.sensor_speed_for(dpi)) == asic
    # 1440 and 2400 would need host resampling under Speed2.
    assert hardware_x_dpi(1440, 2) == 1800
    assert hardware_x_dpi(2400, 2) == 3600


@pytest.mark.parametrize(
    ("dpi", "window"),
    [(600, (35, 5219)), (1200, (35, 5219)), (7200, (35, 10403))],
)
def test_image_window_matches_captures(dpi, window):
    assert MODEL_135I.image_window(dpi) == window


def test_line_margin_rule():
    assert image_line_margin(7200, 7200) == 53
    assert image_line_margin(1200, 1200) == 9
    assert image_line_margin(600, 600) == 5
    assert MODEL_135I.image_line_margin(7200) == 53


# Image-pass register state in the captures (after the scan overlay).
_CAPTURED_IMAGE_PASS = {
    1200: {0x06: 0xF8, 0x16: 0x23, 0x18: 0x00, 0x1C: 0x00, 0x1D: 0x00, 0x3B: 0x00, 0x3C: 0x01,
           0x52: 0x0F, 0x53: 0x02, 0x54: 0x05, 0x55: 0x08, 0x56: 0x0A, 0x57: 0x0D,
           0x5A: 0x62, 0x5B: 0x22, 0x70: 0x00, 0x71: 0x01, 0x72: 0x01, 0x73: 0x02,
           0x88: 0x00, 0x89: 0x05, 0x90: 0x00, 0x91: 0x2A, 0x92: 0xF8, 0x0B: 0x8C},
    600: {0x18: 0x00, 0x3B: 0x02, 0x3C: 0x00, 0x52: 0x0F, 0x5A: 0x62, 0x70: 0x00},
    7200: {0x06: 0xF8, 0x18: 0x10, 0x3B: 0x00, 0x3C: 0x01,
           0x52: 0x0A, 0x53: 0x0D, 0x54: 0x0F, 0x55: 0x02, 0x56: 0x05, 0x57: 0x08,
           0x5A: 0x2A, 0x5B: 0x6A, 0x70: 0x0A, 0x71: 0x0B, 0x72: 0x0B, 0x73: 0x0C,
           0x88: 0x00, 0x89: 0x05, 0x80: 0x00, 0x81: 0x23},
}
# Captured full values of the masked registers on the same passes.
_CAPTURED_MASKED = {
    1200: {0x03: 0x30, 0x0C: 0x01, 0x15: 0x90, 0x19: 0x08, 0x32: 0xB5, 0x33: 0x0E, 0x35: 0xFB, 0x3A: 0x10},
    7200: {0x03: 0x30, 0x0C: 0x01, 0x15: 0x90, 0x19: 0x08, 0x32: 0x35, 0x33: 0x0E, 0x35: 0xFB, 0x3A: 0x10},
}


@pytest.mark.parametrize("dpi", sorted(_CAPTURED_IMAGE_PASS))
def test_toggle_scan_overlay_matches_capture(dpi):
    regs = MODEL_135I.scan_regs_for(dpi, infrared=True)
    for addr, value in _CAPTURED_IMAGE_PASS[dpi].items():
        assert regs[addr] == value, hex(addr)


@pytest.mark.parametrize("dpi", sorted(_CAPTURED_MASKED))
def test_toggle_masked_bits_match_capture(dpi):
    masked = MODEL_135I.scan_masked_regs_for(dpi, infrared=True)
    for addr, wire in _CAPTURED_MASKED[dpi].items():
        mask, value = masked[addr]
        assert wire & mask == value, hex(addr)
    # IR LED (0x32 bit 5) on in toggle mode, off for plain colour.
    assert masked[0x32][1] & 0x20
    assert not MODEL_135I.scan_masked_regs_for(dpi, infrared=False)[0x32][1] & 0x20


# --- boot ----------------------------------------------------------------------


def _boot_transactions():
    usb = MockScannerTransport()
    asic = create_asic(GenesysUsbProtocol(usb), MODEL_135I)
    asic._motor_moves_enabled = False
    asic.init()
    return usb


def _register_writes(usb):
    out = []
    for txn in usb.transactions:
        if txn.operation == "control_write" and (txn.value & 0xFF) == 0x83:
            data = txn.data or b""
            high = 0x100 if txn.value & 0x100 else 0
            out.extend((data[i] | high, data[i + 1]) for i in range(0, len(data), 2))
    return out


def test_boot_replays_vuescan_open():
    usb = _boot_transactions()
    assert not [t for t in usb.transactions if t.operation == "bulk_write"]
    writes = _register_writes(usb)
    blast = writes[: len(INIT_REGS_135I)]
    assert blast == sorted(INIT_REGS_135I.items())
    tail = writes[len(INIT_REGS_135I):]
    assert tail[:5] == [(0x0B, 0x64), (0x13, 0x0F), (0x0B, 0x6C), (0x03, 0x10), (0x03, 0x00)]
    afe = tail[5:]
    assert afe[:3] == [(0x51, 0x00), (0x5D, 0x00), (0x5E, 0xF8)]
    assert len(afe) == 24
    assert not any(0xD0 <= addr <= 0xF8 for addr, _ in tail)


def test_boot_vendor_requests_in_capture_order():
    usb = _boot_transactions()
    vendor = [
        (t.operation, t.request, t.value, t.index, t.data if t.operation == "control_write" else t.length)
        for t in usb.transactions
        if (t.value or 0) & 0xFF in (0x8A, 0x8B, 0x8C)
    ]
    assert vendor == [
        ("control_write", 0x0C, 0x8C, 0x10, b"\x0d"),
        ("control_write", 0x0C, 0x8C, 0x13, b"\x0d"),
        ("control_write", 0x0C, 0x8B, 0x07, b"\x01"),
        ("control_write", 0x04, 0x8B, 0x09, bytes(4)),
        ("control_read", 0x04, 0x8A, 0x10, 3),
        ("control_write", 0x04, 0x8B, 0x0B, bytes(4)),
        ("control_read", 0x04, 0x8A, 0x0F, 64),
    ]


@pytest.mark.parametrize("serial", ["1", "20241113", "20201119", None])
def test_firmware_serial_never_changes_the_135i_boot_map(serial):
    assert gl128_firmware_variant(MODEL_135I, serial) is MODEL_135I


def test_135i_is_not_scan_ready():
    assert model_is_scan_ready(MODEL_135I) is False
