# SPDX-License-Identifier: GPL-3.0-or-later

"""OpticFilm 8100 V2 model definition (GL128).

The 8100 V2 (``07b3:1824``) uses the GL128 ASIC and is closely related to the
8200i SE (``07b3:1825``).  It subclasses :class:`Gl128Common` (shared tables),
**not** :class:`~pyopticfilm.device.model_8200i_se.Model8200iSE`, so SE capture
constants cannot leak through inheritance.  Five constants differ from the SE,
all derived from USB captures of the V2 Windows driver (capture session Aug
2026).

**feed_to_scan_steps = 13 128**
    The V2's full-frame colour scan starts at the top of the TA window (second
    feed = 13 128 steps from the reference position), not the SE default of
    13 704.  The SE documents 13 128 as ``feed_to_scan_top_steps`` — the
    top-of-window preview position used in session 03.  Using the SE default
    (13 704) on V2 hardware positions the scan head 576 steps (~1 mm) too deep,
    clipping ~1 mm at the top of the 35 mm film frame.
    *Capture evidence*: ``04_color_7200.pcapng`` frame 2999, registers
    0x3D–0x3F = 0x00 0x33 0x48 = 13 128.

**lperiod_by_dpi[7200] = 16 035**
    LPERIOD for the 8200i SE comes from the SilverFast 9 PPI ladder (SE session
    13) and is 15 963 at 7200 dpi.  Every scan phase in the V2 capture — dark
    shading (frame 1661), white shading (frame 2257), and image pass (frame
    3203) — uses 16 035 instead.  The difference of +72 shifts the pixel-clock
    budget and would produce timing errors on V2 hardware if the SE value is
    used.  Only 7200 dpi is confirmed; all other DPI entries are inherited from
    the shared GL128 map.

**shading_strip_clocks: white-strip dummy at 7200 dpi = 0x10**
    The white shading strip on V2 uses dummy register 0x2B = 0x10 (frame 2257)
    while the SE driver computes 0x17 from ``dummy_by_dpi[7200]``.  This
    difference affects only the calibration strip; the image-pass dummy remains
    0x17 on both devices.  Dark-strip dummy is 0x17 on both (the SE driver
    falls back to ``dummy_by_dpi[7200]`` at 7200 dpi; V2 frame 1661 confirms
    the same value).

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
    ``feed_to_scan_steps``.

Unlike the 8200i SE, the 8100 V2 has no infrared channel or iSRD support.
Multi-exposure colour scanning remains supported.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pyopticfilm.device.gl128_common import LPERIOD_BY_DPI, Gl128Common

# V2 7200 dpi LPERIOD observed in all three scan phases (dark/white shading and
# image pass): 16 035.  All other DPI entries are carried over from the shared
# GL128 map (SE session 13).
_LPERIOD_BY_DPI_V2: dict[int, int] = {
    **LPERIOD_BY_DPI,
    7200: 16035,
}


@dataclass(frozen=True)
class Model8100V2(Gl128Common):
    """OpticFilm 8100 V2 — GL128 sibling of the 8200i SE without IR.

    See module docstring for the five capture-derived overrides.
    """

    name: str = "plustek-opticfilm-8100-v2"
    model: str = "OpticFilm 8100 (V2)"
    usb_product_id: int = 0x1824
    supports_infrared: bool = False

    # Capture-derived override: V2 full-frame scan starts at the TA window top.
    # 04_color_7200.pcapng frame 2999, regs 0x3D–0x3F = 0x003348 = 13 128.
    feed_to_scan_steps: int = 13128

    # 04_color_7200.pcapng frames 1661/2257/3203, reg 0x28–0x2A = 0x003EA3 = 16035.
    lperiod_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(_LPERIOD_BY_DPI_V2)
    )

    # V2 capture LINCNT at feed2=13128: 29012 (full-frame 7200 dpi, frame 3203).
    max_image_lincnt_by_feed2: Mapping[int, int] = field(
        default_factory=lambda: {13128: 29012}
    )

    # V2 uses feed2=13128 for all scan types (top of TA window), not SE's 13560.
    ladder_feed2_steps: int = 13128

    # V2-only: second (final positioning) feed uses SLOPE_TABLE_SLOW.
    # Two independent V2 captures (plus recovered 04_color_7200.pcapng).
    # 8200i SE is the inverse (slow reference feed, fast final feed).
    use_slow_final_positioning_feed: bool = True
    # 2026-08-30: n_brackets > 2 (N-bracket ME) validated on real V2 hardware
    # specifically at the fixed 42000 top exposure (SilverFast known-good
    # colour-long, see clamp_me_long_for_dpi). Real per-frame adaptive
    # selection has not been separately validated for the N-bracket path on
    # this model, so brackets stay pinned to that one proven-safe value by
    # default. Still fully overridable via Scanner.scan(me_exposure_mode=
    # "adaptive"). Does not affect n_brackets == 2, which is unchanged.
    me_default_exposure_mode: str = "fixed"
    # 2026-08-31: V2 hardware validation of the ME long ceiling has only
    # covered 42000 (see me_default_exposure_mode above) — no per-DPI data
    # yet the way the SE's 7200-dpi-vs-other split has. Until further
    # hardware testing, pin the ceiling flat at 42000 for every resolution
    # (no DPI-keyed entries → every lookup falls through to the default).
    # Loosen this once V2 is validated at higher exposures / other DPIs.
    me_long_exposure_ceiling_by_dpi: Mapping[int, int] = field(default_factory=dict)
    me_long_exposure_ceiling_default: int = 42000

    #: Use row-banded pass alignment (pyopticfilm.pass_align.
    #: align_pass_to_reference_banded) and the N-bracket merge's luma-only
    #: misalignment gate for the 2-bracket ME path too, not just
    #: n_brackets > 2. V2-only: independently measured near-pure Y-axis
    #: drift that grows along a tall pass (see jboneng/pyopticfilm#33) and
    #: real-hardware ghosting on flat/neutral content this addresses are
    #: both V2-specific so far; SE keeps the original byte-identical path.
    me_use_banded_alignment: bool = True

    def me_n_bracket_long_exposure_ceiling(self, resolution: int) -> int:
        """Pin N-bracket (``n_brackets > 2``) longs at 42000 for every DPI.

        The only top exposure validated on real V2 hardware for N-bracket ME.
        Two-bracket scans still use :meth:`me_long_exposure_ceiling` /
        ``clamp_me_long_for_dpi`` (85000 at non-7200).
        """
        return 42_000

    def shading_strip_clocks(self, resolution: int, *, dvdset: bool) -> tuple[int, int, int]:
        """Return ``(dummy, clk_a, clk_b)`` for a shading strip.

        V2 white shading at 7200 dpi uses dummy=0x10 (SE computes 0x17 from
        ``dummy_by_dpi``).  All other cases delegate to the shared
        implementation.  Capture evidence: ``04_color_7200.pcapng`` frame 2257,
        reg 0x2B = 0x10.
        """
        if dvdset and self.asic_dpi_for(resolution) == 7200:
            return 0x10, 0x01, 0x01
        return super().shading_strip_clocks(resolution, dvdset=dvdset)


MODEL_8100_V2 = Model8100V2()
