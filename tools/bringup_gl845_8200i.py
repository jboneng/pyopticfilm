# SPDX-License-Identifier: GPL-3.0-or-later
"""Lab bring-up for GL845 OpticFilm 8200i / 7600i v2 — temporary unlock, does not flip scan_ready.

Run only with the scanner on the bench and a hand near the power switch for
home/motor steps. Public ``scan_ready`` stays False until this sequence
completes with a real image + park (see docs/scanner-validation.md).

Usage (from repo root)::

    uv run python tools/bringup_gl845_8200i.py --dry-run
    uv run python tools/bringup_gl845_8200i.py --steps open,status,lamp
    uv run python tools/bringup_gl845_8200i.py --model 7600i-v2 --steps open,status
    uv run python tools/bringup_gl845_8200i.py --model 7600i-v2 \\
        --allow-unvalidated --steps init,home,tiny,park

``--allow-unvalidated`` is required for ASIC init, motor, and image steps (lab unlock only).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Repo tools/ is not a package; keep imports local to pyopticfilm.


STEPS = (
    "open",
    "status",
    "init",
    "lamp",
    "home",
    "tiny",
    "park",
    "calib",
    "ir",
    "combo",
    "me",
)

TARGET_MODELS = {
    "8200i": ("plustek-opticfilm-8200i", "OpticFilm 8200i"),
    "7600i-v2": ("plustek-opticfilm-7600i-v2", "OpticFilm 7600i (v2)"),
}


def _parse_steps(raw: str) -> list[str]:
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    unknown = [p for p in parts if p not in STEPS]
    if unknown:
        raise SystemExit(f"Unknown steps {unknown}; choose from {', '.join(STEPS)}")
    return parts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        choices=tuple(TARGET_MODELS),
        default="8200i",
        help="GL845 model to target (default: 8200i)",
    )
    parser.add_argument(
        "--device-id",
        help="Exact USB device id from pyopticfilm enumeration (recommended if multiple scanners are attached)",
    )
    parser.add_argument(
        "--steps",
        default="open,status,lamp",
        help=f"Comma list from: {', '.join(STEPS)}",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=900,
        help="Tiny-scan / calib DPI (default 900)",
    )
    parser.add_argument(
        "--area",
        type=_parse_area,
        default=(0.0, 0.0, 0.3, 0.2),
        help="Normalised x0,y0,x1,y1 for calib/combo/me (default 0,0,0.3,0.2; 0,0,1,1 = full frame)",
    )
    parser.add_argument(
        "--me-long",
        type=int,
        default=None,
        help="ME long LPERIOD override (clamped to the model envelope); default adaptive",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Directory for NumPy data and PPM/PGM previews",
    )
    parser.add_argument(
        "--allow-unvalidated",
        action="store_true",
        help="Lab unlock for home/scan/park (required for motor steps)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned sequence without opening USB",
    )
    args = parser.parse_args(argv)
    steps = _parse_steps(args.steps)
    hardware_steps = {"init", "home", "tiny", "park", "calib", "ir", "combo", "me"}
    if hardware_steps.intersection(steps) and not args.allow_unvalidated and not args.dry_run:
        raise SystemExit(
            "ASIC init, motor, and image steps need --allow-unvalidated (lab unlock). "
            "scan_ready is not flipped by this tool."
        )

    expected_name, model_label = TARGET_MODELS[args.model]
    print(f"Bring-up plan (GL845 {model_label}):")
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step}")
    print(
        "After a successful image + park on real hardware, update only this "
        "model's scan_ready flag and add its model-lock tests."
    )
    if args.dry_run:
        return 0

    from pyopticfilm.device.model_8200i import MODEL_8200I
    from pyopticfilm.device.select import MODEL_7600I_V2
    from pyopticfilm.scanner import Scanner

    target_model = {
        "8200i": MODEL_8200I,
        "7600i-v2": MODEL_7600I_V2,
    }[args.model]
    if target_model.scan_ready:
        print(f"Note: {target_model.name}.scan_ready is already True.")

    scanner = Scanner.open(args.device_id)
    try:
        if scanner.model.asic != "GL845" or scanner.model.name != expected_name:
            raise SystemExit(
                f"Expected {model_label} GL845, got {scanner.model.model} "
                f"({scanner.model.asic}); select its exact USB device with --device-id"
            )
        if args.allow_unvalidated:
            scanner._allow_unvalidated_scan = True  # lab harness unlock
            scanner.arm_bringup_motor()

        out = args.out or Path(f"bringup_{args.model.replace('-', '_')}_out")
        if {"tiny", "calib", "ir"}.intersection(steps):
            out.mkdir(parents=True, exist_ok=True)

        if "open" in steps:
            print(f"open: {scanner.model.vendor} {scanner.model.model}")
        if "status" in steps:
            st = scanner.status()
            print(f"status: {st}")
        if "init" in steps:
            print("init: configuring GL845 registers…")
            scanner.asic.init()
            print("init: ok")
        if "lamp" in steps:
            scanner.lamp_on()
            print("lamp: on")
            scanner.lamp_off()
            print("lamp: off")
        if "home" in steps:
            print("home: seeking (hand near power)…")
            scanner.home()
            print("home: ok")
        if "tiny" in steps:
            print(f"tiny: {args.dpi} dpi crop, apply_calib=False…")
            image = scanner.scan(
                resolution=args.dpi,
                mode="color",
                area=(0.0, 0.0, 0.15, 0.1),
                apply_calib=False,
            )
            path = out / f"tiny_{args.dpi}"
            _save_rgb(image.rgb, path)
            print(
                f"tiny: wrote {path.with_suffix('.npy')} and "
                f"{path.with_suffix('.ppm')} shape={image.rgb.shape}"
            )
        if "park" in steps:
            print("park:…")
            scanner.park()
            print("park: ok")
        if "calib" in steps:
            print(f"calib+scan: host calib at {args.dpi} dpi area={args.area}…")
            image = scanner.scan(
                resolution=args.dpi,
                mode="color",
                area=args.area,
                apply_calib=True,
            )
            path = out / f"calib_{args.dpi}"
            _save_rgb(image.rgb, path)
            print(f"calib: wrote {path.with_suffix('.npy')} and {path.with_suffix('.ppm')}")
            scanner.park()
        if "ir" in steps:
            if not scanner.model.supports_infrared:
                raise SystemExit("Model reports no IR")
            print("ir: pass…")
            image = scanner.scan(
                resolution=args.dpi,
                mode="infrared",
                area=(0.0, 0.0, 0.15, 0.1),
                apply_calib=True,
            )
            path = out / f"ir_{args.dpi}"
            plane = image.ir if image.ir is not None else image.rgb[:, :, 1]
            _save_gray(plane, path)
            print(f"ir: wrote {path.with_suffix('.npy')} and {path.with_suffix('.pgm')}")
            scanner.park()
        if "combo" in steps:
            print(f"combo: colour + IR at {args.dpi} dpi area={args.area}…")
            image = scanner.scan(
                resolution=args.dpi, mode="color", area=args.area, infrared=True
            )
            path = out / f"combo_{args.dpi}"
            _save_rgb(image.rgb, path)
            _save_gray(image.ir, out / f"combo_{args.dpi}_ir")
            print(
                f"combo: rgb={image.rgb.shape} ir={image.ir.shape} "
                f"ir_shift={scanner.last_align_shift_ir} status={scanner.status()}"
            )
        if "me" in steps:
            import numpy as np

            print(f"me: multi-exposure at {args.dpi} dpi area={args.area} long={args.me_long}…")
            image = scanner.scan(
                resolution=args.dpi,
                mode="color",
                area=args.area,
                multi_exposure=True,
                me_long_exposure=args.me_long,
            )
            debug = scanner.last_me_debug
            _save_rgb(image.rgb, out / f"me_{args.dpi}")
            _save_rgb(debug.rgb_short, out / f"me_{args.dpi}_short")
            _save_rgb(debug.rgb_long, out / f"me_{args.dpi}_long")
            ratio = float(np.median(debug.rgb_long)) / max(1.0, float(np.median(debug.rgb_short)))
            clipped = float((debug.rgb_long >= 65000).mean())
            print(
                f"me: short={debug.exposure_short} long={debug.exposure_long} "
                f"({debug.exposure_reason}) median ratio={ratio:.2f} "
                f"long clipped={clipped:.2%} shift={debug.align_shift_long} "
                f"status={scanner.status()}"
            )
    finally:
        scanner.close()

    print(
        "Done. If image + park succeeded, update only this model's "
        "scan_ready flag and add its model-lock tests."
    )
    return 0


def _parse_area(raw: str) -> tuple[float, float, float, float]:
    parts = tuple(float(v) for v in raw.split(","))
    if len(parts) != 4 or not all(0.0 <= v <= 1.0 for v in parts):
        raise argparse.ArgumentTypeError("area must be four values in 0..1: x0,y0,x1,y1")
    return parts  # type: ignore[return-value]


def _save_rgb(rgb, path: Path) -> None:
    import numpy as np

    arr = np.asarray(rgb)
    np.save(path.with_suffix(".npy"), arr)
    # Also write a crude PPM preview (8-bit) for quick eyeballing without Pillow.
    if arr.dtype == np.uint16:
        arr8 = (arr / 257).astype(np.uint8)
    else:
        arr8 = arr.astype(np.uint8)
    h, w, _ = arr8.shape
    ppm = path.with_suffix(".ppm")
    with ppm.open("wb") as fh:
        fh.write(f"P6\n{w} {h}\n255\n".encode("ascii"))
        fh.write(np.ascontiguousarray(arr8).tobytes())


def _save_gray(plane, path: Path) -> None:
    import numpy as np

    arr = np.asarray(plane)
    np.save(path.with_suffix(".npy"), arr)
    if arr.dtype == np.uint16:
        arr8 = (arr / 257).astype(np.uint8)
    else:
        arr8 = arr.astype(np.uint8)
    h, w = arr8.shape
    pgm = path.with_suffix(".pgm")
    with pgm.open("wb") as fh:
        fh.write(f"P5\n{w} {h}\n255\n".encode("ascii"))
        fh.write(np.ascontiguousarray(arr8).tobytes())



if __name__ == "__main__":
    sys.exit(main())
