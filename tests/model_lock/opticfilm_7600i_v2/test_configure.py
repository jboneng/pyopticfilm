# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen 7600i v2 GL845 configure / home-seek registers — do not retarget to match new code."""

from __future__ import annotations

from dataclasses import replace

import pytest

from pyopticfilm.asic.gl845 import HOME_FEED_STEPS, Gl845
from pyopticfilm.device.select import MODEL_7600I_V2
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.scan.session import ScanSession
from pyopticfilm.usb.fake import MockScannerTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol


def _session():
    usb = MockScannerTransport()
    asic = Gl845(GenesysUsbProtocol(usb), MODEL_7600I_V2)
    asic.init()
    return usb, ScanSession(asic, MODEL_7600I_V2, None)


def _r16(regs, addr):
    return (regs.get(addr, 0) << 8) | regs.get(addr + 1, 0)


def _r24(regs, addr):
    return (regs.get(addr, 0) << 16) | (regs.get(addr + 1, 0) << 8) | regs.get(addr + 2, 0)


@pytest.mark.parametrize(
    ("dpi", "strpixel", "endpixel", "maxwd", "feedl"),
    [
        (900, 256, 10552, 5791, 16133),
        (1800, 260, 10500, 11520, 16148),
        (3600, 260, 10532, 23112, 16152),
        (7200, 261, 10549, 46296, 16153),
    ],
)
def test_full_frame_registers(dpi, strpixel, endpixel, maxwd, feedl):
    usb, session = _session()
    geo = compute_geometry(dpi, model=MODEL_7600I_V2)
    session._configure(geo)
    r = usb.registers
    assert _r16(r, 0x38) == 14000  # LPERIOD
    assert _r16(r, 0x2C) == dpi // 6  # DPISET
    assert (_r16(r, 0x30), _r16(r, 0x32)) == (strpixel, endpixel)
    assert _r24(r, 0x25) == geo.lincnt_register
    assert _r24(r, 0x35) == maxwd
    assert _r24(r, 0x3D) == feedl
    assert r[0x02] == 0x10  # MTRPWR, forward
    assert r[0x03] & 0x10  # LAMPPWR
    assert r[0x04] == 0x62  # 16-bit colour, ADI FESET


def test_me_long_pass_writes_lperiod():
    usb, session = _session()
    geo = compute_geometry(1800, model=MODEL_7600I_V2)
    session._configure(replace(geo, exposure_lperiod=64400))
    assert _r16(usb.registers, 0x38) == 64400
    assert _r16(usb.registers, 0x2C) == 300


def test_dark_capture_keeps_lamp_off():
    usb, session = _session()
    session._lamp_requested = False
    session._configure(compute_geometry(1800, model=MODEL_7600I_V2))
    assert not usb.registers[0x03] & 0x10


def test_reverse_home_seek_registers():
    assert Gl845.REVERSE_SCAN_HOME is True
    usb, session = _session()
    session.start_home_seek(starty=HOME_FEED_STEPS)
    r = usb.registers
    assert r[0x02] == 0xB4  # MTRPWR | AGOHOME | NOTHOME | MTRREV
    assert r[0x04] == 0x2A  # 8-bit green
    assert _r24(r, 0x25) == 3
    assert _r16(r, 0x2C) == 150  # lowest resolution (900 dpi)
