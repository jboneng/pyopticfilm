# SPDX-License-Identifier: GPL-3.0-or-later
"""Calibration strips and shading tables for the GL128 film loader (OpticFilm 135i).

The 135i calibrates at the loader home in toggle mode, like every VueScan pass
in the 135i captures (``docs/gl128-model-comparison.md`` §11):

* **AFE**: a 512-pixel probe strip and a full-width strip, two sensor lines
  each (IR, then visible), at ``DPISET = 1200``. The optical-black pixels
  are the first 31 columns of the window (STRPIXEL 35).
* **Dark**: 256 lines at the scan's DPISET and window, lamp off, toggle bit
  and IR LED off.
* **White**: 256 lines (128 IR + 128 visible) with ``DVDSET`` on the unity
  tables, lamp and IR LED on, motor off.
* **Shading**: one table per line type: the first line of each pair (IR) at
  ``0x10014000`` and the second (visible) at ``0x10034000`` (``0xD0 = 0x0A``,
  ``0xD1 = 0x1A``). Unity is ``0x4000`` on
  this model (``0x06 = 0xF8``); the 8200i SE family uses ``0x2000``.

The table layout is the family one (:func:`~pyopticfilm.scan.calib_gl128.pack_shading_table`).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from pyopticfilm.scan.calib_gl128 import (
    COLOR_SHADING_GAIN_CLAMP_FRAC,
    COLOR_SHADING_GAIN_MEDIAN_MAX,
    COLOR_SHADING_GAIN_MEDIAN_MIN,
    IR_SHADING_WHITE_CLIP_FRAC,
    IR_SHADING_WHITE_CLIP_LEVEL,
    IR_SHADING_WHITE_MEAN_MIN,
    declared_shading_size,
    pack_shading_table,
)

#: Shading banks: bank 10 shades the first line of each pair (IR), bank 26
#: the second (visible). In the panorama capture each table divides out its
#: own line to 0.03 % across the sensor, against 5 % for the other line.
#: VueScan's AFE gains balance the visible line, so in the white strip the IR
#: line reads cast (G/R ~0.73, neutral 0.96 at gain 0) and the visible one
#: neutral (~1.0, cast 1.27 at gain 0); hence unbalanced gains in bank 10.
AHB_SHADING_IR = 0x10014000
AHB_SHADING_VISIBLE = 0x10034000
#: Room between the two banks; one table may not be larger.
SHADING_BANK_BYTES = AHB_SHADING_VISIBLE - AHB_SHADING_IR

#: Gain that leaves a pixel unchanged: VueScan's first shading upload on the
#: 135i carries ``0x4000`` in every gain slot.
SHADING_GAIN_UNITY = 0x4000
#: Gain clamps: 0.25x up to the 16-bit limit (the edge pixels of VueScan's
#: tables are ``0xFFFF``).
SHADING_GAIN_MIN = SHADING_GAIN_UNITY // 4
SHADING_GAIN_MAX = 0xFFFF
#: White level each bank's gains aim for: ``gain = target * 0x4000 / white``
#: reproduces both captured tables (0xF000 and 0x16000 to within 0.03 %).
SHADING_WHITE_TARGET_IR = 0xF000
SHADING_WHITE_TARGET_VISIBLE = 0x16000

#: Sensor lines in the dark and white strips (``LINCNT = 256`` in both
#: previews): 256 dark lines, or 128 IR + 128 visible lines in toggle mode.
SHADING_LINES = 256

#: AFE strip geometry: ``DPISET = 1200`` (one pixel per sensor unit in both
#: timing sets), ``LINCNT = 2`` (one IR + one visible line).
AFE_DPISET = 1200
AFE_STRIP_LINES = 2
AFE_PROBE_PIXELS = 512
#: Optical-black columns of an AFE strip (captured probes: columns 0-30 dark
#: on both lines, column 31 the edge, lit from 32). Column 0-1 read low.
AFE_OPTICAL_BLACK_COLUMNS = slice(2, 30)
#: First lit column of an AFE strip.
AFE_FIRST_LIT_COLUMN = 32
#: ``(0x2B, 0xA5, 0xAB)`` for the probe and the full-width strip (35 mm
#: preview). ``0xAB`` keeps the boot value on AFE strips.
AFE_PROBE_CLOCKS: tuple[int, int, int] = (0x01, 0x01, 0x30)
AFE_WIDE_CLOCKS: tuple[int, int, int] = (0x04, 0x01, 0x30)


def strip_line_period(pixels: int, base: int) -> int:
    """``LPERIOD`` of a calibration strip: ``base + pixels / 4`` (10935 / 12103 measured)."""
    return int(base) + int(pixels) // 4


def strip_bytes(pixels: int, lines: int) -> int:
    """Bulk bytes of a 16-bit RGB strip."""
    return int(pixels) * 6 * int(lines)


def split_toggle_lines(raw: bytes | bytearray | memoryview, *, line_bytes: int, pairs: int) -> tuple[np.ndarray, np.ndarray]:
    """Visible and IR halves of ``pairs`` toggle-mode line pairs.

    Every line pair is an IR line followed by a visible line of the same size.
    In VueScan's whole two-line AFE probes, at gain 0 line 0 is neutral
    (channel spread ~4 %) and line 1 carries the lamp's green cast (G/R ~1.25);
    VueScan then sizes its gains to balance line 1. The same gains give line 0
    G/R ~0.73 and line 1 ~1.0, so neutrality does not identify the IR line once
    gains are set. Returns two flat, C-contiguous ``uint8`` arrays of
    ``pairs * line_bytes`` bytes each.
    """
    need = 2 * int(line_bytes) * int(pairs)
    if len(raw) < need:
        raise ValueError(f"toggle buffer too short: {len(raw)} < {need}")
    lines = np.frombuffer(raw, dtype=np.uint8, count=need).reshape(int(pairs), 2, int(line_bytes))
    return np.ascontiguousarray(lines[:, 1]).reshape(-1), np.ascontiguousarray(lines[:, 0]).reshape(-1)


def strip_column_means(
    raw: bytes, *, pixels: int, lines: int
) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    """Per-column RGB means of the odd (visible) and even (IR) lines of a strip.

    The dark strip runs with the toggle bit off, so both halves are dark; they
    still fill one bank each, as in the captures.
    """
    pairs = int(lines) // 2
    visible, ir = split_toggle_lines(raw, line_bytes=int(pixels) * 6, pairs=pairs)
    return _column_means(visible, pixels=pixels, lines=pairs), _column_means(ir, pixels=pixels, lines=pairs)


def _column_means(data: np.ndarray, *, pixels: int, lines: int) -> list[tuple[int, int, int]]:
    """Per-column rounded RGB means of chunky RGB16 lines (as :func:`average_rgb16_columns`)."""
    words = data.view("<u2").reshape(int(lines), int(pixels), 3)
    means = np.rint(words.sum(axis=0, dtype=np.int64) * (1.0 / float(lines))).astype(np.int64)
    return [(int(r), int(g), int(b)) for r, g, b in means]


def _strip_array(strip: bytes, *, pixels: int, lines: int) -> np.ndarray:
    need = strip_bytes(pixels, lines)
    if len(strip) < need:
        raise ValueError(f"AFE strip too short: {len(strip)} < {need}")
    return np.frombuffer(strip, dtype="<u2", count=need // 2).reshape(int(lines), int(pixels), 3)


def optical_black_means(strip: bytes, *, pixels: int = AFE_PROBE_PIXELS) -> tuple[float, float, float]:
    """Mean R/G/B of the optical-black columns over both lines of an AFE strip."""
    arr = _strip_array(strip, pixels=pixels, lines=AFE_STRIP_LINES)[:, AFE_OPTICAL_BLACK_COLUMNS, :]
    means = arr.reshape(-1, 3).astype(np.float64).mean(axis=0)
    return (float(means[0]), float(means[1]), float(means[2]))


def lit_channel_means(strip: bytes, *, pixels: int) -> tuple[float, float, float]:
    """Per-channel mean of the lit columns, the brighter of the visible and IR line.

    One AFE gain serves both line types, and the IR line reads about a quarter
    brighter than the visible one in the captured probes, so the gain is sized
    for whichever would clip first.
    """
    arr = _strip_array(strip, pixels=pixels, lines=AFE_STRIP_LINES)[:, AFE_FIRST_LIT_COLUMN:, :]
    per_line = arr.astype(np.float64).mean(axis=1)
    best = per_line.max(axis=0)
    return (float(best[0]), float(best[1]), float(best[2]))


def shading_gain_for_white(white_unity: float, *, target: int) -> int:
    """Gain that lifts the unity-table white reading ``white_unity`` to ``target``."""
    gain = round(float(target) * SHADING_GAIN_UNITY / max(1.0, float(white_unity)))
    return max(SHADING_GAIN_MIN, min(SHADING_GAIN_MAX, gain))


def shading_gains_from_white(white: Sequence[Sequence[int]], *, target: int) -> list[tuple[int, int, int]]:
    """Per-pixel RGB gains for one bank's unity-table white strip."""
    out: list[tuple[int, int, int]] = []
    for row in white:
        g = [shading_gain_for_white(row[c], target=target) for c in range(3)]
        out.append((g[0], g[1], g[2]))
    return out


def unity_shading_table(dark: Sequence[Sequence[int]]) -> bytes:
    """First upload: per-pixel dark terms, unity gains (VueScan's first 135i upload)."""
    gains = [(SHADING_GAIN_UNITY,) * 3] * len(dark)
    return pack_shading_table(dark, gains, declared_size=declared_shading_size(len(dark)))


def measured_shading_table(dark: Sequence[Sequence[int]], gains: Sequence[Sequence[int]]) -> bytes:
    """Second upload: the same per-pixel dark terms with computed gains."""
    return pack_shading_table(dark, gains, declared_size=declared_shading_size(len(dark)))


def validate_gains(gains: Sequence[Sequence[int]]) -> tuple[bool, str]:
    """``(ok, reason)`` for one bank's computed gains, against the 135i unity."""
    flat = sorted(int(c) for row in gains for c in row)
    if not flat:
        return False, "empty gains"
    median = flat[len(flat) // 2] / SHADING_GAIN_UNITY
    if median < COLOR_SHADING_GAIN_MEDIAN_MIN:
        return False, f"median gain {median:.3f}x < {COLOR_SHADING_GAIN_MEDIAN_MIN}x (white already at target)"
    if median > COLOR_SHADING_GAIN_MEDIAN_MAX:
        return False, f"median gain {median:.3f}x > {COLOR_SHADING_GAIN_MEDIAN_MAX}x (white too dim to flatten)"
    clamped = sum(1 for v in flat if v <= SHADING_GAIN_MIN or v >= SHADING_GAIN_MAX)
    if clamped > len(flat) * COLOR_SHADING_GAIN_CLAMP_FRAC:
        return False, f"{clamped}/{len(flat)} gains hit a clamp ({SHADING_GAIN_MIN}..{SHADING_GAIN_MAX})"
    return True, f"ok (median gain {median:.3f}x)"


def validate_ir_white(white: Sequence[Sequence[int]]) -> tuple[bool, str]:
    """``(ok, reason)`` for the IR half of the white strip (lit, not railed)."""
    flat = [int(c) for row in white for c in row]
    if not flat:
        return False, "empty IR white"
    mean = sum(flat) / len(flat)
    if mean < IR_SHADING_WHITE_MEAN_MIN:
        return False, f"IR white mean {mean:.0f} < {IR_SHADING_WHITE_MEAN_MIN} (IR LED off?)"
    clipped = sum(1 for v in flat if v >= IR_SHADING_WHITE_CLIP_LEVEL)
    if clipped > len(flat) * IR_SHADING_WHITE_CLIP_FRAC:
        return False, f"IR white railed ({clipped}/{len(flat)} samples >= {IR_SHADING_WHITE_CLIP_LEVEL})"
    return True, f"ok (IR white mean {mean:.0f})"


def split_shading_blob(blob: bytes) -> tuple[bytes, bytes]:
    """IR and visible tables of a two-bank blob, in bank order (:meth:`Gl128Loader.run_asic_shading`)."""
    if not blob or len(blob) % 2:
        raise ValueError(f"two-bank shading blob must have an even, non-zero size (got {len(blob)})")
    half = len(blob) // 2
    if half > SHADING_BANK_BYTES:
        raise ValueError(f"shading table of {half} bytes overruns its {SHADING_BANK_BYTES}-byte bank")
    return blob[:half], blob[half:]
