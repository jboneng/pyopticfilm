# SPDX-License-Identifier: GPL-3.0-or-later
"""Plustek USB driver — raw access for OpticFilm (Genesys) scanners."""

from __future__ import annotations

from pyopticfilm._version import __version__
from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2, Model8100V2
from pyopticfilm.device.model_8200i import MODEL_8200I, Model8200i
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE, Model8200iSE
from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE, Model8300iSE
from pyopticfilm.device.select import KNOWN_MODELS
from pyopticfilm.exceptions import (
    AsicError,
    CalibrationError,
    DeviceNotFoundError,
    DriverBindingError,
    MotorTimeoutError,
    PlustekError,
    ScanCancelled,
    ScanError,
    UnsupportedDeviceError,
    UsbError,
)
from pyopticfilm.image import ScanImage
from pyopticfilm.scanner import Scanner, ScanStatus

__all__ = [
    "KNOWN_MODELS",
    "MODEL_8100_V2",
    "MODEL_8200I",
    "MODEL_8200I_SE",
    "MODEL_8300I_SE",
    "AsicError",
    "CalibrationError",
    "DeviceNotFoundError",
    "DriverBindingError",
    "Model8100V2",
    "Model8200i",
    "Model8200iSE",
    "Model8300iSE",
    "MotorTimeoutError",
    "PlustekError",
    "ScanCancelled",
    "ScanError",
    "ScanImage",
    "ScanStatus",
    "Scanner",
    "UnsupportedDeviceError",
    "UsbError",
    "__version__",
]
