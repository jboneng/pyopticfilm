# SPDX-License-Identifier: GPL-3.0-or-later
"""GL128 image DVDSET follows this scan's calibration, not an earlier scan's table."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from pyopticfilm.asic.registers import Gl128Registers
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.scan.calibrate import CalibEntry
from pyopticfilm.scanner import Scanner

_TINY = (0.0, 0.0, 0.08, 0.08)
_R = Gl128Registers()


def _open(tmp_path: Path) -> tuple[Scanner, list[int]]:
    """Fake 8200i SE whose colour calibration only marks the ASIC table ready.

    Returns the scanner and the ``0x01`` values of every batched register write;
    the image pass's blast is the last one (``START`` is a single write).
    """
    scanner = Scanner.open_fake(MODEL_8200I_SE, calib_cache=tmp_path / "calib.json")
    cal = scanner.calibrator
    asic = scanner.asic

    def ensure(geometry):
        asic.asic_shading_ready = True
        entry = CalibEntry(
            method="transparency",
            resolution=geometry.resolution,
            startx=geometry.startx,
            pixels=geometry.pixels,
            dark=np.zeros((geometry.pixels, 3), dtype=np.uint16),
            white=np.full((geometry.pixels, 3), 65535, dtype=np.uint16),
            asic_shading=True,
            shading_blob=b"TABLE",
            afe_offsets=(0, 0, 0),
            afe_gains=(0, 0, 0),
        )
        cal._active = entry
        cal.prefer_asic_shading = True
        return entry

    cal.ensure_colour_asic_shading = ensure  # type: ignore[method-assign]

    reg01: list[int] = []
    batched = scanner.protocol.write_registers_batched

    def spy(pairs, **kw):
        reg01.extend(v for a, v in pairs if a == _R.REG_0x01)
        return batched(pairs, **kw)

    scanner.protocol.write_registers_batched = spy  # type: ignore[method-assign]
    return scanner, reg01


def test_calibrated_scan_arms_dvdset(tmp_path: Path):
    scanner, reg01 = _open(tmp_path)
    try:
        scanner.scan(resolution=600, area=_TINY)
    finally:
        scanner.close()
    assert reg01[-1] & _R.DVDSET


def test_apply_calib_false_after_calibrated_scan_keeps_dvdset_off(tmp_path: Path):
    """The table left in RAM fits the earlier scan's window, not this one."""
    scanner, reg01 = _open(tmp_path)
    try:
        scanner.scan(resolution=600, area=_TINY)
        assert scanner.asic.asic_shading_ready is True
        scanner.scan(resolution=1200, area=_TINY, apply_calib=False)
    finally:
        scanner.close()
    assert not reg01[-1] & _R.DVDSET
