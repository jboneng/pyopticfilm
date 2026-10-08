# SPDX-License-Identifier: GPL-3.0-or-later
"""Dump GL128 init+_configure register programs (SE / 8100 V2 / 8300i SE).

Usage:
    python tools/dump_gl128_setup_trace.py
    python tools/dump_gl128_setup_trace.py --model se --dpi 1800
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from scanners.setup_gl128 import run_gl128_setup
from scanners.trace_compare import dump_trace

from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE

_MODELS = {
    "se": (MODEL_8200I_SE, "8200i_se", "OpticFilm 8200i SE"),
    "v2": (MODEL_8100_V2, "8100_v2", "OpticFilm 8100 (V2)"),
    "8300i": (MODEL_8300I_SE, "8300i_se", "OpticFilm 8300i SE"),
}
_DPIS = (1200, 1800, 7200)


def _out_path(folder: str, dpi: int) -> Path:
    return ROOT / "tests" / "traces" / "python" / folder / f"{dpi}_rgb16_setup.json"


def _dump_one(key: str, dpi: int) -> Path:
    model, folder, label = _MODELS[key]
    usb, geo = run_gl128_setup(model, dpi=dpi)
    out = _out_path(folder, dpi)
    dump_trace(
        out,
        transactions=(),
        registers={addr: val & 0xFF for addr, val in usb.registers.items()},
        meta={
            "source": "pyopticfilm",
            "backend": "pyopticfilm",
            "model": label,
            "asic": "GL128",
            "dpi": int(geo.resolution),
            "mode": "rgb16",
            "phase": "init+configure",
            "pixels": geo.pixels,
            "lines": geo.lines,
            "dpiset": geo.register_dpiset,
            "lincnt": geo.lincnt_register,
            "lperiod": geo.exposure_lperiod,
        },
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("se", "v2", "8300i", "all"), default="all")
    parser.add_argument("--dpi", type=int, default=0, help="One PPI, or 0 for 1200/1800/7200")
    args = parser.parse_args()
    keys = ("se", "v2", "8300i") if args.model == "all" else (args.model,)
    dpis = _DPIS if not args.dpi else (args.dpi,)
    for key in keys:
        for dpi in dpis:
            path = _dump_one(key, dpi)
            print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
