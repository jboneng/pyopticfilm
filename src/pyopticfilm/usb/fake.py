# SPDX-License-Identifier: GPL-3.0-or-later
"""Fake USB transports for hardwareless scans and protocol tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from pyopticfilm.asic.registers import Gl128Registers, Gl845Registers
from pyopticfilm.device.protocol import FilmModel
from pyopticfilm.exceptions import UsbError
from pyopticfilm.usb.device import VID_PLUSTEK, UsbDeviceInfo
from pyopticfilm.usb.protocol import (
    REGISTER_LINK_OK,
    REQUEST_BUFFER,
    REQUEST_REGISTER,
    REQUEST_TYPE_IN,
    REQUEST_TYPE_OUT,
    VALUE_BUF_ENDACCESS,
    VALUE_BUFFER,
    VALUE_GET_REGISTER,
    VALUE_SET_REGISTER,
)
from pyopticfilm.usb.trace import UsbTransaction

_R = Gl845Registers()
_SE = Gl128Registers()

# Idle: at home, buffer empty, frontend ready, powered (not replugged).
IDLE_STATUS_REG41 = _R.STATUS_BUFEMPTY | _R.STATUS_HOMESNR | _R.STATUS_PWRBIT


def _gl845_read_address(value: int, index: int) -> int:
    """Recover the ASIC address from a GL845 GET_REGISTER control transfer."""
    addr = (int(index) >> 8) & 0xFF
    if int(value) & 0x100:
        addr |= 0x100
    return addr


@dataclass
class FakeDeviceHandle:
    """Stand-in for :class:`~pyopticfilm.usb.device.UsbDeviceHandle` (always open)."""

    info: UsbDeviceInfo
    timeout_ms: int = 5000

    @property
    def is_open(self) -> bool:
        return True

    def close(self) -> None:
        return None

    @classmethod
    def for_model(cls, model: FilmModel) -> FakeDeviceHandle:
        pid = int(getattr(model, "usb_product_id", 0) or 0)
        vid = int(getattr(model, "usb_vendor_id", VID_PLUSTEK) or VID_PLUSTEK)
        return cls(
            UsbDeviceInfo(
                vendor_id=vid,
                product_id=pid,
                bus=0,
                address=0,
                product=getattr(model, "model", None),
                manufacturer=getattr(model, "vendor", None),
            )
        )


@dataclass
class FakeUsbTransport:
    """Scripted USB device that records every transfer in order.

    Register reads use the GL845 vendor-request framing. Status register ``0x41``
    defaults to an idle scanner (at home, buffer empty, frontend ready) so
    ASIC poll loops exit on the first read.
    """

    registers: dict[int, int] = field(default_factory=dict)
    transactions: list[UsbTransaction] = field(default_factory=list)
    bulk_in_queue: list[bytes] = field(default_factory=list)
    request_register: dict[int, int] = field(default_factory=dict)
    errors: list[BaseException] = field(default_factory=list)
    aborted: int = 0
    abort_drain_bytes: int = 42

    def __post_init__(self) -> None:
        self.registers.setdefault(_R.REG_0x41, IDLE_STATUS_REG41)
        self.registers.setdefault(_SE.REG_STATUS, IDLE_STATUS_REG41)

    def control_msg(
        self,
        request_type: int,
        request: int,
        value: int,
        index: int,
        data_or_length: int | bytes | bytearray,
        *,
        timeout_ms: int | None = None,
    ) -> bytes:
        self._raise_queued_error()
        is_read = isinstance(data_or_length, int)
        if is_read:
            txn = UsbTransaction(
                operation="control_read",
                request_type=int(request_type),
                request=int(request),
                value=int(value),
                index=int(index),
                length=int(data_or_length),
                timeout_ms=timeout_ms,
            )
        else:
            txn = UsbTransaction(
                operation="control_write",
                request_type=int(request_type),
                request=int(request),
                value=int(value),
                index=int(index),
                data=bytes(data_or_length),
                timeout_ms=timeout_ms,
            )
        self.transactions.append(txn)
        return self._handle_control(txn)

    def bulk_read(self, size: int, *, timeout_ms: int | None = None) -> bytes:
        self._raise_queued_error()
        self.transactions.append(
            UsbTransaction(
                operation="bulk_read",
                endpoint=0x81,
                length=int(size),
                timeout_ms=timeout_ms,
            )
        )
        if self.bulk_in_queue:
            chunk = self.bulk_in_queue.pop(0)
            return chunk[:size]
        return self._bulk_in_bytes(int(size))

    def bulk_write(self, data: bytes | bytearray, *, timeout_ms: int | None = None) -> int:
        self._raise_queued_error()
        payload = bytes(data)
        self.transactions.append(
            UsbTransaction(
                operation="bulk_write",
                endpoint=0x02,
                data=payload,
                timeout_ms=timeout_ms,
            )
        )
        return len(payload)

    def abort_bulk_in(self) -> int:
        self.aborted += 1
        return int(self.abort_drain_bytes)

    def queue_error(self, exc: BaseException) -> None:
        """Raise ``exc`` on the next control or bulk transfer."""
        self.errors.append(exc)

    def _bulk_in_bytes(self, size: int) -> bytes:
        return b"\x00" * size

    @property
    def control_log(self) -> list[tuple]:
        """Legacy 5-tuples: ``(type, request, value, index, data_or_length)``."""
        out: list[tuple] = []
        for txn in self.transactions:
            if txn.operation == "control_read":
                out.append(
                    (txn.request_type, txn.request, txn.value, txn.index, txn.length)
                )
            elif txn.operation == "control_write":
                out.append(
                    (txn.request_type, txn.request, txn.value, txn.index, txn.data)
                )
        return out

    @property
    def bulk_out_log(self) -> list[bytes]:
        return [txn.data or b"" for txn in self.transactions if txn.operation == "bulk_write"]

    def _raise_queued_error(self) -> None:
        if self.errors:
            raise self.errors.pop(0)

    def _handle_control(self, txn: UsbTransaction) -> bytes:
        rtype = int(txn.request_type or 0)
        request = int(txn.request or 0)
        value = int(txn.value or 0)
        index = int(txn.index or 0)

        if rtype == REQUEST_TYPE_IN and request == REQUEST_BUFFER:
            if (value & 0xFF) != VALUE_GET_REGISTER:
                raise UsbError(
                    f"unexpected GET_REGISTER value=0x{value:04x} index=0x{index:04x}"
                )
            addr = _gl845_read_address(value, index)
            self._before_register_read(addr)
            reg_val = self.registers.get(addr, 0x00) & 0xFF
            return bytes((reg_val, REGISTER_LINK_OK))

        if rtype == REQUEST_TYPE_IN and request == REQUEST_REGISTER:
            self._before_probe_read(index)
            return bytes((self.request_register.get(index, 0x00) & 0xFF,))

        if rtype == REQUEST_TYPE_OUT and request == REQUEST_BUFFER:
            if (value & 0xFF) == VALUE_SET_REGISTER:
                payload = txn.data or b""
                high = 0x100 if value & 0x100 else 0
                if len(payload) < 2 or len(payload) % 2:
                    raise UsbError(f"SET_REGISTER payload length {len(payload)}")
                for i in range(0, len(payload), 2):
                    addr = high | payload[i]
                    self.registers[addr] = payload[i + 1]
                    self._after_register_write(addr, payload[i + 1])
                return b""
            if value == VALUE_BUFFER:
                return b""
            raise UsbError(f"unexpected BUFFER out value=0x{value:04x}")

        if (
            rtype == REQUEST_TYPE_OUT
            and request == REQUEST_REGISTER
            and value == VALUE_BUF_ENDACCESS
        ):
            return b""

        raise UsbError(
            f"unexpected control_msg type=0x{rtype:02x} req=0x{request:02x} "
            f"val=0x{value:04x} idx=0x{index:04x}"
        )

    def _before_register_read(self, addr: int) -> None:
        del addr

    def _before_probe_read(self, index: int) -> None:
        del index

    def _after_register_write(self, addr: int, value: int) -> None:
        del addr, value


@dataclass
class MockScannerTransport(FakeUsbTransport):
    """Fake scanner that can complete a full :meth:`ScanSession.run`.

    Status / feed / valid-word registers are updated so GL845 and GL128 poll
    loops exit. ``bulk_read`` returns a deterministic XY/channel pattern.
    """

    at_home: bool = True
    buffer_empty: bool = True
    motor_enabled: bool = False
    feed_finished: bool = False
    _motion_reads: int = 0
    _bulk_offset: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        self._sync_status()
        # GL845 feed/valid-word counters: always "done" / data present.
        self.registers.setdefault(0x48, 0x1F)
        self.registers.setdefault(0x49, 0xFF)
        self.registers.setdefault(0x4A, 0xFF)
        self.registers.setdefault(0x42, 0x02)
        self.registers.setdefault(0x43, 0x00)
        self.registers.setdefault(0x44, 0x00)
        self.registers.setdefault(0x45, 0x01)
        self.registers.setdefault(0x06, _R.PWRBIT)

    def _status_byte(self) -> int:
        v = _R.STATUS_PWRBIT
        if self.registers.get(0x03, 0) & _R.LAMPPWR:
            v |= _R.STATUS_LAMPSTS
        if self.buffer_empty:
            v |= _R.STATUS_BUFEMPTY
        if self.at_home:
            v |= _R.STATUS_HOMESNR
        if self.feed_finished:
            v |= _R.STATUS_FEEDFSH
        if self.motor_enabled:
            v |= _R.STATUS_MOTORENB
        return v & 0xFF

    def _sync_status(self) -> None:
        raw = self._status_byte()
        self.registers[_R.REG_0x41] = raw
        self.registers[_SE.REG_STATUS] = raw
        motor_flags = _R.DATAENB if not self.buffer_empty else 0
        if self.motor_enabled:
            motor_flags |= _R.MOTMFLG
        self.registers[0x40] = motor_flags

    def _before_register_read(self, addr: int) -> None:
        if addr in (_R.REG_0x41, _SE.REG_STATUS):
            self._tick_motion()
            self._sync_status()

    def _before_probe_read(self, index: int) -> None:
        if index == 0x21:
            self._tick_motion()

    def _tick_motion(self) -> None:
        if not self.motor_enabled:
            return
        self._motion_reads += 1
        # First status sample shows motor on (GL128 require_motion); then finish.
        if self._motion_reads >= 4:
            self.motor_enabled = False
            self.feed_finished = True
            if self.registers.get(0x02, 0) & _R.AGOHOME:
                self.at_home = True
            self.request_register[0x21] = 0x04
            self._sync_status()

    def _after_register_write(self, addr: int, value: int) -> None:
        value &= 0xFF
        if addr == 0x03:
            self._sync_status()
            return
        if addr == 0x01:
            scanning = bool(value & _R.SCAN)
            self.buffer_empty = not scanning
            if not scanning and (self.registers.get(0x02, 0) & _R.AGOHOME):
                self.at_home = True
                self.motor_enabled = False
            self._sync_status()
            return
        if addr == 0x0F and value == 0x01:
            self.at_home = False
            self.motor_enabled = True
            self.feed_finished = False
            self._motion_reads = 0
            self.request_register[0x21] = 0x00
            self._sync_status()
            return
        if addr == 0x02 and not (value & _R.MTRPWR):
            self.motor_enabled = False
            self._sync_status()

    def _line_width(self) -> int:
        str_px = (
            ((self.registers.get(0x82, 0) & 0xFF) << 16)
            | ((self.registers.get(0x83, 0) & 0xFF) << 8)
            | (self.registers.get(0x84, 0) & 0xFF)
        )
        end_px = (
            ((self.registers.get(0x85, 0) & 0xFF) << 16)
            | ((self.registers.get(0x86, 0) & 0xFF) << 8)
            | (self.registers.get(0x87, 0) & 0xFF)
        )
        if end_px > str_px:
            return max(1, end_px - str_px)
        start = ((self.registers.get(0x30, 0) & 0xFF) << 8) | (self.registers.get(0x31, 0) & 0xFF)
        end = ((self.registers.get(0x32, 0) & 0xFF) << 8) | (self.registers.get(0x33, 0) & 0xFF)
        if end > start:
            return max(1, end - start)
        return 256

    def _bulk_in_bytes(self, size: int) -> bytes:
        """Chunky RGB16 LE: R=x, G=y, B=x XOR y (visible layout/channel bugs).

        Values scale with LPERIOD (0x38) relative to 14000 so exposure brackets differ,
        and drop to near-black with neither the lamp nor the IR LED on.
        """
        width = self._line_width()
        lperiod = ((self.registers.get(0x38, 0) & 0xFF) << 8) | (self.registers.get(0x39, 0) & 0xFF)
        gain_num, gain_den = (lperiod, 14000) if lperiod else (1, 1)
        lit = self.registers.get(0x03, 0) & _R.LAMPPWR or self.registers.get(0xA8, 0) & _R.IR_LAMP_A8_MASK
        if not lit:
            gain_den *= 64  # dark frame: no light source on
        out = bytearray(size)
        off = self._bulk_offset
        for i in range(0, size - 1, 2):
            sample = (off + i) // 2
            pixel = sample // 3
            channel = sample % 3
            x = pixel % width
            y = pixel // width
            if channel == 0:
                val = x & 0xFFFF
            elif channel == 1:
                val = y & 0xFFFF
            else:
                val = (x ^ y) & 0xFFFF
            val = min(0xFFFF, val * gain_num // gain_den)
            out[i] = val & 0xFF
            out[i + 1] = (val >> 8) & 0xFF
        self._bulk_offset += size
        return bytes(out)
