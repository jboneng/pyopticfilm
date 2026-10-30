# SPDX-License-Identifier: GPL-3.0-or-later
"""GL843 chip operations for OpticFilm 7200i / 7300 / 7500i.

Boot sequences ported from SANE genesys ``gl843.cpp`` (film-scanner paths).
"""

from __future__ import annotations

import time

from pyopticfilm.asic.gl845 import Gl845
from pyopticfilm.logging import get_logger

logger = get_logger(__name__)

ENBDRAM = 0x08
DRAMSEL = 0x07


class Gl843(Gl845):
    """GL843 ASIC — shares status/lamp/home with Gl845; different asic_boot."""

    REVERSE_SCAN_HOME = False

    def asic_boot(self, *, cold: bool | None = None) -> None:
        if cold is None:
            cold = self.is_cold_boot()
        logger.info("gl843 asic_boot cold=%s model=%s", cold, self.model.name)

        if cold:
            self.soft_reset()

        try:
            speed = self.protocol.read_usb_speed_byte()
            usb_mode = 1 if (speed & 0x08) else 2
        except Exception:  # noqa: BLE001
            usb_mode = 2
        self.protocol.write_0x8c(0x0F, 0x14 if usb_mode == 1 else 0x11)

        # Pre-bulk 0x6B poke then restore from model tables
        self.protocol.write_register(0x6B, 0x02)

        regs = self.model.boot_register_map()
        # DRAM enable: keep DRAMSEL, set ENBDRAM; then apply CLKSET from model
        clkset = int(getattr(self.model, "gl843_clkset", 0x40))
        reg0b = (regs.get(0x0B, 0x4A) & DRAMSEL) | ENBDRAM | clkset
        regs = dict(regs)
        regs[0x0B] = reg0b

        pairs = [(a, v) for a, v in sorted(regs.items())]
        self.protocol.write_registers(pairs)
        self._reg_cache = dict(regs)

        clock = int(getattr(self.model, "gl843_clock_0x8c10", 0xD4))
        self.protocol.write_0x8c(0x10, clock)

        # Clear RAM address high bytes
        for addr in (0x29, 0x2A, 0x2B):
            try:
                self.protocol.write_register(addr, 0x00)
                self._reg_cache[addr] = 0x00
            except Exception as exc:  # noqa: BLE001
                logger.debug("RAM addr clear 0x%02x skipped: %s", addr, exc)

        self._init_gpio()
        time.sleep(0.1)
        logger.info("gl843 asic_boot complete")
