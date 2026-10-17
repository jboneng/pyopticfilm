# SPDX-License-Identifier: GPL-3.0-or-later
"""Stationary GL128 probe: are AFE indices 0x02-0x04 gains or offsets?

pyopticfilm names ``0x02``-``0x04`` offsets and ``0x05``-``0x07`` gains. The
AD9826 register map (and another 8200i SE backend) say the reverse, with a
6-bit PGA gain at ``0x02``-``0x04``. This probe sweeps the red code of each
field with the lamp on (white) and off (dark) and classifies the response:

- gain: white moves a lot, dark barely moves; a 6-bit field wraps at 0x40.
- offset: white and dark shift by about the same amount, no wrap.

Safety: the carriage must be at home. Only stationary AFE strips are read
(``0x02`` motor register cleared, no feed/home/park). The calibration cache is
not touched. Lamp off + close always run.

Usage (from repo root, scanner on, holder in, carriage at home)::

    uv run python tools/probe_afe_fields.py
    uv run python tools/probe_afe_fields.py --fields 0x03,0x04,0x06,0x07
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

SWEEP_A_INDEX = 0x02
SWEEP_A_CODES = (0x00, 0x10, 0x20, 0x30, 0x3E, 0x3F, 0x40, 0x41, 0x50, 0x7F, 0x80, 0xFF)
SWEEP_B_INDEX = 0x05
SWEEP_B_CODES = (0x000, 0x040, 0x080, 0x0C0, 0x100, 0x180, 0x1FF)

BASELINE_GAINS = (0x00, 0x00, 0x00)
BASELINE_OFFSETS = (0x80, 0x80, 0x80)

#: Minimum white movement (u16 counts) before a verdict is trusted.
MIN_SPAN = 512.0
#: Dark readings at or below this are treated as clipped at zero.
DARK_FLOOR = 64.0
#: White readings at or above this are treated as saturated.
WHITE_CEIL = 65000.0
GAIN_RATIO = 3.0
OFFSET_RATIO_LO = 0.5
OFFSET_RATIO_HI = 2.0

DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "scanlab" / "runs" / "afe-probe"


def _span(values: Mapping[int, float]) -> float:
    return max(values.values()) - min(values.values()) if values else 0.0


def wraps_at_0x40(values: Mapping[int, float]) -> bool:
    """True if ``0x40`` reads like ``0x00`` while ``0x3F`` clearly differs."""
    if not all(code in values for code in (0x00, 0x3F, 0x40)):
        return False
    step = abs(values[0x3F] - values[0x00])
    if step < MIN_SPAN:
        return False
    return abs(values[0x40] - values[0x00]) < 0.25 * step


def classify_afe_field(white: Mapping[int, float], dark: Mapping[int, float]) -> str:
    """Return ``"gain"``, ``"offset"`` or ``"unclear"`` for one swept field.

    ``white`` / ``dark`` map FE code to the swept channel's mean (u16) with the
    lamp on / off. A wrap at ``0x40`` in the white sweep is a gain on its own.
    Otherwise the ratio of white span to dark span decides; a dark sweep that
    sits clipped at zero (or a white sweep pinned at saturation) gives no
    usable ratio, so that case is ``"unclear"``.
    """
    if not white or not dark:
        return "unclear"
    if wraps_at_0x40(white):
        return "gain"
    white_span = _span(white)
    dark_span = _span(dark)
    if white_span < MIN_SPAN:
        return "unclear"
    if max(dark.values()) <= DARK_FLOOR or min(white.values()) >= WHITE_CEIL:
        return "unclear"
    ratio = white_span / max(dark_span, 1.0)
    if ratio >= GAIN_RATIO:
        return "gain"
    if OFFSET_RATIO_LO <= ratio <= OFFSET_RATIO_HI:
        return "offset"
    return "unclear"


def _frontend_for(index: int, code: int):
    from pyopticfilm.scan.calib_gl128 import AfeFrontend

    gains = list(BASELINE_GAINS)
    offsets = list(BASELINE_OFFSETS)
    if 0x02 <= index <= 0x04:
        gains[index - 0x02] = code
    elif 0x05 <= index <= 0x07:
        offsets[index - 0x05] = code
    else:
        raise ValueError(f"FE index {index:#x} is not a channel field")
    return AfeFrontend(gains=tuple(gains), offsets=tuple(offsets))


def _pick_layout(asic) -> bool:
    """Planar/chunky choice for AFE strips, same rule as ``search_afe``."""
    from pyopticfilm.scan.calib_gl128 import AFE_STRIP_BYTES, channel_means_u16, choose_usb_planar

    asic.apply_frontend(_frontend_for(SWEEP_A_INDEX, BASELINE_GAINS[0]))
    probe = asic.acquire_afe_strip(AFE_STRIP_BYTES)
    mean_planar = channel_means_u16(probe, planar=True)
    mean_chunky = channel_means_u16(probe, planar=False)

    def _imbalance(means: tuple[float, float, float]) -> float:
        return max(means) / max(1.0, min(means))

    planar = choose_usb_planar(probe)
    if _imbalance(mean_planar if planar else mean_chunky) > 2.0:
        planar = _imbalance(mean_planar) <= _imbalance(mean_chunky)
    print(
        f"Strip layout: {'planar' if planar else 'chunky'} "
        f"(planar means {_fmt(mean_planar)}, chunky means {_fmt(mean_chunky)})"
    )
    return planar


def _fmt(means: tuple[float, float, float]) -> str:
    return "(" + ", ".join(f"{m:7.0f}" for m in means) + ")"


def _read(asic, planar: bool, index: int, code: int) -> tuple[float, float, float]:
    from pyopticfilm.scan.calib_gl128 import AFE_STRIP_BYTES, channel_means_u16

    asic.apply_frontend(_frontend_for(index, code))
    return channel_means_u16(asic.acquire_afe_strip(AFE_STRIP_BYTES), planar=planar)


def sweep_codes(index: int) -> tuple[int, ...]:
    """Sweep A codes for ``0x02``-``0x04``, sweep B codes for ``0x05``-``0x07``."""
    if 0x02 <= index <= 0x04:
        return SWEEP_A_CODES
    if 0x05 <= index <= 0x07:
        return SWEEP_B_CODES
    raise ValueError(f"FE index {index:#x} is not a channel field")


def field_channel(index: int) -> int:
    """Channel (0=R, 1=G, 2=B) that ``index`` is named for."""
    return (index - 0x02) % 3


def _run_lamp_state(
    asic, planar: bool, lamp: str, fields: tuple[int, ...], rows: list[dict]
) -> dict[int, dict[int, tuple[float, float, float]]]:
    """Baseline, then each field sweep followed by a baseline. Returns RGB means per field and code."""
    out: dict[int, dict[int, tuple[float, float, float]]] = {index: {} for index in fields}

    def _baseline(tag: str) -> None:
        means = _read(asic, planar, SWEEP_A_INDEX, BASELINE_GAINS[0])
        rows.append(_row(lamp, f"baseline-{tag}", None, means))
        print(f"  baseline {tag:<6}            {_fmt(means)}")

    print(f"\nLamp {lamp}:                      R        G        B")
    _baseline("start")
    for n, index in enumerate(fields):
        for code in sweep_codes(index):
            means = _read(asic, planar, index, code)
            out[index][code] = means
            rows.append(_row(lamp, f"sweep-{index:#04x}", (index, code), means))
            print(f"  sweep FE[{index:#04x}]={code:#05x}    {_fmt(means)}")
        _baseline("end" if n == len(fields) - 1 else f"after{index:#04x}")
    return out


def _row(lamp: str, phase: str, target: tuple[int, int] | None, means: tuple[float, float, float]) -> dict:
    return {
        "lamp": lamp,
        "phase": phase,
        "fe_index": f"{target[0]:#04x}" if target else "",
        "code": f"{target[1]:#05x}" if target else "",
        "r": f"{means[0]:.1f}",
        "g": f"{means[1]:.1f}",
        "b": f"{means[2]:.1f}",
    }


def _drift(rows: list[dict], lamp: str) -> None:
    base = [float(r["r"]) for r in rows if r["lamp"] == lamp and r["phase"].startswith("baseline")]
    if len(base) >= 2:
        ref = max(1.0, base[0])
        print(f"  lamp {lamp} baseline R drift: " + ", ".join(f"{(v - base[0]) / ref:+.1%}" for v in base[1:]))


def _write_csv(rows: list[dict], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{datetime.now().astimezone():%Y%m%d-%H%M%S}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="CSV output directory")
    parser.add_argument(
        "--fields",
        default=f"{SWEEP_A_INDEX:#04x},{SWEEP_B_INDEX:#04x}",
        help="Comma list of FE indices to sweep, from 0x02-0x07 (default 0x02,0x05)",
    )
    parser.add_argument(
        "--layout",
        choices=("auto", "planar", "chunky"),
        default="auto",
        help="Strip decode; auto picks it the way search_afe does",
    )
    args = parser.parse_args(argv)
    fields = tuple(int(part, 0) for part in args.fields.split(",") if part.strip())
    for index in fields:
        sweep_codes(index)

    from pyopticfilm.scanner import Scanner

    scanner = Scanner.open()
    rows: list[dict] = []
    try:
        model = scanner.model
        if model.asic != "GL128":
            print(f"Refusing: {model.model} uses {model.asic}, this probe is GL128 only.")
            return 2
        asic = scanner._asic
        if hasattr(asic, "_motor_moves_enabled"):
            asic._motor_moves_enabled = False
        if not asic.is_at_home():
            print("Refusing: carriage is not at home. Park it (normal scan or SilverFast) first.")
            return 2
        asic.init()
        if not asic.is_at_home():
            print("Refusing: carriage left home after init.")
            return 2
        print(f"Probing {model.model} ({model.asic}) - stationary, motor off.")

        asic.set_scan_method("transparency")
        asic.lamp_on()
        time.sleep(1.0)
        planar = _pick_layout(asic)
        if args.layout != "auto":
            planar = args.layout == "planar"
            print(f"Strip layout forced: {args.layout}")
        white = _run_lamp_state(asic, planar, "on", fields, rows)

        asic.lamp_off()
        time.sleep(0.5)
        dark = _run_lamp_state(asic, planar, "off", fields, rows)

        print("\nLamp drift:")
        _drift(rows, "on")
        _drift(rows, "off")

        print("\nVerdict (on the field's own channel; white spans per channel show cross-talk):")
        for index in fields:
            ch = field_channel(index)
            w = {code: m[ch] for code, m in white[index].items()}
            d = {code: m[ch] for code, m in dark[index].items()}
            spans = "/".join(f"{_span({c: m[k] for c, m in white[index].items()}):.0f}" for k in range(3))
            print(
                f"  FE[{index:#04x}] ({'RGB'[ch]}) -> {classify_afe_field(w, d):<7} "
                f"(white span {_span(w):.0f}, dark span {_span(d):.0f}, "
                f"wrap at 0x40: {'yes' if wraps_at_0x40(w) else 'no'}, white span R/G/B {spans})"
            )
    finally:
        try:
            scanner._asic.lamp_off()
        except Exception as exc:  # noqa: BLE001
            print(f"lamp_off failed: {exc}", file=sys.stderr)
        scanner.close()
        if rows:
            print(f"\nCSV: {_write_csv(rows, args.out)}")
        print("\nPower-cycle the scanner (or run a normal scan) so AFE codes are recalibrated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
