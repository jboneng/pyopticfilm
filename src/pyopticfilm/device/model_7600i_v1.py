# SPDX-License-Identifier: GPL-3.0-or-later
"""OpticFilm 7600i v1 (GL843): SilverFast job sequences from USB captures (see docs/opticfilm-7600i-v1.md)."""

from __future__ import annotations

import copy
import gzip
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

from pyopticfilm.device.protocol import MotorProfile

#: Fractional R/G/B line delays per vertical sampling rate (lines/inch), measured on B&W film.
CALIBRATED_SHIFTS: dict[int, tuple[float, float, float]] = {
    2880: (0.0, 9.84, 19.30),
    7200: (0.0, 24.22, 48.21),
    14400: (0.0, 48.44, 96.42),
}

_MOTOR = MotorProfile(20325, 2604, 256, 2, 0, 1024)  # unused: homing uses the vendor tables


def shifts_for(yres: int) -> tuple[float, ...]:
    """R/G/B line delays at ``yres`` lines/inch, scaled from the measurement at twice it if needed."""
    ref = yres if yres in CALIBRATED_SHIFTS else 2 * yres
    return tuple(s * yres / ref for s in CALIBRATED_SHIFTS[ref])


@lru_cache(maxsize=1)
def _load_data() -> dict:
    raw = resources.files("pyopticfilm.device.data").joinpath("opticfilm_7600i_v1.json.gz").read_bytes()
    return json.loads(gzip.decompress(raw))


@lru_cache(maxsize=1)
def _infrared_7200() -> dict:
    from pyopticfilm.scan.replay_gl843_v1 import derive_infrared

    p = _load_data()["profiles"]
    return derive_infrared(p["color_7200"], p["color_3600"], p["infrared_3600"])


@dataclass(frozen=True)
class Model7600iV1:
    name: str = "plustek-opticfilm-7600i-v1"
    vendor: str = "PLUSTEK"
    model: str = "OpticFilm 7600i (v1)"
    asic: str = "GL843"
    usb_vendor_id: int = 0x07B3
    usb_product_id: int = 0x0C3B
    scan_ready: bool = True
    resolutions_dpi: tuple[int, ...] = (7200, 3600, 1440)
    infrared_resolutions_dpi: tuple[int, ...] = (7200, 3600)
    bpp_gray: tuple[int, ...] = ()
    bpp_color: tuple[int, ...] = (16,)
    supports_infrared: bool = True
    mirror_x: bool = True
    #: CCD dummy lines in the main scan: "none", "fewer" or "recorded" (SilverFast's).
    dummy_lines: str = "none"
    #: sample the nominal resolution vertically instead of twice it (SilverFast):
    #: half the lines and scan time, without SilverFast's line-pair averaging.
    single_sample: bool = True
    #: first sensor pixel of the scan window: 80, the first lit pixel (the full area, 36.61 mm;
    #: 82 at 3600 dpi to keep an even pixel count). SilverFast starts at 210. None keeps it.
    window_start: int | None = 80
    lamp_warmup_s: float = 1.0
    x_size_mm: float = 36.61
    y_size_mm: float = 24.72
    x_offset_ta_mm: float = 0.0
    y_offset_ta_mm: float = 0.0
    x_size_ta_mm: float = 36.61
    y_size_ta_mm: float = 24.72
    x_size_calib_mm: float = 36.61
    y_size_calib_ta_mm: float = 0.0
    y_offset_calib_white_ta_mm: float = 0.0
    y_offset_sensor_to_ta_mm: float = 0.0
    ld_shift_r: int = 0
    ld_shift_g: int = 12
    ld_shift_b: int = 24
    stagger_y_by_dpi: Mapping[int, tuple[int, ...]] = field(
        default_factory=lambda: {1440: (), 3600: (), 7200: (4, 0)}
    )
    register_dpiset_by_dpi: Mapping[int, int] = field(default_factory=dict)
    output_pixel_offset_by_dpi: Mapping[int, int] = field(default_factory=dict)
    register_dpihw: int = 1200
    exposure_lperiod: int = 14000
    motor_base_ydpi: int = 3600
    optical_resolution: int = 7200
    motor_profile: MotorProfile = _MOTOR
    init_regs: Mapping[int, int] = field(default_factory=dict)
    sensor_custom_regs: Mapping[int, int] = field(default_factory=dict)
    frontend_regs: Mapping[int, int] = field(default_factory=dict)
    gpo_regs: Mapping[int, int] = field(default_factory=dict)
    memory_layout_regs: Mapping[int, int] = field(default_factory=dict)

    @property
    def max_area_mm(self) -> tuple[float, float]:
        return (self.x_size_ta_mm, self.y_size_ta_mm)

    def boot_register_map(self) -> dict[int, int]:
        regs: dict[int, int] = {}
        for group in self.replay_data()["boot"]:
            if group[0] != "8c":
                regs.update(zip(group[::2], group[1::2], strict=True))
        return regs

    def replay_data(self) -> dict:
        return _load_data()

    def replay_profile(self, mode: str, dpi: int) -> dict:
        """A copy of the vendor job for ``mode`` ("color" / "infrared") at ``dpi``; infrared at 7200 dpi
        is derived from the 7200 dpi colour job (not captured)."""
        key = f"{'infrared' if mode == 'infrared' else 'color'}_{dpi}"
        profiles = self.replay_data()["profiles"]
        if key == "infrared_7200":
            return copy.deepcopy(_infrared_7200())
        if key not in profiles:
            raise ValueError(f"{self.model} has no {mode} sequence at {dpi} dpi")
        return copy.deepcopy(profiles[key])


MODEL_7600I_V1 = Model7600iV1()
