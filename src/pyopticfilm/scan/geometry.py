# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan geometry helpers for OpticFilm models."""

from __future__ import annotations

from dataclasses import dataclass
from math import lcm

from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.device.protocol import FilmModel
from pyopticfilm.device.sensor_lookup import dummy_pixel_for, exposure_lperiod_for

MM_PER_INCH = 25.4


@dataclass(frozen=True)
class ScanGeometry:
    resolution: int
    pixels: int
    lines: int
    startx: int
    starty: int
    pixel_startx: int
    pixel_endx: int
    optical_pixels: int
    register_dpiset: int
    output_pixel_offset: int
    shift_r: int
    shift_g: int
    shift_b: int
    optical_line_count: int
    line_bytes: int
    stagger_y: tuple[int, ...] = ()
    num_staggered_lines: int = 0
    channels: int = 3
    depth: int = 16
    exposure_lperiod: int = 14000
    dummy_pixel: int = 20
    disable_buffer_full_move: bool = False
    #: Value programmed into ``LINCNT``. May differ from ``optical_line_count``
    #: on the GL128 image path (16-bit wire sized as ``LINCNT * width * 3``).
    register_lincnt: int = 0
    #: ``LINCNT`` register units per *output* line. GL845 writes one; the GL128
    #: image path writes four — LINCNT tracks the programmed dpi rather than
    #: 7200, and Y is sampled at twice that dpi (two buffer rows per line).
    lincnt_per_line: int = 1
    #: Native lines the ASIC covers per output line at ``asic_dpi``. Used for
    #: exposure and for the calibration LINCNT; the image path does *not* return
    #: this many rows (see :attr:`lincnt_per_line`).
    y_oversample: int = 1
    #: Normalized TA crop top edge (0..1). Used by GL128 for the second feed.
    area_y1: float = 0.0
    #: True when ``compute_geometry`` used the default full TA window.
    is_default_full_frame: bool = True
    #: Optional original normalized area; ``None`` means full frame.
    area: tuple[float, float, float, float] | None = None
    #: When > 1, ASIC acquired at a higher PPI and the host must downsample
    #: (SE: 150/300 share 600 dpi programming). Integer factors only in practice.
    host_downsample: int = 1
    #: Cap on content-aware trim of USB ENDPIXEL dummy columns after decode
    #: (image left after ``mirror_x``), from ``optical_end_inactive_native``.
    #: 0 on every GL128 model: the holder edge reads dark there too, so any
    #: cap would cut real columns. Assemble never drops more than this width.
    usb_end_drop: int = 0
    #: When > 0, assembled rows (at ``asic_dpi``) are cut to this height after
    #: line shift and unstagger. Default full frames only, from the model's
    #: ``full_frame_output_inches``.
    frame_lines: int = 0

    @property
    def max_color_shift(self) -> int:
        return max(self.shift_r, self.shift_g, self.shift_b)

    @property
    def total_bytes(self) -> int:
        return self.line_bytes * self.optical_line_count

    @property
    def lincnt_register(self) -> int:
        """ASIC ``LINCNT`` value (falls back to USB line count)."""
        return self.register_lincnt or self.optical_line_count

    @property
    def asic_dpi(self) -> int:
        """Resolution the ASIC is programmed at (>= :attr:`resolution`)."""
        return self.resolution * max(1, self.host_downsample)

    @property
    def travel_mm(self) -> float:
        """Physical Y distance the carriage covers during this pass."""
        per_line = max(1, self.lincnt_per_line)
        return self.lincnt_register * MM_PER_INCH / (per_line * self.asic_dpi)


def _stagger_for(resolution: int, model: FilmModel) -> tuple[tuple[int, ...], int]:
    stagger_fn = getattr(model, "stagger_y_for", None)
    if callable(stagger_fn):
        shifts = tuple(int(v) for v in stagger_fn(resolution))
    else:
        shifts = tuple(model.stagger_y_by_dpi.get(resolution, ()))
    if not shifts:
        return (), 0
    return shifts, max(shifts)


def _scaled_ld_shift(ld: int, asic_dpi: int, model: FilmModel) -> int:
    """Scale base-ydpi line-distance shift to ``asic_dpi``.

    Default is truncating ``//`` (SANE/Genesys). Leaves may supply
    ``scaled_ld_shift(ld, asic_dpi)`` — 8200i SE / 8300i SE round to cut
    mid-ladder residual.
    """
    scale_fn = getattr(model, "scaled_ld_shift", None)
    if callable(scale_fn):
        return int(scale_fn(ld, asic_dpi))
    return int(ld) * int(asic_dpi) // int(model.motor_base_ydpi)


def compute_geometry(
    resolution: int,
    *,
    model: FilmModel = MODEL_8200I,
    area: tuple[float, float, float, float] | None = None,
) -> ScanGeometry:
    """Compute session geometry for a color transparency scan.

    ``area`` is optional normalized (x1,y1,x2,y2) in 0..1 over the TA window.
    """
    if resolution not in model.resolutions_dpi:
        raise ValueError(f"Unsupported resolution {resolution}")

    x1, y1, x2, y2 = area if area is not None else (0.0, 0.0, 1.0, 1.0)
    x1, y1 = max(0.0, min(1.0, x1)), max(0.0, min(1.0, y1))
    x2, y2 = max(0.0, min(1.0, x2)), max(0.0, min(1.0, y2))
    if x2 <= x1 or y2 <= y1:
        raise ValueError("Invalid scan area")

    tl_x_mm = model.x_offset_ta_mm + x1 * model.x_size_ta_mm
    tl_y_mm = model.y_offset_ta_mm + y1 * model.y_size_ta_mm
    width_mm = (x2 - x1) * model.x_size_ta_mm
    height_mm = (y2 - y1) * model.y_size_ta_mm
    full_frame = area is None

    return _geometry_from_mm(
        resolution,
        model=model,
        tl_x_mm=tl_x_mm,
        tl_y_mm=tl_y_mm,
        width_mm=width_mm,
        height_mm=height_mm,
        area_y1=y1,
        is_default_full_frame=full_frame,
        area=None if full_frame else (x1, y1, x2, y2),
    )


def compute_calib_geometry(
    resolution: int,
    *,
    model: FilmModel = MODEL_8200I,
) -> ScanGeometry:
    """Geometry for dark/white shading strip (genesys ``init_regs_for_shading``).

    Y feed matches SANE ``y_offset_ta`` + ``y_offset_calib_white_ta`` (mm → motor
    steps). For OpticFilm 8200i both ``y_offset_calib_white_ta`` and
    ``y_offset_sensor_to_ta`` are 0 — white strip is at the TA home field after
    the TA feed (``tables_model.cpp``). Host stretch in
    :meth:`ImagePipeline.apply_host_calib` replaces ASIC ``send_shading_data``
    for the first genesys pass; DVDSET shading remains a follow-up if needed.
    """
    if resolution not in model.resolutions_dpi:
        raise ValueError(f"Unsupported resolution {resolution}")

    # After move-to-TA, white-calib Y offset is relative to TA sensor position.
    move_mm = model.y_offset_calib_white_ta_mm - model.y_offset_sensor_to_ta_mm
    starty = int((move_mm * model.motor_base_ydpi) / MM_PER_INCH)
    # Head is already at TA for white; dark uses same regs but motor off.
    # Absolute TA feed from home for our simplified path:
    ta_starty = int((model.y_offset_ta_mm * model.motor_base_ydpi) / MM_PER_INCH)
    starty = ta_starty + starty

    width_mm = model.x_size_calib_mm
    height_mm = model.y_size_calib_ta_mm
    # Calib uses startx=0 in SANE; optical STR still gets output_pixel_offset.
    return _geometry_from_mm(
        resolution,
        model=model,
        tl_x_mm=0.0,
        tl_y_mm=0.0,  # starty overridden below
        width_mm=width_mm,
        height_mm=height_mm,
        starty_override=starty,
        disable_buffer_full_move=True,
        is_default_full_frame=False,
    )


def _geometry_from_mm(
    resolution: int,
    *,
    model: FilmModel,
    tl_x_mm: float,
    tl_y_mm: float,
    width_mm: float,
    height_mm: float,
    starty_override: int | None = None,
    disable_buffer_full_move: bool = False,
    area_y1: float = 0.0,
    is_default_full_frame: bool = True,
    area: tuple[float, float, float, float] | None = None,
) -> ScanGeometry:
    asic_fn = getattr(model, "asic_dpi_for", None)
    asic_dpi = int(asic_fn(resolution)) if callable(asic_fn) else int(resolution)
    host_downsample = max(1, asic_dpi // int(resolution))

    lines = int((height_mm * asic_dpi) / MM_PER_INCH)
    if lines < 1:
        raise ValueError("Scan height too small")
    frame_inches = float(getattr(model, "full_frame_output_inches", 0) or 0)
    frame_lines = (
        round(frame_inches * asic_dpi)
        if is_default_full_frame and not disable_buffer_full_move and frame_inches > 0
        else 0
    )

    startx = int((tl_x_mm * asic_dpi) / MM_PER_INCH)
    starty = (
        starty_override
        if starty_override is not None
        else int((tl_y_mm * model.motor_base_ydpi) / MM_PER_INCH)
    )

    try:
        offset = model.output_pixel_offset_by_dpi[resolution]
        register_dpiset = model.register_dpiset_by_dpi[resolution]
    except KeyError as exc:
        raise ValueError(f"No sensor DPI maps for resolution {resolution}") from exc

    optical_res = model.optical_resolution
    span_align = int(getattr(model, "optical_span_alignment", 0) or 0)
    native_x = bool(getattr(model, "strpixel_native_units", False))
    if native_x:
        # Native 7200 clocks: session 03 full-width STR 242 / END 10610, and
        # the same crop keeps STR/END across 1800 vs 3600. Output-space
        # ``int(mm×dpi/25.4)`` then × factor undershoots that origin by 2.
        origin_native = offset * optical_res // asic_dpi
        end_inactive = int(getattr(model, "optical_end_inactive_native", 0) or 0)
        # Models with ENDPIXEL dummy clocks (displayed left after mirror_x)
        # shift the programmed window by that width so the remaining optical
        # span is not STR-heavy. GL128 has none: STR/END match SilverFast.
        frame_shift = 0 if disable_buffer_full_move or end_inactive <= 0 else end_inactive
        pixel_startx = (
            origin_native + frame_shift + round(tl_x_mm * optical_res / MM_PER_INCH)
        )
        optical_pixels = max(1, round(width_mm * optical_res / MM_PER_INCH))
        if span_align > 1 and optical_pixels >= span_align:
            factor = max(1, optical_res // asic_dpi)
            align = lcm(span_align, 2 * factor)
            optical_pixels = (optical_pixels // align) * align
        pixels = max(1, optical_pixels * asic_dpi // optical_res)
        optical_pixels = pixels * optical_res // asic_dpi
        pixel_endx = pixel_startx + optical_pixels
        startx = max(0, pixel_startx * asic_dpi // optical_res - offset)
        usb_end_drop = (
            0
            if disable_buffer_full_move or end_inactive <= 0
            else end_inactive * int(resolution) // optical_res
        )
    else:
        pixels = int((width_mm * asic_dpi) / MM_PER_INCH)
        # GL845: align to 16 when xres==yres and xres>1200. The GL128 SE
        # captures show unaligned widths (2478 at 1800 dpi, 4956 at 3600),
        # so models can opt out via ``pixel_alignment``.
        alignment = int(getattr(model, "pixel_alignment", 16))
        if alignment > 1 and asic_dpi > 1200:
            pixels = (pixels // alignment) * alignment
        optical_pixels = pixels * optical_res // asic_dpi
        # SE: keep native span coherent with USB line length. Session 13
        # spans are always % 4 == 0; at factor 4 (1800 dpi) that alone still
        # allows odd output widths (e.g. 2455), which shear ~1 px/row into a
        # diamond. Also require an even pixel count via lcm(span_align, 2*factor).
        if span_align > 1 and optical_pixels >= span_align:
            factor = max(1, optical_res // asic_dpi)
            align = lcm(span_align, 2 * factor)
            optical_pixels = (optical_pixels // align) * align
            pixels = max(1, optical_pixels * asic_dpi // optical_res)
            optical_pixels = pixels * optical_res // asic_dpi
        pixel_startx = (startx + offset) * optical_res // asic_dpi
        pixel_endx = pixel_startx + optical_pixels
        usb_end_drop = 0

    if pixels < 16:
        raise ValueError("Scan width too small")

    shift_r = _scaled_ld_shift(model.ld_shift_r, asic_dpi, model)
    shift_g = _scaled_ld_shift(model.ld_shift_g, asic_dpi, model)
    shift_b = _scaled_ld_shift(model.ld_shift_b, asic_dpi, model)
    max_shift = max(shift_r, shift_g, shift_b)
    stagger_y, num_staggered = _stagger_for(asic_dpi, model)
    y_oversample = (
        max(1, optical_res // asic_dpi)
        if getattr(model, "y_oversampled", False)
        else 1
    )
    # Session 13 oracle: LINCNT / dpi is constant (3.816) across the whole PPI
    # ladder and the bulk buffer holds LINCNT/2 rows, but Y is sampled at twice
    # the programmed dpi, so one output line is four LINCNT units (the ladder
    # crop is a 3:2 35 mm frame). It is not native 7200 dpi travel. Shading
    # passes keep the legacy oversample math (no capture contradicts it).
    lincnt_per_line = int(getattr(model, "image_lincnt_per_line", 0) or 0)
    if disable_buffer_full_move or lincnt_per_line <= 0:
        lincnt_per_line = y_oversample
    # GL845 scans ``max_shift`` extra lines so line-shift alignment keeps the full
    # crop height. The SE cannot: its captures size LINCNT for the crop alone and
    # session 03 already stops on the scan-window end, so alignment crops instead.
    extra_lines = num_staggered
    if bool(getattr(model, "lincnt_includes_line_shift", True)):
        extra_lines += max_shift
    register_lincnt = (lines + extra_lines) * lincnt_per_line

    channels = 3
    if disable_buffer_full_move:
        depth = int(getattr(model, "usb_calib_depth", 16))
    else:
        depth = int(getattr(model, "usb_image_depth", 16))
    if depth not in (8, 16):
        raise ValueError(f"Unsupported USB depth {depth}")

    # Session 11 oracle: image programs DEPTH8 regs (0x33=0x1F, 0xAF=0xFF) and
    # sizes the bulk as LINCNT×width×3, but the wire samples are 16-bit LE
    # chunky — so USB line count is LINCNT/2. Session 13 confirms this at every
    # PPI including 7200, where oversample is 1.
    optical_line_count = register_lincnt
    if (
        not disable_buffer_full_move
        and depth == 16
        and getattr(model, "usb_image_lincnt_half_lines", False)
    ):
        optical_line_count = register_lincnt // 2

    line_bytes = pixels * channels * (depth // 8)

    # SE shading/AFE always programs DPISET = optical_resolution/6 (1200).
    register_dpiset_out = register_dpiset
    if disable_buffer_full_move and getattr(model, "calib_uses_native_dpiset", False):
        register_dpiset_out = int(model.optical_resolution) // 6

    # Prefer SE helper when present; else SANE dpi/method lookup.
    lperiod_fn = getattr(model, "line_period_for", None)
    if callable(lperiod_fn):
        exposure_lperiod = int(lperiod_fn(resolution))
    else:
        exposure_lperiod = exposure_lperiod_for(model, resolution)

    return ScanGeometry(
        resolution=resolution,
        pixels=pixels,
        lines=lines,
        startx=startx,
        starty=starty,
        pixel_startx=pixel_startx,
        pixel_endx=pixel_endx,
        optical_pixels=optical_pixels,
        register_dpiset=register_dpiset_out,
        output_pixel_offset=offset,
        shift_r=shift_r,
        shift_g=shift_g,
        shift_b=shift_b,
        optical_line_count=optical_line_count,
        line_bytes=line_bytes,
        stagger_y=stagger_y,
        num_staggered_lines=num_staggered,
        channels=channels,
        depth=depth,
        exposure_lperiod=exposure_lperiod,
        dummy_pixel=dummy_pixel_for(model, resolution),
        disable_buffer_full_move=disable_buffer_full_move,
        register_lincnt=register_lincnt,
        lincnt_per_line=lincnt_per_line,
        y_oversample=y_oversample,
        area_y1=float(area_y1),
        is_default_full_frame=bool(is_default_full_frame),
        area=area,
        host_downsample=host_downsample,
        usb_end_drop=max(0, min(int(usb_end_drop), max(0, pixels - 16))),
        frame_lines=frame_lines,
    )
