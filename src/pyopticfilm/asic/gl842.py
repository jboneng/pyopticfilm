# SPDX-License-Identifier: GPL-3.0-or-later
"""GL842 chip operations for OpticFilm 7200.

Boot sequences ported from SANE genesys ``gl842.cpp`` (7200 path).
"""

from __future__ import annotations

import time

from pyopticfilm.asic.gl845 import Gl845
from pyopticfilm.logging import get_logger

logger = get_logger(__name__)


class Gl842(Gl845):
    """GL842 ASIC — shares status/lamp/home with Gl845; different asic_boot."""

    REVERSE_SCAN_HOME = False

    def asic_boot(self, *, cold: bool | None = None) -> None:
        if cold is None:
            cold = self.is_cold_boot()
        logger.info("gl842 asic_boot cold=%s model=%s", cold, self.model.name)

        if cold:
            self.soft_reset()

        regs = self.model.boot_register_map()
        pairs = sorted(regs.items())
        self.protocol.write_registers(pairs)
        self._reg_cache = dict(regs)

        self.protocol.write_0x8c(0x10, 0x94)

        for addr in (0x2A, 0x2B):
            self.protocol.write_register(addr, 0x00)
            self._reg_cache[addr] = 0x00

        self._init_gpio()
        time.sleep(0.1)
        logger.info("gl842 asic_boot complete")
