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

Usage (from repo root)::

    uv run python tools/analyze_scan_sweep.py scan_sweep_20260829T120000Z/
    uv run python tools/analyze_scan_sweep.py scan_sweep_20260829T120000Z/ --csv report.csv
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
        record["resolution"],
        record["mode"],
        record["single_pass_exposure"],
        record["me_short_exposure"],
        record["me_long_exposure"],
        record["me_exposure_mode"],
    )


def _config_label(key: tuple) -> str:
    prime, resolution, mode, single_exp, short_exp, long_exp, me_mode = key
    prime_label = "on" if prime else "off"
    if mode == "single":
        exp_label = "auto" if single_exp is None else str(single_exp)
        combo = f"single_{exp_label}"
    else:
        s = "auto" if short_exp is None else str(short_exp)
        l = "auto" if long_exp is None else str(long_exp)
        combo = f"me_s{s}_l{l}({me_mode})"
    return f"p-{prime_label}_r{resolution}_{combo}"


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
class GroupResult:
    label: str
    n_images: int
    n_errors: int
    n_pairs: int
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

    images = []
    for r in ok_records:
        img = cv2.imread(str(sweep_dir / r["filename"]), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            images.append(img)

    shifts: list[float] = []
    for a, b in pairwise(images):
        if a.shape != b.shape:
            continue
        dx, dy = _phase_correlate_shift(a, b)
        shifts.append((dx * dx + dy * dy) ** 0.5)

    return GroupResult(
        label=label,
        n_images=len(ok_records),
        n_errors=len(errors),
        n_pairs=len(shifts),
        mean_shift=(sum(shifts) / len(shifts)) if shifts else None,
        max_shift=max(shifts) if shifts else None,
        mean_clip_fraction=mean_clip,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sweep_dir", type=Path)
    parser.add_argument("--csv", type=Path, default=None, help="Also write the table to this CSV path")
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

    header = f"{'config':45} {'n':>3} {'err':>3} {'pairs':>5} {'mean_shift_px':>13} {'max_shift_px':>12} {'mean_clip':>10}"
    print(header)
    print("-" * len(header))
    for g in results:
        mean_shift = f"{g.mean_shift:.2f}" if g.mean_shift is not None else "n/a"
        max_shift = f"{g.max_shift:.2f}" if g.max_shift is not None else "n/a"
        mean_clip = f"{g.mean_clip_fraction:.4f}" if g.mean_clip_fraction is not None else "n/a"
        print(f"{g.label:45} {g.n_images:>3} {g.n_errors:>3} {g.n_pairs:>5} {mean_shift:>13} {max_shift:>12} {mean_clip:>10}")

    if args.csv is not None:
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["config", "n_images", "n_errors", "n_pairs", "mean_shift_px", "max_shift_px", "mean_clip_fraction"])
            for g in results:
                writer.writerow([g.label, g.n_images, g.n_errors, g.n_pairs, g.mean_shift, g.max_shift, g.mean_clip_fraction])
        print(f"\nWrote {args.csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
