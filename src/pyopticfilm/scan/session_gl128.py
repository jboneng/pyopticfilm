# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan session for GL128 OpticFilm (8200i SE, 8100 V2, and 8300i SE).

Only the chip-specific steps are overridden; run/assemble/calibration lookup
stay in :class:`~pyopticfilm.scan.session.ScanSession`. What differs from GL845:

* the register block comes from the model tables plus a small set of
  resolution-dependent values, instead of being computed from a motor profile;
* motor slope tables are replayed from the capture rather than generated, so
  there is no ``zmod`` calculation;
* feeding is a separate, synchronous move before the scan starts;
* the image is streamed with ``VALUE_BUFFER`` announces of at most
  ``model.image_read_chunk_bytes`` (a single full-image preamble was louder on
  real GL128 hardware); source is selected with ``wIndex`` — RAM for
  calibration, live stream for a scan.
"""

from __future__ import annotations

import functools
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from pyopticfilm.asic.registers import Gl128Registers
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.protocol import AsicDriver, FilmModel, Gl128Model
from pyopticfilm.device.tables_8200i_se import exposure_table
from pyopticfilm.exceptions import AsicError, ScanCancelled, ScanError
from pyopticfilm.logging import get_logger
from pyopticfilm.scan.calibrate import Calibrator
from pyopticfilm.scan.exposure_override import validate_manual_exposure, validate_n_passes
from pyopticfilm.scan.geometry import ScanGeometry
from pyopticfilm.scan.me_exposure import validate_me_long_mode
from pyopticfilm.scan.session import DATA_TIMEOUT_S, ScanSession
from pyopticfilm.usb.device import BULK_MAX_SIZE

logger = get_logger(__name__)

#: Default max bytes announced per ``bulk_read_begin`` when the model sets no
#: ``image_read_chunk_bytes``. SilverFast uses one preamble for the whole image;
#: chunked announces let cancel stop between chunks with no transfer in flight.
IMAGE_CHUNK_BYTES = BULK_MAX_SIZE

#: Environment variable naming a CSV that each image read appends per-chunk
#: timing to (``dpi,offset,bytes,read_s,gap_s``).
ACQUIRE_TIMING_ENV = "PYOPTICFILM_ACQUIRE_TIMING"

#: Low bits of ``0x1D`` for an image pass, by ASIC dpi (8200i SE 13_ppi_ladder,
#: 8100 V2 04_color_7200). SilverFast writes ``0x81`` before a 7200 dpi image;
#: with ``0x80`` a full-width line outruns USB and the motor stops and restarts.
_IMAGE_REG_1D_LOW_BY_DPI: dict[int, int] = {7200: 0x01}
_REG_1D = 0x1D
_REG_1D_LOW_MASK = 0x1F

#: AHB scan motor table words are 16-bit. At oversample == 1 (native
#: optical resolution, 7200 dpi) ``channel_exposure_for`` copies
#: ``REG_EXPOSURE`` into that table unmasked. A value at or above 65536 wraps
#: there while the 24-bit exposure register does not, desyncing line timing
#: and jamming the motor (issue #66). 64000 is a margin under that wrap.
#: Where oversample is 2 or more the same margin is ``64000 * oversample``,
#: so the shared ``me_long_clamp_max`` of 85000 is left intact.
_ME_CHANNEL_EXPOSURE_MAX = 64_000


def clamp_me_long(model: Gl128Model, resolution: int, exp_long: int) -> int:
    """Clamp ME colour-long exposure to the shared range and the AHB table.

    ``me_long_clamp_min`` / ``me_long_clamp_max`` are shared by both GL128
    models (14000–85000). The ceiling is also
    ``min(model max, 64000 * oversample(resolution))`` so a native 7200 dpi
    pass cannot overflow the 16-bit scan motor table.
    """
    lo = int(model.me_long_clamp_min)
    hi = int(model.me_long_clamp_max)
    oversample = max(1, int(model.oversample_for(resolution)))
    hi = min(hi, _ME_CHANNEL_EXPOSURE_MAX * oversample)
    return min(max(int(exp_long), lo), hi)


def native_line_floor(model: Gl128Model) -> int:
    """Lowest scan motor table word for a driver-derived image pass at oversample 1.

    Below ``exposure_short * native_image_exposure_factor`` (28000 on the
    8200i SE / 8100 V2, 15000 on the 8300i SE) a full-width 7200 dpi line comes
    out faster than USB drains it, and the motor stops and restarts.
    """
    factor = max(1, int(getattr(model, "native_image_exposure_factor", 1)))
    return int(model.exposure_short) * factor


def gl128_multi_pass_allowed(model: FilmModel, resolution: int) -> bool:
    """Whether multi-exposure and Multi-Pass (``n_passes > 1``) may run at ``resolution``."""
    allowed_at = getattr(model, "multi_pass_allowed_at", None)
    if not callable(allowed_at):
        return True
    return bool(allowed_at(int(resolution)))


def check_gl128_multi_pass(
    model: FilmModel, resolution: int, *, multi_exposure: bool, n_passes: int
) -> None:
    """Raise :class:`ScanError` for multi-exposure / Multi-Pass where they are disabled."""
    if not (multi_exposure or n_passes > 1):
        return
    if gl128_multi_pass_allowed(model, resolution):
        return
    mode = "multi_exposure=True" if multi_exposure else f"n_passes={n_passes}"
    raise ScanError(
        f"{mode} is not supported at {int(resolution)} dpi on {model.model}: "
        "multi-exposure and Multi-Pass are disabled at native resolution. "
        "Scan a single pass, or pick a lower resolution."
    )


try:
    from pyopticfilm.asic.gl128 import MOTOR_GATED_HINT as _MOTOR_GATED_HINT
except ImportError:  # pragma: no cover
    _MOTOR_GATED_HINT = "GL128 motor moves are temporarily disabled."


def image_feed2_steps(
    model: FilmModel,
    geometry: ScanGeometry,
    *,
    long_exposure: bool = False,
) -> int:
    """Second-feed steps for an image pass.

    Prefer ``geometry.area``. If that tuple was dropped, reconstruct from
    ``area_y1`` so a crop does not fall back to the full-frame feed2.
    Full-frame (no crop) uses :meth:`~Gl128Common.feed_to_scan_steps_for_dpi`
    when available so PPI-dependent leaves (8300i SE) match captures.
    """
    area = geometry.area
    if area is None:
        y1 = float(getattr(geometry, "area_y1", 0.0) or 0.0)
        if y1 > 1e-9:
            area = (0.0, y1, 1.0, 1.0)
    if area is None:
        dpi_fn = getattr(model, "feed_to_scan_steps_for_dpi", None)
        if callable(dpi_fn):
            return int(
                dpi_fn(int(geometry.resolution), long_exposure=bool(long_exposure))
            )
        return int(getattr(model, "feed_to_scan_steps", 0) or 0)
    feed_fn = getattr(model, "feed_to_scan_steps_for_area", None)
    if not callable(feed_fn):
        return int(getattr(model, "feed_to_scan_steps", 0) or 0)
    return int(feed_fn(area))


class Gl128ScanSession(ScanSession):
    """GL128 scan state machine for OpticFilm 8200i SE, 8100 (V2), and 8300i SE.

    The 8300i SE uses the same session path with leaf-specific tables.
    """

    def __init__(
        self,
        asic: AsicDriver,
        model: FilmModel = MODEL_8200I_SE,
        calibrator: Calibrator | None = None,
    ) -> None:
        super().__init__(asic, model, calibrator)
        self.se_regs = Gl128Registers()
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
        #: Lab-only Multi-Pass per-slot stacking stats, set when ``n_passes > 1``.
        self.last_multi_pass_debug = None
        #: IR→short alignment shift from the last multi-pass scan (ME or IR-only).
        self.last_align_shift_ir: tuple[float, float] | None = None
        #: True when this scan runs colour calibration, which loads the ASIC
        #: shading table for its window. Without it the table in RAM is from an
        #: earlier scan (any resolution / width), so DVDSET must stay off.
        self._use_asic_shading: bool = False

    def run(
        self,
        *args,
        multi_exposure: bool = False,
        infrared: bool = False,
        align_passes: bool = True,
        single_pass_exposure: int | None = None,
        me_short_exposure: int | None = None,
        me_long_exposure: int | None = None,
        me_long_mode: str = "adaptive",
        n_passes: int = 1,
        **kwargs,
    ):  # type: ignore[no-untyped-def]
        """Refuse unless the ASIC explicitly arms motor moves."""
        if not getattr(self.asic, "_motor_moves_enabled", False):
            raise AsicError(_MOTOR_GATED_HINT)
        validate_me_long_mode(me_long_mode)
        # Fail fast on bad manual-exposure input before any register write.
        validate_manual_exposure(single_pass_exposure, label="single_pass_exposure")
        validate_manual_exposure(me_short_exposure, label="me_short_exposure")
        validate_manual_exposure(me_long_exposure, label="me_long_exposure")
        validate_n_passes(n_passes)
        self._use_asic_shading = bool(kwargs.get("apply_calib", True)) and self.calibrator is not None
        geometry = kwargs.get("geometry")
        resolution = getattr(geometry, "resolution", None) or kwargs.get("resolution", 1800)
        check_gl128_multi_pass(
            self.model, resolution, multi_exposure=multi_exposure, n_passes=n_passes
        )
        if multi_exposure or n_passes > 1 or (infrared and kwargs.get("mode", "color") == "color"):
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
                single_pass_exposure=single_pass_exposure,
                me_short_exposure=me_short_exposure,
                me_long_exposure=me_long_exposure,
                me_long_mode=me_long_mode,
                n_passes=n_passes,
                **kwargs,
            )
        self.asic.enter_scan_clock()
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
        # Infrared must never keep DVDSET: a colour table after a colour+IR pair
        # makes the IR frame magenta, uneven, and low-contrast for dust, and an
        # IR table streaks it (Gl128.run_asic_shading); the host flattens IR.
        # An uncalibrated scan (apply_calib=False) must not reuse the table an
        # earlier scan left in RAM: it fits that scan's window, not this one.
        reg01 = (cache.get(r.REG_0x01, 0x22) | r.SHDAREA) & ~r.SCAN & ~r.STAGGER
        infrared = getattr(self.asic, "_scan_method", None) == "infrared"
        ready = self._use_asic_shading and getattr(self.asic, "asic_shading_ready", False)
        if shading or infrared or not ready:
            reg01 &= ~r.DVDSET
        cache[r.REG_0x01] = reg01

        # Leave lamp / IR LED alone — :meth:`Gl128.lamp_on` already programmed
        # ``0x03`` / ``0x37`` for the scan method; rewriting them from the boot
        # cache would clear IR LED or re-enable the white lamp.
        cache.pop(r.REG_0x03, None)
        cache.pop(r.REG_IR, None)
        # Leave the AFE ports alone too: a ``0x5E`` write commits a value to the
        # FE index in ``0x51``, so the boot map's ``0x5E=0x1F`` blasted after
        # :meth:`apply_frontend` overwrote the blue offset (FE index 7).
        cache.pop(r.REG_FE_INDEX, None)
        cache.pop(r.REG_FE_HIGH, None)
        cache.pop(r.REG_FE_LOW, None)

        if not shading:
            low = _IMAGE_REG_1D_LOW_BY_DPI.get(model.asic_dpi_for(dpi), 0)
            cache[_REG_1D] = (cache.get(_REG_1D, 0x80) & ~_REG_1D_LOW_MASK) | low

        motor = r.MTRPWR
        if not shading:
            # Image pass: AGOHOME parks the carriage when the scan ends.
            motor |= r.AGOHOME
        cache[r.REG_0x02] = motor

        self._set24(cache, r.REG_LINCNT, geometry.lincnt_register)
        span = geometry.pixel_endx - geometry.pixel_startx
        self._set24(cache, r.REG_LPERIOD, model.line_period_for(dpi, span=span))
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

        ch_exp_fn = getattr(model, "channel_exposure_for", None)
        if callable(ch_exp_fn):
            try:
                channel_exp = int(ch_exp_fn(dpi, exposure=exposure_reg))
            except TypeError:
                channel_exp = int(ch_exp_fn(dpi))
        else:
            channel_exp = None
        if (
            channel_exp is not None
            and not shading
            and not self._pass_manual
            and model.oversample_for(dpi) == 1
        ):
            channel_exp = max(channel_exp, native_line_floor(model))
        # Validate before any motor move below: an out-of-range channel_exp
        # (e.g. a manual-override exposure above the 16-bit AHB table) must
        # raise here rather than after position_for_full_frame_scan already
        # moved the carriage, which would strand it mid-window.
        if channel_exp is not None:
            exposure_table(channel_exp)

        # Capture-constant feeds from home — never geometry.starty (that was the
        # grinding bug). Calibration passes stay put (no motor). Positioning is
        # skipped while motor moves are gated so configure unit tests stay safe.
        if not shading and getattr(self.asic, "_motor_moves_enabled", False):
            scan_steps = image_feed2_steps(
                model,
                geometry,
                long_exposure=bool(self._pass_long_exposure),
            )
            # The scan must stop where SilverFast's own full frames stop:
            # feed2 + travel <= 27648 steps. Overrunning the window end is what
            # ground the motor in the Lab.
            max_fn = getattr(model, "max_safe_lincnt_for", None)
            max_lc = max_fn(scan_steps, dpi) if callable(max_fn) else None
            if max_lc is not None and geometry.lincnt_register > int(max_lc):
                start_mm = scan_steps * 25.4 / model.feed_steps_per_inch
                raise ScanError(
                    f"Image LINCNT {geometry.lincnt_register} at {dpi} dpi is "
                    f"{geometry.travel_mm:.1f} mm of travel from feed2="
                    f"{scan_steps} ({start_mm:.1f} mm), past the "
                    f"{model.max_travel_end_steps * 25.4 / model.feed_steps_per_inch:.1f} mm "
                    f"travel limit. Max LINCNT here is {max_lc} "
                    "(see docs/gl128-model-comparison.md, section 4)."
                )
            # Feeds run under the image registers, like SilverFast's. SF does
            # not rewrite 0x2B before them, so they keep its white shading
            # strip's dummy (differs from the image's on the 8300i SE <= 600
            # and the 8100 V2 at 3600).
            feed_regs = dict(cache)
            feed_regs[0x2B] = model.shading_strip_clocks(dpi, dvdset=True)[0]
            self.asic.position_for_full_frame_scan(scan_steps=scan_steps, scan_regs=feed_regs)

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
        if not shading:
            # The next stationary strip block re-initialises the ASIC first (B8).
            self.asic.image_pass_since_init = True

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
            model.line_period_for(dpi, span=span),
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
        single_pass_exposure: int | None = None,
        me_short_exposure: int | None = None,
        me_long_exposure: int | None = None,
        me_long_mode: str = "adaptive",
        n_passes: int = 1,
    ):
        from pyopticfilm.image import ScanImage
        from pyopticfilm.pass_align import (
            align_pass_to_reference,
            align_pass_to_reference_banded,
            estimate_pass_shift,
            warn_if_align_unavailable,
        )
        from pyopticfilm.scan.exposure_merge import merge_exposures_result, merge_n_passes
        from pyopticfilm.scan.geometry import compute_geometry
        from pyopticfilm.scan.me_debug import MeScanDebug, MultiPassDebug, SlotStackDebug
        from pyopticfilm.scan.me_exposure import select_long_exposure
        from pyopticfilm.scan.pipeline import HOST_CALIB_BORDER_INSET

        if mode == "infrared":
            raise ValueError("Use mode='color' with infrared=True for colour+IR scans")
        if infrared and n_passes > 1:
            raise ScanError(
                "n_passes > 1 is not supported together with infrared=True yet "
                "(each repeat is its own motor cycle and IR stacking is unvalidated) "
                "— scan infrared separately, or use n_passes=1."
            )

        model = self.model
        # me_short_exposure (ME's own override key) always wins. Without ME the
        # short slot *is* the single pass, so single_pass_exposure sets it for
        # Multi-Pass and for colour+IR alike (the IR pass follows the short, as
        # with me_short_exposure). Calls without an override are unchanged.
        if me_short_exposure is not None:
            short_manual = True
            exp_short = int(me_short_exposure)
        elif not multi_exposure and single_pass_exposure is not None:
            short_manual = True
            exp_short = int(single_pass_exposure)
        else:
            short_manual = False
            exp_short = int(getattr(model, "exposure_short", model.exposure_lperiod))
        exp_long = int(getattr(model, "exposure_long", exp_short * 3))
        noise_alpha = float(getattr(model, "me_noise_alpha", 1.0))
        noise_beta = float(getattr(model, "me_noise_beta", 4096.0))

        if not self.asic._initialized:
            self.asic.init()

        self.last_me_debug = None
        self.last_multi_pass_debug = None
        self.last_align_shift_ir = None

        if geometry is None:
            geometry = compute_geometry(resolution, model=model, area=area)

        # Total physical motor passes: n_passes repeats of the short/single
        # slot, one IR pass (only ever paired with n_passes==1, guarded
        # above), and n_passes repeats of the long slot when doing ME.
        n_pass = n_passes + (1 if infrared else 0) + (n_passes if multi_exposure else 0)

        logger.info(
            "GL128 multi-pass %ddpi passes=%d n_passes=%d me=%s ir=%s",
            geometry.resolution,
            n_pass,
            n_passes,
            multi_exposure,
            infrared,
        )

        ir_plane = None
        exposure_decision = None
        fixed_reason: str | None = None
        # Content-aware ENDPIXEL drop, discovered on the very first capture of
        # the whole scan and reused by every later capture — including
        # Multi-Pass repeats of the same slot — so merge/align see matching
        # widths (edge DN differs across exposures).
        locked_usb_end_drop: int | None = None
        pass_idx = 0

        def _acquire_pass(
            *,
            key: str,
            method: str,
            exposure: int,
            remeasure: bool,
            long_pass: bool,
            manual: bool = False,
        ) -> np.ndarray:
            nonlocal locked_usb_end_drop, pass_idx
            idx = pass_idx
            pass_idx += 1

            def _prog(p: float, _i: int = idx) -> None:
                if progress is not None:
                    progress(min(1.0, (_i + p) / n_pass))

            self.asic.enter_scan_clock()
            calib = None
            if apply_calib and self.calibrator is not None:
                if method == "transparency":
                    if remeasure:
                        self.asic.asic_shading_ready = False  # type: ignore[attr-defined]
                        calib = self.calibrator.measure_colour_asic_shading(geometry)
                    else:
                        calib = self.calibrator.ensure_colour_asic_shading(geometry)
                else:
                    self.calibrator.measure_ir_host_flatten(geometry)

            logger.info(
                "GL128 pass %s dpi=%d area=%s feed2=%d lincnt=%d str=%d end=%d %dx%d",
                key,
                geometry.resolution,
                geometry.area,
                image_feed2_steps(model, geometry, long_exposure=bool(long_pass)),
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
            drop_override = None if locked_usb_end_drop is None else locked_usb_end_drop
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
            if locked_usb_end_drop is None:
                locked_usb_end_drop = int(self.pipeline.last_usb_end_drop)
            return rgb

        def _capture_stacked_slot(
            *,
            key: str,
            method: str,
            exposure: int,
            long_pass: bool,
            manual: bool,
            remeasure_first: bool,
        ) -> tuple[np.ndarray, list[tuple[float, float]], object | None]:
            """Capture ``key`` ``n_passes`` times, align repeats 2..N to
            repeat 1 (when ``align_passes``), and stack via
            :func:`merge_n_passes`. Reduces to a single ``_acquire_pass``
            call with no stacking at ``n_passes == 1`` — byte-identical to
            the pre-Multi-Pass single-capture-per-slot behavior. Only the
            first repeat of a slot honors ``remeasure_first`` (a shading
            remeasure is about switching exposure levels, not about the
            repeat count); every later repeat reuses the cached shading."""
            shifts: list[tuple[float, float]] = []
            first = _acquire_pass(
                key=key,
                method=method,
                exposure=exposure,
                remeasure=remeasure_first,
                long_pass=long_pass,
                manual=manual,
            )
            if n_passes == 1:
                return first, shifts, None
            frames = [first]
            for _ in range(n_passes - 1):
                frame = _acquire_pass(
                    key=key,
                    method=method,
                    exposure=exposure,
                    remeasure=False,
                    long_pass=long_pass,
                    manual=manual,
                )
                if align_passes:
                    warn_if_align_unavailable(f"Multi-Pass {key} repeat")
                    aligned, shift = align_pass_to_reference_banded(frames[0], frame)
                    frames.append(aligned)
                    shifts.append(shift)
                else:
                    frames.append(frame)
            stacked = merge_n_passes(frames, alpha=noise_alpha, beta=noise_beta)
            return stacked.rgb, shifts, stacked.fusion_stats

        # Short (+ optional IR) first; long exposure is chosen after short RGB.
        rgb_short, short_align_shifts, short_stack_stats = _capture_stacked_slot(
            key="color_short",
            method="transparency",
            exposure=exp_short,
            long_pass=False,
            manual=short_manual,
            remeasure_first=False,
        )

        if infrared:
            ir_raw = _acquire_pass(
                key="ir",
                method="infrared",
                exposure=exp_short,
                remeasure=False,
                long_pass=False,
                manual=short_manual,
            )
            ir_plane = self._infrared_plane(ir_raw)

        assert rgb_short is not None

        long_manual = me_long_exposure is not None
        rgb_long = None
        long_align_shifts: list[tuple[float, float]] = []
        long_stack_stats = None
        if multi_exposure:
            if long_manual:
                # Explicit override: use the caller's value verbatim and skip
                # adaptive selection and the hardware-max clamp entirely.
                exp_long = int(me_long_exposure)
                logger.info(
                    "ME long exposure: manual-override selected=%d",
                    exp_long,
                )
            elif me_long_mode == "fixed":
                # SilverFast-style: the model's fixed long exposure on every
                # frame, no metering, still inside the driver's safety clamp.
                requested = int(getattr(model, "me_fixed_long_exposure", exp_long))
                exp_long = clamp_me_long(model, geometry.resolution, requested)
                fixed_reason = "fixed" if exp_long == requested else "fixed (clamped)"
                logger.info(
                    "ME long exposure: fixed requested=%d selected=%d",
                    requested,
                    exp_long,
                )
            else:
                adaptive_max = clamp_me_long(
                    model,
                    geometry.resolution,
                    int(getattr(model, "me_adaptive_max_exposure", exp_long)),
                )
                hardware_max = clamp_me_long(
                    model,
                    geometry.resolution,
                    int(getattr(model, "me_hardware_max_exposure", exp_long)),
                )
                # Meter the film, not the holder: the default frame carries ~5 %
                # near-black holder rows/columns, right at the 5th percentile,
                # which drove the proposal to the cap. Same inset as the makeup.
                region = self.pipeline._inset_slice(
                    rgb_short.shape[0], rgb_short.shape[1], HOST_CALIB_BORDER_INSET
                )
                metered = rgb_short[region] if region is not None else rgb_short
                exposure_decision = select_long_exposure(
                    metered,
                    exp_short,
                    black_level=float(getattr(model, "me_black_level", 0.0)),
                    dense_percentile=float(getattr(model, "me_dense_percentile", 5.0)),
                    target_dense_dn=float(getattr(model, "me_target_dense_dn", 10000.0)),
                    adaptive_min=int(
                        getattr(model, "me_adaptive_min_exposure", exp_long)
                    ),
                    adaptive_max=adaptive_max,
                    hardware_max=hardware_max,
                    max_ratio=float(getattr(model, "me_max_exposure_ratio", 5.0)),
                    default_long=clamp_me_long(model, geometry.resolution, exp_long),
                )
                exp_long = int(exposure_decision.selected)
                clamped = clamp_me_long(model, geometry.resolution, exp_long)
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
                    hardware_max,
                    exp_long,
                    exposure_decision.reason,
                )
            rgb_long, long_align_shifts, long_stack_stats = _capture_stacked_slot(
                key="color_long",
                method="transparency",
                exposure=exp_long,
                long_pass=True,
                manual=long_manual,
                remeasure_first=True,
            )

        if n_passes > 1:
            self.last_multi_pass_debug = MultiPassDebug(
                short=SlotStackDebug(
                    n_passes=n_passes,
                    align_shifts=short_align_shifts,
                    stack_stats=short_stack_stats,
                ),
                long=(
                    SlotStackDebug(
                        n_passes=n_passes,
                        align_shifts=long_align_shifts,
                        stack_stats=long_stack_stats,
                    )
                    if multi_exposure
                    else None
                ),
            )

        align_shift_long: tuple[float, float] | None = None
        align_shift_ir: tuple[float, float] | None = None

        if align_passes and rgb_long is not None:
            warn_if_align_unavailable("ME long")
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
        if multi_exposure and rgb_long is not None:
            shift = align_shift_long if align_passes else (0.0, 0.0)
            merged = merge_exposures_result(
                rgb_short,
                rgb_long,
                exposure_short=exp_short,
                exposure_long=exp_long,
                align_shift=shift,
                alpha=noise_alpha,
                beta=noise_beta,
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
                exposure_proposed=(
                    None if exposure_decision is None else exposure_decision.proposed
                ),
                exposure_reason=(
                    "manual-override"
                    if long_manual
                    else fixed_reason
                    or (None if exposure_decision is None else exposure_decision.reason)
                ),
            )

        # Single film-base makeup on the deliverable only (not on bracket planes).
        # Headroom cap keeps IVW highlight recovery from being crushed to white —
        # only relevant when an actual IVW merge happened; plain Multi-Pass
        # stacking (no ME) should stretch highlights exactly like Single-Pass.
        # n_passes==1 must stay byte-identical to main for every existing call
        # shape (Single-Pass, ME, IR-combo), so the relaxed gate only applies
        # to the new n_passes>1-without-ME stacking mode.
        preserve_headroom = (multi_exposure and rgb_long is not None) or n_passes == 1
        primary = self.pipeline.expose_film_base(
            primary,
            source="me deliverable",
            preserve_headroom=preserve_headroom,
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

        if start_motor:
            self.asic.wait_motor_idle("before the image pass START")
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

    @staticmethod
    def _chunk_bytes(
        geometry: ScanGeometry, remaining: int, max_bytes: int = IMAGE_CHUNK_BYTES
    ) -> int:
        raw_line = getattr(geometry, "line_bytes", 0)
        try:
            line = max(1, int(raw_line))
        except (TypeError, ValueError):
            line = max_bytes
        if remaining <= line:
            return remaining
        if line > max_bytes:
            return min(line, remaining)
        full_lines = min(remaining // line, max_bytes // line)
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

        chunk_max = max(1, int(getattr(self.model, "image_read_chunk_bytes", IMAGE_CHUNK_BYTES)))
        read_exact = (
            proto.bulk_read_exact
            if chunk_max <= BULK_MAX_SIZE
            else functools.partial(proto.bulk_read_exact, max_transfer=chunk_max)
        )

        # Filled in place: growing a bytearray to a 1.7 GB frame reallocates it
        # repeatedly while the scanner keeps producing lines.
        buf = bytearray(total)
        view = memoryview(buf)
        got = 0
        timing: list[tuple[int, int, float, float]] = []
        t_start = t_prev_end = time.perf_counter()
        try:
            while got < total:
                if cancel is not None and cancel.is_set():
                    raise ScanCancelled("cancelled during bulk read")
                remaining = total - got
                want = self._chunk_bytes(geometry, remaining, chunk_max)
                p0 = time.perf_counter()
                proto.bulk_read_begin(want, index=index)
                self._bulk_stream_active = True
                try:
                    chunk = read_exact(want)
                finally:
                    self._bulk_stream_active = False
                if not chunk:
                    raise ScanError(f"Bulk stream ended after {got} of {total} bytes")
                n = min(len(chunk), remaining)
                view[got : got + n] = memoryview(chunk)[:n]
                got += n
                read_s = time.perf_counter() - p0
                if progress is not None:
                    progress(min(1.0, got / total))
                timing.append((got - n, n, read_s, p0 - t_prev_end))
                t_prev_end = time.perf_counter()
        finally:
            view.release()
            self._report_acquire_timing(geometry, timing, time.perf_counter() - t_start)

        if progress is not None:
            progress(1.0)
        return buf  # type: ignore[return-value]

    #: A chunk read faster than this rate had its data waiting in the scanner
    #: buffer, i.e. the host was behind the line clock.
    _INSTANT_READ_BPS = 20_000_000.0
    _INSTANT_READ_SLACK_S = 0.002

    @classmethod
    def _read_was_instant(cls, nbytes: int, seconds: float) -> bool:
        return seconds <= nbytes / cls._INSTANT_READ_BPS + cls._INSTANT_READ_SLACK_S

    @classmethod
    def _report_acquire_timing(
        cls,
        geometry: ScanGeometry,
        timing: list[tuple[int, int, float, float]],
        wall_s: float,
    ) -> None:
        """Log one summary line per image read; optionally dump per-chunk CSV."""
        if not timing:
            return
        nbytes = sum(t[1] for t in timing)
        instant = sum(1 for t in timing if cls._read_was_instant(t[1], t[2]))
        longest_read = max(t[2] for t in timing)
        longest_gap = max(t[3] for t in timing)
        logger.info(
            "GL128 image read: %d dpi %.1f MB in %.1f s (%.2f MB/s) chunks=%d "
            "instant_reads=%d longest_read=%.3f s longest_gap=%.3f s",
            int(getattr(geometry, "resolution", 0) or 0),
            nbytes / 1e6,
            wall_s,
            nbytes / 1e6 / wall_s if wall_s > 0 else 0.0,
            len(timing),
            instant,
            longest_read,
            longest_gap,
        )
        path = os.environ.get(ACQUIRE_TIMING_ENV)
        if not path:
            return
        try:
            out = Path(path)
            new = not out.exists()
            with out.open("a", encoding="utf-8") as fh:
                if new:
                    fh.write("dpi,offset,bytes,read_s,gap_s\n")
                dpi = int(getattr(geometry, "resolution", 0) or 0)
                for off, n, read_s, gap in timing:
                    fh.write(f"{dpi},{off},{n},{read_s:.6f},{gap:.6f}\n")
            logger.info("GL128 image read timing appended to %s", out)
        except OSError as exc:
            logger.warning("Could not write %s=%s: %s", ACQUIRE_TIMING_ENV, path, exc)

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
