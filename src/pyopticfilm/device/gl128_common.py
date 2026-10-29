# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared GL128 tables, ops, and sibling-diff catalog.

OpticFilm 8200i SE, 8100 V2, and 8300i SE share register maps and geometry
helpers that are capture-identical. Capture-proven *divergences* live on the
leaf model classes (``Model8200iSE``, ``Model8100V2``, ``Model8300iSE``), which
subclass :class:`Gl128Common` rather than each other.

Adding a field to :class:`Gl128Common` without listing it in
:data:`GL128_SHARED_FIELDS` (or adding a leaf-only field without listing it in
:data:`GL128_DIVERGENT_FIELDS`) fails the sibling-diff catalog test.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING, Any

from pyopticfilm.device.protocol import DEFAULT_GL845_MOTOR, MotorProfile

#: Lowest PPI that gets its own ASIC programming. Session 13: 150 and 300 share
#: the 600 dpi register set (``DPISET=100``).
MIN_ASIC_DPI = 600

MM_PER_INCH = 25.4

#: Cold-boot register blast, in ascending address order, from session
#: ``02_cold_boot_open``. The Windows driver sends these 116 registers before
#: touching anything else and performs no soft reset first.
INIT_REGS: dict[int, int] = {
    0x01: 0x22, 0x02: 0x78, 0x03: 0x20, 0x04: 0x02, 0x05: 0x48, 0x06: 0x18,
    0x07: 0x00, 0x08: 0x00, 0x09: 0x00, 0x0A: 0x40, 0x0B: 0x6C, 0x0C: 0x00,
    0x0D: 0x00, 0x11: 0x00, 0x12: 0x04, 0x13: 0x08, 0x14: 0x01, 0x15: 0x80,
    0x16: 0x27, 0x17: 0x0C, 0x18: 0x10, 0x19: 0x02, 0x1A: 0x00, 0x1B: 0x00,
    0x1C: 0x00, 0x1D: 0x00, 0x1E: 0x10, 0x1F: 0x00, 0x20: 0x0C, 0x21: 0x00,
    0x22: 0x1A, 0x23: 0x00, 0x24: 0x1A, 0x25: 0x00, 0x26: 0x00, 0x27: 0x00,
    0x2B: 0x20, 0x2C: 0x12, 0x2D: 0xC0, 0x30: 0x6F, 0x31: 0x00, 0x32: 0x22,
    0x33: 0x04, 0x34: 0x80, 0x35: 0x2F, 0x36: 0x1C, 0x37: 0xC0, 0x38: 0x44,
    0x39: 0x00, 0x3A: 0x00, 0x3B: 0xFF, 0x3C: 0xFF, 0x3D: 0x00, 0x3E: 0x00,
    0x3F: 0x01, 0x4F: 0x03, 0x52: 0x07, 0x53: 0x09, 0x54: 0x0B, 0x55: 0x01,
    0x56: 0x03, 0x57: 0x05, 0x5A: 0x12, 0x5B: 0x00, 0x5C: 0x40, 0x5E: 0x1F,
    0x5F: 0x05, 0x60: 0x00, 0x61: 0x00, 0x63: 0x20, 0x67: 0x7F, 0x68: 0x7F,
    0x69: 0x01, 0x70: 0x01, 0x71: 0x02, 0x72: 0x03, 0x73: 0x04, 0x74: 0x00,
    0x75: 0x00, 0x76: 0x00, 0x77: 0x00, 0x78: 0x00, 0x79: 0x0F, 0x7A: 0xFF,
    0x7B: 0xFF, 0x7C: 0xFF, 0x7D: 0x00, 0x7E: 0x2A, 0x7F: 0xF8, 0x80: 0x00,
    0x81: 0x22, 0x82: 0x00, 0x83: 0x01, 0x84: 0x18, 0x85: 0x00, 0x86: 0x00,
    0x87: 0x00, 0x93: 0x00, 0x94: 0x00, 0x95: 0x00, 0x9D: 0x08, 0xA0: 0x12,
    0xA4: 0x00, 0xA5: 0x20, 0xA6: 0x00, 0xA7: 0x00, 0xA8: 0x00, 0xA9: 0x00,
    0xAA: 0x00, 0xAB: 0x30, 0xB8: 0x00, 0xB9: 0x38, 0xBA: 0x00, 0xBD: 0x00,
    0xBE: 0x00, 0xBF: 0x00,
}

#: Memory layout block, byte-identical at every resolution captured, so these
#: are per-model constants rather than computed sizes (sessions 03/04/06).
MEMORY_LAYOUT_REGS: dict[int, int] = {
    0xD0: 0x0A, 0xD1: 0x0A, 0xD2: 0x0A,
    0xE0: 0x00, 0xE1: 0x68, 0xE2: 0x0B, 0xE3: 0x00, 0xE4: 0x0B, 0xE5: 0x01,
    0xE6: 0x15, 0xE7: 0x99, 0xE8: 0x15, 0xE9: 0x9A, 0xEA: 0x20, 0xEB: 0x32,
    0xEC: 0x20, 0xED: 0x33, 0xEE: 0x2A, 0xEF: 0xCB, 0xF0: 0x2A, 0xF1: 0xCC,
    0xF2: 0x35, 0xF3: 0x64, 0xF4: 0x35, 0xF5: 0x65, 0xF6: 0x3F, 0xF7: 0xFD,
    0xF8: 0x05,
}

#: Analog frontend defaults written through ``0x51``/``0x5D``/``0x5E`` during
#: boot. AD9826 layout: indices 0x02-0x04 are per-channel 6-bit PGA gains and
#: 0x05-0x07 per-channel 9-bit sign-magnitude offsets; the driver searches
#: those at calibration time, so boot zeroes them.
FRONTEND_REGS: dict[int, int] = {
    0x00: 0x00F8,
    0x01: 0x0080,
    0x02: 0x0000,
    0x03: 0x0000,
    0x04: 0x0000,
    0x05: 0x0000,
    0x06: 0x0000,
    0x07: 0x0000,
}

#: GPO block. The SE uses ``0xA2``-``0xAE``; GL845's ``0x6B``-``0x6F`` is never
#: touched. ``0xAF`` is set separately because it doubles as a depth control.
GPO_REGS: dict[int, int] = {
    0xA2: 0x00, 0xA3: 0x00, 0xA4: 0x00, 0xA6: 0x00, 0xA7: 0x00,
    0xA8: 0x00, 0xA9: 0x00, 0xAA: 0x00, 0xAC: 0x00, 0xAD: 0x01, 0xAE: 0x00,
}

#: Constant overrides the Windows driver applies on top of the boot map before
#: every acquisition, at every resolution captured. Motor, lamp, depth and
#: geometry registers are excluded here — the driver computes those per scan.
#: 8200i SE / 8100 V2 values; the 8300i SE leaf overlays its own sensor timing.
SCAN_REGS: dict[int, int] = {
    0x04: 0x42, 0x05: 0x40, 0x06: 0xF0, 0x0B: 0x4C,
    0x1C: 0x20, 0x1D: 0x80, 0x1E: 0x20,
    0x3B: 0x01,
    0x52: 0x0B, 0x53: 0x0D, 0x54: 0x0F, 0x55: 0x01, 0x56: 0x05, 0x57: 0x07,
    0x5A: 0x31, 0x5B: 0x79,
    0x70: 0x0A, 0x71: 0x0B, 0x72: 0x0C, 0x73: 0x0D,
    0x81: 0x40,
    0x8A: 0x00, 0x8B: 0x00, 0x8C: 0x00, 0x8D: 0x00, 0x8E: 0x00, 0x8F: 0x00,
    0x90: 0x00, 0x91: 0x00, 0x92: 0x00,
    0x114: 0x80, 0x115: 0x80,
}

#: ``DPISET`` (``0x2C``) is ``dpi / 6`` at 600 dpi and above; below 600 the
#: capture programs ``100`` (same as 600). Source: session ``13_ppi_ladder``.
REGISTER_DPISET: dict[int, int] = {
    150: 100,
    300: 100,
    600: 100,
    720: 120,
    900: 150,
    1200: 200,
    1440: 240,
    1800: 300,
    2400: 400,
    3600: 600,
    7200: 1200,
}

#: Native-unit optical origin is a constant 120, so the per-resolution offset is
#: ``dpi / 60`` (using the ASIC dpi, which floors at 600).
OUTPUT_PIXEL_OFFSET: dict[int, int] = {
    150: 10,
    300: 10,
    600: 10,
    720: 12,
    900: 15,
    1200: 20,
    1440: 24,
    1800: 30,
    2400: 40,
    3600: 60,
    7200: 120,
}

#: ``LPERIOD`` (``0x28``, 24-bit BE) as SilverFast programs it: a constant plus
#: the pixels read per line, /4 below 3600 ppi and /2 from 3600 ppi (the scan's
#: ASIC dpi, also for the AFE strips of that scan). This reproduces every value
#: in the SF captures exactly, on all three models: image passes and shading
#: strips at every PPI, full frames and crops (8200i SE sessions 03-14, 8100 V2
#: 03-07, 8300i SE ladder), and the AFE strips (512 px -> 10979 / 11107, 10378
#: px -> 13445 / 16040). The old per-dpi table was the SE session-13 *crop*
#: (span 10224), not a full frame; the V2 / 8300i "overrides" were full frames.
LPERIOD_BASE = 10851
LPERIOD_HIGH_FROM_DPI = 3600
#: SilverFast's full-frame image window span in native pixels (STR 242, END 10610).
FULL_FRAME_SPAN_NATIVE = 10368
_NATIVE_DPI = 7200


def lperiod_for_line(pixels: float, scan_dpi: int) -> int:
    """``LPERIOD`` for ``pixels`` read per line in a scan at ASIC ``scan_dpi``."""
    divisor = 2 if int(scan_dpi) >= LPERIOD_HIGH_FROM_DPI else 4
    return int(LPERIOD_BASE + float(pixels) / divisor)


def lperiod_for_window(asic_dpi: int, span_native: int) -> int:
    """``LPERIOD`` for an image or shading window ``span_native`` (``END - STR``) wide."""
    return lperiod_for_line(int(span_native) * int(asic_dpi) / _NATIVE_DPI, asic_dpi)


#: Full-frame ``LPERIOD`` by scan PPI (150/300 scan at ASIC 600). Crops differ:
#: use :meth:`Gl128Common.line_period_for` with the window span.
LPERIOD_BY_DPI: dict[int, int] = {
    dpi: lperiod_for_window(max(dpi, 600), FULL_FRAME_SPAN_NATIVE)
    for dpi in (150, 300, 600, 720, 900, 1200, 1440, 1800, 2400, 3600, 7200)
}


def shading_clocks_for_window(asic_dpi: int, span_native: int, *, dvdset: bool) -> tuple[int, int, int]:
    """``(0x2B, 0xA5, 0xAB)`` SilverFast writes for a shading strip ``span_native`` wide.

    ``0x2B`` counts 8 KiB of strip-line buffer (16-bit RGB, 6 bytes per pixel):
    the dark strip (DVDSET off) needs 3 lines' worth with clocks ``01/30``; the
    white strip 1 line (2 from 3600 ppi) with clocks ``02/02`` (``01/01`` from
    3600 ppi). Exact for all 66 dark and white strips in the 8200i SE sessions
    03-14 and 8100 V2 sessions 03-07, full frames and crops (spans 6552-10370),
    and for the 8300i SE dark strips; the 8300i SE white strip has its own clocks.
    """
    pixels = int(span_native) * int(asic_dpi) / _NATIVE_DPI
    line_bytes = 6 * pixels
    if not dvdset:
        return math.ceil(3 * line_bytes / 8192), 0x01, 0x30
    high = int(asic_dpi) >= 3600
    clk = 0x01 if high else 0x02
    return math.ceil((2 if high else 1) * line_bytes / 8192), clk, clk

#: ``0xA5``/``0xAB`` and ``0x2B`` — replayed verbatim from session 13.
PIXEL_CLOCK_BY_DPI: dict[int, int] = {
    150: 0x02,
    300: 0x02,
    600: 0x02,
    720: 0x02,
    900: 0x02,
    1200: 0x02,
    1440: 0x02,
    1800: 0x02,
    2400: 0x01,
    3600: 0x01,
    7200: 0x01,
}
#: Long ME image pass ``0xA5``/``0xAB`` (session 14: slower clock at 1440/1800).
PIXEL_CLOCK_LONG_BY_DPI: dict[int, int] = {
    150: 0x01,
    300: 0x01,
    600: 0x01,
    720: 0x01,
    900: 0x01,
    1200: 0x01,
    1440: 0x01,
    1800: 0x01,
    2400: 0x01,
    3600: 0x01,
    7200: 0x01,
}
DUMMY_BY_DPI: dict[int, int] = {
    150: 0x01,
    300: 0x01,
    600: 0x01,
    720: 0x01,
    900: 0x01,
    1200: 0x02,
    1440: 0x02,
    1800: 0x02,
    2400: 0x03,
    3600: 0x04,
    7200: 0x17,
}

STAGGER_BY_DPI: dict[int, tuple[int, ...]] = {
    150: (),
    300: (),
    600: (),
    720: (),
    900: (),
    1200: (),
    1440: (),
    1800: (),
    2400: (),
    3600: (),
    7200: (),
}

ALL_PPI: tuple[int, ...] = (
    7200,
    3600,
    2400,
    1800,
    1440,
    1200,
    900,
    720,
    600,
    300,
    150,
)

#: Session 13 image-pass ``LINCNT`` per SilverFast PPI (SE ladder crop).
LADDER_LINCNT_BY_DPI: dict[int, int] = {
    150: 2292,
    300: 2292,
    600: 2292,
    720: 2748,
    900: 3436,
    1200: 4580,
    1440: 5496,
    1800: 6868,
    2400: 9156,
    3600: 13732,
    7200: 27476,
}

#: ``0x1D`` low bits (LINESEL) on the dark and white shading strips, by ASIC
#: dpi; every other rung writes 0. SilverFast captures: 8200i SE
#: ``13_ppi_ladder`` / ``06_color_high_dpi``, 8100 V2 ``04_color_7200`` /
#: ``06_ppi_ladder`` / ``07_multi_exposure``. AFE strips stay at 0.
SHADING_LINESEL_BY_DPI: dict[int, int] = {3600: 1, 7200: 2}

#: Leaf-only fields that must be declared independently on every GL128 sibling.
#: Values are compared in ``tests/test_gl128_siblings.py``.
GL128_DIVERGENT_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "model",
        "usb_product_id",
        "supports_infrared",
        "scan_ready",
        "feed_to_scan_steps",
        "max_image_lincnt_by_feed2",
        "ladder_feed2_steps",
        "ladder_lincnt_by_dpi",
        "exposure_lperiod",
        "exposure_short",
        "me_fixed_long_exposure",
        "pixel_clock_by_dpi",
        "pixel_clock_long_by_dpi",
        "dummy_by_dpi",
        "sensor_custom_regs",
        "asic_clock_boot_0x8c",
        "asic_clock_scan_0x8c",
        "asic_boot_0x0b",
        "native_image_exposure_factor",
        "image_read_chunk_bytes",
        "usb_image_lincnt_half_lines",
        "image_lincnt_per_line",
        "shading_linesel_by_dpi",
    }
)

#: Fields on :class:`Gl128Common`. Must match ``dataclasses.fields(Gl128Common)``.
GL128_SHARED_FIELDS: frozenset[str] = frozenset(
    {
        "vendor",
        "asic",
        "usb_vendor_id",
        "resolutions_dpi",
        "bpp_gray",
        "bpp_color",
        "usb_image_depth",
        "usb_calib_depth",
        "usb_planar_rgb",
        "mirror_x",
        "calib_uses_native_dpiset",
        "pixel_alignment",
        "optical_span_alignment",
        "strpixel_native_units",
        "optical_end_inactive_native",
        "min_asic_dpi",
        "x_size_mm",
        "y_size_mm",
        "x_offset_ta_mm",
        "x_size_ta_mm",
        "y_offset_ta_mm",
        "y_size_ta_mm",
        "x_size_calib_mm",
        "y_size_calib_ta_mm",
        "y_offset_calib_white_ta_mm",
        "y_offset_sensor_to_ta_mm",
        "ld_shift_r",
        "ld_shift_g",
        "ld_shift_b",
        "lincnt_includes_line_shift",
        "full_frame_output_inches",
        "y_oversampled",
        "stagger_y_by_dpi",
        "register_dpiset_by_dpi",
        "lperiod_by_dpi",
        "output_pixel_offset_by_dpi",
        "register_dpihw",
        "exposure_long",
        "multi_exposure_factor",
        "me_adaptive_min_exposure",
        "me_adaptive_max_exposure",
        "me_hardware_max_exposure",
        "me_long_clamp_min",
        "me_long_clamp_max",
        "me_max_exposure_ratio",
        "me_target_dense_dn",
        "me_dense_percentile",
        "me_black_level",
        "motor_base_ydpi",
        "optical_resolution",
        "feed_steps_per_inch",
        "max_feed_mm",
        "feed_to_reference_steps",
        "feed_to_scan_top_steps",
        "feed_to_scan_bottom_steps",
        "max_feed_steps",
        "scan_window_end_steps",
        "max_travel_end_steps",
        "motor_profile",
        "init_regs",
        "frontend_regs",
        "gpo_regs",
        "memory_layout_regs",
    }
)


def dataclass_field_names(cls: type[Any]) -> frozenset[str]:
    """Public dataclass field names (no methods)."""
    return frozenset(f.name for f in fields(cls))


@dataclass(frozen=True)
class Gl128Common:
    """Capture-identical GL128 tables and helpers.

    Not a complete :class:`~pyopticfilm.device.protocol.FilmModel` — leaf
    classes add identity and the :data:`GL128_DIVERGENT_FIELDS` knobs.
    """

    vendor: str = "PLUSTEK"
    asic: str = "GL128"
    usb_vendor_id: int = 0x07B3

    resolutions_dpi: tuple[int, ...] = ALL_PPI
    bpp_gray: tuple[int, ...] = (16,)
    bpp_color: tuple[int, ...] = (16,)
    usb_image_depth: int = 16
    usb_calib_depth: int = 16
    usb_planar_rgb: bool = False
    mirror_x: bool = True
    calib_uses_native_dpiset: bool = True
    pixel_alignment: int = 1
    optical_span_alignment: int = 4
    strpixel_native_units: bool = True
    #: Native clocks trimmed from the USB ENDPIXEL side (and shifted into the
    #: window). SilverFast reads the full STR/END span and its USB lines carry
    #: no ENDPIXEL dummy on any GL128 model, so nothing is trimmed (#102).
    optical_end_inactive_native: int = 0
    min_asic_dpi: int = MIN_ASIC_DPI

    x_size_mm: float = 36.0
    y_size_mm: float = 44.0
    x_offset_ta_mm: float = 0.43
    x_size_ta_mm: float = 36.58
    y_offset_ta_mm: float = 28.5
    y_size_ta_mm: float = 25.59
    x_size_calib_mm: float = 36.58
    y_size_calib_ta_mm: float = 2.0
    y_offset_calib_white_ta_mm: float = 0.0
    y_offset_sensor_to_ta_mm: float = 0.0

    ld_shift_r: int = 0
    ld_shift_g: int = 24
    ld_shift_b: int = 48
    lincnt_includes_line_shift: bool = False
    #: Output height of the default full frame. SilverFast's full-frame
    #: ``LINCNT`` is one inch + ``ld_shift_b`` + 1 at every PPI and it returns
    #: the inch (1813 → 1800 rows at 1800 ppi, #102).
    full_frame_output_inches: float = 1.0
    y_oversampled: bool = True

    stagger_y_by_dpi: Mapping[int, tuple[int, ...]] = field(
        default_factory=lambda: dict(STAGGER_BY_DPI)
    )
    register_dpiset_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(REGISTER_DPISET)
    )
    #: Full-frame ``LPERIOD`` by PPI (same on every model); see :meth:`line_period_for`.
    lperiod_by_dpi: Mapping[int, int] = field(default_factory=lambda: dict(LPERIOD_BY_DPI))
    output_pixel_offset_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(OUTPUT_PIXEL_OFFSET)
    )

    register_dpihw: int = 1200
    exposure_long: int = 42000
    multi_exposure_factor: int = 3
    me_adaptive_min_exposure: int = 42000
    me_adaptive_max_exposure: int = 85000
    me_hardware_max_exposure: int = 85000
    #: Hard floor for adaptive ME colour-long ``REG_EXPOSURE``.
    me_long_clamp_min: int = 14_000
    #: Hard ceiling for adaptive ME colour-long ``REG_EXPOSURE``. At
    #: oversample == 1 (7200 dpi) :func:`pyopticfilm.scan.session_gl128.clamp_me_long`
    #: still caps the channel word at 64000, because the AHB exposure table is
    #: 16-bit and would wrap a value at or above 65536.
    me_long_clamp_max: int = 85_000
    me_max_exposure_ratio: float = 7.0
    me_target_dense_dn: float = 10000.0
    me_dense_percentile: float = 5.0
    me_black_level: float = 0.0
    motor_base_ydpi: int = 7200
    optical_resolution: int = 7200
    feed_steps_per_inch: int = 14400
    max_feed_mm: float = 50.0
    feed_to_reference_steps: int = 28292
    feed_to_scan_top_steps: int = 13128
    feed_to_scan_bottom_steps: int = 20232
    max_feed_steps: int = 28292
    scan_window_end_steps: int = 27636
    #: Furthest carriage position (steps from home) SilverFast drives an image
    #: pass to: 8100 V2 ``06_ppi_ladder`` full frames at 600/720 ppi end at
    #: 13128 + 14520 = 27648. Only the image-pass motor gate uses it; crop
    #: sizing keeps :attr:`scan_window_end_steps`.
    max_travel_end_steps: int = 27648
    motor_profile: MotorProfile = DEFAULT_GL845_MOTOR
    init_regs: Mapping[int, int] = field(default_factory=lambda: dict(INIT_REGS))
    frontend_regs: Mapping[int, int] = field(default_factory=lambda: dict(FRONTEND_REGS))
    gpo_regs: Mapping[int, int] = field(default_factory=lambda: dict(GPO_REGS))
    memory_layout_regs: Mapping[int, int] = field(
        default_factory=lambda: dict(MEMORY_LAYOUT_REGS)
    )

    if TYPE_CHECKING:
        name: str
        model: str
        usb_product_id: int
        supports_infrared: bool
        feed_to_scan_steps: int
        max_image_lincnt_by_feed2: Mapping[int, int]
        ladder_feed2_steps: int
        ladder_lincnt_by_dpi: Mapping[int, int]
        sensor_custom_regs: Mapping[int, int]
        usb_image_lincnt_half_lines: bool
        image_lincnt_per_line: int
        shading_linesel_by_dpi: Mapping[int, int]

    @property
    def max_area_mm(self) -> tuple[float, float]:
        return (self.x_size_ta_mm, self.y_size_ta_mm)

    def asic_dpi_for(self, resolution: int) -> int:
        """PPI used for ASIC geometry / tables (floors at :attr:`min_asic_dpi`)."""
        return max(int(resolution), int(self.min_asic_dpi))

    def oversample_for(self, resolution: int) -> int:
        """Native lines the ASIC returns per *ASIC* output line."""
        return max(1, self.optical_resolution // self.asic_dpi_for(resolution))

    def image_line_margin(self, resolution: int) -> int:
        """Lines SilverFast scans past the frame or crop height on an image pass.

        ``round(ld_shift_b × dpi / 7200) + (5 at 7200 dpi, else 1)``: the blue
        line shift plus a tail. Full frames: one inch + margin (605 at 600 dpi,
        1813 at 1800, 7253 at 7200), on all three models. The 8200i SE ladder
        crop: ``floor(h × dpi)`` + margin at every rung (h = 24.045 mm). The
        margin is cut again after line-shift alignment.
        """
        dpi = self.asic_dpi_for(resolution)
        shift = round(int(self.ld_shift_b) * dpi / int(self.motor_base_ydpi))
        return shift + (5 if self.oversample_for(resolution) == 1 else 1)

    def multi_pass_allowed_at(self, resolution: int) -> bool:
        """Whether multi-exposure and Multi-Pass may run at ``resolution``.

        Not at oversample 1 (native 7200 dpi): on the 8200i SE the ME long pass
        (64000) drained at about 1 MB/s and the scanner dropped off USB part way
        through the frame.
        """
        return self.oversample_for(resolution) != 1

    @property
    def multi_exposure_resolutions_dpi(self) -> tuple[int, ...]:
        """Resolutions where ``scan(multi_exposure=True)`` is accepted."""
        return tuple(dpi for dpi in self.resolutions_dpi if self.multi_pass_allowed_at(dpi))

    @property
    def multi_pass_resolutions_dpi(self) -> tuple[int, ...]:
        """Resolutions where ``scan(n_passes > 1)`` is accepted."""
        return tuple(dpi for dpi in self.resolutions_dpi if self.multi_pass_allowed_at(dpi))

    def line_period_for(self, resolution: int, *, span: int | None = None) -> int:
        """``LPERIOD`` (``0x28``) at ``resolution`` for a window ``span`` native pixels wide.

        ``span`` is ``END - STR`` of the image or shading window; the default is
        SilverFast's full frame. See :func:`lperiod_for_window`.
        """
        if span is None:
            span = FULL_FRAME_SPAN_NATIVE
        return lperiod_for_window(self.asic_dpi_for(resolution), int(span))

    def slope_table_fast(self) -> tuple[int, ...]:
        """Motor ramp for feed2 / scan positioning (AHB slope windows).

        Default is the 8200i SE capture table. The 8300i SE overrides with its
        CUSTOM ROM (docs §10.6).
        """
        from pyopticfilm.device.tables_8200i_se import SLOPE_TABLE_FAST

        return SLOPE_TABLE_FAST

    def slope_table_slow(self) -> tuple[int, ...]:
        """Motor ramp for the reference feed / shading acquires.

        Default is the 8200i SE capture table. The 8300i SE overrides with its
        CUSTOM ROM (docs §10.6).
        """
        from pyopticfilm.device.tables_8200i_se import SLOPE_TABLE_SLOW

        return SLOPE_TABLE_SLOW

    def shading_strip_clocks(
        self, resolution: int, *, dvdset: bool, span: int | None = None
    ) -> tuple[int, int, int]:
        """``(0x2B, 0xA5, 0xAB)`` for a shading strip ``span`` native pixels wide.

        ``span`` is the strip window's ``END - STR``; the default is SilverFast's
        full frame. See :func:`shading_clocks_for_window`.
        """
        if span is None:
            span = FULL_FRAME_SPAN_NATIVE
        return shading_clocks_for_window(self.asic_dpi_for(resolution), int(span), dvdset=dvdset)

    def shading_linesel_for(self, resolution: int) -> int:
        """``0x1D`` low bits (LINESEL) for the dark and white shading strips."""
        return int(self.shading_linesel_by_dpi.get(self.asic_dpi_for(resolution), 0))

    def shading_white_motor_period(self, resolution: int) -> int:
        """Scan-motor table period for the white shading strip.

        SilverFast uploads ``exposure × (LINESEL + 1) / 4`` before every white
        strip, not the image pass's table: 3500 / 7000 / 10500 on the 8200i SE
        and 8100 V2, 3750 / 7500 / 15000 on the 8300i SE, in every capture from
        150 to 7200 ppi.
        """
        return int(self.exposure_lperiod) * (self.shading_linesel_for(resolution) + 1) // 4

    def channel_exposure_for(self, resolution: int, *, exposure: int | None = None) -> int:
        """Scan motor table period, ``exposure // oversample`` in the captures."""
        base = int(exposure if exposure is not None else self.exposure_lperiod)
        return base // self.oversample_for(resolution)

    def scan_motor_table(self, period: int) -> tuple[int, ...]:
        """Words for AHB ``0x10000000``/``0x10004000``/``0x10008000``: flat ``period``."""
        from pyopticfilm.device.tables_8200i_se import exposure_table

        return exposure_table(period)

    def pixel_clock_for_image(self, resolution: int, *, long_exposure: bool = False) -> int:
        """``0xA5``/``0xAB`` for an image pass at ``resolution``."""
        key = self.asic_dpi_for(resolution)
        if long_exposure:
            clk_map = self.pixel_clock_long_by_dpi
        else:
            clk_map = self.pixel_clock_by_dpi
        return int(clk_map.get(key, 0x02))

    def image_exposure(self, *, long_exposure: bool = False) -> int:
        """``REG_EXPOSURE`` for short or long ME bracket."""
        return int(self.exposure_long if long_exposure else self.exposure_short)

    def feed_to_scan_steps_for_dpi(
        self, resolution: int, *, long_exposure: bool = False
    ) -> int:
        """Full-frame second-feed steps at ``resolution``.

        Default is :attr:`feed_to_scan_steps` (SE/V2). The 8300i SE overrides
        with a PPI-dependent colour-short map; ME-long uses 13128.
        """
        del resolution, long_exposure
        return int(self.feed_to_scan_steps)

    def feed_to_scan_steps_for_area(
        self,
        area: tuple[float, float, float, float] | None = None,
    ) -> int:
        """Second-feed steps for a normalized TA crop ``(x1,y1,x2,y2)``.

        Default full frame (``area is None``) uses :attr:`feed_to_scan_steps`.
        Otherwise ``y1`` is a fraction of the scan window, which runs from the
        preview top (:attr:`feed_to_scan_top_steps`) to the window end
        (:attr:`scan_window_end_steps`). Prefer
        :meth:`feed_to_scan_steps_for_dpi` for full-frame image passes that
        know the scan DPI.
        """
        if area is None:
            return int(self.feed_to_scan_steps)
        _x1, y1, _x2, _y2 = area
        y1 = max(0.0, min(1.0, float(y1)))
        top = int(self.feed_to_scan_top_steps)
        end = int(self.scan_window_end_steps)
        return round(top + y1 * (end - top))

    def max_lincnt_for_feed2(self, feed2: int) -> int | None:
        """Image ``LINCNT`` captured at this second-feed distance.

        Regression fixture only — the values come from different resolutions, so
        they are not a cap. Use :meth:`max_lincnt_for`. Returns ``None`` when no
        table entry is within 16 steps.
        """
        table = dict(self.max_image_lincnt_by_feed2)
        if not table:
            return None
        if feed2 in table:
            return int(table[feed2])
        nearest = min(table, key=lambda k: abs(int(k) - int(feed2)))
        if abs(int(nearest) - int(feed2)) > 16:
            return None
        return int(table[nearest])

    def max_travel_steps_for_feed2(self, feed2: int) -> int:
        """Steps left between ``feed2`` and the scan-window end."""
        return max(0, int(self.scan_window_end_steps) - int(feed2))

    def max_lincnt_for(self, feed2: int, resolution: int) -> int:
        """Largest image ``LINCNT`` that still stops at the scan-window end."""
        return self._lincnt_for_travel_steps(
            self.max_travel_steps_for_feed2(feed2), resolution
        )

    def max_safe_lincnt_for(self, feed2: int, resolution: int) -> int:
        """Largest image ``LINCNT`` within the travel SilverFast itself uses.

        The motor gate for image passes. Looser than :meth:`max_lincnt_for` by
        the 12 steps SilverFast's own full frames run past the scan-window end
        (1440 ppi LINCNT 5804, 7200 ppi 29012 at feed2 13128).
        """
        steps = max(0, int(self.max_travel_end_steps) - int(feed2))
        return self._lincnt_for_travel_steps(steps, resolution)

    def _lincnt_for_travel_steps(self, steps: int, resolution: int) -> int:
        asic_dpi = self.asic_dpi_for(resolution)
        raw = (
            steps * asic_dpi * int(self.image_lincnt_per_line)
        ) // int(self.feed_steps_per_inch)
        per_line = max(1, int(self.image_lincnt_per_line))
        return max(per_line, (int(raw) // per_line) * per_line)

    def travel_mm_for_lincnt(self, lincnt: int, resolution: int) -> float:
        """Physical Y travel of an image pass with ``lincnt`` at ``resolution``."""
        asic_dpi = self.asic_dpi_for(resolution)
        return (
            int(lincnt) * MM_PER_INCH / (int(self.image_lincnt_per_line) * asic_dpi)
        )

    def lincnt_for_travel_mm(self, travel_mm: float, resolution: int) -> int:
        """Image ``LINCNT`` needed to cover ``travel_mm`` at ``resolution``."""
        asic_dpi = self.asic_dpi_for(resolution)
        lines = round(float(travel_mm) * asic_dpi / MM_PER_INCH)
        return max(1, lines) * int(self.image_lincnt_per_line)

    def ladder_lincnt_for(self, resolution: int) -> int:
        """Session-13 image ``LINCNT`` for ``resolution`` (exact or nearest PPI)."""
        table = dict(self.ladder_lincnt_by_dpi)
        dpi = int(resolution)
        if dpi in table:
            return int(table[dpi])
        nearest = min(table, key=lambda k: abs(int(k) - dpi))
        return int(table[nearest])

    def boot_register_map(self) -> dict[int, int]:
        """Boot registers: the init blast plus the memory layout block."""
        regs = dict(self.init_regs)
        regs.update(self.memory_layout_regs)
        return regs


#: USB serial-number string (``iSerialNumber``) of current GL128 firmware. The
#: vendor driver loads its ``VID_07b3&PID_xxxx-20241113`` register set for this
#: string and the plain ``VID_07b3&PID_xxxx`` set for anything else; the two sets
#: differ only in ``0x36``. :data:`INIT_REGS` is the ``-20241113`` set (every
#: captured unit reports this string).
GL128_FIRMWARE_CURRENT = "20241113"
#: ``0x36`` from the plain register set.
GL128_REG36_LEGACY_FIRMWARE = 0x04


def gl128_firmware_variant(model: Any, firmware: str | None) -> Any:
    """Return ``model`` with the vendor's per-firmware ``0x36`` for this unit.

    ``firmware`` is the USB serial-number string. ``None`` (not readable) keeps
    the model unchanged; non-GL128 models are always returned unchanged.
    """
    if not isinstance(model, Gl128Common) or firmware is None:
        return model
    if firmware.strip() == GL128_FIRMWARE_CURRENT:
        return model
    regs = dict(model.init_regs)
    regs[0x36] = GL128_REG36_LEGACY_FIRMWARE
    return replace(model, init_regs=regs)
