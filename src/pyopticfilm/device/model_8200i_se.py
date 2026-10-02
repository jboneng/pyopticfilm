# SPDX-License-Identifier: GPL-3.0-or-later
"""OpticFilm 8200i SE model tables (GL128).

SANE genesys now has a GL128 driver ported from this project and checked
against SilverFast. The 7200 dpi width timing on this model follows that
check. Every other register value is taken from USB captures of the Windows
driver stored in ``captures/8200i-se/``; each table below names the session
that produced it.
See ``captures/8200i-se/PROTOCOL.md`` for the full protocol synthesis and
``SESSION_LOG.md`` / per-session ``NOTES.md`` for decode detail.

The ASIC is GL124-family, not GL845: the frontend is reached through
``0x51``/``0x5D``/``0x5E``, status lives at ``0x101``, and the geometry
registers are ``LINCNT`` ``0x25``, ``LPERIOD`` ``0x28``, ``DPISET`` ``0x2C``,
``STRPIXEL`` ``0x82`` and ``ENDPIXEL`` ``0x85``.

Two properties of this map are worth knowing before reading the code:

* ``STRPIXEL`` / ``ENDPIXEL`` are in **native 7200 dpi units** and therefore do
  not change with resolution — the captures show byte-identical values for the
  same crop at 1800 and 3600 dpi.
* ``LINCNT`` is **not** in native units. Session 13 shows ``LINCNT / dpi``
  constant at 3.816 across the whole PPI ladder (one crop scanned at eleven
  resolutions) and every capture's bulk buffer holds exactly ``LINCNT / 2``
  rows, but those rows are *not* output lines: the buffer is sampled at twice
  the programmed dpi in Y. The ladder crop is 36.06 x 24.24 mm — a 3:2 35 mm
  frame — so one output line is four ``LINCNT`` units and two buffer rows, and
  Y travel is ``LINCNT x 25.4 / (4 x asic_dpi)``
  (see :attr:`Model8200iSE.image_lincnt_per_line`). Getting this factor wrong
  stretches every scan vertically; the 1200 dpi ladder buffer is 1704 x 2290
  and must render 1704 x 1145.

SilverFast 9 PPI ladder (session ``13_ppi_ladder``): 150, 300, 600, 720, 900,
1200, 1440, 1800, 2400, 3600, 7200. Below 600 dpi the ASIC is programmed like
600 (``DPISET`` floors at 100); the host downsamples. ``STAGGER`` was clear at
every PPI including 7200.

Shared GL128 tables and helpers live in :mod:`pyopticfilm.device.gl128_common`.
This class only declares SE identity and the capture-proven divergences from
the 8100 V2 (see :data:`~pyopticfilm.device.gl128_common.GL128_DIVERGENT_FIELDS`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pyopticfilm.device.gl128_common import LADDER_LINCNT_BY_DPI, LPERIOD_BY_DPI, Gl128Common


@dataclass(frozen=True)
class Model8200iSE(Gl128Common):
    """OpticFilm 8200i SE — GL128 capture tables (hardware-tested)."""

    name: str = "plustek-opticfilm-8200i-se"
    model: str = "OpticFilm 8200i SE"
    usb_product_id: int = 0x1825
    supports_infrared: bool = True

    #: Default full-frame colour. Sessions 03/08/09a measured the true scan-window
    #: top at 13128 (matches feed_to_scan_top_steps and the V2's value for this
    #: purpose); 13704 from session 04 is only "full-ish" (see
    #: captures/8200i-se/09_y_crop_pair/NOTES.md) and overruns the window at high
    #: DPI (see #67).
    feed_to_scan_steps: int = 13128

    #: SilverFast 9 PPI ladder (session 13). V2 overrides 7200 dpi only.
    lperiod_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(LPERIOD_BY_DPI)
    )

    #: Capture image ``LINCNT`` for each second-feed distance, kept as a
    #: regression fixture. The motor gate uses :meth:`max_lincnt_for`.
    max_image_lincnt_by_feed2: Mapping[int, int] = field(
        default_factory=lambda: {
            13128: 4836,  # session 03 preview @1200 / 09a @1800 (3700)
            13560: 27476,  # session 13 PPI ladder @7200
            13704: 6628,  # session 04 colour @1800
            20232: 3700,  # session 09b @1800
        }
    )

    #: Session 13 PPI-ladder second feed (crop origin; PPI-independent).
    ladder_feed2_steps: int = 13560

    #: Session 13 PPI-ladder image LINCNT per SilverFast PPI, at this model's
    #: own ``ladder_feed2_steps`` (13560). V2's ladder crop starts 432 steps
    #: earlier (feed2=13128), so it needs a taller crop and its own table —
    #: see :data:`~pyopticfilm.device.model_8100_v2._LADDER_LINCNT_BY_DPI_V2`.
    ladder_lincnt_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(LADDER_LINCNT_BY_DPI)
    )

    def timing_for_native_width(
        self, resolution: int, width_native: int
    ) -> tuple[int, tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]] | None:
        """7200 dpi line timing for a native window, or ``None`` to use the DPI table.

        SilverFast sets ``LPERIOD``, dummy and pixel clocks from the window
        width at 7200 dpi. Four captured widths are the rows below; a wider
        window keeps :meth:`line_period_for` (15963) and the DPI clock tables.
        The 8100 V2 does not use this — its 7200 dpi line period stays 16035.
        """
        if self.asic_dpi_for(resolution) != 7200:
            return None
        width = int(width_native)
        for max_width, lperiod, image, dark, white in _SE_7200_WIDTH_PROGRAMS:
            if width <= max_width:
                return lperiod, image, dark, white
        return None


#: ``(max native width, LPERIOD, image clocks, dark clocks, white clocks)``.
#: Clocks are ``(0x2B, 0xA5, 0xAB)``. ``LPERIOD = 10851 + width/2`` on these
#: four SilverFast 7200 dpi captures; the clock bytes do not follow a formula.
_SE_7200_WIDTH_PROGRAMS: tuple[
    tuple[int, int, tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]],
    ...,
] = (
    (1416, 11559, (0x03, 0x01, 0x01), (0x04, 0x01, 0x30), (0x02, 0x02, 0x02)),
    (2832, 12267, (0x05, 0x01, 0x01), (0x07, 0x01, 0x30), (0x03, 0x02, 0x02)),
    (5664, 13683, (0x09, 0x01, 0x01), (0x0D, 0x01, 0x30), (0x09, 0x01, 0x01)),
    (10200, 15951, (0x17, 0x01, 0x01), (0x17, 0x01, 0x30), (0x0F, 0x01, 0x01)),
)


MODEL_8200I_SE = Model8200iSE()
