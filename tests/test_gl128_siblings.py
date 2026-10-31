# SPDX-License-Identifier: GPL-3.0-or-later
"""Sibling-diff catalog: every GL128 field is shared-identical or declared divergent."""

from __future__ import annotations

from pyopticfilm.device.gl128_common import (
    ALL_PPI,
    GL128_DIVERGENT_FIELDS,
    GL128_SHARED_FIELDS,
    INIT_REGS,
    LPERIOD_BY_DPI,
    MEMORY_LAYOUT_REGS,
    REGISTER_DPISET,
    Gl128Common,
    dataclass_field_names,
)
from pyopticfilm.device.model_135i import MODEL_135I, Model135i
from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2, Model8100V2
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE, Model8200iSE
from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE, Model8300iSE
from pyopticfilm.device.protocol import Gl128Model

_LEAVES = (MODEL_8200I_SE, MODEL_8100_V2, MODEL_8300I_SE, MODEL_135I)
_LEAF_CLASSES = (Model8200iSE, Model8100V2, Model8300iSE, Model135i)
_FLATBED_LEAVES = (MODEL_8200I_SE, MODEL_8100_V2, MODEL_8300I_SE)


def _value(model: object, name: str) -> object:
    value = getattr(model, name)
    if hasattr(value, "items"):
        return dict(value)
    return value


def test_gl128_common_fields_match_shared_catalog():
    assert dataclass_field_names(Gl128Common) == GL128_SHARED_FIELDS


def test_leaf_extra_fields_are_exactly_the_divergent_catalog():
    for cls in _LEAF_CLASSES:
        extra = dataclass_field_names(cls) - GL128_SHARED_FIELDS
        assert extra == GL128_DIVERGENT_FIELDS, cls.__name__


def test_siblings_do_not_subclass_each_other():
    assert not issubclass(Model8100V2, Model8200iSE)
    assert not issubclass(Model8300iSE, Model8200iSE)
    assert not issubclass(Model8300iSE, Model8100V2)
    assert not isinstance(MODEL_8100_V2, Model8200iSE)
    assert not isinstance(MODEL_8300I_SE, Model8200iSE)
    assert not isinstance(MODEL_8300I_SE, Model8100V2)
    for sibling in (Model8200iSE, Model8100V2, Model8300iSE):
        assert not issubclass(Model135i, sibling)
        assert not isinstance(MODEL_135I, sibling)
    for model in _LEAVES:
        assert isinstance(model, Gl128Common)
        assert isinstance(model, Gl128Model)


def test_shared_fields_are_equal_on_all_siblings():
    for name in sorted(GL128_SHARED_FIELDS):
        values = [_value(m, name) for m in _LEAVES]
        assert all(v == values[0] for v in values), name


def test_fields_split_for_the_135i_keep_the_flatbed_values():
    # Declared per leaf since the 135i was added; the 8200i SE / 8100 V2 /
    # 8300i SE keep the values they shared before.
    for model in _FLATBED_LEAVES:
        assert model.resolutions_dpi == ALL_PPI
        assert dict(model.register_dpiset_by_dpi) == REGISTER_DPISET
        assert dict(model.lperiod_by_dpi) == LPERIOD_BY_DPI
        assert model.feed_steps_per_inch == 14400
        assert dict(model.init_regs) == INIT_REGS
        assert dict(model.memory_layout_regs) == MEMORY_LAYOUT_REGS
        assert len(model.boot_blobs()) == 2
        assert model.boot_writes_layout_and_gpo() is True
        assert model.boot_vendor_steps() == ()
        assert model.boot_uploads_tables() is True


def test_135i_divergent_fields_match_capture_catalog():
    m = MODEL_135I
    assert m.name == "plustek-opticfilm-135i"
    assert m.model == "OpticFilm 135i"
    assert m.usb_product_id == 0x1436
    assert m.supports_infrared is True
    assert m.scan_ready is False
    assert m.resolutions_dpi == (7200, 3600, 1800, 1200, 900, 720, 600, 300, 150)
    # vuescan open: 29 values differ from the SE boot blast, plus 0x114/0x115.
    se_diff = {a for a in INIT_REGS if INIT_REGS[a] != m.init_regs[a]}
    assert len(se_diff) == 29
    assert set(m.init_regs) - set(INIT_REGS) == {0x114, 0x115}
    assert (m.asic_clock_boot_0x8c, m.asic_clock_scan_0x8c, m.asic_boot_0x0b) == (0x0D, 0x0E, 0x6C)
    assert (m.memory_layout_regs[0xD0], m.memory_layout_regs[0xD1]) == (0x0A, 0x1A)
    assert m.feed_steps_per_inch == 7200
    assert (m.exposure_lperiod, m.exposure_short) == (14000, 14000)
    assert m.image_lincnt_per_line == 2
    assert m.usb_image_lincnt_half_lines is False
    assert dict(m.shading_linesel_by_dpi) == {}
    for dpi, (dpiset, lperiod, dummy, clk) in {
        600: (200, 11023, 0x0A, 0x23),
        1200: (400, 11239, 0x0D, 0x0B),
        7200: (1200, 15991, 0x17, 0x01),
    }.items():
        assert m.register_dpiset_by_dpi[dpi] == dpiset
        assert m.lperiod_by_dpi[dpi] == lperiod
        assert m.line_period_for(dpi) == lperiod
        assert m.dummy_by_dpi[dpi] == dummy
        assert m.pixel_clock_for_image(dpi) == clk
    assert m.slope_table_fast()[0] == 64817
    assert m.slope_table_slow()[0] == 34482
    assert m.boot_blobs() == ()
    assert m.boot_writes_layout_and_gpo() is False
    assert m.boot_uploads_tables() is False


def test_divergent_fields_match_capture_catalog():
    assert MODEL_8200I_SE.name == "plustek-opticfilm-8200i-se"
    assert MODEL_8100_V2.name == "plustek-opticfilm-8100-v2"
    assert MODEL_8300I_SE.name == "plustek-opticfilm-8300i-se"
    assert MODEL_8200I_SE.model == "OpticFilm 8200i SE"
    assert MODEL_8100_V2.model == "OpticFilm 8100 (V2)"
    assert MODEL_8300I_SE.model == "OpticFilm 8300i SE"
    assert MODEL_8200I_SE.usb_product_id == 0x1825
    assert MODEL_8100_V2.usb_product_id == 0x1824
    assert MODEL_8300I_SE.usb_product_id == 0x181F
    assert MODEL_8200I_SE.supports_infrared is True
    assert MODEL_8100_V2.supports_infrared is False
    assert MODEL_8300I_SE.supports_infrared is True
    assert MODEL_8200I_SE.scan_ready is True
    assert MODEL_8100_V2.scan_ready is True
    assert MODEL_8300I_SE.scan_ready is True
    assert MODEL_8200I_SE.feed_to_scan_steps == 13128
    assert MODEL_8100_V2.feed_to_scan_steps == 13128
    assert MODEL_8300I_SE.feed_to_scan_steps == 13128
    assert MODEL_8200I_SE.ladder_feed2_steps == 13560
    assert MODEL_8100_V2.ladder_feed2_steps == 13128
    assert MODEL_8300I_SE.ladder_feed2_steps == 13128
    assert MODEL_8200I_SE.lperiod_by_dpi[7200] == 16035
    assert MODEL_8100_V2.lperiod_by_dpi[7200] == 16035
    assert MODEL_8300I_SE.lperiod_by_dpi[7200] == 16035
    assert MODEL_8300I_SE.lperiod_by_dpi[1200] == 11283
    assert MODEL_8200I_SE.exposure_lperiod == 14000
    assert MODEL_8100_V2.exposure_lperiod == 14000
    assert MODEL_8300I_SE.exposure_lperiod == 15000
    assert MODEL_8300I_SE.exposure_short == 15000
    assert (MODEL_8200I_SE.asic_clock_boot_0x8c, MODEL_8200I_SE.asic_clock_scan_0x8c) == (0x0C, 0x0C)
    assert (MODEL_8100_V2.asic_clock_boot_0x8c, MODEL_8100_V2.asic_clock_scan_0x8c) == (0x0E, 0x0C)
    assert (MODEL_8300I_SE.asic_clock_boot_0x8c, MODEL_8300I_SE.asic_clock_scan_0x8c) == (0x0E, 0x0D)
    assert MODEL_8200I_SE.asic_boot_0x0b == 0x4C
    assert MODEL_8100_V2.asic_boot_0x0b == 0x8C
    assert MODEL_8300I_SE.asic_boot_0x0b == 0x8C
    assert MODEL_8300I_SE.dummy_by_dpi[7200] == 0x1F
    assert MODEL_8300I_SE.pixel_clock_by_dpi[2400] == 0x05
    assert MODEL_8300I_SE.shading_strip_clocks(7200, dvdset=True) == (0x26, 0x02, 0x02)
    assert MODEL_8300I_SE.shading_strip_clocks(7200, dvdset=False) == (0x17, 0x01, 0x30)
    # CUSTOM slope ROM (docs §10.6); SE/V2 keep capture heads 0x1FB4 / 0x16DE.
    assert MODEL_8200I_SE.slope_table_slow()[0] == 0x1FB4
    assert MODEL_8200I_SE.slope_table_fast()[0] == 0x16DE
    assert MODEL_8100_V2.slope_table_slow()[0] == 0x1FB4
    assert MODEL_8100_V2.slope_table_fast()[0] == 0x16DE
    assert MODEL_8300I_SE.slope_table_slow()[0] == 0x32BB
    assert MODEL_8300I_SE.slope_table_fast()[0] == 0x846A
    assert MODEL_8300I_SE.slope_table_slow() != MODEL_8200I_SE.slope_table_slow()
    assert MODEL_8300I_SE.slope_table_fast() != MODEL_8200I_SE.slope_table_fast()
    # Colour-short feed2 is PPI-dependent on 8300i; SE/V2 stay constant.
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(150) == 13040
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(300) == 13040
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(600) == 13040
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(900) == 13096
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(1200) == 13112
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(1800) == 13122
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(2400) == 13124
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(3600) == 13126
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(7200) == 13128
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(1200, long_exposure=True) == 13128
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(3600, long_exposure=True) == 13128
    assert MODEL_8200I_SE.feed_to_scan_steps_for_dpi(1200) == MODEL_8200I_SE.feed_to_scan_steps
    assert MODEL_8100_V2.feed_to_scan_steps_for_dpi(7200) == MODEL_8100_V2.feed_to_scan_steps
    # Mid-ladder corrections from expanded captures (not nearest-band fills).
    assert MODEL_8300I_SE.dummy_by_dpi[900] == 0x06
    assert MODEL_8300I_SE.dummy_by_dpi[1800] == 0x08
    assert MODEL_8300I_SE.pixel_clock_by_dpi[900] == 0x22
    assert MODEL_8300I_SE.pixel_clock_by_dpi[1800] == 0x08
    assert MODEL_8300I_SE.lperiod_by_dpi[900] == 11175
    assert MODEL_8300I_SE.lperiod_by_dpi[1800] == 11499
    assert MODEL_8300I_SE.shading_strip_clocks(900, dvdset=False) == (0x03, 0x01, 0x30)
    assert MODEL_8300I_SE.shading_strip_clocks(900, dvdset=True) == (0x04, 0x03, 0x03)
    assert MODEL_8300I_SE.shading_strip_clocks(1800, dvdset=False) == (0x06, 0x01, 0x30)
    assert MODEL_8300I_SE.shading_strip_clocks(1800, dvdset=True) == (0x08, 0x03, 0x03)
    # Host assemble fixes (TIFF-validated): round ld_shift; period-2 unstagger @1440/2400.
    assert MODEL_8300I_SE.scaled_ld_shift(24, 1440) == 5
    assert MODEL_8300I_SE.scaled_ld_shift(48, 1440) == 10
    assert MODEL_8300I_SE.stagger_y_for(1440) == (1, 0)
    assert MODEL_8300I_SE.stagger_y_for(2400) == (1, 0)
    assert MODEL_8300I_SE.stagger_y_for(1800) == ()
    assert MODEL_8200I_SE.scaled_ld_shift(24, 1440) == 5
    assert MODEL_8200I_SE.scaled_ld_shift(48, 1440) == 10
    assert MODEL_8200I_SE.stagger_y_for(1440) == (1, 0)
    assert MODEL_8200I_SE.stagger_y_for(2400) == (1, 0)
    assert MODEL_8200I_SE.stagger_y_for(1800) == ()
    # V2 (Scan Lab TIFF evidence 2026-10-16, see model_8100_v2._STAGGER_BY_DPI_8100_V2):
    # rounded ld_shift like the SE leaves; host unstagger (4, 0) at 7200, (1, 0) at 1440/2400.
    assert MODEL_8100_V2.scaled_ld_shift(24, 1440) == 5
    assert MODEL_8100_V2.scaled_ld_shift(48, 1440) == 10
    assert MODEL_8100_V2.scaled_ld_shift(24, 7200) == 24
    assert MODEL_8100_V2.stagger_y_for(7200) == (4, 0)
    assert MODEL_8100_V2.stagger_y_for(1440) == (1, 0)
    assert MODEL_8100_V2.stagger_y_for(2400) == (1, 0)
    assert MODEL_8100_V2.stagger_y_for(3600) == ()
    assert MODEL_8100_V2.stagger_y_for(1800) == ()
    assert dict(MODEL_8200I_SE.max_image_lincnt_by_feed2) == {
        13128: 4836,
        13560: 27476,
        13704: 6628,
        20232: 3700,
    }
    assert dict(MODEL_8100_V2.max_image_lincnt_by_feed2) == {13128: 29012}
    assert dict(MODEL_8300I_SE.max_image_lincnt_by_feed2) == {13128: 7253}
    assert dict(MODEL_8200I_SE.ladder_lincnt_by_dpi) == {
        150: 2292,
        300: 2292,
        600: 2292,
        720: 2748,
        900: 3436,
        1200: 4580,
        1440: 5496,
        1800: 6868,
        2400: 9156,
        3600: 13732,
        7200: 27476,
    }
    v2_ladder = {
        150: 2420,
        300: 2420,
        600: 2420,
        720: 2904,
        900: 3628,
        1200: 4836,
        1440: 5804,
        1800: 7252,
        2400: 9668,
        3600: 14500,
        7200: 29012,
    }
    assert dict(MODEL_8100_V2.ladder_lincnt_by_dpi) == v2_ladder
    assert dict(MODEL_8300I_SE.ladder_lincnt_by_dpi) == {
        dpi: lincnt // 4 for dpi, lincnt in v2_ladder.items()
    }
    for model in (MODEL_8200I_SE, MODEL_8100_V2):
        assert model.image_lincnt_per_line == 4
        assert model.usb_image_lincnt_half_lines is True
    assert MODEL_8300I_SE.image_lincnt_per_line == 1
    assert MODEL_8300I_SE.usb_image_lincnt_half_lines is False
    # Per-scan sensor timing: SE/V2 share SCAN_REGS; 8300i SilverFast writes its own.
    assert dict(MODEL_8200I_SE.sensor_custom_regs) == dict(MODEL_8100_V2.sensor_custom_regs)
    se_regs = dict(MODEL_8200I_SE.sensor_custom_regs)
    i8300_regs = dict(MODEL_8300I_SE.sensor_custom_regs)
    assert (se_regs[0x06], se_regs[0x0B], se_regs[0x5A], se_regs[0x5B]) == (0xF0, 0x4C, 0x31, 0x79)
    assert (i8300_regs[0x06], i8300_regs[0x0B], i8300_regs[0x5A], i8300_regs[0x5B]) == (0x10, 0x6C, 0x22, 0x4A)
    assert [i8300_regs[a] for a in range(0x52, 0x58)] == [0x08, 0x0A, 0x00, 0x02, 0x04, 0x06]
    assert [i8300_regs[a] for a in range(0x70, 0x74)] == [0x06, 0x07, 0x07, 0x08]
    changed = {a for a in se_regs if se_regs[a] != i8300_regs[a]}
    assert changed == {0x06, 0x0B, 0x52, 0x53, 0x54, 0x55, 0x56, 0x57, 0x5A, 0x5B, 0x70, 0x71, 0x72, 0x73}
    assert set(i8300_regs) == set(se_regs)


def test_every_public_field_is_catalogued():
    union = set()
    for cls in _LEAF_CLASSES:
        union |= dataclass_field_names(cls)
    assert union == GL128_SHARED_FIELDS | GL128_DIVERGENT_FIELDS
