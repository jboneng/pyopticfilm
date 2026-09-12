# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for adaptive ME long-pass exposure selection."""

from __future__ import annotations

import numpy as np
import pytest

from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.scan.me_exposure import (
    choose_long_exposure,
    clamp_long_exposure,
    select_long_exposure,
)


def _rgb(value: float, shape: tuple[int, int] = (32, 32)) -> np.ndarray:
    h, w = shape
    return np.full((h, w, 3), value, dtype=np.float64)


def test_choose_long_dense_frame_raises_exposure():
    # p05 ≈ 1765 → need ~5.7× to reach target 10000
    short = _rgb(1765.0)
    proposed = choose_long_exposure(short, 14000, target_dense_dn=10000.0)
    assert proposed > 42000
    assert proposed == pytest.approx(14000 * (10000.0 / 1765.0), rel=1e-6)


def test_choose_long_thin_frame_low_ratio():
    # Already bright dense region → ratio near 1
    short = _rgb(12000.0)
    proposed = choose_long_exposure(short, 14000, target_dense_dn=10000.0)
    assert proposed < 42000
    assert proposed == pytest.approx(14000 * (10000.0 / 12000.0), rel=1e-6)


def test_clamp_above_hardware_max():
    selected, reason = clamp_long_exposure(
        100000,
        short_exposure=14000,
        adaptive_min=42000,
        adaptive_max=85000,
        hardware_max=85000,
        max_ratio=7.0,
    )
    assert selected == 85000
    assert "hardware" in reason


def test_clamp_below_adaptive_min():
    selected, reason = clamp_long_exposure(
        20000,
        short_exposure=14000,
        adaptive_min=42000,
        adaptive_max=85000,
        hardware_max=85000,
        max_ratio=7.0,
    )
    assert selected == 42000
    assert "minimum" in reason


def test_clamp_respects_max_ratio():
    # short*5 = 70000 caps before adaptive/hardware 85000
    selected, reason = clamp_long_exposure(
        100000,
        short_exposure=14000,
        adaptive_min=42000,
        adaptive_max=85000,
        hardware_max=85000,
        max_ratio=5.0,
    )
    assert selected == 70000
    assert "hardware" in reason


def test_select_dense_rises_toward_max():
    # Very dense: p05=1000 → proposed ≈ 140000 → clamp to 85000
    short = _rgb(1000.0)
    decision = select_long_exposure(
        short,
        14000,
        target_dense_dn=10000.0,
        adaptive_min=42000,
        adaptive_max=85000,
        hardware_max=85000,
        max_ratio=7.0,
        default_long=42000,
    )
    assert decision.proposed > 85000
    assert decision.selected == 85000
    assert decision.reason.startswith("clamped")


def test_select_thin_stays_at_adaptive_min():
    short = _rgb(12000.0)
    decision = select_long_exposure(
        short,
        14000,
        target_dense_dn=10000.0,
        adaptive_min=42000,
        adaptive_max=85000,
        hardware_max=85000,
        max_ratio=7.0,
        default_long=42000,
    )
    assert decision.selected == 42000
    assert "minimum" in decision.reason


def test_select_nan_fallback():
    short = np.full((8, 8, 3), np.nan, dtype=np.float64)
    decision = select_long_exposure(
        short,
        14000,
        default_long=42000,
        adaptive_min=42000,
        adaptive_max=85000,
        hardware_max=85000,
        max_ratio=7.0,
    )
    assert decision.selected == 42000
    assert decision.reason == "fallback"


def test_select_empty_fallback():
    short = np.zeros((0, 0, 3), dtype=np.float64)
    decision = select_long_exposure(short, 14000, default_long=42000)
    assert decision.selected == 42000
    assert decision.reason == "fallback"


def test_model_adaptive_envelope_defaults():
    assert MODEL_8200I_SE.exposure_long == 42000
    assert MODEL_8200I_SE.me_adaptive_min_exposure == 42000
    assert MODEL_8200I_SE.me_adaptive_max_exposure == 85000
    assert MODEL_8200I_SE.me_hardware_max_exposure == 85000
    assert MODEL_8200I_SE.me_max_exposure_ratio == 7.0
    assert MODEL_8200I_SE.me_target_dense_dn == 10000.0
