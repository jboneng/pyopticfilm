# SPDX-License-Identifier: GPL-3.0-or-later
"""High-level scanner façade."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Self

from pyopticfilm.advanced import AdvancedRegisters
from pyopticfilm.asic.status import ScannerStatus
from pyopticfilm.device.protocol import ScanMethod
from pyopticfilm.device.select import (
    FilmModel,
    create_asic,
    model_for_device,
    model_is_scan_ready,
)
from pyopticfilm.exceptions import AsicError, PlustekError
from pyopticfilm.image import ScanImage
from pyopticfilm.logging import get_logger
from pyopticfilm.scan.calibrate import CalibEntry, Calibrator, default_cache_path
from pyopticfilm.scan.exposure_override import validate_manual_exposure
from pyopticfilm.usb.device import UsbDeviceHandle
from pyopticfilm.usb.fake import FakeDeviceHandle, MockScannerTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol, UsbTransport

logger = get_logger(__name__)

# GL128 prime (discarded first image pass): fixed 600 dpi over a small top
# crop for all GL128 models. Measured on the OpticFilm 8100 V2: one such pass
# (~5 s, constant) lands the first retained scan within ~1 px @1200 of the
# steady-state position, versus ~30 px with no prime. A full-frame prime
# costs scale with the requested PPI (~24 s @1200, ~71 s @3600, ~150 s @7200);
# the AGOHOME park does not.
#
# The discarded pass always forces geometry=None (so dpi/area take effect),
# apply_calib=False, and mode=color — caller geometry/calib/mode must not
# stretch the prime into a full request-PPI shading+scan cycle.
#
# Override with POF_GL128_PRIME:
#   unset/empty         -> default small pass (600 dpi, area (0, 0, 1, 0.12))
#   full                -> full pass at the requested PPI and area
#   <dpi>:x0,y0,x1,y1   -> custom pass, e.g. 600:0.0,0.0,1.0,0.12
#
# Scanner.scan(gl128_prime=False) skips the pass entirely for that call
# (expect ~30 px of first-scan position drift on models where priming is
# actually needed). It does not mark the scanner as primed, so a later call
# without the override still primes if the model's default is on. Leave
# gl128_prime unset (None) to use the model's own default
# (Model.default_gl128_prime) — False for both GL128 models (8200i SE and
# 8100 V2). Explicit gl128_prime=True still primes.
_PRIME_ENV = "POF_GL128_PRIME"
_PRIME_DEFAULT = (600, (0.0, 0.0, 1.0, 0.12))


def _gl128_prime_spec() -> tuple[int, tuple[float, float, float, float] | None]:
    """(resolution, area) for the discarded GL128 prime pass.

    A resolution of 0 or area of ``None`` means "use the requested values"
    (full pass at the caller's PPI/area).
    """
    raw = os.environ.get(_PRIME_ENV, "").strip()
    if not raw:
        return _PRIME_DEFAULT
    if raw.lower() == "full":
        return 0, None
    if ":" in raw:
        dpi_part, area_part = raw.split(":", 1)
        x0, y0, x1, y1 = (float(v) for v in area_part.split(","))
        return int(dpi_part), (x0, y0, x1, y1)
    raise ValueError(f"POF_GL128_PRIME must be 'full' or '<dpi>:x0,y0,x1,y1', got {raw!r}")


ScanMode = Literal["color", "infrared", "gray"]
ScanStatus = Literal["priming", "prime_skipped", "scanning"]


class Scanner:
    """User-facing entry point for OpticFilm scanners (hardware-tested GL128)."""

    def __init__(
        self,
        handle: UsbDeviceHandle,
        protocol: GenesysUsbProtocol | None = None,
        asic: Any | None = None,
        *,
        model: FilmModel | None = None,
        calib_cache: Path | None = None,
    ) -> None:
        self._handle = handle
        self._protocol = protocol or GenesysUsbProtocol(handle)
        self._model = model or model_for_device(
            handle.info.product_id,
            getattr(handle.info, "bcd_device", 0),
        )
        self._asic = asic or create_asic(self._protocol, self._model)
        self._advanced = AdvancedRegisters(self._protocol)
        self._calibrator = Calibrator(
            self._asic,
            cache_path=calib_cache if calib_cache is not None else default_cache_path(),
            model=self._model,  # type: ignore[arg-type]
        )
        self._closed = False
        self._last_me_debug = None
        self._last_align_shift_ir = None
        #: Lab / session may disarm GL128 briefly for stationary shading.
        self._bringup_motor_armed = bool(model_is_scan_ready(self._model))
        #: When True, scan/home/park are allowed even if ``scan_ready`` is False.
        #: Set only by :meth:`open_fake` (mock USB). Real hardware stays gated.
        self._allow_unvalidated_scan = False
        #: GL128's first image pass after open establishes the repeatable AGOHOME
        #: park position. It is discarded before the first user-visible scan.
        self._gl128_primed = False

    @classmethod
    def open(
        cls,
        device_id: str | None = None,
        *,
        calib_cache: Path | None = None,
    ) -> Self:
        """Open a scan-ready device when present, else the first matching OpticFilm."""
        handle = UsbDeviceHandle.open(device_id)
        scanner = cls(handle, calib_cache=calib_cache)
        logger.info(
            "Scanner open: %s model=%s asic=%s scan_ready=%s",
            handle.info.device_id,
            scanner._model.name,
            scanner._model.asic,
            model_is_scan_ready(scanner._model),
        )
        return scanner

    @classmethod
    def open_fake(
        cls,
        model: FilmModel,
        transport: UsbTransport | None = None,
        *,
        calib_cache: Path | None = None,
    ) -> Self:
        """Open ``model`` against a mock USB device (no hardware, no ``scan_ready``).

        Does not change ``model.scan_ready``. Real :meth:`open` stays gated.
        """
        inner = transport if transport is not None else MockScannerTransport()
        handle = FakeDeviceHandle.for_model(model)
        protocol = GenesysUsbProtocol(inner)
        scanner = cls(handle, protocol, model=model, calib_cache=calib_cache)
        scanner._allow_unvalidated_scan = True
        scanner._bringup_motor_armed = True
        if hasattr(scanner._asic, "_motor_moves_enabled"):
            scanner._asic._motor_moves_enabled = True
        logger.info(
            "Scanner open_fake: model=%s asic=%s (mock USB)",
            scanner._model.name,
            scanner._model.asic,
        )
        return scanner

    def close(self) -> None:
        if not self._closed:
            try:
                if self._asic._initialized:
                    self._asic.lamp_off()
                    self._asic.stop_motor()
            except Exception as exc:  # noqa: BLE001
                logger.debug("close cleanup: %s", exc)
            self._handle.close()
            self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def device_id(self) -> str:
        return self._handle.info.device_id

    @property
    def model(self) -> FilmModel:
        return self._model

    @property
    def protocol(self) -> GenesysUsbProtocol:
        self._ensure_open()
        return self._protocol

    @property
    def asic(self) -> Any:
        self._ensure_open()
        return self._asic

    @property
    def advanced(self) -> AdvancedRegisters:
        """Low-level register access (debug / bring-up only)."""
        self._ensure_open()
        return self._advanced

    @property
    def calibrator(self) -> Calibrator:
        self._ensure_open()
        return self._calibrator

    def status(self) -> ScannerStatus:
        self._ensure_open()
        return self._asic.read_status_reliable()

    def warmup(self, *, home: bool = True, lamp: bool = True) -> None:
        """ASIC boot, frontend init, optional home + lamp on."""
        self._ensure_scan_ready()
        self._ensure_open()
        self._asic.init()
        if home:
            self._asic.home()
        if lamp:
            self._asic.set_scan_method("transparency")
            self._asic.lamp_on()

    def lamp_on(self, method: ScanMethod = "transparency") -> None:
        """Turn the lamp on. Allowed on probe-only models that implement lamp."""
        self._ensure_open()
        self._asic.set_scan_method(method)
        self._asic.lamp_on()

    def lamp_off(self) -> None:
        self._ensure_open()
        self._asic.lamp_off()

    def home(self, *, timeout_s: float = 30.0) -> None:
        self._ensure_scan_ready()
        self._ensure_open()
        self._asic.home(timeout_s=timeout_s)

    def park(self, *, timeout_s: float = 30.0) -> None:
        self._ensure_scan_ready()
        self._ensure_open()
        self._asic.park(timeout_s=timeout_s)

    def calibrate(
        self,
        *,
        resolution: int = 1800,
        mode: ScanMode = "color",
        force: bool = False,
        area: tuple[float, float, float, float] | None = None,
        geometry: object | None = None,
    ) -> CalibEntry:
        """Run dark/white shading (or IR white-only) and update the cache.

        GL128 AFE/ASIC shading runs with the motor disarmed, then re-arms so
        the following image feed can move.
        """
        self._ensure_scan_ready()
        self._ensure_open()
        if not self._asic._initialized:
            self._asic.init()
            if not self._asic.is_at_home():
                self._asic.home()
        was_armed = self._bringup_motor_armed
        try:
            if getattr(self._model, "asic", "") == "GL128":
                self.disarm_bringup_motor()
            return self._calibrator.run(
                resolution=resolution,
                mode=mode,
                force=force,
                area=area,
                geometry=geometry,  # type: ignore[arg-type]
            )
        finally:
            if was_armed:
                self.arm_bringup_motor()

    @property
    def last_me_debug(self):
        """Bracket planes / IVW stats from the last ME scan (GL128 only), or ``None``."""
        return self._last_me_debug

    @property
    def last_align_shift_ir(self) -> tuple[float, float] | None:
        """IR→colour-short alignment shift from the last multi-pass scan, or ``None``."""
        return self._last_align_shift_ir

    def scan(
        self,
        *,
        resolution: int = 1800,
        mode: ScanMode = "color",
        area: tuple[float, float, float, float] | None = None,
        geometry: object | None = None,
        progress: Callable[[float], None] | None = None,
        cancel: threading.Event | None = None,
        on_status: Callable[[ScanStatus], None] | None = None,
        apply_calib: bool = True,
        multi_exposure: bool = False,
        infrared: bool = False,
        align_passes: bool = True,
        me_exposure_mode: str | None = None,
        single_pass_exposure: int | None = None,
        me_short_exposure: int | None = None,
        me_long_exposure: int | None = None,
        me_target_exposure: int | None = None,
        gl128_prime: bool | None = None,
        n_brackets: int = 2,
    ) -> ScanImage:
        # Fail fast on bad manual-exposure input before touching the ASIC.
        validate_manual_exposure(single_pass_exposure, label="single_pass_exposure")
        validate_manual_exposure(me_short_exposure, label="me_short_exposure")
        validate_manual_exposure(me_long_exposure, label="me_long_exposure")
        validate_manual_exposure(me_target_exposure, label="me_target_exposure")
        if me_long_exposure is not None and me_target_exposure is not None:
            raise ValueError(
                "me_long_exposure (unrestricted debug override) and "
                "me_target_exposure (model-envelope-clamped) are mutually "
                "exclusive — pass only one."
            )
        if not (2 <= n_brackets <= 9):
            raise ValueError(f"n_brackets must be between 2 and 9, got {n_brackets!r}")
        self._ensure_scan_ready()
        self._ensure_open()
        if not self._asic._initialized:
            self._asic.init()
            if not self._asic.is_at_home():
                self._asic.home()
        from pyopticfilm.scan.session import create_session

        run_kwargs = {
            "resolution": resolution,
            "mode": mode,
            "area": area,
            "geometry": geometry,
            "progress": progress,
            "cancel": cancel,
            "apply_calib": apply_calib,
            "multi_exposure": multi_exposure,
            "infrared": infrared,
            "align_passes": align_passes,
            "me_exposure_mode": me_exposure_mode,
            "single_pass_exposure": single_pass_exposure,
            "me_short_exposure": me_short_exposure,
            "me_long_exposure": me_long_exposure,
            "me_target_exposure": me_target_exposure,
            "n_brackets": n_brackets,
        }
        if gl128_prime is None:
            gl128_prime = bool(getattr(self._model, "default_gl128_prime", False))
        if getattr(self._model, "asic", "") == "GL128" and not self._gl128_primed:
            if not gl128_prime:
                # Debug/testing only: skip the discarded pass for this call.
                # _gl128_primed stays False, so a later call without the
                # override still primes normally.
                logger.info(
                    "GL128 priming pass skipped (gl128_prime=False, debug/testing "
                    "override) — first retained scan position may drift ~30px"
                )
                if on_status is not None:
                    on_status("prime_skipped")
            else:
                prime_dpi, prime_area = _gl128_prime_spec()
                if prime_area is None:
                    prime_area = area
                if prime_dpi == 0:
                    prime_dpi = resolution
                logger.info(
                    "GL128 priming pass (%s dpi, area=%s): discard first image to establish AGOHOME park "
                    "(ignores caller geometry/calib/mode)",
                    prime_dpi,
                    prime_area,
                )
                if on_status is not None:
                    on_status("priming")
                # Do not spread caller kwargs: geometry= would ignore dpi/area, and
                # apply_calib=True can add a cold shading cycle before the park.
                prime_kwargs = {
                    "resolution": prime_dpi,
                    "mode": "color",
                    "area": prime_area,
                    "geometry": None,
                    "progress": None,
                    "cancel": None,
                    "apply_calib": False,
                    "multi_exposure": False,
                    "infrared": False,
                    "align_passes": align_passes,
                }
                prime_session = create_session(self._asic, self._model, self._calibrator)
                prime_session.run(**prime_kwargs)  # type: ignore[arg-type]
                self._gl128_primed = True

        if on_status is not None:
            on_status("scanning")
        session = create_session(self._asic, self._model, self._calibrator)
        image = session.run(**run_kwargs)  # type: ignore[arg-type]
        self._last_me_debug = getattr(session, "last_me_debug", None)
        self._last_align_shift_ir = getattr(session, "last_align_shift_ir", None)
        return image

    def arm_bringup_motor(self) -> None:
        """Enable GL128 motor moves (default on for scan-ready GL128).

        Lab also uses this to re-arm after :meth:`disarm_bringup_motor` around
        stationary IR shading.
        """
        self._ensure_open()
        self._bringup_motor_armed = True
        if hasattr(self._asic, "_motor_moves_enabled"):
            self._asic._motor_moves_enabled = True
        logger.debug("Motor armed for %s", self._model.model)

    def disarm_bringup_motor(self) -> None:
        """Temporarily disable GL128 motor moves (stationary shading safety)."""
        self._bringup_motor_armed = False
        if hasattr(self._asic, "_motor_moves_enabled"):
            self._asic._motor_moves_enabled = False

    def _ensure_scan_ready(self) -> None:
        if getattr(self, "_allow_unvalidated_scan", False):
            return
        if self._bringup_motor_armed and getattr(self._model, "asic", "") == "GL128":
            return
        if not model_is_scan_ready(self._model):
            raise AsicError(
                f"{self._model.model} ({self._model.asic}) is locked out in this "
                "release: only OpticFilm 8200i SE (07b3:1825) and OpticFilm 8100 "
                "(V2) (07b3:1824) are validated for scanning. Open, status, lamp "
                "and register dumps still work."
            )

    def _ensure_open(self) -> None:
        if self._closed or not self._handle.is_open:
            raise PlustekError("Scanner is closed.")
