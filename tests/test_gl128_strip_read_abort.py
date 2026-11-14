# SPDX-License-Identifier: GPL-3.0-or-later
"""A calibration strip read that fails mid-stream must stop the strip and abort bulk IN."""

from __future__ import annotations

import pytest

from pyopticfilm.device.model_135i import MODEL_135I
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.select import create_asic
from pyopticfilm.exceptions import UsbError
from pyopticfilm.usb.fake import MockFilmLoaderTransport, MockScannerTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)


def _asic(model, usb):
    asic = create_asic(GenesysUsbProtocol(usb), model)
    asic._motor_moves_enabled = True
    asic.init()
    return asic


def _fail_reads(asic, monkeypatch, order: list[str]) -> None:
    def fail(*_args, **_kwargs):
        raise UsbError("bulk IN timeout mid-strip")

    real_abort = asic.protocol.abort_bulk_stream
    monkeypatch.setattr(asic.protocol, "bulk_read_chunk", fail)
    monkeypatch.setattr(
        asic.protocol, "abort_bulk_stream", lambda: order.append("abort") or real_abort()
    )


@pytest.mark.parametrize(
    ("model", "transport"),
    [(MODEL_8200I_SE, MockScannerTransport), (MODEL_135I, MockFilmLoaderTransport)],
)
def test_failed_strip_read_stops_the_strip_then_aborts_bulk(monkeypatch, model, transport):
    usb = transport()
    asic = _asic(model, usb)
    order: list[str] = []
    _fail_reads(asic, monkeypatch, order)

    with pytest.raises(UsbError):
        asic._read_strip_stream(1024, stop=lambda: order.append("stop"))

    assert order == ["stop", "abort"]
    assert usb.aborted == 1


def test_failed_stop_still_aborts_bulk_and_keeps_the_read_error(monkeypatch):
    usb = MockScannerTransport()
    asic = _asic(MODEL_8200I_SE, usb)
    _fail_reads(asic, monkeypatch, [])

    def stop():
        raise UsbError("control transfer failed")

    with pytest.raises(UsbError, match="mid-strip"):
        asic._read_strip_stream(1024, stop=stop)
    assert usb.aborted == 1


def test_white_strip_read_error_clears_scan_and_aborts_bulk(monkeypatch):
    usb = MockScannerTransport()
    asic = _asic(MODEL_8200I_SE, usb)
    _fail_reads(asic, monkeypatch, [])
    r = asic.registers

    with pytest.raises(UsbError):
        asic.acquire_shading_strip(resolution=1800, dvdset=True)

    assert usb.aborted == 1
    assert not usb.registers[r.REG_0x01] & r.SCAN


def test_completed_strip_read_does_not_abort():
    usb = MockScannerTransport()
    asic = _asic(MODEL_8200I_SE, usb)
    asic.acquire_shading_strip(resolution=1800, dvdset=True)
    assert usb.aborted == 0
