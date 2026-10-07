# SPDX-License-Identifier: GPL-3.0-or-later
"""Select model / ASIC implementation from USB product id + bcdDevice."""

from __future__ import annotations

from typing import Any

from pyopticfilm.asic.gl128 import Gl128
from pyopticfilm.asic.gl842 import Gl842
from pyopticfilm.asic.gl843 import Gl843
from pyopticfilm.asic.gl845 import Gl845
from pyopticfilm.device.model_7200 import MODEL_7200
from pyopticfilm.device.model_7200i import MODEL_7200_V2, MODEL_7200I
from pyopticfilm.device.model_7300 import MODEL_7300, MODEL_7400_V1
from pyopticfilm.device.model_7400 import MODEL_7400, MODEL_8100
from pyopticfilm.device.model_7500i import MODEL_7500I, MODEL_7600I_V1
from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE
from pyopticfilm.device.protocol import FilmModel
from pyopticfilm.exceptions import UnsupportedDeviceError
from pyopticfilm.usb.device import (
    PID_OPTICFILM_7200,
    PID_OPTICFILM_7200_V2,
    PID_OPTICFILM_7200I,
    PID_OPTICFILM_7300,
    PID_OPTICFILM_7400,
    PID_OPTICFILM_7500I,
    PID_OPTICFILM_7600I,
    PID_OPTICFILM_8100,
    PID_OPTICFILM_8100_V2,
    PID_OPTICFILM_8200I,
    PID_OPTICFILM_8200I_SE,
    PID_OPTICFILM_8300I_SE,
)
from pyopticfilm.usb.protocol import GenesysUsbProtocol

# 7600i v2 shares 8200i tables (SANE alias)
MODEL_7600I_V2 = MODEL_8200I.__class__(
    name="plustek-opticfilm-7600i-v2",
    model="OpticFilm 7600i (v2)",
    usb_product_id=PID_OPTICFILM_7600I,
)

KNOWN_MODELS: tuple[FilmModel, ...] = (
    MODEL_8200I,
    MODEL_7600I_V2,
    MODEL_7400,
    MODEL_8100,
    MODEL_7400_V1,
    MODEL_7500I,
    MODEL_7600I_V1,
    MODEL_7200I,
    MODEL_7200_V2,
    MODEL_7300,
    MODEL_7200,
    MODEL_8200I_SE,
    MODEL_8100_V2,
    MODEL_8300I_SE,
)

__all__ = [
    "KNOWN_MODELS",
    "MODEL_7600I_V2",
    "FilmModel",
    "create_asic",
    "model_for_device",
    "model_for_pid",
    "model_is_scan_ready",
]


def model_for_pid(product_id: int) -> FilmModel:
    """Resolve model from product id alone (bcdDevice defaults to 0)."""
    return model_for_device(product_id, 0)


def model_for_device(product_id: int, bcd_device: int = 0) -> FilmModel:
    """Resolve OpticFilm model from USB product id and optional bcdDevice.

    Matches SANE ``UsbDeviceEntry`` rules: exact bcd when required for
    ``0x0c3a`` / ``0x0c3b`` disambiguation.
    """
    if product_id == PID_OPTICFILM_8200I:
        return MODEL_8200I
    if product_id == PID_OPTICFILM_8200I_SE:
        return MODEL_8200I_SE
    if product_id == PID_OPTICFILM_7200:
        return MODEL_7200
    if product_id == PID_OPTICFILM_7200I:
        return MODEL_7200I
    if product_id == PID_OPTICFILM_7200_V2:
        return MODEL_7200_V2
    if product_id == PID_OPTICFILM_7300:
        return MODEL_7300
    if product_id == PID_OPTICFILM_7500I:
        return MODEL_7500I
    if product_id == PID_OPTICFILM_8100:
        return MODEL_8100
    if product_id == PID_OPTICFILM_7400:
        # bcd 0x0400 → 7400-v1 (7300 tables); bcd 0x0605 → 7400-v2
        if bcd_device == 0x0400:
            return MODEL_7400_V1
        return MODEL_7400
    if product_id == PID_OPTICFILM_7600I:
        # bcd 0x0400 → 7600i-v1 (7500i); bcd 0x0605 → 7600i-v2 (8200i)
        if bcd_device == 0x0400:
            return MODEL_7600I_V1
        return MODEL_7600I_V2
    if product_id == PID_OPTICFILM_8100_V2:
        return MODEL_8100_V2
    if product_id == PID_OPTICFILM_8300I_SE:
        return MODEL_8300I_SE
    raise UnsupportedDeviceError(f"No model mapping for product_id=0x{product_id:04x}")


def model_is_scan_ready(model: FilmModel) -> bool:
    """True for models validated for scan."""
    return bool(getattr(model, "scan_ready", False))


def create_asic(protocol: GenesysUsbProtocol, model: FilmModel) -> Any:
    """Return the ASIC driver for ``model``."""
    asic = getattr(model, "asic", "")
    if asic == "GL128":
        return Gl128(protocol, model)  # type: ignore[arg-type]
    if asic == "GL845":
        return Gl845(protocol, model)
    if asic == "GL843":
        return Gl843(protocol, model)
    if asic == "GL842":
        return Gl842(protocol, model)
    raise UnsupportedDeviceError(f"No ASIC driver for model {model.name!r} asic={asic!r}")
