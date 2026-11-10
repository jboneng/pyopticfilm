# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan several times in one process, reopening the scanner for each scan as NegPy does.

Each pass logs reg 0x01 and the status right after open, before ``init``.

Usage (scanner connected, film loaded)::

    uv run python scripts/repro_reopen_scans.py --infrared
    uv run python scripts/repro_reopen_scans.py --scans 5 --dpi 1800 --debug 2> reopen.log
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback

from pyopticfilm import Scanner
from pyopticfilm.logging import enable_debug_logging


def _snap(scanner: Scanner, label: str) -> None:
    asic = scanner.asic
    try:
        st = asic.read_status_reliable()
        r01 = asic.protocol.read_register(0x01)
    except Exception as exc:  # noqa: BLE001
        print(f"[{label}] register read FAILED: {exc}")
        return
    print(
        f"[{label}] 0x01=0x{r01:02x} SCAN={bool(r01 & 0x01)} status=0x{st.raw:02x} "
        f"home={st.is_at_home} motor={st.is_motor_enabled} buf_empty={st.is_buffer_empty}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scans", type=int, default=3)
    parser.add_argument("--dpi", type=int, default=900)
    parser.add_argument("--infrared", action="store_true", help="Colour + IR (parks after the scan)")
    parser.add_argument("--device", default=None, help="device_id; default: first scan-ready scanner")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if args.debug:
        enable_debug_logging()

    for n in range(1, args.scans + 1):
        t0 = time.monotonic()
        print(f"=== scan {n}/{args.scans} ===")
        try:
            with Scanner.open(args.device) as scanner:
                print(f"opened {scanner.device_id} model={scanner.model.model}")
                _snap(scanner, "before_init")
                if not scanner.asic._initialized:
                    scanner.asic.init()
                if not scanner.asic.is_at_home():
                    scanner.home()
                image = scanner.scan(resolution=args.dpi, infrared=args.infrared)
                _snap(scanner, "after_scan")
        except Exception as exc:  # noqa: BLE001
            print(f"scan {n} FAILED after {time.monotonic() - t0:.1f}s: {exc!r}")
            traceback.print_exc()
            return 1
        print(f"scan {n} OK shape={image.rgb.shape} ({time.monotonic() - t0:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
