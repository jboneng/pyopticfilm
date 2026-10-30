# SPDX-License-Identifier: GPL-3.0-or-later
"""Public types for the native registration engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TypeAlias

import numpy as np


class RegistrationMode(str, Enum):
    MULTI_EXPOSURE = "multi_exposure"
    MULTI_PASS = "multi_pass"
    RGB_IR = "rgb_ir"


class RegistrationFailure(str, Enum):
    NONE = "none"
    LOW_TEXTURE = "low_texture"
    AMBIGUOUS_PEAK = "ambiguous_peak"
    INSUFFICIENT_OVERLAP = "insufficient_overlap"
    REGIONAL_DISAGREEMENT = "regional_disagreement"
    OUTSIDE_SCANNER_PRIOR = "outside_scanner_prior"
    CROSS_MODAL_MISMATCH = "cross_modal_mismatch"
    NON_TRANSLATIONAL_MOTION = "non_translational_motion"
    RESIDUAL_NOT_IMPROVED = "residual_not_improved"
    SHAPE_MISMATCH = "shape_mismatch"
    PATHLOGICAL_SHIFT = "pathological_shift"


@dataclass(frozen=True)
class TranslationTransform:
    """Maps moving-image coordinates into the reference coordinate system."""

    dx: float
    dy: float

    @property
    def nearest_integer(self) -> tuple[int, int]:
        return round(self.dx), round(self.dy)


@dataclass(frozen=True)
class LineWarpTransform:
    """Constant dx with a per-row dy profile (banded Multi-Pass, ME row drift)."""

    dx: float
    dy_per_row: np.ndarray  # length H, float64

    @property
    def dy_at_center(self) -> float:
        h = int(self.dy_per_row.shape[0])
        if h == 0:
            return 0.0
        return float(self.dy_per_row[h // 2])


Transform: TypeAlias = TranslationTransform | LineWarpTransform


@dataclass(frozen=True)
class RegionalShift:
    center_x: float
    center_y: float
    dx: float
    dy: float
    response: float
    accepted: bool


@dataclass(frozen=True)
class RegistrationQuality:
    response: float = 0.0
    peak_ratio: float = 0.0
    regional_inliers: int = 0
    regional_total: int = 0
    regional_mad_px: float = 0.0
    residual_before: float = 0.0
    residual_after: float = 0.0
    overlap_fraction: float = 1.0
    prior_distance: float = 0.0


@dataclass(frozen=True)
class RegistrationResult:
    transform: Transform
    mode: RegistrationMode
    accepted: bool
    integer_exact: bool
    quality: RegistrationQuality = field(default_factory=RegistrationQuality)
    regions: tuple[RegionalShift, ...] = ()
    reason: str = ""
    failure: RegistrationFailure = RegistrationFailure.NONE

    @property
    def shift(self) -> tuple[float, float]:
        """Representative (dx, dy) for debug / legacy callers."""
        t = self.transform
        if isinstance(t, TranslationTransform):
            return (float(t.dx), float(t.dy))
        return (float(t.dx), float(t.dy_at_center))


@dataclass(frozen=True)
class RegistrationContext:
    model: str | None = None
    dpi: int | None = None
    crop: tuple[float, float, float, float] | None = None
    reference_pass: str | None = None
    moving_pass: str | None = None
    acquisition_index: int | None = None
    reference_exposure: int | None = None
    moving_exposure: int | None = None
    expected_dx: float | None = None
    expected_dy: float | None = None
    max_abs_dx: float | None = None
    max_abs_dy: float | None = None


@dataclass(frozen=True)
class RegistrationOptions:
    coarse_target_px: int = 1024
    #: Keep refine ROI small so Guizar stays cheap and estimate stays under targets.
    refine_roi_side: int = 512
    subpixel_factor: int = 16
    max_candidates: int = 5
    #: Floor for accepting a phase-correlation peak (float32 coarse).
    min_response: float = 0.03
    sanity_max_frac: float = 0.5
    refine_max_residual: float = 2.0
    enable_subpixel: bool = True
    #: Guizar on the refine ROI when the tile is at most this side length.
    guizar_max_roi_side: int = 512
    #: Guizar localized DFT on the refine ROI (subpixel); cheap when ROI is small.
    enable_guizar_refine: bool = True
    #: 3×3 regional consensus on the coarse plane (audit / diagnostics).
    enable_regional: bool = False
    snap_frac_eps: float = 0.08
    band_count: int = 8
    band_min_height: int = 256
    band_outlier_px: float = 8.0
    fractional_resampler: str = "cubic"  # cubic | linear
    #: Row chunk height for separable fractional warps (peak RAM control).
    warp_chunk_rows: int = 512
    #: ME row drift: full-resolution tile grid (rows x columns) and tile side.
    drift_tile_rows: int = 6
    drift_tile_cols: int = 3
    drift_tile_side: int = 384
    #: Tiles below this phase-correlation response carry too little detail.
    drift_min_response: float = 0.1
    #: Residual past the integer seed a tile may report (long frames drift further).
    drift_max_residual: float = 8.0
    drift_min_tiles: int = 6
    #: Top-to-bottom drift below this (px) stays a plain translation.
    drift_min_range_px: float = 0.15
    drift_outlier_px: float = 0.25
