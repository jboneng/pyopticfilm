# SPDX-License-Identifier: GPL-3.0-or-later
"""OpticFilm 8300i SE model definition (GL128).

The 8300i SE (``07b3:181f``, ``bcdDevice=0x0702``) uses the GL128 ASIC and is
a sibling of the 8200i SE and 8100 V2.  It subclasses :class:`Gl128Common`,
**not** either leaf, so sibling capture constants cannot leak through
inheritance.

All divergent knobs below come from SilverFast 9 USBPcap full-frame captures
(``docs/gl128-model-comparison.md`` §10).  Summary:

**Geometry (V2-like)**
    Full-frame STR/END = 242/10610.  Settled colour-short feed2 is
    PPI-dependent (13040…13128); :meth:`feed_to_scan_steps_for_dpi` programs
    that map (ME-long → 13128).  ``feed_to_scan_steps`` stays 13128 as a
    dpi-less fallback.  Image ``LINCNT`` is the V2 ladder table (SilverFast
    programs SF = V2/4 on the wire; pyopticfilm keeps the 4× convention).

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

# Measured full-frame PPI in the capture set: 300, 1200, 2400, 3600, 7200.
# Mid-ladder keys below are filled (not capture-proven); CI goldens at 1800
# use those fills — see docs §10.8.

# V2 capture LPERIOD ladder (docs §5 / 06_ppi_ladder). Measured PPI match
# byte-identically; unmeasured rungs (150/600/720/900/1440/1800) are the same
# V2 table fills, not an 8300i capture.
_LPERIOD_BY_DPI_8300I: dict[int, int] = {
    150: 11067,  # filled (V2 ladder; asic band shares 300)
    300: 11067,  # measured
    600: 11067,  # filled (V2 ladder; asic band shares 300)
    720: 11110,  # filled (V2 ladder)
    900: 11175,  # filled (V2 ladder)
    1200: 11283,  # measured
    1440: 11369,  # filled (V2 ladder)
    1800: 11499,  # filled (V2 ladder; CI setup golden)
    2400: 11715,  # measured
    3600: 13443,  # measured
    7200: 16035,  # measured
}
assert set(_LPERIOD_BY_DPI_8300I) == set(LADDER_LINCNT_BY_DPI)

# Same physical full-frame height as the 8100 V2 ladder (SF wire LINCNT × 4).
# Measured at 300/1200/2400/3600/7200; other rungs match V2's
# SE + 128 * asic_dpi / 600 formula (filled).
_LADDER_LINCNT_BY_DPI_8300I: dict[int, int] = {
    150: 2420,  # filled
    300: 2420,  # measured (SF×4)
    600: 2420,  # filled
    720: 2904,  # filled
    900: 3628,  # filled
    1200: 4836,  # measured (SF×4)
    1440: 5804,  # filled
    1800: 7252,  # filled (CI setup golden)
    2400: 9668,  # measured (SF×4)
    3600: 14500,  # measured (SF×4)
    7200: 29012,  # measured (SF×4)
}
assert set(_LADDER_LINCNT_BY_DPI_8300I) == set(LADDER_LINCNT_BY_DPI)

# Image-pass dummy 0x2B. Measured at 300/1200/2400/3600/7200; other rungs use
# the nearest measured asic-dpi band (150/300/600 share; 720/900→600 band;
# 1440/1800→1200 band) — filled, not capture-proven.
_DUMMY_BY_DPI_8300I: dict[int, int] = {
    150: 0x06,  # filled → 300 band
    300: 0x06,  # measured
    600: 0x06,  # filled → 300 band
    720: 0x06,  # filled → 300 band
    900: 0x06,  # filled → 300 band
    1200: 0x07,  # measured
    1440: 0x07,  # filled → 1200 band
    1800: 0x07,  # filled → 1200 band (CI setup golden)
    2400: 0x0B,  # measured
    3600: 0x10,  # measured
    7200: 0x1F,  # measured
}

# Image-pass 0xA5/0xAB. Same fill policy as dummy.
_PIXEL_CLOCK_BY_DPI_8300I: dict[int, int] = {
    150: 0x59,  # filled → 300 band
    300: 0x59,  # measured
    600: 0x59,  # filled → 300 band
    720: 0x59,  # filled → 300 band
    900: 0x59,  # filled → 300 band
    1200: 0x12,  # measured
    1440: 0x12,  # filled → 1200 band
    1800: 0x12,  # filled → 1200 band (CI setup golden)
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
_FEED2_BY_ASIC_DPI_8300I: dict[int, int] = {
    600: 13040,  # measured at 300 (DPISET 100)
    1200: 13112,  # measured
    2400: 13124,  # measured
    3600: 13126,  # measured
    7200: 13128,  # measured
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

    # Dpi-less fallback (7200 / ME-long). Prefer feed_to_scan_steps_for_dpi.
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
