# SPDX-License-Identifier: GPL-3.0-or-later
"""Lab-only multi-exposure bracket diagnostics (not part of ScanImage)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from pyopticfilm.scan.exposure_merge import FusionStats


@dataclass(frozen=True)
class BracketDebug:
    """One captured exposure bracket (linear, pre-merge)."""

    rgb: np.ndarray
    exposure: int
    #: Shift into bracket 0's frame (``None`` for bracket 0 itself).
    align_shift: tuple[float, float] | None = None


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
    #: Every captured bracket (ascending exposure), bracket 0 = rgb_short,
    #: bracket -1 = rgb_long. Populated whenever n_brackets is passed to
    #: Scanner.scan() (including n_brackets=2); None for legacy call sites
    #: that never set it.
    brackets: list[BracketDebug] | None = None
