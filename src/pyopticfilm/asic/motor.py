# SPDX-License-Identifier: GPL-3.0-or-later
"""Motor slope tables (ported from SANE genesys motor.cpp / low.cpp)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from pyopticfilm.device.protocol import DEFAULT_GL845_MOTOR, FilmModel, MotorProfile
from pyopticfilm.logging import get_logger

logger = get_logger(__name__)

# Back-compat aliases (8200i / GL845 defaults)
MOTOR_INITIAL_W = DEFAULT_GL845_MOTOR.initial_w
MOTOR_MAX_W = DEFAULT_GL845_MOTOR.max_w
MOTOR_SLOPE_STEPS = DEFAULT_GL845_MOTOR.slope_steps
STEP_TYPE_QUARTER = DEFAULT_GL845_MOTOR.step_type
MOTOR_VREF = DEFAULT_GL845_MOTOR.vref
SLOPE_TABLE_MAX = DEFAULT_GL845_MOTOR.slope_table_max


@dataclass
class MotorSlope:
    initial_speed_w: int
    max_speed_w: int
    acceleration: float

    @classmethod
    def create_from_steps(cls, initial_w: int, max_w: int, steps: int) -> MotorSlope:
        initial_v = 1.0 / float(initial_w)
        max_v = 1.0 / float(max_w)
        accel = (max_v * max_v - initial_v * initial_v) / (2.0 * steps)
        return cls(initial_speed_w=initial_w, max_speed_w=max_w, acceleration=accel)

    def get_table_step_shifted(self, step: int, step_type: int) -> int:
        if step < 2:
            return self.initial_speed_w >> step_type
        step -= 1
        initial_v = 1.0 / float(self.initial_speed_w)
        speed_v = math.sqrt(initial_v * initial_v + 2.0 * self.acceleration * step)
        return int(1.0 / speed_v) >> step_type


@dataclass
class MotorSlopeTable:
    table: list[int] = field(default_factory=list)

    @property
    def pixeltime_sum(self) -> int:
        return sum(self.table)


def create_slope_table_for_speed(
    slope: MotorSlope,
    target_speed_w: int,
    step_type: int,
    steps_alignment: int,
    min_size: int,
    max_size: int,
) -> MotorSlopeTable:
    target_shifted = target_speed_w >> step_type
    max_shifted = slope.max_speed_w >> step_type
    final_speed = max(target_shifted, max_shifted)

    table: list[int] = []
    while len(table) < max_size - 1:
        current = slope.get_table_step_shifted(len(table), step_type)
        if current <= final_speed:
            break
        table.append(current)
    table.append(final_speed)
    while len(table) < max_size - 1 and (
        len(table) % steps_alignment != 0 or len(table) < min_size
    ):
        table.append(table[-1])
    return MotorSlopeTable(table=table)


def _profile_of(model: FilmModel | MotorProfile | None) -> MotorProfile:
    if model is None:
        return DEFAULT_GL845_MOTOR
    if isinstance(model, MotorProfile):
        return model
    return model.motor_profile


def create_scan_slope_table(
    *,
    ydpi: int,
    exposure: int,
    base_ydpi: int,
    step_multiplier: int = 1,
    profile: FilmModel | MotorProfile | None = None,
) -> MotorSlopeTable:
    mp = _profile_of(profile)
    slope = MotorSlope.create_from_steps(mp.initial_w, mp.max_w, mp.slope_steps)
    target = (exposure * ydpi) // base_ydpi
    return create_slope_table_for_speed(
        slope,
        target,
        mp.step_type,
        step_multiplier,
        2 * step_multiplier,
        mp.slope_table_max,
    )


def create_fast_slope_table(
    *,
    step_multiplier: int = 1,
    profile: FilmModel | MotorProfile | None = None,
) -> MotorSlopeTable:
    mp = _profile_of(profile)
    if mp.fast_table is not None:
        return MotorSlopeTable(table=list(mp.fast_table))
    slope = MotorSlope.create_from_steps(mp.initial_w, mp.max_w, mp.slope_steps)
    return create_slope_table_for_speed(
        slope,
        slope.max_speed_w,
        mp.step_type,
        step_multiplier,
        2 * step_multiplier,
        mp.slope_table_max,
    )


def slope_table_to_bytes(table: list[int]) -> bytes:
    out = bytearray()
    for value in table:
        v = int(value) & 0xFFFF
        out.append(v & 0xFF)
        out.append((v >> 8) & 0xFF)
    return bytes(out)


def calculate_zmod(
    *,
    exposure_time: int,
    slope_table: list[int],
    acceleration_steps: int,
    move_steps: int,
    buffer_acceleration_steps: int,
) -> tuple[int, int]:
    """``sanei_genesys_calculate_zmod`` (single-table path)."""
    steps = min(acceleration_steps, len(slope_table))
    total = sum(slope_table[:steps])
    cruise = slope_table[steps - 1]
    z1 = (total + buffer_acceleration_steps * cruise) % exposure_time
    z2 = (total + move_steps * cruise) % exposure_time
    return z1, z2


# Slope table slots → AHB base (genesys scanner_send_slope_table GL845)
SLOPE_TABLE_AHB = {
    0: 0x10000000,  # SCAN
    1: 0x10004000,  # BACKTRACK
    2: 0x10008000,  # STOP
    3: 0x1000C000,  # FAST
    4: 0x10010000,  # HOME
}
