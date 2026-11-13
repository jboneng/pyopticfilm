# SPDX-License-Identifier: GPL-3.0-or-later
"""Genesys USB vendor-request helpers (GL845 path).

Constants and register/bulk framing mirror SANE genesys ``low.h`` and
``scanner_interface_usb.cpp``. Ported under GPL-3.0; see NOTICE.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pyopticfilm.logging import get_logger
from pyopticfilm.usb.device import BULK_MAX_SIZE

logger = get_logger(__name__)

# USB bmRequestType bits (libusb / USB 2.0)
USB_TYPE_VENDOR = 0x40
USB_DIR_OUT = 0x00
USB_DIR_IN = 0x80

REQUEST_TYPE_IN = USB_TYPE_VENDOR | USB_DIR_IN  # 0xC0
REQUEST_TYPE_OUT = USB_TYPE_VENDOR | USB_DIR_OUT  # 0x40

REQUEST_REGISTER = 0x0C
REQUEST_BUFFER = 0x04

# From SANE genesys/low.h
VALUE_BUFFER = 0x82
VALUE_SET_REGISTER = 0x83
VALUE_READ_REGISTER = 0x84
VALUE_WRITE_REGISTER = 0x85
VALUE_INIT = 0x87
GPIO_OUTPUT_ENABLE = 0x89
GPIO_READ = 0x8A
GPIO_WRITE = 0x8B
VALUE_BUF_ENDACCESS = 0x8C
VALUE_GET_REGISTER = 0x8E

INDEX = 0x00

BULK_OUT = 0x01
BULK_IN = 0x00
BULK_RAM = 0x00
BULK_REGISTER = 0x11

# GL845/846/847/GL124 read path expects this status byte
REGISTER_LINK_OK = 0x55


@runtime_checkable
class UsbTransport(Protocol):
    """Minimal transport used by :class:`GenesysUsbProtocol`."""

    def control_msg(
        self,
        request_type: int,
        request: int,
        value: int,
        index: int,
        data_or_length: int | bytes | bytearray,
        *,
        timeout_ms: int | None = None,
    ) -> bytes: ...

    def bulk_read(self, size: int, *, timeout_ms: int | None = None) -> bytes: ...

    def bulk_write(
        self, data: bytes | bytearray, *, timeout_ms: int | None = None
    ) -> int: ...


class GenesysUsbProtocol:
    """Register and bulk helpers for Genesys GL845 (8200i)."""

    def __init__(self, transport: UsbTransport) -> None:
        self._transport = transport

    @staticmethod
    def check_link_status(status_byte: int) -> None:
        """SANE genesys validates USB link with ``0x55`` on GL845-class reads."""
        from pyopticfilm.exceptions import UsbError

        if status_byte != REGISTER_LINK_OK:
            raise UsbError(
                f"Invalid USB link status 0x{status_byte:02x} (expected 0x55); "
                "scanner unplugged or driver binding wrong?"
            )

    def read_register(self, address: int) -> int:
        """Read one ASIC register (GL845/846/847/GL124 control-msg path)."""
        address = int(address) & 0xFFFF
        # SANE: address16 = 0x22 + (address << 8) as uint16
        address16 = (0x22 + (address << 8)) & 0xFFFF
        usb_value = VALUE_GET_REGISTER
        if address > 0xFF:
            usb_value |= 0x100

        raw = self._transport.control_msg(
            REQUEST_TYPE_IN,
            REQUEST_BUFFER,
            usb_value,
            address16,
            2,
        )
        if len(raw) != 2:
            from pyopticfilm.exceptions import UsbError

            raise UsbError(f"Register read returned {len(raw)} bytes, expected 2")

        value, status = raw[0], raw[1]
        self.check_link_status(status)
        logger.debug("read_register(0x%04x) -> 0x%02x", address, value)
        return value

    def write_register(self, address: int, value: int) -> None:
        """Write one ASIC register (GL845/846/847/GL124 control-msg path)."""
        address = int(address) & 0xFFFF
        value = int(value) & 0xFF
        payload = bytes((address & 0xFF, value))
        usb_value = VALUE_SET_REGISTER
        if address > 0xFF:
            usb_value |= 0x100

        self._transport.control_msg(
            REQUEST_TYPE_OUT,
            REQUEST_BUFFER,
            usb_value,
            INDEX,
            payload,
        )
        logger.debug("write_register(0x%04x, 0x%02x)", address, value)

    def write_registers(self, pairs: list[tuple[int, int]]) -> None:
        """Write many registers (GL845 uses per-register writes)."""
        for address, value in pairs:
            self.write_register(address, value)

    def write_registers_batched(
        self, pairs: list[tuple[int, int]], *, max_pairs: int = 32
    ) -> None:
        """Write many registers, packing ``(addr, value)`` pairs per transfer.

        The Windows driver for the 8200i SE sends up to 32 pairs in a single
        ``VALUE_SET_REGISTER`` transfer, which is what makes its 116-register
        boot blast fast. Addresses above ``0xFF`` need the high-address flag in
        ``wValue``, so they cannot share a transfer with low addresses and are
        emitted individually.
        """
        if max_pairs < 1:
            raise ValueError("max_pairs must be >= 1")

        batch: list[tuple[int, int]] = []

        def flush() -> None:
            if not batch:
                return
            if len(batch) == 1:
                self.write_register(*batch[0])
            else:
                payload = bytes(b for a, v in batch for b in (a & 0xFF, v & 0xFF))
                self._transport.control_msg(
                    REQUEST_TYPE_OUT,
                    REQUEST_BUFFER,
                    VALUE_SET_REGISTER,
                    INDEX,
                    payload,
                )
                logger.debug("write_registers_batched: %d pairs", len(batch))
            batch.clear()

        for address, value in pairs:
            if int(address) > 0xFF:
                flush()
                self.write_register(address, value)
                continue
            batch.append((int(address), int(value)))
            if len(batch) >= max_pairs:
                flush()
        flush()

    def write_0x8c(self, index: int, value: int) -> None:
        """SANE ``write_0x8c`` — VALUE_BUF_ENDACCESS vendor request."""
        payload = bytes((int(value) & 0xFF,))
        self._transport.control_msg(
            REQUEST_TYPE_OUT,
            REQUEST_REGISTER,
            VALUE_BUF_ENDACCESS,
            int(index) & 0xFF,
            payload,
        )

    def write_vendor(self, value: int, index: int, data: bytes | bytearray) -> None:
        """Vendor control OUT with ``wValue``/``wIndex`` and ``data``.

        ``bRequest`` follows the Windows ``usbscan.sys`` rule the vendor
        drivers rely on: ``REQUEST_BUFFER`` (0x04) for transfers longer than
        one byte, ``REQUEST_REGISTER`` (0x0C) for a single byte.
        """
        payload = bytes(data)
        request = REQUEST_BUFFER if len(payload) > 1 else REQUEST_REGISTER
        self._transport.control_msg(
            REQUEST_TYPE_OUT, request, int(value) & 0xFFFF, int(index) & 0xFFFF, payload
        )

    def read_vendor(self, value: int, index: int, length: int) -> bytes:
        """Vendor control IN of ``length`` bytes (same ``bRequest`` rule as above)."""
        request = REQUEST_BUFFER if int(length) > 1 else REQUEST_REGISTER
        return bytes(
            self._transport.control_msg(
                REQUEST_TYPE_IN, request, int(value) & 0xFFFF, int(index) & 0xFFFF, int(length)
            )
        )

    def read_usb_speed_byte(self) -> int:
        """Cold-init probe used by SANE ``sanei_genesys_asic_init`` (REQUEST_REGISTER)."""
        return self.read_request_register(0x00)

    def read_request_register(self, index: int) -> int:
        """1-byte ``REQUEST_REGISTER`` / ``VALUE_GET_REGISTER`` probe.

        The SE vendor driver polls several ``wIndex`` slots this way. Index
        ``0x00`` is the SANE USB-speed / warm-boot probe; index ``0x21`` is
        polled during fast feeds until it returns ``0x04`` (session 03).
        """
        raw = self._transport.control_msg(
            REQUEST_TYPE_IN,
            REQUEST_REGISTER,
            VALUE_GET_REGISTER,
            int(index) & 0xFF,
            1,
        )
        if not raw:
            from pyopticfilm.exceptions import UsbError

            raise UsbError(f"REQUEST_REGISTER probe index=0x{index:02x} returned no data")
        return raw[0]

    def write_fe_register(self, address: int, value: int) -> None:
        """Write analog frontend register (GL845 non-GL124 path)."""
        address = int(address) & 0xFF
        value = int(value) & 0xFFFF
        self.write_register(0x51, address)
        self.write_register(0x3A, (value >> 8) & 0xFF)
        self.write_register(0x3B, value & 0xFF)

    def write_fe_register_gl124(self, address: int, value: int) -> None:
        """Write analog frontend register (GL124 path, used by GL128 OpticFilm).

        The GL128 AFE is 16-bit and reached through ``0x5D`` (high) / ``0x5E``
        (low) rather than GL845's ``0x3A`` / ``0x3B`` — confirmed by every
        frontend write in the SE captures following a ``0x51`` index write.
        """
        address = int(address) & 0xFF
        value = int(value) & 0xFFFF
        self.write_register(0x51, address)
        self.write_register(0x5D, (value >> 8) & 0xFF)
        self.write_register(0x5E, value & 0xFF)

    def read_fe_register(self, address: int) -> int:
        """Read analog frontend register."""
        self.write_register(0x50, int(address) & 0xFF)
        high = self.read_register(0x46)
        low = self.read_register(0x47)
        return ((high & 0xFF) << 8) | (low & 0xFF)

    def write_u16(self, address: int, value: int) -> None:
        """Write a 16-bit big-endian value starting at ``address``."""
        value = int(value) & 0xFFFF
        self.write_register(address, (value >> 8) & 0xFF)
        self.write_register(address + 1, value & 0xFF)

    def write_u24(self, address: int, value: int) -> None:
        """Write a 24-bit big-endian value starting at ``address``."""
        value = int(value) & 0xFFFFFF
        self.write_register(address, (value >> 16) & 0xFF)
        self.write_register(address + 1, (value >> 8) & 0xFF)
        self.write_register(address + 2, value & 0xFF)

    def read_intr(self, timeout_ms: int | None = None) -> bytes:
        return self._transport.read_intr(timeout_ms=timeout_ms)

    @staticmethod
    def _bulk_preamble(addr: int, size: int) -> bytes:
        """8-byte little-endian ``(address, size)`` header used by every bulk op."""
        return bytes(
            (
                addr & 0xFF,
                (addr >> 8) & 0xFF,
                (addr >> 16) & 0xFF,
                (addr >> 24) & 0xFF,
                size & 0xFF,
                (size >> 8) & 0xFF,
                (size >> 16) & 0xFF,
                (size >> 24) & 0xFF,
            )
        )

    def _bulk_read_send_header(self, size: int) -> None:
        """GL845/846/847/GL124 bulk-read preamble (fixed 0x10000000 address)."""
        # hard coded 0x10000000 address (SANE scanner_interface_usb.cpp)
        self._transport.control_msg(
            REQUEST_TYPE_OUT,
            REQUEST_BUFFER,
            VALUE_BUFFER,
            0x00,
            self._bulk_preamble(0x10000000, size),
        )

    def bulk_read_begin(
        self, size: int, *, index: int = BULK_IN, addr: int = 0x10000000
    ) -> None:
        """Announce a single bulk transfer of ``size`` bytes and stop.

        The GL128 path sends one preamble for a whole image (tens or hundreds of
        megabytes) and then streams it, instead of GL845's header-per-chunk. It
        also selects the source with ``wIndex``: ``0x00`` reads scanner RAM
        (used for shading/calibration passes) and ``0x08`` reads the live image
        stream. Pair this with :meth:`bulk_read_chunk`.
        """
        self._transport.control_msg(
            REQUEST_TYPE_OUT,
            REQUEST_BUFFER,
            VALUE_BUFFER,
            int(index) & 0xFF,
            self._bulk_preamble(int(addr), int(size)),
        )
        logger.debug(
            "bulk_read_begin size=%d index=0x%02x addr=0x%08x", size, index, addr
        )

    def bulk_read_chunk(self, size: int) -> bytes:
        """Read one chunk of an in-flight transfer started by ``bulk_read_begin``."""
        if size <= 0:
            return b""
        data = self._transport.bulk_read(min(size, BULK_MAX_SIZE))
        if not data:
            from pyopticfilm.exceptions import UsbError

            raise UsbError("bulk_read returned empty data mid-transfer")
        return data

    def bulk_read_exact(self, size: int, *, max_transfer: int = BULK_MAX_SIZE) -> bytes:
        """Read exactly ``size`` bytes from an in-flight :meth:`bulk_read_begin`.

        Each bulk IN request asks for at most ``max_transfer`` bytes.
        """
        if size <= 0:
            return b""
        step = max(1, int(max_transfer))
        buf = bytearray()
        while len(buf) < size:
            data = self._transport.bulk_read(min(size - len(buf), step))
            if not data:
                from pyopticfilm.exceptions import UsbError

                raise UsbError(
                    f"bulk_read returned empty after {len(buf)} of {size} bytes"
                )
            buf.extend(data)
        return bytes(buf)

    def abort_bulk_stream(self) -> int:
        """Abandon an in-flight :meth:`bulk_read_begin` stream (cancel / error).

        Drains residual bulk IN and clears endpoint halt when the transport
        supports it. Returns bytes drained, or 0 if unsupported.
        """
        abort = getattr(self._transport, "abort_bulk_in", None)
        if not callable(abort):
            logger.debug("abort_bulk_stream: transport has no abort_bulk_in")
            return 0
        drained = int(abort())
        logger.info("abort_bulk_stream drained=%d", drained)
        return drained

    def bulk_read_data(self, size: int) -> bytes:
        """Read ``size`` bytes via GL845 bulk path (header before each chunk)."""
        if size <= 0:
            return b""

        remaining = size
        chunks: list[bytes] = []
        while remaining > 0:
            block = min(remaining, BULK_MAX_SIZE)
            self._bulk_read_send_header(block)
            data = self._transport.bulk_read(block)
            if not data:
                from pyopticfilm.exceptions import UsbError

                raise UsbError("bulk_read returned empty data mid-transfer")
            chunks.append(data)
            remaining -= len(data)
            logger.debug(
                "bulk_read_data: got %d bytes, %d remaining", len(data), remaining
            )
        return b"".join(chunks)

    def write_ahb(self, addr: int, data: bytes | bytearray) -> None:
        """Write an AHB memory window (GL845/846/847/GL124 table upload path)."""
        payload = bytes(data)
        size = len(payload)
        outdata = bytearray(8)
        outdata[0] = addr & 0xFF
        outdata[1] = (addr >> 8) & 0xFF
        outdata[2] = (addr >> 16) & 0xFF
        outdata[3] = (addr >> 24) & 0xFF
        outdata[4] = size & 0xFF
        outdata[5] = (size >> 8) & 0xFF
        outdata[6] = (size >> 16) & 0xFF
        outdata[7] = (size >> 24) & 0xFF

        self._transport.control_msg(
            REQUEST_TYPE_OUT,
            REQUEST_BUFFER,
            VALUE_BUFFER,
            0x01,
            bytes(outdata),
        )

        offset = 0
        while offset < size:
            block = min(size - offset, BULK_MAX_SIZE)
            written = self._transport.bulk_write(payload[offset : offset + block])
            if written <= 0:
                from pyopticfilm.exceptions import UsbError

                raise UsbError("bulk_write wrote 0 bytes mid-AHB transfer")
            offset += written
