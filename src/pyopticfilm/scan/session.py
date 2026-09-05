# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan state machine: configure → acquire → assemble."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from pyopticfilm.asic.motor import (
    SLOPE_TABLE_AHB,
    calculate_zmod,
    create_fast_slope_table,
    create_scan_slope_table,
    slope_table_to_bytes,
)
from pyopticfilm.asic.registers import Gl845Registers
from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.device.protocol import AsicDriver, FilmModel
from pyopticfilm.device.sensor_lookup import (
    exposure_lperiod_for,
    maxwd_register_value,
    sensor_regs_for,
)
from pyopticfilm.exceptions import MotorTimeoutError, ScanCancelled, ScanError
from pyopticfilm.image import ScanImage
from pyopticfilm.logging import get_logger
from pyopticfilm.scan.calibrate import Calibrator
from pyopticfilm.scan.geometry import ScanGeometry, compute_geometry
from pyopticfilm.scan.pipeline import ImagePipeline

logger = get_logger(__name__)

FEED_TIMEOUT_S = 60.0
DATA_TIMEOUT_S = 30.0
BULK_CHUNK_LINES = 8


class ScanSession:
    """Owns one color/IR transparency scan from configure → TIFF-ready buffer.

    This class implements the SANE ``CommandSetGl846`` (GL845) OpticFilm path.
    GL843 / GL842 subclasses override optical register quirks (MAXWD, LPERIOD).
    """

    #: When True, divide exposure by ``tgtime`` before writing LPERIOD (GL843/GL842).
    _lperiod_divide_tgtime: bool = False

    def __init__(
        self,
        asic: AsicDriver,
        model: FilmModel = MODEL_8200I,
        calibrator: Calibrator | None = None,
    ) -> None:
        self.asic = asic
        self.model = model
        self.pipeline = ImagePipeline(model)
        self.regs = Gl845Registers()
        self.calibrator = calibrator
        self._lamp_requested = True

    def run(
        self,
        *,
        resolution: int = 1800,
        mode: str = "color",
        area: tuple[float, float, float, float] | None = None,
        geometry: ScanGeometry | None = None,
        progress: Callable[[float], None] | None = None,
        cancel: threading.Event | None = None,
        apply_calib: bool = True,
        multi_exposure: bool = False,
        infrared: bool = False,
        align_passes: bool = True,
        me_exposure_mode: str | None = None,  # GL128 ME only; ignored here
        single_pass_exposure: int | None = None,
        me_short_exposure: int | None = None,  # GL128 ME only; ignored here
        me_long_exposure: int | None = None,  # GL128 ME only; ignored here
        me_target_exposure: int | None = None,  # GL128 ME only; ignored here
        n_brackets: int = 2,  # GL128 ME only; ignored here
    ) -> ScanImage:
        if multi_exposure or infrared:
            raise NotImplementedError(
                f"Multi-exposure / combined IR scans are not implemented for "
                f"{getattr(self.model, 'asic', '?')}"
            )
        if single_pass_exposure is not None:
            raise NotImplementedError(
                "Manual exposure overrides are only implemented for GL128; "
                f"{getattr(self.model, 'asic', '?')} does not support single_pass_exposure."
            )
        if mode == "gray":
            raise ValueError("Grayscale is experimental / not implemented yet")
        if mode not in {"color", "infrared"}:
            raise ValueError(f"Unsupported mode {mode!r} (color|infrared)")
        if mode == "infrared" and not self.model.supports_infrared:
            raise ValueError(f"{self.model.model} does not support infrared")

        if not self.asic._initialized:
            self.asic.init()

        method = "infrared" if mode == "infrared" else "transparency"
        if geometry is None:
            geometry = compute_geometry(resolution, model=self.model, area=area)
            # Re-bind exposure for IR when the model has method-keyed LPERIOD.
            from dataclasses import replace as dc_replace

            from pyopticfilm.device.sensor_lookup import exposure_lperiod_for

            lperiod = exposure_lperiod_for(
                self.model, geometry.resolution, method=method
            )
            if lperiod != geometry.exposure_lperiod:
                geometry = dc_replace(geometry, exposure_lperiod=lperiod)
        logger.info(
            "scan %sdpi %s pixels=%d lines=%d starty=%d bytes=%d stagger=%d",
            geometry.resolution,
            mode,
            geometry.pixels,
            geometry.lines,
            geometry.starty,
            geometry.total_bytes,
            geometry.num_staggered_lines,
        )

        calib = None
        if apply_calib and self.calibrator is not None:
            is_gl128 = getattr(self.model, "asic", "") == "GL128"
            if is_gl128 and method == "transparency":
                # SilverFast / SE: ASIC shading before any image feed.
                calib = self.calibrator.ensure_colour_asic_shading(geometry)
            elif is_gl128:
                calib = self.calibrator.find_for_scan(method=method, geometry=geometry)
                if calib is None:
                    logger.warning(
                        "No calib cache for method=%s dpi=%d — scanning uncalibrated.",
                        method,
                        geometry.resolution,
                    )
            else:
                # Genesys (GL845/843/842): host dark/white stretch — never the
                # SE ``run_asic_shading`` path (those ASICs do not implement it).
                calib = self.calibrator.ensure_host_calib(
                    geometry,
                    method=method,
                    mode="infrared" if method == "infrared" else "color",
                )

        raw = self.acquire_raw(
            geometry,
            method=method,
            lamp_on=True,
            start_motor=True,
            progress=progress,
            cancel=cancel,
        )

        use_host = (
            calib is not None
            and self.calibrator is not None
            and self.calibrator.should_apply_host_calib()
        )
        dark = calib.dark if use_host else None
        white = calib.white if use_host else None
        if calib is not None and not use_host:
            logger.info("Using ASIC shading; skipping host dark/white stretch")
        # Prefer the model USB layout (SE film = chunky). ``asic.usb_planar_rgb``
        # is kept in sync with the model after AFE; Comm-test may override it
        # after a scored decode.
        planar = getattr(self.asic, "usb_planar_rgb", None)
        if planar is None:
            planar = bool(getattr(self.model, "usb_planar_rgb", False))
        rgb = self.pipeline.assemble(
            raw, geometry, dark=dark, white=white, planar=bool(planar)
        )

        ir_plane = None
        if mode == "infrared":
            # CCD still delivers RGB under the IR LED; NegPy / iSRD use G.
            ir_plane = self._infrared_plane(rgb)

        return ScanImage(
            rgb=rgb,
            dpi=geometry.resolution,
            device_model=f"{self.model.vendor} {self.model.model}",
            ir=ir_plane,
        )

    def _infrared_plane(self, rgb):
        """Build the HxW IR sidecar: green CCD plane + optional host flatten."""
        import numpy as np

        from pyopticfilm.scan.calib_gl128 import (
            flatten_ir_columns,
            flatten_ir_image_columns,
        )

        plane = np.ascontiguousarray(rgb[:, :, 1])
        white = getattr(self.asic, "last_ir_host_white", None)
        if getattr(self.asic, "ir_host_flatten_ready", False) and white:
            try:
                plane = flatten_ir_columns(plane, white)
            except ValueError as exc:
                logger.warning("IR host-white flatten skipped: %s", exc)
        # Per-image column flatten lifts residual L/R falloff toward the
        # SilverFast IR page level so dust sits in a dark hole on a bright field.
        if getattr(self.model, "asic", "") == "GL128":
            plane = flatten_ir_image_columns(plane)
        return plane

    def acquire_raw(
        self,
        geometry: ScanGeometry,
        *,
        method: str,
        lamp_on: bool,
        start_motor: bool,
        progress: Callable[[float], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> bytes:
        """Configure ASIC, run one capture, return raw optical bytes."""
        self.asic.set_scan_method(method)  # type: ignore[arg-type]
        # Recorded for subclasses that must reflect lamp state in the register
        # block they write during _configure.
        self._lamp_requested = lamp_on
        if lamp_on:
            self.asic.lamp_on()
        else:
            self.asic.lamp_off()

        try:
            self._configure(geometry)
            self._begin_scan(start_motor=start_motor)
            return self._acquire(
                geometry,
                progress=progress,
                cancel=cancel,
                wait_feed=start_motor and getattr(self, "_feedl", 0) > 0,
            )
        finally:
            self._end_scan()

    # --- configure ------------------------------------------------------

    def _configure(self, geometry: ScanGeometry) -> None:
        proto = self.asic.protocol
        r = self.regs
        if self.asic._reg_cache:
            cache = dict(self.asic._reg_cache)
        else:
            cache = self.model.boot_register_map()

        method = getattr(self.asic, "_scan_method", "transparency")
        for addr, value in sensor_regs_for(self.model, geometry.resolution).items():
            cache[addr] = value

        apply_fe = getattr(self.asic, "apply_frontend_for_scan", None)
        if callable(apply_fe):
            apply_fe(resolution=geometry.resolution, method=method)
        else:
            self.asic.set_frontend_init()

        # Optical: SHDAREA on, DVDSET off (host-side calib), SCAN off
        cache[0x01] = (cache.get(0x01, 0x22) | r.SHDAREA) & ~r.DVDSET & ~r.SCAN

        if method == "infrared":
            cache[0x03] = cache.get(0x03, 0xBF) & ~r.LAMPPWR
        else:
            cache[0x03] = cache.get(0x03, 0xBF) | r.LAMPPWR
        cache[0x03] = cache[0x03] & ~0x40  # clear AVEENB

        # 0x04: 16-bit color — BITSET, AFEMOD=2, FESET=2 (ADI)
        cache[0x04] = (cache.get(0x04, 0x22) & ~0x8C) | 0x40 | 0x20

        # dpihw 1200, clear gamma (GMMENB)
        dpihw = int(getattr(self.model, "register_dpihw", 1200))
        dpihw_bits = {600: 0x00, 1200: 0x40, 2400: 0x80, 4800: 0xC0}.get(dpihw, 0x40)
        cache[0x05] = (cache.get(0x05, 0x48) & ~0xC0 & ~0x08) | dpihw_bits

        cache[0x2E] = 0x7F
        cache[0x2F] = 0x7F

        lperiod = int(geometry.exposure_lperiod)
        if lperiod <= 0:
            lperiod = exposure_lperiod_for(
                self.model, geometry.resolution, method=str(method)
            )
        tgtime = 1 << (cache.get(0x1C, 0) & 0x07)
        if self._lperiod_divide_tgtime and tgtime > 1:
            lperiod = max(1, lperiod // tgtime)

        self._set16(cache, 0x2C, geometry.register_dpiset)
        self._set16(cache, 0x30, geometry.pixel_startx)
        self._set16(cache, 0x32, geometry.pixel_endx)
        self._set16(cache, 0x38, lperiod)
        cache[0x34] = geometry.dummy_pixel
        maxwd = maxwd_register_value(
            self.model, line_bytes=geometry.line_bytes, channels=geometry.channels
        )
        self._set24(cache, 0x35, maxwd)
        self._set24(cache, 0x25, geometry.lincnt_register)

        step_mult = 1
        mp = self.model.motor_profile
        scan_slope = create_scan_slope_table(
            ydpi=geometry.resolution,
            exposure=int(geometry.exposure_lperiod),
            base_ydpi=self.model.motor_base_ydpi,
            step_multiplier=step_mult,
            profile=mp,
        )
        fast_slope = create_fast_slope_table(step_multiplier=step_mult, profile=mp)

        cache[0x02] = r.MTRPWR
        if geometry.disable_buffer_full_move:
            cache[0x02] |= r.ACDCDIS

        n_scan = len(scan_slope.table) // step_mult
        n_fast = len(fast_slope.table) // step_mult
        cache[0x21] = n_scan & 0xFF
        cache[0x24] = n_scan & 0xFF
        cache[0x69] = n_scan & 0xFF
        cache[0x6A] = n_fast & 0xFF
        cache[0x5F] = n_fast & 0xFF

        feed_steps = geometry.starty
        feedl = feed_steps << mp.step_type
        dist = len(scan_slope.table)
        feedl = feedl - dist if dist < feedl else 0
        self._set24(cache, 0x3D, feedl)

        min_restep = max(1, (len(scan_slope.table) // step_mult) // 2 - 1)
        cache[0x22] = min_restep & 0xFF
        cache[0x23] = min_restep & 0xFF

        ccdlmt = (cache.get(0x0C, 0) & 0x0F) + 1
        exposure_mod = int(geometry.exposure_lperiod) * ccdlmt * tgtime
        z1, z2 = calculate_zmod(
            exposure_time=exposure_mod,
            slope_table=scan_slope.table,
            acceleration_steps=len(scan_slope.table),
            move_steps=feedl,
            buffer_acceleration_steps=min_restep * step_mult,
        )
        step_sel = mp.step_type << 5
        self._set24(cache, 0x60, (z1 & 0xFFFF) | (step_sel << 16))
        self._set24(cache, 0x63, (z2 & 0xFFFF) | (step_sel << 16))

        cache[0x1E] = (cache.get(0x1E, 0xF0) & 0xF0) | 0x00
        cache[0x67] = 0x7F
        cache[0x68] = 0x7F

        vref = (
            (mp.vref << 0)
            | (mp.vref << 2)
            | (mp.vref << 4)
            | (mp.vref << 6)
        )
        cache[0x80] = vref & 0xFF

        pairs = [(a, v) for a, v in sorted(cache.items()) if a != 0x0B]
        for addr, value in pairs:
            proto.write_register(addr, value)
        self.asic._reg_cache = cache

        payload = slope_table_to_bytes(scan_slope.table)
        fast_payload = slope_table_to_bytes(fast_slope.table)
        for table_nr in (0, 1, 2):
            proto.write_ahb(SLOPE_TABLE_AHB[table_nr], payload)
        for table_nr in (3, 4):
            proto.write_ahb(SLOPE_TABLE_AHB[table_nr], fast_payload)

        self._feedl = feedl
        logger.info(
            "configured asic=%s dpiset=%d str=%d end=%d lperiod=%d maxwd=%d "
            "lincnt=%d feedl=%d slopes=%d",
            getattr(self.model, "asic", "?"),
            geometry.register_dpiset,
            geometry.pixel_startx,
            geometry.pixel_endx,
            lperiod,
            maxwd,
            geometry.lincnt_register,
            feedl,
            len(scan_slope.table),
        )

    @staticmethod
    def _set16(cache: dict[int, int], addr: int, value: int) -> None:
        value &= 0xFFFF
        cache[addr] = (value >> 8) & 0xFF
        cache[addr + 1] = value & 0xFF

    @staticmethod
    def _set24(cache: dict[int, int], addr: int, value: int) -> None:
        value &= 0xFFFFFF
        cache[addr] = (value >> 16) & 0xFF
        cache[addr + 1] = (value >> 8) & 0xFF
        cache[addr + 2] = value & 0xFF

    # --- acquire --------------------------------------------------------

    def _begin_scan(self, *, start_motor: bool = True) -> None:
        proto = self.asic.protocol
        r = self.regs
        proto.write_register(0x0D, 0x05)  # CLRLNCNT | CLRMCNT
        reg01 = proto.read_register(0x01) | r.SCAN
        proto.write_register(0x01, reg01)
        self.asic._reg_cache[0x01] = reg01
        proto.write_register(0x0F, 0x01 if start_motor else 0x00)
        self.asic.update_home_sensor_gpio()
        logger.info("scan started motor=%s", start_motor)

    def _end_scan(self) -> None:
        try:
            self.asic.stop_motor()
        except Exception as exc:  # noqa: BLE001
            logger.warning("end_scan stop_motor: %s", exc)

    def _read_feed_steps(self) -> int:
        proto = self.asic.protocol
        steps = proto.read_register(0x4A)
        steps += proto.read_register(0x49) * 256
        steps += (proto.read_register(0x48) & 0x1F) * 256 * 256
        return steps

    def _read_valid_words(self) -> int:
        proto = self.asic.protocol
        words = proto.read_register(0x42) & 0x02
        words = words * 256 + proto.read_register(0x43)
        words = words * 256 + proto.read_register(0x44)
        words = words * 256 + proto.read_register(0x45)
        return words

    def _wait_feed(self, cancel: threading.Event | None) -> None:
        deadline = time.monotonic() + FEED_TIMEOUT_S
        target = getattr(self, "_feedl", 0)
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                raise ScanCancelled("cancelled during feed")
            if self._read_feed_steps() >= target:
                logger.debug("feed complete (%d)", target)
                return
            time.sleep(0.05)
        raise MotorTimeoutError(f"Feed did not complete within {FEED_TIMEOUT_S:.0f}s")

    def _wait_data(self, cancel: threading.Event | None) -> None:
        deadline = time.monotonic() + DATA_TIMEOUT_S
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                raise ScanCancelled("cancelled waiting for data")
            status = self.asic.read_status()
            if not status.is_buffer_empty and self._read_valid_words() >= 1:
                return
            time.sleep(0.02)
        raise ScanError(f"No scan data within {DATA_TIMEOUT_S:.0f}s")

    def _acquire(
        self,
        geometry: ScanGeometry,
        *,
        progress: Callable[[float], None] | None,
        cancel: threading.Event | None,
        wait_feed: bool = True,
    ) -> bytes:
        if wait_feed:
            self._wait_feed(cancel)
        self._wait_data(cancel)

        total = geometry.total_bytes
        chunk = geometry.line_bytes * BULK_CHUNK_LINES
        buf = bytearray()
        proto = self.asic.protocol

        while len(buf) < total:
            if cancel is not None and cancel.is_set():
                raise ScanCancelled("cancelled during bulk read")
            need = min(chunk, total - len(buf))
            data = proto.bulk_read_data(need)
            if not data:
                raise ScanError("Empty bulk read during scan")
            buf.extend(data)
            if progress is not None:
                progress(min(1.0, len(buf) / total))
            logger.debug("acquired %d / %d", len(buf), total)

        if progress is not None:
            progress(1.0)
        return bytes(buf[:total])


def create_session(
    asic: AsicDriver,
    model: FilmModel = MODEL_8200I,
    calibrator: Calibrator | None = None,
) -> ScanSession:
    """Build the scan session matching ``model``'s ASIC.

    * GL128 → :class:`~pyopticfilm.scan.session_gl128.Gl128ScanSession` (captures)
    * GL843 → :class:`~pyopticfilm.scan.session_gl843.Gl843ScanSession` (SANE)
    * GL842 → :class:`~pyopticfilm.scan.session_gl842.Gl842ScanSession` (SANE)
    * GL845 → :class:`ScanSession` (SANE ``CommandSetGl846``)
    """
    asic_name = getattr(model, "asic", "")
    if asic_name == "GL128":
        from pyopticfilm.scan.session_gl128 import Gl128ScanSession

        return Gl128ScanSession(asic, model, calibrator)
    if asic_name == "GL843":
        from pyopticfilm.scan.session_gl843 import Gl843ScanSession

        return Gl843ScanSession(asic, model, calibrator)
    if asic_name == "GL842":
        from pyopticfilm.scan.session_gl842 import Gl842ScanSession

        return Gl842ScanSession(asic, model, calibrator)
    return ScanSession(asic, model, calibrator)


# Re-export for type checkers / older imports
__all__ = ["ScanSession", "create_session"]
