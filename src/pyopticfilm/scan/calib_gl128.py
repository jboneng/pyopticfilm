# SPDX-License-Identifier: GPL-3.0-or-later
"""Capture-derived AFE search + ASIC shading table helpers for GL128 SE.

Step 1 (offline): SilverFast choreography + shading pack/unpack.
Step 2: dichotomy AFE search driven by strip means.
Step 3: build/upload ASIC shading at ``0x10014000`` (unity then measured).
Pure functions only — hardware I/O lives on :class:`Gl128`.

See ``captures/8200i-se/CALIB.md`` and ``decoded/afe_shading_fixture.json``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

#: Analog frontend register indices (GL124 ``0x51`` / ``0x5d`` / ``0x5e`` path).
FE_OFFSET_R = 0x02
FE_OFFSET_G = 0x03
FE_OFFSET_B = 0x04
FE_GAIN_R = 0x05
FE_GAIN_G = 0x06
FE_GAIN_B = 0x07

FE_OFFSET_INDICES: tuple[int, int, int] = (FE_OFFSET_R, FE_OFFSET_G, FE_OFFSET_B)
FE_GAIN_INDICES: tuple[int, int, int] = (FE_GAIN_R, FE_GAIN_G, FE_GAIN_B)

#: One AFE probe line: 512 px × 3 ch × 2 bytes (16-bit).
AFE_STRIP_PIXELS = 512
AFE_STRIP_BYTES = AFE_STRIP_PIXELS * 3 * 2  # 3072

#: Capture AFE window in native sensor units (session 03 NOTES).
AFE_STRPIXEL = 0x40  # 64
AFE_ENDPIXEL = 0x240  # 576 → 512 pixels

#: Wider mid-search read observed between offset and gain phases (session 03/04).
AFE_WIDE_BYTES = 62268
AFE_WIDE_PIXELS = AFE_WIDE_BYTES // 6  # 10378
#: Capture wide window: STRPIXEL=64, ENDPIXEL=10442 (span 10378).
AFE_WIDE_ENDPIXEL = AFE_STRPIXEL + AFE_WIDE_PIXELS

#: ASIC shading coefficient window.
AHB_SHADING = 0x10014000

#: Per-pixel record: dark_r, gain_r, dark_g, gain_g, dark_b, gain_b (u16 LE).
SHADING_RECORD_BYTES = 12
#: One ``(dark, gain)`` coefficient pair.
SHADING_PAIR_BYTES = 4

#: The blob is written in 512-byte AHB blocks. DVDSET indexes only the first 126
#: pairs of each block; the last two pairs are padding the vendor fills with
#: ``gain = 0``. Packing records contiguously slides the map by 8 bytes per block
#: and DVDSET paints a ~42 px barcode (sessions 03/04 both show this layout).
SHADING_BLOCK_BYTES = 512
SHADING_BLOCK_PAIRS = SHADING_BLOCK_BYTES // SHADING_PAIR_BYTES
SHADING_BLOCK_DATA_PAIRS = 126
SHADING_BLOCK_PAD_PAIRS = SHADING_BLOCK_PAIRS - SHADING_BLOCK_DATA_PAIRS

#: The second u16 of each pair is a gain in fixed point: ``0x2000`` = 1.0
#: (``captures/8200i-se/03_home_or_preview/NOTES.md``), not a white level.
SHADING_GAIN_UNITY = 0x2000
#: White level the gain maps the measured white to. Session 04 fits
#: ``coeff * white_unity / 0x2000 = 0xFFFF`` on R to ~0.1%; SF tilts G/B up by
#: 1-3% for its own white balance, which NegPy does later in the pipeline.
SHADING_GAIN_TARGET = 0xFFFF
#: Hard clamps (0.25x..4.0x). Sessions 03/04 gains land 1.13x-1.60x.
SHADING_GAIN_MIN = SHADING_GAIN_UNITY // 4
SHADING_GAIN_MAX = SHADING_GAIN_UNITY * 4

#: Capture shading acquires use 128 lines × window_width × RGB16 (sessions 03/04).
SHADING_LINES = 128

#: Shading table width in pixels by scan DPI, for passes that supply no image
#: window. Session 03 (1200 → 1728) and 04 (1800 → 2478) are capture-proven: the
#: vendor table width equals the strip width it measured. Other rows are scaled
#: from the declared AHB sizes of session ``13_ppi_ladder`` and are approximate —
#: real scans pass their own ``strpixel``/``endpixel``.
SHADING_WIDTH_BY_DPI: dict[int, int] = {
    150: 852,
    300: 852,
    600: 852,
    720: 1021,
    900: 1277,
    1200: 1728,  # session 03, declared 21064
    1440: 2042,
    1800: 2478,  # session 04, declared 30208
    2400: 3406,
    3600: 4956,  # session 06, declared 60416
    7200: 10223,
}
#: Dichotomy defaults (16-bit strip means). White target sits near the mid-phase
#: means seen after offsets settle (~0xCC00–0xD800 at gain 0xFF) *when the head
#: is on a bright calib patch*. At home / underexposed, that target is unreachable
#: and the search pegs ``gain_max`` — use :func:`adaptive_afe_gain_target`.
AFE_OFFSET_TARGET = 0x1000
AFE_GAIN_TARGET = 0xD000
AFE_OFFSET_MAX = 0xFF
AFE_GAIN_MAX = 0x1FF
AFE_DICHOTOMY_ITERS = 9
#: Colour gain search floor — SF sessions 03/04 settle ~18–31; never walk to 0.
AFE_GAIN_MIN = 0x10
#: Floor/ceiling when adapting the gain target from a mid-gain probe.
#: Floor is soft — :func:`adaptive_afe_gain_target` may return the probe peak
#: itself when that is below this (see implementation).
AFE_GAIN_TARGET_MIN = 0x0800
AFE_GAIN_TARGET_MAX = AFE_GAIN_TARGET
#: Aim here when the ``0x80`` probe is already hot. Empirical: this lands gains
#: near SF's own codes (0x14, 0x1F, 0x17) and shading whites ~50k. It is *not*
#: comparable to SF's own probe mean (~49.9k in sessions 03/04) — NegPy reads a
#: different window and exposure, and takes planar means. Raising it to match SF
#: pegged R at ``AFE_GAIN_MAX`` and clipped the R dark term to 0.
AFE_GAIN_TARGET_SF_WHITE = 0x3000
#: Session-04 colour gain fallback when search collapses to zero.
COLOR_AFE_SESSION04_GAINS: tuple[int, int, int] = (0x14, 0x1F, 0x17)
#: Best-fit divisor for capture coarse offsets: ``round((65535 - mean) / D)``.
#: Fits sessions 03/04 within ±2 counts (CALIB.md); not a proven SF identity.
AFE_OFFSET_COARSE_DIVISOR = 1155


def _u16_plane(strip: bytes, *, pixels: int) -> list[int]:
    need = pixels * 2
    if len(strip) < need:
        raise ValueError(f"strip too short for plane: {len(strip)} < {need}")
    return [int.from_bytes(strip[i : i + 2], "little") for i in range(0, need, 2)]


def _lag_corr(samples: Sequence[int], lag: int) -> float:
    if lag <= 0 or len(samples) <= lag:
        return 0.0
    a = [float(v) for v in samples[:-lag]]
    b = [float(v) for v in samples[lag:]]
    n = len(a)
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    da = sum((x - ma) ** 2 for x in a)
    db = sum((y - mb) ** 2 for y in b)
    denom = (da * db) ** 0.5
    if denom <= 1e-9:
        return 0.0
    return num / denom


def choose_usb_planar(strip: bytes, *, pixels: int = AFE_STRIP_PIXELS) -> bool:
    """True if ``strip`` looks planar (``RRR…``) rather than chunky ``RGBRGB…``.

    Chunky data in a flat field has strong lag-3 correlation (same channel) and
    weaker lag-1. Planar RRR… correlates at lag 1 and lag 3 similarly.
    Summing channel means cannot decide — every byte is counted once either way.
    """
    need = pixels * 6
    if len(strip) < need:
        return True
    plane = _u16_plane(strip, pixels=pixels)
    c1 = _lag_corr(plane, 1)
    c3 = _lag_corr(plane, 3)
    # Prefer chunky when lag-3 clearly dominates lag-1.
    return not (c3 > c1 + 0.08)


def adaptive_afe_gain_target(probe_means: Sequence[float]) -> float:
    """Pick a reachable white target from a mid-gain (0x80) probe.

    SF sessions 03/04 settle gains *down* from ``0x80`` toward ~20. If the mid-gain
    probe is already at/above ``AFE_GAIN_TARGET_SF_WHITE``, aim there so dichotomy
    lowers gains. If the probe is dimmer (dark home), aim ~15% above the peak so
    search can raise gains.
    """
    peak = max(float(m) for m in probe_means) if probe_means else 0.0
    if peak <= 0:
        return float(AFE_GAIN_TARGET_MIN)
    if peak >= float(AFE_GAIN_TARGET_SF_WHITE):
        return float(AFE_GAIN_TARGET_SF_WHITE)
    guessed = max(peak * 1.15, peak + 256.0)
    return float(max(AFE_GAIN_TARGET_MIN, min(AFE_GAIN_TARGET_MAX, int(guessed))))


def coarse_offsets_from_wide_means(
    means: Sequence[float],
    *,
    divisor: int = AFE_OFFSET_COARSE_DIVISOR,
    offset_max: int = AFE_OFFSET_MAX,
) -> tuple[int, int, int]:
    """Capture coarse offset seed: ``round((65535 - mean) / divisor)``.

    Matches sessions 03/04 within a couple of counts (CALIB.md). Seed offset
    dichotomy from this rather than ``(0,0,0)``.
    """
    if len(means) != 3:
        raise ValueError("means must be length-3 RGB")
    out: list[int] = []
    for mean in means:
        code = round((65535.0 - float(mean)) / float(divisor))
        out.append(max(0, min(int(offset_max), code)))
    return (out[0], out[1], out[2])


def rgb_layout_score(rgb) -> float:
    """Higher is better for photo-like structure vs 1-pixel columnar rainbow.

    Adjacent-column luminance correlation: real scene content correlates;
    independent columnar noise does not.
    """
    import numpy as np

    arr = np.asarray(rgb, dtype=np.float32)
    if arr.ndim != 3 or arr.shape[0] < 1 or arr.shape[1] < 2:
        return 0.0
    luma = arr.mean(axis=2)
    a = luma[:, :-1].ravel()
    b = luma[:, 1:].ravel()
    a = a - a.mean()
    b = b - b.mean()
    denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
    if denom <= 1e-6:
        return 0.0
    return float((a * b).sum() / denom)


@dataclass(frozen=True)
class AfeFrontend:
    """Settled FE offset/gain words (16-bit) for R,G,B."""

    offsets: tuple[int, int, int]
    gains: tuple[int, int, int]

    def as_fe_writes(self) -> list[tuple[int, int]]:
        """Return ``(fe_index, value)`` pairs in capture-friendly order."""
        out: list[tuple[int, int]] = []
        for idx, val in zip(FE_OFFSET_INDICES, self.offsets, strict=True):
            out.append((idx, int(val) & 0xFFFF))
        for idx, val in zip(FE_GAIN_INDICES, self.gains, strict=True):
            out.append((idx, int(val) & 0xFFFF))
        return out


@dataclass(frozen=True)
class AfeSearchConfig:
    """Parameters for the runtime dichotomy AFE search."""

    offset_target: int = AFE_OFFSET_TARGET
    gain_target: int = AFE_GAIN_TARGET
    offset_max: int = AFE_OFFSET_MAX
    gain_max: int = AFE_GAIN_MAX
    iterations: int = AFE_DICHOTOMY_ITERS
    #: On this FE, raising the offset code *raises* strip means (sessions 03/04).
    offset_increases_mean: bool = True
    gain_increases_mean: bool = True
    tolerance: float = 512.0


@dataclass(frozen=True)
class AfeSearchPhase:
    """One observed FE programming step from the capture timeline."""

    name: str
    offsets: tuple[int, int, int] | None = None
    gains: tuple[int, int, int] | None = None
    strip_bytes: int | None = None
    note: str = ""


#: Capture-derived phase list (session 03). Numeric FE updates are unit-specific;
#: runtime search uses :func:`run_afe_dichotomy` rather than replaying these
#: constants.
AFE_SEARCH_PHASES_CAPTURE: tuple[AfeSearchPhase, ...] = (
    AfeSearchPhase("init_zeros", offsets=(0, 0, 0), gains=(0, 0, 0)),
    AfeSearchPhase(
        "gain_probe_0x80",
        offsets=(0, 0, 0),
        gains=(0x80, 0x80, 0x80),
        strip_bytes=AFE_STRIP_BYTES,
        note="first 3072-byte strip",
    ),
    AfeSearchPhase(
        "gain_probe_0xff",
        gains=(0xFF, 0xFF, 0xFF),
        strip_bytes=AFE_STRIP_BYTES,
    ),
    AfeSearchPhase(
        "gain_high_estimate",
        gains=(0x016A, 0x0142, 0x015B),
        note="session-03 example; values are unit-specific",
    ),
    AfeSearchPhase(
        "wide_offset_strip",
        offsets=(0, 0, 0),
        strip_bytes=AFE_WIDE_BYTES,
        note="62268-byte read before coarse offsets",
    ),
    AfeSearchPhase(
        "offset_coarse",
        offsets=(0x24, 0x1A, 0x21),
        note="session-03 example coarse offsets",
    ),
    AfeSearchPhase(
        "gain_reprobe_0x80",
        gains=(0x80, 0x80, 0x80),
        strip_bytes=AFE_STRIP_BYTES,
    ),
    AfeSearchPhase(
        "gain_reprobe_0xff",
        gains=(0xFF, 0xFF, 0xFF),
        strip_bytes=AFE_STRIP_BYTES,
    ),
    AfeSearchPhase("gain_mid_high", gains=(0x115, 0x113, 0x113)),
    AfeSearchPhase("gain_low", gains=(0x0C, 0x16, 0x10)),
    AfeSearchPhase(
        "shading_upload_unity_white",
        note="AHB 0x10014000 with white=0x2000; dark from dark strip",
    ),
    AfeSearchPhase("gain_settle", gains=(0x12, 0x1E, 0x17)),
    AfeSearchPhase(
        "shading_upload_measured_white",
        note="AHB 0x10014000 with per-pixel white terms",
    ),
    AfeSearchPhase("offset_fine", offsets=(0x26, 0x1E, 0x25)),
)


def shading_pair_count(entries: int) -> int:
    """Total ``(dark, gain)`` pairs for ``entries`` pixels, block padding included."""
    data = max(0, int(entries)) * 3
    return data + SHADING_BLOCK_PAD_PAIRS * (data // SHADING_BLOCK_DATA_PAIRS)


def declared_shading_size(entries: int) -> int:
    """AHB declared size for ``entries`` pixels of shading coefficients.

    Session 04 (2478 px) declares 30208 bytes and session 03 (1728 px) 21064 —
    both are ``4 × pairs`` with two pad pairs per full 512-byte block.
    """
    return SHADING_PAIR_BYTES * shading_pair_count(entries)


def shading_entry_count(declared_size: int) -> int:
    """Pixels covered by a declared AHB shading upload size (inverse of above)."""
    size = int(declared_size)
    if size < 0 or size % SHADING_PAIR_BYTES:
        raise ValueError(f"Shading declared_size={size} is not a whole number of 4-byte pairs")
    estimate = size * SHADING_BLOCK_DATA_PAIRS // (SHADING_PAIR_BYTES * 3 * SHADING_BLOCK_PAIRS)
    for candidate in range(max(0, estimate - 2), estimate + 3):
        if declared_shading_size(candidate) == size:
            return candidate
    raise ValueError(f"Shading declared_size={size} does not match any pixel count")


def shading_width_for_resolution(resolution: int) -> int:
    """Declared AHB shading table width for ``resolution`` dpi.

    Capture-proven across session ``13_ppi_ladder`` (and 03/04/06 for key PPIs).
    PPI below 600 share the 600 dpi table width. This is the *upload* width;
    the acquire width is the image window (see :func:`shading_acquire_width`).
    """
    dpi = int(resolution)
    if dpi in SHADING_WIDTH_BY_DPI:
        return SHADING_WIDTH_BY_DPI[dpi]
    if dpi < 600:
        return SHADING_WIDTH_BY_DPI[600]
    return max(1, round(SHADING_WIDTH_BY_DPI[1800] * dpi / 1800))


def shading_acquire_width(
    *,
    strpixel: int,
    endpixel: int,
    dpiset: int,
    optical_resolution: int = 7200,
) -> int:
    """USB pixels per shading line for an image window (sessions 04/05).

    ``bytes = LINCNT × width × 6`` with ``LINCNT=128`` and
    ``width = (ENDPIXEL−STRPIXEL) / (optical_resolution / (DPISET×6))``.
    At 1800 dpi that is ``9912 / 4 = 2478``, which is also the table width.
    """
    span = int(endpixel) - int(strpixel)
    if span <= 0:
        raise ValueError(f"shading window empty: {strpixel}..{endpixel}")
    dpi = max(1, int(dpiset) * 6)
    factor = max(1, int(optical_resolution) // dpi)
    return max(1, span // factor)


def shading_native_factor(*, dpiset: int, optical_resolution: int = 7200) -> int:
    """Native-dpi samples per USB pixel for ``DPISET`` (session 04: 4 at 1800)."""
    dpi = max(1, int(dpiset) * 6)
    return max(1, int(optical_resolution) // dpi)


#: Session 05 IR measured whites: spread 149. Gate allows some home-film slack.
IR_SHADING_WHITE_SPREAD_MAX = 500
#: Dark terms on IR are ≈0 in session 05; allow a small floor for noise.
IR_SHADING_DARK_MAX = 64
#: Reject a dim white field (session 05 whites ~13000; below this DVDSET clips).
IR_SHADING_WHITE_MEAN_MIN = 10000
#: Reject a clipped-flat white field (almost all samples near rail).
IR_SHADING_WHITE_CLIP_LEVEL = 60000
IR_SHADING_WHITE_CLIP_FRAC = 0.90
#: Period of the old mis-acquire dropout pattern (SHADING_LINES bug era).
IR_SHADING_DROPOUT_PERIOD = 126
IR_SHADING_DROPOUT_MIN_RUNS = 4

#: Colour lamp-off dark must stay near session-04 black (~1k), not mid-scale film.
COLOR_SHADING_DARK_MEAN_MAX = 3000
#: Post-unity DVDSET white floor. Sessions 03/04 read ~50–57k here (the ASIC
#: returns ``raw - dark`` at unity gain). Below this the gain needed to reach
#: :data:`SHADING_GAIN_TARGET` runs into :data:`SHADING_GAIN_MAX`.
COLOR_SHADING_WHITE_MEAN_MIN = 20000
#: A white strip pinned at the rail carries no shape to flatten.
COLOR_SHADING_WHITE_CLIP_LEVEL = 65000
COLOR_SHADING_WHITE_CLIP_FRAC = 0.05
#: Per-channel white mean floor — one starved channel makes DVDSET clip that
#: channel to the rail across the frame.
COLOR_SHADING_CHANNEL_WHITE_MIN = 8000
#: Gain band. Sessions 03/04 medians sit 1.12x–1.61x; outside this the strip was
#: not a clear illuminated field and DVDSET would clip or barely act.
COLOR_SHADING_GAIN_MEDIAN_MIN = 1.02
COLOR_SHADING_GAIN_MEDIAN_MAX = 3.0
#: Fraction of gains allowed to sit on a hard clamp.
COLOR_SHADING_GAIN_CLAMP_FRAC = 0.02
#: Orange-mask heuristic: R mean below this fraction of min(G, B) at home.
COLOR_SHADING_FILM_R_FRAC = 0.55
COLOR_SHADING_FILM_GB_MIN = 12000
#: SF colour off→dark gap ≈0.52s (session 03); IR dark settle (table dark forced to 0).
COLOR_SHADING_DARK_SETTLE_S = 0.5
IR_SHADING_DARK_SETTLE_S = 0.5
#: Planar/chunky flip only when alternate mean improves by at least this.
COLOR_SHADING_LAYOUT_MEAN_IMPROVE_MIN = 5000.0


def shading_columns_mean(cols: Sequence[Sequence[int]]) -> float:
    """Mean of all channel samples across columns."""
    if not cols:
        return 0.0
    return sum(int(c) for row in cols for c in row) / max(1, len(cols) * 3)


def pick_shading_dark_layout(
    primary: Sequence[Sequence[int]],
    alternate: Sequence[Sequence[int]],
    *,
    primary_is_planar: bool,
    dark_mean_max: float = COLOR_SHADING_DARK_MEAN_MAX,
    mean_improve_min: float = COLOR_SHADING_LAYOUT_MEAN_IMPROVE_MIN,
) -> tuple[list[tuple[int, int, int]], bool]:
    """Choose dark-column USB layout for shading averages.

    Flip only when the alternate mean is strictly lower and either falls under
    ``dark_mean_max`` or improves by at least ``mean_improve_min``. Equal means
    (balance-only) keep the primary layout — total mean is layout-invariant.
    """
    mean_a = shading_columns_mean(primary)
    mean_b = shading_columns_mean(alternate)
    use_alt = mean_b < mean_a and (mean_b <= dark_mean_max or (mean_a - mean_b) >= mean_improve_min)
    chosen = alternate if use_alt else primary
    planar = (not primary_is_planar) if use_alt else primary_is_planar
    return [(int(row[0]), int(row[1]), int(row[2])) for row in chosen], planar


def color_shading_looks_like_film(white: Sequence[Sequence[int]]) -> bool:
    """True when a home white strip looks like colour-neg orange mask, not clear TA."""
    if not white:
        return False
    wr = sum(int(row[0]) for row in white) / len(white)
    wg = sum(int(row[1]) for row in white) / len(white)
    wb = sum(int(row[2]) for row in white) / len(white)
    gb = min(wg, wb)
    return gb >= COLOR_SHADING_FILM_GB_MIN and wr < gb * COLOR_SHADING_FILM_R_FRAC


def validate_color_shading_strip(
    dark: Sequence[Sequence[int]],
    white: Sequence[Sequence[int]],
    *,
    acquire_width: int | None = None,
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for the raw strips a colour shading table is built from.

    ``white`` is the post-unity DVDSET strip, so the ASIC has already removed the
    dark term and sessions 03/04 read ~50–57k. This checks the light path only —
    clear field, not at the rail, no starved channel, no periodic dropouts. The
    coefficients themselves are checked by :func:`validate_color_shading_gains`.
    """
    if not dark or not white:
        return False, "empty dark/white"
    if len(dark) != len(white):
        return False, f"dark/white length mismatch {len(dark)}!={len(white)}"
    if acquire_width is not None and acquire_width > 0:
        n = min(int(acquire_width), len(white))
        if n < 8:
            return False, f"acquire_width too small ({n})"
        dark = dark[:n]
        white = white[:n]

    dark_mean = shading_columns_mean(dark)
    white_mean = shading_columns_mean(white)
    if white_mean < COLOR_SHADING_WHITE_MEAN_MIN:
        return False, f"white mean {white_mean:.0f} < {COLOR_SHADING_WHITE_MEAN_MIN}"
    samples = [int(c) for row in white for c in row]
    clipped = sum(1 for v in samples if v >= COLOR_SHADING_WHITE_CLIP_LEVEL)
    if clipped > len(samples) * COLOR_SHADING_WHITE_CLIP_FRAC:
        return (
            False,
            f"white clipped at the rail ({clipped}/{len(samples)} ≥ {COLOR_SHADING_WHITE_CLIP_LEVEL})",
        )
    if color_shading_looks_like_film(white):
        return False, "white strip looks like film (not clear home field)"

    for ch in range(3):
        w = sum(int(row[ch]) for row in white) / len(white)
        if w < COLOR_SHADING_CHANNEL_WHITE_MIN:
            return False, f"ch{ch} white {w:.0f} < {COLOR_SHADING_CHANNEL_WHITE_MIN}"

    # Period≈126 zero/dropout columns — SHADING_LINES-era barcode (IR gate reused).
    zero_cols = [i for i, row in enumerate(white) if min(int(c) for c in row) < COLOR_SHADING_WHITE_MEAN_MIN // 4]
    if len(zero_cols) >= IR_SHADING_DROPOUT_MIN_RUNS and _has_periodic_dropouts(zero_cols, IR_SHADING_DROPOUT_PERIOD):
        return (
            False,
            f"periodic white dropouts (n={len(zero_cols)}, period≈{IR_SHADING_DROPOUT_PERIOD})",
        )

    if dark_mean > COLOR_SHADING_DARK_MEAN_MAX:
        return True, f"ok (soft dark mean {dark_mean:.0f}, white {white_mean:.0f})"
    return True, "ok"


def validate_color_shading_gains(
    gains: Sequence[Sequence[int]],
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for computed DVDSET gains before arming them."""
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


def validate_ir_shading_table(
    dark: Sequence[Sequence[int]],
    white: Sequence[Sequence[int]],
    *,
    acquire_width: int | None = None,
    raw_white: Sequence[Sequence[int]] | None = None,
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a just-built IR shading table.

    Accepts session-05 shape (dark≈0, near-equal useful whites). Rejects the
    old period-126 zero-column failure mode, extreme white imbalance, and
    dim/clipped white fields so DVDSET cannot silently punch bars.

    Pass ``raw_white`` (pre-equalize strip) so bar/period checks still see
    channel dropouts that ``equalize_ir_white_columns`` would otherwise hide.
    """
    if not dark or not white:
        return False, "empty dark/white"
    if len(dark) != len(white):
        return False, f"dark/white length mismatch {len(dark)}!={len(white)}"
    if acquire_width is not None and acquire_width > 0:
        # Compare the measured (unpadded) prefix when table was padded.
        n = min(int(acquire_width), len(white))
        if n < 8:
            return False, f"acquire_width too small ({n})"
        dark = dark[:n]
        white = white[:n]
        if raw_white is not None:
            raw_white = raw_white[:n]

    dark_peak = max(max(int(c) for c in row) for row in dark)
    if dark_peak > IR_SHADING_DARK_MAX:
        return False, f"dark peak {dark_peak} > {IR_SHADING_DARK_MAX}"

    # First-column and global white spread (session 05 head spread was 149).
    w0 = [int(c) for c in white[0]]
    head_spread = max(w0) - min(w0)
    if head_spread > IR_SHADING_WHITE_SPREAD_MAX:
        return False, f"white head spread {head_spread} > {IR_SHADING_WHITE_SPREAD_MAX}"

    whites = [int(c) for row in white for c in row]
    mean_white = sum(whites) / len(whites)
    if mean_white < IR_SHADING_WHITE_MEAN_MIN:
        return (
            False,
            f"white mean {mean_white:.0f} < {IR_SHADING_WHITE_MEAN_MIN}",
        )
    clipped = sum(1 for v in whites if v > IR_SHADING_WHITE_CLIP_LEVEL)
    if clipped >= len(whites) * IR_SHADING_WHITE_CLIP_FRAC:
        return (
            False,
            f"white clipped ({clipped}/{len(whites)} > {IR_SHADING_WHITE_CLIP_LEVEL})",
        )

    bar_white = raw_white if raw_white is not None else white
    bar_vals = [int(c) for row in bar_white for c in row]
    if bar_vals and min(bar_vals) <= 0:
        # Count zero-valued green (or any) channel columns — bar signature.
        zero_cols = [i for i, row in enumerate(bar_white) if any(int(c) <= 0 for c in row)]
        if len(zero_cols) >= IR_SHADING_DROPOUT_MIN_RUNS and _has_periodic_dropouts(zero_cols, IR_SHADING_DROPOUT_PERIOD):
            return (
                False,
                f"periodic zero whites (n={len(zero_cols)}, period≈{IR_SHADING_DROPOUT_PERIOD})",
            )
        if min(bar_vals) <= 0 and len(zero_cols) > max(8, len(bar_white) // 20):
            return False, f"too many zero white columns ({len(zero_cols)})"

    global_spread = max(whites) - min(whites)
    # Allow larger global spread than head (illumination falloff is OK);
    # reject only pathological ranges that look like mis-parsed planes.
    if global_spread > 40000 and min(whites) < 256:
        return False, f"pathological white range spread={global_spread}"

    return True, "ok"


def _has_periodic_dropouts(zero_cols: Sequence[int], period: int) -> bool:
    """True when zero-column starts cluster on a repeating period."""
    if len(zero_cols) < IR_SHADING_DROPOUT_MIN_RUNS:
        return False
    # Collapse runs to start indices.
    starts = [zero_cols[0]]
    for i in zero_cols[1:]:
        if i != starts[-1] + 1 and i != starts[-1]:
            starts.append(i)
    if len(starts) < IR_SHADING_DROPOUT_MIN_RUNS:
        return False
    diffs = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
    near = sum(1 for d in diffs if abs(d - period) <= 2 or abs(d - period // 2) <= 2)
    return near >= max(3, len(diffs) // 2)


def equalize_ir_white_columns(
    white: Sequence[Sequence[int]],
) -> list[tuple[int, int, int]]:
    """Broadcast per-column mean R/G/B to ``(m, m, m)`` for IR shading.

    Session 05: under a shared IR illuminant the three CCD taps see the same
    light, so measured whites are nearly equal. Live strips often keep a large
    channel spread (film dyes / AFE); equalizing preserves the spatial profile
    while matching the capture table shape so DVDSET can arm safely.
    """
    out: list[tuple[int, int, int]] = []
    for row in white:
        vals = [int(c) for c in row]
        if not vals:
            out.append((0, 0, 0))
            continue
        mean = round(sum(vals) / len(vals))
        mean = max(0, min(65535, mean))
        out.append((mean, mean, mean))
    return out


def _box_smooth_profile(profile, half_window: int):
    """Edge-padded box filter; ``half_window<=0`` returns a copy."""
    import numpy as np

    w = np.asarray(profile, dtype=np.float32)
    if half_window <= 0 or w.size == 0:
        return w.copy()
    k = 2 * int(half_window) + 1
    ext = np.pad(w, int(half_window), mode="edge")
    c = np.concatenate([[0.0], np.cumsum(ext, dtype=np.float64)])
    return ((c[k:] - c[:-k]) / k).astype(np.float32)


def flatten_ir_columns(
    ir,
    white: Sequence[int],
    *,
    target: float | None = None,
    smooth_half: int | None = None,
):
    """Host IR flatten: ``out[x] = ir[x] * target / white[x]`` (per column).

    ``ir`` is HxW uint16. ``white`` is a per-column profile (length ≥ W).
    Default ``target`` is the mean of the *smoothed* ``white[:W]`` so average
    level is kept. Live ASIC DVDSET clipped IR to full scale; this is the Lab
    substitute.

    Raw per-column divide imprinted barcode banding on HW (period-4 / strip
    FPN). By default the white profile is low-pass filtered so only slow
    illumination falloff is corrected. Pass ``smooth_half=0`` for the raw
    column profile (tests / experiments).
    """
    import numpy as np

    arr = np.asarray(ir, dtype=np.float32)
    if arr.ndim != 2:
        raise ValueError(f"expected HxW infrared plane, got {arr.shape}")
    width = int(arr.shape[1])
    if len(white) < width:
        raise ValueError(f"white profile length {len(white)} < image width {width}")
    w_raw = np.asarray([max(1, int(v)) for v in white[:width]], dtype=np.float32)
    # ~2.5% of width each side; skip on tiny synthetic profiles.
    half = (0 if width < 64 else max(8, width // 40)) if smooth_half is None else int(smooth_half)
    w = _box_smooth_profile(w_raw, half)
    w = np.maximum(w, 1.0)
    tgt = float(np.mean(w)) if target is None else float(target)
    if tgt <= 0:
        tgt = 1.0
    out = arr * (tgt / w[np.newaxis, :])
    return np.clip(np.rint(out), 0, 65535).astype(np.uint16)


#: SilverFast 9 IR page median ballpark (see captures/8200i-se/Silverfast_ir_tiff/).
IR_SIDECAR_TARGET_LEVEL = 56000.0
#: Exclude this fraction of width at each edge when estimating flatten levels.
IR_FLATTEN_BORDER_INSET = 0.04
#: Cap per-column gain so garbage/dark edge columns cannot blow up to ~56k.
IR_FLATTEN_MAX_GAIN = 4.0


def flatten_ir_image_columns(
    ir,
    *,
    percentile: float = 90.0,
    target: float | None = IR_SIDECAR_TARGET_LEVEL,
    smooth_half: int | None = None,
    border_inset: float = IR_FLATTEN_BORDER_INSET,
    max_gain: float = IR_FLATTEN_MAX_GAIN,
):
    """Per-image IR flatten for a SilverFast-like iSRD sidecar.

    Uses a robust bright level per column (``percentile``) so dark dust does
    not pull the gain up, then low-pass-smooths that profile and rescales so
    ``out[x] = ir[x] * target / level[x]``. Cancels residual L/R falloff after
    the stationary white-strip flatten without high-pass masking film structure.

    Edge columns are scored from the nearest interior levels (inset
    ``border_inset``) so decode padding / holder samples cannot dominate the
    gain. ``max_gain`` caps amplification of near-zero columns.

    Default ``target`` matches the SilverFast IR page bright field (~56k).
    Pass ``target=None`` to keep the mean of the smoothed column levels.
    """
    import numpy as np

    arr = np.asarray(ir, dtype=np.float32)
    if arr.ndim != 2:
        raise ValueError(f"expected HxW infrared plane, got {arr.shape}")
    height, width = int(arr.shape[0]), int(arr.shape[1])
    if height < 1 or width < 1:
        raise ValueError(f"empty infrared plane: {arr.shape}")
    # Robust bright level ignores dark defects / holder edges in the column.
    levels = np.percentile(arr, float(percentile), axis=0).astype(np.float32)
    inset = min(max(float(border_inset), 0.0), 0.2)
    cut = max(0, round(width * inset)) if width >= 16 else 0
    if cut * 2 < width:
        # Replace edge column stats with the nearest interior sample before smooth.
        levels[:cut] = levels[cut]
        levels[width - cut :] = levels[width - cut - 1]
    half = (0 if width < 64 else max(8, width // 40)) if smooth_half is None else int(smooth_half)
    levels = _box_smooth_profile(levels, half)
    levels = np.maximum(levels, 1.0)
    if cut * 2 < width:
        interior = levels[cut : width - cut]
        tgt_base = float(np.mean(interior)) if target is None else float(target)
    else:
        tgt_base = float(np.mean(levels)) if target is None else float(target)
    if tgt_base <= 0:
        tgt_base = 1.0
    gain = tgt_base / levels
    cap = float(max_gain)
    if cap > 0:
        gain = np.minimum(gain, cap)
    out = arr * gain[np.newaxis, :]
    return np.clip(np.rint(out), 0, 65535).astype(np.uint16)


def _box_mean_2d(arr, half_y: int, half_x: int):
    """Edge-padded separable box mean via integral image."""
    import numpy as np

    a = np.asarray(arr, dtype=np.float64)
    hy = int(half_y)
    hx = int(half_x)
    if hy < 0 or hx < 0:
        raise ValueError("half_y/half_x must be >= 0")
    if hy == 0 and hx == 0:
        return a.astype(np.float32)
    padded = np.pad(a, ((hy, hy), (hx, hx)), mode="edge")
    integral = np.zeros((padded.shape[0] + 1, padded.shape[1] + 1), dtype=np.float64)
    integral[1:, 1:] = padded.cumsum(0).cumsum(1)
    height, width = a.shape
    ky = 2 * hy + 1
    kx = 2 * hx + 1
    y = np.arange(height)[:, None]
    x = np.arange(width)[None, :]
    total = integral[y + ky, x + kx] - integral[y, x + kx] - integral[y + ky, x] + integral[y, x]
    return (total / float(ky * kx)).astype(np.float32)


#: Default strength for :func:`mild_ir_ghost_fade` (Lab IR preview).
MILD_IR_GHOST_FADE_STRENGTH = 0.35


def mild_ir_ghost_fade(
    rgb_or_plane,
    *,
    strength: float = MILD_IR_GHOST_FADE_STRENGTH,
):
    """Gently fade dye/ghost structure and deepen sharp dark defects.

    Softer than :func:`enhance_ir_defect_contrast` (no floor-punch to ~2000).
    Intended for Lab IR preview and optional app use; scan TIFF stays raw.

    ``rgb_or_plane`` is HxW or HxWx3 uint16. HxWx3 uses the green plane, then
    broadcasts the faded result to all channels for RGB preview helpers.
    """
    import numpy as np

    arr = np.asarray(rgb_or_plane)
    if arr.ndim == 3:
        if arr.shape[2] < 3:
            raise ValueError(f"expected HxWx3, got {arr.shape}")
        plane = arr[:, :, 1].astype(np.float32)
        as_rgb = True
    elif arr.ndim == 2:
        plane = arr.astype(np.float32)
        as_rgb = False
    else:
        raise ValueError(f"expected HxW or HxWx3, got {arr.shape}")

    height, width = int(plane.shape[0]), int(plane.shape[1])
    if height < 8 or width < 8:
        return np.asarray(rgb_or_plane).copy()

    s = float(np.clip(strength, 0.0, 1.0))
    # Wide low-pass ≈ dye/ghost; keep most of it so the look stays photographic.
    film_hy = max(12, min(90, height // 12))
    film_hx = max(16, min(140, width // 10))
    low = _box_mean_2d(plane, film_hy, film_hx)
    residual = plane - low

    # Compress slow ghost toward the local field; mildly deepen dark residuals.
    film_retain = 1.0 - 0.55 * s  # at 0.35 → ~0.81 of low-frequency contrast
    base = float(np.mean(low)) + (low - float(np.mean(low))) * film_retain
    dark = np.minimum(residual, 0.0)
    bright = np.maximum(residual, 0.0)
    dark_boost = 1.0 + 1.4 * s  # deepen defects without floor-punch
    bright_keep = 1.0 - 0.35 * s  # slightly tame bright HF noise
    out_plane = base + dark * dark_boost + bright * bright_keep
    out_plane = np.clip(np.rint(out_plane), 0, 65535).astype(np.uint16)

    if not as_rgb:
        return out_plane
    out = np.empty_like(arr, dtype=np.uint16)
    out[:, :, 0] = out_plane
    out[:, :, 1] = out_plane
    out[:, :, 2] = out_plane
    if arr.shape[2] > 3:
        out[:, :, 3:] = arr[:, :, 3:]
    return out


#: SilverFast iSRD Detection scale (0 = selective, 20 = most sensitive).
IR_DETECTION_MIN = 0
IR_DETECTION_MAX = 20
#: Legacy enhance percentiles (too hot on ghost) ≡ Detection 20.
_IR_DET20_LO = 96.0
_IR_DET20_HI = 99.25
#: Detection 0 (strictest) percentiles.
_IR_DET0_LO = 99.0
_IR_DET0_HI = 99.85


def detection_to_enhance_params(detection: int) -> dict[str, float]:
    """Map SilverFast-like Detection ``0..20`` → enhance percentiles.

    ``detection=20`` is today's legacy enhance (many false ghost hits).
    Lower values raise the dip thresholds (fewer false positives).
    """
    d = int(detection)
    if d < IR_DETECTION_MIN or d > IR_DETECTION_MAX:
        raise ValueError(f"detection must be {IR_DETECTION_MIN}..{IR_DETECTION_MAX}, got {detection}")
    t = d / float(IR_DETECTION_MAX)  # 0 at selective, 1 at legacy/hot
    lo = _IR_DET0_LO + t * (_IR_DET20_LO - _IR_DET0_LO)
    hi = _IR_DET0_HI + t * (_IR_DET20_HI - _IR_DET0_HI)
    return {"dip_lo_percentile": float(lo), "dip_hi_percentile": float(hi)}


def _ir_dip_maps(arr):
    """Local dip map + center sample mask for IR detection / enhance."""
    import numpy as np

    height, width = int(arr.shape[0]), int(arr.shape[1])
    film_hy = max(8, min(80, height // 20))
    film_hx = max(12, min(120, width // 16))
    local_hy = max(1, min(3, height // 200))
    local_hx = max(4, min(12, width // 120))
    film = _box_mean_2d(arr, film_hy, film_hx)
    local = _box_mean_2d(arr, local_hy, local_hx)
    dip = np.maximum(0.0, local - arr)
    y0, y1 = int(height * 0.08), int(height * 0.92)
    x0, x1 = int(width * 0.08), int(width * 0.92)
    center = np.zeros((height, width), dtype=bool)
    center[y0:y1, x0:x1] = True
    center &= arr > 10000.0
    return film, local, dip, center


def _punch_strength(dip, sample_dips, dip_lo_percentile: float, dip_hi_percentile: float):
    import numpy as np

    if sample_dips.size < 64:
        sample_dips = dip.reshape(-1)
    lo = float(np.percentile(sample_dips, float(dip_lo_percentile)))
    hi = float(np.percentile(sample_dips, float(dip_hi_percentile)))
    hi = max(hi, lo + 1.0)
    strength = np.clip((dip - lo) / (hi - lo), 0.0, 1.0)
    return strength * strength * (3.0 - 2.0 * strength)


def estimate_ir_detection(ir_flat) -> int:
    """Auto Detection: max real-defect hits, minimize ghost false positives.

    Sweeps ``0..20``, scores ``tp - 2*fp`` on dip-pool proxies, ties → lower ``d``.
    """
    import numpy as np

    arr = np.asarray(ir_flat, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[0] < 8 or arr.shape[1] < 8:
        return 15
    _film, _local, dip, center = _ir_dip_maps(arr)
    sample = dip[center]
    if sample.size < 64:
        sample = dip.reshape(-1)
    p80, p96, p995 = np.percentile(sample, (80.0, 96.0, 99.5))
    true_pool = center & (dip >= p995)
    ghost_pool = center & (dip >= p80) & (dip <= p96)
    n_true = int(true_pool.sum())
    n_ghost = int(ghost_pool.sum())
    if n_true < 1:
        return 10

    best_d = 0
    best_score = -1e9
    for d in range(IR_DETECTION_MIN, IR_DETECTION_MAX + 1):
        params = detection_to_enhance_params(d)
        strength = _punch_strength(
            dip,
            sample,
            params["dip_lo_percentile"],
            params["dip_hi_percentile"],
        )
        tp = float(np.mean(strength[true_pool] >= 0.5)) if n_true else 0.0
        fp = float(np.mean(strength[ghost_pool] >= 0.5)) if n_ghost else 0.0
        score = tp - 2.0 * fp
        # Prefer lower d on ties (fewer false positives).
        if score > best_score + 1e-9 or (abs(score - best_score) <= 1e-9 and d < best_d):
            best_score = score
            best_d = d
    return int(best_d)


def enhance_ir_defect_contrast(
    ir,
    *,
    detection: int | None = None,
    target: float = IR_SIDECAR_TARGET_LEVEL,
    film_retain: float = 0.28,
    dip_lo_percentile: float | None = None,
    dip_hi_percentile: float | None = None,
    defect_floor: float = 2000.0,
):
    """Wash film ghost and punch local dark defects toward SilverFast IR levels.

    SilverFast's IR page keeps film in a bright band (~53–61k) with only a faint
    ghost, while dust/scratches sit near ~2k. Lab IR after flatten still has a
    strong dye ghost whose darks compete with shallow defects; this remaps:

    1. Low-pass film → bright field at ``target`` with ``film_retain`` ghost.
    2. Local dips (vs a small box mean) above adaptive percentiles are punched
       toward ``defect_floor``.

    Pass ``detection`` (0..20, SilverFast iSRD scale) to set percentiles;
    ``detection=20`` is the legacy enhance (hot / many false positives).
    Explicit ``dip_*_percentile`` override when ``detection`` is None.

    Reference: ``captures/8200i-se/Silverfast_ir_tiff/``.
    """
    import numpy as np

    if detection is not None:
        params = detection_to_enhance_params(int(detection))
        dip_lo_percentile = params["dip_lo_percentile"]
        dip_hi_percentile = params["dip_hi_percentile"]
    else:
        if dip_lo_percentile is None:
            dip_lo_percentile = _IR_DET20_LO
        if dip_hi_percentile is None:
            dip_hi_percentile = _IR_DET20_HI

    arr = np.asarray(ir, dtype=np.float32)
    if arr.ndim != 2:
        raise ValueError(f"expected HxW infrared plane, got {arr.shape}")
    height, width = int(arr.shape[0]), int(arr.shape[1])
    if height < 8 or width < 8:
        return np.clip(np.rint(arr), 0, 65535).astype(np.uint16)

    film, local, dip, center = _ir_dip_maps(arr)
    base = float(target) + (film - float(np.mean(film))) * float(film_retain)
    base = base + (local - film) * (float(film_retain) * 0.35)

    sample = dip[center]
    strength = _punch_strength(dip, sample, float(dip_lo_percentile), float(dip_hi_percentile))
    # Preserve already-crushed blacks (holder / prior zeros).
    strength = np.maximum(strength, np.clip((8000.0 - arr) / 8000.0, 0.0, 1.0))
    out = base * (1.0 - strength) + float(defect_floor) * strength
    return np.clip(np.rint(out), 0, 65535).astype(np.uint16)


def average_rgb16_columns(
    raw: bytes,
    *,
    pixels: int,
    lines: int,
    planar: bool = True,
) -> list[tuple[int, int, int]]:
    """Per-column mean R/G/B from ``lines`` of RGB16.

    ``planar=True``: each line is ``RRR…GGG…BBB…`` (SE USB).
    ``planar=False``: chunky ``RGBRGB…``.
    """
    row_bytes = int(pixels) * 6
    need = row_bytes * int(lines)
    if len(raw) < need:
        raise ValueError(f"shading raw too short: {len(raw)} < {need}")
    sums = [[0, 0, 0] for _ in range(pixels)]
    plane = int(pixels) * 2
    for line in range(lines):
        base = line * row_bytes
        for x in range(pixels):
            for c in range(3):
                off = base + c * plane + x * 2 if planar else base + x * 6 + 2 * c
                sums[x][c] += int.from_bytes(raw[off : off + 2], "little")
    inv = 1.0 / float(lines)
    return [(round(s[0] * inv), round(s[1] * inv), round(s[2] * inv)) for s in sums]


def channel_means_u16(
    strip: bytes,
    *,
    pixels: int = AFE_STRIP_PIXELS,
    planar: bool = True,
) -> tuple[float, float, float]:
    """Mean R/G/B from a 16-bit LE strip (AFE probe line).

    ``planar=True``: ``RRR…GGG…BBB…``. ``planar=False``: interleaved ``RGBRGB…``.
    """
    expected = pixels * 3 * 2
    if len(strip) < expected:
        raise ValueError(f"strip too short: {len(strip)} < {expected}")
    sums = [0, 0, 0]
    if planar:
        plane = pixels * 2
        for c in range(3):
            base = c * plane
            for i in range(pixels):
                off = base + i * 2
                sums[c] += int.from_bytes(strip[off : off + 2], "little")
    else:
        for i in range(pixels):
            base = i * 6
            for c in range(3):
                sums[c] += int.from_bytes(strip[base + 2 * c : base + 2 * c + 2], "little")
    return (sums[0] / pixels, sums[1] / pixels, sums[2] / pixels)


def _rgb_mean(acc: list[int], count: int) -> tuple[int, int, int]:
    return (round(acc[0] / count), round(acc[1] / count), round(acc[2] / count))


def parity_dark_from_columns(
    dark_per_pixel: Sequence[Sequence[int]],
) -> list[tuple[int, int, int]]:
    """Broadcast the even-column mean and the odd-column mean separately.

    At 1440, 2400, and 7200 dpi the two CCD rows land on alternate columns
    and their dark levels differ. One mean leaves a stripe in the shadows.
    """
    if not dark_per_pixel:
        raise ValueError("dark_per_pixel is empty")
    even = [0, 0, 0]
    odd = [0, 0, 0]
    n_even = n_odd = 0
    for i, sample in enumerate(dark_per_pixel):
        if len(sample) != 3:
            raise ValueError("each dark sample must be length-3 RGB")
        acc = even if i % 2 == 0 else odd
        if i % 2 == 0:
            n_even += 1
        else:
            n_odd += 1
        for c in range(3):
            acc[c] += int(sample[c])
    if n_even == 0:
        mean = _rgb_mean(odd, n_odd)
        even_mean = odd_mean = mean
    elif n_odd == 0:
        mean = _rgb_mean(even, n_even)
        even_mean = odd_mean = mean
    else:
        even_mean = _rgb_mean(even, n_even)
        odd_mean = _rgb_mean(odd, n_odd)
    return [even_mean if i % 2 == 0 else odd_mean for i in range(len(dark_per_pixel))]


def constant_dark_from_columns(
    dark_per_pixel: Sequence[Sequence[int]],
) -> list[tuple[int, int, int]]:
    """Broadcast channel means across X — first-upload shape in sessions 03/04."""
    if not dark_per_pixel:
        raise ValueError("dark_per_pixel is empty")
    n = len(dark_per_pixel)
    acc = [0, 0, 0]
    for sample in dark_per_pixel:
        if len(sample) != 3:
            raise ValueError("each dark sample must be length-3 RGB")
        for c in range(3):
            acc[c] += int(sample[c])
    mean: tuple[int, int, int] = (
        round(acc[0] / n),
        round(acc[1] / n),
        round(acc[2] / n),
    )
    return [mean] * n


def shading_gain_for_white(
    white_unity: float,
    *,
    target: int = SHADING_GAIN_TARGET,
) -> int:
    """Fixed-point gain that lifts ``white_unity`` to ``target``.

    ``white_unity`` is the DVDSET reading with the unity table loaded, so the
    ASIC has already removed the dark term. Session 04 reproduces SF's
    coefficients from this to within a few LSB.
    """
    denom = max(1.0, float(white_unity))
    gain = round(float(target) * SHADING_GAIN_UNITY / denom)
    return max(SHADING_GAIN_MIN, min(SHADING_GAIN_MAX, gain))


def shading_gains_from_white(
    white: Sequence[Sequence[int]],
    *,
    target: int = SHADING_GAIN_TARGET,
) -> list[tuple[int, int, int]]:
    """Per-pixel RGB gains for a post-unity DVDSET white strip."""
    out: list[tuple[int, int, int]] = []
    for row in white:
        if len(row) != 3:
            raise ValueError("each white sample must be length-3 RGB")
        g = tuple(shading_gain_for_white(row[c], target=target) for c in range(3))
        out.append((g[0], g[1], g[2]))
    return out


def build_measured_shading_table(
    dark: Sequence[Sequence[int]],
    white: Sequence[Sequence[int]],
    *,
    flatten_dark: bool = True,
    declared_size: int | None = None,
    target: int = SHADING_GAIN_TARGET,
) -> bytes:
    """Pack the second shading upload: measured dark plus computed gains.

    ``white`` is the post-unity DVDSET white strip, **not** a coefficient — the
    gain slot gets ``target * 0x2000 / white`` so DVDSET flattens the field.
    Packing the measurement itself amplifies the vignette instead.
    """
    dark_rows: Sequence[Sequence[int]] = constant_dark_from_columns(dark) if flatten_dark else dark
    if declared_size is None:
        declared_size = declared_shading_size(len(white))
    return pack_shading_table(
        dark_rows,
        shading_gains_from_white(white, target=target),
        declared_size=declared_size,
    )


def pack_shading_table(
    dark: Sequence[Sequence[int]],
    gain: Sequence[Sequence[int]],
    *,
    declared_size: int | None = None,
) -> bytes:
    """Pack per-pixel RGB dark/gain u16 terms into the ASIC shading blob.

    Pairs run ``(dark_r, gain_r), (dark_g, gain_g), (dark_b, gain_b)`` per pixel,
    little-endian, and every 512-byte block ends in two ``gain = 0`` pad pairs
    that DVDSET skips (see :data:`SHADING_BLOCK_DATA_PAIRS`). ``declared_size``
    zero-fills the tail.
    """
    if len(dark) != len(gain):
        raise ValueError("dark/gain length mismatch")
    out = bytearray()
    pairs = 0
    last_dark = 0
    for d, g in zip(dark, gain, strict=True):
        if len(d) != 3 or len(g) != 3:
            raise ValueError("each dark/gain sample must be length-3 RGB")
        for channel in range(3):
            if pairs and pairs % SHADING_BLOCK_DATA_PAIRS == 0:
                for _ in range(SHADING_BLOCK_PAD_PAIRS):
                    out += last_dark.to_bytes(2, "little") + b"\x00\x00"
            last_dark = int(d[channel]) & 0xFFFF
            out += last_dark.to_bytes(2, "little")
            out += (int(g[channel]) & 0xFFFF).to_bytes(2, "little")
            pairs += 1
    if declared_size is not None:
        if len(out) > declared_size:
            raise ValueError(f"payload {len(out)} exceeds declared_size {declared_size}")
        out.extend(b"\x00" * (declared_size - len(out)))
    return bytes(out)


def unpack_shading_table(payload: bytes) -> list[dict[str, list[int]]]:
    """Parse a shading AHB payload into ``{dark:[r,g,b], gain:[r,g,b]}`` rows.

    Skips the block pad pairs, so the result is indexed by pixel.
    """
    pairs: list[tuple[int, int]] = []
    for off in range(0, len(payload) - SHADING_PAIR_BYTES + 1, SHADING_PAIR_BYTES):
        if (off // SHADING_PAIR_BYTES) % SHADING_BLOCK_PAIRS >= SHADING_BLOCK_DATA_PAIRS:
            continue
        pairs.append(
            (
                int.from_bytes(payload[off : off + 2], "little"),
                int.from_bytes(payload[off + 2 : off + 4], "little"),
            )
        )
    entries: list[dict[str, list[int]]] = []
    for i in range(0, len(pairs) - 2, 3):
        trio = pairs[i : i + 3]
        entries.append({"dark": [p[0] for p in trio], "gain": [p[1] for p in trio]})
    return entries


def make_unity_white_table(
    dark_rgb_per_pixel: Sequence[Sequence[int]],
    *,
    declared_size: int | None = None,
    flatten_dark: bool = True,
) -> bytes:
    """First shading upload shape: measured dark, gain fixed at unity (``0x2000``)."""
    dark_rows: Sequence[Sequence[int]] = constant_dark_from_columns(dark_rgb_per_pixel) if flatten_dark else dark_rgb_per_pixel
    gain = [(SHADING_GAIN_UNITY,) * 3] * len(dark_rows)
    if declared_size is None:
        declared_size = declared_shading_size(len(dark_rows))
    return pack_shading_table(dark_rows, gain, declared_size=declared_size)


def dichotomy_bracket_update(
    low: int,
    high: int,
    current: int,
    mean: float,
    target: float,
    *,
    code_increases_mean: bool,
) -> tuple[int, int, int]:
    """One binary-search step. Returns ``(new_low, new_high, next_code)``."""
    if high <= low:
        return low, high, low
    if code_increases_mean:
        if mean > target:
            high = current
        else:
            low = current
    elif mean > target:
        low = current
    else:
        high = current
    if high <= low + 1:
        return low, high, low
    next_code = (low + high) // 2
    if next_code == current:
        next_code = current + 1 if high > current else max(low, current - 1)
    return low, high, next_code


def search_afe_codes(
    *,
    initial: tuple[int, int, int],
    code_max: int,
    target: float,
    iterations: int,
    tolerance: float,
    code_increases_mean: bool,
    apply: Callable[[tuple[int, int, int]], tuple[float, float, float]],
    code_min: int = 0,
) -> tuple[int, int, int]:
    """Dichotomy per RGB channel with a shared strip acquire via ``apply``."""
    lo0 = max(0, int(code_min))
    hi0 = max(lo0, int(code_max))
    lows = [lo0, lo0, lo0]
    highs = [hi0, hi0, hi0]
    codes = [max(lo0, min(hi0, int(v))) for v in initial]
    for _ in range(max(1, int(iterations))):
        means = apply((codes[0], codes[1], codes[2]))
        done = True
        for c in range(3):
            if abs(means[c] - target) <= tolerance:
                continue
            done = False
            lows[c], highs[c], codes[c] = dichotomy_bracket_update(
                lows[c],
                highs[c],
                codes[c],
                means[c],
                target,
                code_increases_mean=code_increases_mean,
            )
            codes[c] = max(lo0, min(hi0, int(codes[c])))
        if done:
            break
    return (codes[0], codes[1], codes[2])


def run_afe_dichotomy(
    measure: Callable[[AfeFrontend], tuple[float, float, float]],
    *,
    config: AfeSearchConfig | None = None,
    start: AfeFrontend | None = None,
) -> AfeFrontend:
    """Search offsets then gains so strip channel means approach the targets.

    ``measure(fe)`` must program the FE (or accept the argument as the codes to
    use) and return the RGB means of one stationary strip. Offset search runs
    first with gains held; gain search then runs with the settled offsets.
    """
    cfg = config or AfeSearchConfig()
    fe0 = start or AfeFrontend(offsets=(0, 0, 0), gains=(0, 0, 0))

    def measure_offsets(offsets: tuple[int, int, int]) -> tuple[float, float, float]:
        return measure(AfeFrontend(offsets=offsets, gains=fe0.gains))

    offsets = search_afe_codes(
        initial=fe0.offsets,
        code_max=cfg.offset_max,
        target=float(cfg.offset_target),
        iterations=cfg.iterations,
        tolerance=cfg.tolerance,
        code_increases_mean=cfg.offset_increases_mean,
        apply=measure_offsets,
    )

    def measure_gains(gains: tuple[int, int, int]) -> tuple[float, float, float]:
        return measure(AfeFrontend(offsets=offsets, gains=gains))

    # Start gain search near the capture mid probe (0x80), not zero.
    gain0 = fe0.gains if any(fe0.gains) else (0x80, 0x80, 0x80)
    gains = search_afe_codes(
        initial=gain0,
        code_max=cfg.gain_max,
        target=float(cfg.gain_target),
        iterations=cfg.iterations,
        tolerance=cfg.tolerance,
        code_increases_mean=cfg.gain_increases_mean,
        apply=measure_gains,
    )
    return AfeFrontend(offsets=offsets, gains=gains)
