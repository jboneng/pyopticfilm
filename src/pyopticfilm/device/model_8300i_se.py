# SPDX-License-Identifier: GPL-3.0-or-later
"""OpticFilm 8300i SE model definition (GL128).

The 8300i SE (``07b3:181f``, ``bcdDevice=0x0702``) uses the GL128 ASIC and is
a sibling of the 8200i SE and 8100 V2.  It subclasses :class:`Gl128Common`,
**not** either leaf, so sibling capture constants cannot leak through
inheritance.

All divergent knobs below come from SilverFast 9 USBPcap full-frame captures
(``docs/gl128-model-comparison.md`` §10).  Summary:

**Geometry (V2-like)**
    Full-frame STR/END = 242/10610, all columns kept.  Settled colour-short feed2 is
    PPI-dependent (13040…13128); :meth:`feed_to_scan_steps_for_dpi` programs
    that map (ME-long → 13128).  ``feed_to_scan_steps`` stays 13128 as a
    dpi-less fallback.  Image ``LINCNT`` is SilverFast's: one unit and one
    bulk row per output line, a quarter of the SE/V2 value for the same
    travel, because :meth:`Model8300iSE.scan_motor_table` runs the motor at
    twice the SE/V2 speed.

**Timing**
    ``LPERIOD`` follows the window rule shared by every GL128 model and
    matches the 8300i ladder at every measured PPI.  Exposure is **15000**
    (SE/V2 use 14000).  Image dummy and pixel
    clock maps are 8300i-specific.

**Shading**
    The dark strip follows the window rule shared with the SE/V2. White-strip
    clocks are 8300i-specific (``0xA5``/``0xAB`` ``03``/``02``); white @7200
    dummy is ``0x26`` (SE/V2 ``0x10``).  See :meth:`shading_strip_clocks`.

**Sensor timing**
    Per-scan overlay ``0x06``/``0x0B``/``0x52``–``0x57``/``0x5A``/``0x5B``/
    ``0x70``–``0x73`` differs from SE/V2 ``SCAN_REGS`` (docs §10.7).

**Slope ROM**
    CUSTOM AHB tables (heads ``0x32BB`` / ``0x846A``), not SE
    ``0x1FB4`` / ``0x16DE``.  See :mod:`pyopticfilm.device.tables_8300i_se`.

**IR / ME**
    Infrared is present (``0x37`` → ``0xB4`` on most IR passes; ``0xF4``
    seen at 150/2400).  ME long exposure 60000 observed at 1200 and 3600
    (``me_fixed_long_exposure``).

**scan_ready**
    ``True`` after hardware sign-off (see ``docs/scanner-validation.md``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pyopticfilm.device.gl128_common import (
    ALL_PPI,
    INIT_REGS,
    LADDER_LINCNT_BY_DPI,
    LPERIOD_BY_DPI,
    MEMORY_LAYOUT_REGS,
    REGISTER_DPISET,
    SCAN_REGS,
    Gl128Common,
)
from pyopticfilm.device.tables_8300i_se import (
    SLOPE_TABLE_FAST,
    SLOPE_TABLE_SLOW,
    scan_motor_table,
)

# Per-scan sensor/AFE timing overlay. SilverFast writes these before every AFE,
# shading and image acquire at every measured PPI, IR included (docs §10.7).
# Under the SE/V2 values (``SCAN_REGS``) the colour white strip reads ~2x hotter
# and rails on some units. ``0x36`` is a boot register chosen per firmware
# (:func:`~pyopticfilm.device.gl128_common.gl128_firmware_variant`).
_SCAN_REGS_8300I: dict[int, int] = {
    **SCAN_REGS,
    0x06: 0x10,
    0x0B: 0x6C,
    0x52: 0x08, 0x53: 0x0A, 0x54: 0x00, 0x55: 0x02, 0x56: 0x04, 0x57: 0x06,
    0x5A: 0x22, 0x5B: 0x4A,
    0x70: 0x06, 0x71: 0x07, 0x72: 0x07, 0x73: 0x08,
}

# Measured full-frame PPI: 150, 300, 600, 900, 1200, 1800, 2400, 3600, 7200.
# Still filled (no capture): 720, 1440 — nearest-band fills are known-unreliable
# for clocks/feed2 at mid-ladder (see 900/1800 corrections). Session lookups
# use asic_dpi_for (floors at 600), so optical 150/300 share the 600 key.

# Host column Y unstagger (apply_y_stagger) for PPIs with native period-2 sawtooth
# in Scan Lab TIFFs. Pre-mirror pattern (1, 0); mirror_x + odd/even width flips
# post-mirror polarity so 1440 (odd W) and 2400 (even W) both match offline trials.
# ASIC STAGGER bit stays clear (captures / Scan Lab runs). 1800 left empty (clean).
_STAGGER_BY_DPI_8300I: dict[int, tuple[int, ...]] = {
    150: (),
    300: (),
    600: (),
    720: (),
    900: (),
    1200: (),
    1440: (1, 0),
    1800: (),
    2400: (1, 0),
    3600: (),
    7200: (),
}

# SilverFast full-frame LINCNT; the same travel as the 8100 V2 ladder (V2 / 4).
_LADDER_LINCNT_BY_DPI_8300I: dict[int, int] = {
    150: 605,  # measured (asic→600)
    300: 605,  # measured (asic→600)
    600: 605,  # measured
    720: 726,  # filled (V2 formula / 4; no capture)
    900: 907,  # measured
    1200: 1209,  # measured
    1440: 1451,  # filled (V2 formula / 4; no capture)
    1800: 1813,  # measured
    2400: 2417,  # measured
    3600: 3625,  # measured
    7200: 7253,  # measured
}
assert set(_LADDER_LINCNT_BY_DPI_8300I) == set(LADDER_LINCNT_BY_DPI)

# Image-pass dummy 0x2B (keyed by asic dpi in session).
_DUMMY_BY_DPI_8300I: dict[int, int] = {
    150: 0x06,  # measured band (asic→600)
    300: 0x06,  # measured band (asic→600)
    600: 0x06,  # measured
    720: 0x06,  # filled (no capture; nearest-band unreliable)
    900: 0x06,  # measured
    1200: 0x07,  # measured
    1440: 0x07,  # filled (no capture; nearest-band unreliable)
    1800: 0x08,  # measured (not 1200's 0x07)
    2400: 0x0B,  # measured
    3600: 0x10,  # measured
    7200: 0x1F,  # measured
}

# Image-pass 0xA5/0xAB (keyed by asic dpi).
_PIXEL_CLOCK_BY_DPI_8300I: dict[int, int] = {
    150: 0x59,  # measured band (asic→600)
    300: 0x59,  # measured band (asic→600)
    600: 0x59,  # measured
    720: 0x59,  # filled (no capture; nearest-band unreliable)
    900: 0x22,  # measured (not 300-band 0x59)
    1200: 0x12,  # measured
    1440: 0x12,  # filled (no capture; nearest-band unreliable)
    1800: 0x08,  # measured (not 1200's 0x12)
    2400: 0x05,  # measured
    3600: 0x03,  # measured
    7200: 0x02,  # measured
}

# ME long image pass: 0x02 observed at 1200 and 3600; other rungs filled.
_PIXEL_CLOCK_LONG_BY_DPI_8300I: dict[int, int] = {
    150: 0x02,
    300: 0x02,
    600: 0x02,
    720: 0x02,
    900: 0x02,
    1200: 0x02,  # measured
    1440: 0x02,
    1800: 0x02,
    2400: 0x02,
    3600: 0x02,  # measured
    7200: 0x02,
}

# Colour-short settled feed2 by asic dpi (docs §10.2). ME-long → 13128.
# Offsets from 13128: 150/300/600:88; 900:32; 1200:16; 1800:6; 2400:4; 3600:2; 7200:0.
_FEED2_BY_ASIC_DPI_8300I: dict[int, int] = {
    600: 13040,  # measured at 150/300/600
    900: 13096,  # measured
    1200: 13112,  # measured
    1800: 13122,  # measured
    2400: 13124,  # measured
    3600: 13126,  # measured
    7200: 13128,  # measured
}

# Shading-strip 0x1D low bits (LINESEL) keyed by asic_dpi; 600-1800 measured 0,
# 720/1440 not captured. One step above the SE/V2 from 2400 ppi.
_SHADING_LINESEL_BY_ASIC_DPI_8300I: dict[int, int] = {
    2400: 1,  # measured
    3600: 1,  # measured
    7200: 3,  # measured
}

# White shading strip (dummy, clk_a, clk_b) keyed by asic_dpi. The dark strip
# follows the shared GL128 rule, which reproduces every measured 8300i dark strip.
_SHADING_WHITE_BY_ASIC_DPI: dict[int, tuple[int, int, int]] = {
    600: (0x03, 0x03, 0x03),  # measured
    900: (0x04, 0x03, 0x03),  # measured
    1200: (0x06, 0x03, 0x03),  # measured
    1800: (0x08, 0x03, 0x03),  # measured
    2400: (0x0B, 0x02, 0x02),  # measured
    3600: (0x10, 0x02, 0x02),  # measured
    7200: (0x26, 0x02, 0x02),  # measured
}


@dataclass(frozen=True)
class Model8300iSE(Gl128Common):
    """OpticFilm 8300i SE — GL128 sibling with IR; hardware-tested."""

    name: str = "plustek-opticfilm-8300i-se"
    model: str = "OpticFilm 8300i SE"
    usb_product_id: int = 0x181F
    supports_infrared: bool = True
    scan_ready: bool = True

    # Dpi-less fallback (7200 / ME-long). Prefer feed_to_scan_steps_for_dpi.
    feed_to_scan_steps: int = 13128


    max_image_lincnt_by_feed2: Mapping[int, int] = field(
        default_factory=lambda: {13128: 7253}
    )

    ladder_feed2_steps: int = 13128

    ladder_lincnt_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(_LADDER_LINCNT_BY_DPI_8300I)
    )

    exposure_lperiod: int = 15000
    exposure_short: int = 15000
    #: ME long pass for ``me_long_mode="fixed"``: 4x the short. SilverFast's ME
    #: captures program 60000 at both 1200 and 3600 ppi.
    me_fixed_long_exposure: int = 60000
    pixel_clock_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(_PIXEL_CLOCK_BY_DPI_8300I)
    )
    pixel_clock_long_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(_PIXEL_CLOCK_LONG_BY_DPI_8300I)
    )
    dummy_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(_DUMMY_BY_DPI_8300I)
    )
    sensor_custom_regs: Mapping[int, int] = field(
        default_factory=lambda: dict(_SCAN_REGS_8300I)
    )
    # Cold-boot capture: the SE boot blast and memory layout (0x36 per firmware,
    # docs §10.7); LPERIOD follows the shared window rule at every measured PPI.
    # Declared here because the OpticFilm 135i has its own.
    resolutions_dpi: tuple[int, ...] = ALL_PPI
    register_dpiset_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(REGISTER_DPISET)
    )
    lperiod_by_dpi: Mapping[int, int] = field(default_factory=lambda: dict(LPERIOD_BY_DPI))
    feed_steps_per_inch: int = 14400
    init_regs: Mapping[int, int] = field(default_factory=lambda: dict(INIT_REGS))
    memory_layout_regs: Mapping[int, int] = field(
        default_factory=lambda: dict(MEMORY_LAYOUT_REGS)
    )
    # Every capture: 0x8C[0x10] = 0x8C[0x13] = 0x0E at boot, then 0x0E and 0x0D
    # before each scan pass. Scanning at 0x0E runs the motor fast, rattles and
    # stalls mid-pass; left unset, the clock keeps its power-on value.
    asic_clock_boot_0x8c: int = 0x0E
    asic_clock_scan_0x8c: int = 0x0D
    # Plug-in capture: 0x0B = 0x84, 0x13 = 0x0F, 0x0B = 0x8C after the boot clock.
    asic_boot_0x0b: int = 0x8C
    #: The ``0x1D=0x81`` doubling at 7200 dpi is already in
    #: :meth:`channel_exposure_for`, which gives 15000 there like SilverFast.
    native_image_exposure_factor: int = 1
    #: 512 KiB announces crashed the motor mid-pass at 1800 and 7200 dpi on
    #: hardware with the SilverFast scan motor table.
    image_read_chunk_bytes: int = 0xF000
    #: Every SilverFast capture, 150 to 7200 dpi: bulk bytes = ``LINCNT`` x one
    #: full row. Under the SE/V2 4x ``LINCNT`` the carriage ran twice the frame
    #: height and hit the end of travel.
    usb_image_lincnt_half_lines: bool = False
    image_lincnt_per_line: int = 1
    shading_linesel_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(_SHADING_LINESEL_BY_ASIC_DPI_8300I)
    )

    def feed_to_scan_steps_for_dpi(
        self, resolution: int, *, long_exposure: bool = False
    ) -> int:
        """Colour-short feed2 by asic dpi; ME-long always 13128 (docs §10.2)."""
        if long_exposure:
            return 13128
        key = self.asic_dpi_for(resolution)
        table = _FEED2_BY_ASIC_DPI_8300I
        if key in table:
            return int(table[key])
        nearest = min(table, key=lambda k: abs(int(k) - int(key)))
        return int(table[nearest])

    def scaled_ld_shift(self, ld: int, asic_dpi: int) -> int:
        """Round line-distance shifts (floor left ~0.8 G / ~0.6 B at 1440)."""
        return round(int(ld) * int(asic_dpi) / int(self.motor_base_ydpi))

    def stagger_y_for(self, resolution: int) -> tuple[int, ...]:
        """Host column unstagger; see ``_STAGGER_BY_DPI_8300I``."""
        key = self.asic_dpi_for(resolution)
        return tuple(_STAGGER_BY_DPI_8300I.get(key, ()))

    def channel_exposure_for(self, resolution: int, *, exposure: int | None = None) -> int:
        """Scan motor period: ``exposure × ((0x1D & 0x1F) + 1) × asic_dpi / 14400``.

        Half the SE/V2 ``exposure // oversample`` at every PPI in the SilverFast
        captures (1875 at 1800 dpi, 3750 at 3600, 15000 at 7200 with
        ``0x1D=0x81``; ME-long 60000 gives 15000 at 3600).
        """
        base = int(exposure if exposure is not None else self.exposure_lperiod)
        oversample = self.oversample_for(resolution)
        reg_1d_lines = 2 if oversample == 1 else 1
        return base * reg_1d_lines // (2 * oversample)

    def scan_motor_table(self, period: int) -> tuple[int, ...]:
        """Vendor acceleration ramp clamped at ``period`` (head 40404)."""
        return scan_motor_table(period)

    def slope_table_fast(self) -> tuple[int, ...]:
        """8300i CUSTOM fast ramp (head ``0x846A``)."""
        return SLOPE_TABLE_FAST

    def slope_table_slow(self) -> tuple[int, ...]:
        """8300i CUSTOM slow ramp (head ``0x32BB``)."""
        return SLOPE_TABLE_SLOW

    def shading_strip_clocks(
        self, resolution: int, *, dvdset: bool, span: int | None = None
    ) -> tuple[int, int, int]:
        """Return ``(dummy, clk_a, clk_b)`` for a shading strip.

        Dark (DVDSET off): the shared GL128 window rule, which matches every
        measured 8300i dark strip (600-7200 ppi). White: the 8300i's own clocks,
        per asic dpi (docs §10.4); unmeasured 720/1440 fall back to the nearest
        measured key.
        """
        if not dvdset:
            return super().shading_strip_clocks(resolution, dvdset=False, span=span)
        key = self.asic_dpi_for(resolution)
        table = _SHADING_WHITE_BY_ASIC_DPI
        if key in table:
            return table[key]
        nearest = min(table, key=lambda k: abs(int(k) - int(key)))
        return table[nearest]


MODEL_8300I_SE = Model8300iSE()
