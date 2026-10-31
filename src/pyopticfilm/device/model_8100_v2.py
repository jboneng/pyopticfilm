# SPDX-License-Identifier: GPL-3.0-or-later

"""OpticFilm 8100 V2 model definition (GL128).

The 8100 V2 (``07b3:1824``) uses the GL128 ASIC and is closely related to the
8200i SE (``07b3:1825``).  It subclasses :class:`Gl128Common` (shared tables),
**not** :class:`~pyopticfilm.device.model_8200i_se.Model8200iSE`, so SE capture
constants cannot leak through inheritance.  Three constants differ from the SE,
all derived from USB captures of the V2 Windows driver (capture session Aug
2026); the ASIC clock and ``0x0B`` boot values on the class differ as well.

**feed_to_scan_steps = 13 128** (same as the SE today)
    The V2's full-frame colour scan starts at the top of the TA window (second
    feed = 13 128 steps from the reference position).  The SE originally used
    13 704 from its session 04 crop, which positions the scan head 576 steps
    (~1 mm) too deep; the SE now uses 13 128 as well, so this is declared on
    both leaves rather than being a V2 difference.
    *Capture evidence*: ``04_color_7200.pcapng`` frame 2999, registers
    0x3D–0x3F = 0x00 0x33 0x48 = 13 128.

``LPERIOD`` is not a V2 difference: the V2's 16 035 at 7200 dpi (frames 1661 /
2257 / 3203) is the full-frame value of the window rule every GL128 model shares
(:func:`~pyopticfilm.device.gl128_common.lperiod_for_window`); the SE's old
15 963 was its session-13 crop.

The shading-strip clocks are not a V2 difference: the V2's white-strip dummy
``0x10`` at 7200 dpi (frame 2257) is the full-frame value of the window rule both
models share (:func:`~pyopticfilm.device.gl128_common.shading_clocks_for_window`).

**max_image_lincnt_by_feed2 = {13128: 29012}**
    The SE regression fixture maps second-feed distances to capture LINCNT
    values.  At key 13 128, the SE entry is 4 836 (a 1200 dpi preview from SE
    session 03).  V2 capture shows LINCNT = 29 012 at feed2 = 13 128 — the
    full-frame 7200 dpi image pass.  The V2 table contains only the confirmed
    V2 entry; SE-specific entries are not meaningful here.
    *Capture evidence*: ``04_color_7200.pcapng`` frame 3203, registers
    0x25–0x27 = 0x00 0x71 0x54 = 29 012.

**ladder_feed2_steps = 13 128**
    The SE PPI ladder uses feed2 = 13 560 (session 13 crop origin).  V2 uses
    feed2 = 13 128 (top of TA window) for all scan types, matching
    ``feed_to_scan_steps``.
    *Capture evidence*: ``04_color_7200.pcapng`` frame 2999, same as
    ``feed_to_scan_steps``. Confirmed directly (not just extrapolated) by the
    ``06_ppi_ladder.pcapng`` reference capture in ``pyopticfilm_captures``,
    which reproduces feed2 = 13 128 at every rung of the DPI ladder.

**ladder_lincnt_by_dpi**
    V2's ladder crop starts 432 steps earlier than the SE's (13 128 vs
    13 560), so it needs a taller image LINCNT at every PPI to cover the same
    physical window. ``06_ppi_ladder.pcapng`` gives a directly measured image
    LINCNT for each rung (150/300/600 dpi all floor to the same ASIC
    programming and share one value); the 7200 dpi entry is carried over from
    ``04_color_7200.pcapng`` (matches ``max_image_lincnt_by_feed2``). Every
    entry is the SE's session-13 value plus ``128 * asic_dpi / 600`` — the
    proportional cost of the 432-step-longer crop.

Unlike the 8200i SE, the 8100 V2 has no infrared channel or iSRD support.
Multi-exposure colour scanning remains supported.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pyopticfilm.device.gl128_common import (
    ALL_PPI,
    DUMMY_BY_DPI,
    INIT_REGS,
    LADDER_LINCNT_BY_DPI,
    LPERIOD_BY_DPI,
    MEMORY_LAYOUT_REGS,
    PIXEL_CLOCK_BY_DPI,
    PIXEL_CLOCK_LONG_BY_DPI,
    REGISTER_DPISET,
    SCAN_REGS,
    SHADING_LINESEL_BY_DPI,
    Gl128Common,
)

# V2's ladder crop (feed2=13128) starts 432 steps earlier than the SE's
# (feed2=13560), so it needs a taller image LINCNT at every PPI: the SE's
# session-13 value plus 128 * asic_dpi / 600. 150/300/600 dpi all floor to the
# same 600 dpi ASIC programming and share one measured value.
# Source: 06_ppi_ladder.pcapng (150-3600 dpi) + 04_color_7200.pcapng (7200 dpi).
_LADDER_LINCNT_BY_DPI_V2: dict[int, int] = {
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
assert set(_LADDER_LINCNT_BY_DPI_V2) == set(LADDER_LINCNT_BY_DPI)

# Host column Y unstagger (apply_y_stagger), pre-mirror patterns. Scan Lab TIFF
# evidence (8100 V2, 35 mm frame, 2026-10-16), measured on the mount edge as the
# row offset between even and odd output columns (period 2 in x, lag-2 autocorr
# dominant), then confirmed by rescanning with the pattern applied:
#   7200: 4.04 rows -> (4, 0) brings it to 0.05 (same CCD stagger the GL845
#         leaves correct at 7200).
#   1440: 0.79 rows and 2400: 1.35 rows -> (1, 0) as on the SE / 8300i leaves;
#         the mirrored (0, 1) was tried first and doubled the comb instead.
# 1200 / 3600 measured flat. Other PPIs left empty; ASIC STAGGER bit stays clear.
_STAGGER_BY_DPI_8100_V2: dict[int, tuple[int, ...]] = {
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
    7200: (4, 0),
}


@dataclass(frozen=True)
class Model8100V2(Gl128Common):
    """OpticFilm 8100 V2 — GL128 sibling of the 8200i SE without IR.

    See module docstring for the three capture-derived overrides.
    """

    name: str = "plustek-opticfilm-8100-v2"
    model: str = "OpticFilm 8100 (V2)"
    usb_product_id: int = 0x1824
    supports_infrared: bool = False
    scan_ready: bool = True

    # Capture-derived override: V2 full-frame scan starts at the TA window top.
    # 04_color_7200.pcapng frame 2999, regs 0x3D–0x3F = 0x003348 = 13 128.
    feed_to_scan_steps: int = 13128

    # V2 capture LINCNT at feed2=13128: 29012 (full-frame 7200 dpi, frame 3203).
    max_image_lincnt_by_feed2: Mapping[int, int] = field(
        default_factory=lambda: {13128: 29012}
    )

    # V2 uses feed2=13128 for all scan types (top of TA window), not SE's 13560.
    ladder_feed2_steps: int = 13128

    # V2 ladder crop needs a taller LINCNT than the SE's at every PPI (see
    # module docstring). Source: 06_ppi_ladder.pcapng + 04_color_7200.pcapng.
    ladder_lincnt_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(_LADDER_LINCNT_BY_DPI_V2)
    )

    # Capture-identical to the SE session-13 maps (shared before 8300i promotion).
    exposure_lperiod: int = 14000
    exposure_short: int = 14000
    #: ME long pass for ``me_long_mode="fixed"``: 3x the short, as on the 8200i SE.
    me_fixed_long_exposure: int = 42000
    pixel_clock_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(PIXEL_CLOCK_BY_DPI)
    )
    pixel_clock_long_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(PIXEL_CLOCK_LONG_BY_DPI)
    )
    dummy_by_dpi: Mapping[int, int] = field(default_factory=lambda: dict(DUMMY_BY_DPI))
    sensor_custom_regs: Mapping[int, int] = field(default_factory=lambda: dict(SCAN_REGS))
    # 02_cold_boot_open: the boot blast and memory layout are the SE's (0 diffs,
    # docs/gl128-model-comparison.md §3), as are the DPISET / LPERIOD rules and
    # the feed scale. Declared here because the OpticFilm 135i has its own.
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
    # 02_cold_boot_open / 03_prescan / 05_midtravel_home: 0x8C[0x10] = 0x8C[0x13]
    # = 0x0E at boot, 0x0C before the scan session (SE: 0x0C for both).
    asic_clock_boot_0x8c: int = 0x0E
    asic_clock_scan_0x8c: int = 0x0C
    # 03_prescan: 0x0B = 0x84, 0x13 = 0x0F, 0x0B = 0x8C after the boot clock.
    asic_boot_0x0b: int = 0x8C
    #: 04_color_7200: SilverFast uploads 28000 per channel with REG_EXPOSURE 14000.
    native_image_exposure_factor: int = 2
    image_read_chunk_bytes: int = 0xF000
    usb_image_lincnt_half_lines: bool = True
    image_lincnt_per_line: int = 4
    shading_linesel_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(SHADING_LINESEL_BY_DPI)
    )

    def scaled_ld_shift(self, ld: int, asic_dpi: int) -> int:
        """Round line-distance shifts (floor left G +0.8 / B +0.6 rows at 1440)."""
        return round(int(ld) * int(asic_dpi) / int(self.motor_base_ydpi))

    def stagger_y_for(self, resolution: int) -> tuple[int, ...]:
        """Host column unstagger; see ``_STAGGER_BY_DPI_8100_V2``."""
        key = self.asic_dpi_for(resolution)
        return tuple(_STAGGER_BY_DPI_8100_V2.get(key, ()))


MODEL_8100_V2 = Model8100V2()
