# SPDX-License-Identifier: GPL-3.0-or-later
"""Frozen 7600i v2 exposure clamps and pre-feed travel — do not retarget to match new code."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from pyopticfilm.asic.gl845 import Gl845
from pyopticfilm.device.select import MODEL_7600I_V2
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.scan.session import ScanSession
from pyopticfilm.usb.fake import MockScannerTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol


@pytest.fixture
def session():
    asic = Gl845(GenesysUsbProtocol(MockScannerTransport()), MODEL_7600I_V2)
    asic.init()
    return ScanSession(asic, MODEL_7600I_V2, None)


@pytest.mark.parametrize(
    ("requested", "used"),
    [(5000, 14000), (14000, 14000), (30000, 30000), (65000, 65000), (70000, 65000)],
)
def test_manual_single_pass_lperiod_clamp(session, requested, used):
    assert session._clamp_manual_exposure(requested) == used


@pytest.mark.parametrize(
    ("requested", "used"),
    [(10000, 28000), (28000, 28000), (64400, 64400), (65000, 65000), (90000, 65000)],
)
def test_me_long_override_clamp(session, requested, used):
    rgb = np.full((50, 50, 3), 3000, np.uint16)
    assert session._select_me_long(rgb, 14000, 1800, requested)[0] == used


@pytest.mark.parametrize(
    ("dpi", "area", "pre_starty", "image_starty"),
    [
        (3600, (0.3, 0.35, 0.55, 0.55), 4879, 338),
        (7200, (0.3, 0.35, 0.55, 0.55), 4879, 338),
        (3600, None, 3639, 338),
    ],
)
def test_prefeed_stops_short_of_the_image(session, dpi, area, pre_starty, image_starty):
    calls = []
    session.acquire_raw = lambda geo, **kw: calls.append(geo) or b""
    geo = compute_geometry(dpi, model=MODEL_7600I_V2, area=area)
    out = session._prefeed(geo, method="transparency")
    (pre,) = calls
    assert (pre.resolution, pre.starty, pre.lincnt_register) == (900, pre_starty, 14)
    assert out.starty == image_starty


def test_no_prefeed_at_900_dpi(session):
    calls = []
    session.acquire_raw = lambda geo, **kw: calls.append(geo) or b""
    geo = compute_geometry(900, model=MODEL_7600I_V2)
    assert session._prefeed(geo, method="transparency") is geo
    assert not calls


def test_long_lperiod_prefeeds_at_1800(session):
    calls = []
    session.acquire_raw = lambda geo, **kw: calls.append(geo) or b""
    geo = replace(compute_geometry(1800, model=MODEL_7600I_V2), exposure_lperiod=64400)
    assert session._prefeed(geo, method="transparency").starty == 338
    assert len(calls) == 1
