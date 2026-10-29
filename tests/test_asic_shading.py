# SPDX-License-Identifier: GPL-3.0-or-later
"""Colour ASIC shading: home measure/apply before acquire (SilverFast order)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.exceptions import CalibrationError
from pyopticfilm.scan.calibrate import CalibEntry, Calibrator
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.scan.session import ScanSession


def _geometry(dpi: int = 1200):
    return compute_geometry(dpi, model=MODEL_8200I_SE, area=(0.0, 0.0, 1.0, 0.5))


def _blob_entry(geometry, *, blob: bytes = b"\x01\x02\x03\x04") -> CalibEntry:
    return CalibEntry(
        method="transparency",
        resolution=geometry.resolution,
        startx=geometry.startx,
        pixels=geometry.pixels,
        dark=np.zeros((geometry.pixels, 3), dtype=np.uint16),
        white=np.full((geometry.pixels, 3), 65535, dtype=np.uint16),
        asic_shading=True,
        shading_blob=blob,
        afe_gains=(38, 30, 36),
        afe_offsets=(19, 31, 23),
    )


def _asic(*, shading_ready: bool = False, motor: bool = True) -> MagicMock:
    asic = MagicMock()
    asic._initialized = True
    asic._motor_moves_enabled = motor
    asic.asic_shading_ready = shading_ready
    asic.last_color_shading_host_ok = False
    asic.last_host_calib_dark = None
    asic.last_host_calib_white = None
    asic.usb_planar_rgb = False
    asic._reg_cache = {}
    asic.is_at_home = MagicMock(return_value=True)
    asic.home = MagicMock()
    asic.search_afe = MagicMock()
    asic.run_asic_shading = MagicMock(return_value=b"\x00" * 16)
    asic.upload_shading_table = MagicMock()
    asic.apply_frontend = MagicMock()
    asic.last_afe = None
    asic.last_color_shading_reject_reason = None
    return asic


def test_measure_disarms_motor_and_persists_blob(tmp_path: Path):
    asic = _asic(motor=True)
    order: list[str] = []

    def search_afe(**_kw):
        order.append("afe")
        assert asic._motor_moves_enabled is False

    def run_asic_shading(**_kw):
        order.append("shade")
        assert asic._motor_moves_enabled is False
        asic.asic_shading_ready = True
        asic.last_afe = MagicMock(gains=(38, 30, 36), offsets=(19, 31, 23))
        return b"BLOBDATA"

    asic.search_afe.side_effect = search_afe
    asic.run_asic_shading.side_effect = run_asic_shading

    cal = Calibrator(asic, cache_path=tmp_path / "calib.json", model=MODEL_8200I_SE)
    entry = cal.measure_colour_asic_shading(_geometry())

    assert order == ["afe", "shade"]
    assert asic._motor_moves_enabled is True
    assert entry.has_asic_blob
    assert entry.shading_blob == b"BLOBDATA"
    assert cal.prefer_asic_shading is True


def _afe_reuse_asic(searched_dpi):
    asic = _asic(motor=True)
    asic.last_afe = MagicMock(gains=(38, 30, 36), offsets=(19, 31, 23))
    asic.last_afe_dpi = searched_dpi
    asic._scan_method = "transparency"

    def run_asic_shading(**_kw):
        asic.asic_shading_ready = True
        return b"TABLE"

    asic.run_asic_shading.side_effect = run_asic_shading
    return asic


def test_measure_reuses_colour_afe_within_the_same_strip_timing(tmp_path: Path):
    """1800→2400 runs the AFE strips under the same timing: remeasure shading only."""
    asic = _afe_reuse_asic(1800)
    asic.search_afe.side_effect = AssertionError("must not re-search")
    cal = Calibrator(asic, cache_path=tmp_path / "calib.json", model=MODEL_8200I_SE)
    entry = cal.measure_colour_asic_shading(_geometry(2400))
    asic.search_afe.assert_not_called()
    asic.run_asic_shading.assert_called_once()
    assert entry.shading_blob == b"TABLE"


@pytest.mark.parametrize("searched_dpi", [1800, None])
def test_measure_researches_afe_when_strip_timing_differs(tmp_path: Path, searched_dpi):
    """3600 ppi strips run a different line period from 1800's; codes of unknown origin are not trusted."""
    asic = _afe_reuse_asic(searched_dpi)
    cal = Calibrator(asic, cache_path=tmp_path / "calib.json", model=MODEL_8200I_SE)
    cal.measure_colour_asic_shading(_geometry(3600))
    asic.search_afe.assert_called_once_with(method="transparency", resolution=3600)


def test_forced_calibration_researches_afe(tmp_path: Path):
    """``calibrate(force=True)`` runs a new AFE search even at the same dpi."""
    asic = _afe_reuse_asic(1800)
    cal = Calibrator(asic, cache_path=tmp_path / "calib.json", model=MODEL_8200I_SE)
    cal.measure_colour_asic_shading(_geometry(1800), research_afe=True)
    asic.search_afe.assert_called_once_with(method="transparency", resolution=1800)


def test_calibrate_force_passes_research_afe(tmp_path: Path, monkeypatch):
    asic = _asic(motor=True)
    cal = Calibrator(asic, cache_path=tmp_path / "calib.json", model=MODEL_8200I_SE)
    seen: list[bool] = []
    monkeypatch.setattr(
        cal, "measure_colour_asic_shading", lambda _geo, *, research_afe=False: seen.append(research_afe)
    )
    cal.run(resolution=1800, force=True)
    assert seen == [True]


def test_apply_cached_blob_skips_strip_acquire(tmp_path: Path):
    asic = _asic(shading_ready=False)
    cal = Calibrator(asic, cache_path=tmp_path / "calib.json", model=MODEL_8200I_SE)
    geometry = _geometry()
    entry = _blob_entry(geometry, blob=b"CACHED")
    cal.cache.upsert(entry)
    cal.cache.save()

    asic.run_asic_shading.side_effect = AssertionError("must not re-measure")
    asic.search_afe.side_effect = AssertionError("must not re-search")

    cal.apply_colour_asic_shading(entry)

    asic.upload_shading_table.assert_called_once_with(b"CACHED")
    asic.apply_frontend.assert_called_once()
    assert asic.asic_shading_ready is True


def test_ensure_reuploads_when_dpi_or_width_changes(tmp_path: Path):
    """Stale narrower DVDSET tables (1200→1440/2400) black out END-side columns."""
    asic = _asic(shading_ready=False)
    cal = Calibrator(asic, cache_path=tmp_path / "calib.json", model=MODEL_8200I_SE)
    geo_1200 = _geometry(1200)
    geo_2400 = _geometry(2400)
    entry_1200 = _blob_entry(geo_1200, blob=b"DPI1200")
    entry_2400 = _blob_entry(geo_2400, blob=b"DPI2400")
    cal.cache.upsert(entry_1200)
    cal.cache.upsert(entry_2400)
    cal.cache.save()

    cal.ensure_colour_asic_shading(geo_1200)
    assert asic.upload_shading_table.call_args_list[-1].args == (b"DPI1200",)
    assert asic.asic_shading_ready is True

    # Same window again: skip re-upload.
    asic.upload_shading_table.reset_mock()
    cal.ensure_colour_asic_shading(geo_1200)
    asic.upload_shading_table.assert_not_called()

    # Wider/different DPI must replace the table even though ready stays True.
    cal.ensure_colour_asic_shading(geo_2400)
    asic.upload_shading_table.assert_called_once_with(b"DPI2400")
    assert cal._active is not None
    assert cal._active.resolution == 2400
    assert cal._active.pixels == geo_2400.pixels


def test_cache_roundtrip_blob(tmp_path: Path):
    path = tmp_path / "calib.json"
    geometry = _geometry()
    cal = Calibrator(_asic(), cache_path=path, model=MODEL_8200I_SE)
    cal.cache.upsert(_blob_entry(geometry, blob=b"XYZ"))
    cal.cache.save()

    # A new session measures again by default; the saved entry is only kept in the file.
    fresh = Calibrator(_asic(), cache_path=path, model=MODEL_8200I_SE)
    assert fresh.find_for_scan(method="transparency", geometry=geometry) is None
    assert len(fresh.cache.entries) == 1

    cal2 = Calibrator(_asic(), cache_path=path, model=MODEL_8200I_SE, reuse_saved=True)
    hit = cal2.find_for_scan(method="transparency", geometry=geometry)
    assert hit is not None
    assert hit.has_asic_blob
    assert hit.shading_blob == b"XYZ"


def test_dummy_marker_without_blob_is_cache_miss(tmp_path: Path):
    geometry = _geometry()
    cal = Calibrator(_asic(), cache_path=tmp_path / "calib.json", model=MODEL_8200I_SE)
    cal.cache.upsert(
        CalibEntry(
            method="transparency",
            resolution=geometry.resolution,
            startx=geometry.startx,
            pixels=geometry.pixels,
            dark=np.zeros((geometry.pixels, 3), dtype=np.uint16),
            white=np.full((geometry.pixels, 3), 65535, dtype=np.uint16),
            asic_shading=True,
        )
    )
    assert cal.find_for_scan(method="transparency", geometry=geometry) is None


def test_session_ensures_before_acquire(tmp_path: Path, monkeypatch):
    asic = _asic()
    cal = Calibrator(asic, cache_path=tmp_path / "calib.json", model=MODEL_8200I_SE)
    geometry = _geometry()
    order: list[str] = []

    def fake_ensure(geo):
        order.append("ensure")
        asic.asic_shading_ready = True
        entry = _blob_entry(geo)
        cal._active = entry
        cal.prefer_asic_shading = True
        return entry

    monkeypatch.setattr(cal, "ensure_colour_asic_shading", fake_ensure)
    session = ScanSession(asic, MODEL_8200I_SE, cal)
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
    session.run(resolution=geometry.resolution, mode="color", geometry=geometry)
    assert order == ["ensure", "acquire"]


def test_measure_retries_home_then_raises(tmp_path: Path):
    asic = _asic()
    calls = {"n": 0}

    def shade(**_kw):
        calls["n"] += 1
        asic.asic_shading_ready = False
        asic.last_color_shading_reject_reason = "ASIC shading validation failed"
        return b"bad"

    asic.run_asic_shading.side_effect = shade
    asic.search_afe.side_effect = lambda **_k: None
    cal = Calibrator(asic, cache_path=tmp_path / "calib.json", model=MODEL_8200I_SE)
    with pytest.raises(CalibrationError, match="Retry the scan"):
        cal.measure_colour_asic_shading(_geometry())
    assert calls["n"] == 2
    asic.home.assert_called()


def test_measure_raises_lamp_off_dark_message(tmp_path: Path):
    asic = _asic()

    def shade(**_kw):
        asic.asic_shading_ready = False
        asic.last_color_shading_reject_reason = "lamp-off dark still bright (dark mean 22936 > 3000)"
        return b"bad"

    asic.run_asic_shading.side_effect = shade
    asic.search_afe.side_effect = lambda **_k: None
    cal = Calibrator(asic, cache_path=tmp_path / "calib.json", model=MODEL_8200I_SE)
    with pytest.raises(CalibrationError, match="AFE black level"):
        cal.measure_colour_asic_shading(_geometry())


def test_colour_shading_failure_message_mapping():
    from pyopticfilm.scan.calibrate import (
        colour_shading_failure_message,
    )

    assert "AFE black level" in colour_shading_failure_message("dark mean 12000 > 3000")
    assert "illumination" in colour_shading_failure_message("ch0 white≈dark (w=100 d=200); lamp-off dark still bright (dark mean 22000)")
    assert "clear home field" in colour_shading_failure_message("white strip looks like film (not clear home field)")
    assert "park at home" in colour_shading_failure_message("carriage not at home")
    assert "power-cycle" in colour_shading_failure_message("ch0 range 10 < 500")


def test_pick_shading_dark_layout_keeps_equal_mean():
    from pyopticfilm.scan.calib_gl128 import (
        pick_shading_dark_layout,
    )

    # Same total energy, different col0 balance — must not flip for balance alone.
    primary = [(1000, 2000, 3000), (30000, 20000, 10000)]
    alternate = [(7000, 7000, 7000), (15000, 15000, 15000)]
    assert sum(sum(c) for c in primary) == sum(sum(c) for c in alternate)
    chosen, planar = pick_shading_dark_layout(primary, alternate, primary_is_planar=False)
    assert planar is False
    assert chosen[0] == (1000, 2000, 3000)


def test_pick_shading_dark_layout_flips_when_alt_truly_dark():
    from pyopticfilm.scan.calib_gl128 import (
        pick_shading_dark_layout,
    )

    primary = [(20000, 21000, 19000)] * 4
    alternate = [(800, 900, 850)] * 4
    chosen, planar = pick_shading_dark_layout(primary, alternate, primary_is_planar=False)
    assert planar is True
    assert chosen[0] == (800, 900, 850)


def test_shading_settle_constants_match_silverfast():
    from pyopticfilm.scan.calib_gl128 import (
        COLOR_SHADING_DARK_MEAN_MAX,
        COLOR_SHADING_DARK_SETTLE_S,
        COLOR_SHADING_WHITE_MEAN_MIN,
        IR_SHADING_DARK_SETTLE_S,
        IR_SHADING_WHITE_MEAN_MIN,
    )

    assert COLOR_SHADING_DARK_SETTLE_S == 0.5
    assert IR_SHADING_DARK_SETTLE_S == 0.5
    assert COLOR_SHADING_DARK_MEAN_MAX == 3000
    assert IR_SHADING_WHITE_MEAN_MIN == 10000
    # Post-unity DVDSET whites read ~50-57k, so the colour floor sits well above IR.
    assert COLOR_SHADING_WHITE_MEAN_MIN == 20000


def test_shading_table_covers_the_full_image_window():
    """The table spans every acquired column — no narrower AHB cap (sessions 03/04)."""
    from pyopticfilm.scan.bringup import (
        bringup_scan_geometry,
        preview_safe_scan_area,
    )
    from pyopticfilm.scan.calib_gl128 import (
        declared_shading_size,
        shading_acquire_width,
        shading_entry_count,
    )
    from pyopticfilm.scan.geometry import compute_geometry

    for dpi in (1800, 3600):
        area, _ = preview_safe_scan_area(MODEL_8200I_SE, dpi, y1=0.0)
        raw = compute_geometry(dpi, model=MODEL_8200I_SE, area=area)
        raw_n = shading_acquire_width(
            strpixel=raw.pixel_startx,
            endpixel=raw.pixel_endx,
            dpiset=raw.register_dpiset,
            optical_resolution=MODEL_8200I_SE.optical_resolution,
        )
        assert shading_entry_count(declared_shading_size(raw_n)) == raw_n

        geometry, _ = bringup_scan_geometry(MODEL_8200I_SE, dpi, profile="preview_safe")
        bring_n = shading_acquire_width(
            strpixel=geometry.pixel_startx,
            endpixel=geometry.pixel_endx,
            dpiset=geometry.register_dpiset,
            optical_resolution=MODEL_8200I_SE.optical_resolution,
        )
        assert bring_n == raw_n
        assert geometry.pixels == raw.pixels
        assert geometry.pixel_endx == raw.pixel_endx


def test_declared_shading_size_matches_capture_sessions():
    """Declared AHB size = 4 x pairs with two pad pairs per full 512-byte block."""
    from pyopticfilm.scan.calib_gl128 import (
        SHADING_WIDTH_BY_DPI,
        declared_shading_size,
        shading_entry_count,
    )

    assert declared_shading_size(1728) == 21064  # session 03, 1200 dpi
    assert declared_shading_size(2478) == 30208  # session 04, 1800 dpi
    assert declared_shading_size(4956) == 60416  # session 06, 3600 dpi
    assert SHADING_WIDTH_BY_DPI[1200] == 1728
    assert SHADING_WIDTH_BY_DPI[1800] == 2478
    for size in (21064, 30208, 60416):
        assert declared_shading_size(shading_entry_count(size)) == size


def test_pack_shading_table_pads_every_512_byte_block():
    """DVDSET skips the last two pairs of each block; data must not fill them."""
    from pyopticfilm.scan.calib_gl128 import (
        SHADING_BLOCK_BYTES,
        SHADING_BLOCK_DATA_PAIRS,
        SHADING_GAIN_UNITY,
        declared_shading_size,
        make_unity_white_table,
        unpack_shading_table,
    )

    n = 2478
    dark = [(927, 1039, 1171)] * n
    blob = make_unity_white_table(dark, declared_size=declared_shading_size(n))
    assert len(blob) == 30208

    gains = [int.from_bytes(blob[off + 2 : off + 4], "little") for off in range(0, len(blob), 4)]
    for i, gain in enumerate(gains):
        in_pad = i % (SHADING_BLOCK_BYTES // 4) >= SHADING_BLOCK_DATA_PAIRS
        assert gain == (0 if in_pad else SHADING_GAIN_UNITY), f"pair {i}"
    assert gains.count(SHADING_GAIN_UNITY) == n * 3

    rows = unpack_shading_table(blob)[:n]
    assert len(rows) == n
    assert rows[0] == {"dark": [927, 1039, 1171], "gain": [SHADING_GAIN_UNITY] * 3}
    assert rows[-1]["dark"] == [927, 1039, 1171]


def test_shading_gain_inverts_the_white_profile():
    from pyopticfilm.scan.calib_gl128 import (
        SHADING_GAIN_MAX,
        SHADING_GAIN_MIN,
        SHADING_GAIN_TARGET,
        SHADING_GAIN_UNITY,
        shading_gain_for_white,
        shading_gains_from_white,
    )

    assert SHADING_GAIN_UNITY == 0x2000
    assert SHADING_GAIN_TARGET == 0xFFFF
    # White already at target needs unity; a dimmer column needs more gain.
    assert shading_gain_for_white(SHADING_GAIN_TARGET) == SHADING_GAIN_UNITY
    assert shading_gain_for_white(SHADING_GAIN_TARGET / 2) == SHADING_GAIN_UNITY * 2
    assert shading_gain_for_white(50000) > shading_gain_for_white(57000)
    # Clamps hold at both extremes.
    assert shading_gain_for_white(1) == SHADING_GAIN_MAX
    assert shading_gain_for_white(1_000_000) == SHADING_GAIN_MIN

    gains = shading_gains_from_white([(50000, 55000, 56000)])
    assert gains[0][0] > gains[0][1] > gains[0][2]


def test_measured_shading_table_stores_gains_not_whites():
    """The gain slot must hold ~1.2x, never the 50k measurement (that inverts it)."""
    from pyopticfilm.scan.calib_gl128 import (
        SHADING_GAIN_UNITY,
        build_measured_shading_table,
        declared_shading_size,
        unpack_shading_table,
    )

    n = 256
    dark = [(927, 1039, 1171)] * n
    white = [(49610, 55067, 56009)] * n
    blob = build_measured_shading_table(dark, white, declared_size=declared_shading_size(n))
    rows = unpack_shading_table(blob)[:n]
    assert rows[0]["dark"] == [927, 1039, 1171]
    for gain, expected in zip(rows[0]["gain"], (10822, 9750, 9586), strict=True):
        assert abs(gain - expected) <= 2
        assert 1.05 <= gain / SHADING_GAIN_UNITY <= 2.0
    assert rows[-1]["gain"] == rows[0]["gain"]


def test_session04_capture_gains_match_silverfast():
    """Golden: SF's own coefficients from the white strip it measured."""
    import json

    from pyopticfilm.scan.calib_gl128 import (
        SHADING_GAIN_TARGET,
        SHADING_GAIN_UNITY,
        shading_gain_for_white,
    )

    fixture = json.loads((Path(__file__).parent / "data" / "session04_shading_gain.json").read_text(encoding="utf-8"))
    assert fixture["unity_gain"] == SHADING_GAIN_UNITY
    assert fixture["sf_target"][0] == SHADING_GAIN_TARGET
    for column in fixture["columns"]:
        for channel, target in enumerate(fixture["sf_target"]):
            got = shading_gain_for_white(column["white_unity"][channel], target=target)
            assert abs(got - column["coeff"][channel]) <= 16, (column["x"], channel, got)


def test_validate_color_shading_rejects_film_like_white():
    from pyopticfilm.scan.calib_gl128 import (
        validate_color_shading_strip,
    )

    dark = [(800, 900, 850)] * 32
    # R≪G/B is the orange-mask tell.
    white = [(20000, 52000, 50000)] * 32
    ok, reason = validate_color_shading_strip(dark, white)
    assert ok is False
    assert "film" in reason


def test_validate_color_shading_accepts_session04_whites():
    """Post-unity DVDSET whites (~50-57k) are the expected reading, not a fault."""
    from pyopticfilm.scan.calib_gl128 import (
        validate_color_shading_strip,
    )

    dark = [(927, 1039, 1171)] * 32
    white = [(49610, 55067, 56009)] * 32
    ok, reason = validate_color_shading_strip(dark, white)
    assert ok is True
    assert reason == "ok"


def test_validate_color_shading_rejects_railed_white():
    from pyopticfilm.scan.calib_gl128 import (
        validate_color_shading_strip,
    )

    dark = [(800, 900, 850)] * 32
    white = [(65535, 65535, 65535)] * 32
    ok, reason = validate_color_shading_strip(dark, white)
    assert ok is False
    assert "rail" in reason


def test_validate_color_shading_rejects_dim_white():
    from pyopticfilm.scan.calib_gl128 import (
        validate_color_shading_strip,
    )

    dark = [(800, 900, 850)] * 32
    white = [(12000, 11500, 11000)] * 32
    ok, reason = validate_color_shading_strip(dark, white)
    assert ok is False
    assert "white mean" in reason


def test_validate_color_shading_soft_accepts_high_dark():
    from pyopticfilm.scan.calib_gl128 import (
        validate_color_shading_strip,
    )

    dark = [(5000, 5500, 4800)] * 32
    white = [(50000, 52000, 51000)] * 32
    ok, reason = validate_color_shading_strip(dark, white)
    assert ok is True
    assert "soft dark" in reason


def test_validate_color_shading_gains_band():
    from pyopticfilm.scan.calib_gl128 import (
        SHADING_GAIN_MAX,
        SHADING_GAIN_UNITY,
        shading_gains_from_white,
        validate_color_shading_gains,
    )

    ok, reason = validate_color_shading_gains(shading_gains_from_white([(49610, 55067, 56009)] * 32))
    assert ok is True
    assert "median gain" in reason

    ok, reason = validate_color_shading_gains([(SHADING_GAIN_UNITY,) * 3] * 32)
    assert ok is False
    assert "already at target" in reason

    ok, reason = validate_color_shading_gains([(SHADING_GAIN_MAX,) * 3] * 32)
    assert ok is False
    assert "too dim" in reason






def test_coarse_gains_from_wide_means_match_silverfast():
    from pyopticfilm.scan.calib_gl128 import (
        AFE_SF_WHITE_TARGETS_COLOR,
        AFE_SF_WHITE_TARGETS_IR,
        coarse_gains_from_wide_means,
    )

    # SF wide-strip means (gain 0, pre-offsets) and the gains SF then wrote:
    # 8200i SE Alignment_test, 8100 V2 06_ppi_ladder, 8300i SE 1800 colour + IR.
    sf = AFE_SF_WHITE_TARGETS_COLOR
    assert coarse_gains_from_wide_means((24768, 35190, 30620), targets=sf) == (35, 25, 32)
    assert coarse_gains_from_wide_means((27316, 39037, 33349), targets=sf) == (31, 20, 28)
    assert coarse_gains_from_wide_means((27028, 37931, 31495), targets=sf) == (32, 22, 31)
    assert coarse_gains_from_wide_means((32244, 27261, 28848), targets=AFE_SF_WHITE_TARGETS_IR) == (28, 35, 33)


def test_settled_probe_targets_land_on_silverfast_gains():
    """#124: with the throwaway strip, the 8200i SE wide means rise by SF's black
    deficit; the scaled targets still give SF's warm gains (2026-10-27 A/B)."""
    from pyopticfilm.scan.calib_gl128 import AFE_WHITE_TARGETS_IR, coarse_gains_from_wide_means

    # Same scanner, minutes apart: SF-style probe after 20 s idle gave (35, 25, 32)
    # colour and (5, 15, 11) IR; these are the wide means with the throwaway strip.
    assert coarse_gains_from_wide_means((26821, 36750, 32191)) == (35, 25, 32)
    assert coarse_gains_from_wide_means((48199, 41725, 44274), targets=AFE_WHITE_TARGETS_IR) == (5, 15, 11)


def test_search_afe_codes_respects_code_min():
    from pyopticfilm.scan.calib_gl128 import search_afe_codes

    floor = 0x10

    def apply(codes: tuple[int, int, int]) -> tuple[float, float, float]:
        # Always above target so dichotomy keeps lowering toward code_min.
        del codes
        return (40000.0, 40000.0, 40000.0)

    offsets = search_afe_codes(
        initial=(0x80, 0x80, 0x80),
        code_max=0xFF,
        code_min=floor,
        target=0x3000,
        iterations=9,
        tolerance=512.0,
        code_increases_mean=True,
        apply=apply,
    )
    assert offsets == (floor, floor, floor)








def test_stationary_data_ready_ignores_busy_accepts_home_not_empty(monkeypatch):
    """Ready = not BUFEMPTY at home (0xa9/0x9c/…); reject motor-busy 0xa5."""
    import pyopticfilm.asic.gl128 as gl128_mod
    from pyopticfilm.asic.gl128 import Gl128
    from pyopticfilm.asic.status import ScannerStatus
    from pyopticfilm.exceptions import ScanError

    monkeypatch.setattr(gl128_mod.time, "sleep", lambda *_a, **_k: None)

    asic = Gl128(MagicMock(), MODEL_8200I_SE)
    # Discard read, then busy 0xa5 (no HOME), then live ready 0x9c.
    asic.read_status = MagicMock(
        side_effect=[
            ScannerStatus.from_reg41(0xDC),
            ScannerStatus.from_reg41(0xA5),
            ScannerStatus.from_reg41(0x9C),
        ]
    )
    asic._wait_stationary_data_ready(1.0, where="test")
    assert asic.read_status.call_count == 3

    asic.read_status = MagicMock(return_value=ScannerStatus.from_reg41(0xA5))
    with pytest.raises(ScanError, match="stationary data ready"):
        asic._wait_stationary_data_ready(0.0, where="test")


def test_wait_idle_at_home_requires_two_samples_without_motor(monkeypatch):
    """0xcd (HOME|BUFEMPTY|LAMP|MOTORENB) is not idle; 0xcc (motor off) is."""
    import pyopticfilm.asic.gl128 as gl128_mod
    from pyopticfilm.asic.gl128 import Gl128
    from pyopticfilm.asic.status import ScannerStatus

    monkeypatch.setattr(gl128_mod.time, "sleep", lambda *_a, **_k: None)

    asic = Gl128(MagicMock(), MODEL_8200I_SE)
    asic.read_status = MagicMock(
        side_effect=[
            ScannerStatus.from_reg41(0xCD),
            ScannerStatus.from_reg41(0xCC),
            ScannerStatus.from_reg41(0xCC),
        ]
    )
    asic._wait_idle_at_home_for_stationary(1.0, where="test")
    assert asic.read_status.call_count == 3


def test_acquire_afe_strip_waits_out_motor_busy_then_absolute_scan(monkeypatch):
    """Consecutive scans leave 0xcd; wait idle, then SCAN via write not RMW."""
    import pyopticfilm.asic.gl128 as gl128_mod
    from pyopticfilm.asic.gl128 import Gl128
    from pyopticfilm.asic.registers import Gl128Registers
    from pyopticfilm.asic.status import ScannerStatus
    from pyopticfilm.scan.calib_gl128 import AFE_STRIP_BYTES, AFE_STRIP_DUMMY_PRE

    monkeypatch.setattr(gl128_mod.time, "sleep", lambda *_a, **_k: None)

    r = Gl128Registers()

    def _read_reg(addr, *_a, **_k):
        addr = int(addr)
        if addr == r.REG_0x01:
            return 0x22  # cancel leftover: SHDAREA|DVDSET, SCAN clear
        if addr == r.REG_0x02:
            return 0x00
        return 0

    proto = MagicMock()
    proto.read_register = MagicMock(side_effect=_read_reg)
    proto.bulk_read_chunk = MagicMock(return_value=b"\x00" * AFE_STRIP_BYTES)
    asic = Gl128(proto, MODEL_8200I_SE)
    asic._initialized = True
    asic.read_status = MagicMock(
        side_effect=[
            ScannerStatus.from_reg41(0xCD),
            ScannerStatus.from_reg41(0xCC),
            ScannerStatus.from_reg41(0xDC),
            ScannerStatus.from_reg41(0x9C),
        ]
    )

    buf = asic.acquire_afe_strip(AFE_STRIP_BYTES)
    assert len(buf) == AFE_STRIP_BYTES

    written_01 = [
        int(c.args[1]) & 0xFF
        for c in proto.write_register.call_args_list
        if c.args and int(c.args[0]) == r.REG_0x01
    ]
    scan_writes = [v for v in written_01 if v & r.SCAN]
    assert scan_writes, "expected an absolute SCAN write to 0x01"
    assert all((v & r.DVDSET) == 0 for v in scan_writes)
    start_go = [
        c
        for c in proto.write_register.call_args_list
        if c.args and int(c.args[0]) == r.REG_START and int(c.args[1]) & r.START_GO
    ]
    assert len(start_go) == 1
    # Default timing is SF's pre-offset narrow probe.
    assert asic._reg_cache.get(0x2B) == AFE_STRIP_DUMMY_PRE


def test_acquire_afe_strip_retries_start_after_data_timeout(monkeypatch):
    import pyopticfilm.asic.gl128 as gl128_mod
    from pyopticfilm.asic.gl128 import Gl128
    from pyopticfilm.asic.registers import Gl128Registers
    from pyopticfilm.exceptions import ScanError
    from pyopticfilm.scan.calib_gl128 import AFE_STRIP_BYTES

    monkeypatch.setattr(gl128_mod.time, "sleep", lambda *_a, **_k: None)

    r = Gl128Registers()
    proto = MagicMock()
    proto.read_register = MagicMock(return_value=0x00)
    proto.bulk_read_chunk = MagicMock(return_value=b"\x00" * AFE_STRIP_BYTES)
    asic = Gl128(proto, MODEL_8200I_SE)
    asic._initialized = True
    asic._reset_stationary_scan_engine = MagicMock()
    asic._wait_stationary_data_ready = MagicMock(
        side_effect=[ScanError("AFE strip: no stationary data ready"), None]
    )

    buf = asic.acquire_afe_strip(AFE_STRIP_BYTES)
    assert len(buf) == AFE_STRIP_BYTES
    start_go = [
        c
        for c in proto.write_register.call_args_list
        if c.args and int(c.args[0]) == r.REG_START and int(c.args[1]) & r.START_GO
    ]
    assert len(start_go) == 2
    assert asic._wait_stationary_data_ready.call_count == 2


def test_motorized_shading_data_ready_accepts_busy_without_home(monkeypatch):
    """Colour white (DVDSET+MTRPWR) may leave home — accept not BUFEMPTY / 0xa5."""
    import pyopticfilm.asic.gl128 as gl128_mod
    from pyopticfilm.asic.gl128 import Gl128
    from pyopticfilm.asic.status import ScannerStatus
    from pyopticfilm.exceptions import ScanError

    monkeypatch.setattr(gl128_mod.time, "sleep", lambda *_a, **_k: None)

    asic = Gl128(MagicMock(), MODEL_8200I_SE)
    asic.read_status = MagicMock(
        side_effect=[
            ScannerStatus.from_reg41(0xDC),  # discard
            ScannerStatus.from_reg41(0xE5),  # empty, no home
            ScannerStatus.from_reg41(0xA5),  # not empty, motor busy, no home
        ]
    )
    asic._wait_shading_data_ready(1.0, where="white", motorized=True)
    assert asic.read_status.call_count == 3

    # Stationary path still rejects the same busy code.
    asic.read_status = MagicMock(return_value=ScannerStatus.from_reg41(0xA5))
    with pytest.raises(ScanError, match="stationary data ready"):
        asic._wait_shading_data_ready(0.0, where="dark", motorized=False)


def test_apply_stationary_scan_regs_writes_capture_mode_block():
    from pyopticfilm.asic.gl128 import Gl128

    proto = MagicMock()
    asic = Gl128(proto, MODEL_8200I_SE)
    asic._apply_stationary_scan_regs()
    written = dict(proto.write_registers_batched.call_args_list[0][0][0])
    written.update(dict(proto.write_registers_batched.call_args_list[1][0][0]))
    assert written[0x04] == 0x42
    assert written[0x05] == 0x40
    assert 0xD0 in written
    assert 0x03 not in written


def test_shading_strip_setup_sets_dvdset_when_requested():
    from pyopticfilm.asic.gl128 import Gl128
    from pyopticfilm.asic.registers import Gl128Registers

    r = Gl128Registers()
    proto = MagicMock()
    proto.write_u24 = MagicMock()
    proto.write_u16 = MagicMock()
    proto.read_register = MagicMock(
        side_effect=lambda addr: {
            0x01: 0x22,
            0x02: 0x10,
            0x04: 0x42,
            0x05: 0x40,
            0x2B: 0x02,
            0xA5: 0x02,
            0xAB: 0x02,
        }.get(addr, 0)
    )
    asic = Gl128(proto, MODEL_8200I_SE)
    asic._apply_stationary_scan_regs = MagicMock()
    asic._setup_shading_strip_regs(pixels=100, lines=128, resolution=1800, dvdset=True)
    asic._apply_stationary_scan_regs.assert_not_called()
    # Geometry is written on both strips so the white pass never inherits a stale window.
    assert proto.write_u24.call_count >= 4
    assert asic._reg_cache[0x01] & 0x20  # DVDSET
    # SF white uses MTRPWR; NegPy ORs AGOHOME so SCAN-clear parks on live HW.
    assert asic._reg_cache[r.REG_0x02] & r.MTRPWR
    assert asic._reg_cache[r.REG_0x02] & r.AGOHOME
    # SF white clears 0x3B/0xA3; _SCAN_REGS alone would leave 0x3B=1.
    assert asic._reg_cache[0x3B] == 0x00
    assert asic._reg_cache[0xA3] == 0x00
    # Strip clocks follow the strip's own window (SilverFast's width rule).
    start, end, _ = asic._shading_window(pixels=100, resolution=1800, strpixel=None, endpixel=None, dpiset=None)
    white = MODEL_8200I_SE.shading_strip_clocks(1800, dvdset=True, span=end - start)
    assert (asic._reg_cache[0x2B], asic._reg_cache[0xA5], asic._reg_cache[0xAB]) == white
    proto.write_u24.reset_mock()
    asic._setup_shading_strip_regs(pixels=100, lines=128, resolution=1800, dvdset=False)
    asic._apply_stationary_scan_regs.assert_called_once()
    assert proto.write_u24.call_count >= 4  # LINCNT, STR, END, FEEDL, ...
    assert not (asic._reg_cache[0x01] & 0x20)
    assert asic._reg_cache[r.REG_0x02] == 0x00
    assert asic._reg_cache[0xA3] == 0x01  # SF dark
    dark_dummy, dark_a, dark_b = MODEL_8200I_SE.shading_strip_clocks(1800, dvdset=False, span=end - start)
    assert asic._reg_cache[0x2B] == dark_dummy
    assert asic._reg_cache[0xA5] == dark_a
    assert asic._reg_cache[0xAB] == dark_b


def test_capture_lperiod_matches_session_13_crop_and_full_frame():
    # Session 13 ladder crop (span 10224) and SF full frames (span 10368).
    for dpi, crop, full in ((1200, 11277, 11283), (1800, 11490, 11499), (3600, 13407, 13443), (7200, 15963, 16035)):
        assert MODEL_8200I_SE.line_period_for(dpi, span=10224) == crop
        assert MODEL_8200I_SE.line_period_for(dpi) == full


def test_upload_tables_slope_false_skips_slope_ahb():
    from pyopticfilm.asic.gl128 import Gl128
    from pyopticfilm.asic.registers import Gl128Registers

    r = Gl128Registers()
    proto = MagicMock()
    proto.write_ahb = MagicMock()
    asic = Gl128(proto, MODEL_8200I_SE)
    asic.upload_tables(resolution=1800, shading=False, slope=False)
    addrs = [int(c.args[0]) for c in proto.write_ahb.call_args_list]
    assert r.AHB_SLOPE_SCAN not in addrs
    assert r.AHB_SLOPE_FAST not in addrs
    assert r.AHB_SCAN_MOTOR_0 in addrs
    assert r.AHB_SCAN_MOTOR_1 in addrs
    assert r.AHB_SCAN_MOTOR_2 in addrs


def test_run_asic_shading_calls_exposure_only_after_unity(monkeypatch):
    """Unity→white window must not push slow slope (session 03/04)."""
    import pyopticfilm.asic.gl128 as gl128_mod
    from pyopticfilm.asic.gl128 import Gl128
    from pyopticfilm.scan.calib_gl128 import AfeFrontend

    monkeypatch.setattr(gl128_mod.time, "sleep", lambda *_a, **_k: None)

    proto = MagicMock()
    asic = Gl128(proto, MODEL_8200I_SE)
    asic._initialized = True
    asic._motor_moves_enabled = False
    asic.last_afe = AfeFrontend(gains=(19, 16, 18), offsets=(16, 16, 16))
    asic.apply_frontend = MagicMock()
    asic._require_carriage_at_home = MagicMock()
    asic.lamp_off = MagicMock()
    asic.lamp_on = MagicMock()
    asic._reassert_lamp_off = MagicMock()
    asic._log_lamp_off_state = MagicMock()
    asic._log_lamp_state = MagicMock()
    asic._await_colour_optical_dark = MagicMock()
    asic.upload_shading_table = MagicMock()
    n = 8
    strip = b"\x00\x04" * (n * 128 * 3)
    white = b"\x00\xd0" * (n * 128 * 3)
    asic.acquire_shading_strip = MagicMock(side_effect=[strip, white])
    calls: list[dict] = []

    def _upload(
        *, resolution: int, shading: bool = False, slope: bool = True, channel_exposure: int | None = None
    ):
        calls.append(
            {"resolution": resolution, "shading": shading, "slope": slope, "exposure": channel_exposure}
        )

    asic.upload_tables = MagicMock(side_effect=_upload)
    monkeypatch.setattr(
        gl128_mod,
        "average_rgb16_columns",
        MagicMock(
            side_effect=[
                [(900, 970, 990)] * n,
                [(50000, 52000, 51000)] * n,
            ]
        ),
    )

    asic.run_asic_shading(resolution=1800, strpixel=240, endpixel=240 + n * 4, dpiset=300)
    assert calls, "expected upload_tables after unity"
    assert calls[0]["slope"] is False
    # SilverFast's white-strip motor table (exposure / 4 at LINESEL 0).
    assert calls[0]["exposure"] == 3500
    # One unity upload, one measured upload — no re-upload after arming 0x22.
    assert asic.upload_shading_table.call_count == 2
    assert asic.asic_shading_ready is True


def test_measure_colour_falls_back_to_host_calib_when_dvdset_white_raw():
    """Host-fake ASIC whites cause diamond — calibrator must host-stretch instead."""
    from pyopticfilm.scan.calib_gl128 import AfeFrontend

    geo = _geometry(1800)
    asic = _asic(shading_ready=False)
    asic.search_afe = MagicMock(return_value=None)
    asic.last_afe = AfeFrontend(gains=(18, 15, 18), offsets=(16, 16, 16))
    asic.asic_shading_ready = False
    asic.last_color_shading_host_ok = True
    asic.last_host_calib_dark = [(900, 970, 990)] * 64
    asic.last_host_calib_white = [(40000, 45000, 42000)] * 64
    asic.last_color_shading_reject_reason = "white mean hot"
    asic.run_asic_shading = MagicMock(return_value=b"\x00" * 100)

    cal = Calibrator(asic, cache_path=None, model=MODEL_8200I_SE)
    entry = cal.measure_colour_asic_shading(geo)
    assert entry.asic_shading is False
    assert entry.shading_blob is None
    assert entry.dark.shape[0] == geo.pixels
    assert int(entry.white[0, 0]) == 40000
    assert cal.prefer_asic_shading is False
    assert cal.should_apply_host_calib() is True


def test_await_colour_optical_dark_retries_once_then_raises(monkeypatch):
    import pyopticfilm.asic.gl128 as gl128_mod
    from pyopticfilm.asic.gl128 import Gl128
    from pyopticfilm.exceptions import ScanError
    from pyopticfilm.scan.calib_gl128 import AFE_STRIP_PIXELS

    monkeypatch.setattr(gl128_mod.time, "sleep", lambda *_a, **_k: None)

    sample = (20000).to_bytes(2, "little")
    strip = (sample * 3) * AFE_STRIP_PIXELS
    asic = Gl128(MagicMock(), MODEL_8200I_SE)
    asic._initialized = True
    asic.usb_planar_rgb = False
    asic.last_afe = None
    asic.acquire_afe_strip = MagicMock(return_value=strip)
    asic.apply_frontend = MagicMock()
    asic._reassert_lamp_off = MagicMock()
    asic._log_lamp_off_state = MagicMock()
    asic.read_status = MagicMock()

    with pytest.raises(ScanError, match="lamp-off strip still bright"):
        asic._await_colour_optical_dark()
    assert asic.acquire_afe_strip.call_count == 2


def test_await_colour_optical_dark_accepts_dark_probe(monkeypatch):
    import pyopticfilm.asic.gl128 as gl128_mod
    from pyopticfilm.asic.gl128 import Gl128
    from pyopticfilm.scan.calib_gl128 import AFE_STRIP_PIXELS

    monkeypatch.setattr(gl128_mod.time, "sleep", lambda *_a, **_k: None)

    sample = (800).to_bytes(2, "little")
    strip = (sample * 3) * AFE_STRIP_PIXELS
    asic = Gl128(MagicMock(), MODEL_8200I_SE)
    asic._initialized = True
    asic.usb_planar_rgb = False
    asic.last_afe = None
    asic.acquire_afe_strip = MagicMock(return_value=strip)
    asic.apply_frontend = MagicMock()

    asic._await_colour_optical_dark()
    assert asic.acquire_afe_strip.call_count == 1


def test_validate_rejects_starved_channel():
    from pyopticfilm.scan.calib_gl128 import (
        COLOR_SHADING_CHANNEL_WHITE_MIN,
        validate_color_shading_strip,
    )

    assert COLOR_SHADING_CHANNEL_WHITE_MIN >= 8000
    dark = [(900, 1000, 1100)] * 32
    white = [(55000, 56000, 4000)] * 32
    ok, reason = validate_color_shading_strip(dark, white)
    assert ok is False
    assert "ch2 white" in reason


def test_infrared_cache_miss_does_not_force_colour_calib(tmp_path: Path, monkeypatch):
    asic = _asic()
    cal = Calibrator(asic, cache_path=tmp_path / "calib.json", model=MODEL_8200I_SE)
    geometry = _geometry()
    order: list[str] = []
    monkeypatch.setattr(cal, "ensure_colour_asic_shading", lambda *_a, **_k: order.append("ensure"))
    session = ScanSession(asic, MODEL_8200I_SE, cal)
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
    session.run(resolution=geometry.resolution, mode="infrared", geometry=geometry)
    assert order == ["acquire"]


def test_infrared_clears_dvdset_even_when_colour_shading_ready():
    from dataclasses import replace

    from pyopticfilm.asic.registers import Gl128Registers
    from pyopticfilm.scan.session_gl128 import Gl128ScanSession

    asic = _asic()
    asic.asic_shading_ready = True
    asic._scan_method = "infrared"
    asic._motor_moves_enabled = False
    asic._reg_cache = {}
    asic.last_afe = None
    wrote: dict[int, int] = {}

    def write_registers_batched(items):
        for addr, val in items:
            wrote[int(addr)] = int(val)
            asic._reg_cache[int(addr)] = int(val)

    asic.protocol.write_registers_batched.side_effect = write_registers_batched
    asic.upload_tables = MagicMock()
    asic.apply_frontend = MagicMock()
    asic.lamp_on = MagicMock()
    asic.lamp_off = MagicMock()
    geometry = replace(_geometry(1800), disable_buffer_full_move=False)
    session = Gl128ScanSession(asic, MODEL_8200I_SE)
    session._configure(geometry)
    r = Gl128Registers()
    assert r.REG_0x01 in wrote
    assert wrote[r.REG_0x01] & r.DVDSET == 0
    assert wrote[r.REG_0x01] & r.SHDAREA


def test_infrared_session_populates_flattened_ir_plane(tmp_path: Path, monkeypatch):
    asic = _asic()
    asic.ir_host_flatten_ready = False
    cal = Calibrator(asic, cache_path=tmp_path / "calib.json", model=MODEL_8200I_SE)
    geometry = _geometry()
    session = ScanSession(asic, MODEL_8200I_SE, cal)
    monkeypatch.setattr(session, "acquire_raw", lambda *_a, **_k: b"\x00" * 64)

    def fake_assemble(*_a, **_k):
        w = min(64, geometry.pixels)
        rgb = np.zeros((8, w, 3), dtype=np.uint16)
        # Uneven green column levels + a dark dust hole.
        for x in range(w):
            rgb[:, x, 1] = 20000 + x * 400
        rgb[3:5, w // 2, 1] = 2000
        return rgb

    monkeypatch.setattr(session.pipeline, "assemble", fake_assemble)
    image = session.run(resolution=geometry.resolution, mode="infrared", geometry=geometry)
    assert image.ir is not None
    assert image.ir.ndim == 2
    assert image.ir.shape[0] == 8
    # Flatten lifts the bright field; dust stays relatively darker.
    assert float(np.median(image.ir)) > 30000
    assert float(image.ir[4, image.ir.shape[1] // 2]) < float(np.median(image.ir)) * 0.5


def test_host_calib_exposure_makeup_lifts_dim_film_window():
    """Home-chrome stretch leaves film-window peaks low — makeup to near white."""
    from pyopticfilm.scan.pipeline import (
        HOST_CALIB_PEAK_TARGET,
        ImagePipeline,
    )

    pipe = ImagePipeline(MODEL_8200I_SE)
    w = 32
    # Raw: dark~1000, mid film~8000, bright film~16000; home white~50000.
    rgb = np.full((16, w, 3), 8000, dtype=np.uint16)
    rgb[:, w // 4 : 3 * w // 4, :] = 16000
    dark = np.full((w, 3), 1000, dtype=np.uint16)
    white = np.full((w, 3), 50000, dtype=np.uint16)
    out = pipe.apply_host_calib(rgb, dark=dark, white=white)
    peak = float(np.percentile(out, 99.7))
    assert peak >= HOST_CALIB_PEAK_TARGET * 0.95
    # Midtones must lift with the peak (not only clip edges).
    assert float(np.median(out)) > 20000


def test_host_calib_border_clamp_caps_hot_margins():
    """Holder chrome brighter than film inset must not stay above film peak."""
    from pyopticfilm.scan.pipeline import ImagePipeline

    pipe = ImagePipeline(MODEL_8200I_SE)
    h, w = 100, 100
    rgb = np.full((h, w, 3), 20000, dtype=np.uint16)
    rgb[10:90, 10:90, :] = 25000
    rgb[:4, :, :] = 65535
    rgb[-4:, :, :] = 65535
    rgb[:, :4, :] = 65535
    rgb[:, -4:, :] = 65535
    out = pipe.clamp_border_highlights(rgb)
    assert int(out[:4].max()) <= int(out[10:90, 10:90].max())
    assert int(out[40:60, 40:60].mean()) == 25000


def test_border_clamp_ceiling_is_per_channel():
    """A joint ceiling leaves the margin above a dim channel's own film peak.

    Session 004: green's film window peaked at 19306 while the joint p99.7 was
    27432, so the clamped chrome became green's brightest content and auto Dmin
    metered it — a 1.42x green lift in the positive.
    """
    from pyopticfilm.scan.pipeline import ImagePipeline

    pipe = ImagePipeline(MODEL_8200I_SE)
    h, w = 200, 200
    rgb = np.zeros((h, w, 3), dtype=np.uint16)
    rgb[:, :, 0] = 27000
    rgb[:, :, 1] = 19300
    rgb[:, :, 2] = 27600
    rgb[:6, :, :] = 65535
    rgb[-6:, :, :] = 65535
    rgb[:, :6, :] = 65535
    rgb[:, -6:, :] = 65535
    out = pipe.clamp_border_highlights(rgb)
    film = out[20:180, 20:180]
    for c, expected in enumerate((27000, 19300, 27600)):
        assert int(film[:, :, c].max()) == expected
        assert int(out[:6, :, c].max()) <= expected + 1


def test_assemble_exposes_and_clamps_on_the_asic_shading_path():
    """DVDSET references home chrome, so both makeup and clamp must still run."""
    from pyopticfilm.scan.pipeline import ImagePipeline

    pipe = ImagePipeline(MODEL_8200I_SE)
    seen: list[str] = []
    pipe.expose_film_base = lambda rgb, **_kw: (seen.append("expose") or rgb)  # type: ignore[method-assign]
    pipe.clamp_border_highlights = lambda rgb, **_kw: (seen.append("clamp") or rgb)  # type: ignore[method-assign]
    geometry = _geometry(1800)
    rgb = np.zeros((geometry.lines, geometry.pixels, 3), dtype=np.uint16)
    pipe.decode_rgb = lambda *_a, **_k: rgb  # type: ignore[method-assign]
    pipe.assemble(b"", geometry, dark=None, white=None, planar=False)
    assert seen == ["expose", "clamp"]


def test_expose_film_base_keeps_the_orange_mask_ratios():
    """The makeup must be scalar — a per-channel lift neutralizes the mask."""
    from pyopticfilm.scan.pipeline import (
        HOST_CALIB_PEAK_TARGET,
        ImagePipeline,
    )

    pipe = ImagePipeline(MODEL_8200I_SE)
    h, w = 120, 120
    rgb = np.zeros((h, w, 3), dtype=np.uint16)
    # Session 004 film-window levels: base at 42% of scale, green 0.71x of red.
    rgb[:, :, 0] = 27046
    rgb[:, :, 1] = 19306
    rgb[:, :, 2] = 27695
    out = pipe.expose_film_base(rgb, source="test")
    peak = float(out.max())
    assert 0.98 <= peak / HOST_CALIB_PEAK_TARGET <= 1.02
    before = 19306 / 27046
    after = float(out[:, :, 1].mean()) / float(out[:, :, 0].mean())
    assert abs(after - before) < 0.005
