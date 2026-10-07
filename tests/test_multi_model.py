# SPDX-License-Identifier: GPL-3.0-or-later
"""Multi-model OpticFilm selection and table smoke tests (no hardware)."""

from __future__ import annotations

from pyopticfilm.asic.gl842 import Gl842
from pyopticfilm.asic.gl843 import Gl843
from pyopticfilm.asic.gl845 import Gl845
from pyopticfilm.device.model_7200 import MODEL_7200
from pyopticfilm.device.model_7200i import MODEL_7200_V2, MODEL_7200I
from pyopticfilm.device.model_7300 import MODEL_7300, MODEL_7400_V1
from pyopticfilm.device.model_7400 import MODEL_7400, MODEL_8100
from pyopticfilm.device.model_7500i import MODEL_7500I, MODEL_7600I_V1
from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.device.select import (
    KNOWN_MODELS,
    MODEL_7600I_V2,
    create_asic,
    model_for_device,
)
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.usb.device import (
    PID_OPTICFILM_7200,
    PID_OPTICFILM_7200_V2,
    PID_OPTICFILM_7200I,
    PID_OPTICFILM_7300,
    PID_OPTICFILM_7400,
    PID_OPTICFILM_7500I,
    PID_OPTICFILM_7600I,
    PID_OPTICFILM_8100,
    PID_OPTICFILM_8200I,
    PID_OPTICFILM_8200I_SE,
    PID_OPTICFILM_8300I_SE,
    SUPPORTED_IDS,
    VID_PLUSTEK,
)


def test_supported_ids_cover_complete_opticfilm():
    expected = {
        PID_OPTICFILM_8200I,
        PID_OPTICFILM_8200I_SE,
        PID_OPTICFILM_8300I_SE,
        PID_OPTICFILM_7200,
        PID_OPTICFILM_7200I,
        PID_OPTICFILM_7200_V2,
        PID_OPTICFILM_7300,
        PID_OPTICFILM_7400,
        PID_OPTICFILM_7500I,
        PID_OPTICFILM_7600I,
        PID_OPTICFILM_8100,
    }
    pids = {pid for vid, pid in SUPPORTED_IDS if vid == VID_PLUSTEK}
    assert expected <= pids


def test_bcd_disambiguation_7400_and_7600i():
    assert model_for_device(PID_OPTICFILM_7400, 0x0400) is MODEL_7400_V1
    assert model_for_device(PID_OPTICFILM_7400, 0x0605).name == MODEL_7400.name
    assert model_for_device(PID_OPTICFILM_7400, 0) is MODEL_7400
    assert model_for_device(PID_OPTICFILM_7600I, 0x0400) is MODEL_7600I_V1
    assert model_for_device(PID_OPTICFILM_7600I, 0x0605).name == MODEL_7600I_V2.name


def test_simple_pid_aliases():
    from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
    from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
    from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE
    from pyopticfilm.usb.device import PID_OPTICFILM_8100_V2

    assert model_for_device(PID_OPTICFILM_8200I) is MODEL_8200I
    assert model_for_device(PID_OPTICFILM_8200I_SE) is MODEL_8200I_SE
    assert model_for_device(PID_OPTICFILM_8300I_SE) is MODEL_8300I_SE
    assert model_for_device(PID_OPTICFILM_8100) is MODEL_8100
    assert model_for_device(PID_OPTICFILM_8100_V2) is MODEL_8100_V2
    assert model_for_device(PID_OPTICFILM_7200I) is MODEL_7200I
    assert model_for_device(PID_OPTICFILM_7200_V2) is MODEL_7200_V2
    assert model_for_device(PID_OPTICFILM_7300) is MODEL_7300
    assert model_for_device(PID_OPTICFILM_7500I) is MODEL_7500I
    assert model_for_device(PID_OPTICFILM_7200) is MODEL_7200


def test_create_asic_routing():
    class _Proto:
        pass

    proto = _Proto()  # type: ignore[assignment]
    assert isinstance(create_asic(proto, MODEL_8200I), Gl845)  # type: ignore[arg-type]
    assert isinstance(create_asic(proto, MODEL_7400), Gl845)  # type: ignore[arg-type]
    assert isinstance(create_asic(proto, MODEL_7200I), Gl843)  # type: ignore[arg-type]
    assert isinstance(create_asic(proto, MODEL_7200), Gl842)  # type: ignore[arg-type]


def test_scan_ready_validate_set():
    from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
    from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE

    scan_ready_models = {id(m) for m in KNOWN_MODELS if m.scan_ready}
    assert scan_ready_models == {id(MODEL_8200I_SE), id(MODEL_8100_V2)}
    for m in KNOWN_MODELS:
        if m is MODEL_8200I_SE or m is MODEL_8100_V2:
            assert m.scan_ready is True
            continue
        assert m.scan_ready is False, f"{m.model} must stay locked out"


def test_geometry_for_each_canonical_model():
    for model in (
        MODEL_8200I,
        MODEL_7400,
        MODEL_7200I,
        MODEL_7300,
        MODEL_7500I,
        MODEL_7200,
    ):
        dpi = model.resolutions_dpi[-1]  # typically lowest
        g = compute_geometry(dpi, model=model)
        assert g.resolution == dpi
        assert g.pixels >= 16
        assert g.lines >= 1
        assert g.register_dpiset == model.register_dpiset_by_dpi[dpi]


def test_infrared_caps():
    assert MODEL_8200I.supports_infrared is True
    assert MODEL_7400.supports_infrared is False
    assert MODEL_7200I.supports_infrared is True
    assert MODEL_7200_V2.supports_infrared is False
    assert MODEL_7300.supports_infrared is False
    assert MODEL_7500I.supports_infrared is True
    assert MODEL_7200.supports_infrared is False


def test_boot_maps_nonempty_for_complete_models():
    for model in (
        MODEL_8200I,
        MODEL_7400,
        MODEL_8100,
        MODEL_7200I,
        MODEL_7300,
        MODEL_7500I,
        MODEL_7200,
    ):
        boot = model.boot_register_map()
        assert boot
        assert 0x05 in boot


def test_8100_v2_capture_derived_constants():
    """Capture-derived overrides in Model8100V2 must match 04_color_7200.pcapng.

    Evidence sources (all from 04_color_7200.pcapng, Aug 2026):
    - feed_to_scan_steps: frame 2999, regs 0x3D-0x3F = 0x003348 = 13128
    - lperiod_by_dpi[7200]: frames 1661/2257/3203, reg 0x28-0x2A = 0x003EA3 = 16035
    - shading_strip_clocks white/7200: frame 2257, reg 0x2B = 0x10
    - max_image_lincnt_by_feed2[13128]: frame 3203, regs 0x25-0x27 = 0x007154 = 29012
    - ladder_feed2_steps: frame 2999, same positioning feed as feed_to_scan_steps
    """
    from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
    from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE

    # feed_to_scan_steps matches SE (both use the TA window top, see #67)
    assert MODEL_8100_V2.feed_to_scan_steps == 13128
    assert MODEL_8200I_SE.feed_to_scan_steps == 13128

    # lperiod at 7200 dpi differs from SE (15963)
    assert MODEL_8100_V2.lperiod_by_dpi[7200] == 16035
    assert MODEL_8200I_SE.lperiod_by_dpi[7200] == 15963

    # lperiod at other DPIs is inherited unchanged from SE
    for dpi in (600, 1200, 1800, 3600):
        assert MODEL_8100_V2.lperiod_by_dpi[dpi] == MODEL_8200I_SE.lperiod_by_dpi[dpi], dpi

    # White shading at 7200 dpi uses dummy=0x10 (SE would produce 0x17)
    dummy, clk_a, clk_b = MODEL_8100_V2.shading_strip_clocks(7200, dvdset=True)
    assert dummy == 0x10, f"white shading dummy: expected 0x10, got {dummy:#04x}"
    assert clk_a == 0x01
    assert clk_b == 0x01

    # Dark shading at 7200 dpi uses dummy=0x17 on both V2 and SE (SE fallback)
    dark_dummy, _, _ = MODEL_8100_V2.shading_strip_clocks(7200, dvdset=False)
    se_dark_dummy, _, _ = MODEL_8200I_SE.shading_strip_clocks(7200, dvdset=False)
    assert dark_dummy == 0x17, f"V2 dark dummy: expected 0x17, got {dark_dummy:#04x}"
    assert se_dark_dummy == 0x17, f"SE dark dummy: expected 0x17, got {se_dark_dummy:#04x}"

    # V2 full-frame LINCNT at feed2=13128: 29012 (04_color_7200 frame 3203)
    # SE inherits 4836 (1200 dpi preview) at the same key — wrong for V2
    assert MODEL_8100_V2.max_image_lincnt_by_feed2[13128] == 29012
    assert MODEL_8200I_SE.max_image_lincnt_by_feed2[13128] == 4836

    # V2 ladder_feed2_steps matches feed_to_scan_steps (top of TA window)
    assert MODEL_8100_V2.ladder_feed2_steps == 13128
    assert MODEL_8200I_SE.ladder_feed2_steps == 13560
    assert MODEL_8100_V2.ladder_feed2_steps == MODEL_8100_V2.feed_to_scan_steps

    # shading_strip_clocks at lower DPIs delegates to SE (not the 0x10 override)
    dummy_1200, _, _ = MODEL_8100_V2.shading_strip_clocks(1200, dvdset=True)
    se_dummy_1200, _, _ = MODEL_8200I_SE.shading_strip_clocks(1200, dvdset=True)
    assert dummy_1200 == se_dummy_1200, "V2 lower-DPI white shading must match SE"
