# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal interactive OpticFilm scan example.

Enumerate connected scanners, pick one, choose PPI, optionally enable
multi-exposure (ME) and infrared (IR) where the model offers them (ME on the
8200i SE, 8100 V2 and 8300i SE except at 7200 dpi; IR on the 8200i SE and
8300i SE, and on the 7600i v1 at 3600/7200 dpi), scan the default full frame,
and write 16-bit TIFF files to a folder.

Requires WinUSB/libusb binding and ``pip install tifffile`` for TIFF output::

    uv run python examples/scan.py
    uv run python examples/scan.py --out ./frames
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pyopticfilm import Scanner
from pyopticfilm.device.select import model_for_device, model_is_scan_ready
from pyopticfilm.image import save_gray16_tiff
from pyopticfilm.usb.device import find_devices


def _prompt_choice(prompt: str, count: int) -> int:
    while True:
        raw = input(f"{prompt} [1-{count}]: ").strip()
        if not raw.isdigit():
            print("Enter a number.")
            continue
        choice = int(raw)
        if 1 <= choice <= count:
            return choice - 1
        print(f"Choose between 1 and {count}.")


def _prompt_yes_no(prompt: str, *, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("Answer y or n.")


def _prompt_ppi(supported: tuple[int, ...]) -> int:
    ordered = tuple(sorted(supported))
    print("Supported PPI:", ", ".join(str(d) for d in ordered))
    while True:
        raw = input("PPI: ").strip()
        if not raw.isdigit():
            print("Enter a whole number.")
            continue
        dpi = int(raw)
        if dpi in supported:
            return dpi
        print(f"PPI {dpi} is not supported. Pick one from the list above.")


def _progress(fraction: float) -> None:
    pct = min(100, max(0, int(fraction * 100)))
    print(f"\rScanning… {pct:3d}%", end="", flush=True)
    if pct >= 100:
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output folder (prompted when omitted)",
    )
    args = parser.parse_args()

    devices = find_devices()
    if not devices:
        print("No Plustek OpticFilm scanners found.", file=sys.stderr)
        print("Check USB connection and WinUSB/libusb binding.", file=sys.stderr)
        return 1

    print("Connected scanners:")
    for i, info in enumerate(devices, start=1):
        model = model_for_device(info.product_id, info.bcd_device)
        tag = "scan-ready" if model_is_scan_ready(model) else "probe-only"
        print(f"  {i}. {model.model}  {info.device_id}  [{tag}]")

    index = _prompt_choice("Select scanner", len(devices))
    picked = devices[index]
    model = model_for_device(picked.product_id, picked.bcd_device)

    if not model_is_scan_ready(model):
        print(
            f"{model.model} is not scan-ready in this release; "
            "only the OpticFilm 8200i SE, OpticFilm 8100 (V2), "
            "OpticFilm 8300i SE, and OpticFilm 7600i v1 can scan.",
            file=sys.stderr,
        )
        return 1

    dpi = _prompt_ppi(model.resolutions_dpi)

    multi_exposure = False
    infrared = False
    me_dpis = getattr(model, "multi_exposure_resolutions_dpi", None)
    if me_dpis is not None:
        if dpi in me_dpis:
            multi_exposure = _prompt_yes_no("Enable multi-exposure (ME)?")
        else:
            print(f"Multi-exposure is not available at {dpi} dpi.")
    if getattr(model, "supports_infrared", False):
        # Models without the attribute (GL128) take IR at every resolution.
        if dpi in getattr(model, "infrared_resolutions_dpi", model.resolutions_dpi):
            infrared = _prompt_yes_no("Enable infrared dust plane (IR)?")
        else:
            print(f"Infrared is not available at {dpi} dpi.")

    if args.out is not None:
        out = args.out
    else:
        raw = input("Output folder [scan_out]: ").strip()
        out = Path(raw or "scan_out")
    out.mkdir(parents=True, exist_ok=True)

    with Scanner.open(picked.device_id) as scanner:
        print(f"Opened {scanner.model.model}")
        scanner.warmup()
        print(f"Scanning at {dpi} dpi…")

        # Default full frame: no area or geometry, as in the README quick start.
        image = scanner.scan(
            resolution=dpi,
            mode="color",
            multi_exposure=multi_exposure,
            infrared=infrared,
            progress=_progress,
        )

        colour_path = image.save_tiff(out / "colour.tif")
        print(f"Colour {image.rgb.shape} → {colour_path}")

        if image.ir is not None:
            ir_path = save_gray16_tiff(image.ir, out / "ir_plane.tif", dpi=image.dpi)
            print(f"IR plane {image.ir.shape} → {ir_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
