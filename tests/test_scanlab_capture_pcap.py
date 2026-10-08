# SPDX-License-Identifier: GPL-3.0-or-later
"""USBPcap capture parse + Lab decode helpers (no display)."""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from pyopticfilm.device.model_7400 import MODEL_8100
from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE
from pyopticfilm.usb.protocol import (
    REQUEST_BUFFER,
    REQUEST_TYPE_OUT,
    VALUE_SET_REGISTER,
)
from tools.scanlab.capture_pcap import (
    ME_EXPOSURE_LONG,
    ME_EXPOSURE_SHORT,
    USBPCAP_CONTROL_STAGE_SETUP,
    USBPCAP_INFO_PDO_TO_FDO,
    USBPCAP_TRANSFER_BULK,
    USBPCAP_TRANSFER_CONTROL,
    _keep_bulk_in_payload,
    analyze_usbpcap,
    build_usbpcap_packet,
    classify_capture_pass_kind,
    classify_capture_pass_label,
    decode_capture_bulk,
    format_capture_usb_log_lines,
    is_ir_capture_pass,
    is_me_long_pass,
    motor_register_diff,
    optical_snapshot,
    wrap_pcap_classic,
)


def _reg_write_packets(pairs: list[tuple[int, int]]) -> list[bytes]:
    """USBPcap SETUP with embedded OUT data (real Windows capture layout)."""
    payload = b"".join(bytes((a & 0xFF, v & 0xFF)) for a, v in pairs)
    setup = struct.pack(
        "<BBHHH",
        REQUEST_TYPE_OUT,
        REQUEST_BUFFER,
        VALUE_SET_REGISTER,
        0,
        len(payload),
    )
    return [
        build_usbpcap_packet(
            transfer=USBPCAP_TRANSFER_CONTROL,
            endpoint=0x00,
            info=0,
            data=setup + payload,
            control_stage=USBPCAP_CONTROL_STAGE_SETUP,
        ),
    ]


def _u16_bytes(addr: int, value: int) -> list[tuple[int, int]]:
    return [(addr, (value >> 8) & 0xFF), (addr + 1, value & 0xFF)]


def _u24_bytes(addr: int, value: int) -> list[tuple[int, int]]:
    return [
        (addr, (value >> 16) & 0xFF),
        (addr + 1, (value >> 8) & 0xFF),
        (addr + 2, value & 0xFF),
    ]


def test_analyze_usbpcap_registers_and_bulk(tmp_path: Path):
    # Optical window: DPISET=200 (1200 dpi on 8100), STR=0 END=16, LINCNT=2, FEEDL=100
    pairs: list[tuple[int, int]] = []
    pairs += _u16_bytes(0x2C, 200)
    pairs += _u16_bytes(0x30, 0)
    pairs += _u16_bytes(0x32, 16)
    pairs += _u24_bytes(0x25, 2)
    pairs += _u24_bytes(0x3D, 100)
    pairs.append((0x04, 0x60))  # 16-bit

    pixels, lines, channels = 16, 2, 3
    # Chunky RGB16: each pixel R,G,B little-endian
    row = []
    for x in range(pixels):
        row.extend([x & 0xFF, 0x00, 0x10, 0x00, 0x20, 0x00])  # R=x, G=0x10, B=0x20
    bulk = bytes(row) * lines
    # Split into USB-sized chunks with small status INs between (real captures
    # use 512-byte status; fixtures stay below the image chunk size).
    chunk = 64
    chunks = [bulk[i : i + chunk] for i in range(0, len(bulk), chunk)]

    packets = _reg_write_packets(pairs)
    for c in chunks:
        packets.append(
            build_usbpcap_packet(
                transfer=USBPCAP_TRANSFER_BULK,
                endpoint=0x81,
                info=USBPCAP_INFO_PDO_TO_FDO,
                data=c,
            )
        )
        packets.append(
            build_usbpcap_packet(
                transfer=USBPCAP_TRANSFER_BULK,
                endpoint=0x81,
                info=USBPCAP_INFO_PDO_TO_FDO,
                data=b"\x00" * 16,
            )
        )
    pcap = tmp_path / "8100_sample.pcap"
    pcap.write_bytes(wrap_pcap_classic(packets))

    analysis = analyze_usbpcap(pcap)
    assert analysis.image_bulk == bulk
    assert len(analysis.bulk_ins) == len(chunks) + len(chunks)  # image + status
    snap = optical_snapshot(analysis.registers, asic="GL845")
    assert snap["dpiset"] == 200
    assert snap["strpixel"] == 0
    assert snap["endpixel"] == 16
    assert snap["lincnt"] == 2
    assert snap["feedl"] == 100

    rgb, geo = decode_capture_bulk(MODEL_8100, analysis, dpi=1200, planar=False)
    assert rgb.shape == (lines, pixels, channels)
    assert int(rgb[0, 5, 0]) == 5  # R
    assert int(rgb[0, 5, 1]) == 0x10
    assert int(rgb[0, 5, 2]) == 0x20
    assert geo.pixels == 16

    # Planar layout of the same bytes looks different
    rgb_p, _ = decode_capture_bulk(MODEL_8100, analysis, dpi=1200, planar=True)
    assert rgb_p.shape == rgb.shape
    assert not np.array_equal(rgb_p, rgb)

    diff = "\n".join(motor_register_diff(MODEL_8100, analysis, dpi=1200))
    assert "Capture DPISET=200" in diff
    assert "Capture LINCNT=2" in diff
    assert "Capture FEEDL=100" in diff

    log = "\n".join(format_capture_usb_log_lines(analysis))
    assert "======== CAPTURE" in log
    assert "control_write" in log
    assert "bulk_read" in log
    assert "register writes" in log


def test_capture_pass_classification():
    assert is_ir_capture_pass({0x03: 0x20, 0x37: 0xc0})
    assert not is_ir_capture_pass({0x03: 0x30, 0x37: 0xc0})
    preview_regs = {0x03: 0x30, 0x2C: 0x00, 0x2D: 0xC8, 0x25: 0, 0x26: 0x12, 0x27: 0xE4}
    assert (
        classify_capture_pass_kind(preview_regs, capture_has_ir=False, asic="GL128")
        == "prescan"
    )
    colour_regs = {0x03: 0x30, 0x2C: 0x01, 0x2D: 0x2C, 0x25: 0, 0x26: 0x19, 0x27: 0xF4}
    assert (
        classify_capture_pass_kind(colour_regs, capture_has_ir=False, asic="GL128")
        == "color"
    )
    assert (
        classify_capture_pass_kind({0x03: 0x20, 0x2C: 300}, capture_has_ir=True)
        == "ir"
    )


def _exposure_regs(exp: int, *, r03: int = 0x30) -> dict[int, int]:
    return {
        0x03: r03,
        0x7D: (exp >> 16) & 0xFF,
        0x7E: (exp >> 8) & 0xFF,
        0x7F: exp & 0xFF,
        0x2C: 0x01,
        0x2D: 0x2C,
    }


def test_me_exposure_pass_labels():
    short = _exposure_regs(ME_EXPOSURE_SHORT)
    long = _exposure_regs(ME_EXPOSURE_LONG)
    ir = _exposure_regs(ME_EXPOSURE_SHORT, r03=0x20)
    assert not is_me_long_pass(short)
    assert is_me_long_pass(long)
    assert not is_me_long_pass(ir)
    assert (
        classify_capture_pass_label(short, kind="color", capture_has_me=True)
        == "color ME-short"
    )
    assert (
        classify_capture_pass_label(long, kind="color", capture_has_me=True)
        == "color ME-long"
    )
    assert (
        classify_capture_pass_label(short, kind="color", capture_has_me=False) == "color"
    )
    assert classify_capture_pass_label(ir, kind="ir", capture_has_me=True) == "ir"


def test_me_exposure_pass_labels_8300i_se():
    """8300i short is 15000; adaptive long (~60000) is any exp above short."""
    short = _exposure_regs(15000)
    long = _exposure_regs(60000)
    se_long = _exposure_regs(ME_EXPOSURE_LONG)
    assert not is_me_long_pass(short, model=MODEL_8300I_SE)
    assert is_me_long_pass(long, model=MODEL_8300I_SE)
    assert is_me_long_pass(se_long, model=MODEL_8300I_SE)
    assert not is_me_long_pass(short)  # no model: SE equality only
    assert not is_me_long_pass(long)  # 60000 ≠ SE 42000 without model
    assert (
        classify_capture_pass_label(
            short, kind="color", capture_has_me=True, model=MODEL_8300I_SE
        )
        == "color ME-short"
    )
    assert (
        classify_capture_pass_label(
            long, kind="color", capture_has_me=True, model=MODEL_8300I_SE
        )
        == "color ME-long"
    )


def test_keep_bulk_in_payload_skips_unaligned_status():
    # 7200: 65508-byte image URB is already RGB16-aligned; 512-byte status is not.
    assert _keep_bulk_in_payload(65508, 0) is True
    assert _keep_bulk_in_payload(512, 0) is False
    # 1800: 30208 leaves a 4-byte remainder; the following 512 completes a pixel.
    assert _keep_bulk_in_payload(30208, 0) is True
    assert (30208 + 512) % 6 == 0
    assert _keep_bulk_in_payload(512, 30208 % 6) is True
