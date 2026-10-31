# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan session for the GL128 film loader (OpticFilm 135i).

Every pass runs in toggle mode: each scan line is an IR line then a visible
line, so colour and infrared come from the same pass and are registered to
the same scan line. ``infrared=False`` drops the IR half. What differs from
:class:`~pyopticfilm.scan.session_gl128.Gl128ScanSession`:

* the register block comes from the 135i timing sets and scan overlays
  (:meth:`~pyopticfilm.device.model_135i.Model135i.scan_regs_for`), with the
  depth pair left at ``0x33 = 0x0E`` / ``0xAF = 0xFF``;
* positioning is one loader feed from the loader home to the frame (or to the
  top of the strip for a panorama, ``frame=None``); the image pass parks with
  ``AGOHOME``;
* calibration fills a visible and an IR shading bank
  (:meth:`~pyopticfilm.asic.gl128_loader.Gl128Loader.run_asic_shading`);
* the first chunk of the image is announced on the image stream and the rest
  on RAM, as in VueScan's 135i passes.

Multi-exposure and Multi-Pass reuse the family pass sequence and merge
(:meth:`Gl128ScanSession._run_multi_pass`); each pass is one feed and one
toggle-mode pass, and the IR plane comes from the first short pass.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace

import numpy as np

from pyopticfilm.asic.gl128_loader import LIVE_REGS
from pyopticfilm.device.protocol import FilmModel
from pyopticfilm.exceptions import AsicError, ScanError
from pyopticfilm.logging import get_logger
from pyopticfilm.scan.calib_gl128_loader import split_toggle_lines
from pyopticfilm.scan.exposure_override import validate_manual_exposure, validate_n_passes
from pyopticfilm.scan.geometry import ScanGeometry
from pyopticfilm.scan.me_exposure import validate_me_long_mode
from pyopticfilm.scan.pipeline import ImagePipeline
from pyopticfilm.scan.session_gl128 import _MOTOR_GATED_HINT, Gl128ScanSession, check_gl128_multi_pass

logger = get_logger(__name__)

#: Image-pass values outside the scan overlay (every captured image pass):
#: scan selects, no calibration ``0xA6``-``0xA9``, ramp count 255, motor
#: counters, ``0x1C``/``0x1D`` 0.
_IMAGE_PASS_REGS: dict[int, int] = {
    0x04: 0x42, 0x05: 0x40, 0x1C: 0x00, 0x1D: 0x00,
    0xA2: 0x00, 0xA3: 0x00, 0xA4: 0x00, 0xA6: 0x00, 0xA7: 0x00, 0xA8: 0x00, 0xA9: 0x00,
    0xAA: 0x00, 0xAC: 0x00, 0xAD: 0x01, 0xAE: 0x00, 0xAF: 0xFF,
}


def loader_feed_steps(model: FilmModel, geometry: ScanGeometry, frame: int | None) -> int:
    """Feed (``FEEDL``) from the loader home to the top of ``geometry``.

    ``frame`` is the 35 mm frame (0-based); ``None`` is the whole strip
    (panorama), whose slot starts at 6414.
    """
    top = model.strip_feed_steps() if frame is None else model.frame_feed_steps(int(frame))
    return int(top) + int(geometry.starty)


def loader_travel_end(model: FilmModel, geometry: ScanGeometry, frame: int | None) -> int:
    """Feed steps from home at which the image pass of ``geometry`` ends."""
    pairs = geometry.lincnt_register // max(1, geometry.lincnt_per_line)
    per_pair = int(model.feed_steps_per_inch) / geometry.asic_dpi
    return loader_feed_steps(model, geometry, frame) + round(pairs * per_pair)


def plane_geometry(geometry: ScanGeometry) -> ScanGeometry:
    """Geometry of the visible (or IR) half of a toggle-mode buffer."""
    if geometry.lincnt_per_line <= 1:
        return geometry
    pairs = geometry.optical_line_count // geometry.lincnt_per_line
    return replace(geometry, optical_line_count=pairs, register_lincnt=pairs, lincnt_per_line=1)


class LoaderImagePipeline(ImagePipeline):
    """:class:`ImagePipeline` that takes the toggle-mode geometry of a loader pass.

    :meth:`Gl128LoaderScanSession.acquire_raw` returns the visible half only,
    so assembly runs on the plane geometry (one line per output line).
    """

    def assemble(self, raw, geometry: ScanGeometry, **kwargs):  # type: ignore[no-untyped-def, override]
        return super().assemble(raw, plane_geometry(geometry), **kwargs)


class Gl128LoaderScanSession(Gl128ScanSession):
    """Toggle-mode RGB+IR frame and strip scans on the OpticFilm 135i loader."""

    def __init__(self, asic, model, calibrator=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(asic, model, calibrator)
        self.pipeline = LoaderImagePipeline(model)
        #: 35 mm frame (0-based) the next image pass feeds to; ``None`` = strip.
        self._frame: int | None = 0
        #: IR half of the first pass of the current scan (the IR plane).
        self._first_ir: np.ndarray | None = None

    def run(  # type: ignore[override]
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
        single_pass_exposure: int | None = None,
        me_short_exposure: int | None = None,
        me_long_exposure: int | None = None,
        me_long_mode: str = "adaptive",
        n_passes: int = 1,
        frame: int | None = 0,
    ):
        """Scan ``frame`` of the loaded holder (``None``: the whole strip).

        ``infrared=True`` also returns the IR plane, from the first pass.
        """
        from pyopticfilm.image import ScanImage
        from pyopticfilm.scan.geometry import compute_geometry, compute_strip_geometry

        if not getattr(self.asic, "_motor_moves_enabled", False):
            raise AsicError(_MOTOR_GATED_HINT)
        validate_me_long_mode(me_long_mode)
        validate_manual_exposure(single_pass_exposure, label="single_pass_exposure")
        validate_manual_exposure(me_short_exposure, label="me_short_exposure")
        validate_manual_exposure(me_long_exposure, label="me_long_exposure")
        validate_n_passes(n_passes)
        if mode != "color":
            raise ValueError(
                f"{self.model.model} scans RGB and IR together: use mode='color' "
                "(with infrared=True for the IR plane)."
            )
        if frame is not None and int(frame) < 0:
            raise ValueError(f"frame must be >= 0 or None, got {frame}")
        multi = multi_exposure or n_passes > 1
        if geometry is None:
            if frame is None:
                geometry = compute_strip_geometry(resolution, model=self.model, area=area)
            else:
                geometry = compute_geometry(resolution, model=self.model, area=area)
        check_gl128_multi_pass(
            self.model, geometry.resolution, multi_exposure=multi_exposure, n_passes=n_passes
        )

        if not self.asic._initialized:
            self.asic.init()
        self.asic.require_holder_loaded("Scan")
        end = loader_travel_end(self.model, geometry, frame)
        travel = int(self.model.loader_travel_steps())
        if end > travel:
            where = "The strip scan" if frame is None else f"Frame {frame}"
            raise ScanError(
                f"{where} at {geometry.resolution} dpi would end {end} steps from the "
                f"loader home, past the {travel}-step loader travel."
            )

        self._frame = None if frame is None else int(frame)
        self._first_ir = None
        self._use_asic_shading = bool(apply_calib) and self.calibrator is not None
        try:
            if multi:
                image = self._run_multi_pass(
                    resolution=geometry.resolution,
                    geometry=geometry,
                    progress=progress,
                    cancel=cancel,
                    apply_calib=apply_calib,
                    multi_exposure=multi_exposure,
                    infrared=False,
                    align_passes=align_passes,
                    single_pass_exposure=single_pass_exposure,
                    me_short_exposure=me_short_exposure,
                    me_long_exposure=me_long_exposure,
                    me_long_mode=me_long_mode,
                    n_passes=n_passes,
                )
                rgb = image.rgb
            else:
                rgb = self._run_single(
                    geometry,
                    progress=progress,
                    cancel=cancel,
                    single_pass_exposure=single_pass_exposure,
                )
            ir_plane = self._ir_plane(geometry) if infrared else None
        finally:
            self._first_ir = None
        return ScanImage(
            rgb=rgb,
            dpi=geometry.resolution,
            device_model=f"{self.model.vendor} {self.model.model}",
            ir=ir_plane,
        )

    def _run_single(
        self,
        geometry: ScanGeometry,
        *,
        progress: Callable[[float], None] | None,
        cancel: threading.Event | None,
        single_pass_exposure: int | None,
    ) -> np.ndarray:
        self.asic.enter_scan_clock()
        calib = None
        if self._use_asic_shading:
            assert self.calibrator is not None
            calib = self.calibrator.ensure_colour_asic_shading(geometry)
        if single_pass_exposure is not None:
            self._pass_exposure = int(single_pass_exposure)
            self._pass_manual = True
        try:
            visible = self.acquire_raw(
                geometry,
                method="transparency",
                lamp_on=True,
                start_motor=True,
                progress=progress,
                cancel=cancel,
            )
        finally:
            self._pass_exposure = None
            self._pass_manual = False
        use_host = (
            calib is not None
            and self.calibrator is not None
            and self.calibrator.should_apply_host_calib()
        )
        return self.pipeline.assemble(
            visible,
            geometry,
            dark=calib.dark if use_host else None,
            white=calib.white if use_host else None,
            planar=False,
        )

    def _ir_plane(self, geometry: ScanGeometry) -> np.ndarray | None:
        if self._first_ir is None:
            return None
        ir_rgb = self.pipeline.assemble(
            memoryview(self._first_ir),
            geometry,
            planar=False,
            expose_base=False,
            usb_end_drop=self.pipeline.last_usb_end_drop,
        )
        return self._infrared_plane(ir_rgb)

    def acquire_raw(  # type: ignore[override]
        self,
        geometry: ScanGeometry,
        *,
        method: str,
        lamp_on: bool,
        start_motor: bool,
        progress: Callable[[float], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> memoryview:
        """One toggle-mode pass; returns the visible half and keeps the first IR half."""
        raw = super().acquire_raw(
            geometry,
            method="transparency",
            lamp_on=lamp_on,
            start_motor=start_motor,
            progress=progress,
            cancel=cancel,
        )
        del method
        visible, ir = split_toggle_lines(
            raw, line_bytes=geometry.line_bytes, pairs=plane_geometry(geometry).optical_line_count
        )
        del raw
        if self._first_ir is None:
            self._first_ir = ir
        return memoryview(visible)

    # --- configure ------------------------------------------------------

    def _configure(self, geometry: ScanGeometry) -> None:
        r = self.se_regs
        model = self.model
        dpi = geometry.resolution
        asic_dpi = model.asic_dpi_for(dpi)

        cache = model.boot_register_map()
        cache.update(model.scan_regs_for(dpi))
        for addr, (mask, value) in model.scan_masked_regs_for(dpi).items():
            if addr in LIVE_REGS:
                continue
            cache[addr] = (int(cache.get(addr, 0)) & ~mask & 0xFF) | (value & mask)
        cache.update(_IMAGE_PASS_REGS)
        try:
            cache[0x2B] = int(model.dummy_by_dpi[asic_dpi])
            clk = int(model.pixel_clock_for_image(dpi))
        except KeyError as exc:
            raise ScanError(f"No register values for {dpi} dpi on {model.model}") from exc
        cache[0xA5] = clk
        cache[0xAB] = clk

        ready = self._use_asic_shading and getattr(self.asic, "asic_shading_ready", False)
        cache[r.REG_0x01] = r.SHDAREA | (r.DVDSET if ready else 0)
        cache[r.REG_0x02] = r.MTRPWR | r.AGOHOME
        for addr in LIVE_REGS:
            cache.pop(addr, None)
        for addr in (r.REG_FE_INDEX, r.REG_FE_HIGH, r.REG_FE_LOW, r.REG_CLRCNT, r.REG_START):
            cache.pop(addr, None)

        self._set24(cache, r.REG_LINCNT, geometry.lincnt_register)
        span = geometry.pixel_endx - geometry.pixel_startx
        lperiod = model.line_period_for(dpi, span=span)
        self._set24(cache, r.REG_LPERIOD, lperiod)
        self._set16(cache, r.REG_DPISET, geometry.register_dpiset)
        self._set24(cache, r.REG_STRPIXEL, geometry.pixel_startx)
        self._set24(cache, r.REG_ENDPIXEL, geometry.pixel_endx)
        self._verify_geometry_usb_span(geometry)
        exposure = None if self._pass_exposure is None else int(self._pass_exposure)
        cache.update(model.exposure_regs_for(dpi, exposure=exposure))
        exposure_reg = (cache[0x7D] << 16) | (cache[0x7E] << 8) | cache[0x7F]
        self._set24(cache, r.REG_FEEDL, 1)

        # Validate the scan motor table before the holder moves.
        channel_exp = int(model.channel_exposure_for(dpi, exposure=exposure_reg))
        model.scan_motor_table(channel_exp)

        if getattr(self.asic, "_motor_moves_enabled", False):
            feed = loader_feed_steps(model, geometry, self._frame)
            self.asic.position_for_frame(feed_steps=feed, scan_regs=cache)

        self.asic.upload_tables(resolution=dpi, slope=False, channel_exposure=channel_exp)
        last_afe = getattr(self.asic, "last_afe", None)
        if last_afe is not None:
            self.asic.apply_frontend(last_afe)

        self.asic.protocol.write_registers_batched(sorted(cache.items()))
        self.asic._reg_cache.update(cache)
        self.asic.apply_timing_gpio(dpi)
        self.asic.image_pass_since_init = True

        if self._lamp_requested:
            self.asic.lamp_on()
        else:
            self.asic.lamp_off()
        self._feedl = 0

        logger.info(
            "GL128 loader configured %ddpi frame=%s dpiset=%d lincnt=%d str=%d end=%d "
            "lperiod=%d exposure=%d dvdset=%s",
            dpi,
            "strip" if self._frame is None else self._frame,
            geometry.register_dpiset,
            geometry.lincnt_register,
            geometry.pixel_startx,
            geometry.pixel_endx,
            lperiod,
            exposure_reg,
            bool(ready),
        )

    # --- acquire --------------------------------------------------------

    def _bulk_index(self, geometry: ScanGeometry, *, first: bool) -> int:
        """VueScan's 135i image reads: the first announce on the image stream, the rest on RAM."""
        r = self.se_regs
        if geometry.disable_buffer_full_move or not first:
            return r.BULK_INDEX_RAM
        return r.BULK_INDEX_IMAGE
