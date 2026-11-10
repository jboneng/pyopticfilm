# SPDX-License-Identifier: GPL-3.0-or-later
"""USB identity, claim helpers, and Genesys protocol tests (no hardware)."""

from __future__ import annotations

import pytest
from scanners.fake_usb import FakeUsbTransport as FakeTransport

from pyopticfilm.exceptions import UsbError
from pyopticfilm.usb.device import (
    PID_OPTICFILM_7600I,
    PID_OPTICFILM_8200I,
    PID_OPTICFILM_8200I_SE,
    VID_PLUSTEK,
    UsbDeviceHandle,
    UsbDeviceInfo,
    _info_from_pyusb,
)
from pyopticfilm.usb.protocol import (
    INDEX,
    REGISTER_LINK_OK,
    REQUEST_BUFFER,
    REQUEST_TYPE_IN,
    REQUEST_TYPE_OUT,
    VALUE_BUF_ENDACCESS,
    VALUE_BUFFER,
    VALUE_GET_REGISTER,
    VALUE_READ_REGISTER,
    VALUE_SET_REGISTER,
    VALUE_WRITE_REGISTER,
    GenesysUsbProtocol,
)


def test_device_id_format():
    info = UsbDeviceInfo(
        vendor_id=VID_PLUSTEK,
        product_id=PID_OPTICFILM_8200I,
        bus=1,
        address=8,
    )
    assert info.device_id == "plustek:usb:07b3:130d:001:008"
    assert info.is_supported
    assert not info.is_known_unsupported


def test_se_variant_identified_as_gl128():
    info = UsbDeviceInfo(
        vendor_id=VID_PLUSTEK,
        product_id=PID_OPTICFILM_8200I_SE,
        bus=1,
        address=2,
    )
    assert info.is_supported
    assert info.is_8200i_se
    assert info.asic_hint == "GL128"
    assert not info.is_known_unsupported
    handle = UsbDeviceHandle(info)
    assert handle.info.product_id == PID_OPTICFILM_8200I_SE


class _UnopenableDevice:
    """A device libusb can see but not open, as when a vendor driver owns it."""

    idVendor = VID_PLUSTEK
    idProduct = PID_OPTICFILM_8200I_SE
    bcdDevice = 0x0702
    iManufacturer = 1
    iProduct = 2
    bus = 2
    address = 16


class _RefusingUtil:
    @staticmethod
    def get_string(_dev, _index):
        # libusb-1.0 on Windows surfaces "not supported on this platform" here.
        raise NotImplementedError("Operation not supported or unimplemented")


def test_listing_survives_unreadable_string_descriptors():
    info = _info_from_pyusb(_UnopenableDevice(), _RefusingUtil(), read_strings=True)
    assert info.product_id == PID_OPTICFILM_8200I_SE
    assert info.bcd_device == 0x0702
    assert info.manufacturer is None
    assert info.product is None


def test_listing_skips_string_reads_by_default():
    class _BoomUtil:
        @staticmethod
        def get_string(_dev, _index):
            raise AssertionError("string reads must be skipped by default")

    info = _info_from_pyusb(_UnopenableDevice(), _BoomUtil())
    assert info.product is None
    assert info.manufacturer is None
    assert info.product_id == PID_OPTICFILM_8200I_SE


def test_genesys_constants_match_sane_low_h():
    assert REQUEST_TYPE_IN == 0xC0
    assert REQUEST_TYPE_OUT == 0x40
    assert REQUEST_BUFFER == 0x04
    assert VALUE_BUFFER == 0x82
    assert VALUE_SET_REGISTER == 0x83
    assert VALUE_READ_REGISTER == 0x84
    assert VALUE_WRITE_REGISTER == 0x85
    assert VALUE_BUF_ENDACCESS == 0x8C
    assert VALUE_GET_REGISTER == 0x8E
    assert REGISTER_LINK_OK == 0x55


def test_link_status_check():
    GenesysUsbProtocol.check_link_status(0x55)
    with pytest.raises(UsbError, match="0x00"):
        GenesysUsbProtocol.check_link_status(0x00)


def test_abort_bulk_stream_calls_transport():
    transport = FakeTransport()
    assert GenesysUsbProtocol(transport).abort_bulk_stream() == 42
    assert transport.aborted == 1


class _ResettableDevice:
    def __init__(self) -> None:
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1


@pytest.mark.parametrize(("platform", "resets"), [("darwin", 0), ("linux", 1), ("win32", 1)])
def test_close_after_aborted_bulk_resets_except_on_macos(monkeypatch, platform, resets):
    import sys

    import usb.util

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(usb.util, "release_interface", lambda *_a, **_k: None)
    monkeypatch.setattr(usb.util, "dispose_resources", lambda *_a, **_k: None)
    handle = UsbDeviceHandle(UsbDeviceInfo(vendor_id=VID_PLUSTEK, product_id=PID_OPTICFILM_7600I))
    dev = _ResettableDevice()
    handle._dev = dev
    handle._claimed = True
    handle._needs_usb_reset = True

    handle.close()

    assert dev.resets == resets
    assert not handle.is_open


def test_abort_bulk_stream_without_transport_method():
    class _Bare:
        def control_msg(self, *a, **k):
            raise AssertionError("unused")

        def bulk_read(self, size, **k):
            return b""

        def bulk_write(self, data, **k):
            return 0

    assert GenesysUsbProtocol(_Bare()).abort_bulk_stream() == 0  # type: ignore[arg-type]


def test_read_register_gl845_framing():
    transport = FakeTransport(registers={0x6B: 0x30})
    proto = GenesysUsbProtocol(transport)
    assert proto.read_register(0x6B) == 0x30

    rtype, req, value, index, length = transport.control_log[0]
    assert rtype == REQUEST_TYPE_IN
    assert req == REQUEST_BUFFER
    assert value == VALUE_GET_REGISTER
    assert index == (0x22 + (0x6B << 8)) & 0xFFFF
    assert length == 2


def test_read_register_rejects_bad_link():
    transport = FakeTransport()

    def bad_control_msg(*_a, **_k):
        return bytes((0x11, 0x00))

    transport.control_msg = bad_control_msg  # type: ignore[method-assign]
    with pytest.raises(UsbError, match="link status"):
        GenesysUsbProtocol(transport).read_register(0x6B)


def test_write_register_gl845_framing():
    transport = FakeTransport()
    proto = GenesysUsbProtocol(transport)
    proto.write_register(0x6C, 0x4C)

    rtype, req, value, index, payload = transport.control_log[0]
    assert rtype == REQUEST_TYPE_OUT
    assert req == REQUEST_BUFFER
    assert value == VALUE_SET_REGISTER
    assert index == INDEX
    assert payload == bytes((0x6C, 0x4C))
    assert transport.registers[0x6C] == 0x4C


def test_write_register_high_address_sets_flag():
    transport = FakeTransport()
    GenesysUsbProtocol(transport).write_register(0x1A6, 0x07)
    _rtype, _req, value, _index, payload = transport.control_log[0]
    assert value == (VALUE_SET_REGISTER | 0x100)
    assert payload == bytes((0xA6, 0x07))


def test_read_write_roundtrip():
    transport = FakeTransport()
    proto = GenesysUsbProtocol(transport)
    proto.write_register(0x6D, 0x80)
    assert proto.read_register(0x6D) == 0x80


def test_bulk_read_data_sends_gl845_header_per_chunk():
    transport = FakeTransport(bulk_in_queue=[b"\x01\x02\x03\x04"])
    data = GenesysUsbProtocol(transport).bulk_read_data(4)
    assert data == b"\x01\x02\x03\x04"
    # first control is the bulk header
    rtype, req, value, index, payload = transport.control_log[0]
    assert rtype == REQUEST_TYPE_OUT
    assert req == REQUEST_BUFFER
    assert value == VALUE_BUFFER
    assert index == 0x00
    assert payload[3] == 0x10  # 0x10000000 address marker
    assert payload[4:8] == b"\x04\x00\x00\x00"


def test_write_ahb_header_and_bulk():
    transport = FakeTransport()
    GenesysUsbProtocol(transport).write_ahb(0x10000000, b"abcd")
    _rtype, _req, value, index, payload = transport.control_log[0]
    assert value == VALUE_BUFFER
    assert index == 0x01
    assert payload[0:4] == b"\x00\x00\x00\x10"
    assert payload[4:8] == b"\x04\x00\x00\x00"
    assert transport.bulk_out_log == [b"abcd"]
