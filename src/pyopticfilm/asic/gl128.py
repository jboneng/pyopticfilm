# SPDX-License-Identifier: GPL-3.0-or-later
"""GL128 driver for OpticFilm 8200i SE and 8100 (V2).

SANE genesys has no GL128 command set, so none of this is ported from SANE.
Every register write below replays what the Windows driver does in the USB
captures under ``captures/8200i-se/``; the model tables live in
``pyopticfilm.device.model_8200i_se`` (8100 V2 subclasses those tables; no IR).

Differences from :class:`~pyopticfilm.asic.gl845.Gl845` that matter here:

* status is at ``0x101`` (high-address read) rather than ``0x41``, though the
  bit layout is the same;
* the analog frontend is written through ``0x51``/``0x5D``/``0x5E``;
* the white lamp is ``0x03`` bit 4 and infrared is ``0x37`` bit 2 set by
  read-modify-write (``0x03`` bit 5 / ``AVEENB`` is held during lamp-on as in
  the captures);
* positioning is two capture-constant feeds from home, then the image pass
  runs with ``FEEDL=1`` and ``AGOHOME`` so the carriage parks afterwards —
  see ``captures/8200i-se/MOTOR.md``.
"""

from __future__ import annotations

import time
from typing import Literal

from pyopticfilm.asic.registers import Gl128Registers
from pyopticfilm.asic.status import ScannerStatus
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE, Model8200iSE
from pyopticfilm.device.tables_8200i_se import (
    SLOPE_TABLE_FAST,
    SLOPE_TABLE_SLOW,
    exposure_table,
)
from pyopticfilm.exceptions import AsicError, MotorTimeoutError, ScanError
from pyopticfilm.logging import get_logger
from pyopticfilm.scan.calib_gl128 import (
    AFE_ENDPIXEL,
    AFE_GAIN_MIN,
    AFE_STRIP_BYTES,
    AFE_STRPIXEL,
    AFE_WIDE_BYTES,
    AFE_WIDE_ENDPIXEL,
    AFE_WIDE_PIXELS,
    AHB_SHADING,
    COLOR_AFE_SESSION04_GAINS,
    COLOR_SHADING_DARK_MEAN_MAX,
    COLOR_SHADING_DARK_SETTLE_S,
    IR_SHADING_DARK_SETTLE_S,
    SHADING_GAIN_UNITY,
    SHADING_LINES,
    AfeFrontend,
    AfeSearchConfig,
    adaptive_afe_gain_target,
    average_rgb16_columns,
    build_measured_shading_table,
    channel_means_u16,
    choose_usb_planar,
    coarse_offsets_from_wide_means,
    declared_shading_size,
    equalize_ir_white_columns,
    make_unity_white_table,
    search_afe_codes,
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

#: Session 05 IR pass settled near these FE codes when home IR is too dim for
#: the dichotomy target — prefer them over mid-probe 0x80 / searched offsets.
IR_AFE_FALLBACK_OFFSETS: tuple[int, int, int] = (6, 16, 12)
IR_AFE_FALLBACK_GAINS: tuple[int, int, int] = (0x30, 0x29, 0x31)
#: Pegged at max gain with at least this mean is already usable — keep those
#: gains (dropping to ``IR_AFE_FALLBACK_GAINS`` starved shading whites and
#: DVDSET clipped the image to full scale).
IR_AFE_HEALTHY_MEAN = 8000
#: Colour peg fallback uses mid-probe gains; keep dichotomy offsets.

BRINGUP_HINT = (
    "GL128 (OpticFilm 8200i SE / 8100 V2) support is derived from USB captures "
    "of the Windows driver rather than from SANE — see docs/gl128-bringup.md."
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

#: Vendor probe ``wIndex`` polled during fast feeds until it returns this.
_FEED_PROBE_INDEX = 0x21
_FEED_PROBE_DONE = 0x04
#: How long a feed may go without reporting motion before its start-up state is
#: taken at face value. The probe and ``FEEDFSH`` both survive the previous
#: feed, so a completion seen inside this window is the *old* move's.
_FEED_START_TIMEOUT_S = 2.0

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

#: Two 34-byte blobs the Windows driver writes to ``0x000FFF00`` and
#: ``0x000FFF01`` before the register blast. Their meaning is unknown; they are
#: replayed byte-for-byte because boot is not reproducible without them.
_BOOT_BLOB_ADDR_A = 0x000FFF00
_BOOT_BLOB_ADDR_B = 0x000FFF01
_BOOT_BLOB_A = bytes(34)
_BOOT_BLOB_B = bytes(32) + bytes((0x33, 0x00))

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


#: Default adaptive quiet USB drain on GL128 image acquire (on/off sentinel).
#: Any value ``> 0`` enables LPERIOD-matched pacing; ``0`` is flat-out drain
#: (louder motor). Not a per-line sleep ceiling — ``_acquire`` sleeps the full
#: line-period deficit (plus a small lag) when the host outran the ASIC.
DEFAULT_IMAGE_USB_PACE_S = 0.003


class Gl128:
    """GL128 chip operations for OpticFilm 8200i SE and 8100 (V2)."""

    def __init__(
        self,
        protocol: GenesysUsbProtocol,
        model: Model8200iSE = MODEL_8200I_SE,
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
        #: IR Lab never sets this — ASIC DVDSET clipped IR to full scale.
        self.asic_shading_ready = False
        #: EXPERIMENTAL, default off. Positioning feeds (home->reference,
        #: reference->scan-start) always upload SLOPE_TABLE_FAST — the
        #: fastest ramp in the whole capture set, real vendor-driver
        #: behavior, never toggled by anything (unlike the image-pass creep,
        #: which already has ``image_slope_slow``). A benchmark found
        #: ~8px mean / up to 220px max Y-only drift between otherwise
        #: identical repeat scans, consistent with occasional lost steps
        #: during an aggressive feed ramp. Flip this on to try
        #: SLOPE_TABLE_SLOW for feeds instead and A/B the drift before
        #: considering it for a real fix — this changes real hardware motor
        #: behavior, not just image processing.
        self.experimental_feed_slope_slow: bool = False
        #: Equalized per-column IR white (one value/column) for host flatten.
        self.last_ir_host_white: list[int] | None = None
        #: True when :attr:`last_ir_host_white` passed the IR validator.
        self.ir_host_flatten_ready: bool = False
        #: Last successful :meth:`search_afe` result; image configure re-applies
        #: it so scan setup does not wipe calibrated FE gains back to boot zero.
        self.last_afe: AfeFrontend | None = None
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
        #: Quiet USB drain flag: ``> 0`` paces bulk reads to ``LPERIOD``;
        #: ``0`` drains flat-out (louder motor creep).
        self.image_usb_pace_s: float = DEFAULT_IMAGE_USB_PACE_S

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
        """Write per-channel FE offsets (``0x02``–``0x04``) and/or gains (``0x05``–``0x07``)."""
        fe = AfeFrontend(
            offsets=offsets if offsets is not None else (0, 0, 0),
            gains=gains if gains is not None else (0, 0, 0),
        )
        for index, value in fe.as_fe_writes():
            if offsets is None and index <= 0x04:
                continue
            if gains is None and index >= 0x05:
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

    def _setup_afe_strip_regs(self, *, wide: bool = False) -> None:
        """Stationary AFE strip geometry (session 03 window, motor off)."""
        r = self.registers
        self._apply_stationary_scan_regs()
        dpi_calib = self.model.optical_resolution // 6
        end = AFE_WIDE_ENDPIXEL if wide else AFE_ENDPIXEL
        self.protocol.write_u24(r.REG_LINCNT, 1)
        self.protocol.write_u16(r.REG_DPISET, dpi_calib)
        self.protocol.write_u24(r.REG_STRPIXEL, AFE_STRPIXEL)
        self.protocol.write_u24(r.REG_ENDPIXEL, end)
        self.protocol.write_u24(r.REG_FEEDL, 1)
        self.protocol.write_u24(r.REG_LPERIOD, int(self.model.exposure_lperiod))
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
    ) -> bytes:
        """Read one stationary 16-bit AFE strip. Does not move the carriage."""
        if not self._initialized:
            self.init()
        r = self.registers
        size = int(size)
        if size <= 0:
            raise ValueError("AFE strip size must be positive")

        self._setup_afe_strip_regs(wide=size >= AFE_WIDE_BYTES)
        # Capture start recipe: 0x0d → SCAN → 0x0f (no motor).
        self._write(r.REG_CLRCNT, r.CLRCNT_ALL)
        self._update_bits(r.REG_0x01, set_bits=r.SCAN)
        self._write(r.REG_START, r.START_GO)

        try:
            self._wait_stationary_data_ready(timeout_s, where="AFE strip")
        except ScanError:
            self._update_bits(r.REG_0x01, clear_bits=r.SCAN)
            raise

        self.protocol.bulk_read_begin(size, index=r.BULK_INDEX_RAM, addr=r.AHB_CHANNEL_R)
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
        config: AfeSearchConfig | None = None,
        method: ScanMethod | None = None,
    ) -> AfeFrontend:
        """Run stationary offset then gain dichotomy; leave FE at the result.

        Motor stays gated/off. Colour AFE keeps the lamp on through wide/coarse
        (SF session 03/04). Infrared turns the white lamp off and uses the IR LED.
        """
        if not self._initialized:
            self.init()
        if method is not None:
            self.set_scan_method(method)
        cfg = config or AfeSearchConfig()

        # AFE strips may look planar or chunky; film *image* USB is chunky on SE
        # (session 11). Never copy the AFE probe into ``usb_planar_rgb`` — that
        # flag drives image assemble + shading averages. Dark IR probes often
        # fail the lag test and wrongly pick planar → barcode IR / rainbow.
        infrared = self._scan_method == "infrared"
        # Capture timeline: lamp on for colour mid probe + wide/coarse + gains.
        self.lamp_on()
        time.sleep(0.3)
        self.apply_frontend(AfeFrontend(offsets=(0, 0, 0), gains=(0x80, 0x80, 0x80)))
        probe = self.acquire_afe_strip(AFE_STRIP_BYTES)
        mean_planar = channel_means_u16(probe, planar=True)
        mean_chunky = channel_means_u16(probe, planar=False)

        def _imbalance(means: tuple[float, float, float]) -> float:
            lo = max(1.0, min(means))
            return max(means) / lo

        # Lag test plus channel balance: a dark IR strip often fails the lag
        # test and picks planar, which then drives the FE search off a bad
        # mean and leaves colour/IR underexposed after the 0x80 fallback.
        lag_planar = choose_usb_planar(probe)
        afe_planar = lag_planar
        if _imbalance(mean_planar if lag_planar else mean_chunky) > 2.0:
            afe_planar = _imbalance(mean_planar) <= _imbalance(mean_chunky)
        probe_means = mean_planar if afe_planar else mean_chunky
        gain_target = adaptive_afe_gain_target(probe_means)
        self.usb_planar_rgb = bool(getattr(self.model, "usb_planar_rgb", False))
        logger.info(
            "GL128 AFE strip layout → %s (means only); image/shading layout → %s; "
            "probe_means=(%.0f,%.0f,%.0f); gain_target=%#x (capture default %#x)",
            "planar" if afe_planar else "chunky",
            "planar" if self.usb_planar_rgb else "chunky",
            probe_means[0],
            probe_means[1],
            probe_means[2],
            int(gain_target),
            cfg.gain_target,
        )

        def measure(fe: AfeFrontend) -> tuple[float, float, float]:
            self.apply_frontend(fe)
            strip = self.acquire_afe_strip(AFE_STRIP_BYTES)
            return channel_means_u16(strip, planar=afe_planar)

        offset_seed = (0, 0, 0)
        if infrared:
            # IR offset hunt under white lamp off / IR LED on.
            self.lamp_off()
            time.sleep(0.2)

            def apply_offsets(offsets: tuple[int, int, int]) -> tuple[float, float, float]:
                return measure(AfeFrontend(offsets=offsets, gains=(0x80, 0x80, 0x80)))

            offsets = search_afe_codes(
                initial=offset_seed,
                code_max=cfg.offset_max,
                target=float(cfg.offset_target),
                iterations=cfg.iterations,
                tolerance=cfg.tolerance,
                code_increases_mean=cfg.offset_increases_mean,
                apply=apply_offsets,
            )
            self.lamp_on()
            time.sleep(0.5)
        else:
            # SF: wide strip + coarse offsets with lamp still on; no dichotomy.
            self.apply_frontend(AfeFrontend(offsets=(0, 0, 0), gains=(0x80, 0x80, 0x80)))
            wide = self.acquire_afe_strip(AFE_WIDE_BYTES)
            wide_means = channel_means_u16(wide, pixels=AFE_WIDE_PIXELS, planar=afe_planar)
            offsets = coarse_offsets_from_wide_means(wide_means)
            offset_seed = offsets
            logger.info(
                "GL128 AFE wide strip (lamp on) means=(%.0f,%.0f,%.0f) coarse_offsets=%s",
                wide_means[0],
                wide_means[1],
                wide_means[2],
                offsets,
            )
            time.sleep(0.5)

        def apply_gains(gains: tuple[int, int, int]) -> tuple[float, float, float]:
            return measure(AfeFrontend(offsets=offsets, gains=gains))

        gain_floor = 0 if infrared else AFE_GAIN_MIN
        gains = search_afe_codes(
            initial=(0x80, 0x80, 0x80),
            code_max=cfg.gain_max,
            code_min=gain_floor,
            target=float(gain_target),
            iterations=cfg.iterations,
            tolerance=cfg.tolerance,
            code_increases_mean=cfg.gain_increases_mean,
            apply=apply_gains,
        )
        result = AfeFrontend(offsets=offsets, gains=gains)
        # Near-max (incl. 509 of 511) means the target was unreachable — fall back.
        peg_floor = cfg.gain_max - 2
        if all(g >= peg_floor for g in result.gains):
            pegged_means = measure(result)
            pegged_gains = result.gains
            if infrared:
                if min(pegged_means) >= IR_AFE_HEALTHY_MEAN:
                    result = AfeFrontend(
                        offsets=IR_AFE_FALLBACK_OFFSETS,
                        gains=pegged_gains,
                    )
                    fallback_label = "session-05 offsets + keeping pegged gains"
                else:
                    result = AfeFrontend(
                        offsets=IR_AFE_FALLBACK_OFFSETS,
                        gains=IR_AFE_FALLBACK_GAINS,
                    )
                    fallback_label = "session-05 IR offsets+gains"
            else:
                result = AfeFrontend(offsets=offsets, gains=(0x80, 0x80, 0x80))
                fallback_label = "kept offsets + mid-probe 0x80"
            logger.warning(
                "GL128 AFE gains pegged at max %s with means=(%.0f,%.0f,%.0f); falling back to %s (offsets=%s gains=%s)",
                pegged_gains,
                pegged_means[0],
                pegged_means[1],
                pegged_means[2],
                fallback_label,
                result.offsets,
                result.gains,
            )
        elif not infrared and any(g >= peg_floor for g in result.gains):
            # One runaway channel is as bad as three: at the rail its dark term
            # clips to 0 and the whole frame takes that channel's cast. SF's own
            # codes are the safe stand-in for a channel the search cannot reach.
            safe = [COLOR_AFE_SESSION04_GAINS[c] if g >= peg_floor else g for c, g in enumerate(result.gains)]
            capped: tuple[int, int, int] = (safe[0], safe[1], safe[2])
            logger.warning(
                "GL128 AFE gain pegged on some channels %s (target %#x unreachable); using session-04 codes there → %s",
                result.gains,
                int(gain_target),
                capped,
            )
            result = AfeFrontend(offsets=offsets, gains=capped)
        elif not infrared and (result.offsets == (0, 0, 0) or result.gains == (0, 0, 0)):
            collapsed = []
            if result.offsets == (0, 0, 0):
                collapsed.append("offsets")
                if offset_seed != (0, 0, 0):
                    restored_offsets = offset_seed
                    offset_src = f"offset_seed={offset_seed}"
                else:
                    restored_offsets = (38, 30, 36)
                    offset_src = "hardcoded baseline (38,30,36)"
            else:
                restored_offsets = result.offsets
                offset_src = "search result"
            if result.gains == (0, 0, 0):
                collapsed.append("gains")
                restored_gains = COLOR_AFE_SESSION04_GAINS
                gain_src = f"session-04 codes {COLOR_AFE_SESSION04_GAINS}"
            else:
                restored_gains = result.gains
                gain_src = "search result"
            result = AfeFrontend(offsets=restored_offsets, gains=restored_gains)
            logger.warning(
                "GL128 AFE search collapsed to zeros for %s; "
                "restored offsets=%s [%s] gains=%s [%s]; "
                "image colour may be degraded — recalibrate if quality is poor",
                "/".join(collapsed),
                result.offsets, offset_src,
                result.gains, gain_src,
            )
        self.apply_frontend(result)
        self.last_afe = result
        final_means = measure(result)
        logger.info(
            "GL128 AFE search done offsets=%s gains=%s means=(%.0f,%.0f,%.0f) targets offset=%#x gain=%#x planar=%s",
            result.offsets,
            result.gains,
            final_means[0],
            final_means[1],
            final_means[2],
            cfg.offset_target,
            int(gain_target),
            self.usb_planar_rgb,
        )
        return result

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
        clocks = getattr(self.model, "shading_strip_clocks", None)
        if callable(clocks):
            dummy, clk_a, clk_b = clocks(int(resolution), dvdset=dvdset)
        else:
            asic_dpi = self.model.asic_dpi_for(int(resolution))
            dummy_map = getattr(self.model, "dummy_by_dpi", None)
            clock_map = getattr(self.model, "pixel_clock_by_dpi", None)
            dummy = int(dummy_map.get(asic_dpi, 0x02)) if dummy_map else 0x02
            clk = int(clock_map.get(asic_dpi, 0x02)) if clock_map else 0x02
            clk_a = clk_b = clk
        # Session 04 leaves the geometry standing between the dark strip and the
        # DVDSET white; write it on both so a white strip never inherits a stale
        # window (the values are identical when the caller passes one window).
        if not dvdset:
            self._apply_stationary_scan_regs()
        self._write(0x2B, int(dummy))
        self._write(0xA5, int(clk_a))
        self._write(0xAB, int(clk_b))
        self.protocol.write_u24(r.REG_LINCNT, int(lines))
        self.protocol.write_u16(r.REG_DPISET, dpi_calib)
        self.protocol.write_u24(r.REG_STRPIXEL, start)
        self.protocol.write_u24(r.REG_ENDPIXEL, end)
        self.protocol.write_u24(r.REG_FEEDL, 1)
        self.protocol.write_u24(r.REG_LPERIOD, int(self.model.line_period_for(int(resolution))))
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
                int(self.model.line_period_for(int(resolution))),
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

        self.protocol.bulk_read_begin(size, index=r.BULK_INDEX_RAM, addr=r.AHB_CHANNEL_R)
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

        Colour white strip: DVDSET + ``MTRPWR|AGOHOME`` (AGOHOME during the
        measure so SCAN-clear parks; SF is MTRPWR-only). Exposure AHB only
        after unity; dark/white use capture clocks.

        Pass ``method=\"infrared\"`` so the white strip runs under the IR LED
        (session 05: IR table uses zero dark terms + near-equal whites).

        Pass the image ``strpixel``/``endpixel``/``dpiset`` so shading uses the
        same origin as the scan. The table covers exactly the measured columns —
        a table measured through a different window is indexed differently from
        the image and DVDSET turns that into periodic dropouts.

        For ``method=\"infrared\"``, the optical dark strip is diagnostic only —
        the uploaded table dark is forced to zero (session 05). Live ASIC DVDSET
        clipped IR to full scale, so infrared **never** sets
        ``asic_shading_ready``; a validated white profile is stored for host
        flatten instead (:attr:`ir_host_flatten_ready`).

        Refuses if ``_motor_moves_enabled`` — Lab must disarm before shading and
        re-arm only for the image feeds (capture order: shade, then motor).
        """
        if not self._initialized:
            self.init()
        if self._motor_moves_enabled:
            raise ScanError("ASIC shading requires motor disarmed — call disarm_bringup_motor before run_asic_shading")
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
        self.upload_tables(resolution=resolution, shading=False, slope=False)
        if not infrared:
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
        # SF session 03: after unity, white strip with 0x01=0x23 (SHDAREA|DVDSET).
        # At unity gain the ASIC returns ``raw - dark``, so ~50-57k here is the
        # expected reading; the gains below turn that into the flattening table.
        white_raw = self.acquire_shading_strip(resolution=resolution, pixels=n, dvdset=not infrared, **window)
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
        # IR: never arm ASIC DVDSET (live HW clipped to 0xFFFF). Store a host
        # white profile when the table validates; image flatten uses that.
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

    def upload_tables(
        self,
        *,
        resolution: int,
        shading: bool = False,
        slope: bool = True,
        channel_exposure: int | None = None,
    ) -> None:
        """Upload motor slope and/or per-channel exposure tables to scanner RAM.

        ``shading=True`` loads the slow ramp (session 03 feeds); otherwise the
        fast ramp used for feeds and image. Pass ``slope=False`` for the
        unity→white window (session 03/04: exposure AHB only).

        When ``image_slope_slow`` is set (Scan Lab acoustic probe), non-shading
        uploads also use the slow ramp. Feeds still call
        :meth:`_upload_feed_slopes` directly (see ``experimental_feed_slope_slow``).
        """
        r = self.registers
        if slope:
            use_slow = bool(shading or getattr(self, "image_slope_slow", False))
            slope_bytes = _u16_table_bytes(SLOPE_TABLE_SLOW if use_slow else SLOPE_TABLE_FAST)
            self.protocol.write_ahb(r.AHB_SLOPE_SCAN, slope_bytes)
            self.protocol.write_ahb(r.AHB_SLOPE_FAST, slope_bytes)

        if channel_exposure is None:
            channel_exposure = self.model.channel_exposure_for(resolution)
        exposure = _u16_table_bytes(exposure_table(int(channel_exposure)))
        for addr in (r.AHB_CHANNEL_R, r.AHB_CHANNEL_G, r.AHB_CHANNEL_B):
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
        ``0x10``, so neither does this.
        """
        del cold
        self.protocol.write_ahb(_BOOT_BLOB_ADDR_A, _BOOT_BLOB_A)
        self.protocol.write_ahb(_BOOT_BLOB_ADDR_B, _BOOT_BLOB_B)
        self._write_many(dict(self.model.init_regs))
        self._write_many(dict(self.model.memory_layout_regs))
        self.set_frontend_init()
        self._write_many(dict(self.model.gpo_regs))
        logger.info(
            "GL128 boot: %d init + %d layout + %d gpo registers",
            len(self.model.init_regs),
            len(self.model.memory_layout_regs),
            len(self.model.gpo_regs),
        )

    def init(self, *, force: bool = False) -> None:
        if self._initialized and not force:
            return
        self.asic_boot()
        self.upload_tables(resolution=max(self.model.resolutions_dpi))
        self.last_afe = None
        self.asic_shading_ready = False
        self.last_ir_host_white = None
        self.ir_host_flatten_ready = False
        self.usb_planar_rgb = False
        self._initialized = True
        logger.info("GL128 initialised (%s)", self.model.model)

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
        else:
            self._update_bits(r.REG_IR, clear_bits=r.IR_LED)

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

    def _upload_feed_slopes(self) -> None:
        """Upload the feed motor ramp to both AHB slope windows.

        Real vendor behavior is always ``SLOPE_TABLE_FAST`` here. See
        ``experimental_feed_slope_slow`` for the (unconfirmed) A/B toggle.
        """
        use_slow = getattr(self, "experimental_feed_slope_slow", False)
        slope = _u16_table_bytes(SLOPE_TABLE_SLOW if use_slow else SLOPE_TABLE_FAST)
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
    ) -> None:
        """Replay the captured fast-feed recipe (see MOTOR.md).

        Does **not** write ``0x0d`` before ``0x0f`` — feeds in the capture start
        with ``0x0f = 0x01`` alone. Completion is the vendor probe at
        ``wIndex=0x21`` returning ``0x04``.
        """
        steps = max(0, int(steps))
        if steps == 0:
            return
        max_steps = int(self.model.max_feed_steps)
        if steps > max_steps:
            raise AsicError(f"Refusing FEEDL={steps}: larger than any captured feed ({max_steps}). See captures/8200i-se/MOTOR.md.")

        r = self.registers
        setup = dict(_FEED_SETUP_REGS)
        self._write_many(setup)
        self.protocol.write_u24(r.REG_FEEDL, steps)
        self._write(r.REG_0x02, r.MTRPWR | r.FASTFED)  # 0x18
        self._upload_feed_slopes()
        # This recipe deliberately does not clear the counter, so the probe and
        # FEEDFSH still carry the previous feed's completion. Sample them now so
        # the wait below knows not to believe the first "done" it sees.
        stale_done = self._feed_done_indicated()
        before = self.read_status_reliable()
        # Capture: start feed with 0x0f only — no 0x0d counter clear here.
        self._write(r.REG_START, r.START_GO)

        # Sanity-check: require motion to last at least a fraction of the
        # reference feed time. This caps the minimum to keep tests responsive.
        min_motion_s: float | None = None
        if require_motion:
            ref_steps = max(1, int(self.model.feed_to_reference_steps))
            # Session 03 shows ~1s for 28292 steps; we keep this conservative.
            expected_s = 1.0 * (steps / ref_steps) * 0.9
            min_motion_s = min(0.25, max(0.05, expected_s))

        self._wait_feed_probe_done(
            steps=steps,
            timeout_s=timeout_s,
            stale_done=stale_done,
            require_motion=require_motion,
            min_motion_s=min_motion_s,
        )
        self._write(r.REG_0x02, r.FASTFED)  # 0x08 after move
        self.protocol.write_u24(r.REG_FEEDL, 1)
        after = self.read_status_reliable()
        logger.info(
            "GL128 feed of %d steps complete (status 0x%02x -> 0x%02x, home %s -> %s)",
            steps,
            before.raw,
            after.raw,
            before.is_at_home,
            after.is_at_home,
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
        min_motion_s: float | None = None,
    ) -> None:
        """Wait for a feed to finish.

        When ``require_motion`` is true (positioning feeds), the wait is
        capture-faithful: it refuses to accept a stale ``0x21=0x04``
        completion. Instead, it requires observing motor motion on the
        `0x101` status register at least once before accepting completion.
        """
        deadline = time.monotonic() + timeout_s
        motion_seen = False
        motion_start_t: float | None = None
        while time.monotonic() < deadline:
            probe = self._read_feed_probe()
            status = self.read_status_reliable()
            if not require_motion:
                if probe == _FEED_PROBE_DONE:
                    return
                if status.is_feeding_finished and not status.is_motor_enabled:
                    return
            else:
                if not motion_seen:
                    # Phase 1: observe motion (use 0x101's MOTORENB bit).
                    if status.is_motor_enabled:
                        motion_seen = True
                        motion_start_t = time.monotonic()
                else:
                    # Phase 2: observe completion, but optionally wait a minimum
                    # motion duration so we do not accept a stale completion
                    # too early.
                    if min_motion_s is not None and motion_start_t is not None and (time.monotonic() - motion_start_t) < min_motion_s:
                        time.sleep(HOME_POLL_S)
                        continue
                    if probe == _FEED_PROBE_DONE:
                        return
                    if status.is_feeding_finished and not status.is_motor_enabled:
                        return
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
    ) -> None:
        """From home: feed to reference, then to the scan-start line.

        Replays ``28292`` then ``scan_steps`` (default session-04 full-frame
        ``13704``; crop-dependent values from session 09). Requires home.
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
        self._feed_capture(first, timeout_s=timeout_s / 2, require_motion=True)
        self._feed_capture(second, timeout_s=timeout_s / 2, require_motion=True)
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
            "See captures/8200i-se/MOTOR.md."
        )

    def park(self, *, timeout_s: float = 30.0) -> None:
        del timeout_s
        self._require_motor_enabled()
        if not self.read_status_reliable().is_at_home:
            raise AsicError(
                "GL128 park needs the carriage already at home (no reverse-home recipe in captures). Use SilverFast or power-cycle first."
            )
        self.lamp_off()
