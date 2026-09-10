# SPDX-License-Identifier: GPL-3.0-or-later
"""GL845 chip operations for OpticFilm 8200i.

Sequences ported from SANE genesys ``gl846.cpp`` / ``low.cpp`` /
``command_set_common.cpp`` (GL845 shares the GL846 path). See NOTICE.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

from pyopticfilm.asic.registers import Gl845Registers
from pyopticfilm.asic.status import ScannerStatus
from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.device.protocol import FilmModel, ScanMethod
from pyopticfilm.device.sensor_lookup import frontend_regs_for
from pyopticfilm.exceptions import AsicError, MotorTimeoutError
from pyopticfilm.logging import get_logger
from pyopticfilm.scan.calib_gl128 import (
    AfeFrontend,
    AfeSearchConfig,
    run_afe_dichotomy,
)
from pyopticfilm.usb.protocol import GenesysUsbProtocol

logger = get_logger(__name__)

# Home/park must never run unbounded.
DEFAULT_HOME_TIMEOUT_S = 30.0
HOME_POLL_S = 0.1
FE_BUSY_TIMEOUT_S = 5.0
# Large feed length used while seeking home (genesys reverse-home uses ~40000).
HOME_FEED_STEPS = 40000
#: GL845 FE gain/offset registers (0x02-0x07) are single-byte; a wider
#: AfeSearchConfig (e.g. GL128's default 0x1FF) would converge on a code that
#: silently truncates on write (apply_afe_frontend masks & 0xFF).
FE_REGISTER_MAX = 0xFF


class Gl845:
    """High-level ASIC ops: boot, lamp, home/park, status."""

    def __init__(
        self,
        protocol: GenesysUsbProtocol,
        model: FilmModel = MODEL_8200I,
    ) -> None:
        self.protocol = protocol
        self.model = model
        self.registers = Gl845Registers()
        self._initialized = False
        self._reg_cache: dict[int, int] = {}
        self._scan_method: ScanMethod = "transparency"
        #: Last applied AFE gains/offsets (host calib may refresh these).
        self.last_afe_gains: tuple[int, int, int] | None = None
        self.last_afe_offsets: tuple[int, int, int] | None = None

    # --- status ---------------------------------------------------------

    def read_status(self) -> ScannerStatus:
        return ScannerStatus.from_reg41(self.protocol.read_register(self.registers.REG_0x41))

    def read_status_reliable(self) -> ScannerStatus:
        self.read_status()
        time.sleep(0.1)
        return self.read_status()

    def is_at_home(self) -> bool:
        return self.read_status_reliable().is_at_home

    def is_cold_boot(self) -> bool:
        """True when PWRBIT in 0x06 is clear (fresh power-up)."""
        return (self.protocol.read_register(self.registers.REG_0x06) & self.registers.PWRBIT) == 0

    # --- boot / init ----------------------------------------------------

    def soft_reset(self) -> None:
        self.protocol.write_register(self.registers.REG_0x0E, 0x01)
        self.protocol.write_register(self.registers.REG_0x0E, 0x00)

    def asic_boot(self, *, cold: bool | None = None) -> None:
        """Power-on ASIC bring-up (``CommandSetGl846::asic_boot`` for 8200i)."""
        if cold is None:
            cold = self.is_cold_boot()
        logger.info("asic_boot cold=%s", cold)

        if cold:
            self.soft_reset()

        # Optional chip version probe
        try:
            val40 = self.protocol.read_register(self.registers.REG_0x40)
            if val40 & self.registers.CHKVER:
                ver = self.protocol.read_register(0x00)
                logger.info("genesys chip version 0x%02x", ver)
        except Exception as exc:  # noqa: BLE001
            logger.debug("CHKVER probe skipped: %s", exc)

        regs = self.model.boot_register_map()
        pairs = sorted(regs.items())
        self.protocol.write_registers(pairs)
        self._reg_cache = dict(regs)

        # Clocks (8200i / 7400 path)
        self.protocol.write_0x8c(0x10, 0x0C)
        self.protocol.write_0x8c(0x13, 0x0C)

        self._init_gpio()
        self._init_memory_layout()

        self.protocol.write_register(0xF8, 0x05)
        self._reg_cache[0xF8] = 0x05

    def _init_gpio(self) -> None:
        """Write GPO table with 0x6e/0x6f first (genesys apply_registers_ordered)."""
        order = (0x6E, 0x6F)
        gpo = dict(self.model.gpo_regs)
        for addr in order:
            if addr in gpo:
                self.protocol.write_register(addr, gpo[addr])
                self._reg_cache[addr] = gpo[addr]
        for addr, value in gpo.items():
            if addr in order:
                continue
            self.protocol.write_register(addr, value)
            self._reg_cache[addr] = value

    def _init_memory_layout(self) -> None:
        for addr, value in sorted(self.model.memory_layout_regs.items()):
            self.protocol.write_register(addr, value)
            self._reg_cache[addr] = value

    def _wait_frontend_ready(self, timeout_s: float = FE_BUSY_TIMEOUT_S) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            status = self.read_status()
            if not status.is_front_end_busy:
                return
            if time.monotonic() >= deadline:
                raise AsicError("Analog frontend stayed busy")
            time.sleep(0.01)

    def set_frontend_init(self) -> None:
        """ADI frontend init (``gl846_set_adi_fe`` / AFE_INIT)."""
        self.apply_frontend_for_scan(resolution=None, method=self._scan_method)

    def _frontend_feset(self) -> int:
        return self._reg_cache.get(0x04, self.protocol.read_register(0x04)) & self.registers.FESET

    def afe_dichotomy_applicable(self) -> bool:
        """True when SANE ``scanner_offset_calibration`` / gain search run.

        Genesys ``genesys.cpp`` skips offset+gain for GL845/GL846 when
        ``(reg04 & FESET) == 0x02`` (ADI / AKM AFE). OpticFilm 8200i uses that
        FESET, so live dichotomy is off by default; host dark/white stretch
        covers shading. Wolfson-style frontends (other FESET) keep dichotomy.
        """
        return self._frontend_feset() != self.registers.FESET_ADI

    def apply_frontend_for_scan(
        self,
        *,
        resolution: int | None = None,
        method: ScanMethod | str | None = None,
    ) -> None:
        """Write AFE regs from model tables (+ optional dpi overlay / last search).

        Re-applies ``last_afe_gains`` / ``last_afe_offsets`` when set (genesys
        keeps FE codes from offset/gain calibration into the image scan).
        """
        feset = self._frontend_feset()
        if feset != self.registers.FESET_ADI:
            raise AsicError(f"Unsupported frontend type FESET={feset:#x} (need ADI=0x2)")

        scan_method = method or self._scan_method
        if resolution is None:
            fe = dict(self.model.frontend_regs)
        else:
            fe = dict(
                frontend_regs_for(
                    self.model, int(resolution), method=str(scan_method)
                )
            )
        if self.last_afe_gains is not None:
            for i, g in enumerate(self.last_afe_gains):
                fe[0x02 + i] = int(g) & 0xFF
        if self.last_afe_offsets is not None:
            for i, o in enumerate(self.last_afe_offsets):
                fe[0x05 + i] = int(o) & 0xFF

        self._wait_frontend_ready()
        self.protocol.write_fe_register(0x00, fe.get(0x00, 0xF8) & 0xFF)
        self.protocol.write_fe_register(0x01, fe.get(0x01, 0x80) & 0xFF)
        for i in range(3):
            self.protocol.write_fe_register(0x02 + i, fe.get(0x02 + i, 0) & 0xFF)
        for i in range(3):
            self.protocol.write_fe_register(0x05 + i, fe.get(0x05 + i, 0) & 0xFF)
        self.last_afe_gains = (
            int(fe.get(0x02, 0)),
            int(fe.get(0x03, 0)),
            int(fe.get(0x04, 0)),
        )
        self.last_afe_offsets = (
            int(fe.get(0x05, 0)),
            int(fe.get(0x06, 0)),
            int(fe.get(0x07, 0)),
        )
        logger.debug(
            "GL845 AFE applied gains=%s offsets=%s method=%s dpi=%s",
            self.last_afe_gains,
            self.last_afe_offsets,
            scan_method,
            resolution,
        )

    def apply_afe_frontend(
        self,
        frontend: AfeFrontend,
        *,
        method: ScanMethod | str | None = None,
        resolution: int | None = None,
    ) -> None:
        """Program FE gain/offset codes and remember them for later configure."""
        self.last_afe_gains = tuple(int(v) & 0xFF for v in frontend.gains)  # type: ignore[assignment]
        self.last_afe_offsets = tuple(int(v) & 0xFF for v in frontend.offsets)  # type: ignore[assignment]
        self.apply_frontend_for_scan(resolution=resolution, method=method)

    def search_afe(
        self,
        *,
        method: ScanMethod | str = "transparency",
        resolution: int | None = None,
        measure: Callable[[AfeFrontend], tuple[float, float, float]] | None = None,
        config: AfeSearchConfig | None = None,
        force_dichotomy: bool = False,
    ) -> AfeFrontend:
        """Offset then gain search (SANE ``scanner_*_calibration``) or table seed.

        When ``measure`` is provided and dichotomy applies (or
        ``force_dichotomy``), runs :func:`run_afe_dichotomy` against stationary
        strip means — same order as genesys offset then coarse gain. OpticFilm
        ADI FESET skips dichotomy unless forced (SANE ``genesys.cpp`` ~1403).
        """
        self.last_afe_gains = None
        self.last_afe_offsets = None
        self.apply_frontend_for_scan(resolution=resolution, method=method)
        assert self.last_afe_gains is not None and self.last_afe_offsets is not None
        seed = AfeFrontend(offsets=self.last_afe_offsets, gains=self.last_afe_gains)

        run_search = force_dichotomy or self.afe_dichotomy_applicable()
        if not run_search or measure is None:
            logger.info(
                "GL845 AFE search (table defaults%s) method=%s dpi=%s gains=%s offsets=%s",
                "" if measure is None else "; dichotomy skipped for ADI FESET",
                method,
                resolution,
                self.last_afe_gains,
                self.last_afe_offsets,
            )
            return seed

        cfg = config or AfeSearchConfig()
        if cfg.gain_max > FE_REGISTER_MAX or cfg.offset_max > FE_REGISTER_MAX:
            cfg = replace(
                cfg,
                gain_max=min(cfg.gain_max, FE_REGISTER_MAX),
                offset_max=min(cfg.offset_max, FE_REGISTER_MAX),
            )

        def _measure(fe: AfeFrontend) -> tuple[float, float, float]:
            self.apply_afe_frontend(fe, method=method, resolution=resolution)
            return measure(fe)

        result = run_afe_dichotomy(_measure, config=cfg, start=seed)
        self.apply_afe_frontend(result, method=method, resolution=resolution)
        logger.info(
            "GL845 AFE dichotomy method=%s dpi=%s gains=%s offsets=%s",
            method,
            resolution,
            self.last_afe_gains,
            self.last_afe_offsets,
        )
        return result

    def init(self, *, force: bool = False) -> None:
        """Full init: USB speed probe, asic_boot, frontend (SANE ``asic_init`` core)."""
        if self._initialized and not force and not self.is_cold_boot():
            logger.info("ASIC already initialized (warm); skipping boot")
            return

        try:
            speed = self.protocol.read_usb_speed_byte()
            usb_mode = 1 if (speed & 0x08) else 2
            logger.info("USB mode probe=0x%02x → USB %s.0", speed, usb_mode)
        except Exception as exc:  # noqa: BLE001
            logger.debug("USB speed probe failed (continuing): %s", exc)

        cold = self.is_cold_boot()
        self.asic_boot(cold=cold)
        self.set_frontend_init()
        self._initialized = True
        logger.info("ASIC init complete")

    # --- lamp -----------------------------------------------------------

    def set_scan_method(self, method: ScanMethod) -> None:
        self._scan_method = method

    def lamp_on(self) -> None:
        """Enable lamp (reg 0x03 LAMPPWR). IR uses XPA GPIO instead of white lamp."""
        if self._scan_method == "infrared" and not self.model.supports_infrared:
            raise AsicError(f"{self.model.model} does not support infrared")
        reg = self.protocol.read_register(self.registers.REG_0x03)
        if self._scan_method == "infrared":
            # genesys: IR scans clear LAMPPWR and set IR GPIO
            reg &= ~self.registers.LAMPPWR
            self.protocol.write_register(self.registers.REG_0x03, reg)
            self._set_ir_lamp(True)
        else:
            self._set_ir_lamp(False)
            reg |= self.registers.LAMPPWR
            self.protocol.write_register(self.registers.REG_0x03, reg)
        self._reg_cache[0x03] = reg
        logger.info("lamp_on method=%s reg03=0x%02x", self._scan_method, reg)

    def lamp_off(self) -> None:
        reg = self.protocol.read_register(self.registers.REG_0x03)
        reg &= ~self.registers.LAMPPWR
        self.protocol.write_register(self.registers.REG_0x03, reg)
        self._reg_cache[0x03] = reg
        self._set_ir_lamp(False)
        logger.info("lamp_off reg03=0x%02x", reg)

    def _set_ir_lamp(self, enabled: bool) -> None:
        """8200i IR XPA bit on GPIO 0xa8 (mask 0x04)."""
        a8 = self.protocol.read_register(self.registers.REG_0xA8)
        if enabled:
            a8 = (a8 & ~self.registers.IR_LAMP_A8_MASK) | self.registers.IR_LAMP_A8_MASK
        else:
            a8 &= ~self.registers.IR_LAMP_A8_MASK
        self.protocol.write_register(self.registers.REG_0xA8, a8)
        self._reg_cache[0xA8] = a8

    # --- motor / home / park --------------------------------------------

    def update_home_sensor_gpio(self) -> None:
        """``CommandSetGl846::update_home_sensor_gpio``."""
        val = self.protocol.read_register(self.registers.REG_0x6C)
        val |= 0x41
        self.protocol.write_register(self.registers.REG_0x6C, val)
        self._reg_cache[0x6C] = val

    def stop_motor(self) -> None:
        """Stop scan/motor action with a short settle (genesys stop_action subset)."""
        self.update_home_sensor_gpio()
        reg01 = self.protocol.read_register(self.registers.REG_0x01)
        reg01 &= ~self.registers.SCAN
        self.protocol.write_register(self.registers.REG_0x01, reg01)
        self._reg_cache[0x01] = reg01

        reg02 = self.protocol.read_register(self.registers.REG_0x02)
        reg02 &= ~self.registers.MTRPWR
        self.protocol.write_register(self.registers.REG_0x02, reg02)
        self._reg_cache[0x02] = reg02

        self.protocol.write_register(self.registers.REG_0x0F, 0x00)
        time.sleep(0.1)

    def _wait_until_home(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.read_status().is_at_home:
                return
            time.sleep(HOME_POLL_S)
        raise MotorTimeoutError(
            f"Head did not reach home within {timeout_s:.0f}s — motor stopped for safety."
        )

    def home(self, *, timeout_s: float = DEFAULT_HOME_TIMEOUT_S, wait: bool = True) -> None:
        """Move carriage to home with a hard timeout.

        Uses AGOHOME + MTRREV (genesys reverse-home flags). Always stops the motor
        on completion or failure so moves cannot run unbounded.
        """
        if not self._initialized:
            raise AsicError("Call init()/warmup() before home()")

        self.update_home_sensor_gpio()
        if self.is_at_home():
            logger.info("already at home")
            return

        if not wait:
            self._start_home_seek()
            return

        try:
            self._start_home_seek()
            self._wait_until_home(timeout_s)
            logger.info("reached home")
        finally:
            self.stop_motor()

        if not self.is_at_home():
            raise MotorTimeoutError(
                f"Head did not reach home within {timeout_s:.0f}s — motor stopped for safety."
            )

    def _start_home_seek(self) -> None:
        r = self.registers
        # Motor: power on, auto-go-home, reverse, not-home flag; clear fastfed
        reg02 = r.MTRPWR | r.AGOHOME | r.NOTHOME | r.MTRREV
        self.protocol.write_register(r.REG_0x02, reg02)
        self._reg_cache[0x02] = reg02

        # FEEDL ≈ genesys reverse-home starty
        self.protocol.write_u24(0x3D, HOME_FEED_STEPS)

        # Ensure SCAN cleared before feed
        reg01 = self.protocol.read_register(r.REG_0x01)
        reg01 &= ~r.SCAN
        self.protocol.write_register(r.REG_0x01, reg01)
        self._reg_cache[0x01] = reg01

        self.protocol.write_register(r.REG_0x0F, 0x01)  # start motor
        self.update_home_sensor_gpio()
        logger.info("home seek started (feed=%d)", HOME_FEED_STEPS)

    def park(self, *, timeout_s: float = DEFAULT_HOME_TIMEOUT_S) -> None:
        """Park is home for the OpticFilm 8200i (no separate rest position)."""
        self.home(timeout_s=timeout_s, wait=True)
