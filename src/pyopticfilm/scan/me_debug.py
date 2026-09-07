# SPDX-License-Identifier: GPL-3.0-or-later
"""Lab-only multi-exposure bracket diagnostics (not part of ScanImage)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from pyopticfilm.scan.exposure_merge import FusionStats, PassMergeStats


@dataclass(frozen=True)
class MeScanDebug:
    """Bracket planes and IVW stats from a GL128 ME scan.

    Exposed via :attr:`~pyopticfilm.scanner.Scanner.last_me_debug` for Scan Lab
    and audit tooling. Integrators should use :class:`~pyopticfilm.image.ScanImage`
    ``rgb`` only.
    """

    rgb_short: np.ndarray
    rgb_long: np.ndarray
    exposure_short: int
    exposure_long: int
    fusion_stats: FusionStats | None = None
    align_shift_long: tuple[float, float] | None = None
    align_shift_ir: tuple[float, float] | None = None
    #: Adaptive proposal before safety clamp (``None`` for fixed / legacy).
    exposure_proposed: int | None = None
    #: Why ``exposure_long`` was chosen (adaptive / clamped / fixed / fallback).
    exposure_reason: str | None = None


@dataclass(frozen=True)
class SlotStackDebug:
    """Multi-Pass stacking diagnostics for one exposure slot (short or long)."""

    n_passes: int
    #: Repeat i → repeat 0 alignment shift, for repeats 2..N (len == n_passes - 1).
    align_shifts: list[tuple[float, float]]
    stack_stats: PassMergeStats | None = None


@dataclass(frozen=True)
class MultiPassDebug:
    """Per-slot Multi-Pass stacking diagnostics from a GL128 scan.

    Exposed via :attr:`~pyopticfilm.scanner.Scanner.last_multi_pass_debug`,
    populated whenever ``n_passes > 1``. ``long`` is ``None`` unless
    ``multi_exposure`` was also on (Adaptive Multi-Pass). Integrators should
    use :class:`~pyopticfilm.image.ScanImage` ``rgb`` only.
    """

    short: SlotStackDebug
    long: SlotStackDebug | None = None
