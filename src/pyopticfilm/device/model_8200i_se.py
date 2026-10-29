# SPDX-License-Identifier: GPL-3.0-or-later
"""OpticFilm 8200i SE model tables (GL128).

SANE genesys has no GL128 command set, so nothing here is ported from SANE.
Every register value is taken from USB captures of the Windows driver stored in
``captures/8200i-se/`` (published as jboneng/pyopticfilm_captures); each table
below names the session that produced it. See ``docs/gl128-model-comparison.md``
for the protocol synthesis and the dataset's ``SESSIONS.md`` / per-session
``NOTES.md`` for decode detail.

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
every PPI including 7200. Host assemble still rounds mid-ladder ``ld_shift`` and
unstaggers columns at 1440/2400 (Scan Lab TIFF-validated; ASIC bit stays clear).

Shared GL128 tables and helpers live in :mod:`pyopticfilm.device.gl128_common`.
This class only declares SE identity and the capture-proven divergences from
the 8100 V2 (see :data:`~pyopticfilm.device.gl128_common.GL128_DIVERGENT_FIELDS`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pyopticfilm.device.gl128_common import (
    DUMMY_BY_DPI,
    LADDER_LINCNT_BY_DPI,
    PIXEL_CLOCK_BY_DPI,
    PIXEL_CLOCK_LONG_BY_DPI,
    SCAN_REGS,
    SHADING_LINESEL_BY_DPI,
    Gl128Common,
)

# Host column Y unstagger (apply_y_stagger) for PPIs with native period-2 sawtooth
# in Scan Lab TIFFs (2026-10-13). Pre-mirror pattern (1, 0); ASIC STAGGER bit stays
# clear (session 13 / Scan Lab). Same host technique as the 8300i SE leaf.
_STAGGER_BY_DPI_8200I_SE: dict[int, tuple[int, ...]] = {
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


@dataclass(frozen=True)
class Model8200iSE(Gl128Common):
    """OpticFilm 8200i SE — GL128 capture tables (hardware-tested)."""

    name: str = "plustek-opticfilm-8200i-se"
    model: str = "OpticFilm 8200i SE"
    usb_product_id: int = 0x1825
    supports_infrared: bool = True
    scan_ready: bool = True

    #: Default full-frame colour. Sessions 03/08/09a measured the true scan-window
    #: top at 13128 (matches feed_to_scan_top_steps and the V2's value for this
    #: purpose); 13704 from session 04 is only "full-ish" (see
    #: captures/8200i-se/09_y_crop_pair/NOTES.md) and overruns the window at high
    #: DPI (see #67).
    feed_to_scan_steps: int = 13128

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

    exposure_lperiod: int = 14000
    exposure_short: int = 14000
    #: ME long pass for ``me_long_mode="fixed"``: 3x the short, the long
    #: exposure SilverFast's ME captures use on this model.
    me_fixed_long_exposure: int = 42000
    #: 13_7200ppi_color: SilverFast uploads 28000 per channel with
    #: ``REG_EXPOSURE`` 14000. At 14000 a full-width line outruns USB and the
    #: motor stops and restarts every few seconds.
    native_image_exposure_factor: int = 2
    image_read_chunk_bytes: int = 0xF000
    #: Session 13: the bulk buffer holds ``LINCNT / 2`` rows, two per output line.
    usb_image_lincnt_half_lines: bool = True
    image_lincnt_per_line: int = 4
    shading_linesel_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(SHADING_LINESEL_BY_DPI)
    )
    pixel_clock_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(PIXEL_CLOCK_BY_DPI)
    )
    pixel_clock_long_by_dpi: Mapping[int, int] = field(
        default_factory=lambda: dict(PIXEL_CLOCK_LONG_BY_DPI)
    )
    dummy_by_dpi: Mapping[int, int] = field(default_factory=lambda: dict(DUMMY_BY_DPI))
    sensor_custom_regs: Mapping[int, int] = field(default_factory=lambda: dict(SCAN_REGS))
    #: Clock written through ``0x8C`` to indices ``0x10`` and ``0x13``: once at
    #: boot, then boot value followed by scan value before every scan pass.
    #: ``0x0C`` for both in every SE capture (V2: ``0x0E``/``0x0C``,
    #: 8300i SE: ``0x0E``/``0x0D``).
    asic_clock_boot_0x8c: int = 0x0C
    asic_clock_scan_0x8c: int = 0x0C
    #: ``0x0B`` after the boot clock: DRAM enable is toggled off (``0x44``) and
    #: on (``0x4C``). The top bits track the clock (V2 / 8300i SE: ``0x8C``).
    asic_boot_0x0b: int = 0x4C

    def scaled_ld_shift(self, ld: int, asic_dpi: int) -> int:
        """Round line-distance shifts (floor left ~0.8 G / ~0.6 B at 1440)."""
        return round(int(ld) * int(asic_dpi) / int(self.motor_base_ydpi))

    def stagger_y_for(self, resolution: int) -> tuple[int, ...]:
        """Host column unstagger; see ``_STAGGER_BY_DPI_8200I_SE``."""
        key = self.asic_dpi_for(resolution)
        return tuple(_STAGGER_BY_DPI_8200I_SE.get(key, ()))


MODEL_8200I_SE = Model8200iSE()
