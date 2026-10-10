# SPDX-License-Identifier: GPL-3.0-or-later
"""GL843 chip operations for OpticFilm 7600i v1 (bcdDevice 4.00), from SilverFast captures.

Register reads set the address (``0x83``) then read ``0x84``; every write is acknowledged by
polling ``0x8E``/``0x20``.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Sequence

from pyopticfilm.asic.status import ScannerStatus
from pyopticfilm.device.protocol import ScanMethod
from pyopticfilm.exceptions import AsicError, MotorTimeoutError, UsbError
from pyopticfilm.logging import get_logger
from pyopticfilm.usb.protocol import GenesysUsbProtocol, UsbTransport

logger = get_logger(__name__)

REG_STATUS = 0x41
STATUS_HOME = 0x08
STATUS_MOTORENB = 0x01
STATUS_FEEDFSH = 0x20
LAMP_BIT = 0x10  # 0x03: white LED
IR_GPIO_ON = 0x27  # 0xA8: GPIO27 drives the infrared LED
IR_GPIO_OFF = 0x20
MTRREV = 0x04  # 0x02: reverse
BULK_PACKET = 512


class Gl843V1Usb:
    """GL843 register, table and raw transfer helpers over a :class:`UsbTransport`."""

    def __init__(self, transport: UsbTransport) -> None:
        self.transport = transport
        self.shadow: dict[int, int] = {}

    def ctl_out(self, value: int, index: int, data: Sequence[int]) -> None:
        payload = bytes(int(b) & 0xFF for b in data)
        request = 0x04 if len(payload) > 1 else 0x0C
        error: Exception | None = None
        for _attempt in range(10):
            try:
                self.transport.control_msg(0x40, request, value, index, payload)
            except UsbError as exc:  # a stalled write is sent again
                error = exc
                continue
            for _poll in range(10):
                if self.ctl_in(0x8E, 0x20, 1)[0] & 0x01:
                    if value == 0x83 and len(payload) > 1:
                        for i in range(0, len(payload) - 1, 2):
                            self.shadow[payload[i]] = payload[i + 1]
                    return
        raise UsbError(f"control write 0x{value:02x} not acknowledged: {error}")

    def ctl_in(self, value: int, index: int, length: int) -> bytes:
        data = self.transport.control_msg(0xC0, 0x04 if length > 1 else 0x0C, value, index, length)
        if len(data) != length:
            raise UsbError(f"control read 0x{value:02x}/0x{index:02x} returned {len(data)} bytes")
        return bytes(data)

    def write_register(self, address: int, value: int) -> None:
        self.ctl_out(0x83, 0, [address, value])

    def write_registers(self, pairs: Sequence[tuple[int, int]]) -> None:
        for i in range(0, len(pairs), 32):
            self.ctl_out(0x83, 0, [b for pair in pairs[i : i + 32] for b in pair])

    def read_register(self, address: int) -> int:
        self.ctl_out(0x83, 0, [address])
        return self.ctl_in(0x84, 0, 1)[0]

    def status(self) -> int:
        return self.read_register(REG_STATUS)

    def write_afe(self, addr: int, value: int) -> None:
        self.ctl_out(0x83, 0, [0x51, addr, 0x3A, (value >> 8) & 0xFF, 0x3B, value & 0xFF])

    def upload_table(self, slot: int, table: Sequence[int]) -> None:
        """Motor table into RAM slot ``slot``."""
        addr = 0x40000 + 0x8000 * slot
        data = b"".join(bytes((v & 0xFF, v >> 8)) for v in table)
        self.write_register(0x5B, (addr >> 12) & 0xFF)
        self.write_register(0x5C, (addr >> 4) & 0xFF)
        self.ctl_out(0x83, 0, [0x28])
        n = len(data)
        self.ctl_out(0x82, 0, [1, 0, 0, 0, n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF, 0])
        pad = data + bytes(-n % BULK_PACKET)
        if self.transport.bulk_write(pad) != len(pad):
            raise UsbError("short motor table upload")
        for _ in range(256):
            if not self.ctl_in(0x8E, 0x18, 1)[0] & 0x0C:
                break
        self.write_register(0x5B, ((addr >> 12) & 0xFF) & ~0x40)


def recorded_motion(profile: dict) -> dict:
    """Fast/slow slot-3 tables and the motor registers of the vendor's first fast move."""
    reg: dict[int, int] = {}
    slot: int | None = None
    tables: list[list[int]] = []
    ctx: dict[int, int] | None = None
    for op in profile["ops"]:
        if op["kind"] == "write" and slot == 3:
            b = base64.b64decode(op["data"])
            tables.append([b[i] | (b[i + 1] << 8) for i in range(0, len(b) - 1, 2)])
            continue
        if op["kind"] != "control" or op["rt"] != 0x40 or op["value"] != 0x83 or len(op["data"]) < 2:
            continue
        d = op["data"]
        for k in range(0, len(d), 2):
            r, v = d[k], d[k + 1]
            if r == 0x5B:
                slot = ((v >> 3) & 7) if v & 0x40 else None
            if r == 0x0F and v == 1 and ctx is None:
                feedl = (reg.get(0x3D, 0) << 16) | (reg.get(0x3E, 0) << 8) | reg.get(0x3F, 0)
                if feedl > 1 and len(tables) >= 3:
                    ctx = dict(reg)
            reg[r] = v
    if ctx is None or len(tables) < 2:
        raise AsicError("profile has no recorded fast move")
    tables.sort(key=lambda t: t[-1])
    return {"fast": tables[0], "slow": tables[-1], "regs": ctx}


class Gl843V1Motion:
    """Homing from the vendor's motor primitives: fast reverse to the home sensor, 600 steps
    forward, then a slow reverse approach stopped when the sensor trips."""

    MOTOR_REGS = (0x1C, 0x1F, 0x21, 0x22, 0x23, 0x24, 0x38, 0x39, 0x5E, 0x5F, 0x67, 0x68, 0x69, 0x6B,
                  0x6C, 0x6D, 0x6E, 0x6F, 0x80)

    def __init__(self, usb: Gl843V1Usb, recorded: dict, *, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.usb = usb
        self.rec = recorded
        self.sleep = sleep
        self.now = now
        self.loaded: int | None = None
        self._base01 = recorded["regs"].get(0x01, 0x22) & ~0x01

    @staticmethod
    def _feedl(n: int) -> list[tuple[int, int]]:
        return [(0x3D, (n >> 16) & 0x0F), (0x3E, (n >> 8) & 0xFF), (0x3F, n & 0xFF)]

    def wait_idle(self, timeout_s: float) -> int:
        t0 = self.now()
        s = self.usb.status()
        while s & STATUS_MOTORENB:
            if self.now() - t0 > timeout_s:
                raise MotorTimeoutError(f"motor still running after {timeout_s:.0f} s")
            self.sleep(0.01)
            s = self.usb.status()
        return s

    def stop(self) -> int:
        """Vendor stop: ``0x02=0x08`` then FEEDL=1 while the move runs."""
        self.usb.write_registers([(0x01, self._base01)])
        self.usb.write_registers([(0x02, 0x08)])
        self.usb.write_registers(self._feedl(1))
        return self.wait_idle(5.0)

    def move(self, steps: int, *, reverse: bool, table: list[int], fmovno: int, timeout_s: float,
             until: Callable[[int], bool] | None = None) -> tuple[int, bool]:
        self.wait_idle(5.0)
        if self.loaded != id(table):
            self.usb.upload_table(3, table)
            self.loaded = id(table)
        regs = self.rec["regs"]
        pairs = [(0x01, self._base01)] + [(r, regs.get(r, 0)) for r in self.MOTOR_REGS]
        pairs += [(r, 0) for r in range(0x10, 0x16)]
        pairs += [(0x6A, fmovno), (0x02, 0x18 | (MTRREV if reverse else 0))] + self._feedl(steps)
        self.usb.write_registers(pairs)
        self.usb.write_registers([(0x0F, 1)])
        t0 = self.now()
        while True:
            s = self.usb.status()
            if until is not None and until(s):
                self.stop()
                return self.usb.status(), True
            if not s & STATUS_MOTORENB and s & STATUS_FEEDFSH:
                return s, False
            if self.now() - t0 > timeout_s:
                self.stop()
                raise MotorTimeoutError(f"move did not finish in {timeout_s:.0f} s")
            self.sleep(0.002)

    def home(self, timeout_s: float = 30.0) -> int:
        s = self.usb.status()
        if s & STATUS_MOTORENB:
            s = self.stop()
        at_home = lambda x: bool(x & STATUS_HOME)
        if not s & STATUS_HOME:
            _, reached = self.move(80000, reverse=True, table=self.rec["fast"], fmovno=0xFF, until=at_home,
                                   timeout_s=timeout_s)
            if not reached:
                raise MotorTimeoutError("home sensor not found")
        for _ in range(5):
            s, _ = self.move(600, reverse=False, table=self.rec["fast"], fmovno=0x46, timeout_s=5.0)
            if not s & STATUS_HOME:
                break
        else:
            raise MotorTimeoutError("home sensor stays on after moving forward")
        s, reached = self.move(3000, reverse=True, table=self.rec["slow"], fmovno=0xFF, until=at_home,
                               timeout_s=10.0)
        if not reached:
            raise MotorTimeoutError("home sensor not found on the final approach")
        return s


class Gl843V1:
    """High-level ASIC ops for the 7600i v1: boot, lamp, home/park, status."""

    def __init__(self, protocol: GenesysUsbProtocol, model) -> None:
        self.protocol = protocol
        self.model = model
        self.usb = Gl843V1Usb(getattr(protocol, "_transport", protocol))
        self._initialized = False
        self._scan_method: ScanMethod = "transparency"
        #: Set by homing and by a complete scan (the chip parks the carriage itself).
        self.position_known = False
        self.lamp_on_at: float | None = None
        self._motion: Gl843V1Motion | None = None

    @property
    def _reg_cache(self) -> dict[int, int]:
        return self.usb.shadow

    def read_status(self) -> ScannerStatus:
        return ScannerStatus.from_reg41(self.usb.status())

    def read_status_reliable(self) -> ScannerStatus:
        return self.read_status()

    def is_at_home(self) -> bool:
        return self.read_status().is_at_home

    def init(self, *, force: bool = False) -> None:
        """Vendor boot: register program, GPIO, AFE."""
        if self._initialized and not force:
            return
        data = self.model.replay_data()
        self.usb.ctl_in(0x8E, 0, 1)
        for group in data["boot"]:
            if group[0] == "8c":
                self.usb.ctl_out(0x8C, int(group[1]), [int(group[2])])
            else:
                self.usb.ctl_out(0x83, 0, group)
        self.usb.write_registers([(int(r), int(v)) for r, v in data["gpio"]])
        time.sleep(0.1)
        afe = data["afe"]
        values = {int(k): v for k, v in afe["config"].items()}
        values.update({2 + c: afe["gain"][c] for c in range(3)})
        values.update({5 + c: afe["offset"][c] for c in range(3)})
        for addr in sorted(values):
            self.usb.write_afe(addr, values[addr])
        self._initialized = True
        self.position_known = False
        self.forget_motor_tables()

    def set_scan_method(self, method: ScanMethod) -> None:
        self._scan_method = method

    def lamp_on(self) -> None:
        reg03 = self.usb.shadow.get(0x03, 0x9F)
        if self._scan_method == "infrared":
            self.usb.write_registers([(0x03, reg03 & ~LAMP_BIT), (0xA8, IR_GPIO_ON)])
        else:
            self.usb.write_registers([(0xA8, IR_GPIO_OFF), (0x03, reg03 | LAMP_BIT)])
        self.lamp_on_at = time.monotonic()

    def lamp_off(self) -> None:
        reg03 = self.usb.shadow.get(0x03, 0x9F)
        self.usb.write_registers([(0x03, reg03 & ~LAMP_BIT), (0xA8, IR_GPIO_OFF)])
        self.lamp_on_at = None

    def white_led(self, on: bool) -> None:
        reg03 = self.usb.shadow.get(0x03, 0x9F)
        self.usb.write_register(0x03, reg03 | LAMP_BIT if on else reg03 & ~LAMP_BIT)
        self.lamp_on_at = time.monotonic() if on else None

    def forget_motor_tables(self) -> None:
        """Boot and scan jobs overwrite the table slots homing uses."""
        if self._motion is not None:
            self._motion.loaded = None

    def motion(self) -> Gl843V1Motion:
        if self._motion is None:
            self._motion = Gl843V1Motion(self.usb, recorded_motion(self.model.replay_profile("color", 3600)))
        return self._motion

    def stop_motor(self) -> None:
        self.position_known = False
        self.motion().stop()

    def home(self, *, timeout_s: float = 30.0, wait: bool = True) -> None:
        self.position_known = False
        self.motion().home(timeout_s=max(5.0, timeout_s))
        self.position_known = True

    def park(self, *, timeout_s: float = 30.0) -> None:
        self.home(timeout_s=timeout_s)
