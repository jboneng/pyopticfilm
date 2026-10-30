# SPDX-License-Identifier: GPL-3.0-or-later
"""Genesys (non-SE) colour calib wiring — no SE ASIC shading path."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.scan.calibrate import CalibEntry, Calibrator
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.scan.session import ScanSession


def _geometry(dpi: int = 900):
    return compute_geometry(dpi, model=MODEL_8200I, area=(0.0, 0.0, 0.2, 0.2))


def _asic() -> MagicMock:
    asic = MagicMock()
    asic._initialized = True
    asic.usb_planar_rgb = False
    asic.asic_shading_ready = False
    asic.run_asic_shading = MagicMock(side_effect=AssertionError("must not call SE shading"))
    return asic


def test_gl845_colour_scan_uses_host_calib_not_asic_shading(tmp_path: Path, monkeypatch):
    asic = _asic()
    cal = Calibrator(asic, cache_path=tmp_path / "calib.json", model=MODEL_8200I)
    geometry = _geometry()
    order: list[str] = []

    def fake_ensure_host(geo, *, method="transparency", mode="color"):
        order.append(f"host:{method}:{mode}")
        entry = CalibEntry(
            method=method,
            resolution=geo.resolution,
            startx=geo.startx,
            pixels=geo.pixels,
            dark=np.zeros((geo.pixels, 3), dtype=np.uint16),
            white=np.full((geo.pixels, 3), 40000, dtype=np.uint16),
            asic_shading=False,
        )
        cal._active = entry
        cal.prefer_asic_shading = False
        return entry

    monkeypatch.setattr(cal, "ensure_host_calib", fake_ensure_host)
    monkeypatch.setattr(
        cal,
        "ensure_colour_asic_shading",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("SE path")),
    )
    session = ScanSession(asic, MODEL_8200I, cal)
    monkeypatch.setattr(
        session,
        "acquire_raw",
        lambda *_a, **_k: (order.append("acquire") or b"\x00" * 64),
    )
    monkeypatch.setattr(
        session.pipeline,
        "assemble",
        lambda *_a, **_k: np.zeros((4, geometry.pixels, 3), dtype=np.uint16),
    )
    image = session.run(
        resolution=geometry.resolution,
        mode="color",
        geometry=geometry,
        apply_calib=True,
    )
    assert order == ["host:transparency:color", "acquire"]
    assert image.rgb.shape[2] == 3
    asic.run_asic_shading.assert_not_called()


def test_ensure_host_calib_reuses_cache(tmp_path: Path):
    asic = _asic()

    def apply_afe(fe, **_kw):
        asic.last_afe_gains = fe.gains
        asic.last_afe_offsets = fe.offsets

    asic.apply_afe_frontend = apply_afe
    cal = Calibrator(asic, cache_path=tmp_path / "calib.json", model=MODEL_8200I)
    geometry = _geometry()
    entry = CalibEntry(
        method="transparency",
        resolution=geometry.resolution,
        startx=geometry.startx,
        pixels=geometry.pixels,
        dark=np.zeros((geometry.pixels, 3), dtype=np.uint16),
        white=np.full((geometry.pixels, 3), 50000, dtype=np.uint16),
        asic_shading=False,
        afe_offsets=(0x10, 0x11, 0x12),
        afe_gains=(0x20, 0x21, 0x22),
    )
    cal.cache.upsert(entry)
    cal.cache.save()
    hit = cal.ensure_host_calib(geometry, method="transparency", mode="color")
    assert hit.pixels == geometry.pixels
    assert hit.asic_shading is False
    assert cal.prefer_asic_shading is False
    assert asic.last_afe_gains == (0x20, 0x21, 0x22)
    assert asic.last_afe_offsets == (0x10, 0x11, 0x12)


def test_8200i_calib_geometry_matches_sane_white_offset():
    from pyopticfilm.scan.geometry import MM_PER_INCH, compute_calib_geometry

    geo = compute_calib_geometry(1800, model=MODEL_8200I)
    # SANE: y_offset_ta=28.5, y_offset_calib_white_ta=0 → feed at TA only.
    expect = int((MODEL_8200I.y_offset_ta_mm * MODEL_8200I.motor_base_ydpi) / MM_PER_INCH)
    assert geo.starty == expect
    assert MODEL_8200I.y_offset_calib_white_ta_mm == 0.0


def test_host_stretch_applied_on_genesys_assemble():
    from pyopticfilm.scan.pipeline import ImagePipeline

    pipe = ImagePipeline(MODEL_8200I)
    w = 32
    rgb = np.full((4, w, 3), 20000, dtype=np.uint16)
    dark = np.zeros((w, 3), dtype=np.uint16)
    white = np.full((w, 3), 40000, dtype=np.uint16)
    out = pipe.apply_host_calib(rgb, dark=dark, white=white)
    # (20000/40000)*65535 ≈ 32768 before film-base expose / border clamp
    assert float(out.mean()) > 25000
    cal = Calibrator(None, model=MODEL_8200I)
    cal.prefer_asic_shading = False
    assert cal.should_apply_host_calib() is True


def test_host_calib_chunked_at_large_geometry():
    """7200 host calib must not allocate a full-frame float64 slab."""
    from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
    from pyopticfilm.scan.geometry import compute_geometry
    from pyopticfilm.scan.pipeline import ImagePipeline

    geo = compute_geometry(7200, model=MODEL_8200I_SE, area=None)
    h, w = geo.lines, geo.pixels
    rgb = np.full((h, w, 3), 20000, dtype=np.uint16)
    dark = np.zeros((w, 3), dtype=np.uint16)
    white = np.full((w, 3), 40000, dtype=np.uint16)
    out = ImagePipeline(MODEL_8200I_SE).apply_host_calib(
        rgb, dark=dark, white=white, expose_base=False
    )
    assert out.shape == (h, w, 3)
    assert out.dtype == np.uint16
    assert float(out.mean()) > 25000


def test_7600i_v2_white_strip_uses_open_light_zone_near_home():
    from pyopticfilm.device.select import MODEL_7600I_V2
    from pyopticfilm.scan.geometry import compute_calib_geometry

    geo = compute_calib_geometry(1800, model=MODEL_7600I_V2)
    # 1.5 mm from home in 3600-dpi motor steps, not the 28.5 mm TA offset (holder edge).
    assert geo.starty == int(1.5 * 3600 / 25.4)
    assert compute_calib_geometry(1800, model=MODEL_8200I).starty == int(28.5 * 3600 / 25.4)


def test_pga_gain_matches_ad9826_law():
    from pyopticfilm.scan.calibrate import _pga_gain

    assert _pga_gain(0) == 1.0
    assert abs(_pga_gain(63) - 6.0) < 1e-9
    # 7600i v2 hardware: code 24 → 17 changed G white by 1.135x.
    assert abs(_pga_gain(24) / _pga_gain(17) - 1.135) < 0.005


def test_7600i_v2_dark_reference_averages_more_lines():
    from pyopticfilm.device.select import MODEL_7600I_V2

    for model, expected_taller in ((MODEL_7600I_V2, True), (MODEL_8200I, False)):
        cal = Calibrator(None, model=model)
        scan = compute_geometry(1800, model=model, area=(0.0, 0.0, 0.2, 0.2))
        white = cal._calib_geometry_for(scan, method="transparency")
        dark = cal._dark_geometry_for(scan, white, method="transparency")
        assert dark.pixels == white.pixels and dark.startx == white.startx
        if expected_taller:
            assert dark.lines >= MODEL_7600I_V2.calib_dark_lines - 2
        else:
            assert dark is white
