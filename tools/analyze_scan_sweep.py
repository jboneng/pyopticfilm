# SPDX-License-Identifier: GPL-3.0-or-later
"""Analyze a tools/scan_sweep.py output directory: pixel-shift drift + exposure clip.

Groups sweep_manifest.jsonl records by config (priming, resolution, mode,
exposure), and for every config scanned more than once, phase-correlates
consecutive repeats' PNGs to estimate pixel-shift (dx, dy) between them —
this is the "how much did the image move between scans" measurement. Reports
alongside each config's mean exposure-clip fraction from the manifest (no
precision lost — those stats were computed from the raw 16-bit scan, before
the PNG's 8-bit conversion).

Does not modify anything; pure read-only analysis. Does not reuse
pyopticfilm.pass_align.estimate_pass_shift on purpose: that function silently
returns (0, 0) for shifts above a ~2% guard, which would hide exactly the
kind of large drift (e.g. priming on/off) this tool exists to surface.

Reports dx/dy separately, not just combined magnitude: Y-axis drift
implicates motor/carriage position (what priming addresses); X-axis drift
points somewhere else (e.g. pixel/line timing). ``--verbose`` also prints
each group's raw per-pair shifts, so you can see *where* in the repeat
sequence drift happens — concentrated in repeat 1->2 (the "first scan after
open" case priming is documented to fix) vs spread evenly across all pairs
(a different, ongoing problem).

Usage (from repo root)::

    uv run python tools/analyze_scan_sweep.py scan_sweep_20260829T120000Z/
    uv run python tools/analyze_scan_sweep.py scan_sweep_20260829T120000Z/ --csv report.csv
    uv run python tools/analyze_scan_sweep.py scan_sweep_20260829T120000Z/ --verbose --pairs-csv pairs.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path


def _config_key(record: dict) -> tuple:
    return (
        record["prime"],
        record.get("quiet_drain", True),
        record["resolution"],
        record["mode"],
        record["single_pass_exposure"],
        record["me_short_exposure"],
        record["me_long_exposure"],
        record["me_exposure_mode"],
    )


def _config_label(key: tuple) -> str:
    prime, quiet_drain, resolution, mode, single_exp, short_exp, long_exp, me_mode = key
    prime_label = "on" if prime else "off"
    quiet_label = "on" if quiet_drain else "off"
    if mode == "single":
        exp_label = "auto" if single_exp is None else str(single_exp)
        combo = f"single_{exp_label}"
    else:
        s = "auto" if short_exp is None else str(short_exp)
        l = "auto" if long_exp is None else str(long_exp)
        combo = f"me_s{s}_l{l}({me_mode})"
    return f"p-{prime_label}_q-{quiet_label}_r{resolution}_{combo}"


def _load_manifest(sweep_dir: Path) -> list[dict]:
    manifest_path = sweep_dir / "sweep_manifest.jsonl"
    if not manifest_path.exists():
        raise SystemExit(f"No sweep_manifest.jsonl in {sweep_dir}")
    records = []
    with manifest_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _phase_correlate_shift(a, b) -> tuple[float, float]:
    """(dx, dy) via Hanning-windowed FFT phase correlation. No shift guard —
    this tool exists specifically to see large drift, not suppress it."""
    import cv2
    import numpy as np

    a = a.astype(np.float32)
    b = b.astype(np.float32)
    h, w = a.shape[:2]
    win = cv2.createHanningWindow((w, h), cv2.CV_32F)
    (dx, dy), _response = cv2.phaseCorrelate(a, b, win)
    return float(dx), float(dy)


@dataclass
class PairShift:
    repeat_a: int
    repeat_b: int
    dx: float
    dy: float
    magnitude: float


@dataclass
class GroupResult:
    label: str
    n_images: int
    n_errors: int
    n_pairs: int
    pairs: list[PairShift]
    mean_dx: float | None
    mean_dy: float | None
    max_abs_dx: float | None
    max_abs_dy: float | None
    mean_shift: float | None
    max_shift: float | None
    mean_clip_fraction: float | None


def _analyze_group(sweep_dir: Path, key: tuple, records: list[dict]) -> GroupResult:
    import cv2

    label = _config_label(key)
    errors = [r for r in records if r.get("error")]
    ok_records = sorted((r for r in records if not r.get("error")), key=lambda r: r["repeat_index"])
    clip_fractions = [r["clip_fraction"] for r in ok_records if r.get("clip_fraction") is not None]
    mean_clip = sum(clip_fractions) / len(clip_fractions) if clip_fractions else None

    loaded = []
    for r in ok_records:
        img = cv2.imread(str(sweep_dir / r["filename"]), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            loaded.append((r["repeat_index"], img))

    pairs: list[PairShift] = []
    for (rep_a, a), (rep_b, b) in pairwise(loaded):
        if a.shape != b.shape:
            continue
        dx, dy = _phase_correlate_shift(a, b)
        pairs.append(PairShift(repeat_a=rep_a, repeat_b=rep_b, dx=dx, dy=dy, magnitude=(dx * dx + dy * dy) ** 0.5))

    magnitudes = [p.magnitude for p in pairs]
    dxs = [p.dx for p in pairs]
    dys = [p.dy for p in pairs]

    return GroupResult(
        label=label,
        n_images=len(ok_records),
        n_errors=len(errors),
        n_pairs=len(pairs),
        pairs=pairs,
        mean_dx=(sum(dxs) / len(dxs)) if dxs else None,
        mean_dy=(sum(dys) / len(dys)) if dys else None,
        max_abs_dx=max((abs(v) for v in dxs), default=None),
        max_abs_dy=max((abs(v) for v in dys), default=None),
        mean_shift=(sum(magnitudes) / len(magnitudes)) if magnitudes else None,
        max_shift=max(magnitudes) if magnitudes else None,
        mean_clip_fraction=mean_clip,
    )


def _fmt(v: float | None, digits: int = 2) -> str:
    return f"{v:.{digits}f}" if v is not None else "n/a"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sweep_dir", type=Path)
    parser.add_argument("--csv", type=Path, default=None, help="Also write the per-config summary table to this CSV path")
    parser.add_argument("--pairs-csv", type=Path, default=None, help="Also write one row per individual repeat-pair (dx, dy, magnitude) to this CSV path")
    parser.add_argument("--verbose", action="store_true", help="Also print each config's raw per-pair shifts")
    args = parser.parse_args(argv)

    records = _load_manifest(args.sweep_dir)
    if not records:
        print("Manifest is empty.")
        return 0

    groups: dict[tuple, list[dict]] = {}
    for r in records:
        groups.setdefault(_config_key(r), []).append(r)

    results = [_analyze_group(args.sweep_dir, key, recs) for key, recs in groups.items()]
    results.sort(key=lambda g: (g.max_shift is None, -(g.max_shift or 0.0)))

    header = (
        f"{'config':50} {'n':>3} {'err':>3} {'pairs':>5} "
        f"{'mean_dx':>8} {'mean_dy':>8} {'mean_shift':>10} {'max_shift':>10} {'mean_clip':>10}"
    )
    print(header)
    print("-" * len(header))
    for g in results:
        print(
            f"{g.label:50} {g.n_images:>3} {g.n_errors:>3} {g.n_pairs:>5} "
            f"{_fmt(g.mean_dx):>8} {_fmt(g.mean_dy):>8} {_fmt(g.mean_shift):>10} "
            f"{_fmt(g.max_shift):>10} {_fmt(g.mean_clip_fraction, 4):>10}"
        )
        if args.verbose:
            for p in g.pairs:
                print(f"    repeat {p.repeat_a}->{p.repeat_b}: dx={p.dx:.2f} dy={p.dy:.2f} |shift|={p.magnitude:.2f}")

    if args.csv is not None:
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "config", "n_images", "n_errors", "n_pairs",
                "mean_dx", "mean_dy", "max_abs_dx", "max_abs_dy",
                "mean_shift_px", "max_shift_px", "mean_clip_fraction",
            ])
            for g in results:
                writer.writerow([
                    g.label, g.n_images, g.n_errors, g.n_pairs,
                    g.mean_dx, g.mean_dy, g.max_abs_dx, g.max_abs_dy,
                    g.mean_shift, g.max_shift, g.mean_clip_fraction,
                ])
        print(f"\nWrote {args.csv}")

    if args.pairs_csv is not None:
        with args.pairs_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["config", "repeat_a", "repeat_b", "dx", "dy", "magnitude"])
            for g in results:
                for p in g.pairs:
                    writer.writerow([g.label, p.repeat_a, p.repeat_b, p.dx, p.dy, p.magnitude])
        print(f"Wrote {args.pairs_csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
