# SPDX-License-Identifier: GPL-3.0-or-later
"""Summarise a GL845 USBPcap capture as register snapshots and motor tables.

Streams the capture (bulk-in payloads are counted, never stored) and reports,
for every motor start (``0x0F = 1``): the register state at that moment, the
analogue front-end codes, and the image bytes read until the next start.
Slope tables written to the motor AHB slots are listed separately.

Usage::

    uv run python tools/dump_gl845_capture.py scan.pcapng
    uv run python tools/dump_gl845_capture.py scan.pcapng --json out.json
    uv run python tools/dump_gl845_capture.py a.pcapng b.pcapng --json out.json
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from capture_ledger import (
    CONTROL_STAGE_SETUP,
    TRANSFER_BULK,
    TRANSFER_CONTROL,
    iter_pcapng_packets,
    parse_usbpcap_header,
)

from pyopticfilm.usb.protocol import (
    REQUEST_BUFFER,
    REQUEST_TYPE_OUT,
    VALUE_BUFFER,
    VALUE_SET_REGISTER,
)

#: AHB base of each motor slope slot (see ``asic/motor.py`` ``SLOPE_TABLE_AHB``).
SLOPE_SLOTS = {
    0x10000000: "scan",
    0x10004000: "backtrack",
    0x10008000: "stop",
    0x1000C000: "fast",
    0x10010000: "home",
}

#: Registers reported in each snapshot.
SNAPSHOT_REGS = (
    0x01, 0x02, 0x03, 0x04, 0x05, 0x0B, 0x0D, 0x1C, 0x1E, 0x1F, 0x20, 0x21, 0x22,
    0x23, 0x24, 0x34, 0x5E, 0x5F, 0x67, 0x68, 0x69, 0x6A, 0x80, 0xA8,
)  # fmt: skip


def _u16(regs: dict[int, int], addr: int) -> int:
    return (regs.get(addr, 0) << 8) | regs.get(addr + 1, 0)


def _u24(regs: dict[int, int], addr: int) -> int:
    return (regs.get(addr, 0) << 16) | (regs.get(addr + 1, 0) << 8) | regs.get(addr + 2, 0)


def _snapshot(ts: float, regs: dict[int, int], fe: dict[int, int]) -> dict[str, Any]:
    return {
        "t": round(ts, 3),
        "regs": {f"0x{a:02x}": regs[a] for a in SNAPSHOT_REGS if a in regs},
        "dpiset": _u16(regs, 0x2C),
        "strpixel": _u16(regs, 0x30),
        "endpixel": _u16(regs, 0x32),
        "lincnt": _u24(regs, 0x25),
        "maxwd": _u24(regs, 0x35),
        "lperiod": _u16(regs, 0x38),
        "feedl": _u24(regs, 0x3D),
        "afe": [fe.get(i) for i in range(8)],
        "bulk_in_bytes": 0,
    }


def summarise(path: Path) -> dict[str, Any]:
    """Return ``{"starts": [...], "slope_tables": [...]}`` for one capture."""
    regs: dict[int, int] = {}
    fe: dict[int, int] = {}
    starts: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    pending_ahb: tuple[int, int] | None = None
    t0: float | None = None

    for ts, payload in iter_pcapng_packets(path):
        parsed = parse_usbpcap_header(payload)
        if parsed is None:
            continue
        transfer, endpoint, _info, data, stage, _irp = parsed
        if t0 is None and ts is not None:
            t0 = ts
        rel = (ts - t0) if (ts is not None and t0 is not None) else 0.0

        if transfer == TRANSFER_CONTROL:
            if stage != CONTROL_STAGE_SETUP or len(data) < 8:
                continue
            bm, breq, wvalue, _windex, _wlen = struct.unpack_from("<BBHHH", data, 0)
            if bm != REQUEST_TYPE_OUT or breq != REQUEST_BUFFER:
                continue
            body = bytes(data[8:])
            if (wvalue & 0xFF) == VALUE_SET_REGISTER and not wvalue & 0x100:
                for i in range(0, len(body) - 1, 2):
                    addr, value = body[i], body[i + 1]
                    regs[addr] = value
                    if addr == 0x3B:
                        # AFE write: 0x51 = sub-address, 0x3A/0x3B = value.
                        fe[regs.get(0x51, 0)] = (regs.get(0x3A, 0) << 8) | value
                    elif addr == 0x0F and value == 0x01:
                        starts.append(_snapshot(rel, regs, fe))
            elif wvalue == VALUE_BUFFER and len(body) >= 8:
                pending_ahb = struct.unpack_from("<II", body, 0)
        elif transfer == TRANSFER_BULK and data:
            if endpoint & 0x80:
                if starts:
                    starts[-1]["bulk_in_bytes"] += len(data)
            elif pending_ahb is not None:
                addr, _size = pending_ahb
                slot = SLOPE_SLOTS.get(addr)
                if slot is not None:
                    n = len(data) // 2
                    words = list(struct.unpack_from(f"<{n}H", data, 0))
                    tables.append({"t": round(rel, 3), "slot": slot, "words": words})
                pending_ahb = None

    return {"capture": path.name, "starts": starts, "slope_tables": tables}


def _print(summary: dict[str, Any]) -> None:
    print(f"== {summary['capture']}")
    for s in summary["starts"]:
        r = s["regs"]
        print(
            f"{s['t']:8.2f} 01={r.get('0x01', 0):02x} 02={r.get('0x02', 0):02x} "
            f"03={r.get('0x03', 0):02x} a8={r.get('0xa8', 0):02x} dpiset={s['dpiset']} "
            f"px={s['strpixel']}..{s['endpixel']} lincnt={s['lincnt']} "
            f"lperiod={s['lperiod']} feedl={s['feedl']} afe={s['afe'][2:]} "
            f"bytes={s['bulk_in_bytes']}"
        )
    seen: set[tuple[str, tuple[int, ...]]] = set()
    for t in summary["slope_tables"]:
        key = (t["slot"], tuple(t["words"]))
        if key in seen:
            continue
        seen.add(key)
        w = t["words"]
        print(f"{t['t']:8.2f} slope[{t['slot']}] n={len(w)} head={w[:8]} tail={w[-3:]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", type=Path)
    parser.add_argument("--json", type=Path, default=None, help="Write the summaries here")
    args = parser.parse_args(argv)

    summaries = [summarise(p) for p in args.captures]
    for s in summaries:
        _print(s)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(summaries, indent=1) + "\n")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
