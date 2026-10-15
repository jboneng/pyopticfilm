# SPDX-License-Identifier: GPL-3.0-or-later
"""Frame-adaptive ME long-pass exposure selection (Phase 1).

The short colour plane proposes a useful long ``REG_EXPOSURE``; a separate
safety clamp enforces the validated envelope. Adaptive code never writes the
register unchecked — the GL128 session clamps again at configure time.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

ALGORITHM_VERSION = "1"

_FULL_SCALE = 65535.0
_EPSILON = 1.0
#: Soft clip threshold for predicted long-pass saturation fractions.
_CLIP_DN = 0.95 * _FULL_SCALE
#: Row slabs for clip counting — no full-frame float64 temps at 7200 dpi.
_CHUNK_ROWS = 256


def _channel_signal(plane: np.ndarray, black_level: float) -> np.ndarray:
    """``max(DN - black_level, 0)`` for one channel; unsigned DN at black 0 is used as-is."""
    if float(black_level) == 0.0 and plane.dtype.kind == "u":
        return plane
    return np.maximum(plane.astype(np.float64) - float(black_level), 0.0)


def _dense_signal_percentiles(
    arr: np.ndarray, black_level: float, dense_percentile: float
) -> np.ndarray:
    """Per-channel percentile of signal above black, one channel plane at a time.

    Matches ``np.percentile(signal.reshape(-1, 3), q, axis=0)`` exactly.
    """
    return np.array(
        [
            float(np.percentile(_channel_signal(arr[..., c], black_level), float(dense_percentile)))
            for c in range(3)
        ],
        dtype=np.float64,
    )


def _proposal_from_dense(
    dense: np.ndarray, short_exposure: int, target_dense_dn: float
) -> float:
    ratios = float(target_dense_dn) / np.maximum(dense, _EPSILON)
    proposed_ratio = float(np.max(ratios))
    return float(short_exposure) * proposed_ratio


@dataclass(frozen=True)
class MeExposureDecision:
    """Result of adaptive (or fixed) long-pass exposure selection."""

    proposed: int
    selected: int
    reason: str
    dense_p05: tuple[float, float, float]
    predicted_clip: tuple[float, float, float]
    algorithm_version: str = ALGORITHM_VERSION


def choose_long_exposure(
    short_rgb: np.ndarray,
    short_exposure: int,
    *,
    black_level: float = 0.0,
    dense_percentile: float = 5.0,
    target_dense_dn: float = 10000.0,
) -> float:
    """Propose a long exposure from per-channel dense percentiles.

    Uses ``signal = max(DN - black_level, 0)`` and the densest channel's
    required ratio to reach ``target_dense_dn`` at ``dense_percentile``.
    """
    arr = _validated_short(short_rgb, short_exposure)
    dense = _dense_signal_percentiles(arr, black_level, dense_percentile)
    return _proposal_from_dense(dense, short_exposure, target_dense_dn)


def _validated_short(short_rgb: np.ndarray, short_exposure: int) -> np.ndarray:
    arr = np.asarray(short_rgb)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise ValueError("short_rgb must be HxWx3")
    if short_exposure <= 0:
        raise ValueError("short_exposure must be positive")
    if arr.shape[0] * arr.shape[1] == 0:
        raise ValueError("short_rgb is empty")
    return arr


def predicted_clip_fractions(
    short_rgb: np.ndarray,
    short_exposure: int,
    long_exposure: int,
    *,
    black_level: float = 0.0,
) -> tuple[float, float, float]:
    """Fraction of pixels predicted at/above soft-clip for each RGB channel."""
    arr = np.asarray(short_rgb)
    if arr.ndim != 3 or arr.shape[2] < 3 or short_exposure <= 0:
        return (0.0, 0.0, 0.0)
    scale = float(long_exposure) / float(short_exposure)
    h, w = arr.shape[0], arr.shape[1]
    n = h * w
    if n <= 0:
        return (0.0, 0.0, 0.0)
    black = float(black_level)
    counts = [0, 0, 0]
    for c in range(3):
        for y0 in range(0, h, _CHUNK_ROWS):
            # predicted = black + max(DN - black, 0) * scale, evaluated in place.
            s = arr[y0 : y0 + _CHUNK_ROWS, :, c].astype(np.float64)
            np.subtract(s, black, out=s)
            np.maximum(s, 0.0, out=s)
            np.multiply(s, scale, out=s)
            np.add(s, black, out=s)
            counts[c] += int(np.count_nonzero(s >= _CLIP_DN))
    return (counts[0] / float(n), counts[1] / float(n), counts[2] / float(n))


def dense_percentiles(
    short_rgb: np.ndarray,
    *,
    black_level: float = 0.0,
    dense_percentile: float = 5.0,
) -> tuple[float, float, float]:
    """Per-channel dense percentile of signal above black."""
    arr = np.asarray(short_rgb)
    if arr.ndim != 3 or arr.shape[2] < 3:
        return (0.0, 0.0, 0.0)
    if arr.shape[0] * arr.shape[1] == 0:
        return (0.0, 0.0, 0.0)
    p = _dense_signal_percentiles(arr, black_level, dense_percentile)
    return (float(p[0]), float(p[1]), float(p[2]))


def clamp_long_exposure(
    proposed: float,
    *,
    short_exposure: int,
    adaptive_min: int,
    adaptive_max: int,
    hardware_max: int,
    max_ratio: float,
) -> tuple[int, str]:
    """Clamp a proposed long exposure into the validated safety envelope.

    Returns ``(selected, reason)``. Does not trust ``proposed`` beyond this clamp.
    """
    safe_max = min(
        int(hardware_max),
        int(adaptive_max),
        int(max(1, round(float(short_exposure) * float(max_ratio)))),
    )
    lo = int(adaptive_min)
    if lo > safe_max:
        # Misconfigured envelope — prefer the hard ceiling.
        return safe_max, "clamped to validated hardware maximum (min>max)"

    if not np.isfinite(proposed):
        return lo, "fallback non-finite proposal → adaptive minimum"

    raw = round(float(proposed))
    if raw > safe_max:
        return safe_max, "clamped to validated hardware maximum"
    if raw < lo:
        return lo, "clamped to adaptive minimum"
    return raw, "adaptive"


def select_long_exposure(
    short_rgb: np.ndarray,
    short_exposure: int,
    *,
    black_level: float = 0.0,
    dense_percentile: float = 5.0,
    target_dense_dn: float = 10000.0,
    adaptive_min: int = 42000,
    adaptive_max: int = 85000,
    hardware_max: int = 85000,
    max_ratio: float = 7.0,
    default_long: int = 42000,
) -> MeExposureDecision:
    """Choose and clamp a long exposure; fall back to ``default_long`` on error."""
    p05 = dense_percentiles(
        short_rgb, black_level=black_level, dense_percentile=dense_percentile
    )
    try:
        # Same proposal as choose_long_exposure, reusing p05 instead of a
        # second full-frame percentile pass.
        _validated_short(short_rgb, short_exposure)
        proposed_f = _proposal_from_dense(
            np.asarray(p05, dtype=np.float64), short_exposure, target_dense_dn
        )
    except (ValueError, FloatingPointError, TypeError):
        selected = int(default_long)
        clips = predicted_clip_fractions(
            short_rgb,
            short_exposure,
            selected,
            black_level=black_level,
        )
        return MeExposureDecision(
            proposed=selected,
            selected=selected,
            reason="fallback",
            dense_p05=p05,
            predicted_clip=clips,
        )

    if not np.isfinite(proposed_f) or proposed_f <= 0:
        selected = int(default_long)
        clips = predicted_clip_fractions(
            short_rgb,
            short_exposure,
            selected,
            black_level=black_level,
        )
        return MeExposureDecision(
            proposed=int(default_long),
            selected=selected,
            reason="fallback",
            dense_p05=p05,
            predicted_clip=clips,
        )

    proposed_i = round(proposed_f)
    selected, reason = clamp_long_exposure(
        proposed_f,
        short_exposure=short_exposure,
        adaptive_min=adaptive_min,
        adaptive_max=adaptive_max,
        hardware_max=hardware_max,
        max_ratio=max_ratio,
    )
    clips = predicted_clip_fractions(
        short_rgb,
        short_exposure,
        selected,
        black_level=black_level,
    )
    return MeExposureDecision(
        proposed=proposed_i,
        selected=selected,
        reason=reason,
        dense_p05=p05,
        predicted_clip=clips,
    )

