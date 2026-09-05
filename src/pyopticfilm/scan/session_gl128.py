# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan session for GL128 OpticFilm (8200i SE and 8100 V2).

Only the chip-specific steps are overridden; run/assemble/calibration lookup
stay in :class:`~pyopticfilm.scan.session.ScanSession`. What differs from GL845:

* the register block comes from the model tables plus a small set of
  resolution-dependent values, instead of being computed from a motor profile;
* motor slope tables are replayed from the capture rather than generated, so
  there is no ``zmod`` calculation;
* feeding is a separate, synchronous move before the scan starts;
* the image is streamed with USB-sized ``VALUE_BUFFER`` announces (a single
  full-image preamble was louder on real GL128 hardware); source is selected
  with ``wIndex`` — RAM for calibration, live stream for a scan.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from pyopticfilm.asic.gl128 import DEFAULT_IMAGE_USB_PACE_S
from pyopticfilm.asic.registers import Gl128Registers
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.protocol import AsicDriver, FilmModel, MultiExposureFilmModel
from pyopticfilm.exceptions import AsicError, ScanCancelled, ScanError
from pyopticfilm.logging import get_logger
from pyopticfilm.scan.calibrate import Calibrator
from pyopticfilm.scan.exposure_override import validate_manual_exposure
from pyopticfilm.scan.geometry import ScanGeometry
from pyopticfilm.scan.session import DATA_TIMEOUT_S, ScanSession
from pyopticfilm.usb.device import BULK_MAX_SIZE

logger = get_logger(__name__)

#: Max bytes announced per ``bulk_read_begin``. SilverFast uses one preamble for
#: the whole image; Lab keeps USB-sized announces so cancel can abort cleanly
#: and (empirically) high-PPI creep is quieter than one giant unpaced preamble.
IMAGE_CHUNK_BYTES = BULK_MAX_SIZE

#: Quiet-drain on sentinel (any ``> 0`` enables pacing). Not a sleep ceiling.
#: Matches :data:`pyopticfilm.asic.gl128.DEFAULT_IMAGE_USB_PACE_S` (on by default).
IMAGE_USB_PACE_S = DEFAULT_IMAGE_USB_PACE_S

#: Scale ASIC ``LPERIOD`` register to approximate output-line duration (seconds).
_LINE_PERIOD_TO_SECONDS = 1.0 / 4_500_000.0

#: Host stays slightly behind the ASIC line clock so the image buffer does not
#: empty (start/stop creep). Applied only when quiet drain is on.
_QUIET_DRAIN_LAG = 1.05

def clamp_me_long_for_dpi(
    resolution: int, exp_long: int, model: MultiExposureFilmModel | None = None
) -> int:
    """Clamp ME colour-long exposure for the requested PPI.

    Floor and DPI-keyed ceiling come from ``model``
    (:attr:`~pyopticfilm.device.model_8200i_se.Model8200iSE.exposure_short` /
    :meth:`~pyopticfilm.device.model_8200i_se.Model8200iSE.me_long_exposure_ceiling`
    — the single source of truth shared with adaptive selection and any
    clamped manual override). Defaults to the 8200i SE table (14000 floor,
    42000 at 7200 dpi, 85000 elsewhere) when no model is given.
    """
    mdl = model if model is not None else MODEL_8200I_SE
    value = int(exp_long)
    floor = int(mdl.exposure_short)
    ceiling = mdl.me_long_exposure_ceiling(int(resolution))
    return min(max(value, floor), ceiling)


try:
    from pyopticfilm.asic.gl128 import MOTOR_GATED_HINT as _MOTOR_GATED_HINT
except ImportError:  # pragma: no cover
    _MOTOR_GATED_HINT = "GL128 motor moves are temporarily disabled."


def image_feed2_steps(model: FilmModel, geometry: ScanGeometry) -> int:
    """Second-feed steps for an image pass.

    Prefer ``geometry.area``. If that tuple was dropped, reconstruct from
    ``area_y1`` so a crop does not fall back to full-frame ``13704``.
    """
    feed_fn = getattr(model, "feed_to_scan_steps_for_area", None)
    if not callable(feed_fn):
        return int(getattr(model, "feed_to_scan_steps", 0) or 0)
    area = geometry.area
    if area is None:
        y1 = float(getattr(geometry, "area_y1", 0.0) or 0.0)
        if y1 > 1e-9:
            area = (0.0, y1, 1.0, 1.0)
    return int(feed_fn(area))


class Gl128ScanSession(ScanSession):
    """GL128 scan state machine for OpticFilm 8200i SE and 8100 (V2)."""

    def __init__(
        self,
        asic: AsicDriver,
        model: FilmModel = MODEL_8200I_SE,
        calibrator: Calibrator | None = None,
    ) -> None:
        super().__init__(asic, model, calibrator)
        self.se_regs = Gl128Registers()
        #: Set when the image pass armed ``AGOHOME`` — wait for park in ``_end_scan``.
        self._await_agohome_park = False
        #: True after :meth:`bulk_read_begin` until :meth:`_end_scan` aborts/finishes.
        self._bulk_stream_active = False
        #: Image ``REG_EXPOSURE`` for the current pass (``None`` → short / 14000).
        self._pass_exposure: int | None = None
        #: Explicit ME long-pass clocks (independent of numeric exposure).
        self._pass_long_exposure: bool = False
        #: True when ``_pass_exposure`` is a caller-supplied manual override
        #: (Scan Lab / debug) rather than a driver-derived value — set only
        #: for the duration of the pass that should write it verbatim.
        self._pass_manual: bool = False
        #: Lab-only ME bracket / IVW stats (not on :class:`~pyopticfilm.image.ScanImage`).
        self.last_me_debug = None
        #: IR→short alignment shift from the last multi-pass scan (ME or IR-only).
        self.last_align_shift_ir: tuple[float, float] | None = None

    def run(
        self,
        *args,
        multi_exposure: bool = False,
        infrared: bool = False,
        align_passes: bool = True,
        me_exposure_mode: str | None = None,
        single_pass_exposure: int | None = None,
        me_short_exposure: int | None = None,
        me_long_exposure: int | None = None,
        me_target_exposure: int | None = None,
        n_brackets: int = 2,
        **kwargs,
    ):  # type: ignore[no-untyped-def]
        # Fail fast on bad manual-exposure input before any ASIC state check —
        # same order as Scanner.scan() so a direct-session caller sees the
        # same ValueError a Scanner.scan() caller would, not an unrelated
        # AsicError from the motor gate below.
        validate_manual_exposure(single_pass_exposure, label="single_pass_exposure")
        validate_manual_exposure(me_short_exposure, label="me_short_exposure")
        validate_manual_exposure(me_long_exposure, label="me_long_exposure")
        validate_manual_exposure(me_target_exposure, label="me_target_exposure")
        if me_long_exposure is not None and me_target_exposure is not None:
            raise ValueError(
                "me_long_exposure (unrestricted debug override) and "
                "me_target_exposure (model-envelope-clamped) are mutually "
                "exclusive — pass only one."
            )
        if not (2 <= n_brackets <= 9):
            raise ValueError(f"n_brackets must be between 2 and 9, got {n_brackets!r}")
        # Refuse unless the ASIC explicitly arms motor moves.
        if not getattr(self.asic, "_motor_moves_enabled", False):
            raise AsicError(_MOTOR_GATED_HINT)
        if multi_exposure or (infrared and kwargs.get("mode", "color") == "color"):
            if infrared and not getattr(self.model, "supports_infrared", False):
                raise ScanError(
                    f"{self.model.model} has no infrared channel: IR scans are "
                    "unavailable on this model. Scan with mode='color', "
                    "infrared=False (optionally multi_exposure=True)."
                )
            return self._run_multi_pass(
                *args,
                multi_exposure=multi_exposure,
                infrared=infrared,
                align_passes=align_passes,
                me_exposure_mode=me_exposure_mode,
                me_short_exposure=me_short_exposure,
                me_long_exposure=me_long_exposure,
                me_target_exposure=me_target_exposure,
                n_brackets=n_brackets,
                **kwargs,
            )
        if single_pass_exposure is None:
            # No override: identical to today's model-derived single pass.
            return super().run(*args, **kwargs)
        self._pass_exposure = int(single_pass_exposure)
        self._pass_manual = True
        try:
            return super().run(*args, **kwargs)
        finally:
            self._pass_exposure = None
            self._pass_manual = False

    # --- configure ------------------------------------------------------

    def _configure(self, geometry: ScanGeometry) -> None:
        r = self.se_regs
        model = self.model
        dpi = geometry.resolution
        shading = bool(geometry.disable_buffer_full_move)

        cache = model.boot_register_map()
        cache.update(model.gpo_regs)
        cache.update(model.sensor_custom_regs)

        try:
            asic_dpi = model.asic_dpi_for(dpi)
            cache[0x2B] = model.dummy_by_dpi[asic_dpi]
            long_pass = bool(self._pass_long_exposure)
            clk_fn = getattr(model, "pixel_clock_for_image", None)
            if callable(clk_fn):
                clk = int(clk_fn(dpi, long_exposure=long_pass))
            else:
                clk = int(model.pixel_clock_by_dpi[asic_dpi])
            cache[0xA5] = clk
            cache[0xAB] = clk
        except KeyError as exc:
            raise ScanError(
                f"No capture-derived register values for {dpi} dpi on "
                f"{model.model}; supported: {sorted(model.resolutions_dpi)}"
            ) from exc

        # Image: DEPTH8 *registers* (session 11) but 16-bit LE samples on the
        # wire. Calib/shading: DEPTH16 regs + 16-bit samples (sessions 03–04).
        if shading:
            cache[r.REG_DEPTH_A] = r.DEPTH16_A
            cache[r.REG_DEPTH_B] = r.DEPTH16_B
        else:
            cache[r.REG_DEPTH_A] = r.DEPTH8_A
            cache[r.REG_DEPTH_B] = r.DEPTH8_B

        # Host shading: clear DVDSET on calib. Colour image keeps DVDSET only
        # when a measured ASIC shading table is ready — otherwise boot DVDSET +
        # unity or stale coefficients produce rainbow / clipped garbage.
        # Infrared must never keep colour DVDSET: live HW clips IR to full scale
        # with an IR table, and a colour table after a colour+IR pair makes the
        # IR frame magenta, uneven, and low-contrast for dust.
        reg01 = (cache.get(r.REG_0x01, 0x22) | r.SHDAREA) & ~r.SCAN & ~r.STAGGER
        infrared = getattr(self.asic, "_scan_method", None) == "infrared"
        if shading or infrared or not getattr(self.asic, "asic_shading_ready", False):
            reg01 &= ~r.DVDSET
        cache[r.REG_0x01] = reg01

        # Leave lamp / IR LED alone — :meth:`Gl128.lamp_on` already programmed
        # ``0x03`` / ``0x37`` for the scan method; rewriting them from the boot
        # cache would clear IR LED or re-enable the white lamp.
        cache.pop(r.REG_0x03, None)
        cache.pop(r.REG_IR, None)

        motor = r.MTRPWR
        if not shading:
            # Image pass: AGOHOME parks the carriage when the scan ends.
            motor |= r.AGOHOME
        cache[r.REG_0x02] = motor

        self._set24(cache, r.REG_LINCNT, geometry.lincnt_register)
        self._set24(cache, r.REG_LPERIOD, model.line_period_for(dpi))
        # Captures: AFE/shading always use DPISET = optical_resolution/6 (1200).
        dpiset = (
            model.optical_resolution // 6 if shading else geometry.register_dpiset
        )
        self._set16(cache, r.REG_DPISET, dpiset)
        self._set24(cache, r.REG_STRPIXEL, geometry.pixel_startx)
        self._set24(cache, r.REG_ENDPIXEL, geometry.pixel_endx)
        self._verify_geometry_usb_span(geometry)
        if self._pass_exposure is not None:
            exposure_reg = int(self._pass_exposure)
        else:
            exp_fn = getattr(model, "image_exposure", None)
            exposure_reg = (
                int(exp_fn(long_exposure=False))
                if callable(exp_fn)
                else int(model.exposure_lperiod)
            )
        if self._pass_manual:
            # Explicit Scan Lab / debug override: the caller's value must
            # reach REG_EXPOSURE verbatim, so the hardware-max clamp below
            # (which protects driver-derived exposure) is bypassed.
            logger.info("REG_EXPOSURE %d — manual-override, hardware-max clamp bypassed", exposure_reg)
        else:
            hw_max = getattr(model, "me_hardware_max_exposure", None)
            if hw_max is not None and exposure_reg > int(hw_max):
                logger.warning(
                    "REG_EXPOSURE %d above hardware max %d — clamping",
                    exposure_reg,
                    int(hw_max),
                )
                exposure_reg = int(hw_max)
        self._set24(cache, r.REG_EXPOSURE, exposure_reg)
        # Image/calib acquire with FEEDL=1; positioning is a separate feed pair.
        self._set24(cache, r.REG_FEEDL, 1)
        cache.pop(r.REG_CLRCNT, None)
        cache.pop(r.REG_START, None)

        self._await_agohome_park = not shading and bool(motor & r.AGOHOME)

        # Capture-constant feeds from home — never geometry.starty (that was the
        # grinding bug). Calibration passes stay put (no motor). Positioning is
        # skipped while motor moves are gated so configure unit tests stay safe.
        if not shading and getattr(self.asic, "_motor_moves_enabled", False):
            scan_steps = image_feed2_steps(model, geometry)
            # The scan must stop at the window end: feed2 + travel <= 27636
            # steps. Overrunning it is what ground the motor in the Lab.
            max_fn = getattr(model, "max_lincnt_for", None)
            max_lc = max_fn(scan_steps, dpi) if callable(max_fn) else None
            if max_lc is not None and geometry.lincnt_register > int(max_lc):
                start_mm = scan_steps * 25.4 / model.feed_steps_per_inch
                raise ScanError(
                    f"Image LINCNT {geometry.lincnt_register} at {dpi} dpi is "
                    f"{geometry.travel_mm:.1f} mm of travel from feed2="
                    f"{scan_steps} ({start_mm:.1f} mm), past the "
                    f"{model.scan_window_end_steps * 25.4 / model.feed_steps_per_inch:.1f} mm "
                    f"scan-window end. Max LINCNT here is {max_lc} "
                    "(see captures/8200i-se/MOTOR.md)."
                )
            self.asic.position_for_full_frame_scan(scan_steps=scan_steps)

        ch_exp_fn = getattr(model, "channel_exposure_for", None)
        if callable(ch_exp_fn):
            try:
                channel_exp = int(ch_exp_fn(dpi, exposure=exposure_reg))
            except TypeError:
                channel_exp = int(ch_exp_fn(dpi))
        else:
            channel_exp = None
        self.asic.upload_tables(
            resolution=dpi, shading=shading, channel_exposure=channel_exp
        )
        # Do NOT call set_frontend_init() here — boot zeroes FE gains, and
        # replaying that after search_afe undoes calibration. Captures keep the
        # post-calib FE for the image pass. Re-apply the last search result if
        # we have one (covers any FE touch during table upload / strip setup).
        last_afe = getattr(self.asic, "last_afe", None)
        if last_afe is not None:
            self.asic.apply_frontend(last_afe)

        self.asic.protocol.write_registers_batched(sorted(cache.items()))
        self.asic._reg_cache.update(cache)

        if self._lamp_requested:
            self.asic.lamp_on()
        else:
            self.asic.lamp_off()

        # Base class feed-wait uses this; GL128 feeds synchronously above.
        self._feedl = 0

        logger.info(
            "GL128 configured %ddpi dpiset=%d lincnt=%d str=%d end=%d lperiod=%d "
            "shading=%s exposure=%d",
            dpi,
            dpiset,
            geometry.lincnt_register,
            geometry.pixel_startx,
            geometry.pixel_endx,
            model.line_period_for(dpi),
            shading,
            exposure_reg,
        )

    # --- multi-pass (ME / IR) -------------------------------------------

    def _run_multi_pass(
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
        me_exposure_mode: str | None = None,
        me_short_exposure: int | None = None,
        me_long_exposure: int | None = None,
        me_target_exposure: int | None = None,
        n_brackets: int = 2,
    ):
        from pyopticfilm.image import ScanImage
        from pyopticfilm.pass_align import (
            align_pass_to_reference,
            align_pass_to_reference_banded,
            estimate_pass_shift,
            warn_if_align_unavailable,
        )
        from pyopticfilm.scan.exposure_merge import merge_exposures_result, merge_n_exposures
        from pyopticfilm.scan.geometry import compute_geometry
        from pyopticfilm.scan.me_debug import BracketDebug, MeScanDebug
        from pyopticfilm.scan.me_exposure import fixed_long_exposure, select_long_exposure

        if mode == "infrared":
            raise ValueError("Use mode='color' with infrared=True for colour+IR scans")

        model = self.model
        # Manual short override replaces the model default for every early
        # pass (color_short and, when combined, IR) — same variable those
        # passes already shared before this feature existed.
        short_manual = me_short_exposure is not None
        exp_short = (
            int(me_short_exposure)
            if short_manual
            else int(getattr(model, "exposure_short", model.exposure_lperiod))
        )
        exp_long = int(getattr(model, "exposure_long", exp_short * 3))
        if me_exposure_mode is not None:
            # Explicit caller choice always wins, regardless of n_brackets.
            mode_norm = str(me_exposure_mode).strip().lower()
        elif n_brackets > 2:
            # No explicit choice: n_brackets > 2 defers to the model's own
            # default (Model.me_default_exposure_mode) — e.g. the 8100 V2
            # stays pinned to its one real-hardware-validated exposure
            # rather than adaptively varying per frame in the new N-bracket
            # path. n_brackets == 2 is untouched (see the branch below).
            mode_norm = str(getattr(model, "me_default_exposure_mode", "adaptive")).strip().lower()
        else:
            mode_norm = "adaptive"
        if mode_norm not in ("adaptive", "fixed"):
            raise ValueError(
                f"me_exposure_mode must be 'adaptive' or 'fixed', got {mode_norm!r}"
            )

        if not self.asic._initialized:
            self.asic.init()

        self.last_me_debug = None
        self.last_align_shift_ir = None

        if geometry is None:
            geometry = compute_geometry(resolution, model=model, area=area)

        # Short (+ optional IR) first; long exposure is chosen after short RGB.
        early: list[tuple[str, str, int, bool, bool, bool]] = [
            ("color_short", "transparency", exp_short, False, False, short_manual),
        ]
        if infrared:
            early.append(("ir", "infrared", exp_short, False, False, short_manual))
        # n_brackets counts the short pass too, so n_brackets-1 additional
        # bracket passes get acquired when multi_exposure is set.
        n_pass = len(early) + ((n_brackets - 1) if multi_exposure else 0)

        logger.info(
            "GL128 multi-pass %ddpi passes=%d me=%s n_brackets=%d ir=%s me_exposure_mode=%s",
            geometry.resolution,
            n_pass,
            multi_exposure,
            n_brackets,
            infrared,
            mode_norm,
        )

        rgb_short = None
        rgb_long = None
        ir_plane = None
        exposure_decision = None
        # Content-aware ENDPIXEL drop from the short plane; reuse for long/IR
        # so merge/align see matching widths (edge DN differs across exposures).
        locked_usb_end_drop: int | None = None

        def _acquire_pass(
            *,
            idx: int,
            key: str,
            method: str,
            exposure: int,
            remeasure: bool,
            long_pass: bool,
            manual: bool = False,
        ):
            nonlocal rgb_short, rgb_long, ir_plane, locked_usb_end_drop

            def _prog(p: float, _i: int = idx) -> None:
                if progress is not None:
                    progress(min(1.0, (_i + p) / n_pass))

            calib = None
            if apply_calib and self.calibrator is not None:
                if method == "transparency":
                    if remeasure:
                        self.asic.asic_shading_ready = False  # type: ignore[attr-defined]
                        calib = self.calibrator.measure_colour_asic_shading(geometry)
                    else:
                        calib = self.calibrator.ensure_colour_asic_shading(geometry)
                else:
                    calib = self.calibrator.find_for_scan(
                        method=method, geometry=geometry
                    )
                    if calib is None:
                        logger.warning(
                            "No calib cache for method=%s dpi=%d — scanning uncalibrated.",
                            method,
                            geometry.resolution,
                        )

            logger.info(
                "GL128 pass %s dpi=%d area=%s feed2=%d lincnt=%d str=%d end=%d %dx%d",
                key,
                geometry.resolution,
                geometry.area,
                image_feed2_steps(model, geometry),
                geometry.lincnt_register,
                geometry.pixel_startx,
                geometry.pixel_endx,
                geometry.pixels,
                geometry.lines,
            )

            self._pass_exposure = exposure
            self._pass_long_exposure = bool(long_pass)
            self._pass_manual = bool(manual)
            try:
                raw = self.acquire_raw(
                    geometry,
                    method=method,
                    lamp_on=True,
                    start_motor=True,
                    progress=_prog,
                    cancel=cancel,
                )
            finally:
                self._pass_exposure = None
                self._pass_long_exposure = False
                self._pass_manual = False

            use_host = (
                calib is not None
                and self.calibrator is not None
                and self.calibrator.should_apply_host_calib()
            )
            dark = calib.dark if use_host else None
            white = calib.white if use_host else None
            planar = getattr(self.asic, "usb_planar_rgb", None)
            if planar is None:
                planar = bool(getattr(self.model, "usb_planar_rgb", False))
            # Short discovers the dummy trim; later planes reuse that drop.
            drop_override = None if key == "color_short" else locked_usb_end_drop
            rgb = self.pipeline.assemble(
                raw,
                geometry,
                dark=dark,
                white=white,
                planar=bool(planar),
                # Keep ME colour (and IR) planes linear — per-plane expose_film_base
                # collapses the short/long ratio. Makeup runs once on the deliverable.
                expose_base=False,
                usb_end_drop=drop_override,
            )
            if key == "color_short":
                locked_usb_end_drop = int(self.pipeline.last_usb_end_drop)
                rgb_short = rgb
            elif key == "color_long":
                rgb_long = rgb
            elif key == "ir":
                ir_plane = self._infrared_plane(rgb)

        for idx, (key, method, exposure, remeasure, long_pass, manual) in enumerate(early):
            _acquire_pass(
                idx=idx,
                key=key,
                method=method,
                exposure=exposure,
                remeasure=remeasure,
                long_pass=long_pass,
                manual=manual,
            )

        assert rgb_short is not None

        long_manual = me_long_exposure is not None or me_target_exposure is not None
        if multi_exposure:
            if me_long_exposure is not None:
                # Explicit override: use the caller's value verbatim and skip
                # adaptive/fixed selection, the DPI clamp, and the hardware-max
                # clamp entirely — me_exposure_mode does not apply here.
                exp_long = int(me_long_exposure)
                logger.info(
                    "ME long exposure: manual-override selected=%d (me_exposure_mode=%s ignored)",
                    exp_long,
                    mode_norm,
                )
            elif me_target_exposure is not None:
                # "Manual" bracket target for end-user selection (NegPy): the
                # caller's value is honored, but held inside the same
                # per-model floor/ceiling adaptive uses (model.exposure_short /
                # model.me_long_exposure_ceiling) — unlike me_long_exposure,
                # which is a raw, unrestricted Scan Lab debug escape hatch.
                requested = int(me_target_exposure)
                exp_long = clamp_me_long_for_dpi(geometry.resolution, requested, model=model)
                if exp_long != requested:
                    logger.warning(
                        "ME target exposure clamped at %d dpi: %d -> %d",
                        geometry.resolution,
                        requested,
                        exp_long,
                    )
                logger.info(
                    "ME long exposure: manual-target requested=%d selected=%d "
                    "(me_exposure_mode=%s ignored)",
                    requested,
                    exp_long,
                    mode_norm,
                )
            else:
                # Intersect each model envelope field with the DPI ceiling —
                # not a redundant re-expression of me_long_exposure_ceiling():
                # a model may set me_adaptive_max_exposure/me_hardware_max_
                # exposure below the DPI ceiling, and clamp_me_long_for_dpi
                # correctly picks the smaller of the two. They only equal the
                # ceiling outright for SE/V2 today because both fields are
                # >= every DPI's ceiling on those two models.
                dpi_adaptive_max = clamp_me_long_for_dpi(
                    geometry.resolution,
                    int(getattr(model, "me_adaptive_max_exposure", exp_long)),
                    model=model,
                )
                dpi_hardware_max = clamp_me_long_for_dpi(
                    geometry.resolution,
                    int(getattr(model, "me_hardware_max_exposure", exp_long)),
                    model=model,
                )
                if mode_norm == "fixed":
                    exposure_decision = fixed_long_exposure(
                        clamp_me_long_for_dpi(geometry.resolution, exp_long, model=model),
                        short_rgb=rgb_short,
                        short_exposure=exp_short,
                        black_level=float(getattr(model, "me_black_level", 0.0)),
                    )
                else:
                    exposure_decision = select_long_exposure(
                        rgb_short,
                        exp_short,
                        black_level=float(getattr(model, "me_black_level", 0.0)),
                        dense_percentile=float(getattr(model, "me_dense_percentile", 5.0)),
                        target_dense_dn=float(getattr(model, "me_target_dense_dn", 10000.0)),
                        adaptive_min=int(
                            getattr(model, "me_adaptive_min_exposure", exp_long)
                        ),
                        adaptive_max=dpi_adaptive_max,
                        hardware_max=dpi_hardware_max,
                        max_ratio=float(getattr(model, "me_max_exposure_ratio", 5.0)),
                        default_long=clamp_me_long_for_dpi(geometry.resolution, exp_long, model=model),
                    )
                exp_long = int(exposure_decision.selected)
                clamped = clamp_me_long_for_dpi(geometry.resolution, exp_long, model=model)
                if clamped != exp_long:
                    logger.warning(
                        "ME colour-long exposure clamped at %d dpi: %d → %d",
                        geometry.resolution,
                        exp_long,
                        clamped,
                    )
                    exp_long = clamped
                p05 = exposure_decision.dense_p05
                clips = exposure_decision.predicted_clip
                logger.info(
                    "ME long exposure: short=%d  R/G/B p05=(%.0f, %.0f, %.0f)  "
                    "proposed=%d  predicted clip R/G/B=(%.2f%%, %.2f%%, %.2f%%)  "
                    "safety_max=%d  selected=%d  reason=%s",
                    exp_short,
                    p05[0],
                    p05[1],
                    p05[2],
                    exposure_decision.proposed,
                    clips[0] * 100.0,
                    clips[1] * 100.0,
                    clips[2] * 100.0,
                    dpi_hardware_max,
                    exp_long,
                    exposure_decision.reason,
                )
            if n_brackets == 2:
                # Unchanged original 2-bracket path — kept byte-identical.
                _acquire_pass(
                    idx=len(early),
                    key="color_long",
                    method="transparency",
                    exposure=exp_long,
                    remeasure=True,
                    long_pass=True,
                    manual=long_manual,
                )
            else:
                # n_brackets-1 non-short brackets, geometrically spaced
                # between exp_short and the adaptively-chosen exp_long
                # (itself unchanged from the block above — same safety
                # ceiling as today). The last scheduled value is exp_long
                # exactly, so the acquisition loop's final iteration is
                # equivalent to the n_brackets==2 case's single long pass.
                ratio = exp_long / exp_short
                schedule = [
                    round(exp_short * (ratio ** (i / (n_brackets - 1))))
                    for i in range(1, n_brackets)
                ]
                schedule[-1] = exp_long
                # (exposure, decoded rgb) per non-short bracket, ascending order.
                bracket_planes = []
                for i, e in enumerate(schedule):
                    _acquire_pass(
                        idx=len(early) + i,
                        key="color_long",
                        method="transparency",
                        exposure=e,
                        remeasure=True,
                        long_pass=True,
                        manual=long_manual and i == len(schedule) - 1,
                    )
                    bracket_planes.append((e, rgb_long))

        align_shift_long: tuple[float, float] | None = None
        align_shift_ir: tuple[float, float] | None = None
        # Model-gated: see Model8100V2.me_use_banded_alignment. Routes the
        # 2-bracket path through the same row-banded alignment + luma-only
        # misalignment gate already used for n_brackets > 2, instead of
        # merge_exposures_result's whole-frame shift + AND-gated fallback.
        # SE keeps the original byte-identical path (flag defaults False).
        use_banded_me = n_brackets == 2 and bool(model.me_use_banded_alignment)

        if n_brackets == 2 and align_passes and rgb_long is not None:
            warn_if_align_unavailable("ME long")
            if use_banded_me:
                rgb_long, align_shift_long = align_pass_to_reference_banded(rgb_short, rgb_long)
            else:
                align_shift_long = estimate_pass_shift(rgb_short, rgb_long)
            logger.info(
                "ME long pass shift (dx, dy)=(%.3f, %.3f)",
                align_shift_long[0],
                align_shift_long[1],
            )
        if align_passes and ir_plane is not None:
            warn_if_align_unavailable("IR")
            ir_plane, align_shift_ir = align_pass_to_reference(rgb_short, ir_plane)
            self.last_align_shift_ir = align_shift_ir
            logger.info(
                "IR pass shift (dx, dy)=(%.3f, %.3f)",
                align_shift_ir[0],
                align_shift_ir[1],
            )

        primary = rgb_short
        fusion_stats = None
        # Shared across both branches below — the adaptive/fixed decision and
        # manual-override bookkeeping don't depend on n_brackets.
        exposure_proposed = None if exposure_decision is None else exposure_decision.proposed
        exposure_reason = (
            "manual-target" if me_target_exposure is not None
            else "manual-override" if long_manual
            else (None if exposure_decision is None else exposure_decision.reason)
        )
        me_alpha = float(getattr(model, "me_noise_alpha", 1.0))
        me_beta = float(getattr(model, "me_noise_beta", 4096.0))

        if multi_exposure and n_brackets == 2 and rgb_long is not None:
            if use_banded_me:
                # rgb_long is already banded-aligned above (or untouched if
                # align_passes was False, matching the non-banded path's own
                # "no alignment requested" semantics).
                merged = merge_n_exposures(
                    [rgb_short, rgb_long], [exp_short, exp_long], alpha=me_alpha, beta=me_beta
                )
            else:
                shift = align_shift_long if align_passes else (0.0, 0.0)
                merged = merge_exposures_result(
                    rgb_short,
                    rgb_long,
                    exposure_short=exp_short,
                    exposure_long=exp_long,
                    align_shift=shift,
                    alpha=me_alpha,
                    beta=me_beta,
                )
            primary = merged.rgb
            fusion_stats = merged.fusion_stats
            self.last_me_debug = MeScanDebug(
                rgb_short=rgb_short,
                rgb_long=rgb_long,
                exposure_short=exp_short,
                exposure_long=exp_long,
                fusion_stats=fusion_stats,
                align_shift_long=align_shift_long,
                align_shift_ir=align_shift_ir,
                exposure_proposed=exposure_proposed,
                exposure_reason=exposure_reason,
                brackets=[
                    BracketDebug(rgb=rgb_short, exposure=exp_short, align_shift=None),
                    BracketDebug(rgb=rgb_long, exposure=exp_long, align_shift=align_shift_long),
                ],
            )
        elif multi_exposure and n_brackets > 2 and bracket_planes:
            # Align every non-short bracket to rgb_short individually, then
            # fuse with the N-way IVW generalization — see exposure_merge.py::
            # merge_n_exposures, which also carries the residual-disagreement
            # gate and misalignment fallback from the 2-way merge.
            #
            # Uses the row-banded estimator (not the single whole-frame
            # align_pass_to_reference used by the 2-bracket path above) —
            # these are typically the tallest passes (crop/strip windows),
            # where drift can vary along the pass rather than being one
            # constant offset; see align_pass_to_reference_banded.
            frames = [rgb_short]
            exposures = [exp_short]
            bracket_debugs = [BracketDebug(rgb=rgb_short, exposure=exp_short, align_shift=None)]
            for e, rgb in bracket_planes:
                if align_passes:
                    warn_if_align_unavailable("ME bracket")
                    warped, shift = align_pass_to_reference_banded(rgb_short, rgb)
                else:
                    warped, shift = rgb, None
                frames.append(warped)
                exposures.append(e)
                bracket_debugs.append(BracketDebug(rgb=warped, exposure=e, align_shift=shift))
                logger.info(
                    "ME bracket exposure=%d shift=%s",
                    e,
                    None if shift is None else (round(shift[0], 3), round(shift[1], 3)),
                )
            # Backward-compat field: shift of the top/last bracket (same role
            # as the 2-bracket case's "shift of the long pass").
            align_shift_long = bracket_debugs[-1].align_shift

            merged = merge_n_exposures(frames, exposures, alpha=me_alpha, beta=me_beta)
            primary = merged.rgb
            fusion_stats = merged.fusion_stats
            self.last_me_debug = MeScanDebug(
                rgb_short=rgb_short,
                rgb_long=frames[-1],
                exposure_short=exp_short,
                exposure_long=exposures[-1],
                fusion_stats=fusion_stats,
                align_shift_long=align_shift_long,
                align_shift_ir=align_shift_ir,
                exposure_proposed=exposure_proposed,
                exposure_reason=exposure_reason,
                brackets=bracket_debugs,
            )

        # Single film-base makeup on the deliverable only (not on bracket planes).
        # Headroom cap keeps IVW highlight recovery from being crushed to white.
        primary = self.pipeline.expose_film_base(
            primary, source="me deliverable", preserve_headroom=True
        )
        primary = self.pipeline.clamp_border_highlights(primary)

        return ScanImage(
            rgb=primary,
            dpi=geometry.resolution,
            device_model=f"{self.model.vendor} {self.model.model}",
            ir=ir_plane,
        )

    # --- acquire --------------------------------------------------------

    def _begin_scan(self, *, start_motor: bool = True) -> None:
        """Capture order: ``0x0d=0x07`` → set SCAN → ``0x0f`` (session 03)."""
        r = self.se_regs
        proto = self.asic.protocol

        proto.write_register(r.REG_CLRCNT, r.CLRCNT_ALL)
        reg01 = proto.read_register(r.REG_0x01) | r.SCAN
        proto.write_register(r.REG_0x01, reg01)
        self.asic._reg_cache[r.REG_0x01] = reg01
        proto.write_register(r.REG_START, r.START_GO if start_motor else 0x00)
        logger.info("GL128 scan started motor=%s", start_motor)

    def _wait_data(self, cancel: threading.Event | None) -> None:
        """Wait until the ASIC reports data in its buffer.

        GL845 also cross-checks the valid-word counters at ``0x42``-``0x45``;
        the SE captures never touch those, so buffer state is all there is.
        """
        deadline = time.monotonic() + DATA_TIMEOUT_S
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                raise ScanCancelled("cancelled waiting for data")
            if not self.asic.read_status().is_buffer_empty:
                return
            time.sleep(0.02)
        raise ScanError(f"No scan data within {DATA_TIMEOUT_S:.0f}s")

    def _verify_geometry_usb_span(self, geometry: ScanGeometry) -> None:
        """Fail loud when STR/END span disagrees with USB line width (diamond shear)."""
        span = int(geometry.pixel_endx) - int(geometry.pixel_startx)
        if span != int(geometry.optical_pixels):
            raise ScanError(
                f"STR/END span {span} != optical_pixels {geometry.optical_pixels} "
                f"at {geometry.resolution} dpi"
            )
        sample_bytes = 2 if geometry.depth == 16 else 1
        expected_line = geometry.pixels * geometry.channels * sample_bytes
        if geometry.line_bytes != expected_line:
            raise ScanError(
                f"USB line_bytes {geometry.line_bytes} != pixels×channels×sample "
                f"({expected_line}) at {geometry.resolution} dpi"
            )

    def _line_interval_s(self, geometry: ScanGeometry) -> float:
        lperiod = float(self.model.line_period_for(geometry.resolution))
        return lperiod * _LINE_PERIOD_TO_SECONDS

    @staticmethod
    def _chunk_bytes(geometry: ScanGeometry, remaining: int) -> int:
        raw_line = getattr(geometry, "line_bytes", 0)
        try:
            line = max(1, int(raw_line))
        except (TypeError, ValueError):
            line = IMAGE_CHUNK_BYTES
        if remaining <= line:
            return remaining
        if line > IMAGE_CHUNK_BYTES:
            return min(line, remaining)
        full_lines = min(remaining // line, IMAGE_CHUNK_BYTES // line)
        if full_lines >= 1:
            return full_lines * line
        return min(line, remaining)

    def _acquire(
        self,
        geometry: ScanGeometry,
        *,
        progress: Callable[[float], None] | None,
        cancel: threading.Event | None,
        wait_feed: bool = True,
    ) -> bytes:
        del wait_feed  # GL128 feeds synchronously in _configure
        self._wait_data(cancel)

        r = self.se_regs
        proto = self.asic.protocol
        total = geometry.total_bytes
        index = (
            r.BULK_INDEX_RAM
            if geometry.disable_buffer_full_move
            else r.BULK_INDEX_IMAGE
        )

        buf = bytearray()
        pace_on = float(getattr(self.asic, "image_usb_pace_s", IMAGE_USB_PACE_S) or 0.0) > 0
        line_interval = self._line_interval_s(geometry) if pace_on else 0.0
        while len(buf) < total:
            if cancel is not None and cancel.is_set():
                raise ScanCancelled("cancelled during bulk read")
            remaining = total - len(buf)
            want = self._chunk_bytes(geometry, remaining)
            t0 = time.monotonic()
            proto.bulk_read_begin(want, index=index)
            self._bulk_stream_active = True
            try:
                chunk = proto.bulk_read_exact(want)
            finally:
                self._bulk_stream_active = False
            if not chunk:
                raise ScanError(
                    f"Bulk stream ended after {len(buf)} of {total} bytes"
                )
            buf.extend(chunk)
            if progress is not None:
                progress(min(1.0, len(buf) / total))
            if pace_on and line_interval > 0:
                try:
                    line_bytes = max(1, int(geometry.line_bytes))
                except (TypeError, ValueError):
                    line_bytes = IMAGE_CHUNK_BYTES
                lines = max(1.0, len(chunk) / line_bytes)
                expected = line_interval * lines * _QUIET_DRAIN_LAG
                elapsed = time.monotonic() - t0
                throttle = max(0.0, expected - elapsed)
                if throttle > 0:
                    time.sleep(throttle)

        if progress is not None:
            progress(1.0)
        return bytes(buf[:total])

    def _end_scan(self) -> None:
        # Capture end/cancel recipe (lamp strobe + clear SCAN + AGOHOME park)
        # lives in Gl128.stop_motor — do not bare-clear 0x01 here or the strobe
        # order is lost and SCAN is cleared twice.
        #
        # Mid-bulk cancel must stop the ASIC first, then abort the host bulk IN
        # pipe; otherwise the next Scanner.open()/init control transfers time out
        # until power-cycle (Phase 2 repro).
        try:
            # Stop ASIC DMA first (clear SCAN / AGOHOME park), then abort the
            # host bulk IN so the pipe is not left half-open across close/reopen.
            super()._end_scan()
        finally:
            if self._bulk_stream_active:
                self._bulk_stream_active = False
                try:
                    drained = self.asic.protocol.abort_bulk_stream()
                    logger.info("GL128 bulk abort after end_scan drained=%d", drained)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("GL128 bulk abort after end_scan: %s", exc)
            self._await_agohome_park = False
