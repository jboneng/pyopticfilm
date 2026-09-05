# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared FilmModel / AsicDriver protocols for multi-model Genesys support."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pyopticfilm.asic.status import ScannerStatus
    from pyopticfilm.usb.protocol import GenesysUsbProtocol

ScanMethod = Literal["transparency", "infrared"]


@dataclass(frozen=True)
class MotorProfile:
    """Per-model motor slope parameters (SANE ``tables_motor.cpp``)."""

    initial_w: int
    max_w: int
    slope_steps: int
    step_type: int  # StepType::QUARTER = 2
    vref: int
    slope_table_max: int


# Default GL845 OpticFilm profile (8200i / 7400 / …)
DEFAULT_GL845_MOTOR = MotorProfile(
    initial_w=64102 * 4,
    max_w=400 * 4,
    slope_steps=100,
    step_type=2,
    vref=3,
    slope_table_max=1024,
)


@runtime_checkable
class FilmModel(Protocol):
    """Structural interface for OpticFilm model tables + caps."""

    name: str
    vendor: str
    model: str
    asic: str
    usb_vendor_id: int
    usb_product_id: int
    scan_ready: bool

    resolutions_dpi: tuple[int, ...]
    bpp_gray: tuple[int, ...]
    bpp_color: tuple[int, ...]
    supports_infrared: bool

    x_size_mm: float
    y_size_mm: float
    x_offset_ta_mm: float
    y_offset_ta_mm: float
    x_size_ta_mm: float
    y_size_ta_mm: float

    x_size_calib_mm: float
    y_size_calib_ta_mm: float
    y_offset_calib_white_ta_mm: float
    y_offset_sensor_to_ta_mm: float

    ld_shift_r: int
    ld_shift_g: int
    ld_shift_b: int

    stagger_y_by_dpi: Mapping[int, tuple[int, ...]]
    register_dpiset_by_dpi: Mapping[int, int]
    output_pixel_offset_by_dpi: Mapping[int, int]

    register_dpihw: int
    exposure_lperiod: int
    motor_base_ydpi: int
    optical_resolution: int
    motor_profile: MotorProfile

    init_regs: Mapping[int, int]
    sensor_custom_regs: Mapping[int, int]
    frontend_regs: Mapping[int, int]
    gpo_regs: Mapping[int, int]
    memory_layout_regs: Mapping[int, int]

    # Optional SANE dpi-keyed overlays (see device.sensor_lookup):
    # lperiod_by_dpi, dummy_pixel / dummy_pixel_by_dpi, sensor_regs_by_dpi,
    # frontend_regs_by_dpi — structural Protocol cannot require them.

    @property
    def max_area_mm(self) -> tuple[float, float]: ...

    def boot_register_map(self) -> dict[int, int]: ...


@runtime_checkable
class Gl128Model(FilmModel, Protocol):
    """Required knobs for GL128 scan-ready models (8200i SE and 8100 V2).

    Shared-session / ASIC code must read these as real attributes — do not
    introduce ``getattr(model, "new_knob", se_default)`` for GL128-specific
    behaviour. Add the field here and on **every** GL128 leaf model instead.
    """

    usb_image_depth: int
    usb_image_lincnt_half_lines: bool
    usb_calib_depth: int
    usb_planar_rgb: bool
    mirror_x: bool
    calib_uses_native_dpiset: bool
    pixel_alignment: int
    optical_span_alignment: int
    strpixel_native_units: bool
    optical_end_inactive_native: int
    min_asic_dpi: int
    default_gl128_prime: bool
    lincnt_includes_line_shift: bool
    image_lincnt_per_line: int
    y_oversampled: bool
    lperiod_by_dpi: Mapping[int, int]
    dummy_by_dpi: Mapping[int, int]
    pixel_clock_by_dpi: Mapping[int, int]
    pixel_clock_long_by_dpi: Mapping[int, int]
    exposure_short: int
    exposure_long: int
    me_adaptive_min_exposure: int
    me_adaptive_max_exposure: int
    me_hardware_max_exposure: int
    me_max_exposure_ratio: float
    me_target_dense_dn: float
    me_dense_percentile: float
    me_black_level: float
    me_noise_alpha: float
    me_noise_beta: float
    me_default_exposure_mode: str
    me_use_banded_alignment: bool
    feed_steps_per_inch: int
    feed_to_reference_steps: int
    feed_to_scan_steps: int
    feed_to_scan_top_steps: int
    feed_to_scan_bottom_steps: int
    max_feed_steps: int
    scan_window_end_steps: int
    max_image_lincnt_by_feed2: Mapping[int, int]
    ladder_feed2_steps: int
    ladder_lincnt_by_dpi: Mapping[int, int]
    use_slow_final_positioning_feed: bool

    def asic_dpi_for(self, resolution: int) -> int: ...

    def oversample_for(self, resolution: int) -> int: ...

    def line_period_for(self, resolution: int) -> int: ...

    def shading_strip_clocks(
        self, resolution: int, *, dvdset: bool
    ) -> tuple[int, int, int]: ...

    def channel_exposure_for(self, resolution: int, *, exposure: int | None = None) -> int: ...

    def pixel_clock_for_image(
        self, resolution: int, *, long_exposure: bool = False
    ) -> int: ...

    def image_exposure(self, *, long_exposure: bool = False) -> int: ...

    def feed_to_scan_steps_for_area(
        self,
        area: tuple[float, float, float, float] | None = None,
    ) -> int: ...

    def max_lincnt_for_feed2(self, feed2: int) -> int | None: ...

    def max_travel_steps_for_feed2(self, feed2: int) -> int: ...

    def max_lincnt_for(self, feed2: int, resolution: int) -> int: ...

    def travel_mm_for_lincnt(self, lincnt: int, resolution: int) -> float: ...

    def lincnt_for_travel_mm(self, travel_mm: float, resolution: int) -> int: ...

    def ladder_lincnt_for(self, resolution: int) -> int: ...


@runtime_checkable
class MultiExposureFilmModel(FilmModel, Protocol):
    """FilmModel + GL128 multi-exposure (ME) fields.

    Not every FilmModel has a colour-long ME pass (GL845 models do not), so
    these cannot live on the base Protocol; GL128-only session/merge code
    (session_gl128.py, me_exposure.py, exposure_merge.py) should type its
    ``model`` parameters as this Protocol instead of the bare ``FilmModel``
    so ME field access is checked rather than relying on
    ``getattr(model, "...", default)``. See device.model_8200i_se.Model8200iSE
    for the concrete definitions.
    """

    exposure_short: int
    exposure_long: int
    multi_exposure_factor: int
    me_adaptive_min_exposure: int
    me_adaptive_max_exposure: int
    me_hardware_max_exposure: int
    me_max_exposure_ratio: float
    me_long_exposure_ceiling_by_dpi: Mapping[int, int]
    me_long_exposure_ceiling_default: int
    me_target_dense_dn: float
    me_dense_percentile: float
    me_black_level: float
    me_default_exposure_mode: str
    me_noise_alpha: float
    me_noise_beta: float

    def me_long_exposure_ceiling(self, resolution: int) -> int: ...


@runtime_checkable
class AsicDriver(Protocol):
    """Chip ops used by Scanner / ScanSession / Calibrator."""

    _initialized: bool
    _reg_cache: dict[int, int]
    _scan_method: ScanMethod
    model: FilmModel
    protocol: GenesysUsbProtocol

    def read_status(self) -> ScannerStatus: ...

    def read_status_reliable(self) -> ScannerStatus: ...

    def is_at_home(self) -> bool: ...

    def is_cold_boot(self) -> bool: ...

    def init(self, *, force: bool = False) -> None: ...

    def set_frontend_init(self) -> None: ...

    def set_scan_method(self, method: ScanMethod) -> None: ...

    def lamp_on(self) -> None: ...

    def lamp_off(self) -> None: ...

    def home(self, *, timeout_s: float = ..., wait: bool = True) -> None: ...

    def park(self, *, timeout_s: float = ...) -> None: ...

    def stop_motor(self) -> None: ...

    def update_home_sensor_gpio(self) -> None: ...
