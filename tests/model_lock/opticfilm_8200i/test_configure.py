# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen 8200i GL845 configure / feed / home registers — do not retarget to match new code."""

from __future__ import annotations

from dataclasses import replace

import pytest

from pyopticfilm.asic.gl845 import HOME_FEED_STEPS, Gl845
from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.scan.session import ScanSession
from pyopticfilm.usb.fake import MockScannerTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol


def _session():
    usb = MockScannerTransport()
    asic = Gl845(GenesysUsbProtocol(usb), MODEL_8200I)
    asic.init()
    return usb, ScanSession(asic, MODEL_8200I, None)


def _r16(regs, addr):
    return (regs.get(addr, 0) << 8) | regs.get(addr + 1, 0)


def _r24(regs, addr):
    return (regs.get(addr, 0) << 16) | (regs.get(addr + 1, 0) << 8) | regs.get(addr + 2, 0)


def _motor_starts(usb):
    """Register state at each motor start (``0x0F = 1``)."""
    regs: dict[int, int] = {}
    starts = []
    for txn in usb.transactions:
        if txn.operation != "control_write" or txn.request != 0x04 or txn.value != 0x83:
            continue
        data = txn.data or b""
        for i in range(0, len(data) - 1, 2):
            regs[data[i]] = data[i + 1]
            if data[i] == 0x0F and data[i + 1] == 0x01:
                starts.append(dict(regs))
    return starts


@pytest.mark.parametrize(
    ("dpi", "strpixel", "endpixel", "maxwd", "feedl"),
    [
        (900, 256, 10544, 5787, 16133),
        (1800, 260, 10500, 11520, 16148),
        (3600, 260, 10532, 23112, 16152),
        (7200, 261, 10549, 46296, 16153),
    ],
)
def test_full_frame_registers(dpi, strpixel, endpixel, maxwd, feedl):
    usb, session = _session()
    geo = compute_geometry(dpi, model=MODEL_8200I)
    session._configure(geo)
    r = usb.registers
    assert _r16(r, 0x38) == 14000  # LPERIOD
    assert _r16(r, 0x2C) == dpi // 6  # DPISET
    assert (_r16(r, 0x30), _r16(r, 0x32)) == (strpixel, endpixel)
    assert _r24(r, 0x25) == geo.lincnt_register
    assert _r24(r, 0x35) == maxwd
    assert _r24(r, 0x3D) == feedl
    assert r[0x02] == 0x10  # MTRPWR, forward
    assert r[0x03] == 0xBF  # lamp on, watchdog on outside the image pass
    assert r[0x04] == 0x62  # 16-bit colour, ADI FESET


def test_sensor_and_motor_registers():
    usb, session = _session()
    session._configure(compute_geometry(1800, model=MODEL_8200I))
    r = usb.registers
    assert (r[0x58], r[0x59]) == (0x2A, 0x72)  # sensor sampling point
    assert (r[0x5F], r[0x67], r[0x68], r[0x6A]) == (0x01, 0x80, 0x80, 0xFF)
    assert r[0x6C] == 0x4C


@pytest.mark.parametrize(
    ("dpi", "image_feedl"),
    [(900, 1577), (1800, 1592), (3600, 1596), (7200, 1597)],
)
def test_fast_feed_then_image_pass(dpi, image_feedl):
    # Hardware logs: fast feed 3639 steps (FEEDL 14556), then the image pass
    # feeds the last 400 steps (FEEDL 1577 at 900 dpi, 1592 at 1800 dpi).
    usb, session = _session()
    geo = session._fast_feed(compute_geometry(dpi, model=MODEL_8200I))
    (feed,) = _motor_starts(usb)
    assert feed[0x02] == 0x18  # MTRPWR | FASTFED
    assert not feed[0x01] & 0x01  # SCAN off
    assert _r24(feed, 0x3D) == 14556
    assert geo.starty == 400
    r = usb.registers
    assert r[0x02] == 0x08  # motor power dropped after the feed
    assert _r24(r, 0x3D) == 1

    session._configure(geo, image_pass=True)
    assert _r24(r, 0x3D) == image_feedl
    assert r[0x02] == 0x30  # MTRPWR | AGOHOME: returns home when the scan ends
    assert r[0x03] == 0x3F  # lamp watchdog off for the image pass


def test_stationary_capture_runs_with_motor_power_off():
    usb, session = _session()
    session._lamp_requested = False
    session._configure(compute_geometry(1800, model=MODEL_8200I))
    session._hold_motor_off()
    r = usb.registers
    assert r[0x02] == 0x00
    assert _r24(r, 0x3D) == 1
    assert not r[0x03] & 0x10  # lamp off


def test_me_long_pass_writes_lperiod():
    usb, session = _session()
    geo = compute_geometry(1800, model=MODEL_8200I)
    session._configure(replace(geo, exposure_lperiod=42000))
    assert _r16(usb.registers, 0x38) == 42000
    assert _r16(usb.registers, 0x2C) == 300


def test_reverse_home_seek_registers():
    assert Gl845.REVERSE_SCAN_HOME is True
    usb, session = _session()
    session.start_home_seek(starty=HOME_FEED_STEPS)
    r = usb.registers
    assert r[0x02] == 0xB4  # MTRPWR | AGOHOME | NOTHOME | MTRREV
    assert r[0x04] == 0x2A  # 8-bit green
    assert _r24(r, 0x25) == 3
    assert _r16(r, 0x2C) == 150  # lowest resolution (900 dpi)
    assert _r24(r, 0x3D) == 159977
