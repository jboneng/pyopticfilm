# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic hunt: host calib × R/G/B Y misregistration → green edge fringe.

Recreates the 8300i tester report offline (no HW): near-horizontal high-contrast
edges pick up a green fringe when channels are vertically offset and
``apply_host_calib`` runs — the path taken when ASIC DVDSET shading is rejected
(``host_stretch=True`` in the GL128 log).
"""

from __future__ import annotations

import numpy as np
import pytest

from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE
from pyopticfilm.scan.calibrate import CalibEntry, Calibrator
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.scan.pipeline import ImagePipeline


def _step_edge(h: int, w: int, *, edge_row: int, lo: int = 8_000, hi: int = 48_000) -> np.ndarray:
    """Horizontal step edge (dark above, bright below), aligned RGB."""
    rgb = np.empty((h, w, 3), dtype=np.uint16)
    rgb[:edge_row] = lo
    rgb[edge_row:] = hi
    return rgb


def _shift_channels(rgb: np.ndarray, *, dg: int, db: int) -> np.ndarray:
    """Introduce residual G/B vertical misregistration (R stays put)."""
    h, _, _ = rgb.shape
    out = np.zeros_like(rgb)
    out[:, :, 0] = rgb[:, :, 0]
    if dg >= 0:
        out[dg:, :, 1] = rgb[: h - dg, :, 1]
    else:
        out[: h + dg, :, 1] = rgb[-dg:, :, 1]
    if db >= 0:
        out[db:, :, 2] = rgb[: h - db, :, 2]
    else:
        out[: h + db, :, 2] = rgb[-db:, :, 2]
    return out


def _flat_calib(w: int, *, dark: int = 1000, white_rgb: tuple[int, int, int] = (40_000, 40_000, 40_000)):
    d = np.full((w, 3), dark, dtype=np.uint16)
    wh = np.empty((w, 3), dtype=np.uint16)
    wh[:, 0] = white_rgb[0]
    wh[:, 1] = white_rgb[1]
    wh[:, 2] = white_rgb[2]
    return d, wh


def _edge_green_excess(rgb: np.ndarray, edge_row: int, *, band: int = 2) -> float:
    """Mean (G - max(R,B)) in a band around the edge; >0 ⇒ green fringe."""
    y0 = max(0, edge_row - band)
    y1 = min(rgb.shape[0], edge_row + band)
    slab = rgb[y0:y1].astype(np.float32)
    return float(np.mean(slab[:, :, 1] - np.maximum(slab[:, :, 0], slab[:, :, 2])))


def _edge_chroma(rgb: np.ndarray, edge_row: int, *, band: int = 2) -> float:
    """Mean |R-G| + |G-B| around the edge (channel disagreement)."""
    y0 = max(0, edge_row - band)
    y1 = min(rgb.shape[0], edge_row + band)
    slab = rgb[y0:y1].astype(np.float32)
    return float(np.mean(np.abs(slab[:, :, 0] - slab[:, :, 1]) + np.abs(slab[:, :, 1] - slab[:, :, 2])))


@pytest.mark.parametrize(
    "dg,db,white_rgb",
    [
        (1, 2, (40_000, 40_000, 40_000)),  # neutral stretch
        (1, 2, (42_000, 28_000, 38_000)),  # G home dimmer → higher G gain
        (2, 4, (40_000, 40_000, 40_000)),
    ],
)
def test_host_calib_amplifies_green_fringe_on_misregistered_edge(dg, db, white_rgb):
    """H1: residual channel offset + host calib → green edge fringe."""
    h, w, edge = 64, 128, 32
    pipe = ImagePipeline(MODEL_8300I_SE)
    aligned = _step_edge(h, w, edge_row=edge)
    misreg = _shift_channels(aligned, dg=dg, db=db)
    dark, white = _flat_calib(w, white_rgb=white_rgb)

    base_chroma = _edge_chroma(misreg, edge)

    # expose_base=False isolates column stretch from film-base makeup.
    stretched = pipe.apply_host_calib(misreg, dark=dark, white=white, expose_base=False)
    stretch_chroma = _edge_chroma(stretched, edge)
    stretch_green = _edge_green_excess(stretched, edge)

    aligned_stretch = pipe.apply_host_calib(aligned, dark=dark, white=white, expose_base=False)
    # Uneven channel whites re-tint a neutral step edge; only neutral calib stays fringe-free.
    if white_rgb[0] == white_rgb[1] == white_rgb[2]:
        assert _edge_chroma(aligned_stretch, edge) < 500.0

    # Misregistration already creates chroma; host stretch must not erase it,
    # and with uneven channel whites must make green excess worse.
    assert base_chroma > 1_000.0
    assert stretch_chroma >= base_chroma * 0.9
    if white_rgb[1] < white_rgb[0] * 0.85:
        # Dimmer G home white ⇒ higher G gain vs neutral calib ⇒ greener edge band.
        dark_n, white_n = _flat_calib(w, white_rgb=(40_000, 40_000, 40_000))
        neutral = pipe.apply_host_calib(misreg, dark=dark_n, white=white_n, expose_base=False)
        assert stretch_green > _edge_green_excess(neutral, edge) + 500.0


def test_host_calib_neutral_preserves_misreg_chroma_ratio():
    """Neutral dark/white scales all channels equally — fringe stays, no new green bias."""
    h, w, edge = 64, 128, 32
    pipe = ImagePipeline(MODEL_8300I_SE)
    # Keep highlights below host-calib clip so edge-band chroma scales linearly.
    misreg = _shift_channels(_step_edge(h, w, edge_row=edge, lo=8_000, hi=36_000), dg=1, db=2)
    dark, white = _flat_calib(w, white_rgb=(40_000, 40_000, 40_000))
    out = pipe.apply_host_calib(misreg, dark=dark, white=white, expose_base=False)
    # Equal gains ≈ 65535/39000; chroma scales by the same factor when nothing clips.
    scale = 65535.0 / (40_000.0 - 1000.0)
    assert _edge_chroma(out, edge) == pytest.approx(_edge_chroma(misreg, edge) * scale, rel=0.05)


def test_skipping_host_calib_keeps_raw_misreg_only():
    """Control: without host calib, fringe is pure geometric misregistration."""
    h, w, edge = 64, 128, 32
    misreg = _shift_channels(_step_edge(h, w, edge_row=edge), dg=1, db=2)
    # One edge row is hi in R and lo in G → large |R-G|.
    assert _edge_chroma(misreg, edge) > 5_000.0
    assert _edge_green_excess(_step_edge(h, w, edge_row=edge), edge) < 1.0


def test_expose_base_does_not_create_fringe_on_aligned_edge():
    h, w, edge = 80, 160, 40
    pipe = ImagePipeline(MODEL_8300I_SE)
    aligned = _step_edge(h, w, edge_row=edge, lo=12_000, hi=28_000)
    dark, white = _flat_calib(w, white_rgb=(50_000, 50_000, 50_000))
    out = pipe.apply_host_calib(aligned, dark=dark, white=white, expose_base=True)
    assert _edge_chroma(out, edge) < 500.0


@pytest.mark.parametrize("dpi", [1200, 1440, 1800, 2400, 3600, 7200])
def test_gl128_line_shift_scales_with_asic_dpi(dpi: int):
    geo = compute_geometry(dpi, model=MODEL_8300I_SE, area=None)
    assert geo.shift_r == 0
    # 8300i / 8200i SE round; V2 still floors via geometry default.
    assert geo.shift_g == MODEL_8300I_SE.scaled_ld_shift(
        MODEL_8300I_SE.ld_shift_g, geo.asic_dpi
    )
    assert geo.shift_b == MODEL_8300I_SE.scaled_ld_shift(
        MODEL_8300I_SE.ld_shift_b, geo.asic_dpi
    )
    if dpi == 1440:
        assert geo.shift_g == 5  # round(4.8)
        assert geo.shift_b == 10  # round(9.6)
    if dpi == 1200:
        assert geo.shift_g == 4
        assert geo.shift_b == 8


def test_8300i_ld_shift_round_clears_1440_residual():
    """8300i rounds ld_shift; floor residual at 1440 is gone."""
    base = MODEL_8300I_SE.motor_base_ydpi
    geo = compute_geometry(1440, model=MODEL_8300I_SE, area=None)
    ideal_g = MODEL_8300I_SE.ld_shift_g * 1440 / base
    ideal_b = MODEL_8300I_SE.ld_shift_b * 1440 / base
    assert abs(ideal_g - geo.shift_g) == pytest.approx(0.2)  # 4.8→5
    assert abs(ideal_b - geo.shift_b) == pytest.approx(0.4)  # 9.6→10
    # Floor would have left 0.8 / 0.6 — round is strictly closer.
    assert abs(ideal_g - geo.shift_g) < 0.8
    assert abs(ideal_b - geo.shift_b) < 0.6


def test_se_rounds_ld_shift_at_1440():
    geo = compute_geometry(1440, model=MODEL_8200I_SE, area=None)
    assert geo.shift_g == MODEL_8200I_SE.scaled_ld_shift(
        MODEL_8200I_SE.ld_shift_g, 1440
    )
    assert geo.shift_b == MODEL_8200I_SE.scaled_ld_shift(
        MODEL_8200I_SE.ld_shift_b, 1440
    )
    assert geo.shift_g == 5
    assert geo.shift_b == 10


def test_v2_rounds_ld_shift_at_1440():
    """8100 V2 TIFFs (2026-10-16) showed the same floor residual (G +0.8 / B +0.6 rows)."""
    geo = compute_geometry(1440, model=MODEL_8100_V2, area=None)
    assert geo.shift_g == 5
    assert geo.shift_b == 10


def test_v2_host_stagger_table():
    """8100 V2 unstagger: (4, 0) @7200, 4 x dpi / 7200 @1440/2400 (0.8 / 1.333), nothing elsewhere.

    The fractional patterns keep one spare row (``ceil(d / 2)``), as ``(1, 0)`` did.
    """
    g7200 = compute_geometry(7200, model=MODEL_8100_V2, area=None)
    g2400 = compute_geometry(2400, model=MODEL_8100_V2, area=None)
    g1800 = compute_geometry(1800, model=MODEL_8100_V2, area=None)
    g1440 = compute_geometry(1440, model=MODEL_8100_V2, area=None)
    assert g7200.stagger_y == (4, 0)
    assert g7200.num_staggered_lines == 4
    assert g2400.stagger_y == (1.25, 0)
    assert g2400.num_staggered_lines == 1
    assert g1440.stagger_y == (0.8, 0)
    assert g1440.num_staggered_lines == 1
    assert g1800.stagger_y == ()
    assert g1800.num_staggered_lines == 0


def test_8300i_host_stagger_at_7200():
    """8300i SE SilverFast 7200 capture: 3.95-row even/odd offset, uncorrected before."""
    g = compute_geometry(7200, model=MODEL_8300I_SE, area=None)
    assert g.stagger_y == (4, 0)
    assert g.num_staggered_lines == 4
    assert g.stagger_phase % 2 == 0
    pipe = ImagePipeline(MODEL_8300I_SE)
    rgb = np.zeros((g.optical_line_count, 4, 3), dtype=np.uint16)
    rgb = pipe.reduce_y_oversample(rgb, g)
    rgb = pipe.apply_line_shifts(rgb, g)
    rgb = pipe.apply_y_stagger(rgb, g)
    assert rgb.shape[0] >= g.frame_lines


def test_se_host_stagger_at_7200():
    """8200i SE 2026-11-03 TIFFs: even/odd columns 4 rows apart at 7200, as on the V2.

    The spare lines come out of the margin, not LINCNT: the full frame keeps
    SilverFast's travel and the 7200-row frame, as the V2 did in eada6d2.
    """
    g = compute_geometry(7200, model=MODEL_8200I_SE, area=None)
    assert g.stagger_y == (4, 0)
    assert g.num_staggered_lines == 4
    assert g.stagger_phase == g.pixel_startx == 242
    assert g.optical_line_count == 14506
    assert g.lines == 7249
    assert g.frame_lines == 7200


@pytest.mark.parametrize("model", [MODEL_8200I_SE, MODEL_8100_V2, MODEL_8300I_SE])
@pytest.mark.parametrize("dpi", [1440, 2400, 7200])
def test_gl128_full_frame_stagger_phase_is_even(model, dpi):
    """The patterns were measured on full frames; an even phase keeps them as measured."""
    g = compute_geometry(dpi, model=model, area=None)
    if g.stagger_y:
        assert g.stagger_phase % 2 == 0


def test_non_gl128_stagger_phase_stays_buffer_column():
    """GL845 leaves keep buffer-column parity, even where the full frame starts odd."""
    from pyopticfilm.device.model_8200i import MODEL_8200I

    g = compute_geometry(7200, model=MODEL_8200I, area=None)
    assert g.stagger_y == (4, 0)
    assert g.pixel_startx % 2 == 1
    assert g.stagger_phase == 0


def _staggered_edge(h: int, w: int, *, edge_row: int, odd_lag: int, phase: int) -> np.ndarray:
    """Step edge where even sensor columns see the edge ``odd_lag`` rows late in
    buffer order (sensor column = ``phase + x``); the pattern (odd_lag, 0) undoes it."""
    rgb = np.empty((h, w, 3), dtype=np.uint16)
    for x in range(w):
        lag = odd_lag if (x + phase) % 2 == 0 else 0
        rgb[:, x] = 8_000
        rgb[edge_row + lag :, x] = 48_000
    return rgb


@pytest.mark.parametrize("pixel_startx", [242, 2803])
def test_apply_y_stagger_follows_sensor_column(pixel_startx):
    """Even and odd crop starts both come out flat: the pattern rotates with pixel_startx."""
    g = compute_geometry(7200, model=MODEL_8200I_SE, area=None)
    from dataclasses import replace

    g = replace(g, lines=40, pixel_startx=pixel_startx, stagger_phase=pixel_startx)
    raw = _staggered_edge(48, 16, edge_row=20, odd_lag=4, phase=pixel_startx)
    out = ImagePipeline(MODEL_8200I_SE).apply_y_stagger(raw, g)
    assert out.shape == (40, 16, 3)
    # Every column now has its edge on the same row.
    edge_rows = {int(np.argmax(out[:, x, 1] > 20_000)) for x in range(16)}
    assert len(edge_rows) == 1


def test_apply_y_stagger_odd_start_without_phase_doubles_the_comb():
    """Regression guard: indexing by buffer column on an odd start leaves 2x the offset."""
    g = compute_geometry(7200, model=MODEL_8200I_SE, area=None)
    from dataclasses import replace

    g = replace(g, lines=40, pixel_startx=2803, stagger_phase=0)
    raw = _staggered_edge(48, 16, edge_row=20, odd_lag=4, phase=2803)
    out = ImagePipeline(MODEL_8200I_SE).apply_y_stagger(raw, g)
    edge_rows = sorted({int(np.argmax(out[:, x, 1] > 20_000)) for x in range(16)})
    assert edge_rows[-1] - edge_rows[0] == 8


def _smooth_staggered_edge(h: int, w: int, *, edge: float, lag: float, phase: int) -> np.ndarray:
    """Soft (tanh) step at row ``edge``, ``lag`` rows later on even sensor columns."""
    rows = np.arange(h, dtype=np.float64)[:, None]
    lags = np.array([lag if (x + phase) % 2 == 0 else 0.0 for x in range(w)])[None, :]
    plane = 28_000 + 20_000 * np.tanh((rows - edge - lags) / 1.5)
    return np.repeat(np.rint(plane).astype(np.uint16)[:, :, None], 3, axis=2)


def _edge_positions(plane: np.ndarray, level: float = 28_000.0) -> np.ndarray:
    """Sub-row crossing of ``level`` in each column (linear between samples)."""
    pos = []
    for x in range(plane.shape[1]):
        c = plane[:, x].astype(np.float64)
        i = int(np.argmax(c > level))
        pos.append(i - 1 + (level - c[i - 1]) / (c[i] - c[i - 1]))
    return np.array(pos)


#: Measured fractional stagger (rows): 8200i SE scans and 8100 V2 / 8300i SE
#: SilverFast captures, the zero crossing of the even/odd offset (2026-11-03).
_FRACTIONAL = {
    (MODEL_8200I_SE.model, 1440): 0.8,
    (MODEL_8200I_SE.model, 2400): 1.25,
    (MODEL_8100_V2.model, 1440): 0.8,
    (MODEL_8100_V2.model, 2400): 1.25,
    (MODEL_8300I_SE.model, 1440): 0.8,
    (MODEL_8300I_SE.model, 2400): 1.15,
}


@pytest.mark.parametrize("model", [MODEL_8200I_SE, MODEL_8100_V2, MODEL_8300I_SE])
@pytest.mark.parametrize("dpi", [1440, 2400])
@pytest.mark.parametrize("pixel_startx", [242, 2825])
def test_fractional_stagger_lines_up_columns(model, dpi, pixel_startx):
    """The measured even/odd offset comes out flat at both phases, on every GL128 leaf.

    The old whole-row (1, 0) left 0.2 rows at 1440 and 0.15-0.25 at 2400: the
    sawtooth on sharp edges.
    """
    from dataclasses import replace

    g = compute_geometry(dpi, model=model, area=None)
    lag = _FRACTIONAL[(model.model, dpi)]
    assert g.stagger_y == (lag, 0)
    g = replace(g, lines=60, pixel_startx=pixel_startx, stagger_phase=pixel_startx)
    raw = _smooth_staggered_edge(64, 16, edge=30.0, lag=lag, phase=pixel_startx)
    out = ImagePipeline(model).apply_y_stagger(raw, g)
    assert out.shape == (60, 16, 3)
    pos = _edge_positions(out[:, :, 1])
    assert abs(float(pos[0::2].mean() - pos[1::2].mean())) < 0.05
    assert float(pos.max() - pos.min()) < 0.1


@pytest.mark.parametrize("dpi", [1440, 2400])
def test_fractional_stagger_softens_both_columns_equally(dpi):
    """The +-d/2 split interpolates both columns alike: no alternating sharpness."""
    from dataclasses import replace

    g = compute_geometry(dpi, model=MODEL_8200I_SE, area=None)
    g = replace(g, lines=500, stagger_phase=0)
    rng = np.random.default_rng(7)
    raw = rng.normal(30_000, 2_000, size=(504, 64, 3)).clip(0, 65535).astype(np.uint16)
    out = ImagePipeline(MODEL_8200I_SE).apply_y_stagger(raw, g).astype(np.float64)
    var_even = float(out[5:-5, 0::2, 1].var())
    var_odd = float(out[5:-5, 1::2, 1].var())
    assert abs(var_even / var_odd - 1.0) < 0.05


def test_integer_stagger_is_plain_row_slicing():
    """(4, 0) at 7200 still slices whole rows: no interpolation, output unchanged."""
    from dataclasses import replace

    g = compute_geometry(7200, model=MODEL_8200I_SE, area=None)
    g = replace(g, lines=40, stagger_phase=0)
    rng = np.random.default_rng(3)
    raw = rng.integers(0, 65535, size=(48, 16, 3), dtype=np.uint16)
    out = ImagePipeline(MODEL_8200I_SE).apply_y_stagger(raw, g)
    expect = np.empty((40, 16, 3), dtype=np.uint16)
    expect[:, 0::2] = raw[4:44, 0::2]
    expect[:, 1::2] = raw[0:40, 1::2]
    assert np.array_equal(out, expect)


@pytest.mark.parametrize("model", [MODEL_8200I_SE, MODEL_8100_V2, MODEL_8300I_SE])
@pytest.mark.parametrize("dpi", [1440, 2400])
def test_fractional_stagger_keeps_lincnt_and_frame(model, dpi):
    """One spare row, as with (1, 0): SilverFast's LINCNT and the full frame are unchanged."""
    g = compute_geometry(dpi, model=model, area=None)
    assert g.num_staggered_lines == 1
    pipe = ImagePipeline(model)
    rgb = np.zeros((g.optical_line_count, 4, 3), dtype=np.uint16)
    rgb = pipe.reduce_y_oversample(rgb, g)
    rgb = pipe.apply_line_shifts(rgb, g)
    rgb = pipe.apply_y_stagger(rgb, g)
    assert rgb.shape[0] >= g.frame_lines


@pytest.mark.parametrize("model", [MODEL_8300I_SE, MODEL_8200I_SE])
def test_host_stagger_at_1440_and_2400_only(model):
    g1440 = compute_geometry(1440, model=model, area=None)
    g1800 = compute_geometry(1800, model=model, area=None)
    g2400 = compute_geometry(2400, model=model, area=None)
    assert g1440.stagger_y == (0.8, 0)
    assert g1440.num_staggered_lines == 1
    assert g2400.stagger_y == ((1.15 if model is MODEL_8300I_SE else 1.25), 0)
    assert g2400.num_staggered_lines == 1
    assert g1800.stagger_y == ()
    assert g1800.num_staggered_lines == 0


@pytest.mark.parametrize("dpi", [1440, 2400])
@pytest.mark.parametrize("model", [MODEL_8300I_SE, MODEL_8200I_SE])
def test_bringup_lincnt_keeps_y_stagger_spare(dpi: int, model):
    """Ladder LINCNT override must not fold unstagger spare into geometry.lines."""
    from pyopticfilm.scan.bringup import bringup_scan_geometry

    geo, _meta = bringup_scan_geometry(model, dpi, profile="preview_safe")
    assert geo.num_staggered_lines == 1
    assert geo.stagger_y == (_FRACTIONAL[(model.model, dpi)], 0)
    y_ov = geo.optical_line_count // geo.lines
    assert y_ov >= 1
    rows_after_reduce = geo.optical_line_count // y_ov
    assert rows_after_reduce >= geo.lines + geo.num_staggered_lines


def test_residual_plus_host_calib_recreates_green_edge():
    """Host calib + residual channel offset → green edge (general interaction)."""
    h, w, edge = 64, 128, 32
    pipe = ImagePipeline(MODEL_8300I_SE)
    misreg = _shift_channels(_step_edge(h, w, edge_row=edge), dg=1, db=1)
    dark, white = _flat_calib(w, white_rgb=(42_000, 28_000, 38_000))
    out = pipe.apply_host_calib(misreg, dark=dark, white=white, expose_base=False)
    dark_n, white_n = _flat_calib(w, white_rgb=(40_000, 40_000, 40_000))
    neutral = pipe.apply_host_calib(misreg, dark=dark_n, white=white_n, expose_base=False)
    assert _edge_chroma(out, edge) > 5_000.0
    assert _edge_green_excess(out, edge) > _edge_green_excess(neutral, edge) + 500.0


def test_host_calib_path_selected_when_asic_shading_rejected():
    """Wiring: host fallback entry ⇒ should_apply_host_calib True (DVDSET off)."""
    w = 64
    dark, white = _flat_calib(w)
    cal = Calibrator(None, model=MODEL_8300I_SE)
    cal._active = CalibEntry(
        method="transparency",
        resolution=1440,
        startx=0,
        pixels=w,
        dark=dark,
        white=white,
        calibrated_at=0.0,
        asic_shading=False,
    )
    cal.prefer_asic_shading = False
    assert cal.should_apply_host_calib() is True

    cal._active = CalibEntry(
        method="transparency",
        resolution=1200,
        startx=0,
        pixels=w,
        dark=np.zeros((w, 3), dtype=np.uint16),
        white=np.full((w, 3), 65535, dtype=np.uint16),
        calibrated_at=0.0,
        asic_shading=True,
        shading_blob=b"\x00" * 16,
        afe_offsets=(0, 0, 0),
        afe_gains=(0, 0, 0),
    )
    cal.prefer_asic_shading = True
    assert cal.should_apply_host_calib() is False


def test_uneven_channel_whites_turn_misreg_into_green_bias():
    """Root interaction: dimmer G home white ⇒ higher G gain ⇒ green edge fringe."""
    h, w, edge = 64, 128, 32
    pipe = ImagePipeline(MODEL_8300I_SE)
    misreg = _shift_channels(_step_edge(h, w, edge_row=edge), dg=1, db=2)
    dark, white = _flat_calib(w, white_rgb=(45_000, 30_000, 40_000))
    out = pipe.apply_host_calib(misreg, dark=dark, white=white, expose_base=False)
    # Neutral stretch of the same misreg is chroma without a strong green bias.
    dark_n, white_n = _flat_calib(w, white_rgb=(40_000, 40_000, 40_000))
    neutral = pipe.apply_host_calib(misreg, dark=dark_n, white=white_n, expose_base=False)
    assert _edge_green_excess(out, edge) > _edge_green_excess(neutral, edge) + 2_000.0
