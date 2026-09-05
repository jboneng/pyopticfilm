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
from pyopticfilm.device.protocol import Gl128Model


def _value(model: object, name: str) -> object:
    value = getattr(model, name)
    if hasattr(value, "items"):
        return dict(value)
    return value


def test_gl128_common_fields_match_shared_catalog():
    assert dataclass_field_names(Gl128Common) == GL128_SHARED_FIELDS


def test_leaf_extra_fields_are_exactly_the_divergent_catalog():
    se_extra = dataclass_field_names(Model8200iSE) - GL128_SHARED_FIELDS
    v2_extra = dataclass_field_names(Model8100V2) - GL128_SHARED_FIELDS
    assert se_extra == GL128_DIVERGENT_FIELDS
    assert v2_extra == GL128_DIVERGENT_FIELDS


def test_v2_does_not_subclass_se():
    assert not issubclass(Model8100V2, Model8200iSE)
    assert not isinstance(MODEL_8100_V2, Model8200iSE)
    assert isinstance(MODEL_8200I_SE, Gl128Common)
    assert isinstance(MODEL_8100_V2, Gl128Common)
    assert isinstance(MODEL_8200I_SE, Gl128Model)
    assert isinstance(MODEL_8100_V2, Gl128Model)


def test_shared_fields_are_equal_on_se_and_v2():
    for name in sorted(GL128_SHARED_FIELDS):
        assert _value(MODEL_8200I_SE, name) == _value(MODEL_8100_V2, name), name


def test_divergent_fields_match_capture_catalog():
    assert MODEL_8200I_SE.name == "plustek-opticfilm-8200i-se"
    assert MODEL_8100_V2.name == "plustek-opticfilm-8100-v2"
    assert MODEL_8200I_SE.model == "OpticFilm 8200i SE"
    assert MODEL_8100_V2.model == "OpticFilm 8100 (V2)"
    assert MODEL_8200I_SE.usb_product_id == 0x1825
    assert MODEL_8100_V2.usb_product_id == 0x1824
    assert MODEL_8200I_SE.supports_infrared is True
    assert MODEL_8100_V2.supports_infrared is False
    assert MODEL_8200I_SE.feed_to_scan_steps == 13704
    assert MODEL_8100_V2.feed_to_scan_steps == 13128
    assert MODEL_8200I_SE.ladder_feed2_steps == 13560
    assert MODEL_8100_V2.ladder_feed2_steps == 13128
    assert MODEL_8200I_SE.use_slow_final_positioning_feed is False
    assert MODEL_8100_V2.use_slow_final_positioning_feed is True
    assert MODEL_8200I_SE.me_default_exposure_mode == "adaptive"
    assert MODEL_8100_V2.me_default_exposure_mode == "fixed"
    assert MODEL_8200I_SE.me_use_banded_alignment is False
    assert MODEL_8100_V2.me_use_banded_alignment is True
    assert MODEL_8200I_SE.lperiod_by_dpi[7200] == 15963
    assert MODEL_8100_V2.lperiod_by_dpi[7200] == 16035
    assert dict(MODEL_8200I_SE.max_image_lincnt_by_feed2) == {
        13128: 4836,
        13560: 27476,
        13704: 6628,
        20232: 3700,
    }
    assert dict(MODEL_8100_V2.max_image_lincnt_by_feed2) == {13128: 29012}


def test_every_public_field_is_catalogued():
    union = dataclass_field_names(Model8200iSE) | dataclass_field_names(Model8100V2)
    assert union == GL128_SHARED_FIELDS | GL128_DIVERGENT_FIELDS
