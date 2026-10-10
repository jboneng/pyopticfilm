# SPDX-License-Identifier: GPL-3.0-or-later
"""Device models and selection."""

from pyopticfilm.device.model_7200 import MODEL_7200, Model7200
from pyopticfilm.device.model_7200i import MODEL_7200_V2, MODEL_7200I, Model7200i
from pyopticfilm.device.model_7300 import MODEL_7300, MODEL_7400_V1, Model7300
from pyopticfilm.device.model_7400 import MODEL_7400, MODEL_8100, Model7400
from pyopticfilm.device.model_7500i import MODEL_7500I, Model7500i
from pyopticfilm.device.model_7600i_v1 import MODEL_7600I_V1, Model7600iV1
from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2, Model8100V2
from pyopticfilm.device.model_8200i import MODEL_8200I, Model8200i
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE, Model8200iSE
from pyopticfilm.device.protocol import AsicDriver, FilmModel, Gl128Model, MotorProfile
from pyopticfilm.device.select import (
    KNOWN_MODELS,
    MODEL_7600I_V2,
    create_asic,
    model_for_device,
    model_for_pid,
    model_is_scan_ready,
)
from pyopticfilm.device.sensor_lookup import (
    dummy_pixel_for,
    exposure_lperiod_for,
    frontend_regs_for,
    maxwd_register_value,
    sensor_regs_for,
)

__all__ = [
    "KNOWN_MODELS",
    "MODEL_7200",
    "MODEL_7200I",
    "MODEL_7200_V2",
    "MODEL_7300",
    "MODEL_7400",
    "MODEL_7400_V1",
    "MODEL_7500I",
    "MODEL_7600I_V1",
    "MODEL_7600I_V2",
    "MODEL_8100",
    "MODEL_8100_V2",
    "MODEL_8200I",
    "MODEL_8200I_SE",
    "AsicDriver",
    "FilmModel",
    "Gl128Model",
    "Model7200",
    "Model7200i",
    "Model7300",
    "Model7400",
    "Model7500i",
    "Model7600iV1",
    "Model8100V2",
    "Model8200i",
    "Model8200iSE",
    "MotorProfile",
    "create_asic",
    "dummy_pixel_for",
    "exposure_lperiod_for",
    "frontend_regs_for",
    "maxwd_register_value",
    "model_for_device",
    "model_for_pid",
    "model_is_scan_ready",
    "sensor_regs_for",
]
