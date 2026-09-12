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
    arr = np.asarray(short_rgb, dtype=np.float64)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise ValueError("short_rgb must be HxWx3")
    if short_exposure <= 0:
        raise ValueError("short_exposure must be positive")

    signal = np.maximum(arr[..., :3] - float(black_level), 0.0)
    flat = signal.reshape(-1, 3)
    if flat.size == 0:
        raise ValueError("short_rgb is empty")

    dense = np.percentile(flat, float(dense_percentile), axis=0)
    ratios = float(target_dense_dn) / np.maximum(dense, _EPSILON)
    proposed_ratio = float(np.max(ratios))
    return float(short_exposure) * proposed_ratio


def predicted_clip_fractions(
    short_rgb: np.ndarray,
    short_exposure: int,
    long_exposure: int,
    *,
    black_level: float = 0.0,
) -> tuple[float, float, float]:
    """Fraction of pixels predicted at/above soft-clip for each RGB channel."""
    arr = np.asarray(short_rgb, dtype=np.float64)
    if arr.ndim != 3 or arr.shape[2] < 3 or short_exposure <= 0:
        return (0.0, 0.0, 0.0)
    scale = float(long_exposure) / float(short_exposure)
    signal = np.maximum(arr[..., :3] - float(black_level), 0.0)
    predicted = float(black_level) + signal * scale
    clips: list[float] = []
    n = predicted.shape[0] * predicted.shape[1]
    if n <= 0:
        return (0.0, 0.0, 0.0)
    for c in range(3):
        clips.append(float(np.count_nonzero(predicted[..., c] >= _CLIP_DN)) / float(n))
    return (clips[0], clips[1], clips[2])


def dense_percentiles(
    short_rgb: np.ndarray,
    *,
    black_level: float = 0.0,
    dense_percentile: float = 5.0,
) -> tuple[float, float, float]:
    """Per-channel dense percentile of signal above black."""
    arr = np.asarray(short_rgb, dtype=np.float64)
    if arr.ndim != 3 or arr.shape[2] < 3:
        return (0.0, 0.0, 0.0)
    signal = np.maximum(arr[..., :3] - float(black_level), 0.0)
    flat = signal.reshape(-1, 3)
    if flat.size == 0:
        return (0.0, 0.0, 0.0)
    p = np.percentile(flat, float(dense_percentile), axis=0)
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
        proposed_f = choose_long_exposure(
            short_rgb,
            short_exposure,
            black_level=black_level,
            dense_percentile=dense_percentile,
            target_dense_dn=target_dense_dn,
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

