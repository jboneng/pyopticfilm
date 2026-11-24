# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen 8200i exposure clamps — do not retarget to match new code."""

from __future__ import annotations

import numpy as np
import pytest

from pyopticfilm.asic.gl845 import Gl845
from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.scan.session import ScanSession
from pyopticfilm.usb.fake import MockScannerTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol


@pytest.fixture
def session():
    asic = Gl845(GenesysUsbProtocol(MockScannerTransport()), MODEL_8200I)
    asic.init()
    return ScanSession(asic, MODEL_8200I, None)


@pytest.mark.parametrize(
    ("requested", "used"),
    [(5000, 14000), (14000, 14000), (30000, 30000), (42000, 42000), (70000, 42000)],
)
def test_manual_single_pass_lperiod_clamp(session, requested, used):
    assert session._clamp_manual_exposure(requested) == used


@pytest.mark.parametrize(
    ("requested", "used"),
    [(10000, 28000), (28000, 28000), (42000, 42000), (90000, 42000)],
)
def test_me_long_override_clamp(session, requested, used):
    rgb = np.full((50, 50, 3), 3000, np.uint16)
    assert session._select_me_long(rgb, 14000, 1800, requested)[0] == used
