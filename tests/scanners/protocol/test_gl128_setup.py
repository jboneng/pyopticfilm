# SPDX-License-Identifier: GPL-3.0-or-later
"""GL128 OpticFilm setup register goldens over MockScannerTransport (no hardware)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE
from scanners.setup_gl128 import run_gl128_setup
from scanners.trace_compare import (
    GL128_OPTICAL_REGISTER_KEYS,
    compare_registers,
    load_trace,
)

TRACE_ROOT = Path(__file__).resolve().parents[2] / "traces" / "python"
_CASES = (
    ("8200i_se", MODEL_8200I_SE, 1200),
    ("8200i_se", MODEL_8200I_SE, 1800),
    ("8200i_se", MODEL_8200I_SE, 7200),
    ("8100_v2", MODEL_8100_V2, 1200),
    ("8100_v2", MODEL_8100_V2, 1800),
    ("8100_v2", MODEL_8100_V2, 7200),
    ("8300i_se", MODEL_8300I_SE, 1200),
    ("8300i_se", MODEL_8300I_SE, 1800),
    ("8300i_se", MODEL_8300I_SE, 7200),
)


def _u24(regs: dict[int, int], addr: int) -> int:
    return ((regs.get(addr, 0) & 0xFF) << 16) | ((regs.get(addr + 1, 0) & 0xFF) << 8) | (
        regs.get(addr + 2, 0) & 0xFF
    )


@pytest.mark.parametrize(("folder", "model", "dpi"), _CASES)
def test_gl128_setup_optical_registers_match_geometry(folder, model, dpi):
    usb, geo = run_gl128_setup(model, dpi=dpi)
    assert _u24(usb.registers, 0x25) == geo.lincnt_register
    assert _u24(usb.registers, 0x28) == model.line_period_for(dpi)
    assert _u24(usb.registers, 0x82) == geo.pixel_startx
    assert _u24(usb.registers, 0x85) == geo.pixel_endx
    dpiset = (usb.registers.get(0x2C, 0) << 8) | usb.registers.get(0x2D, 0)
    assert dpiset == geo.register_dpiset


@pytest.mark.parametrize(("folder", "model", "dpi"), _CASES)
def test_gl128_setup_matches_golden_registers(folder, model, dpi):
    golden_path = TRACE_ROOT / folder / f"{dpi}_rgb16_setup.json"
    if not golden_path.is_file():
        pytest.fail(f"missing golden register program {golden_path}")
    usb, _geo = run_gl128_setup(model, dpi=dpi)
    golden = load_trace(golden_path)
    written = {addr: val & 0xFF for addr, val in usb.registers.items()}
    diff = compare_registers(golden.registers, written, keys=GL128_OPTICAL_REGISTER_KEYS)
    assert diff is None, diff
