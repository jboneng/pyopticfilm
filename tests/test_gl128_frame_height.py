"""GL128 default full frame is one inch tall, like SilverFast (#102)."""

from __future__ import annotations

import pytest

from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE
from pyopticfilm.scan.geometry import compute_calib_geometry, compute_geometry
from pyopticfilm.scan.pipeline import ImagePipeline

GL128 = [MODEL_8200I_SE, MODEL_8100_V2, MODEL_8300I_SE]


@pytest.mark.parametrize("model", GL128, ids=lambda m: m.model)
@pytest.mark.parametrize("dpi", [150, 300, 600, 720, 900, 1200, 1440, 1800, 2400, 3600])
def test_full_frame_is_one_inch(model, dpi):
    geometry = compute_geometry(dpi, model=model)
    rgb = ImagePipeline(model=model).assemble(bytes(geometry.total_bytes), geometry, expose_base=False)
    assert rgb.shape[0] == dpi


def test_full_frame_1800_is_silverfast_size():
    geometry = compute_geometry(1800, model=MODEL_8300I_SE)
    rgb = ImagePipeline(model=MODEL_8300I_SE).assemble(bytes(geometry.total_bytes), geometry, expose_base=False)
    assert rgb.shape[:2] == (1800, 2592)


@pytest.mark.parametrize("model", GL128, ids=lambda m: m.model)
def test_7200_reads_enough_rows_for_one_inch(model):
    geometry = compute_geometry(7200, model=model)
    assert geometry.frame_lines == 7200
    after_shift = geometry.optical_line_count // max(1, geometry.optical_line_count // geometry.lines)
    assert after_shift - geometry.max_color_shift - geometry.num_staggered_lines >= 7200


def test_crops_and_calib_are_not_cut():
    assert compute_geometry(1800, model=MODEL_8300I_SE, area=(0.0, 0.0, 1.0, 1.0)).frame_lines == 0
    assert compute_calib_geometry(1800, model=MODEL_8300I_SE).frame_lines == 0
