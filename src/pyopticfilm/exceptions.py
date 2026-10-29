# SPDX-License-Identifier: GPL-3.0-or-later
"""Public exception hierarchy for the Plustek USB driver."""

from __future__ import annotations


class PlustekError(Exception):
    """Base error for all Plustek USB driver failures."""


class DeviceNotFoundError(PlustekError):
    """No matching OpticFilm 8200i was found on the bus."""


class UnsupportedDeviceError(PlustekError):
    """A Plustek USB device was found but is not a supported model/chipset."""


class DriverBindingError(PlustekError):
    """The OS driver does not allow raw USB access (e.g. vendor driver still bound)."""


class UsbError(PlustekError):
    """USB control/bulk transfer failure or link loss."""


class ScanError(PlustekError):
    """Scan sequencing or image assembly failed."""


class ScannerStuckError(ScanError):
    """The scanner is in a state only a power-cycle clears; do not keep driving it."""


class ScanCancelled(PlustekError):
    """Scan aborted because a cancel flag was set."""


class CalibrationError(PlustekError):
    """Dark/white/shading calibration failed or cache is invalid."""


class MotorTimeoutError(PlustekError):
    """Motor failed to reach home/park within the timeout."""


class AsicError(PlustekError):
    """ASIC initialization or control sequence failed."""
