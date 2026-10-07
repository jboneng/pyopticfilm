# SPDX-License-Identifier: GPL-3.0-or-later
"""Sibling-diff catalog: every GL128 field is shared-identical or declared divergent."""

from __future__ import annotations

from pyopticfilm.device.gl128_common import (
    GL128_DIVERGENT_FIELDS,
    GL128_SHARED_FIELDS,
    Gl128Common,
    dataclass_field_names,
)
from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2, Model8100V2
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE, Model8200iSE
from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE, Model8300iSE
from pyopticfilm.device.protocol import Gl128Model

_LEAVES = (MODEL_8200I_SE, MODEL_8100_V2, MODEL_8300I_SE)
_LEAF_CLASSES = (Model8200iSE, Model8100V2, Model8300iSE)


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
    for model in _LEAVES:
        assert isinstance(model, Gl128Common)
        assert isinstance(model, Gl128Model)


def test_shared_fields_are_equal_on_all_siblings():
    for name in sorted(GL128_SHARED_FIELDS):
        values = [_value(m, name) for m in _LEAVES]
        assert values[0] == values[1] == values[2], name


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
    assert MODEL_8300I_SE.scan_ready is False
    assert MODEL_8200I_SE.feed_to_scan_steps == 13128
    assert MODEL_8100_V2.feed_to_scan_steps == 13128
    assert MODEL_8300I_SE.feed_to_scan_steps == 13128
    assert MODEL_8200I_SE.ladder_feed2_steps == 13560
    assert MODEL_8100_V2.ladder_feed2_steps == 13128
    assert MODEL_8300I_SE.ladder_feed2_steps == 13128
    assert MODEL_8200I_SE.lperiod_by_dpi[7200] == 15963
    assert MODEL_8100_V2.lperiod_by_dpi[7200] == 16035
    assert MODEL_8300I_SE.lperiod_by_dpi[7200] == 16035
    assert MODEL_8300I_SE.lperiod_by_dpi[1200] == 11283
    assert MODEL_8200I_SE.exposure_lperiod == 14000
    assert MODEL_8100_V2.exposure_lperiod == 14000
    assert MODEL_8300I_SE.exposure_lperiod == 15000
    assert MODEL_8300I_SE.exposure_short == 15000
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
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(300) == 13040
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(1200) == 13112
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(2400) == 13124
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(3600) == 13126
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(7200) == 13128
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(1200, long_exposure=True) == 13128
    assert MODEL_8300I_SE.feed_to_scan_steps_for_dpi(3600, long_exposure=True) == 13128
    assert MODEL_8200I_SE.feed_to_scan_steps_for_dpi(1200) == MODEL_8200I_SE.feed_to_scan_steps
    assert MODEL_8100_V2.feed_to_scan_steps_for_dpi(7200) == MODEL_8100_V2.feed_to_scan_steps
    assert dict(MODEL_8200I_SE.max_image_lincnt_by_feed2) == {
        13128: 4836,
        13560: 27476,
        13704: 6628,
        20232: 3700,
    }
    assert dict(MODEL_8100_V2.max_image_lincnt_by_feed2) == {13128: 29012}
    assert dict(MODEL_8300I_SE.max_image_lincnt_by_feed2) == {13128: 29012}
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
    assert dict(MODEL_8300I_SE.ladder_lincnt_by_dpi) == v2_ladder


def test_every_public_field_is_catalogued():
    union = set()
    for cls in _LEAF_CLASSES:
        union |= dataclass_field_names(cls)
    assert union == GL128_SHARED_FIELDS | GL128_DIVERGENT_FIELDS
