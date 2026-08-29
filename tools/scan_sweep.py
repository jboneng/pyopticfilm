# SPDX-License-Identifier: GPL-3.0-or-later
"""Sweep GL128 crop scans across resolution / exposure / priming / quiet-drain for analysis.

Built to investigate the jittery 7200 dpi scans and scan-to-scan image
movement that GL128 priming is a rough workaround for. Runs a grid of real
crop scans on real hardware (default a small middle square — aim ``--crop``
at real detail on your mounted negative so pixel-shift correlation has a
signal to lock onto) and saves:

* one labeled PNG per scan in ``--out`` (or an auto-timestamped directory)
* ``sweep_manifest.jsonl`` in the same directory: one JSON record per scan
  with the exact parameters used, raw-16-bit mean/max/clip stats (computed
  *before* the 8-bit PNG conversion, so exposure-clip analysis doesn't lose
  precision), duration, and any error.

Follow up with ``tools/analyze_scan_sweep.py <out-dir>`` to get a pixel-shift
(drift) and exposure-clip report.

IMPORTANT — priming and ``--prime both``: ``Scanner._gl128_primed`` is a
one-shot flag for the life of a ``Scanner`` object. Once anything in a
session primes (explicitly or naturally), passing ``gl128_prime=False`` later
in that *same* session is a no-op. So each priming condition here gets its
own fresh ``Scanner.open()`` ... ``close()`` bracket — comparing "on" vs
"off" is only meaningful across separate sessions, never within one.

``--quiet-drain`` (Adaptive quiet USB drain, ``asic.image_usb_pace_s``) has
no such constraint — it's a plain per-scan attribute, toggled fresh before
every scan regardless of session.

Usage (from repo root)::

    uv run python tools/scan_sweep.py --dry-run
    uv run python tools/scan_sweep.py --resolutions 1200,3600,7200 --repeat 3
    uv run python tools/scan_sweep.py --mode me --me-long-exposures auto,60000,90000
    uv run python tools/scan_sweep.py --resolutions 7200 --prime off --repeat 5 --yes
    uv run python tools/scan_sweep.py --resolutions 1200,3600,7200 --prime both \\
        --quiet-drain both --repeat 8 --order shuffled --seed 1 --max-scans 300 --yes
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: Any channel at/above this (of 65535) counts as "clipped" for clip_fraction.
CLIP_THRESHOLD = 64000


@dataclass(frozen=True)
class ScanSpec:
    prime: bool
    quiet_drain: bool
    feed_slope_slow: bool
    resolution: int
    mode: str  # "single" or "me"
    single_pass_exposure: int | None
    me_short_exposure: int | None
    me_long_exposure: int | None
    me_exposure_mode: str
    repeat_index: int

    @property
    def combo_label(self) -> str:
        if self.mode == "single":
            val = "auto" if self.single_pass_exposure is None else str(self.single_pass_exposure)
            return f"single_{val}"
        short = "auto" if self.me_short_exposure is None else str(self.me_short_exposure)
        long_ = "auto" if self.me_long_exposure is None else str(self.me_long_exposure)
        return f"me_s{short}_l{long_}"

    def filename(self, index: int) -> str:
        prime_label = "on" if self.prime else "off"
        quiet_label = "on" if self.quiet_drain else "off"
        slope_label = "slow" if self.feed_slope_slow else "fast"
        return (
            f"{index:04d}_p-{prime_label}_q-{quiet_label}_f-{slope_label}_"
            f"r{self.resolution}_{self.combo_label}_n{self.repeat_index}.png"
        )


def _parse_int_list_or_auto(raw: str) -> list[int | None]:
    values: list[int | None] = []
    for part in raw.split(","):
        part = part.strip().lower()
        if not part:
            continue
        if part == "auto":
            values.append(None)
            continue
        try:
            values.append(int(part))
        except ValueError:
            raise SystemExit(f"Expected 'auto' or an integer, got {part!r} in {raw!r}") from None
    if not values:
        raise SystemExit(f"Expected at least one value or 'auto', got {raw!r}")
    return values


def _parse_crop(raw: str) -> tuple[float, float, float, float]:
    parts_raw = raw.split(",")
    if len(parts_raw) != 4:
        raise SystemExit(f"--crop needs 4 comma-separated floats x1,y1,x2,y2, got {raw!r}")
    try:
        parts = [float(v.strip()) for v in parts_raw]
    except ValueError:
        raise SystemExit(f"--crop values must be floats, got {raw!r}") from None
    x1, y1, x2, y2 = parts
    return x1, y1, x2, y2


def _build_group_specs(
    *,
    resolutions: list[int],
    mode: str,
    single_exposures: list[int | None],
    me_short_exposures: list[int | None],
    me_long_exposures: list[int | None],
    me_exposure_mode: str,
    repeat: int,
    prime: bool,
    quiet_drain_values: list[bool],
    feed_slope_values: list[bool],
    order: str,
    seed: int,
) -> list[ScanSpec]:
    specs: list[ScanSpec] = []
    modes = ["single", "me"] if mode == "both" else [mode]
    for resolution in resolutions:
        for quiet_drain in quiet_drain_values:
            for feed_slope_slow in feed_slope_values:
                for m in modes:
                    if m == "single":
                        combos: list[tuple[int | None, int | None, int | None]] = [
                            (v, None, None) for v in single_exposures
                        ]
                    else:
                        combos = [
                            (None, s, l) for s in me_short_exposures for l in me_long_exposures
                        ]
                    for single_v, short_v, long_v in combos:
                        for rep in range(1, repeat + 1):
                            specs.append(
                                ScanSpec(
                                    prime=prime,
                                    quiet_drain=quiet_drain,
                                    feed_slope_slow=feed_slope_slow,
                                    resolution=resolution,
                                    mode=m,
                                    single_pass_exposure=single_v,
                                    me_short_exposure=short_v,
                                    me_long_exposure=long_v,
                                    me_exposure_mode=me_exposure_mode,
                                    repeat_index=rep,
                                )
                            )
    if order == "shuffled":
        random.Random(seed).shuffle(specs)
    return specs


def _to_u8(rgb, *, mode: str):
    """16-bit RGB -> 8-bit preview. ``linear`` keeps brightness comparable
    across the whole sweep (needed to *see* clipping); ``auto`` is a 1-99%
    per-image percentile stretch (nicer to eyeball, not brightness-comparable
    across scans) — mirrors tools/scanlab/preview.py::auto_level_u8, kept as
    an inline copy here to avoid a fragile cross-tool import path.
    """
    import numpy as np

    if mode == "linear":
        return (rgb >> 8).astype(np.uint8)
    u8 = np.empty(rgb.shape[:2] + (rgb.shape[2],), dtype=np.uint8)
    probe = rgb[::8, ::8] if min(rgb.shape[:2]) >= 32 else rgb
    for c in range(rgb.shape[2]):
        lo, hi = np.percentile(probe[:, :, c].astype(np.float32), (1.0, 99.0))
        plane = rgb[:, :, c].astype(np.float32)
        if hi <= lo:
            u8[:, :, c] = 0
        else:
            u8[:, :, c] = np.clip((plane - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
    return u8


def _save_png(path: Path, rgb_u8) -> None:
    import cv2

    cv2.imwrite(str(path), cv2.cvtColor(rgb_u8, cv2.COLOR_RGB2BGR))


def _run_one(scanner, spec: ScanSpec, index: int, area, out_dir: Path, preview_mode: str, apply_calib: bool) -> dict:
    t0 = time.monotonic()
    filename = spec.filename(index)
    record: dict = {
        "index": index,
        "timestamp": datetime.now(UTC).isoformat(),
        "filename": filename,
        "prime": spec.prime,
        "quiet_drain": spec.quiet_drain,
        "feed_slope_slow": spec.feed_slope_slow,
        "resolution": spec.resolution,
        "mode": spec.mode,
        "single_pass_exposure": spec.single_pass_exposure,
        "me_short_exposure": spec.me_short_exposure,
        "me_long_exposure": spec.me_long_exposure,
        "me_exposure_mode": spec.me_exposure_mode,
        "repeat_index": spec.repeat_index,
        "crop": list(area),
        "width": None,
        "height": None,
        "mean_rgb": None,
        "max_rgb": None,
        "clip_fraction": None,
        "error": None,
    }
    try:
        from pyopticfilm.scan.session_gl128 import IMAGE_USB_PACE_S

        scanner._asic.image_usb_pace_s = IMAGE_USB_PACE_S if spec.quiet_drain else 0.0
        scanner._asic.experimental_feed_slope_slow = spec.feed_slope_slow
        image = scanner.scan(
            resolution=spec.resolution,
            mode="color",
            area=area,
            apply_calib=apply_calib,
            multi_exposure=(spec.mode == "me"),
            single_pass_exposure=spec.single_pass_exposure,
            me_short_exposure=spec.me_short_exposure,
            me_long_exposure=spec.me_long_exposure,
            me_exposure_mode=spec.me_exposure_mode,
            gl128_prime=spec.prime,
        )
        rgb = image.rgb
        record["width"] = int(rgb.shape[1])
        record["height"] = int(rgb.shape[0])
        record["mean_rgb"] = [round(float(rgb[..., c].mean()), 1) for c in range(3)]
        record["max_rgb"] = [int(rgb[..., c].max()) for c in range(3)]
        record["clip_fraction"] = round(float((rgb >= CLIP_THRESHOLD).any(axis=-1).mean()), 5)
        _save_png(out_dir / filename, _to_u8(rgb, mode=preview_mode))
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["duration_s"] = round(time.monotonic() - t0, 3)
    return record


def _append_manifest(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=None, help="Output directory (default: scan_sweep_<UTC timestamp>/)")
    parser.add_argument("--crop", default="0.45,0.45,0.55,0.55", help="Normalized x1,y1,x2,y2 crop (default a small middle square)")
    parser.add_argument("--resolutions", default="1200,3600,7200", help="Comma list of DPI values (default 1200,3600,7200)")
    parser.add_argument("--mode", choices=["single", "me", "both"], default="single")
    parser.add_argument("--single-exposures", default="auto", help="Comma list of 'auto' or int (single-pass exposure)")
    parser.add_argument("--me-short-exposures", default="auto", help="Comma list of 'auto' or int (ME short exposure)")
    parser.add_argument("--me-long-exposures", default="auto", help="Comma list of 'auto' or int (ME long exposure)")
    parser.add_argument("--me-exposure-mode", choices=["adaptive", "fixed"], default="adaptive", help="Only matters when --me-long-exposures includes 'auto'")
    parser.add_argument("--prime", choices=["on", "off", "both"], default="both")
    parser.add_argument("--quiet-drain", choices=["on", "off", "both"], default="on", help="Adaptive quiet USB drain (asic.image_usb_pace_s) — default 'on' keeps today's default behavior; unlike --prime this needs no fresh session")
    parser.add_argument("--feed-slope", choices=["fast", "slow", "both"], default="fast", help="Positioning-feed motor ramp (asic.experimental_feed_slope_slow) — default 'fast' keeps today's (real vendor) behavior; 'slow' A/B tests SLOPE_TABLE_SLOW for feeds instead, to test whether an aggressive ramp is losing steps")
    parser.add_argument("--repeat", type=int, default=3, help="Repeats per (prime, quiet-drain, feed-slope, resolution, exposure combo)")
    parser.add_argument("--order", choices=["sequential", "shuffled"], default="sequential", help="Shuffling is scoped within one priming condition, never across")
    parser.add_argument("--seed", type=int, default=0, help="RNG seed for --order shuffled")
    parser.add_argument("--no-apply-calib", action="store_true")
    parser.add_argument("--preview", choices=["linear", "auto"], default="linear", help="linear (>>8, brightness-comparable) or auto (per-image percentile stretch, for eyeballing only)")
    parser.add_argument("--max-scans", type=int, default=200, help="Refuse a bigger grid without --force")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print the planned grid and exit; touches no hardware")
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation")
    parser.add_argument("--stop-on-error", action="store_true", help="Abort the whole sweep on the first failed scan instead of logging and continuing")
    args = parser.parse_args(argv)

    crop = _parse_crop(args.crop)
    try:
        resolutions = [int(v.strip()) for v in args.resolutions.split(",") if v.strip()]
    except ValueError:
        raise SystemExit(f"--resolutions must be a comma list of integers, got {args.resolutions!r}") from None
    if not resolutions:
        raise SystemExit("--resolutions needs at least one value")
    single_exposures = _parse_int_list_or_auto(args.single_exposures)
    me_short_exposures = _parse_int_list_or_auto(args.me_short_exposures)
    me_long_exposures = _parse_int_list_or_auto(args.me_long_exposures)

    prime_conditions = {"on": [True], "off": [False], "both": [True, False]}[args.prime]
    quiet_drain_values = {"on": [True], "off": [False], "both": [True, False]}[args.quiet_drain]
    feed_slope_values = {"fast": [False], "slow": [True], "both": [False, True]}[args.feed_slope]
    groups = {
        p: _build_group_specs(
            resolutions=resolutions,
            mode=args.mode,
            single_exposures=single_exposures,
            me_short_exposures=me_short_exposures,
            me_long_exposures=me_long_exposures,
            me_exposure_mode=args.me_exposure_mode,
            repeat=args.repeat,
            prime=p,
            quiet_drain_values=quiet_drain_values,
            feed_slope_values=feed_slope_values,
            order=args.order,
            seed=args.seed,
        )
        for p in prime_conditions
    }
    total = sum(len(v) for v in groups.values())

    print(
        f"Planned sweep: {total} scans across {len(prime_conditions)} priming "
        f"condition(s) x {len(quiet_drain_values)} quiet-drain condition(s) x "
        f"{len(feed_slope_values)} feed-slope condition(s), crop={crop}"
    )
    for p in prime_conditions:
        specs = groups[p]
        print(f"  prime={p}: {len(specs)} scans, e.g. {[s.filename(i) for i, s in enumerate(specs[:3], 1)]}")
    if args.dry_run:
        return 0

    if total > args.max_scans and not args.force:
        raise SystemExit(f"{total} scans exceeds --max-scans {args.max_scans} (use --force to run anyway)")
    if not args.yes:
        reply = input(f"Run {total} scans on real hardware? [y/N] ").strip().lower()
        if reply != "y":
            print("Aborted.")
            return 1

    from pyopticfilm.scanner import Scanner

    out_dir = args.out or Path(f"scan_sweep_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "sweep_manifest.jsonl"
    print(f"Writing to {out_dir}/")

    apply_calib = not args.no_apply_calib
    index = 1
    for prime in prime_conditions:
        specs = groups[prime]
        print(f"\n=== priming condition: {prime} ({len(specs)} scans) ===")
        scanner = Scanner.open()
        try:
            if scanner.model.asic != "GL128":
                raise SystemExit(f"Expected a GL128 model (8100 V2 / 8200i SE), got {scanner.model.model} ({scanner.model.asic})")
            scanner.warmup()
            for spec in specs:
                print(f"[{index}/{total}] {spec.filename(index)} ...", end=" ", flush=True)
                record = _run_one(scanner, spec, index, crop, out_dir, args.preview, apply_calib)
                _append_manifest(manifest_path, record)
                if record["error"]:
                    print(f"FAILED: {record['error']}")
                    if args.stop_on_error:
                        raise SystemExit(1)
                else:
                    print(f"ok ({record['duration_s']}s, clip={record['clip_fraction']:.3f})")
                index += 1
        finally:
            try:
                scanner.lamp_off()
            except Exception:  # noqa: BLE001, S110
                pass
            scanner.close()

    print(f"\nDone. {index - 1} scans attempted. Manifest: {manifest_path}")
    print(f"Next: uv run python tools/analyze_scan_sweep.py {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
