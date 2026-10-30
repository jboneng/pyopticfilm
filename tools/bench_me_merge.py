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

Usage::

    uv run python -m tools.bench_me_merge
    uv run python -m tools.bench_me_merge --ref origin/main --ref prev=HEAD~1
    uv run python -m tools.bench_me_merge --case F --seeds 5
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
import tempfile
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


CASES = (
    Case("A", "noise = old defaults", 1.0, 4096.0),
    Case("B", "low read noise", 0.5, 400.0),
    Case("C", "high gain, mid read noise", 2.0, 900.0),
    Case("D", "B + long G 3% hot", 0.5, 400.0, gain=(1.0, 1.03, 1.0)),
    Case("E", "B + half-px shift", 0.5, 400.0, shift=(0.5, 0.5)),
    Case("F", "mixed realistic", 0.7, 600.0, gain=(1.0, 1.02, 0.98), shift=(0.3, 0.6)),
)


@dataclass(frozen=True)
class Result:
    bands: list[tuple[float, float, float] | None]
    crushed_pct: float
    hue_drift_pct: float
    noise: tuple[tuple[float, ...], tuple[float, ...]] | None


def _scene(dx: float = 0.0, dy: float = 0.0) -> np.ndarray:
    """Truth in short-scale DN, sampled at ``(x + dx, y + dy)``."""
    yy, xx = np.mgrid[0:_H, 0:_W].astype(np.float64)
    base = np.exp(_LOG_LO + (_LOG_HI - _LOG_LO) * (xx + dx) / (_W - 1))
    base = base * (0.9 + 0.2 * (yy + dy) / (_H - 1))
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
    truth = _scene()
    long_truth = _scene(-case.shift[0], -case.shift[1]) * case.r * np.asarray(case.gain)
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
                align_shift=case.shift,
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
        for case in CASES:
            if wanted and case.key not in wanted:
                continue
            _print_case(case, run_case(case, versions, max(1, args.seeds)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
