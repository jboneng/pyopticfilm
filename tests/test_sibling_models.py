# SPDX-License-Identifier: GPL-3.0-or-later
"""Sibling model smoke notes — GL845 aliases share session; GL843 swap covered in unit tests."""

from __future__ import annotations

from pyopticfilm.asic.gl845 import Gl845
from pyopticfilm.device.model_7200 import MODEL_7200
from pyopticfilm.device.model_7200i import MODEL_7200I
from pyopticfilm.device.model_7400 import MODEL_7400, MODEL_8100
from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.device.select import MODEL_7600I_V2, create_asic
from pyopticfilm.scan.session import create_session
from pyopticfilm.usb.fake import FakeUsbTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol


def test_gl845_aliases_use_gl845_asic_and_session():
    proto = GenesysUsbProtocol(FakeUsbTransport())
    for model in (MODEL_8200I, MODEL_8100, MODEL_7400, MODEL_7600I_V2):
        asic = create_asic(proto, model)
        assert isinstance(asic, Gl845)
        session = create_session(asic, model, None)
        assert session.__class__.__name__ == "ScanSession"
        # The 7600i v2 is hardware-tested; the other GL845 aliases stay locked.
        assert model.scan_ready is (model is MODEL_7600I_V2)


def test_gl843_7200i_swap_flag_and_gl842_last():
    assert MODEL_7200I.asic == "GL843"
    assert MODEL_7200I.swap_16bit_data is True
    assert MODEL_7200.asic == "GL842"
    assert MODEL_7200.scan_ready is False
    assert MODEL_7200I.scan_ready is False
