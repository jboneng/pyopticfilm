# SPDX-License-Identifier: GPL-3.0-or-later
"""OpticFilm 135i model definition (GL128).

The 135i (``07b3:1436``, ``bcdDevice=0x0702``) uses the GL128 ASIC, like the
8200i SE, 8100 V2 and 8300i SE: the same register map, status at ``0x101``,
frontend through ``0x51``/``0x5D``/``0x5E`` and AHB windows. It subclasses
:class:`Gl128Common`, **not** any leaf, so sibling capture constants cannot
leak through inheritance.

It is a motorised film loader rather than a manual feeder, and it scans RGB
and infrared in one pass. Evidence is the VueScan usbmon capture set of a 135i
and the Plustek model INI for this scanner (:mod:`pyopticfilm.device.tables_135i`,
``docs/gl128-model-comparison.md`` §11). Summary:

**Boot**
    118-register blast (29 values differ from the SE, plus ``0x114``/``0x115``)
    with no ``0x000FFF00``/``0x000FFF01`` blobs, boot clock ``0x8C = 0x0D``,
    the ``0x0B`` DRAM strobe (``0x64`` / ``0x6C``), five GPIO requests and a
    lamp-off pair, then the frontend defaults. No memory layout, GPO block or
    motor tables at boot. One register set, so no per-firmware ``0x36``.
    *Capture evidence*: ``vuescan open``.

**Timing sets**
    The Y resolution selects an INI subkey (smallest of 300/600/1200/3600/7200
    at or above it). 7200 uses ``Speed1`` (``SENSORSPEED = 1``, DPISET = X/6);
    the rest use ``Speed2`` (``SENSORSPEED = 2``, DPISET = X/3), so the
    hardware X resolutions are 7200 and 3600 / D. 1440 and 2400 dpi have no
    native rung and are not listed. *Capture evidence*: DPISET 200 / 400 /
    1200 at 600 / 1200 / 7200 dpi.

**Window and line period**
    STRPIXEL is 35 at every resolution, ENDPIXEL = STR + full width (10368 at
    7200, 5184 sensor units under ``Speed2``). ``LPERIOD`` follows the family
    window rule with base **10807** instead of 10851, because the second
    (IR) shading table takes SDRAM line buffer. *Capture evidence*: 11023 /
    11239 / 15991 at 600 / 1200 / 7200 dpi.

**Exposure and motor**
    ``REG_EXPOSURE`` 14000 (INI ``7d``) under ``Speed2`` and 28000 at 7200 dpi
    (multiplier 2); the scan motor period is
    ``exposure * (0x1D + 1) * Ydpi / 3600``. Motor tables come from the INI
    ``Motor\\Curve = 30,2500,180``. A move of ``n`` steps (1/7200 inch) uses
    255 ramp steps at each end: ``FEEDL = n - 510``.

**Infrared (toggle mode)**
    Each scan line is an IR line followed by a visible line of the same size
    (``0x0C``, ``0x19`` bit 3, IR LED on ``0x32`` bit 5), so ``LINCNT`` counts
    two sensor lines per output line. Shading has an IR bank
    (``0x10014000``) and a visible bank (``0x10034000``).

**Loader**
    Holder in the slot: ``HOMESNR`` with ``0x32`` bit 1 set. ``0x32`` bit 1
    selects what ``HOMESNR`` reads (clear: loaded position, set: holder in
    the slot). Load and home = 1 inch forward, then a reverse seek of up to
    10 inches to the loader sensor; eject = 3600 steps on interrupt byte
    ``0x48``. Frames sit 10752 steps apart from
    6438; the panorama (whole strip) starts at 6414 and is 66600 steps tall. The flatbed carriage fields inherited from :class:`Gl128Common`
    (reference feed, TA window) do not apply to this model.

**Session**
    :class:`~pyopticfilm.asic.gl128_loader.Gl128Loader` and
    :class:`~pyopticfilm.scan.session_gl128_loader.Gl128LoaderScanSession`
    drive the loader and toggle mode (``film_loader``); the flatbed GL128 path
    is not used for this model.

**scan_ready**
    ``False``. Nothing here has run on 135i hardware yet.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from pyopticfilm.device.gl128_common import BootStep, Gl128Common
from pyopticfilm.device.tables_135i import (
    BOOT_VENDOR_STEPS_135I,
    COLOUR_MASKED,
    COLOUR_REGS,
    EXPOSURE_MULTIPLIER,
    FIRST_FRAME_FEED_STEPS,
    FRAME_PITCH_STEPS,
    FULL_WIDTH_7200,
    INIT_REGS_135I,
    LOADER_TRAVEL_STEPS,
    LPERIOD_BASE,
    MEMORY_LAYOUT_REGS_135I,
    PANORAMA_FEED_STEPS,
    PANORAMA_HEIGHT_STEPS,
    REG_3B_BY_SUBKEY,
    SENSOR_SPEED,
    SLOPE_TABLE_FEED,
    SLOPE_TABLE_LOADER,
    SPEED1_MASKED,
    SPEED1_REGS,
    SPEED2_MASKED,
    SPEED2_REGS,
    TOGGLE_MASKED,
    TOGGLE_REGS,
    WINDOW_ORIGIN,
    dpi_subkey,
    exposure_registers,
    image_line_margin,
    scan_motor_period,
    scan_motor_table,
    timing_set_for,
)

MM_PER_INCH = 25.4

#: Resolutions with a native hardware X rung (7200, or 3600 / D under Speed2).
#: 150 and 300 scan at ASIC 600 like the other GL128 models.
RESOLUTIONS_135I: tuple[int, ...] = (7200, 3600, 1800, 1200, 900, 720, 600, 300, 150)

# DPISET (0x2C) by PPI: X/6 under Speed1 (7200), X/3 under Speed2. 150/300 use
# the 600 dpi programming (min_asic_dpi).
_REGISTER_DPISET_135I: dict[int, int] = {
    150: 200,  # filled (asic 600)
    300: 200,  # filled (asic 600)
    600: 200,  # measured (panorama preview)
    720: 240,  # filled (Speed2 rule)
    900: 300,  # filled (Speed2 rule)
    1200: 400,  # measured (35 mm preview)
    1800: 600,  # filled (Speed2 rule)
    3600: 1200,  # filled (Speed2 rule)
    7200: 1200,  # measured (frame scan, Speed1)
}
assert set(_REGISTER_DPISET_135I) == set(RESOLUTIONS_135I)


def _lperiod_135i(asic_dpi: int, pixels: int | None = None) -> int:
    """``LPERIOD``: 10807 + pixels / 4, / 2 from 3600 dpi (the family window rule).

    ``pixels`` defaults to the full width at ``asic_dpi``.
    """
    if pixels is None:
        pixels = FULL_WIDTH_7200 * int(asic_dpi) // 7200
    return int(LPERIOD_BASE + int(pixels) / (2 if int(asic_dpi) >= 3600 else 4))


# Full-width LPERIOD: measured 11023 (600), 11239 (1200), 15991 (7200).
_LPERIOD_BY_DPI_135I: dict[int, int] = {
    dpi: _lperiod_135i(max(dpi, 600)) for dpi in RESOLUTIONS_135I
}

# Image-pass dummy 0x2B by asic dpi.
_DUMMY_BY_DPI_135I: dict[int, int] = {
    150: 0x0A,  # filled (asic 600)
    300: 0x0A,  # filled (asic 600)
    600: 0x0A,  # measured
    720: 0x0A,  # filled (no capture; nearest measured)
    900: 0x0D,  # filled (no capture; nearest measured)
    1200: 0x0D,  # measured
    1800: 0x0D,  # filled (no capture; nearest measured)
    3600: 0x17,  # filled (no capture; nearest measured)
    7200: 0x17,  # measured
}

# Image-pass 0xA5/0xAB by asic dpi.
_PIXEL_CLOCK_BY_DPI_135I: dict[int, int] = {
    150: 0x23,  # filled (asic 600)
    300: 0x23,  # filled (asic 600)
    600: 0x23,  # measured
    720: 0x23,  # filled (no capture; nearest measured)
    900: 0x0B,  # filled (no capture; nearest measured)
    1200: 0x0B,  # measured
    1800: 0x0B,  # filled (no capture; nearest measured)
    3600: 0x01,  # filled (no capture; nearest measured)
    7200: 0x01,  # measured
}

# Frame image LINCNT: two sensor lines per output line plus the line margin.
# Measured at 1200 (35 mm preview frame 1) and 7200 (frame scan); the rest are
# scaled from the 1200 dpi frame (1991 line pairs) and rounded to whole pairs.
_LADDER_LINCNT_BY_DPI_135I: dict[int, int] = {
    150: 1992,  # filled (asic 600)
    300: 1992,  # filled (asic 600)
    600: 1992,  # filled (scaled from 1200)
    720: 2390,  # filled (scaled from 1200)
    900: 2986,  # filled (scaled from 1200)
    1200: 3982,  # measured
    1800: 5972,  # filled (scaled from 1200)
    3600: 11946,  # filled (scaled from 1200)
    7200: 20340,  # measured
}
assert set(_LADDER_LINCNT_BY_DPI_135I) == set(RESOLUTIONS_135I)

# Shading strips (dummy, clk_a, clk_b) by asic dpi: dark (DVDSET off) and white.
_SHADING_DARK_135I: dict[int, tuple[int, int, int]] = {
    600: (0x06, 0x01, 0x30),  # measured (panorama preview)
    1200: (0x02, 0x01, 0x30),  # measured (35 mm preview)
}
_SHADING_WHITE_135I: dict[int, tuple[int, int, int]] = {
    600: (0x07, 0x0B, 0x0B),  # measured (panorama preview)
    1200: (0x0D, 0x0B, 0x0B),  # measured (35 mm preview)
}

# Per-scan constants outside the timing sets, from every measured image pass:
# 0x04/0x05 scan selects, 0x0B scan clock, 0x1C/0x1D, window origin 0x80-0x81.
_SCAN_BASE_135I: dict[int, int] = {
    0x04: 0x42, 0x05: 0x40, 0x0B: 0x8C, 0x1C: 0x00, 0x1D: 0x00,
    0x80: (WINDOW_ORIGIN >> 8) & 0xFF, 0x81: WINDOW_ORIGIN & 0xFF,
    0x114: 0x80, 0x115: 0x80,
}


def _scan_regs(y_dpi: int, *, infrared: bool) -> dict[int, int]:
    """Full-value scan overlay: base, timing set, mode and the subkey ``0x3B``/``0x3C``."""
    speed = SPEED1_REGS if timing_set_for(y_dpi) == "Speed1" else SPEED2_REGS
    regs = dict(_SCAN_BASE_135I)
    regs.update(TOGGLE_REGS if infrared else COLOUR_REGS)
    regs.update(speed)
    reg_3b, reg_3c = REG_3B_BY_SUBKEY[dpi_subkey(y_dpi)]
    regs[0x3B] = reg_3b
    regs[0x3C] = reg_3c
    return regs


def _scan_masked(y_dpi: int, *, infrared: bool) -> dict[int, tuple[int, int]]:
    """Masked GPIO / mode bits ``address -> (mask, value)`` for a scan."""
    speed = SPEED1_MASKED if timing_set_for(y_dpi) == "Speed1" else SPEED2_MASKED
    out: dict[int, tuple[int, int]] = {}
    for table in (TOGGLE_MASKED if infrared else COLOUR_MASKED, speed):
        for addr, (mask, value) in table.items():
            old_mask, old_value = out.get(addr, (0, 0))
            out[addr] = (old_mask | mask, (old_value & ~mask) | (value & mask))
    return out


@dataclass(frozen=True)
class Model135i(Gl128Common):
    """OpticFilm 135i — GL128 film loader with RGB+IR toggle scanning; not scan_ready."""

    #: Motorised film loader with toggle-mode RGB+IR: selects
    #: :class:`~pyopticfilm.asic.gl128_loader.Gl128Loader`, the loader session
    #: and the frame-slot geometry.
    film_loader: ClassVar[bool] = True

    name: str = "plustek-opticfilm-135i"
    model: str = "OpticFilm 135i"
    usb_product_id: int = 0x1436
    supports_infrared: bool = True
    scan_ready: bool = False

    #: Positioning feed to the first 35 mm frame from the loader home
    #: (1200 dpi preview, frame 1).
    feed_to_scan_steps: int = FIRST_FRAME_FEED_STEPS
    #: Image ``LINCNT`` at each captured positioning feed: 1200 dpi preview
    #: frame 1, 600 dpi panorama strip, 7200 dpi frame scan.
    max_image_lincnt_by_feed2: Mapping[int, int] = field(
        default_factory=lambda: {6438: 3982, 6414: 11218, 17631: 20340}
    )
    #: No PPI ladder capture: the first-frame feed.
    ladder_feed2_steps: int = FIRST_FRAME_FEED_STEPS
    ladder_lincnt_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(_LADDER_LINCNT_BY_DPI_135I)
    )

    #: INI timing sets ``7d`` = 14000; every measured pass.
    exposure_lperiod: int = 14000
    exposure_short: int = 14000
    #: Filled: no multi-exposure capture. 3x the short, as on the 8200i SE.
    me_fixed_long_exposure: int = 42000
    pixel_clock_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(_PIXEL_CLOCK_BY_DPI_135I)
    )
    #: Filled: no long-exposure capture; the short-pass clocks.
    pixel_clock_long_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(_PIXEL_CLOCK_BY_DPI_135I)
    )
    dummy_by_dpi: Mapping[int, int] = field(default_factory=lambda: dict(_DUMMY_BY_DPI_135I))
    #: Toggle-mode overlay at the ``Speed2`` subkeys (300-3600). Use
    #: :meth:`scan_regs_for` for a given resolution; GPIO and mode bits that
    #: are written masked are in :meth:`scan_masked_regs_for`.
    sensor_custom_regs: Mapping[int, int] = field(
        default_factory=lambda: _scan_regs(1200, infrared=True)
    )
    #: ``vuescan open``: ``0x8C[0x10]`` = ``0x8C[0x13]`` = ``0x0D`` at boot
    #: (48 MHz); every scan writes ``0x0D`` then ``0x0E`` (60 MHz).
    asic_clock_boot_0x8c: int = 0x0D
    asic_clock_scan_0x8c: int = 0x0E
    #: ``vuescan open``: ``0x0B = 0x64``, ``0x13 = 0x0F``, ``0x0B = 0x6C``.
    asic_boot_0x0b: int = 0x6C
    #: The 7200 dpi scan motor period already comes from
    #: :meth:`channel_exposure_for`; nothing extra on top.
    native_image_exposure_factor: int = 1
    #: Filled: VueScan reads 497664 / 518400 bytes per announce; kept at the
    #: family's 0xF000 until hardware sign-off.
    image_read_chunk_bytes: int = 0xF000
    #: Bulk bytes = ``LINCNT`` x one 3-channel sensor line; toggle mode sends
    #: a visible and an IR line per output line.
    usb_image_lincnt_half_lines: bool = False
    image_lincnt_per_line: int = 2
    #: ``0x1D`` is 0 on every measured shading strip.
    shading_linesel_by_dpi: Mapping[int, int] = field(default_factory=dict)
    resolutions_dpi: tuple[int, ...] = RESOLUTIONS_135I
    register_dpiset_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(_REGISTER_DPISET_135I)
    )
    lperiod_by_dpi: Mapping[int, int] = field(default_factory=lambda: dict(_LPERIOD_BY_DPI_135I))
    #: ``MotorDPI = 3600`` with step shift 1: 7200 steps per inch (frame pitch
    #: 10752 steps = 37.9 mm).
    feed_steps_per_inch: int = 7200
    init_regs: Mapping[int, int] = field(default_factory=lambda: dict(INIT_REGS_135I))
    memory_layout_regs: Mapping[int, int] = field(
        default_factory=lambda: dict(MEMORY_LAYOUT_REGS_135I)
    )

    # --- boot ------------------------------------------------------------

    def boot_blobs(self) -> tuple[tuple[int, bytes], ...]:
        """None: ``vuescan open`` starts with the register blast."""
        return ()

    def boot_writes_layout_and_gpo(self) -> bool:
        """No: the layout is written before each acquire, the GPO block never."""
        return False

    def boot_vendor_steps(self) -> tuple[BootStep, ...]:
        """GPIO requests and lamp off (:data:`BOOT_VENDOR_STEPS_135I`)."""
        return BOOT_VENDOR_STEPS_135I

    def boot_uploads_tables(self) -> bool:
        """No: the first motor tables go up with the first loader feed."""
        return False

    def firmware_register_overrides(self, firmware: str | None) -> dict[int, int]:
        """None: the model INI has one ``VID_07b3&PID_1436`` register set."""
        del firmware
        return {}

    # --- timing ----------------------------------------------------------

    def sensor_speed_for(self, resolution: int) -> int:
        """``SENSORSPEED`` of the timing set the Y resolution selects."""
        return SENSOR_SPEED[timing_set_for(self.asic_dpi_for(resolution))]

    def scan_regs_for(self, resolution: int, *, infrared: bool = True) -> dict[int, int]:
        """Full-value scan overlay at ``resolution`` (toggle RGB+IR or plain colour)."""
        return _scan_regs(self.asic_dpi_for(resolution), infrared=infrared)

    def scan_masked_regs_for(
        self, resolution: int, *, infrared: bool = True
    ) -> dict[int, tuple[int, int]]:
        """Masked bits ``address -> (mask, value)`` written by read-modify-write."""
        return _scan_masked(self.asic_dpi_for(resolution), infrared=infrared)

    def full_width_units(self, resolution: int) -> int:
        """Full sensor width in the timing set's units (10368 at 7200, 5184 under ``Speed2``)."""
        return FULL_WIDTH_7200 // self.sensor_speed_for(resolution)

    def sensor_units_per_pixel(self, resolution: int) -> int:
        """Sensor units per ASIC pixel: ``(7200 / SENSORSPEED) / asic_dpi`` (3 at 1200 dpi)."""
        hw = 7200 // self.sensor_speed_for(resolution)
        return max(1, hw // self.asic_dpi_for(resolution))

    def line_period_for(self, resolution: int, *, span: int | None = None) -> int:
        """``LPERIOD`` with base 10807 (see :data:`~pyopticfilm.device.tables_135i.LPERIOD_BASE`).

        ``span`` is ``END - STR`` in the timing set's sensor units, as
        programmed; the default is the full width.
        """
        if span is None:
            span = self.full_width_units(resolution)
        pixels = int(span) // self.sensor_units_per_pixel(resolution)
        return _lperiod_135i(self.asic_dpi_for(resolution), pixels)

    def image_window(self, resolution: int) -> tuple[int, int]:
        """Full-width ``(STRPIXEL, ENDPIXEL)`` in the timing set's sensor units."""
        return WINDOW_ORIGIN, WINDOW_ORIGIN + self.full_width_units(resolution)

    def image_line_margin(self, resolution: int) -> int:
        """Sensor lines past the requested height (:func:`~pyopticfilm.device.tables_135i.image_line_margin`)."""
        asic = self.asic_dpi_for(resolution)
        return image_line_margin(asic, asic)

    def exposure_multiplier_for(self, resolution: int) -> int:
        """Exposure multiplier of the timing set (:data:`~pyopticfilm.device.tables_135i.EXPOSURE_MULTIPLIER`)."""
        return EXPOSURE_MULTIPLIER[timing_set_for(self.asic_dpi_for(resolution))]

    def image_exposure_for(self, resolution: int) -> int:
        """``REG_EXPOSURE`` of an image pass: 14000, or 28000 at 7200 dpi."""
        return int(self.exposure_lperiod) * max(1, self.exposure_multiplier_for(resolution))

    def exposure_regs_for(self, resolution: int, *, exposure: int | None = None) -> dict[int, int]:
        """``0x7D``-``0x7F`` and ``0x8A``-``0x92`` for an image pass at ``resolution``.

        The multiplier for ``0x90`` is the timing set's (measured: 0 at 14000,
        2 at 28000). A longer ``exposure`` (multi-exposure long pass, manual
        override) takes ``n = round(exposure / 14000)`` by the same rule
        (filled: no capture above 28000).
        """
        value = self.image_exposure_for(resolution) if exposure is None else int(exposure)
        n = self.exposure_multiplier_for(resolution)
        base = int(self.exposure_lperiod)
        if value > max(base, base * n):
            n = max(n, round(value / base))
        return exposure_registers(value, n)

    def me_long_exposure_cap(self, resolution: int) -> int:
        """Largest long exposure whose scan motor word fits under 64000.

        The word is ``exposure * Ydpi / 3600`` on this model (64000 at 3600
        dpi, 192000 at 1200), not ``exposure / oversample`` as on the
        flatbed GL128 models.
        """
        return 64000 * 3600 // self.asic_dpi_for(resolution)

    def channel_exposure_for(self, resolution: int, *, exposure: int | None = None) -> int:
        """Scan motor period ``exposure * Ydpi / 3600`` (``0x1D = 0``)."""
        base = int(exposure if exposure is not None else self.image_exposure_for(resolution))
        return scan_motor_period(base, self.asic_dpi_for(resolution))

    def scan_motor_table(self, period: int) -> tuple[int, ...]:
        """INI-curve acceleration ramp clamped at ``period``."""
        return scan_motor_table(period)

    def slope_table_fast(self) -> tuple[int, ...]:
        """Frame-positioning and eject ramp (head 64817)."""
        return SLOPE_TABLE_FEED

    def slope_table_slow(self) -> tuple[int, ...]:
        """Load / reverse-seek ramp (head 34482)."""
        return SLOPE_TABLE_LOADER

    def shading_strip_clocks(
        self, resolution: int, *, dvdset: bool, span: int | None = None
    ) -> tuple[int, int, int]:
        """``(dummy, clk_a, clk_b)`` for a shading strip; nearest measured asic dpi."""
        del span
        table = _SHADING_WHITE_135I if dvdset else _SHADING_DARK_135I
        key = self.asic_dpi_for(resolution)
        if key not in table:
            key = min(table, key=lambda k: abs(int(k) - int(key)))
        return table[key]

    # --- loader ----------------------------------------------------------

    def frame_feed_steps(self, frame: int) -> int:
        """Positioning feed from the loader home to 35 mm frame ``frame`` (0-based)."""
        return FIRST_FRAME_FEED_STEPS + int(frame) * FRAME_PITCH_STEPS

    def frame_slot_steps(self, *, strip: bool = False) -> int:
        """Height of a slot in feed steps: one frame pitch, or the panorama strip."""
        return PANORAMA_HEIGHT_STEPS if strip else FRAME_PITCH_STEPS

    def frame_slot_mm(self, *, strip: bool = False) -> tuple[float, float]:
        """``(width, height)`` of a slot: the full sensor width by one frame pitch.

        ``strip=True`` is the panorama slot (the whole strip, 66600 steps).
        A scan ``area`` is normalised over the slot; the default covers all of it.
        """
        return (
            FULL_WIDTH_7200 * MM_PER_INCH / 7200,
            self.frame_slot_steps(strip=strip) * MM_PER_INCH / self.feed_steps_per_inch,
        )

    def strip_feed_steps(self) -> int:
        """Positioning feed to the top of the panorama slot (6414)."""
        return PANORAMA_FEED_STEPS

    def loader_travel_steps(self) -> int:
        """Loader travel from home (:data:`~pyopticfilm.device.tables_135i.LOADER_TRAVEL_STEPS`)."""
        return LOADER_TRAVEL_STEPS


MODEL_135I = Model135i()
