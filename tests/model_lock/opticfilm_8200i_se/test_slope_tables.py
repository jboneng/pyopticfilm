# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen 8200i SE slope-table feed order — do not retarget without fresh capture evidence."""

from __future__ import annotations

from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.select import create_asic
from pyopticfilm.device.tables_8200i_se import SLOPE_TABLE_FAST, SLOPE_TABLE_SLOW
from pyopticfilm.usb.fake import MockScannerTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol


def _track_ahb_writes(asic, protocol):
    ahb_writes: list[bytes] = []
    orig_write_ahb = protocol.write_ahb

    def _tracking_write_ahb(addr, data):
        r = asic.registers
        if addr in (r.AHB_SLOPE_SCAN, r.AHB_SLOPE_FAST):
            ahb_writes.append(bytes(data))
        return orig_write_ahb(addr, data)

    protocol.write_ahb = _tracking_write_ahb
    return ahb_writes


def _pack(words: tuple[int, ...]) -> bytes:
    out = bytearray(len(words) * 2)
    for i, word in enumerate(words):
        out[2 * i] = word & 0xFF
        out[2 * i + 1] = (word >> 8) & 0xFF
    return bytes(out)


def test_position_for_full_frame_scan_uses_slow_then_fast_on_se(monkeypatch):
    """SE SilverFast: first feed SLOW, second FAST (same order as V2)."""
    usb = MockScannerTransport()
    protocol = GenesysUsbProtocol(usb)
    asic = create_asic(protocol, MODEL_8200I_SE)
    asic._motor_moves_enabled = True
    ahb_writes = _track_ahb_writes(asic, protocol)

    asic.position_for_full_frame_scan(scan_steps=13704)

    fast = _pack(SLOPE_TABLE_FAST)
    slow = _pack(SLOPE_TABLE_SLOW)
    assert len(ahb_writes) == 4  # 2 windows x 2 feeds
    assert ahb_writes[0] == slow
    assert ahb_writes[1] == slow
    assert ahb_writes[2] == fast
    assert ahb_writes[3] == fast
