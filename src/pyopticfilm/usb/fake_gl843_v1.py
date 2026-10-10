# SPDX-License-Identifier: GPL-3.0-or-later
"""USB fakes for the OpticFilm 7600i v1: strict capture playback, and a simulated GL843."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from pyopticfilm.usb.trace import UsbTransaction

STATUS = 0x41
PWRBIT = 0x80
BUFEMPTY = 0x40
FEEDFSH = 0x20
HOME = 0x08
MOTORENB = 0x01


def _out(rt: int, req: int, value: int, index: int, data: bytes) -> UsbTransaction:
    return UsbTransaction(operation="control_write", request_type=rt, request=req, value=value, index=index,
                          data=bytes(data))


def _in(rt: int, req: int, value: int, index: int, length: int, reply: bytes) -> UsbTransaction:
    return UsbTransaction(operation="control_read", request_type=rt, request=req, value=value, index=index,
                          length=length, response=bytes(reply))


class PlaybackMismatch(AssertionError):
    pass


class CapturePlaybackTransport:
    """Every transfer must match the next vendor op; reads return what the scanner returned."""

    def __init__(self, ops: list[dict]) -> None:
        self.ops = [o for o in ops if o["kind"] != "delay"]
        self.pos = 0
        self.read_left: int | None = None
        self.transactions: list[UsbTransaction] = []

    def _next(self, what: str) -> dict:
        if self.pos >= len(self.ops):
            raise PlaybackMismatch(f"{what} after the end of the vendor sequence")
        return self.ops[self.pos]

    def control_msg(self, request_type, request, value, index, data_or_length, *, timeout_ms=None) -> bytes:
        op = self._next(f"control 0x{value:02x}")
        where = f"vendor op {self.pos}"
        if op["kind"] != "control":
            raise PlaybackMismatch(f"{where}: expected {op['kind']}, got control 0x{value:02x}")
        if (op["rt"], op["request"], op["value"], op["index"]) != (request_type, request, value, index):
            raise PlaybackMismatch(
                f"{where}: expected rt=0x{op['rt']:02x} req=0x{op['request']:02x} value=0x{op['value']:02x} "
                f"index=0x{op['index']:02x}, got rt=0x{request_type:02x} req=0x{request:02x} "
                f"value=0x{value:02x} index=0x{index:02x}"
            )
        self.pos += 1
        if request_type == 0x40:
            payload = list(bytes(data_or_length))
            if payload != list(op["data"]):
                raise PlaybackMismatch(f"{where}: payload {payload[:16]} != vendor {op['data'][:16]}")
            self.transactions.append(_out(request_type, request, value, index, bytes(payload)))
            return b""
        if int(data_or_length) != op["length"]:
            raise PlaybackMismatch(f"{where}: read length {data_or_length} != vendor {op['length']}")
        reply = bytes(op["expected"])
        self.transactions.append(_in(request_type, request, value, index, len(reply), reply))
        return reply

    def bulk_write(self, data, *, timeout_ms=None) -> int:
        op = self._next("bulk write")
        if op["kind"] != "write" or base64.b64decode(op["data"]) != bytes(data):
            raise PlaybackMismatch(f"vendor op {self.pos}: unexpected bulk write of {len(data)} bytes")
        self.pos += 1
        self.transactions.append(UsbTransaction(operation="bulk_write", data=bytes(data), length=len(data)))
        return len(data)

    def bulk_read(self, size: int, *, timeout_ms=None) -> bytes:
        op = self._next("bulk read")
        if op["kind"] != "read":
            raise PlaybackMismatch(f"vendor op {self.pos}: expected {op['kind']}, got bulk read")
        if self.read_left is None:
            self.read_left = op["length"]
        n = min(size, self.read_left)
        self.read_left -= n
        if self.read_left == 0:
            self.pos += 1
            self.read_left = None
        self.transactions.append(UsbTransaction(operation="bulk_read", length=n))
        return bytes(n)

    @property
    def finished(self) -> bool:
        return self.pos == len(self.ops) and self.read_left is None


@dataclass
class SimulatedGl843V1Transport:
    """Behavioural GL843 v1: enough for boot, homing and replayed scans without hardware."""

    #: carriage position in motor steps from the home sensor (sensor on at <= 0)
    position: int = 6000
    #: motor steps per status read while moving
    steps_per_poll: int = 2500
    #: RGB16 sample values returned with a light source on (white LED or infrared LED) and off
    lit_rgb: tuple[int, int, int] = (30000, 40000, 34000)
    dark_rgb: tuple[int, int, int] = (1000, 1070, 1230)
    regs: dict[int, int] = field(default_factory=dict)
    afe: dict[int, int] = field(default_factory=dict)
    transactions: list[UsbTransaction] = field(default_factory=list)
    address: int = 0
    moving: bool = False
    reverse: bool = False
    steps_left: int = 0
    feed_finished: bool = False
    scan_move: bool = False
    read_phase: int = 0
    aborts: int = 0
    is_open: bool = True

    def _status(self) -> int:
        s = PWRBIT
        scanning = bool(self.regs.get(0x01, 0) & 1)
        if not scanning:
            s |= BUFEMPTY
        if self.feed_finished:
            s |= FEEDFSH
        if self.position <= 0:
            s |= HOME
        if self.moving:
            s |= MOTORENB
        return s

    def _tick(self) -> None:
        if not self.moving:
            return
        step = min(self.steps_per_poll, self.steps_left)
        self.position += -step if self.reverse else step
        self.position = max(self.position, -200)  # mechanical end stop
        self.steps_left -= step
        if self.steps_left <= 0:
            self.moving = False
            self.feed_finished = True
            if self.scan_move and self.regs.get(0x02, 0) & 0x20 and self.position > 0:
                # AGOHOME: after a scan the GL843 drives the carriage back to the home sensor
                self.scan_move = False
                self.moving, self.reverse, self.steps_left = True, True, self.position

    def _write(self, reg: int, value: int) -> None:
        self.regs[reg] = value
        if reg == 0x3B:
            self.afe[self.regs.get(0x51, 0)] = self.regs.get(0x3A, 0) * 256 + value
        feedl = (self.regs.get(0x3D, 0) << 16) | (self.regs.get(0x3E, 0) << 8) | self.regs.get(0x3F, 0)
        if reg == 0x0F and value == 1:
            self.moving = True
            self.feed_finished = False
            self.reverse = bool(self.regs.get(0x02, 0) & 0x04)
            self.steps_left = max(1, feedl)
            self.scan_move = bool(self.regs.get(0x01, 0) & 1)
        elif reg in (0x3D, 0x3E, 0x3F) and self.moving and feedl == 1:
            self.moving = False  # the vendor's stop: 0x02=0x08 then FEEDL=1 while moving
            self.feed_finished = True

    def control_msg(self, request_type, request, value, index, data_or_length, *, timeout_ms=None) -> bytes:
        if request_type == 0x40:
            payload = bytes(data_or_length)
            self.transactions.append(_out(request_type, request, value, index, payload))
            if value == 0x83:
                if len(payload) == 1:
                    self.address = payload[0]
                else:
                    for i in range(0, len(payload) - 1, 2):
                        self._write(payload[i], payload[i + 1])
            return b""
        n = int(data_or_length)
        if value == 0x8E:
            reply = bytes([1 if index == 0x20 else 0] * n)
        elif value == 0x84:
            if self.address == STATUS:
                self._tick()
                reply = bytes([self._status()])
            else:
                reply = bytes([self.regs.get(self.address, 0)])
            self.address = (self.address + 1) & 0xFF
        else:
            reply = bytes(n)
        self.transactions.append(_in(request_type, request, value, index, len(reply), reply))
        return reply

    def bulk_write(self, data, *, timeout_ms=None) -> int:
        self.transactions.append(UsbTransaction(operation="bulk_write", data=bytes(data), length=len(data)))
        return len(data)

    def bulk_read(self, size: int, *, timeout_ms=None) -> bytes:
        self.transactions.append(UsbTransaction(operation="bulk_read", length=size))
        lit = bool(self.regs.get(0x03, 0) & 0x10) or self.regs.get(0xA8) == 0x27
        r, g, b = self.lit_rgb if lit else self.dark_rgb
        start_pixel = self.regs.get(0x30, 0) * 256 + self.regs.get(0x31, 0)
        end_pixel = self.regs.get(0x32, 0) * 256 + self.regs.get(0x33, 0)
        dpi = self.regs.get(0x2C, 0) * 256 + self.regs.get(0x2D, 0)
        if dpi and (end_pixel - start_pixel) * dpi // 1200 == 512:
            values = []
            for c in range(3):
                offset = self.afe.get(5 + c, 128)
                offset = -(offset & 255) if offset & 256 else offset
                gain = 6 / (6 - 5 * self.afe.get(2 + c, 0) / 63)
                values.append(max(0, min(65535, int((offset + 70) * 20 * gain))))
            r, g, b = values
        pixel = bytes((r & 0xFF, r >> 8, g & 0xFF, g >> 8, b & 0xFF, b >> 8))
        start = self.read_phase % 6
        self.read_phase += size
        return (pixel * (size // 6 + 2))[start : start + size]

    def abort_bulk_in(self) -> int:
        self.aborts += 1
        return 0

    def close(self) -> None:
        self.is_open = False
