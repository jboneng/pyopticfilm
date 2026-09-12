# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the GL128 per-channel exposure AHB table builder."""

from __future__ import annotations

import pytest

from pyopticfilm.device.tables_8200i_se import (
    EXPOSURE_FIRST_DELTA,
    EXPOSURE_TABLE_LENGTH,
    exposure_table,
)


def test_exposure_table_shape_in_range():
    table = exposure_table(16035)
    assert len(table) == EXPOSURE_TABLE_LENGTH
    assert table[0] == 16035 + EXPOSURE_FIRST_DELTA
    assert all(v == 16035 for v in table[1:])


def test_exposure_table_accepts_16bit_boundary():
    assert exposure_table(0)[1] == 0
    assert exposure_table(0xFFFF)[1] == 0xFFFF


@pytest.mark.parametrize("value", [0x10000, 150000, -1])
def test_exposure_table_rejects_out_of_16bit_range(value):
    """A value that doesn't fit the table's 16-bit words must fail loudly —
    silently masking it (``& 0xFFFF``) would desync this table from whatever
    was written to the 24-bit REG_EXPOSURE for the same pass."""
    with pytest.raises(ValueError, match="16-bit"):
        exposure_table(value)
