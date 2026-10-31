# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan state machine: configure → acquire → assemble."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

import numpy as np

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
from pyopticfilm.exceptions import AsicError, MotorTimeoutError, ScanCancelled, ScanError
from pyopticfilm.image import ScanImage
from pyopticfilm.logging import get_logger
from pyopticfilm.scan.calibrate import Calibrator
from pyopticfilm.scan.geometry import MM_PER_INCH, ScanGeometry, compute_geometry
from pyopticfilm.scan.pipeline import HOST_CALIB_BORDER_INSET, ImagePipeline

if TYPE_CHECKING:
    from pyopticfilm.scan.me_exposure import MeExposureDecision

logger = get_logger(__name__)

FEED_TIMEOUT_S = 60.0
#: Feed fails early when the step counter stops advancing this long (stall).
FEED_STALL_S = 10.0
#: Pre-feed only when the image feed is this much slower than a low-res feed.
PREFEED_MIN_SPEEDUP = 1.5
#: Tiny normalized window for the pre-feed scan (a few lines, ~50 px).
PREFEED_AREA = (0.0, 0.0, 0.05, 0.01)
DATA_TIMEOUT_S = 30.0
BULK_CHUNK_LINES = 8
HOME_SCAN_PIXELS = 50
HOME_SCAN_LINES = 3


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
        self.last_align_shift_ir: tuple[float, float] | None = None
        self.last_me_debug = None

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
        single_pass_exposure: int | None = None,
        me_short_exposure: int | None = None,  # GL128 and GL845 ME
        me_long_exposure: int | None = None,  # GL128 and GL845 ME (clamped on GL845)
        me_long_mode: str = "adaptive",  # GL128 ME only; GL845 is always adaptive
        n_passes: int = 1,  # GL128 Multi-Pass only; ignored here
    ) -> ScanImage:
        if infrared and not self.model.supports_infrared:
            raise ValueError(f"{self.model.model} does not support infrared")
        multi_pass_ok = getattr(self.model, "asic", "") == "GL845"
        if (multi_exposure or infrared) and not multi_pass_ok:
            raise NotImplementedError(
                f"Multi-exposure / combined IR scans are not implemented for "
                f"{getattr(self.model, 'asic', '?')}"
            )
        if multi_exposure and not getattr(self.model, "supports_multi_exposure", False):
            raise NotImplementedError(f"{self.model.model} has no validated multi-exposure envelope")
        if (infrared or multi_exposure) and mode != "color":
            raise ValueError(
                f"Combined infrared / multi-exposure scans require mode='color', got mode={mode!r}"
            )
        if not multi_pass_ok and (me_short_exposure is not None or me_long_exposure is not None):
            raise NotImplementedError(
                "ME exposure overrides are only implemented for GL128 and GL845; "
                f"{getattr(self.model, 'asic', '?')} does not support them."
            )
        if n_passes > 1:
            raise NotImplementedError(
                f"Multi-Pass (n_passes > 1) is only implemented for GL128; "
                f"{getattr(self.model, 'asic', '?')} does not support it."
            )
        if single_pass_exposure is not None and not multi_pass_ok:
            raise NotImplementedError(
                "Manual exposure overrides are only implemented for GL128 and GL845; "
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
            lperiod = exposure_lperiod_for(
                self.model, geometry.resolution, method=method
            )
            if lperiod != geometry.exposure_lperiod:
                geometry = replace(geometry, exposure_lperiod=lperiod)
        logger.info(
            "scan %sdpi %s pixels=%d lines=%d starty=%d bytes=%d stagger=%d combined_ir=%s",
            geometry.resolution,
            mode,
            geometry.pixels,
            geometry.lines,
            geometry.starty,
            geometry.total_bytes,
            geometry.num_staggered_lines,
            infrared,
        )

        auto_exposure = False
        if method == "transparency" and multi_pass_ok and not multi_exposure:
            if single_pass_exposure is not None:
                geometry = replace(
                    geometry, exposure_lperiod=self._clamp_manual_exposure(single_pass_exposure)
                )
            else:
                auto_exposure = bool(getattr(self.model, "auto_exposure_target", None))

        if not infrared and not multi_exposure:
            try:
                rgb = self._acquire_pass(
                    geometry,
                    method=method,
                    mode=mode,
                    apply_calib=apply_calib,
                    progress=progress,
                    cancel=cancel,
                    auto_exposure=auto_exposure,
                )
            except BaseException:
                if multi_pass_ok:
                    self._park_quietly()
                raise
            ir_plane = self._infrared_plane(rgb) if mode == "infrared" else None
            return ScanImage(
                rgb=rgb,
                dpi=geometry.resolution,
                device_model=f"{self.model.vendor} {self.model.model}",
                ir=ir_plane,
            )

        return self._run_multi_pass(
            geometry,
            apply_calib=apply_calib,
            progress=progress,
            cancel=cancel,
            multi_exposure=multi_exposure,
            infrared=infrared,
            align_passes=align_passes,
            me_short_exposure=me_short_exposure,
            me_long_exposure=me_long_exposure,
            auto_exposure=auto_exposure,
        )

    def _clamp_manual_exposure(self, exposure: int) -> int:
        """Keep a manual LPERIOD inside the motor envelope validated for this model."""
        base = int(getattr(self.model, "exposure_lperiod", 14000))
        hi = min(int(getattr(self.model, "me_long_clamp_max", base)), 0xFFFF)
        clamped = max(base, min(hi, int(exposure)))
        if clamped != exposure:
            logger.warning(
                "single_pass_exposure %d clamped to %d (validated %d..%d)", exposure, clamped, base, hi
            )
        return clamped

    def _run_multi_pass(
        self,
        geometry: ScanGeometry,
        *,
        apply_calib: bool,
        progress: Callable[[float], None] | None,
        cancel: threading.Event | None,
        multi_exposure: bool,
        infrared: bool,
        align_passes: bool,
        me_short_exposure: int | None,
        me_long_exposure: int | None,
        auto_exposure: bool = False,
    ) -> ScanImage:
        """GL845 sequential passes: colour short → (IR) → (colour long), parking between.

        ME planes stay linear (``expose_base=False``) against the short-exposure
        host calib; film-base makeup runs once on the merged deliverable.
        """
        from pyopticfilm.pass_align import align_ir_to_rgb_result, align_pass_to_reference
        from pyopticfilm.scan.exposure_merge import merge_exposures_result
        from pyopticfilm.scan.me_debug import MeScanDebug

        model = self.model
        self.last_align_shift_ir = None
        self.last_me_debug = None

        exp_short = int(me_short_exposure or geometry.exposure_lperiod)
        short_geo = replace(geometry, exposure_lperiod=exp_short)
        n_pass = 1 + int(infrared) + int(multi_exposure)
        pass_idx = 0

        def _pass(geo: ScanGeometry, method: str, mode: str) -> np.ndarray:
            nonlocal pass_idx
            idx = pass_idx
            pass_idx += 1
            if idx > 0:
                self._check_cancel(cancel)
                self.asic.park()
                self._check_cancel(cancel)

            def _prog(val: float) -> None:
                if progress is not None:
                    progress(min(1.0, (idx + val) / n_pass))

            return self._acquire_pass(
                geo,
                method=method,
                mode=mode,
                apply_calib=apply_calib,
                progress=_prog,
                cancel=cancel,
                # ME colour planes stay linear for the merge; IR keeps its makeup.
                expose_base=not (multi_exposure and method == "transparency"),
                # Colour calib is measured at the model LPERIOD, never the long one.
                calib_geometry=geometry if method == "transparency" else geo,
                auto_exposure=auto_exposure and method == "transparency",
            )

        rgb_long = None
        exp_long = None
        decision = None
        ir_plane = None
        try:
            rgb_short = _pass(short_geo, "transparency", "color")
            if infrared:
                ir_lperiod = exposure_lperiod_for(model, geometry.resolution, method="infrared")
                ir_rgb = _pass(
                    replace(geometry, exposure_lperiod=ir_lperiod), "infrared", "infrared"
                )
                ir_plane = self._infrared_plane(ir_rgb)
            if multi_exposure:
                exp_long, decision = self._select_me_long(
                    rgb_short, exp_short, geometry.resolution, me_long_exposure
                )
                rgb_long = _pass(
                    replace(geometry, exposure_lperiod=exp_long), "transparency", "color"
                )
        except BaseException:
            self._park_quietly()
            raise
        self.asic.park()

        if ir_plane is not None and align_passes:
            ir_plane, shift = align_ir_to_rgb_result(rgb_short, ir_plane)
            self.last_align_shift_ir = shift
            logger.info("IR pass shift (dx, dy)=(%.3f, %.3f)", shift[0], shift[1])

        primary = rgb_short
        if rgb_long is not None and exp_long is not None:
            align_shift_long = None
            if align_passes:
                _, align_shift_long = align_pass_to_reference(rgb_short, rgb_long)
                logger.info(
                    "ME long pass shift (dx, dy)=(%.3f, %.3f)",
                    align_shift_long[0],
                    align_shift_long[1],
                )
            # Fixed noise model only when a model pins both terms; otherwise the
            # merge's own default (fit from the pass pair once upstream #149 lands).
            alpha = getattr(model, "me_noise_alpha", None)
            beta = getattr(model, "me_noise_beta", None)
            noise = (
                {"alpha": float(alpha), "beta": float(beta)}
                if alpha is not None and beta is not None
                else {}
            )
            merged = merge_exposures_result(
                rgb_short,
                rgb_long,
                exposure_short=exp_short,
                exposure_long=exp_long,
                align_shift=align_shift_long or (0.0, 0.0),
                **noise,
            )
            self.last_me_debug = MeScanDebug(
                rgb_short=rgb_short,
                rgb_long=rgb_long,
                exposure_short=exp_short,
                exposure_long=exp_long,
                fusion_stats=merged.fusion_stats,
                align_shift_long=align_shift_long,
                align_shift_ir=self.last_align_shift_ir,
                exposure_proposed=None if decision is None else decision.proposed,
                exposure_reason="manual-override" if decision is None else decision.reason,
            )
            primary = self.pipeline.expose_film_base(
                merged.rgb, source="me deliverable", preserve_headroom=True
            )
            primary = self.pipeline.clamp_border_highlights(primary)

        image = ScanImage(
            rgb=primary,
            dpi=geometry.resolution,
            device_model=f"{model.vendor} {model.model}",
            ir=ir_plane,
        )
        if image.ir_tracks_image:
            logger.warning(
                "IR plane follows the picture (silver black-and-white film?); "
                "it is not a dust map — skip IR dust removal for this frame"
            )
        return image

    def _select_me_long(
        self,
        rgb_short: np.ndarray,
        exp_short: int,
        resolution: int,
        me_long_exposure: int | None,
    ) -> tuple[int, MeExposureDecision | None]:
        """Pick the ME long LPERIOD, clamped to the model's validated envelope."""
        from pyopticfilm.scan.me_exposure import select_long_exposure

        model = self.model
        lo = int(getattr(model, "me_long_clamp_min", exp_short))
        hi = min(int(getattr(model, "me_long_clamp_max", 0xFFFF)), 0xFFFF)
        if me_long_exposure is not None:
            exp_long = max(lo, min(hi, int(me_long_exposure)))
            if exp_long != me_long_exposure:
                logger.warning(
                    "ME long exposure override %d clamped to %d (validated %d..%d)",
                    me_long_exposure,
                    exp_long,
                    lo,
                    hi,
                )
            return exp_long, None
        default_long = max(lo, min(hi, int(getattr(model, "exposure_long", exp_short * 3))))
        # Meter the film, not the holder (upstream #135): same inset as the makeup.
        region = self.pipeline._inset_slice(
            rgb_short.shape[0], rgb_short.shape[1], HOST_CALIB_BORDER_INSET
        )
        decision = select_long_exposure(
            rgb_short[region] if region is not None else rgb_short,
            exp_short,
            black_level=float(getattr(model, "me_black_level", 0.0)),
            dense_percentile=float(getattr(model, "me_dense_percentile", 5.0)),
            target_dense_dn=float(getattr(model, "me_target_dense_dn", 10000.0)),
            adaptive_min=lo,
            adaptive_max=hi,
            hardware_max=hi,
            max_ratio=float(getattr(model, "me_max_exposure_ratio", 3.0)),
            default_long=default_long,
        )
        exp_long = max(lo, min(hi, int(decision.selected)))
        logger.info(
            "ME long exposure %ddpi: short=%d proposed=%d selected=%d reason=%s",
            resolution,
            exp_short,
            decision.proposed,
            exp_long,
            decision.reason,
        )
        return exp_long, decision

    @staticmethod
    def _check_cancel(cancel: threading.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise ScanCancelled("Scan cancelled between passes")

    def _park_quietly(self) -> None:
        """Return the head home after a failed pass without masking the original error."""
        try:
            self.asic.park()
        except Exception as exc:  # noqa: BLE001
            logger.warning("park after failed multi-pass scan: %s", exc)

    def _acquire_pass(
        self,
        geometry: ScanGeometry,
        *,
        method: str,
        mode: str,
        apply_calib: bool,
        progress: Callable[[float], None] | None = None,
        cancel: threading.Event | None = None,
        expose_base: bool = True,
        calib_geometry: ScanGeometry | None = None,
        auto_exposure: bool = False,
    ) -> np.ndarray:
        calib_geo = calib_geometry or geometry
        calib = None
        if apply_calib and self.calibrator is not None:
            is_gl128 = getattr(self.model, "asic", "") == "GL128"
            if is_gl128 and method == "transparency":
                # SilverFast / SE: ASIC shading before any image feed.
                calib = self.calibrator.ensure_colour_asic_shading(geometry)
            elif is_gl128:
                self.calibrator.measure_ir_host_flatten(geometry)
            else:
                # Genesys (GL845/843/842): host dark/white stretch — never the
                # SE ``run_asic_shading`` path (those ASICs do not implement it).
                calib = self.calibrator.ensure_host_calib(
                    calib_geo,
                    method=method,
                    mode="infrared" if method == "infrared" else "color",
                )
                if calib is not None and method == "transparency":
                    if auto_exposure:
                        geometry = replace(
                            geometry, exposure_lperiod=self._auto_exposure(geometry, calib)
                        )
                    if expose_base:
                        calib = self._calib_for_exposure(calib, geometry, method)

        if getattr(self.model, "asic", "") == "GL845":
            geometry = self._prefeed(geometry, method=method, cancel=cancel)
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
        return self.pipeline.assemble(
            raw,
            geometry,
            dark=dark,
            white=white,
            planar=bool(planar),
            expose_base=expose_base,
        )

    def _prefeed(
        self,
        geometry: ScanGeometry,
        *,
        method: str,
        cancel: threading.Event | None = None,
    ) -> ScanGeometry:
        """Cover most of a slow feed with a tiny low-resolution scan first.

        The feed to the film runs on the scan slope, so its time scales with
        ``resolution * LPERIOD`` (7200 dpi at LPERIOD 65000: ~274 s). A few-line
        scan at the lowest resolution travels the same distance far faster and
        stops ``prefeed_margin_steps`` short; FEEDL is relative, so the image
        pass then feeds only the remainder.
        """
        model = self.model
        margin = int(getattr(model, "prefeed_margin_steps", 0) or 0)
        if margin <= 0:
            return geometry
        pre_res = min(model.resolutions_dpi)
        fast = pre_res * exposure_lperiod_for(model, pre_res, method="transparency")
        slow = geometry.resolution * int(geometry.exposure_lperiod)
        pre_steps = int(geometry.starty) - margin
        if slow <= PREFEED_MIN_SPEEDUP * fast or pre_steps <= margin:
            return geometry
        pre = compute_geometry(pre_res, model=model, area=PREFEED_AREA)
        pre = replace(pre, starty=pre_steps)
        t0 = time.monotonic()
        self.acquire_raw(pre, method=method, lamp_on=True, start_motor=True, cancel=cancel)
        # Line capture advances the motor by one line per scanned line.
        moved = (
            pre_steps
            + int(pre.lincnt_register) * model.motor_base_ydpi // pre_res
            + int(getattr(model, "prefeed_offset_steps", 0))
        )
        logger.info(
            "prefeed %d steps at %d dpi in %.1fs; image feed %d → %d",
            moved,
            pre_res,
            time.monotonic() - t0,
            geometry.starty,
            geometry.starty - moved,
        )
        return replace(geometry, starty=int(geometry.starty) - moved)

    def _calib_for_exposure(self, calib, geometry: ScanGeometry, method: str):
        """Scale white when the image LPERIOD differs from the calib LPERIOD.

        The CCD is linear in integration time, so the flat-field stays in
        open-light units; makeup then lifts the frame as usual.
        """
        base = exposure_lperiod_for(self.model, geometry.resolution, method=method)
        ratio = int(geometry.exposure_lperiod) / float(base)
        if abs(ratio - 1.0) < 1e-6:
            return calib
        dark = calib.dark.astype(np.float32)
        white = dark + (calib.white.astype(np.float32) - dark) * np.float32(ratio)
        return replace(calib, white=white)

    def _auto_exposure(self, geometry: ScanGeometry, calib) -> int:
        """Pick a single-pass LPERIOD that puts this frame's film peak near the target.

        Read noise is fixed per line (PGA gain does not change the input-referred
        floor), so longer integration is the only SNR lever. A low-resolution
        prescan of the same area measures the brightest channel.
        """
        model = self.model
        base = exposure_lperiod_for(model, geometry.resolution, method="transparency")
        target = float(getattr(model, "auto_exposure_target", 0) or 0)
        hi = min(int(getattr(model, "me_long_clamp_max", base)), 0xFFFF)
        if target <= 0 or hi <= base:
            return base
        pre = compute_geometry(min(model.resolutions_dpi), model=model, area=geometry.area)
        raw = self.acquire_raw(pre, method="transparency", lamp_on=True, start_motor=True)
        self.asic.park()
        rgb = self.pipeline.assemble(raw, pre, expose_base=False)
        dark = calib.dark.astype(np.float64).mean(axis=0)
        peak = float(
            (np.percentile(rgb.reshape(-1, 3).astype(np.float64), 99.5, axis=0) - dark).max()
        )
        headroom = (target - float(dark.max())) / max(1.0, peak)
        exposure = int(min(hi, max(base, base * headroom)))
        logger.info(
            "auto exposure: prescan peak=%.0f base=%d → %d (cap %d)", peak, base, exposure, hi
        )
        return exposure

    def _infrared_plane(self, rgb):
        """Build the HxW IR sidecar: green CCD plane + optional host flatten."""
        from pyopticfilm.scan.calib_gl128 import (
            flatten_ir_columns,
            flatten_ir_image_columns,
        )

        plane = np.ascontiguousarray(rgb[:, :, 1])
        white = getattr(self.asic, "last_ir_host_white", None)
        if getattr(self.asic, "ir_host_flatten_ready", False) and white:
            # The strip profile is in USB column order (starting at STRPIXEL);
            # ``rgb`` has already been mirrored by ``assemble`` on mirror_x models.
            white = list(white)
            if getattr(self.model, "mirror_x", False):
                white = white[: plane.shape[1]][::-1]
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
            was_lit = self._lamp_lit(method)
            self.asic.lamp_on()
            settle_s = float(getattr(self.model, "lamp_settle_s", 0.0))
            if not was_lit and settle_s > 0:
                logger.info("lamp settle %.2fs before %s capture", settle_s, method)
                time.sleep(settle_s)
        else:
            self.asic.lamp_off()

        try:
            self._configure(geometry)
            self._begin_scan(start_motor=start_motor)
            raw = self._acquire(
                geometry,
                progress=progress,
                cancel=cancel,
                wait_feed=start_motor and getattr(self, "_feedl", 0) > 0,
            )
        except BaseException:
            try:
                self._end_scan()
            except Exception as exc:  # noqa: BLE001
                logger.warning("end_scan after failed capture: %s", exc)
            raise
        self._end_scan()
        return raw

    def _lamp_lit(self, method: str) -> bool:
        """True when the light source for ``method`` is already on (cached regs)."""
        cache = getattr(self.asic, "_reg_cache", None) or {}
        if method == "infrared":
            mask = getattr(getattr(self.asic, "registers", None), "IR_LAMP_A8_MASK", 0)
            return bool(cache.get(0xA8, 0) & mask)
        return bool(cache.get(0x03, 0) & self.regs.LAMPPWR)

    def start_home_seek(self, *, starty: int) -> None:
        """Start the short reverse scan used by the GL845 home-sensor seek."""
        if not self.asic.is_initialized:
            raise AsicError("Call init()/warmup() before home()")

        resolution = min(self.model.resolutions_dpi)
        width_mm = HOME_SCAN_PIXELS * MM_PER_INCH / resolution + 0.01
        height_mm = HOME_SCAN_LINES * MM_PER_INCH / resolution + 0.01
        area = (
            0.0,
            0.0,
            min(1.0, width_mm / self.model.x_size_ta_mm),
            min(1.0, height_mm / self.model.y_size_ta_mm),
        )
        geometry = compute_geometry(resolution, model=self.model, area=area)
        if geometry.pixels < HOME_SCAN_PIXELS or geometry.lines < HOME_SCAN_LINES:
            raise ScanError(
                f"Home-scan geometry is too small: {geometry.pixels}x{geometry.lines}"
            )
        geometry = replace(
            geometry,
            startx=0,
            starty=starty,
            channels=1,
            depth=8,
            line_bytes=geometry.pixels,
            optical_line_count=HOME_SCAN_LINES,
            shift_r=0,
            shift_g=0,
            shift_b=0,
            stagger_y=(),
            num_staggered_lines=0,
            register_lincnt=HOME_SCAN_LINES,
        )

        # Leave the lamp alone: parking between calibration and image passes
        # must not cool the LED the next capture was calibrated against.
        prior_method = getattr(self.asic, "_scan_method", "transparency")
        self.asic.set_scan_method("transparency")
        try:
            self._configure(geometry, home_seek=True)
        finally:
            self.asic.set_scan_method(prior_method)
        self._begin_scan(start_motor=True)
        logger.info(
            "started reverse home scan pixels=%d lines=%d starty=%d",
            geometry.pixels,
            geometry.lines,
            starty,
        )

    # --- configure ------------------------------------------------------

    def _configure(self, geometry: ScanGeometry, *, home_seek: bool = False) -> None:
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

        if home_seek:
            pass  # keep the current LAMPPWR state
        elif method == "infrared" or not self._lamp_requested:
            # Dark calib captures must keep the lamp off through configure.
            cache[0x03] = cache.get(0x03, 0xBF) & ~r.LAMPPWR
        else:
            cache[0x03] = cache.get(0x03, 0xBF) | r.LAMPPWR
        cache[0x03] = cache[0x03] & ~0x40  # clear AVEENB

        if home_seek:
            # SANE's reverse-home mini-scan is 8-bit green-only.
            cache[0x04] = (cache.get(0x04, 0x22) & ~0xCC) | 0x28
        else:
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
        self._lperiod = int(geometry.exposure_lperiod)
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
        if home_seek:
            cache[0x02] |= r.AGOHOME | r.NOTHOME | r.MTRREV
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
        self.asic.stop_motor()

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
        # The feed runs on the scan slope, so longer LPERIODs feed proportionally slower.
        base = int(getattr(self.model, "exposure_lperiod", 14000)) or 14000
        timeout_s = FEED_TIMEOUT_S * max(1.0, getattr(self, "_lperiod", base) / base)
        start = time.monotonic()
        deadline = start + timeout_s
        target = getattr(self, "_feedl", 0)
        last_steps, last_move = -1, start
        while (now := time.monotonic()) < deadline:
            if cancel is not None and cancel.is_set():
                raise ScanCancelled("cancelled during feed")
            steps = self._read_feed_steps()
            if steps >= target:
                logger.debug("feed complete (%d) in %.1fs", target, now - start)
                return
            if steps != last_steps:
                last_steps, last_move = steps, now
            elif now - last_move > FEED_STALL_S:
                raise MotorTimeoutError(
                    f"Feed stalled at {steps}/{target} steps for {FEED_STALL_S:.0f}s"
                )
            time.sleep(0.05)
        raise MotorTimeoutError(
            f"Feed did not complete within {timeout_s:.0f}s ({last_steps}/{target} steps)"
        )

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
    * GL128 film loader (135i) →
      :class:`~pyopticfilm.scan.session_gl128_loader.Gl128LoaderScanSession` (captures)
    * GL843 7600i v1 → :class:`~pyopticfilm.scan.session_7600i_v1.Gl843V1ScanSession` (captures)
    * GL843 → :class:`~pyopticfilm.scan.session_gl843.Gl843ScanSession` (SANE)
    * GL842 → :class:`~pyopticfilm.scan.session_gl842.Gl842ScanSession` (SANE)
    * GL845 → :class:`ScanSession` (SANE ``CommandSetGl846``)
    """
    asic_name = getattr(model, "asic", "")
    if asic_name == "GL128" and getattr(model, "film_loader", False):
        from pyopticfilm.scan.session_gl128_loader import Gl128LoaderScanSession

        return Gl128LoaderScanSession(asic, model, calibrator)
    if asic_name == "GL128":
        from pyopticfilm.scan.session_gl128 import Gl128ScanSession

        return Gl128ScanSession(asic, model, calibrator)
    from pyopticfilm.device.model_7600i_v1 import Model7600iV1

    if isinstance(model, Model7600iV1):
        from pyopticfilm.scan.session_7600i_v1 import Gl843V1ScanSession

        return Gl843V1ScanSession(asic, model, calibrator)  # type: ignore[return-value]
    if asic_name == "GL843":
        from pyopticfilm.scan.session_gl843 import Gl843ScanSession

        return Gl843ScanSession(asic, model, calibrator)
    if asic_name == "GL842":
        from pyopticfilm.scan.session_gl842 import Gl842ScanSession

        return Gl842ScanSession(asic, model, calibrator)
    return ScanSession(asic, model, calibrator)


# Re-export for type checkers / older imports
__all__ = ["ScanSession", "create_session"]
