# SPDX-License-Identifier: GPL-3.0-or-later
"""Build ``device/data/opticfilm_7600i_v1.json.gz`` from OpenOptic's capture profiles.

Usage::

    python tools/import_7600i_v1_profiles.py /path/to/OpenOptic
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "pyopticfilm" / "device" / "data" / "opticfilm_7600i_v1.json.gz"

#: OpenOptic profile key -> pyopticfilm resolution / mode key
PROFILE_KEYS = {"prescan": "color_1440", "full": "color_3600", "full7200": "color_7200", "full-ir": "infrared_3600"}


def _js_object(text: str, marker: str) -> dict:
    start = text.index(marker) + len(marker)
    end = text.index(";\n", start) if ";\n" in text[start:] else len(text.rstrip().rstrip(";"))
    return json.loads(text[start:end].rstrip().rstrip(";"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("openoptic", type=Path, help="OpenOptic source checkout")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    src = args.openoptic
    profiles_js = (src / "capture_profiles.js").read_text()
    ui_html = (src / "ui.html").read_text()
    profiles = _js_object(profiles_js, "globalThis.CAPTURE_PROFILES=")
    vendor = _js_object(ui_html, "const VENDOR=")
    try:
        commit = subprocess.run(
            ["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"

    out = {
        "format": 1,
        "model": "OpticFilm 7600i v1",
        "usb": {"vendor_id": 0x07B3, "product_id": 0x0C3B, "bcd_device": 0x0400},
        "asic": "GL843",
        "provenance": {
            "openoptic_commit": commit,
            "openoptic_profiles_sha256": hashlib.sha256(profiles_js.encode()).hexdigest(),
            "captures": {k: {"source": p["source"], "sha256": p["sha256"]} for k, p in profiles.items()},
        },
        "boot": vendor["boot"],
        "gpio": [[0x6B, 0x31], [0x6C, 0x4C], [0x6D, 0x00], [0x6E, 0x4C], [0x6F, 0x80],
                 [0xA6, 0x00], [0xA7, 0x07], [0xA8, 0x20], [0xA9, 0x01]],
        "afe": {"config": {"0": 0xF8, "1": 0x80}, "gain": [0x23, 0x1B, 0x24], "offset": [0x1E, 0x30, 0x18]},
        "profiles": {PROFILE_KEYS[k]: p for k, p in profiles.items() if k in PROFILE_KEYS},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(out, separators=(",", ":"), sort_keys=True).encode()
    with gzip.GzipFile(args.out, "wb", mtime=0) as fh:
        fh.write(raw)
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes; {len(raw)} uncompressed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
