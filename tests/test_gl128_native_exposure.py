# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan motor table for GL128 image passes at native 7200 dpi."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pyopticfilm.asic.registers import Gl128Registers
from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE
from pyopticfilm.device.select import create_asic
from pyopticfilm.exceptions import ScanError
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.scan.session_gl128 import (
    Gl128ScanSession,
    gl128_multi_pass_allowed,
    native_line_floor,
)
from pyopticfilm.scanner import Scanner
from pyopticfilm.usb.fake import MockScannerTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol

_TINY = (0.0, 0.0, 0.08, 0.08)


def _image_regs(model, dpi: int) -> dict[int, int]:
    asic = MagicMock()
    asic._motor_moves_enabled = False
    asic.last_afe = None
    asic.asic_shading_ready = True
    session = Gl128ScanSession(asic, model)
    session.se_regs = Gl128Registers()
    session._lamp_requested = True
    session._configure(compute_geometry(dpi, model=model, area=None))
    return dict(asic.protocol.write_registers_batched.call_args[0][0])


@pytest.mark.parametrize("model", [MODEL_8200I_SE, MODEL_8100_V2, MODEL_8300I_SE])
@pytest.mark.parametrize(("dpi", "reg_1d"), [(3600, 0x80), (7200, 0x81)])
def test_image_reg_0x1d_matches_vendor(model, dpi, reg_1d):
    assert _image_regs(model, dpi)[0x1D] == reg_1d


def _configure(model, dpi: int, **pass_state) -> tuple[int, int]:
    asic = MagicMock()
    asic._motor_moves_enabled = False
    asic.last_afe = None
    asic.asic_shading_ready = True
    asic._scan_method = "color"
    session = Gl128ScanSession(asic, model)
    session.se_regs = Gl128Registers()
    session._lamp_requested = True
    for key, value in pass_state.items():
        setattr(session, key, value)
    session._configure(compute_geometry(dpi, model=model, area=None))
    regs = dict(asic.protocol.write_registers_batched.call_args[0][0])
    reg_exposure = (regs[0x7D] << 16) | (regs[0x7E] << 8) | regs[0x7F]
    return asic.upload_tables.call_args.kwargs["channel_exposure"], reg_exposure


@pytest.mark.parametrize(("dpi", "expected"), [(1200, 2333), (2400, 4666), (3600, 7000), (7200, 28000)])
def test_se_image_table_matches_silverfast_ladder(dpi, expected):
    channel, reg_exposure = _configure(MODEL_8200I_SE, dpi)
    assert channel == expected
    assert reg_exposure == 14000


@pytest.mark.parametrize("manual", [28000, 14000, 10000])
def test_se_manual_override_is_not_floored(manual):
    channel, reg_exposure = _configure(MODEL_8200I_SE, 7200, _pass_exposure=manual, _pass_manual=True)
    assert (channel, reg_exposure) == (manual, manual)


@pytest.mark.parametrize("long_exposure", [42000, 64000])
def test_se_me_long_pass_above_floor_is_unchanged(long_exposure):
    channel, reg_exposure = _configure(
        MODEL_8200I_SE, 7200, _pass_exposure=long_exposure, _pass_long_exposure=True
    )
    assert (channel, reg_exposure) == (long_exposure, long_exposure)


@pytest.mark.parametrize(
    ("model", "expected"),
    [(MODEL_8200I_SE, 28000), (MODEL_8100_V2, 28000), (MODEL_8300I_SE, 15000)],
)
def test_native_line_floor_per_model(model, expected):
    assert native_line_floor(model) == expected


def _session_pass_tables(dpi: int, **run_kwargs) -> list[tuple[int, int]]:
    """``(channel table word, REG_EXPOSURE)`` for every image pass of a mocked run."""
    usb = MockScannerTransport()
    asic = create_asic(GenesysUsbProtocol(usb), MODEL_8200I_SE)
    asic._motor_moves_enabled = True
    asic.init()
    session = Gl128ScanSession(asic, MODEL_8200I_SE)
    passes: list[tuple[int, int]] = []
    orig_upload = asic.upload_tables

    def spy(*args, **kwargs):
        if not kwargs.get("shading"):
            reg = session._pass_exposure or MODEL_8200I_SE.exposure_short
            passes.append((int(kwargs["channel_exposure"]), int(reg)))
        return orig_upload(*args, **kwargs)

    asic.upload_tables = spy
    session.run(resolution=dpi, area=_TINY, apply_calib=False, **run_kwargs)
    return passes


@pytest.mark.parametrize(
    "run_kwargs",
    [{"multi_exposure": True}, {"n_passes": 2}, {"multi_exposure": True, "n_passes": 2}],
)
def test_se_me_and_multi_pass_rejected_at_7200_before_any_pass(run_kwargs):
    passes: list[tuple[int, int]] = []
    with pytest.raises(ScanError, match="7200 dpi"):
        passes = _session_pass_tables(7200, **run_kwargs)
    assert passes == []


@pytest.mark.parametrize("model", [MODEL_8200I_SE, MODEL_8100_V2, MODEL_8300I_SE])
def test_multi_pass_allowed_below_native_only(model):
    assert not gl128_multi_pass_allowed(model, 7200)
    assert all(gl128_multi_pass_allowed(model, dpi) for dpi in model.resolutions_dpi if dpi != 7200)
    below_native = tuple(dpi for dpi in model.resolutions_dpi if dpi != 7200)
    assert model.multi_exposure_resolutions_dpi == below_native
    assert model.multi_pass_resolutions_dpi == below_native


@pytest.mark.parametrize("run_kwargs", [{"multi_exposure": True}, {"n_passes": 2}])
def test_scanner_rejects_7200_me_before_opening_asic(run_kwargs):
    scanner = Scanner.open_fake(MODEL_8200I_SE)
    try:
        with pytest.raises(ScanError, match="7200 dpi"):
            scanner.scan(resolution=7200, area=_TINY, apply_calib=False, **run_kwargs)
        assert not scanner._asic._initialized
    finally:
        scanner.close()


def test_se_single_pass_at_7200_still_floored():
    assert _session_pass_tables(7200) == [(28000, 14000)]


def test_se_me_below_7200_unchanged():
    passes = _session_pass_tables(3600, multi_exposure=True)
    assert len(passes) == 2
    assert passes[0] == (7000, 14000)
    long_ch, long_reg = passes[1]
    assert long_ch == long_reg // 2


@pytest.mark.parametrize(
    ("dpi", "expected"),
    [(600, 625), (900, 937), (1200, 1250), (1800, 1875), (2400, 2500), (3600, 3750), (7200, 15000)],
)
def test_8300i_se_motor_period_matches_silverfast_captures(dpi, expected):
    channel, reg_exposure = _configure(MODEL_8300I_SE, dpi)
    assert (channel, reg_exposure) == (expected, 15000)


@pytest.mark.parametrize(("dpi", "expected"), [(1200, 5000), (3600, 15000)])
def test_8300i_se_me_long_motor_period_matches_silverfast_captures(dpi, expected):
    assert MODEL_8300I_SE.channel_exposure_for(dpi, exposure=60000) == expected


def test_8100_v2_image_table_at_7200():
    channel, reg_exposure = _configure(MODEL_8100_V2, 7200)
    assert (channel, reg_exposure) == (28000, 14000)
