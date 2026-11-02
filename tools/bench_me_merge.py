# SPDX-License-Identifier: GPL-3.0-or-later
"""Benchmark the multi-exposure merge against synthetic ground truth.

Scene: smooth field, log-uniform level 10..60000 DN (short scale) across the
width, three channels at different densities (orange-mask-like), so dense
film, the long's clip and one-channel clipping all occur. Short and long are
drawn with Poisson-Gaussian noise; cases add a per-channel long gain error and
a sub-pixel misregistration (generated analytically, undone by the merge's own
alignment).

Per level band it reports:

* ``eff`` — best-possible variance / measured MSE. The reference is IVW of
  both passes with the true noise model, the long counted only below 80% FS,
  so the 15k-21k handover band can read above 1.0; compare ``dB`` there.
* ``dB`` — noise reduction over the short pass alone.
* ``bias`` — mean relative error.

plus black-crushed pixels and hue drift across tone (G/R, B/R range).

``--real DIR`` runs the merge on a real ``color_short*.tif`` / ``color_long*.tif``
pair (needs tifffile), where there is no ground truth:

* ``flat`` — high-pass noise per channel in the quietest tiles (grain included).
* ``detail`` — fine detail kept, as a fraction of the short pass. Covariance of
  the channel's high-pass with the short's high-pass in *another* channel:
  sensor noise is independent between channels and film detail is not, so
  noise cannot score as detail and a blurred long pass loses score.
* ``dense`` — merged/short per short-level band in blue, the densest channel
  under the orange mask; 1.0 means no shift of colour with tone.
* ``short-only`` — pixels written as the exact short reading, split into ``clipped``
  (the long is past the merge's cutoff there) and ``rejected`` (the long was usable
  and a guard dropped it).

``--real`` also takes a folder of scans (``<film>/<run>/color_short*.tif``), scoring
each pair. Same-size runs in one folder are repeat scans of a frame: their difference
holds scanner noise but no grain, so ``repeat noise`` is what the merge really
removes. ``spread`` is how far the fits and dense blue move between repeats.

``--timing-mp N`` also times the merge on that pair tiled up to N megapixels.

Usage::

    uv run python -m tools.bench_me_merge
    uv run python -m tools.bench_me_merge --ref origin/main --ref prev=HEAD~1
    uv run python -m tools.bench_me_merge --case F --seeds 5
    uv run python -m tools.bench_me_merge --ref base=main --real D:/scans/pair --timing-mp 130
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import itertools
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import numpy as np

_FULL_SCALE = 65535.0
_MERGE_PATH = "src/pyopticfilm/scan/exposure_merge.py"

_H, _W = 384, 1024
_CH_SCALE = np.array([1.0, 0.7, 0.45])
_LOG_LO, _LOG_HI = np.log(10.0), np.log(60000.0)
# The alignment warp clamps at the frame edge.
_CROP = (slice(3, -3), slice(3, -3))

# ``--real``: tile side for the quiet / busy masks, short-level bands for blue.
_REAL_TILE = 32
_REAL_DENSE_BANDS = ((300, 800), (800, 1200), (1200, 1800), (1800, 3000), (3000, 5000), (5000, 7500))
# Long readings past this are the merge's own clip region, not a guard's doing.
_REAL_LONG_CUTOFF = 0.80 * _FULL_SCALE

_BANDS = (
    ("10-44 (long<floor)", 10, 44),
    ("44-150", 44, 150),
    ("150-1k", 150, 1000),
    ("1k-5k", 1000, 5000),
    ("5k-15k", 5000, 15000),
    ("15k-21k (handover)", 15000, 21000),
    ("21k+ (long clipped)", 21000, 60001),
)


@dataclass(frozen=True)
class Case:
    key: str
    name: str
    alpha: float
    beta: float
    r: float = 3.0
    gain: tuple[float, float, float] = (1.0, 1.0, 1.0)
    shift: tuple[float, float] = (0.0, 0.0)
    #: Long-pass black offset per channel, in long DN.
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    #: Long-pass vertical drift in px per 1000 rows, about the frame centre.
    drift: float = 0.0


CASES = (
    Case("A", "noise = old defaults", 1.0, 4096.0),
    Case("B", "low read noise", 0.5, 400.0),
    Case("C", "high gain, mid read noise", 2.0, 900.0),
    Case("D", "B + long G 3% hot", 0.5, 400.0, gain=(1.0, 1.03, 1.0)),
    Case("E", "B + half-px shift", 0.5, 400.0, shift=(0.5, 0.5)),
    Case("F", "mixed realistic", 0.7, 600.0, gain=(1.0, 1.02, 0.98), shift=(0.3, 0.6)),
    Case("G", "B + long black offset", 0.5, 400.0, offset=(-880.0, 0.0, -1230.0)),
    # The frame is short, so the drift is steep: about 5 px end to end.
    Case("H", "B + vertical drift", 0.5, 400.0, drift=-13.0),
)


@dataclass(frozen=True)
class Result:
    bands: list[tuple[float, float, float] | None]
    crushed_pct: float
    hue_drift_pct: float
    noise: tuple[tuple[float, ...], tuple[float, ...]] | None


def _scene(dx: float = 0.0, dy: float | np.ndarray = 0.0, *, texture: bool = False) -> np.ndarray:
    """Truth in short-scale DN, sampled at ``(x + dx, y + dy)``; ``dy`` may vary by row."""
    yy, xx = np.mgrid[0:_H, 0:_W].astype(np.float64)
    if isinstance(dy, np.ndarray):
        dy = dy[:, None]
    xs, ys = xx + dx, yy + dy
    base = np.exp(_LOG_LO + (_LOG_HI - _LOG_LO) * xs / (_W - 1))
    base = base * (0.9 + 0.2 * ys / (_H - 1))
    if texture:
        # Fine rows of detail, so vertical misregistration has something to smear.
        base = base * (1.0 + 0.15 * np.sin(2.0 * np.pi * ys / 7.0) + 0.05 * np.sin(2.0 * np.pi * xs / 11.0))
    return base[..., None] * _CH_SCALE


def _capture(mean: np.ndarray, alpha: float, beta: float, rng: np.random.Generator) -> np.ndarray:
    std = np.sqrt(alpha * np.maximum(mean, 0.0) + beta)
    noisy = np.rint(mean + rng.normal(size=mean.shape) * std)
    return np.clip(noisy, 0, _FULL_SCALE).astype(np.uint16)


def _shift_var_scale(shift: tuple[float, float]) -> float:
    scale = 1.0
    for s in shift:
        t = s - np.floor(s)
        scale *= (1.0 - t) ** 2 + t**2
    return scale


def _oracle_var(truth: np.ndarray, case: Case) -> np.ndarray:
    va = case.alpha * truth + case.beta
    long_mean = truth * case.r * np.asarray(case.gain)
    vb = (case.alpha * long_mean + case.beta) / case.r**2 * _shift_var_scale(case.shift)
    usable = long_mean < 0.80 * _FULL_SCALE
    return np.where(usable, 1.0 / (1.0 / va + 1.0 / vb), va)


def _drift_per_row(case: Case) -> np.ndarray:
    return case.drift / 1000.0 * (np.arange(_H, dtype=np.float64) - (_H - 1) / 2.0)


def _merge_kwargs(mod: ModuleType, shift: tuple[float, float], transform: object | None) -> dict:
    """Alignment arguments for ``mod``; older revisions take a plain shift only."""
    params = inspect.signature(mod.merge_exposures_result).parameters
    if transform is not None and "align_transform" in params:
        return {"align_transform": transform}
    return {"align_shift": shift}


def _load_merge(label: str, source: Path) -> ModuleType:
    name = f"_bench_merge_{len(sys.modules)}"
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {label} from {source}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve their module by name
    spec.loader.exec_module(mod)
    return mod


def _merge_at_ref(ref: str, tmp: Path) -> Path:
    src = subprocess.run(
        ["git", "show", f"{ref}:{_MERGE_PATH}"],
        check=True,
        capture_output=True,
    ).stdout
    path = tmp / f"merge_{len(list(tmp.iterdir()))}.py"
    path.write_bytes(src)
    return path


def run_case(case: Case, versions: dict[str, ModuleType], seeds: int) -> dict[str, Result]:
    texture = case.drift != 0.0
    truth = _scene(texture=texture)
    drift = _drift_per_row(case)
    # The merge remaps dst(x) = long(x - s), so the long is the scene at x + s.
    long_dy = case.shift[1] + drift if texture else case.shift[1]
    long_truth = _scene(case.shift[0], long_dy, texture=texture) * case.r * np.asarray(case.gain)
    long_truth = long_truth + np.asarray(case.offset)
    transform = None
    if texture:
        from pyopticfilm.registration.types import LineWarpTransform

        transform = LineWarpTransform(dx=case.shift[0], dy_per_row=case.shift[1] + drift)
    errs: dict[str, list[np.ndarray]] = {v: [] for v in versions}
    outs: dict[str, list[np.ndarray]] = {v: [] for v in versions}
    noise: dict[str, tuple[tuple[float, ...], tuple[float, ...]] | None] = {}
    short_err: list[np.ndarray] = []
    for seed in range(seeds):
        rng = np.random.default_rng(seed)
        short = _capture(truth, case.alpha, case.beta, rng)
        long = _capture(long_truth, case.alpha, case.beta, rng)
        short_err.append((short.astype(np.float64) - truth)[_CROP])
        for label, mod in versions.items():
            res = mod.merge_exposures_result(
                short,
                long,
                exposure_short=1000,
                exposure_long=int(1000 * case.r),
                **_merge_kwargs(mod, case.shift, transform),
            )
            out = res.rgb.astype(np.float64)[_CROP]
            errs[label].append(out - truth[_CROP])
            outs[label].append(out)
            if seed == 0:
                stats = res.fusion_stats
                fitted = getattr(stats, "noise_alpha", None)
                noise[label] = None if fitted is None else (stats.noise_alpha, stats.noise_beta)

    t = truth[_CROP]
    oracle = _oracle_var(truth, case)[_CROP]
    short_mse = np.mean(np.stack(short_err) ** 2, axis=0)
    results: dict[str, Result] = {}
    for label in versions:
        e = np.stack(errs[label])
        o = np.stack(outs[label])
        mse = (e**2).mean(axis=0)
        bias = e.mean(axis=0)
        bands: list[tuple[float, float, float] | None] = []
        for _, lo, hi in _BANDS:
            m = (t >= lo) & (t < hi)
            if not m.any():
                bands.append(None)
                continue
            bands.append(
                (
                    float(oracle[m].mean() / mse[m].mean()),
                    float(10.0 * np.log10(short_mse[m].mean() / mse[m].mean())),
                    float((bias[m] / t[m]).mean() * 100.0),
                )
            )
        crushed = float(((o == 0) & (t >= 10)).mean() * 100.0)
        col_gain = o.mean(axis=(0, 1)) / t.mean(axis=0)
        ok = t.mean(axis=0).min(axis=1) > 300
        gr = col_gain[ok, 1] / col_gain[ok, 0]
        br = col_gain[ok, 2] / col_gain[ok, 0]
        hue = float(max(np.ptp(gr), np.ptp(br)) * 100.0)
        results[label] = Result(bands, crushed, hue, noise.get(label))
    return results


def _print_case(case: Case, results: dict[str, Result]) -> None:
    print(
        f"\n=== {case.key}: {case.name}  (alpha={case.alpha} beta={case.beta} r={case.r} "
        f"gain={case.gain} shift={case.shift})"
    )
    for label, res in results.items():
        if res.noise is not None:
            a = ", ".join(f"{v:.3f}" for v in res.noise[0])
            b = ", ".join(f"{v:.0f}" for v in res.noise[1])
            print(f"    {label}: fitted alpha=({a}) beta=({b})")
    col = 26
    print(f"    {'band':<21}" + "".join(f"{label[: col - 2]:>{col}}" for label in results))
    print(f"    {'':<21}" + "".join(f"{'eff     dB    bias':>{col}}" for _ in results))
    for i, (band, _, _) in enumerate(_BANDS):
        line = f"    {band:<21}"
        for res in results.values():
            s = res.bands[i]
            cell = "-" if s is None else f"{s[0]:.2f} {s[1]:+6.1f} {s[2]:+7.2f}%"
            line += f"{cell:>{col}}"
        print(line)
    print(f"    {'black-crushed px %':<21}" + "".join(f"{r.crushed_pct:>{col}.3f}" for r in results.values()))
    print(f"    {'hue drift vs tone %':<21}" + "".join(f"{r.hue_drift_pct:>{col}.3f}" for r in results.values()))


def _read_pair(folder: Path) -> tuple[np.ndarray, np.ndarray]:
    import tifffile

    short = sorted(folder.glob("color_short*.tif"))
    long = sorted(folder.glob("color_long*.tif"))
    if not short or not long:
        raise SystemExit(f"no color_short*.tif / color_long*.tif pair in {folder}")
    return tifffile.imread(short[0]), tifffile.imread(long[0])


def _estimate_alignment(short: np.ndarray, long: np.ndarray) -> tuple[tuple[float, float], object | None]:
    """Alignment from the working tree: mean shift plus the transform when there is one."""
    from pyopticfilm import pass_align

    if hasattr(pass_align, "estimate_me_transform"):
        transform = pass_align.estimate_me_transform(short, long)
        return pass_align.transform_mean_shift(transform), transform
    return pass_align.estimate_pass_shift(short, long), None


def _highpass(a: np.ndarray) -> np.ndarray:
    x = a.astype(np.float32)
    return x[1:-1, 1:-1] - 0.25 * (x[:-2, 1:-1] + x[2:, 1:-1] + x[1:-1, :-2] + x[1:-1, 2:])


def _tile_masks(short: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Quietest and busiest tiles of the short's green, on the high-passed grid."""
    tile = _REAL_TILE
    g = short[1:-1, 1:-1, 1].astype(np.float32)
    rows, cols = g.shape[0] // tile, g.shape[1] // tile
    spread = g[: rows * tile, : cols * tile].reshape(rows, tile, cols, tile).std(axis=(1, 3))
    flat = np.zeros(g.shape, dtype=bool)
    busy = np.zeros(g.shape, dtype=bool)
    for mask, keep in ((flat, spread <= np.quantile(spread, 0.05)), (busy, spread >= np.quantile(spread, 0.85))):
        mask[: rows * tile, : cols * tile] = np.repeat(np.repeat(keep, tile, axis=0), tile, axis=1)
    return flat, busy


def _detail(out: np.ndarray, short: np.ndarray, busy: np.ndarray, c: int, partner: int) -> float:
    ref = _highpass(short[..., partner])[busy]
    ref = ref - ref.mean()
    mine = _highpass(out[..., c])[busy]
    theirs = _highpass(short[..., c])[busy]
    return float(np.dot(mine - mine.mean(), ref) / np.dot(theirs - theirs.mean(), ref))


def _find_pairs(root: Path) -> list[Path]:
    if any(root.glob("color_short*.tif")):
        return [root]
    return sorted({p.parent for p in root.rglob("color_short*.tif")})


@dataclass
class _Scored:
    """One version's merge of one pair, kept for the repeat comparison."""

    short: np.ndarray
    out: np.ndarray
    dense: list[float]
    stats: object


def _score_pair(
    short: np.ndarray, long: np.ndarray, folder: Path, versions: dict[str, ModuleType], repeats: int
) -> dict[str, _Scored]:
    from pyopticfilm import pass_align

    t0 = time.perf_counter()
    shift, transform = _estimate_alignment(short, long)
    align_s = time.perf_counter() - t0
    flat, busy = _tile_masks(short)
    level = short[..., 2].astype(np.float32)
    h, w = short.shape[:2]
    print(
        f"\n=== {folder}  {w}x{h}  {type(transform).__name__}  "
        f"mean shift=({shift[0]:.3f}, {shift[1]:.3f})  align {align_s:.2f}s"
    )
    aligned = long
    if transform is not None:
        aligned = pass_align.apply_me_transform(long, transform, clip_level=0.95 * _FULL_SCALE)[0]
    long_clipped = aligned[..., 1] >= _REAL_LONG_CUTOFF
    scored: dict[str, _Scored] = {}
    for label, mod in versions.items():
        kwargs = _merge_kwargs(mod, shift, transform)
        times = []
        for _ in range(repeats):
            t0 = time.perf_counter()
            res = mod.merge_exposures_result(short, long, **kwargs)
            times.append(time.perf_counter() - t0)
        out = res.rgb
        noise = [float(_highpass(out[..., c])[flat].std()) for c in range(3)]
        detail = [_detail(out, short, busy, 1, 0), _detail(out, short, busy, 2, 0), _detail(out, short, busy, 0, 1)]
        dense = []
        for lo, hi in _REAL_DENSE_BANDS:
            m = (level >= lo) & (level < hi)
            dense.append(float(np.median(out[..., 2][m].astype(np.float32)) / np.median(level[m])) if m.any() else 0.0)
        same = out[..., 1] == short[..., 1]
        stats = res.fusion_stats
        offsets = getattr(stats, "exposure_offset_per_channel", None)
        print(f"  {label}:")
        print("    merge s      " + " ".join(f"{t:.2f}" for t in times))
        print("    flat RGB     " + " ".join(f"{v:.0f}" for v in noise))
        print("    detail G/B/R " + " ".join(f"{v:.3f}" for v in detail))
        print(
            "    dense blue   "
            + " ".join(f"{lo}-{hi}:{v:.3f}" for (lo, hi), v in zip(_REAL_DENSE_BANDS, dense, strict=True))
        )
        print(
            f"    short-only   {same.mean() * 100.0:.1f}% of G pixels = "
            f"{(same & long_clipped).mean() * 100.0:.1f}% clipped + {(same & ~long_clipped).mean() * 100.0:.1f}% rejected"
        )
        print(
            "    gain "
            + " ".join(f"{v:.3f}" for v in stats.exposure_ratio_per_channel)
            + ("" if offsets is None else "  offset " + " ".join(f"{v:.0f}" for v in offsets))
            + "  alpha "
            + " ".join(f"{v:.2f}" for v in stats.noise_alpha)
            + "  beta "
            + " ".join(f"{v:.0f}" for v in stats.noise_beta)
        )
        scored[label] = _Scored(short, out, dense, stats)
    return scored


def _repeat_noise(first: _Scored, second: _Scored) -> tuple[list[float], list[float]]:
    """Scanner noise of the short and of the merge, RGB, from two scans of one frame.

    Grain is in both scans, so it cancels and only noise that differs is left. The
    second scan is resampled onto the first, which scales its noise; dividing by
    the combined factor gives the noise of one scan.
    """
    from pyopticfilm import pass_align

    transform = pass_align.estimate_me_transform(first.short, second.short)
    aligned_short, var_scale = pass_align.apply_me_transform(second.short, transform)
    aligned_out, _ = pass_align.apply_me_transform(second.out, transform)
    norm = float(np.sqrt(1.0 + np.mean(var_scale)))
    edge = _REAL_TILE

    def one(a: np.ndarray, b: np.ndarray) -> list[float]:
        return [float(_highpass(a[..., c].astype(np.float32) - b[..., c])[edge:-edge, edge:-edge].std()) / norm for c in range(3)]

    return one(first.short, aligned_short), one(first.out, aligned_out)


def _print_repeats(folder: Path, runs: list[dict[str, _Scored]]) -> None:
    print(f"\n=== repeats {folder}: {len(runs)} runs")
    for label in runs[0]:
        short_noise, merged_noise = _repeat_noise(runs[0][label], runs[1][label])
        print(f"  {label}:")
        print("    repeat noise short  " + " ".join(f"{v:.1f}" for v in short_noise))
        print("    repeat noise merged " + " ".join(f"{v:.1f}" for v in merged_noise))
        dense = np.array([run[label].dense for run in runs])
        print(
            "    spread dense blue   "
            + " ".join(f"{lo}-{hi}:{v:.3f}" for (lo, hi), v in zip(_REAL_DENSE_BANDS, np.ptp(dense, axis=0), strict=True))
        )
        for name in ("exposure_ratio_per_channel", "noise_alpha", "noise_beta"):
            values = np.array([getattr(run[label].stats, name) for run in runs])
            print(f"    spread {name:<27}" + " ".join(f"{v:.3f}" for v in np.ptp(values, axis=0)))


def run_real(root: Path, versions: dict[str, ModuleType], timing_mp: float) -> None:
    pairs = _find_pairs(root)
    if not pairs:
        raise SystemExit(f"no color_short*.tif under {root}")
    # One merge each on a set: three would only refine the timing.
    repeats = 3 if len(pairs) == 1 else 1
    short, long = None, None
    for parent, folders in itertools.groupby(pairs, key=lambda p: p.parent):
        runs: list[dict[str, _Scored]] = []
        shapes: list[tuple[int, ...]] = []
        for folder in folders:
            short, long = _read_pair(folder)
            runs.append(_score_pair(short, long, folder, versions, repeats))
            shapes.append(short.shape)
        same_size = [run for run, shape in zip(runs, shapes, strict=True) if shape == shapes[0]]
        if len(same_size) >= 2:
            _print_repeats(parent, same_size)
    if timing_mp > 0 and short is not None and long is not None:
        h, w = short.shape[:2]
        n = int(np.ceil(np.sqrt(timing_mp * 1e6 / (h * w))))
        big_s = np.tile(short, (n, n, 1))
        big_l = np.tile(long, (n, n, 1))
        print(f"  timing on {big_s.shape[1]}x{big_s.shape[0]} ({big_s.shape[0] * big_s.shape[1] / 1e6:.0f} MP):")
        for label, mod in versions.items():
            t0 = time.perf_counter()
            mod.merge_exposures_result(big_s, big_l, align_shift=(0.0, 0.0))
            print(f"    {label}: {time.perf_counter() - t0:.1f}s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--ref",
        action="append",
        default=[],
        metavar="[LABEL=]REV",
        help="Also benchmark exposure_merge.py at git revision REV (repeatable)",
    )
    parser.add_argument("--case", action="append", default=[], help="Run only these case keys (A-F)")
    parser.add_argument("--seeds", type=int, default=3, help="Noise realisations per case (default 3)")
    parser.add_argument("--real", type=Path, help="Real color_short*/color_long* TIFF pair, or a folder of them")
    parser.add_argument("--timing-mp", type=float, default=0.0, help="With --real, also time a frame tiled to N MP")
    args = parser.parse_args(argv)

    np.seterr(all="ignore")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        versions: dict[str, ModuleType] = {}
        for spec in args.ref:
            label, _, rev = spec.rpartition("=")
            label = label or rev
            versions[label] = _load_merge(label, _merge_at_ref(rev, tmp))
        from pyopticfilm.scan import exposure_merge

        versions["working tree"] = exposure_merge
        wanted = {k.upper() for k in args.case}
        if args.real is not None:
            run_real(args.real, versions, args.timing_mp)
            if not wanted:
                return 0
        for case in CASES:
            if wanted and case.key not in wanted:
                continue
            _print_case(case, run_case(case, versions, max(1, args.seeds)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
