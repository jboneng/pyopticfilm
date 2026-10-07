# SPDX-License-Identifier: GPL-3.0-or-later
"""OpticFilm 8300i SE model definition (GL128).

The 8300i SE (``07b3:181f``, ``bcdDevice=0x0702``) uses the GL128 ASIC and is
a sibling of the 8200i SE and 8100 V2.  It subclasses :class:`Gl128Common`,
**not** either leaf, so sibling capture constants cannot leak through
inheritance.

All divergent knobs below come from SilverFast 9 USBPcap full-frame captures
(``docs/gl128-model-comparison.md`` §10).  Summary:

**Geometry (V2-like)**
    Full-frame STR/END = 242/10610.  Settled feed2 is PPI-dependent
    (13040…13128); this leaf uses **13128** (7200 / ME-long value and V2 TA
    top) as ``feed_to_scan_steps``.  Image ``LINCNT`` is the V2 ladder table
    (SilverFast programs SF = V2/4 on the wire; pyopticfilm keeps the 4×
    convention).

**Timing**
    Image-pass ``LPERIOD`` matches the V2 capture ladder at every measured
    PPI.  Exposure is **15000** (SE/V2 use 14000).  Image dummy and pixel
    clock maps are 8300i-specific.

**Shading**
    Dark/white strip clocks differ from SE/V2; white @7200 dummy is ``0x26``
    (V2 ``0x10``, SE ``0x17``).  See :meth:`shading_strip_clocks`.

**Slope ROM**
    CUSTOM AHB tables (heads ``0x32BB`` / ``0x846A``), not SE
    ``0x1FB4`` / ``0x16DE``.  See :mod:`pyopticfilm.device.tables_8300i_se`.

**IR / ME**
    Infrared is present (``0x37`` → ``0xB4`` on the IR pass).  Adaptive ME
    long exposure 60000 observed at 1200 and 3600.

**scan_ready**
    ``False`` until the hardware sign-off checklist in
    ``docs/scanner-validation.md`` passes.  Enumerate / open / Lab capture
    decode still work; ``Scanner.scan`` stays gated.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pyopticfilm.device.gl128_common import LADDER_LINCNT_BY_DPI, Gl128Common
from pyopticfilm.device.tables_8300i_se import SLOPE_TABLE_FAST, SLOPE_TABLE_SLOW

# V2 capture LPERIOD ladder (docs §5 / 06_ppi_ladder). 8300i matches at every
# measured PPI (300/1200/2400/3600/7200); unmeasured rungs use the same table.
_LPERIOD_BY_DPI_8300I: dict[int, int] = {
    150: 11067,
    300: 11067,
    600: 11067,
    720: 11110,
    900: 11175,
    1200: 11283,
    1440: 11369,
    1800: 11499,
    2400: 11715,
    3600: 13443,
    7200: 16035,
}
assert set(_LPERIOD_BY_DPI_8300I) == set(LADDER_LINCNT_BY_DPI)

# Same physical full-frame height as the 8100 V2 ladder (SF wire LINCNT × 4).
# Measured at 300/1200/2400/3600/7200; other rungs match V2's
# SE + 128 * asic_dpi / 600 formula.
_LADDER_LINCNT_BY_DPI_8300I: dict[int, int] = {
    150: 2420,
    300: 2420,
    600: 2420,
    720: 2904,
    900: 3628,
    1200: 4836,
    1440: 5804,
    1800: 7252,
    2400: 9668,
    3600: 14500,
    7200: 29012,
}
assert set(_LADDER_LINCNT_BY_DPI_8300I) == set(LADDER_LINCNT_BY_DPI)

# Image-pass dummy 0x2B. Measured at 300/1200/2400/3600/7200; other rungs use
# the nearest measured asic-dpi band (150/300/600 share; 720/900→600 band;
# 1440/1800→1200 band).
_DUMMY_BY_DPI_8300I: dict[int, int] = {
    150: 0x06,
    300: 0x06,
    600: 0x06,
    720: 0x06,
    900: 0x06,
    1200: 0x07,
    1440: 0x07,
    1800: 0x07,
    2400: 0x0B,
    3600: 0x10,
    7200: 0x1F,
}

# Image-pass 0xA5/0xAB. Same fill policy as dummy.
_PIXEL_CLOCK_BY_DPI_8300I: dict[int, int] = {
    150: 0x59,
    300: 0x59,
    600: 0x59,
    720: 0x59,
    900: 0x59,
    1200: 0x12,
    1440: 0x12,
    1800: 0x12,
    2400: 0x05,
    3600: 0x03,
    7200: 0x02,
}

# ME long image pass: 0x02 observed at 1200 and 3600.
_PIXEL_CLOCK_LONG_BY_DPI_8300I: dict[int, int] = {
    150: 0x02,
    300: 0x02,
    600: 0x02,
    720: 0x02,
    900: 0x02,
    1200: 0x02,
    1440: 0x02,
    1800: 0x02,
    2400: 0x02,
    3600: 0x02,
    7200: 0x02,
}

# Shading strip (dummy, clk_a, clk_b) keyed by asic_dpi. 150/300/600 share the
# 300-capture strip (DPISET=100). White @7200 dummy 0x26 is the SE/V2 outlier.
_SHADING_DARK_BY_ASIC_DPI: dict[int, tuple[int, int, int]] = {
    600: (0x02, 0x01, 0x30),
    1200: (0x04, 0x01, 0x30),
    2400: (0x08, 0x01, 0x30),
    3600: (0x0C, 0x01, 0x30),
    7200: (0x17, 0x01, 0x30),
}
_SHADING_WHITE_BY_ASIC_DPI: dict[int, tuple[int, int, int]] = {
    600: (0x03, 0x03, 0x03),
    1200: (0x06, 0x03, 0x03),
    2400: (0x0B, 0x02, 0x02),
    3600: (0x10, 0x02, 0x02),
    7200: (0x26, 0x02, 0x02),
}


@dataclass(frozen=True)
class Model8300iSE(Gl128Common):
    """OpticFilm 8300i SE — GL128 sibling with IR; not yet scan_ready."""

    name: str = "plustek-opticfilm-8300i-se"
    model: str = "OpticFilm 8300i SE"
    usb_product_id: int = 0x181F
    supports_infrared: bool = True
    scan_ready: bool = False

    # 7200 / ME-long settled feed2; colour-short at lower PPI is 13040…13126.
    feed_to_scan_steps: int = 13128

    lperiod_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(_LPERIOD_BY_DPI_8300I)
    )

    max_image_lincnt_by_feed2: Mapping[int, int] = field(
        default_factory=lambda: {13128: 29012}
    )

    ladder_feed2_steps: int = 13128

    ladder_lincnt_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(_LADDER_LINCNT_BY_DPI_8300I)
    )

    exposure_lperiod: int = 15000
    exposure_short: int = 15000
    pixel_clock_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(_PIXEL_CLOCK_BY_DPI_8300I)
    )
    pixel_clock_long_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(_PIXEL_CLOCK_LONG_BY_DPI_8300I)
    )
    dummy_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(_DUMMY_BY_DPI_8300I)
    )

    def slope_table_fast(self) -> tuple[int, ...]:
        """8300i CUSTOM fast ramp (head ``0x846A``)."""
        return SLOPE_TABLE_FAST

    def slope_table_slow(self) -> tuple[int, ...]:
        """8300i CUSTOM slow ramp (head ``0x32BB``)."""
        return SLOPE_TABLE_SLOW

    def shading_strip_clocks(self, resolution: int, *, dvdset: bool) -> tuple[int, int, int]:
        """Return ``(dummy, clk_a, clk_b)`` for a shading strip.

        Capture-derived per asic dpi (docs §10.4). Unmeasured mid-ladder asic
        dpi values fall back to the nearest measured key.
        """
        key = self.asic_dpi_for(resolution)
        table = _SHADING_WHITE_BY_ASIC_DPI if dvdset else _SHADING_DARK_BY_ASIC_DPI
        if key in table:
            return table[key]
        nearest = min(table, key=lambda k: abs(int(k) - int(key)))
        return table[nearest]


MODEL_8300I_SE = Model8300iSE()
