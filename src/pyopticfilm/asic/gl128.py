# SPDX-License-Identifier: GPL-3.0-or-later
"""GL128 driver for OpticFilm 8200i SE, 8100 (V2), and 8300i SE.

SANE genesys has no GL128 command set, so none of this is ported from SANE.
Every register write below replays what the Windows driver does in the USB
captures under ``captures/8200i-se/``; the model tables live in
``pyopticfilm.device.gl128_common`` (8100 V2 and 8300i SE are sibling leaves,
not SE subclasses).

Differences from :class:`~pyopticfilm.asic.gl845.Gl845` that matter here:

* status is at ``0x101`` (high-address read) rather than ``0x41``, though the
  bit layout is the same;
* the analog frontend is written through ``0x51``/``0x5D``/``0x5E``;
* the white lamp is ``0x03`` bit 4 and infrared is ``0x37`` bit 2 set by
  read-modify-write; ``0x03`` bit 5 (``XPASEL``) stays set for every
  transparency operation and ``AVEENB`` (``0x40``) is never set;
* positioning is two capture-constant feeds from home, then the image pass
  runs with ``FEEDL=1`` and ``AGOHOME`` so the carriage parks afterwards —
  see ``docs/gl128-model-comparison.md`` section 4.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Literal

from pyopticfilm.asic.buttons import BUTTON_IDS, Button
from pyopticfilm.asic.registers import Gl128Registers
from pyopticfilm.asic.status import ScannerStatus
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.protocol import Gl128Model
from pyopticfilm.device.tables_8200i_se import exposure_table
from pyopticfilm.exceptions import AsicError, MotorTimeoutError, ScanError, ScannerStuckError
from pyopticfilm.logging import get_logger
from pyopticfilm.scan.calib_gl128 import (
    AFE_BLACK_TARGET_COLOR,
    AFE_BLACK_TARGET_IR,
    AFE_ENDPIXEL,
    AFE_FALLBACK_OFFSETS_COLOR,
    AFE_FALLBACK_OFFSETS_IR,
    AFE_FALLBACK_PRE_OFFSETS,
    AFE_OFFSET_PROBE_HIGH,
    AFE_OFFSET_PROBE_LOW,
    AFE_PRE_OFFSET_BLACK,
    AFE_STRIP_BYTES,
    AFE_STRPIXEL,
    AFE_WHITE_TARGETS_COLOR,
    AFE_WHITE_TARGETS_IR,
    AFE_WIDE_BYTES,
    AFE_WIDE_ENDPIXEL,
    AFE_WIDE_PIXELS,
    AHB_SHADING,
    COLOR_SHADING_DARK_MEAN_MAX,
    COLOR_SHADING_DARK_SETTLE_S,
    FE_GAIN_INDICES,
    FE_OFFSET_INDICES,
    IR_SHADING_DARK_SETTLE_S,
    SHADING_GAIN_UNITY,
    SHADING_LINES,
    AfeFrontend,
    AfeStripTiming,
    afe_strip_timing,
    average_rgb16_columns,
    build_measured_shading_table,
    channel_means_u16,
    choose_usb_planar,
    coarse_gains_from_wide_means,
    declared_shading_size,
    encode_afe_offset,
    equalize_ir_white_columns,
    make_unity_white_table,
    offsets_for_black_level,
    optical_black_means,
    shading_acquire_width,
    shading_columns_mean,
    shading_gains_from_white,
    shading_width_for_resolution,
    validate_color_shading_gains,
    validate_color_shading_strip,
    validate_ir_shading_table,
)
from pyopticfilm.usb.protocol import GenesysUsbProtocol

logger = get_logger(__name__)

ScanMethod = Literal["transparency", "infrared"]

BRINGUP_HINT = (
    "GL128 (OpticFilm 8200i SE / 8100 V2 / 8300i SE) support is derived from "
    "USB captures of the Windows driver rather than from SANE — see "
    "docs/gl128-model-comparison.md."
)

#: Raised when code deliberately disarms the motor (e.g. stationary shading).
MOTOR_GATED_HINT = (
    "GL128 motor moves are temporarily disabled on this handle "
    "(disarmed for safety). Re-enable with Scanner.arm_bringup_motor() after "
    "stationary calib, or open a fresh Scanner (motor is on by default for "
    "scan-ready GL128)."
)

MM_PER_INCH = 25.4
HOME_POLL_S = 0.05

#: Stationary calib ready: buffer has data **and** carriage is at home.
#: Session 03 often shows ``0xbd`` / ``0xa9`` / ``0xad``; live HW also settles
#: on ``0x9c`` (SCANFSH|HOME|LAMP). Requiring only ``not BUFEMPTY`` also matches
#: motor-busy ``0xa5`` (no HOME) and can bulk-read stale AHB from a prior strip.
_STATIONARY_DATA_READY: frozenset[int] = frozenset({0xBD, 0xA9, 0xAD, 0x9C})
#: After an image pass, hardware ``0x02`` may still have MTRPWR|AGOHOME (park
#: leaves it alone) and SCAN may still be settling. Stationary START while
#: ``MOTORENB`` is set hangs as ``0xcd`` (HOME|BUFEMPTY|LAMP|MOTORENB).
_STATIONARY_IDLE_WAIT_S = 2.0
_STATIONARY_START_RETRIES = 1

#: Vendor probe ``wIndex`` polled during fast feeds until it returns this.
_FEED_PROBE_INDEX = 0x21
_FEED_PROBE_DONE = 0x04
#: How long a feed may go without reporting motion before its start-up state is
#: taken at face value. The probe and ``FEEDFSH`` both survive the previous
#: feed, so a completion seen inside this window is the *old* move's.
_FEED_START_TIMEOUT_S = 2.0
#: The vendor probe reads ``0x04`` while ``MOTORENB`` is still set. SilverFast drops
#: ``MTRPWR`` and polls ``0x101`` until ``MOTORENB`` clears before it writes ``0x02``,
#: the feed registers or the slope RAM again. A ramp loaded into a moving motor stalls it.
_MOTOR_IDLE_TIMEOUT_S = 2.0
_MOTOR_IDLE_POLL_S = 0.015
#: The reference feed ends when the vendor probe reads ``0x04``, about 0.9 s after
#: START on every GL128 sibling and PPI, long before its ``FEEDL`` runs out. A
#: carriage that starts off home can miss that signal and run on to the end
#: stop (8200i SE, #102), so give up well before ``FEEDL`` would finish.
_REFERENCE_FEED_TIMEOUT_S = 2.5
#: The carriage keeps moving after the probe reads ``0x04`` until ``MTRPWR`` drops,
#: about 0.33 rows per ms at 1800 dpi on the 8200i SE, so the probe poll interval
#: is position jitter. SilverFast polls every 15.6 ms (8200i SE, 8100 V2) or takes
#: the interrupt endpoint (8300i SE).
_FEED_PROBE_POLL_S = 0.002

#: Register block written immediately before every captured fast feed
#: (session 03 t≈8.98). Values are constants in the captures, not DPI-dependent.
_FEED_SETUP_REGS: dict[int, int] = {
    0x01: 0x22,
    0x04: 0x42,
    0x05: 0x48,
    0xA6: 0x00,
    0xA7: 0x00,
    0xA8: 0x00,
    0xA9: 0x00,
    0x7D: 0x00,
    0x7E: 0x36,
    0x7F: 0xB0,  # exposure = 14000
    0x80: 0x00,
    0x81: 0x40,
    0x82: 0x00,
    0x83: 0x00,
    0x84: 0xF2,
    0x85: 0x00,
    0x86: 0x29,
    0x87: 0x72,
    0x2C: 0x00,
    0x2D: 0xC8,  # DPISET = 200
    0x1D: 0x80,
    0x1C: 0x20,
    0xA4: 0x00,
    0xA5: 0x02,
    0xAA: 0x00,
    0xAB: 0x02,
    0xAE: 0x00,
    0xAF: 0x7F,
}
#: ``0xAF`` on the second (scan-start) positioning feed. Every SilverFast capture
#: (8200i SE, 8300i SE at 150-7200 ppi, 8100 V2) writes ``0x7F`` before the
#: reference feed and ``0xFF`` before this one. With ``0x7F`` the feed runs slower
#: and lands about 270 steps short, so every frame started 0.5 mm high (#102).
_FINAL_FEED_REG_AF = 0xFF
#: ``0xAF`` on the reference feed (also what :data:`_FEED_SETUP_REGS` carries).
_REFERENCE_FEED_REG_AF = 0x7F
#: SilverFast runs both positioning feeds with the image pass's registers already
#: programmed (DPISET, pixel clocks, LPERIOD, window, exposure, ``0x1D``, depth
#: ``0x33``), on every model and PPI. A feed changes only these itself: motor /
#: START / counters / FEEDL, and LINCNT stays at the previous strip's value.
_FEED_OWN_REGS: frozenset[int] = frozenset({0x02, 0x0D, 0x0F, 0x25, 0x26, 0x27, 0x3D, 0x3E, 0x3F})
#: Feed-time values SilverFast writes that differ from the image pass's.
_FEED_FIXED_REGS: tuple[int, ...] = (0x01, 0x05)


def _feed_setup_regs(scan_regs: Mapping[int, int] | None, *, final_positioning: bool) -> dict[int, int]:
    """Registers written before a positioning feed.

    With the image pass's ``scan_regs`` the feed runs under them like SilverFast's;
    without, the fixed capture recipe :data:`_FEED_SETUP_REGS` is used.
    """
    if scan_regs is None:
        setup = dict(_FEED_SETUP_REGS)
    else:
        setup = {a: v for a, v in scan_regs.items() if a not in _FEED_OWN_REGS}
        setup.update({a: _FEED_SETUP_REGS[a] for a in _FEED_FIXED_REGS})
    setup[0xAF] = _FINAL_FEED_REG_AF if final_positioning else _REFERENCE_FEED_REG_AF
    return setup

#: ``0x0B`` top bits select the clock and must agree with ``0x8C``; bit 3
#: enables DRAM. After the boot clock the vendor toggles DRAM enable off and on
#: with ``0x13=0x0F`` in between, and the next register blast puts ``0x13``
#: back to its boot value.
_REG_CLK_DRAM = 0x0B
_DRAM_ENABLE = 0x08
_REG_0x13 = 0x13
_DRAM_INIT_0x13 = 0x0F

#: ``0xA6``-``0xA9`` on every calibration strip (AFE, dark, white): SilverFast
#: writes ``00 01 00 01`` in every capture (all three models, 600-7200 ppi,
#: colour and IR); feeds and the image pass get ``00 00 00 00`` from the boot map.
_CALIB_STRIP_A6_A9: dict[int, int] = {0xA6: 0x00, 0xA7: 0x01, 0xA8: 0x00, 0xA9: 0x01}

#: Image cancel / end-scan lamp writes on ``0x03`` (sessions 03 return-home,
#: 08b/08c Cancel). Pre-clear pair, then clear ``SCAN`` to ``0x22``, then post.
_CANCEL_LAMP_PRE: tuple[int, ...] = (0x30, 0x20)
_CANCEL_LAMP_POST: tuple[int, ...] = (0x10, 0x00, 0x20, 0x30, 0x20, 0x30)
#: Capture writes ``0x01 = 0x22`` (clear ``SCAN``, keep SHDAREA|DVDSET).
_CANCEL_REG01 = 0x22


def _u16_table_bytes(words: tuple[int, ...]) -> bytes:
    """Pack 16-bit table entries little-endian, as the AHB windows expect."""
    out = bytearray(len(words) * 2)
    for i, word in enumerate(words):
        out[2 * i] = word & 0xFF
        out[2 * i + 1] = (word >> 8) & 0xFF
    return bytes(out)


def _encode_offsets(values: tuple[int, int, int]) -> tuple[int, int, int]:
    """Signed offsets to sign-magnitude FE codes."""
    return (encode_afe_offset(values[0]), encode_afe_offset(values[1]), encode_afe_offset(values[2]))


class Gl128:
    """GL128 chip operations for OpticFilm 8200i SE, 8100 (V2), and 8300i SE."""

    def __init__(
        self,
        protocol: GenesysUsbProtocol,
        model: Gl128Model = MODEL_8200I_SE,
    ) -> None:
        self.protocol = protocol
        self.model = model
        self.registers = Gl128Registers()
        self._initialized = False
        self._reg_cache: dict[int, int] = {}
        self._scan_method: ScanMethod = "transparency"
        #: On for scan-ready GL128; Lab/session may temporarily disarm for
        #: stationary shading (ASIC shade while armed caused motor grind).
        self._motor_moves_enabled = bool(getattr(model, "scan_ready", False))
        #: Whether the last SilverFast-style AGOHOME park completed.
        #: If false, the next scan/position is refused because the carriage
        #: origin for the next feed pair is unknown.
        self._park_ok: bool = True
        #: Set after a successful :meth:`run_asic_shading` upload this session.
        #: Infrared never sets this: the IR pass keeps DVDSET off (see run_asic_shading).
        self.asic_shading_ready = False
        #: Equalized per-column IR white (one value/column) for host flatten.
        self.last_ir_host_white: list[int] | None = None
        #: True when :attr:`last_ir_host_white` passed the IR validator.
        self.ir_host_flatten_ready: bool = False
        #: Last successful :meth:`search_afe` result; image configure re-applies
        #: it so scan setup does not wipe calibrated FE gains back to boot zero.
        self.last_afe: AfeFrontend | None = None
        #: ASIC dpi whose AFE strip timing :attr:`last_afe` was measured under.
        self.last_afe_dpi: int | None = None
        #: An image pass ran since the last boot: the next stationary strip block
        #: must re-initialise the ASIC first (see :meth:`reinit_before_stationary_strips`).
        self.image_pass_since_init: bool = False
        #: ``LPERIOD`` written by the last shading strip setup (for its log line).
        self._strip_lperiod = 0
        #: USB line layout learned from AFE strip means (True=planar RRR…GGG…BBB…).
        self.usb_planar_rgb: bool = False
        #: Last colour shading validation failure (for CalibrationError text).
        self.last_color_shading_reject_reason: str | None = None
        #: When HW DVDSET leaves whites raw, host stretch uses these columns.
        self.last_host_calib_dark: list[tuple[int, int, int]] | None = None
        self.last_host_calib_white: list[tuple[int, int, int]] | None = None
        #: True when colour shading failed ASIC arm but host dark/white is usable.
        self.last_color_shading_host_ok: bool = False
        #: Lab acoustic A/B: use ``SLOPE_TABLE_SLOW`` for non-shading table uploads.
        self.image_slope_slow: bool = False

    def _require_motor_enabled(self) -> None:
        if not self._motor_moves_enabled:
            raise AsicError(MOTOR_GATED_HINT)

    # --- registers ------------------------------------------------------

    def _write(self, address: int, value: int) -> None:
        self.protocol.write_register(address, value)
        self._reg_cache[address] = value & 0xFF

    def _write_many(self, regs: dict[int, int]) -> None:
        pairs = sorted(regs.items())
        self.protocol.write_registers_batched(pairs)
        self._reg_cache.update({a: v & 0xFF for a, v in pairs})

    def _update_bits(self, address: int, *, set_bits: int = 0, clear_bits: int = 0) -> int:
        """Read-modify-write one register and return the value written."""
        current = self.protocol.read_register(address)
        value = (current & ~clear_bits & 0xFF) | set_bits
        self._write(address, value)
        return value

    # --- status ---------------------------------------------------------

    def read_status(self) -> ScannerStatus:
        """Read the status register at ``0x101``."""
        try:
            raw = self.protocol.read_register(self.registers.REG_STATUS)
        except Exception as exc:
            raise AsicError(f"GL128 status read failed: {exc}") from exc
        return ScannerStatus.from_reg41(raw)

    def read_status_reliable(self) -> ScannerStatus:
        """Read status twice and keep the second value.

        The first read after an operation can still reflect the previous state,
        which is visible in the captures as a single stale sample before the
        driver's poll loops settle.
        """
        self.read_status()
        return self.read_status()

    def is_at_home(self) -> bool:
        return self.read_status_reliable().is_at_home

    def is_cold_boot(self) -> bool:
        """True when the power bit is clear, meaning the ASIC lost its state."""
        return self.read_status().is_replugged

    # --- boot -----------------------------------------------------------

    def set_frontend_init(self) -> None:
        """Load the analog frontend defaults through the GL124 write path."""
        for index, value in sorted(self.model.frontend_regs.items()):
            self.protocol.write_fe_register_gl124(index, value)
        logger.debug("GL128 frontend initialised (%d regs)", len(self.model.frontend_regs))

    def set_frontend_channels(
        self,
        *,
        offsets: tuple[int, int, int] | None = None,
        gains: tuple[int, int, int] | None = None,
    ) -> None:
        """Write per-channel FE gains (``0x02``–``0x04``) and/or offsets (``0x05``–``0x07``)."""
        fe = AfeFrontend(
            offsets=offsets if offsets is not None else (0, 0, 0),
            gains=gains if gains is not None else (0, 0, 0),
        )
        for index, value in fe.as_fe_writes():
            if gains is None and index in FE_GAIN_INDICES:
                continue
            if offsets is None and index in FE_OFFSET_INDICES:
                continue
            self.protocol.write_fe_register_gl124(index, value)

    def apply_frontend(self, frontend: AfeFrontend) -> None:
        """Program a full offset+gain FE state."""
        for index, value in frontend.as_fe_writes():
            self.protocol.write_fe_register_gl124(index, value)

    def _apply_stationary_scan_regs(self, *, include_memory_layout: bool = True) -> None:
        """Capture ``sensor_custom_regs`` + memory layout for calib acquires.

        Boot leaves ``0x04=0x02`` / ``0x05=0x48``; every session-03 AFE and
        shading strip rewrites ``0x04=0x42`` / ``0x05=0x40`` (and the rest of
        ``_SCAN_REGS``) plus the ``0xd0``–``0xf8`` layout before START.

        Session 04 does **not** rewrite ``0xd0``–``0xf8`` between the unity
        shading upload and the DVDSET white measure — skip that block there.
        """
        r = self.registers
        if include_memory_layout:
            self._write_many(dict(self.model.memory_layout_regs))
        custom = dict(self.model.sensor_custom_regs)
        # Never clobber lamp / IR from a stale table copy.
        custom.pop(r.REG_0x03, None)
        custom.pop(r.REG_IR, None)
        self._write_many(custom)

    def _wait_stationary_data_ready(self, timeout_s: float, *, where: str) -> None:
        """Poll ``0x101`` until stationary calib data is ready at home.

        Accept known capture codes, or any ``not BUFEMPTY`` while ``HOME`` is
        set (rejects motor-busy ``0xa5``, which lacks HOME). Use
        :meth:`_wait_shading_data_ready` with ``motorized=True`` for the SF
        colour white strip (``MTRPWR``, may leave home).
        """
        self._wait_shading_data_ready(timeout_s, where=where, motorized=False)

    def _wait_shading_data_ready(self, timeout_s: float, *, where: str, motorized: bool = False) -> None:
        """Poll ``0x101`` until shading/AFE strip data is ready.

        ``motorized=False`` (dark / AFE): require HOME + not BUFEMPTY (or a
        known home ready code). Rejects motor-busy ``0xa5`` so a prior strip's
        stale AHB is not drained while the head is off home.

        ``motorized=True`` (colour white / DVDSET): SF runs ``MTRPWR`` so the
        head may leave home — accept any ``not BUFEMPTY`` (including ``0xa5``).
        Call only after START; CLRCNT + prior SCAN clear avoid true stale AHB.
        """
        self.read_status()
        deadline = time.monotonic() + timeout_s
        last = -1
        while time.monotonic() < deadline:
            status = self.read_status()
            last = int(status.raw) & 0xFF
            if motorized:
                if not status.is_buffer_empty:
                    return
            elif last in _STATIONARY_DATA_READY or (not status.is_buffer_empty and status.is_at_home):
                return
            time.sleep(0.01)
        kind = "motorized" if motorized else "stationary"
        raise ScanError(f"{where}: no {kind} data ready within {timeout_s:.0f}s (last status=0x{last:02x})")

    def _reset_stationary_scan_engine(self, *, where: str) -> None:
        """Abort a leftover image SCAN so stationary geometry writes stick.

        Park leaves hardware ``0x02`` armed (``MTRPWR|AGOHOME``) and ``0x01``
        at ``0x22``. The next AFE START with AGOHOME still set hangs as
        ``0xcd`` (HOME|BUFEMPTY|LAMP|MOTORENB). Confirm SCAN/motor from
        hardware — the cache drops AGOHOME after park without writing it.
        """
        r = self.registers
        motor_mask = r.MTRPWR | r.AGOHOME | r.FASTFED
        self._write(r.REG_START, 0x00)
        self._write(r.REG_0x01, _CANCEL_REG01)
        self._write(r.REG_0x02, 0x00)
        self._write(r.REG_CLRCNT, r.CLRCNT_ALL)
        deadline = time.monotonic() + _STATIONARY_IDLE_WAIT_S
        last01 = last02 = last_st = -1
        status = None
        while time.monotonic() < deadline:
            last01 = int(self.protocol.read_register(r.REG_0x01)) & 0xFF
            last02 = int(self.protocol.read_register(r.REG_0x02)) & 0xFF
            status = self.read_status()
            last_st = int(status.raw) & 0xFF
            scan_clear = (last01 & r.SCAN) == 0
            motor_clear = (last02 & motor_mask) == 0
            idle = status.is_at_home and not status.is_motor_enabled
            if scan_clear and motor_clear and idle:
                self._reg_cache[r.REG_0x01] = last01
                self._reg_cache[r.REG_0x02] = last02
                if last01 & r.DVDSET:
                    self._write(
                        r.REG_0x01,
                        (last01 | r.SHDAREA) & ~r.SCAN & ~r.DVDSET,
                    )
                return
            if last01 & r.SCAN:
                self._write(r.REG_0x01, (last01 | r.SHDAREA) & ~r.SCAN & ~r.DVDSET)
            if last02 & motor_mask:
                self._write(r.REG_0x02, 0x00)
            time.sleep(0.02)
        # Motor power off and the carriage at home, yet MOTORENB stays set for
        # the whole wait (a healthy settle takes ~20 ms). Seen on an 8200i SE
        # after a colour pass: every later strip START hangs and only a
        # power-cycle clears it. Retrying, feeding or waiting for a park cannot
        # help, so stop here instead of driving the motor.
        if (
            status is not None
            and (last02 & motor_mask) == 0
            and status.is_at_home
            and status.is_motor_enabled
        ):
            raise ScannerStuckError(
                f"GL128 {where}: the scanner reports its motor running (status "
                f"0x{last_st:02x}) with motor power off, and it did not clear in "
                f"{_STATIONARY_IDLE_WAIT_S:.0f} s. Switch the scanner off and on, "
                "then retry the scan."
            )
        logger.warning(
            "GL128 %s: scan engine not idle (0x01=%#04x 0x02=%#04x status=0x%02x)",
            where,
            last01,
            last02,
            last_st,
        )

    def _wait_idle_at_home_for_stationary(self, timeout_s: float, *, where: str) -> None:
        """Wait until HOME and motor off so motor-off SCAN/START is accepted.

        Two consecutive idle samples (captures show one stale ``0x101``).
        Logs and returns on timeout so a SCAN retry can still recover.
        """
        deadline = time.monotonic() + timeout_s
        last = -1
        idle_hits = 0
        while time.monotonic() < deadline:
            status = self.read_status()
            last = int(status.raw) & 0xFF
            if status.is_at_home and not status.is_motor_enabled:
                idle_hits += 1
                if idle_hits >= 2:
                    return
            else:
                idle_hits = 0
            time.sleep(0.01)
        logger.warning(
            "GL128 %s: motor not idle at home within %.1fs (last status=0x%02x)",
            where,
            timeout_s,
            last,
        )

    def _start_stationary_strip(self) -> None:
        """Capture start recipe: motor off, ``0x0d`` → absolute SCAN → ``0x0f``.

        Absolute SCAN write — RMW can pick up a stale ``0x01`` (SCAN/DVDSET
        from the previous image pass) and then START never fills the AHB.
        Read back ``0x02`` so AGOHOME leftover from park cannot ride into START.
        """
        r = self.registers
        motor_mask = r.MTRPWR | r.AGOHOME | r.FASTFED
        self._write(r.REG_0x02, 0x00)
        reg02 = int(self.protocol.read_register(r.REG_0x02)) & 0xFF
        if reg02 & motor_mask:
            logger.warning("GL128 AFE START 0x02=%#04x still armed — rewriting 0", reg02)
            self._write(r.REG_0x02, 0x00)
        self._write(r.REG_CLRCNT, r.CLRCNT_ALL)
        reg01 = (self._reg_cache.get(r.REG_0x01, 0x22) | r.SHDAREA | r.SCAN) & ~r.DVDSET
        self._write(r.REG_0x01, reg01)
        self._write(r.REG_START, r.START_GO)

    def _setup_afe_strip_regs(self, *, wide: bool = False, timing: AfeStripTiming | None = None) -> None:
        """Stationary AFE strip geometry and timing (SF window and registers, motor off)."""
        r = self.registers
        self._apply_stationary_scan_regs()
        dpi_calib = self.model.optical_resolution // 6
        if timing is None:
            timing = afe_strip_timing(dpi_calib, wide=wide, pre_offset=not wide)
        # Image leftover 0xA5/0xAB are dpi-specific; AFE uses the 1200-dpi
        # dark-strip pixel clocks (session 03). Skipping this after an 1800 pass
        # left the CCD clock in image mode and START hung at 0xcd.
        _dummy, clk_a, clk_b = self.model.shading_strip_clocks(
            self.model.asic_dpi_for(1200), dvdset=False
        )
        self._write(0x2B, int(timing.dummy))
        self._write(0xA5, int(clk_a))
        self._write(0xAB, int(clk_b))
        # SF AFE strips: 0xA3=0, 0xA6:0xA7 = 0xA8:0xA9 = 1 (PartB B4.2).
        self._write(0xA3, 0x00)
        self._write_many(_CALIB_STRIP_A6_A9)
        end = AFE_WIDE_ENDPIXEL if wide else AFE_ENDPIXEL
        self.protocol.write_u24(r.REG_LINCNT, 1)
        self.protocol.write_u16(r.REG_DPISET, dpi_calib)
        self.protocol.write_u24(r.REG_STRPIXEL, AFE_STRPIXEL)
        self.protocol.write_u24(r.REG_ENDPIXEL, end)
        self.protocol.write_u24(r.REG_FEEDL, 1)
        self.protocol.write_u24(r.REG_LPERIOD, int(timing.lperiod))
        self.protocol.write_u24(r.REG_EXPOSURE, int(self.model.exposure_lperiod))
        self._write(r.REG_DEPTH_A, r.DEPTH16_A)
        self._write(r.REG_DEPTH_B, r.DEPTH16_B)
        # No motor: keep 0x02 clear of MTRPWR / AGOHOME / FASTFED.
        self._write(r.REG_0x02, 0x00)
        # SHDAREA, no SCAN yet — match calib-style 0x01 before the start recipe.
        reg01 = (self._reg_cache.get(r.REG_0x01, 0x22) | r.SHDAREA) & ~r.SCAN & ~r.DVDSET
        self._write(r.REG_0x01, reg01)

    def acquire_afe_strip(
        self,
        size: int = AFE_STRIP_BYTES,
        *,
        timeout_s: float = 5.0,
        timing: AfeStripTiming | None = None,
    ) -> bytes:
        """Read one stationary 16-bit AFE strip. Does not move the carriage."""
        if not self._initialized:
            self.init()
        r = self.registers
        size = int(size)
        if size <= 0:
            raise ValueError("AFE strip size must be positive")

        # Abort leftover image SCAN/AGOHOME before geometry writes (0xcd hang).
        self._reset_stationary_scan_engine(where="AFE strip")
        self._setup_afe_strip_regs(wide=size >= AFE_WIDE_BYTES, timing=timing)
        last_exc: ScanError | None = None
        for attempt in range(1 + _STATIONARY_START_RETRIES):
            if attempt:
                logger.warning("GL128 AFE strip START retry after: %s", last_exc)
                self._reset_stationary_scan_engine(where="AFE strip retry")
            self._start_stationary_strip()
            try:
                self._wait_stationary_data_ready(timeout_s, where="AFE strip")
                last_exc = None
                break
            except ScanError as exc:
                last_exc = exc
                self._reset_stationary_scan_engine(where="AFE strip fail")
        if last_exc is not None:
            raise last_exc

        self.protocol.bulk_read_begin(size, index=r.BULK_INDEX_RAM, addr=r.AHB_RAM_BASE)
        buf = bytearray()
        while len(buf) < size:
            chunk = self.protocol.bulk_read_chunk(min(size - len(buf), size))
            if not chunk:
                break
            buf.extend(chunk)
        self._update_bits(r.REG_0x01, clear_bits=r.SCAN)
        if len(buf) < size:
            raise ScanError(f"AFE strip short read: {len(buf)} of {size} bytes")
        return bytes(buf[:size])

    def search_afe(
        self,
        *,
        method: ScanMethod | None = None,
        resolution: int | None = None,
    ) -> AfeFrontend:
        """SilverFast's coarse AFE calibration; leave the FE at the result.

        Measured from the strip pixel data in SF's own captures (see the AFE
        block in :mod:`~pyopticfilm.scan.calib_gl128`):

        1. gains 0, two-point probe at offsets ``0x80``/``0xFF``, offsets that
           put the optical-black columns at ~0;
        2. a wide strip under the method's light at those offsets, AD9826 gain
           codes toward per-channel white targets;
        3. the two-point probe again under the new gains, offsets that put
           optical black at ``0x500`` (colour) or 0 (infrared).

        ``resolution`` picks SF's strip line periods for that scan. The lamp (or
        IR LED) stays on; the motor stays off.
        """
        if not self._initialized:
            self.init()
        self.reinit_before_stationary_strips()
        if method is not None:
            self.set_scan_method(method)
        infrared = self._scan_method == "infrared"
        scan_dpi = self.model.asic_dpi_for(resolution if resolution is not None else 1200)
        pre_timing = afe_strip_timing(scan_dpi, wide=False, pre_offset=True)
        timing = afe_strip_timing(scan_dpi, wide=True)
        # Strips are decoded chunky (per-channel FE sweeps on the 8200i SE move
        # only their own channel that way); image/shading layout stays the model's.
        self.usb_planar_rgb = self.model.usb_planar_rgb
        # Never lamp_off() here: in IR mode it clears the IR LED too.
        self.lamp_on()
        time.sleep(0.3)

        zero = (0, 0, 0)
        # Throwaway strip (#124): the first line after the CCD sat unclocked
        # carries the charge it gathered meanwhile, so its optical black reads
        # high by an amount that grows with idle time (+600 counts after ~10 s
        # at gains 0) and sensor temperature. That moved the pre-offsets from
        # about -105 (warm) to -25 (cold) and the gains by 2-3 codes. Clocking
        # one strip first makes the probe read true black every time.
        self.apply_frontend(AfeFrontend(gains=zero, offsets=(AFE_OFFSET_PROBE_LOW,) * 3))
        self.acquire_afe_strip(AFE_STRIP_BYTES, timing=pre_timing)
        pre = self._offsets_for_black(
            zero, AFE_PRE_OFFSET_BLACK, pre_timing, fallback=AFE_FALLBACK_PRE_OFFSETS, stage="pre-offset"
        )
        self.apply_frontend(AfeFrontend(gains=zero, offsets=_encode_offsets(pre)))
        wide = self.acquire_afe_strip(AFE_WIDE_BYTES, timing=timing)
        wide_means = channel_means_u16(wide, pixels=AFE_WIDE_PIXELS, planar=False)
        gains = coarse_gains_from_wide_means(
            wide_means, targets=AFE_WHITE_TARGETS_IR if infrared else AFE_WHITE_TARGETS_COLOR
        )
        offsets = self._offsets_for_black(
            gains,
            AFE_BLACK_TARGET_IR if infrared else AFE_BLACK_TARGET_COLOR,
            timing,
            fallback=AFE_FALLBACK_OFFSETS_IR if infrared else AFE_FALLBACK_OFFSETS_COLOR,
            stage="black offset",
        )
        result = AfeFrontend(gains=gains, offsets=_encode_offsets(offsets))
        self.apply_frontend(result)
        self.last_afe = result
        self.last_afe_dpi = scan_dpi
        logger.info(
            "GL128 AFE %s dpi=%d pre_offsets=%s wide_means=(%.0f,%.0f,%.0f) gains=%s offsets=%s",
            "infrared" if infrared else "colour",
            scan_dpi,
            pre,
            wide_means[0],
            wide_means[1],
            wide_means[2],
            gains,
            offsets,
        )
        return result

    def _offsets_for_black(
        self,
        gains: tuple[int, int, int],
        target: float,
        timing: AfeStripTiming,
        *,
        fallback: tuple[int, int, int],
        stage: str,
    ) -> tuple[int, int, int]:
        """Two-point optical-black probe at ``gains``; signed offsets for ``target``."""
        black: list[tuple[float, float, float]] = []
        for code in (AFE_OFFSET_PROBE_LOW, AFE_OFFSET_PROBE_HIGH):
            self.apply_frontend(AfeFrontend(gains=gains, offsets=(code, code, code)))
            black.append(optical_black_means(self.acquire_afe_strip(AFE_STRIP_BYTES, timing=timing)))
        solved = offsets_for_black_level(black[0], black[1], target)
        offsets = tuple(fallback[c] if v is None else v for c, v in enumerate(solved))
        if None in solved:
            logger.warning(
                "GL128 AFE %s: optical black barely moved between offsets %#x and %#x "
                "(%s -> %s); using SilverFast's codes %s on those channels",
                stage,
                AFE_OFFSET_PROBE_LOW,
                AFE_OFFSET_PROBE_HIGH,
                tuple(round(v) for v in black[0]),
                tuple(round(v) for v in black[1]),
                fallback,
            )
        return (offsets[0], offsets[1], offsets[2])

    def upload_shading_table(self, blob: bytes) -> None:
        """Write a packed shading coefficient blob to ``0x10014000``."""
        if not blob:
            raise ValueError("shading blob is empty")
        self.protocol.write_ahb(AHB_SHADING, blob)
        logger.info("GL128 uploaded shading table (%d bytes) to 0x%08x", len(blob), AHB_SHADING)

    def _shading_window(
        self,
        *,
        pixels: int,
        resolution: int,
        strpixel: int | None,
        endpixel: int | None,
        dpiset: int | None,
    ) -> tuple[int, int, int]:
        """Resolve the ``(STRPIXEL, ENDPIXEL, DPISET)`` for a shading pass.

        Sessions 04/05: the vendor runs shading through the **image** window and
        the **image** ``DPISET`` (e.g. 578/10490, DPISET=300 at 1800 dpi), then
        scans with the same window. A shading table measured through a different
        window/rate is indexed differently from the image and comes out as
        periodic dropouts, so callers should pass the scan geometry.
        """
        dpi = int(resolution)
        if dpiset is None:
            by_dpi = getattr(self.model, "register_dpiset_by_dpi", None)
            dpiset = int(by_dpi[dpi]) if by_dpi and dpi in by_dpi else max(1, dpi // 6)
        dpiset = int(dpiset)
        factor = max(1, self.model.optical_resolution // max(1, dpiset * 6))
        start = 240 if strpixel is None else int(strpixel)
        end = int(endpixel) if endpixel is not None else start + int(pixels) * factor
        return start, end, dpiset

    def _setup_shading_strip_regs(
        self,
        *,
        pixels: int,
        lines: int,
        resolution: int,
        strpixel: int | None = None,
        endpixel: int | None = None,
        dpiset: int | None = None,
        dvdset: bool = False,
    ) -> None:
        """Multi-line shading geometry (image window; motor policy per DVDSET)."""
        r = self.registers
        start, end, dpi_calib = self._shading_window(
            pixels=pixels,
            resolution=resolution,
            strpixel=strpixel,
            endpixel=endpixel,
            dpiset=dpiset,
        )
        dummy, clk_a, clk_b = self.model.shading_strip_clocks(
            int(resolution), dvdset=dvdset, span=end - start
        )
        # Session 04 leaves the geometry standing between the dark strip and the
        # DVDSET white; write it on both so a white strip never inherits a stale
        # window (the values are identical when the caller passes one window).
        if not dvdset:
            self._apply_stationary_scan_regs()
        self._write(0x2B, int(dummy))
        self._write(0xA5, int(clk_a))
        self._write(0xAB, int(clk_b))
        # SilverFast raises LINESEL on both strips at high PPI (model table).
        reg1d = self._reg_cache.get(0x1D, 0x80) & ~0x1F
        self._write(0x1D, reg1d | self.model.shading_linesel_for(int(resolution)))
        # Write these here rather than relying on a preceding AFE strip: when the
        # AFE is reused, the last image pass has reset them to 0.
        self._write_many(_CALIB_STRIP_A6_A9)
        self.protocol.write_u24(r.REG_LINCNT, int(lines))
        self.protocol.write_u16(r.REG_DPISET, dpi_calib)
        self.protocol.write_u24(r.REG_STRPIXEL, start)
        self.protocol.write_u24(r.REG_ENDPIXEL, end)
        self.protocol.write_u24(r.REG_FEEDL, 1)
        self._strip_lperiod = int(self.model.line_period_for(int(resolution), span=end - start))
        self.protocol.write_u24(r.REG_LPERIOD, self._strip_lperiod)
        self.protocol.write_u24(r.REG_EXPOSURE, int(self.model.exposure_lperiod))
        self._write(r.REG_DEPTH_A, r.DEPTH16_A)
        self._write(r.REG_DEPTH_B, r.DEPTH16_B)
        if dvdset:
            self._write(r.REG_0x02, r.MTRPWR | r.AGOHOME)
            self._write(0x3B, 0x00)
            self._write(0xA3, 0x00)
            reg01 = (self._reg_cache.get(r.REG_0x01, 0x22) | r.SHDAREA | r.DVDSET) & ~r.SCAN
        else:
            self._write(r.REG_0x02, 0x00)
            self._write(0xA3, 0x01)
            reg01 = (self._reg_cache.get(r.REG_0x01, 0x22) | r.SHDAREA) & ~r.SCAN & ~r.DVDSET
        self._write(r.REG_0x01, reg01)
        if dvdset:
            rb01 = int(self.protocol.read_register(r.REG_0x01)) & 0xFF
            rb02 = int(self.protocol.read_register(r.REG_0x02)) & 0xFF
            rb3b = int(self.protocol.read_register(0x3B)) & 0xFF
            rb_a3 = int(self.protocol.read_register(0xA3)) & 0xFF
            rb04 = int(self.protocol.read_register(0x04)) & 0xFF
            rb05 = int(self.protocol.read_register(0x05)) & 0xFF
            rb2b = int(self.protocol.read_register(0x2B)) & 0xFF
            rb_a5 = int(self.protocol.read_register(0xA5)) & 0xFF
            rb_ab = int(self.protocol.read_register(0xAB)) & 0xFF
            logger.info(
                "GL128 shading strip DVDSET setup readback "
                "0x01=%#04x 0x02=%#04x 0x3B=%#04x 0xA3=%#04x "
                "0x04=%#04x 0x05=%#04x 0x2B=%#04x 0xA5=%#04x 0xAB=%#04x",
                rb01,
                rb02,
                rb3b,
                rb_a3,
                rb04,
                rb05,
                rb2b,
                rb_a5,
                rb_ab,
            )

    def _require_carriage_at_home(self, where: str) -> None:
        """Refuse to continue if the head is off the home sensor (grind risk)."""
        status = self.read_status()
        if not status.is_at_home:
            raise ScanError(f"{where}: carriage is not at home - refuse to avoid grind. Park with SilverFast or power-cycle the scanner.")

    def acquire_shading_strip(
        self,
        *,
        resolution: int,
        pixels: int | None = None,
        lines: int = SHADING_LINES,
        timeout_s: float = 30.0,
        strpixel: int | None = None,
        endpixel: int | None = None,
        dpiset: int | None = None,
        dvdset: bool = False,
    ) -> bytes:
        """Read a multi-line 16-bit strip for ASIC shading.

        Dark (``dvdset=False``): motor off, wait at home. Colour white
        (``dvdset=True``): DVDSET + ``MTRPWR|AGOHOME``, motorized data-ready
        wait, then SCAN-clear parks via AGOHOME.
        """
        if not self._initialized:
            self.init()
        n = int(pixels) if pixels is not None else shading_width_for_resolution(resolution)
        lines = int(lines)
        size = n * lines * 6
        r = self.registers
        if not dvdset:
            self._reset_stationary_scan_engine(where="Shading strip")
        self._setup_shading_strip_regs(
            pixels=n,
            lines=lines,
            resolution=resolution,
            strpixel=strpixel,
            endpixel=endpixel,
            dpiset=dpiset,
            dvdset=dvdset,
        )
        # Re-assert motor policy immediately before START (cache/hardware drift).
        if dvdset:
            self._write(r.REG_0x02, r.MTRPWR | r.AGOHOME)
        else:
            self._write(r.REG_0x02, 0x00)
            self._wait_idle_at_home_for_stationary(
                _STATIONARY_IDLE_WAIT_S, where="Shading strip"
            )
        self._write(r.REG_CLRCNT, r.CLRCNT_ALL)
        # Absolute SCAN write — RMW can drop DVDSET if a stale 0x01 read races.
        reg01 = self._reg_cache.get(r.REG_0x01, 0x22) | r.SHDAREA | r.SCAN
        if dvdset:
            reg01 |= r.DVDSET
        else:
            reg01 &= ~r.DVDSET
        self._write(r.REG_0x01, reg01)
        if dvdset:
            logger.info(
                "GL128 shading strip START 0x01=%#04x 0x02=%#04x LPERIOD=%d DEPTH=%#04x/%#04x (want 0x01=%#04x 0x02=%#04x)",
                int(self.protocol.read_register(r.REG_0x01)) & 0xFF,
                int(self.protocol.read_register(r.REG_0x02)) & 0xFF,
                self._strip_lperiod,
                int(self._reg_cache.get(r.REG_DEPTH_A, 0)) & 0xFF,
                int(self._reg_cache.get(r.REG_DEPTH_B, 0)) & 0xFF,
                reg01 & 0xFF,
                (r.MTRPWR | r.AGOHOME) & 0xFF,
            )
        self._write(r.REG_START, r.START_GO)

        try:
            self._wait_shading_data_ready(timeout_s, where="Shading strip", motorized=dvdset)
        except ScanError:
            self._update_bits(r.REG_0x01, clear_bits=r.SCAN)
            if dvdset:
                self._write(r.REG_0x02, 0x00)
            raise

        self.protocol.bulk_read_begin(size, index=r.BULK_INDEX_RAM, addr=r.AHB_RAM_BASE)
        buf = bytearray()
        while len(buf) < size:
            chunk = self.protocol.bulk_read_chunk(min(65536, size - len(buf)))
            if not chunk:
                break
            buf.extend(chunk)
        # Clear SCAN so AGOHOME (armed during white) parks, then drop motor.
        self._update_bits(r.REG_0x01, clear_bits=r.SCAN)
        if dvdset:
            try:
                self.wait_until_at_home(timeout_s=30.0)
                logger.info("GL128 shading white strip parked at home after AGOHOME")
            except MotorTimeoutError as exc:
                self._write(r.REG_0x02, 0x00)
                raise ScanError(
                    "Shading white strip: AGOHOME did not return the carriage home. Park with SilverFast or power-cycle, then retry."
                ) from exc
        self._write(r.REG_0x02, 0x00)
        if len(buf) < size:
            raise ScanError(f"Shading strip short read: {len(buf)} of {size} bytes")
        return bytes(buf[:size])

    def run_asic_shading(
        self,
        *,
        resolution: int = 1800,
        method: ScanMethod | None = None,
        strpixel: int | None = None,
        endpixel: int | None = None,
        dpiset: int | None = None,
    ) -> bytes:
        """Dark→unity upload→white→measured upload (at home; feeds disarmed).

        Returns the final measured shading blob. Sets ``asic_shading_ready``.
        Image scans keep ``DVDSET`` when ready so the ASIC applies this table.

        White strip (colour and IR): DVDSET on the unity table + ``MTRPWR|AGOHOME``
        (AGOHOME during the measure so SCAN-clear parks; SF is MTRPWR-only), so
        the white is averaged over the carriage travel. Exposure AHB only after
        unity; dark/white use capture clocks.

        Pass ``method=\"infrared\"`` so the white strip runs under the IR LED
        (session 05: IR table uses zero dark terms + near-equal whites).

        Pass the image ``strpixel``/``endpixel``/``dpiset`` so shading uses the
        same origin as the scan. The table covers exactly the measured columns —
        a table measured through a different window is indexed differently from
        the image and DVDSET turns that into periodic dropouts.

        For ``method=\"infrared\"``, the optical dark strip is diagnostic only —
        the uploaded table dark is forced to zero (session 05). Unlike
        SilverFast, infrared **never** sets ``asic_shading_ready``: on the
        8200i SE (2026-10-27) a per-pixel IR table imprinted the white strip's
        column structure, about 1.4-1.8x the fine vertical streaking of the
        host flatten, which corrects only the smoothed falloff (it no longer
        clips). A validated white profile is stored for that host flatten
        instead (:attr:`ir_host_flatten_ready`).

        Refuses if ``_motor_moves_enabled`` — Lab must disarm before shading and
        re-arm only for the image feeds (capture order: shade, then motor).
        """
        if not self._initialized:
            self.init()
        if self._motor_moves_enabled:
            raise ScanError("ASIC shading requires motor disarmed — call disarm_bringup_motor before run_asic_shading")
        self.reinit_before_stationary_strips()
        if method is not None:
            self.set_scan_method(method)
        infrared = self._scan_method == "infrared"
        self._require_carriage_at_home("ASIC shading start")
        start, end, used_dpiset = self._shading_window(
            pixels=shading_width_for_resolution(resolution),
            resolution=resolution,
            strpixel=strpixel,
            endpixel=endpixel,
            dpiset=dpiset,
        )
        # The table covers exactly the pixels the strip measures — sessions 03/04
        # upload one column per acquired column (2478 px at 1800, 1728 at 1200).
        n = shading_acquire_width(
            strpixel=start,
            endpixel=end,
            dpiset=used_dpiset,
            optical_resolution=self.model.optical_resolution,
        )
        window = {"strpixel": start, "endpixel": end, "dpiset": used_dpiset}
        declared = declared_shading_size(n)
        self.asic_shading_ready = False
        if infrared:
            self.ir_host_flatten_ready = False
            self.last_ir_host_white = None

        r = self.registers
        if self.last_afe is not None:
            self.apply_frontend(self.last_afe)

        # SF: lamp off → ~0.5s → dark → unity → exposure AHB (no slope here) →
        # lamp on → white with DVDSET|MTRPWR|AGOHOME (NegPy ORs AGOHOME to park).
        settle_s = IR_SHADING_DARK_SETTLE_S if infrared else COLOR_SHADING_DARK_SETTLE_S
        self.lamp_off()
        self._reassert_lamp_off()
        time.sleep(settle_s)
        self._log_lamp_off_state(where="shading pre-dark")
        if not infrared:
            self._await_colour_optical_dark()

        dark_raw = self.acquire_shading_strip(resolution=resolution, pixels=n, **window)
        # Colour film USB is chunky (session 11). Do not trust lag on a flat dark
        # field — a false planar pick builds a smooth but wrong table that DVDSET
        # turns into a diamond/moiré. IR may still use lag when needed later.
        strip_planar = False if not infrared else choose_usb_planar(dark_raw[: max(0, n * 6)], pixels=n)
        dark_measured = average_rgb16_columns(dark_raw, pixels=n, lines=SHADING_LINES, planar=strip_planar)
        if infrared:
            dark = [(0, 0, 0)] * len(dark_measured)
            logger.info(
                "GL128 IR shading: zero dark terms (session 05); measured dark0=%s (diagnostic only)",
                dark_measured[0],
            )
        else:
            dark = dark_measured
            logger.info(
                "GL128 colour dark strip mean=%.0f dark0=%s",
                shading_columns_mean(dark[:n]),
                dark[0],
            )
        unity = make_unity_white_table(dark, declared_size=declared)
        self.upload_shading_table(unity)

        # SF session 04: lamp on → exposure AHB → arm DVDSET (0x22) → white strip.
        if not infrared:
            self.lamp_on()
            self._log_lamp_state(where="shading pre-white")
        # SilverFast's white-strip motor table, not the image pass's.
        self.upload_tables(
            resolution=resolution,
            shading=False,
            slope=False,
            channel_exposure=self.model.shading_white_motor_period(resolution),
        )
        # Arm DVDSET on the unity table for the white strip, colour and IR alike
        # (SF 8300i SE ladder / 8200i SE session 05: IR white 0x01=0x23, 0x02=0x10).
        reg01 = (self._reg_cache.get(r.REG_0x01, 0x22) | r.SHDAREA | r.DVDSET) & ~r.SCAN
        self._write(r.REG_0x01, reg01)

        if infrared:
            self.lamp_on()
            self._write(r.REG_0x03, r.XPASEL)
            self._apply_infrared(enabled=True)
            reg03 = self.protocol.read_register(r.REG_0x03)
            reg37 = self.protocol.read_register(r.REG_IR)
            logger.info(
                "GL128 IR white strip illum 0x03=%#04x (LAMPPWR=%s) 0x37=%#04x (IR_LED=%s)",
                reg03,
                bool(reg03 & r.LAMPPWR),
                reg37,
                bool(reg37 & r.IR_LED),
            )
            if reg03 & r.LAMPPWR or not (reg37 & r.IR_LED):
                self._write(r.REG_0x03, r.XPASEL)
                self._apply_infrared(enabled=True)
        time.sleep(0.5)
        # SF session 03: after unity, white strip with 0x01=0x23 (SHDAREA|DVDSET)
        # and the motor running, so the white reference is averaged over the
        # carriage travel rather than read at one spot. At unity gain the ASIC
        # returns ``raw - dark`` (IR dark is 0), so ~50-57k here is the expected
        # reading; the gains below turn that into the flattening table.
        white_raw = self.acquire_shading_strip(resolution=resolution, pixels=n, dvdset=True, **window)
        white = average_rgb16_columns(white_raw, pixels=n, lines=SHADING_LINES, planar=strip_planar)
        raw_white: list[tuple[int, int, int]] | None = None
        self.last_host_calib_dark = None
        self.last_host_calib_white = None
        self.last_color_shading_host_ok = False
        if infrared:
            raw0 = white[0]
            raw_spread = max(int(c) for c in raw0) - min(int(c) for c in raw0)
            raw_white = list(white)
            white = equalize_ir_white_columns(white)
            logger.info(
                "GL128 IR shading: equalized white columns (session 05 shape); raw white0=%s spread=%d → equalized white0=%s",
                raw0,
                raw_spread,
                white[0],
            )
        gains = shading_gains_from_white(white)
        measured = build_measured_shading_table(dark, white, declared_size=declared)
        self.upload_shading_table(measured)
        self._write(self.registers.REG_0x02, 0x00)
        self._require_carriage_at_home("ASIC shading end")
        # IR: never arm ASIC DVDSET — a per-pixel IR table streaks the IR plane
        # (see docstring). Store a host white profile when the table validates;
        # the smoothed image flatten uses that.
        if infrared:
            ok, reason = validate_ir_shading_table(
                dark[:n],
                white[:n],
                acquire_width=n,
                raw_white=None if raw_white is None else raw_white[:n],
            )
            white_prefix = white[:n]
            white_mean = sum(int(c) for row in white_prefix for c in row) / max(1, len(white_prefix) * 3)
            self.asic_shading_ready = False
            if ok:
                self.last_ir_host_white = [int(row[0]) for row in white_prefix]
                self.ir_host_flatten_ready = True
                logger.info(
                    "GL128 IR host flatten ready dpi=%d acquire=%d "
                    "window=%d..%d dpiset=%d white0=%s "
                    "white_mean=%.0f measured_dark0=%s (DVDSET off)",
                    resolution,
                    n,
                    start,
                    end,
                    used_dpiset,
                    white[0],
                    white_mean,
                    dark_measured[0],
                )
            else:
                self.last_ir_host_white = None
                self.ir_host_flatten_ready = False
                logger.warning(
                    "GL128 IR host flatten rejected (%s); DVDSET off dpi=%d acquire=%d white0=%s white_mean=%.0f measured_dark0=%s",
                    reason,
                    resolution,
                    n,
                    white[0],
                    white_mean,
                    dark_measured[0],
                )
        else:
            strip_ok, reason = validate_color_shading_strip(dark[:n], white[:n], acquire_width=n)
            ok = strip_ok
            if ok:
                ok, reason = validate_color_shading_gains(gains[:n])
            median_gain = sorted(int(c) for row in gains[:n] for c in row)[n * 3 // 2]
            if ok:
                self.asic_shading_ready = True
                self.last_color_shading_reject_reason = None
                logger.info(
                    "GL128 ASIC shading ready dpi=%d method=%s acquire=%d window=%d..%d "
                    "dpiset=%d dark0=%s white0=%s gain0=%s median_gain=%.3fx",
                    resolution,
                    self._scan_method,
                    n,
                    start,
                    end,
                    used_dpiset,
                    dark[0],
                    white[0],
                    gains[0],
                    median_gain / SHADING_GAIN_UNITY,
                )
            else:
                self.asic_shading_ready = False
                self.last_color_shading_reject_reason = reason
                self.upload_shading_table(unity)
                # Only offer the strips to host stretch when the light path itself
                # was sane; film or a railed white makes host stretch wrong too.
                if strip_ok:
                    self.last_host_calib_dark = [(int(r[0]), int(r[1]), int(r[2])) for r in dark[:n]]
                    self.last_host_calib_white = [(int(r[0]), int(r[1]), int(r[2])) for r in white[:n]]
                    self.last_color_shading_host_ok = True
                logger.warning(
                    "GL128 colour ASIC shading rejected (%s); DVDSET left off, unity "
                    "table restored, host_stretch=%s. dpi=%d acquire=%d "
                    "dark_mean=%.0f white_mean=%.0f median_gain=%.3fx",
                    reason,
                    strip_ok,
                    resolution,
                    n,
                    shading_columns_mean(dark[:n]),
                    shading_columns_mean(white[:n]),
                    median_gain / SHADING_GAIN_UNITY,
                )
        return measured

    def reinit_before_stationary_strips(self) -> None:
        """Re-initialise the ASIC before stationary strips that follow an image pass.

        SilverFast rewrites the full register table (and the AFE defaults)
        before every block of stationary strips that follows an image pass:
        at the next scan, and inside a colour + IR scan before the IR
        calibration. Without it, a stationary strip after a long-exposure pass
        (ME long pass at 85000, or a manual 20000) could hang with ``MOTORENB``
        stuck until a power-cycle (8200i SE, B8). Hardware, 2026-10-28: without
        it, ME then a new resolution stuck 3 of 3 times; with it, 0 of 10
        (5 of those, and 5 colour + IR scans at 20000).

        Keeps what is still valid on the host (AFE codes, IR profile); clears
        the ASIC shading flag, since the boot DRAM toggle may wipe that RAM.
        Re-enters the scan clock last: boot leaves the boot clock, which runs
        the 8300i SE motor too fast.
        """
        if not self.image_pass_since_init:
            return
        logger.info("GL128 re-initialising the ASIC before stationary strips (after an image pass)")
        self.asic_boot()
        self.upload_tables(resolution=max(self.model.resolutions_dpi))
        self.asic_shading_ready = False
        self.image_pass_since_init = False
        self.enter_scan_clock()

    def upload_tables(
        self,
        *,
        resolution: int,
        shading: bool = False,
        slope: bool = True,
        channel_exposure: int | None = None,
    ) -> None:
        """Upload motor slope and/or scan motor tables to scanner RAM.

        ``shading=True`` loads the slow ramp (session 03 feeds); otherwise the
        fast ramp used for feeds and image. Pass ``slope=False`` for the
        unity→white window (session 03/04: exposure AHB only).

        When ``image_slope_slow`` is set (Scan Lab acoustic probe), non-shading
        uploads also use the slow ramp. Fast feeds still call
        :meth:`_upload_fast_slopes` directly.
        """
        r = self.registers
        if slope:
            use_slow = bool(shading or getattr(self, "image_slope_slow", False))
            table = (
                self.model.slope_table_slow()
                if use_slow
                else self.model.slope_table_fast()
            )
            slope_bytes = _u16_table_bytes(table)
            self.protocol.write_ahb(r.AHB_SLOPE_SCAN, slope_bytes)
            self.protocol.write_ahb(r.AHB_SLOPE_FAST, slope_bytes)

        if channel_exposure is None:
            channel_exposure = self.model.channel_exposure_for(resolution)
        table_fn = getattr(self.model, "scan_motor_table", None)
        words = (
            table_fn(int(channel_exposure))
            if callable(table_fn)
            else exposure_table(int(channel_exposure))
        )
        exposure = _u16_table_bytes(words)
        for addr in (r.AHB_SCAN_MOTOR_0, r.AHB_SCAN_MOTOR_1, r.AHB_SCAN_MOTOR_2):
            self.protocol.write_ahb(addr, exposure)
        logger.debug(
            "GL128 uploaded %s for %d dpi",
            (
                "exposure only"
                if not slope
                else (
                    "slow slope + exposure"
                    if (shading or getattr(self, "image_slope_slow", False))
                    else "fast slope + exposure"
                )
            ),
            resolution,
        )

    def asic_boot(self, *, cold: bool | None = None) -> None:
        """Replay the captured cold-boot sequence.

        The Windows driver performs no soft reset and never writes ``0x0E``-
        ``0x10``, so neither does this. Model hooks select the parts that differ
        by model: the AHB blobs, the memory layout and GPO blocks, and extra
        vendor traffic before the frontend init.
        """
        del cold
        for addr, blob in self.model.boot_blobs():
            self.protocol.write_ahb(int(addr), bytes(blob))
        self._write_many(dict(self.model.init_regs))
        self._write_clock(int(self.model.asic_clock_boot_0x8c))
        dram = int(self.model.asic_boot_0x0b)
        self._write(_REG_CLK_DRAM, dram & ~_DRAM_ENABLE)
        self._write(_REG_0x13, _DRAM_INIT_0x13)
        self._write(_REG_CLK_DRAM, dram | _DRAM_ENABLE)
        layout_and_gpo = self.model.boot_writes_layout_and_gpo()
        if layout_and_gpo:
            self._write_many(dict(self.model.memory_layout_regs))
        self._run_boot_vendor_steps()
        self.set_frontend_init()
        if layout_and_gpo:
            self._write_many(dict(self.model.gpo_regs))
        logger.info(
            "GL128 boot: %d init + %d layout + %d gpo registers",
            len(self.model.init_regs),
            len(self.model.memory_layout_regs),
            len(self.model.gpo_regs),
        )

    def _run_boot_vendor_steps(self) -> None:
        """Replay :meth:`Gl128Common.boot_vendor_steps` for this model."""
        for step in self.model.boot_vendor_steps():
            kind = step[0]
            if kind == "out":
                _, value, index, data = step
                self.protocol.write_vendor(int(value), int(index), bytes(data))
            elif kind == "in":
                _, value, index, length = step
                self.protocol.read_vendor(int(value), int(index), int(length))
            elif kind == "reg":
                _, address, value = step
                self._write(int(address), int(value))
            else:
                raise AsicError(f"unknown GL128 boot step {step!r}")

    def _write_clock(self, value: int) -> None:
        self.protocol.write_0x8c(0x10, value)
        self.protocol.write_0x8c(0x13, value)

    def enter_scan_clock(self) -> None:
        """Switch to the scan clock the way the vendor opens every scan pass.

        The boot value goes out first, then the scan value (8300i SE:
        ``0x0E`` then ``0x0D``), then ``0x0B`` and ``0x13`` at their scan
        values as in the vendor's following register blast. Calibration and
        image both run on it.
        """
        if not self._initialized:
            self.init()
        boot = int(self.model.asic_clock_boot_0x8c)
        scan = int(self.model.asic_clock_scan_0x8c)
        self._write_clock(boot)
        self._write_clock(scan)
        self._write(_REG_CLK_DRAM, int(self.model.sensor_custom_regs[_REG_CLK_DRAM]))
        self._write(_REG_0x13, int(self.model.init_regs[_REG_0x13]))
        logger.debug("GL128 scan clock 0x8C: %#04x -> %#04x", boot, scan)

    def init(self, *, force: bool = False) -> None:
        if self._initialized and not force:
            return
        self.asic_boot()
        if self.model.boot_uploads_tables():
            self.upload_tables(resolution=max(self.model.resolutions_dpi))
        self.last_afe = None
        self.last_afe_dpi = None
        self.image_pass_since_init = False
        self.asic_shading_ready = False
        self.last_ir_host_white = None
        self.ir_host_flatten_ready = False
        self.usb_planar_rgb = False
        self._initialized = True
        logger.info("GL128 initialised (%s)", self.model.model)

    # --- buttons --------------------------------------------------------

    def button_state(self, timeout_ms: int = 0) -> set[Button]:
        buttons = set()

        data = self.protocol.read_intr(timeout_ms=timeout_ms)
        if len(data) == 0:
            return buttons
        button_byte = data[0]

        for id, button in BUTTON_IDS:
            if button_byte & id == 0:
                continue
            buttons.add(button)

        return buttons


    # --- lamp / infrared ------------------------------------------------

    def set_scan_method(self, method: ScanMethod) -> None:
        if method not in ("transparency", "infrared"):
            raise ValueError(f"Unsupported scan method {method!r}")
        self._scan_method = method
        logger.debug("GL128 scan_method=%s", method)

    def _apply_infrared(self, *, enabled: bool) -> None:
        r = self.registers
        if enabled:
            self._update_bits(r.REG_IR, set_bits=r.IR_LED)
        elif self._scan_method == "infrared":
            self._update_bits(r.REG_IR, clear_bits=r.IR_LED)
        else:
            # 0x37 reads back 0xB0 after the boot write of 0xC0; the vendor
            # holds 0xC0 for all colour work rather than rewriting the readback.
            self._write(r.REG_IR, int(self.model.init_regs.get(r.REG_IR, 0xC0)))

    def lamp_on(self) -> None:
        """Power the lamp for the selected method.

        Infrared: white lamp off, ``0x37`` bit 2 set. Colour: single
        ``XPASEL|LAMPPWR`` write (capture ``0x30``) — no multi-toggle strobe.
        """
        r = self.registers
        infrared = self._scan_method == "infrared"
        if infrared:
            self._write(r.REG_0x03, r.XPASEL)
            self._apply_infrared(enabled=True)
        else:
            self._write(r.REG_0x03, r.XPASEL | r.LAMPPWR)
            self._apply_infrared(enabled=False)
        logger.info("GL128 lamp on (%s)", self._scan_method)

    def lamp_off(self) -> None:
        if not self._initialized:
            logger.debug("GL128 lamp_off before init — nothing to do")
            return
        self._reassert_lamp_off()
        logger.info("GL128 lamp off")

    def _reassert_lamp_off(self) -> None:
        """Force white lamp + IR LED off (``0x03 = 0x20``)."""
        r = self.registers
        self._write(r.REG_0x03, r.XPASEL)
        self._apply_infrared(enabled=False)

    def _strike_lamp_on(self) -> None:
        """Replay capture lamp-stabilise toggles; end with white lamp on.

        Sessions 03/08 use ``0x20``/``0x30`` pairs around lamp transitions; a
        single ``LAMPPWR`` write after a long off often leaves the tube cold.
        """
        r = self.registers
        infrared = self._scan_method == "infrared"
        if infrared:
            self._write(r.REG_0x03, r.XPASEL)
            self._apply_infrared(enabled=True)
            return
        off = r.XPASEL
        on = r.XPASEL | r.LAMPPWR
        # Capture-like strobe, then hold on.
        for value in (on, off, on, off, on):
            self._write(r.REG_0x03, value)
            time.sleep(0.02)
        self._apply_infrared(enabled=False)
        logger.info("GL128 lamp strike (transparency)")

    def _await_colour_optical_dark(self) -> None:
        """Cheap AFE probe after lamp-off; one extra 0.5s settle then fail-fast.

        SF colour dark is ~1k after ~0.5s off. Mid-scale here means the lamp
        did not extinguish or the head is not on a clear home field.
        """

        def _probe_mean() -> float:
            if self.last_afe is not None:
                self.apply_frontend(self.last_afe)
            strip = self.acquire_afe_strip(AFE_STRIP_BYTES)
            means = channel_means_u16(strip, planar=self.usb_planar_rgb)
            return sum(means) / 3.0

        mean = _probe_mean()
        logger.info(
            "GL128 colour pre-dark AFE probe mean=%.0f (need <= %d)",
            mean,
            COLOR_SHADING_DARK_MEAN_MAX,
        )
        if mean <= COLOR_SHADING_DARK_MEAN_MAX:
            return
        self._reassert_lamp_off()
        time.sleep(COLOR_SHADING_DARK_SETTLE_S)
        self._log_lamp_off_state(where="shading pre-dark retry")
        mean = _probe_mean()
        logger.info(
            "GL128 colour pre-dark AFE probe retry mean=%.0f (need <= %d)",
            mean,
            COLOR_SHADING_DARK_MEAN_MAX,
        )
        if mean <= COLOR_SHADING_DARK_MEAN_MAX:
            return
        raise ScanError(
            f"shading pre-dark: lamp-off strip still bright (mean={mean:.0f} > "
            f"{COLOR_SHADING_DARK_MEAN_MAX}). Lamp did not go dark or head is "
            "not on the clear home field — power-cycle / park and retry."
        )

    def _log_lamp_state(self, *, where: str) -> None:
        r = self.registers
        reg03 = int(self.protocol.read_register(r.REG_0x03))
        status = self.read_status()
        logger.info(
            "GL128 %s lamp state 0x03=%#04x LAMPPWR=%s LAMPSTS=%s",
            where,
            reg03,
            bool(reg03 & r.LAMPPWR),
            status.is_lamp_on,
        )

    def _log_lamp_off_state(self, *, where: str) -> None:
        self._log_lamp_state(where=where)

    def update_home_sensor_gpio(self) -> None:
        """No-op: the SE captures show no GPIO poke around scan start."""

    # --- motion ---------------------------------------------------------

    def feed_steps_for_mm(self, distance_mm: float) -> int:
        """Convert millimetres to steps (experimental; prefer capture constants)."""
        limit_mm = max(0.0, min(float(distance_mm), self.model.max_feed_mm))
        if limit_mm != distance_mm:
            logger.warning(
                "Clamped feed %.2f mm to %.2f mm (model max_feed_mm)",
                distance_mm,
                limit_mm,
            )
        return int(limit_mm * self.model.feed_steps_per_inch / MM_PER_INCH)

    def _upload_fast_slopes(self, *, use_slow: bool = False) -> None:
        """Upload the motor ramp to both AHB slope windows.

        Positioning feed pairs are capture-faithful on GL128: SilverFast
        uploads the model's slow ramp for the first (reference) feed and the
        fast ramp for the second (final positioning) feed — confirmed
        byte-exact on the 8200i SE (39/39) and 8100 V2 (two 2026-09 sessions;
        see jboneng/pyopticfilm#56). The 8300i SE uses the same SLOW→FAST
        *order* with CUSTOM payloads (heads ``0x32BB`` / ``0x846A``). An
        earlier V2 fix shipped the opposite pair based on superseded analysis;
        using fast for both caused a real mechanical fault on V2 hardware.
        :meth:`position_for_full_frame_scan` always uploads slow-then-fast.
        ``feed()`` still uploads the fast ramp.
        """
        table = (
            self.model.slope_table_slow()
            if use_slow
            else self.model.slope_table_fast()
        )
        slope = _u16_table_bytes(table)
        r = self.registers
        self.protocol.write_ahb(r.AHB_SLOPE_SCAN, slope)
        self.protocol.write_ahb(r.AHB_SLOPE_FAST, slope)

    def wait_until_at_home(self, *, timeout_s: float = 60.0) -> None:
        """Poll ``0x101`` until the carriage is home and the motor is idle.

        Used after an image (or cancel) that armed ``AGOHOME`` — session 08/10
        park walk ``0xa5`` → ``0xad`` → ``0xec``.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self.read_status_reliable()
            if status.is_at_home and not status.is_motor_enabled:
                self._park_ok = True
                return
            time.sleep(HOME_POLL_S)
        raise MotorTimeoutError(f"Carriage did not reach home within {timeout_s:.0f}s (AGOHOME park)")

    def warm_prepare(self) -> None:
        """Session-10 style warm re-init while already at home (``0x02=0x78``)."""
        r = self.registers
        self._write(r.REG_0x01, 0x22)
        self._write(r.REG_0x02, 0x78)

    def _feed_capture(
        self,
        steps: int,
        *,
        timeout_s: float = 30.0,
        require_motion: bool = True,
        use_slow_slope: bool = False,
        final_positioning: bool = False,
        ignore_probe: bool = False,
        scan_regs: Mapping[int, int] | None = None,
    ) -> None:
        """Replay the captured fast-feed recipe (``docs/gl128-model-comparison.md`` section 4).

        Does **not** write ``0x0d`` before ``0x0f`` — feeds in the capture start
        with ``0x0f = 0x01`` alone. Completion is either the vendor probe
        (``wIndex=0x21``, ``_FEED_PROBE_INDEX``) returning ``0x04``, or the
        ``0x101`` status register's ``FEEDFSH`` bit with ``MOTORENB`` clear.

        ``use_slow_slope``: pass ``True`` to upload ``SLOPE_TABLE_SLOW``
        instead of the fast ramp — see ``_upload_fast_slopes``.

        ``final_positioning``: pass ``True`` for the feed to the scan-start line
        so ``0xAF`` gets the vendor's second-feed value (``_FINAL_FEED_REG_AF``).

        ``ignore_probe``: end the feed on ``FEEDFSH`` and motor stop only. SilverFast
        never reads the probe on the scan-start feed (#111); the reference feed
        still needs it.

        ``scan_regs``: the image pass's register set; the feed then runs under
        it, as SilverFast's do (see :func:`_feed_setup_regs`).
        """
        steps = max(0, int(steps))
        if steps == 0:
            return
        max_steps = int(self.model.max_feed_steps)
        if steps > max_steps:
            raise AsicError(f"Refusing FEEDL={steps}: larger than any captured feed ({max_steps}). See docs/gl128-model-comparison.md, section 4.")

        r = self.registers
        self.wait_motor_idle(f"before arming a {steps}-step feed")
        self._write_many(_feed_setup_regs(scan_regs, final_positioning=final_positioning))
        self.protocol.write_u24(r.REG_FEEDL, steps)
        self._write(r.REG_0x02, r.MTRPWR | r.FASTFED)  # 0x18
        self._upload_fast_slopes(use_slow=use_slow_slope)
        # This recipe deliberately does not clear the counter, so the probe and
        # FEEDFSH still carry the previous feed's completion. Sample them now so
        # the wait below knows not to believe the first "done" it sees.
        stale_done = self._feed_done_indicated()
        before = self.read_status_reliable()
        # Capture: start feed with 0x0f only — no 0x0d counter clear here.
        self._write(r.REG_START, r.START_GO)
        started = time.monotonic()

        self._wait_feed_probe_done(
            steps=steps,
            timeout_s=timeout_s,
            stale_done=stale_done,
            require_motion=require_motion,
            ignore_probe=ignore_probe,
        )
        done_s = time.monotonic() - started
        self._write(r.REG_0x02, r.FASTFED)  # 0x08 after move
        self.protocol.write_u24(r.REG_FEEDL, 1)
        after = self.wait_motor_idle(f"after a {steps}-step feed")
        logger.info(
            "GL128 feed of %d steps complete in %.2fs, idle at %.2fs (status 0x%02x -> 0x%02x, home %s -> %s)",
            steps,
            done_s,
            time.monotonic() - started,
            before.raw,
            after.raw,
            before.is_at_home,
            after.is_at_home,
        )

    def wait_motor_idle(self, context: str) -> ScannerStatus:
        """Poll ``0x101`` until ``MOTORENB`` clears; raise if the motor stays enabled.

        Every write of ``0x02``, a feed's registers, the slope RAM or ``START`` waits here first.
        """
        deadline = time.monotonic() + _MOTOR_IDLE_TIMEOUT_S
        while True:
            status = self.read_status_reliable()
            if not status.is_motor_enabled:
                return status
            if time.monotonic() >= deadline:
                break
            time.sleep(_MOTOR_IDLE_POLL_S)
        try:
            self._update_bits(self.registers.REG_0x02, clear_bits=self.registers.MTRPWR)
        except Exception as exc:  # noqa: BLE001
            logger.warning("GL128 motor idle timeout motor clear: %s", exc)
        raise MotorTimeoutError(
            f"GL128 motor stayed enabled for {_MOTOR_IDLE_TIMEOUT_S:.0f}s {context} "
            f"(status=0x{int(status.raw) & 0xFF:02x}). Check that the film holder moves freely."
        )

    def _read_feed_probe(self) -> int:
        try:
            return self.protocol.read_request_register(_FEED_PROBE_INDEX)
        except Exception:  # noqa: BLE001 — fall back to status
            return -1

    def _feed_done_indicated(self) -> bool:
        """True when probe/status currently claim a feed has finished."""
        if self._read_feed_probe() == _FEED_PROBE_DONE:
            return True
        status = self.read_status_reliable()
        return status.is_feeding_finished and not status.is_motor_enabled

    def _wait_feed_probe_done(
        self,
        *,
        steps: int,
        timeout_s: float,
        stale_done: bool = False,
        require_motion: bool = True,
        ignore_probe: bool = False,
    ) -> None:
        """Wait for a feed's step count to run out (probe ``0x21=0x04`` or ``FEEDFSH``).

        ``ignore_probe``: end the feed on ``FEEDFSH`` and motor stop only. The probe
        reads ``0x04`` ~50 ms after START on the second positioning feed while the
        carriage is still moving; SilverFast never reads it there.

        The motor is still decelerating at this point; :meth:`wait_motor_idle` marks
        the end of the move. With ``require_motion`` (positioning feeds) a completion
        counts only after ``0x101`` has shown ``MOTORENB``, so a stale ``0x04`` from
        the previous feed is never taken.
        """
        deadline = time.monotonic() + timeout_s
        motion_seen = False
        next_status = 0.0
        while time.monotonic() < deadline:
            if motion_seen:
                if not ignore_probe and self._read_feed_probe() == _FEED_PROBE_DONE:
                    return
                if time.monotonic() >= next_status:
                    status = self.read_status_reliable()
                    if status.is_feeding_finished and not status.is_motor_enabled:
                        return
                    next_status = time.monotonic() + HOME_POLL_S
                time.sleep(_FEED_PROBE_POLL_S)
                continue
            probe = -1 if ignore_probe else self._read_feed_probe()
            status = self.read_status_reliable()
            if not require_motion:
                if probe == _FEED_PROBE_DONE:
                    return
                if status.is_feeding_finished and not status.is_motor_enabled:
                    return
            else:
                # The probe in this iteration was read before the status that
                # shows motion, so only a later iteration's probe is current.
                motion_seen = status.is_motor_enabled
                if motion_seen:
                    next_status = time.monotonic() + HOME_POLL_S
                    continue
            time.sleep(HOME_POLL_S)
        # Feed timed out — clear SCAN and drop motor power (not an AGOHOME park).
        self.stop_motor()
        try:
            self._update_bits(self.registers.REG_0x02, clear_bits=self.registers.MTRPWR)
        except Exception as exc:  # noqa: BLE001
            logger.warning("GL128 feed timeout motor clear: %s", exc)
        if require_motion and stale_done and not motion_seen:
            raise MotorTimeoutError(
                f"Feed of {steps} steps timed out without observing motor "
                f"motion, while stale completion was already visible on the "
                f"vendor probe (wIndex=0x21 -> 0x04)."
            )
        raise MotorTimeoutError(f"Feed of {steps} steps did not finish within {timeout_s:.0f}s")

    def feed(self, steps: int, *, timeout_s: float = 30.0) -> None:
        """Move the carriage ``steps`` using the capture-faithful feed recipe."""
        self._require_motor_enabled()
        self._feed_capture(steps, timeout_s=timeout_s)

    def position_for_full_frame_scan(
        self,
        *,
        scan_steps: int | None = None,
        timeout_s: float = 60.0,
        scan_regs: Mapping[int, int] | None = None,
    ) -> None:
        """From home: feed to reference, then to the scan-start line.

        Replays ``28292`` then ``scan_steps`` (default the model's full-frame
        feed2, ``13128`` on the SE/V2; crops map ``y1`` into the scan window).
        Pass the image pass's ``scan_regs`` so both feeds run under them, as
        SilverFast's do. Requires home.
        """
        self._require_motor_enabled()
        if not getattr(self, "_park_ok", True):
            raise AsicError(
                "GL128 park failed in the previous scan (AGOHOME timed out). "
                "Power-cycle the scanner or park with SilverFast before "
                "running another SE scan."
            )
        start = self.read_status_reliable()
        if not start.is_at_home:
            raise AsicError(
                "SE carriage is not at home. There is no capture-proven "
                "standalone reverse-home; park with SilverFast or power-cycle, "
                "then retry from home."
            )
        self.warm_prepare()
        second = int(self.model.feed_to_scan_steps) if scan_steps is None else int(scan_steps)
        first = int(self.model.feed_to_reference_steps)
        logger.info(
            "GL128 positioning from home (status 0x%02x): %d then %d steps",
            start.raw,
            first,
            second,
        )
        # Vendor-verified on both models: SLOPE_TABLE_SLOW on the first
        # (reference) feed, SLOPE_TABLE_FAST on the second (final
        # positioning) feed — see _upload_fast_slopes docstring.
        self._feed_capture(
            first,
            timeout_s=min(timeout_s / 2, _REFERENCE_FEED_TIMEOUT_S),
            require_motion=True,
            use_slow_slope=True,
            scan_regs=scan_regs,
        )
        self._feed_capture(
            second,
            timeout_s=timeout_s / 2,
            require_motion=True,
            use_slow_slope=False,
            final_positioning=True,
            ignore_probe=True,
            scan_regs=scan_regs,
        )
        end = self.read_status_reliable()
        logger.info("GL128 positioned for scan (status 0x%02x)", end.raw)
        if end.is_at_home:
            raise AsicError(
                "GL128 still reads at-home after feeding the positioning pair "
                f"({first}+{second} steps). The carriage likely did not move, "
                "so the scan would not cover the film."
            )

    def stop_motor(self) -> None:
        """Abort / end an image pass the way SilverFast does (sessions 03 + 08).

        When ``AGOHOME`` is armed: lamp strobe on ``0x03``, write ``0x01=0x22``,
        finish the strobe, leave ``0x02`` / ``FEEDL`` alone, then wait for park.
        Without ``AGOHOME`` (e.g. feed timeout): clear ``SCAN`` only — no
        invented strobe. Mid-feed abort is not capture-proven.
        """
        r = self.registers
        if not self._initialized:
            logger.debug("GL128 stop_motor before init — nothing to do")
            return
        try:
            reg02 = self._reg_cache.get(r.REG_0x02)
            if reg02 is None:
                try:
                    reg02 = self.protocol.read_register(r.REG_0x02)
                except Exception:  # noqa: BLE001
                    reg02 = 0
            if reg02 & r.AGOHOME:
                for value in _CANCEL_LAMP_PRE:
                    self._write(r.REG_0x03, value)
                self._write(r.REG_0x01, _CANCEL_REG01)
                for value in _CANCEL_LAMP_POST:
                    self._write(r.REG_0x03, value)
                try:
                    self.wait_until_at_home(timeout_s=60.0)
                    self._park_ok = True
                    # Captures leave ``0x02`` alone after cancel, but drop AGOHOME
                    # from the cache so :meth:`Scanner.close`'s second
                    # ``stop_motor`` does not re-strobe the lamp / re-wait park.
                    self._reg_cache[r.REG_0x02] = int(reg02) & ~r.AGOHOME
                    logger.info("GL128 parked at home after the scan")
                except MotorTimeoutError as exc:
                    self._park_ok = False
                    logger.error(
                        "GL128 did not park at home: %s. Home is the origin "
                        "for the next scan's feeds, so power-cycle or park "
                        "with SilverFast before scanning again.",
                        exc,
                    )
            else:
                self._update_bits(r.REG_0x01, clear_bits=r.SCAN)
        except Exception as exc:  # noqa: BLE001
            self._park_ok = False
            logger.error("GL128 stop_motor: %s (carriage position unknown)", exc)

    def home(self, *, timeout_s: float = 30.0, wait: bool = True) -> None:
        """No-op when already home; otherwise refuse.

        Captures return home only via ``AGOHOME`` on the image pass
        (``0x02 = 0x30``). A standalone ``FEEDL=0`` seek is not proven and
        previously caused grinding when invented.
        """
        del timeout_s, wait
        self._require_motor_enabled()
        if self.read_status_reliable().is_at_home:
            logger.debug("GL128 already at home")
            return
        raise AsicError(
            "GL128 has no capture-proven standalone home seek. Park with "
            "SilverFast or power-cycle so the carriage is at home, then continue. "
            "See docs/gl128-model-comparison.md, section 4."
        )

    def park(self, *, timeout_s: float = 30.0) -> None:
        del timeout_s
        self._require_motor_enabled()
        if not self.read_status_reliable().is_at_home:
            raise AsicError(
                "GL128 park needs the carriage already at home (no reverse-home recipe in captures). Use SilverFast or power-cycle first."
            )
        self.lamp_off()
