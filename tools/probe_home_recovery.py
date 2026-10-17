# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardware probe for GL128 carriage home recovery (reverse seek, back-off, reverse seek).

Run the stages in order. Each one logs the ``0x101`` status walk and timings.
The ASIC never stops a reverse feed on the home sensor by itself; every seek
ends with the driver dropping motor power ("software stop").

Stages:

- ``backoff``: from home, feed ``home_backoff_steps`` forward, then recover.
- ``reference``: from home, feed the reference distance (28292) forward, then recover.
- ``recover``: recover from wherever the carriage is now.
- ``repeat``: 600 dpi scan from home, positioning pair + recover, second scan;
  reports the frame shift between the two. Compare with ``baseline`` (8200i SE,
  2026-10-17: 2.3 rows after recovery vs 3.3 rows with a normal park).
- ``baseline``: two 600 dpi scans with only the normal park in between; the
  ordinary scan-to-scan shift to compare ``repeat`` against.

Stop and power-cycle if the motor grinds or a stage times out.

Do not hard-kill the process during an image pass to simulate a crash: on the
8200i SE (2026-10-17) the abandoned pass ground hard and the scanner dropped
off USB, so the recovery could not even connect.

Usage (from repo root, scanner on, holder in)::

    uv run python tools/probe_home_recovery.py backoff
    uv run python tools/probe_home_recovery.py reference
    uv run python tools/probe_home_recovery.py recover
    uv run python tools/probe_home_recovery.py repeat
    uv run python tools/probe_home_recovery.py baseline
"""

from __future__ import annotations

import argparse
import csv
import logging
import time
from datetime import datetime
from pathlib import Path

DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "scanlab" / "runs" / "home-recovery"
REPEAT_DPI = 600


class StatusRecorder:
    """Wrap ``read_status_reliable`` and keep each change of the raw status byte."""

    def __init__(self, asic) -> None:
        self.asic = asic
        self.t0 = time.monotonic()
        self.rows: list[dict] = []
        self._last: int | None = None
        self._orig = asic.read_status_reliable
        asic.read_status_reliable = self._read

    def _read(self):
        status = self._orig()
        raw = int(status.raw) & 0xFF
        if raw != self._last:
            self._last = raw
            self.rows.append(
                {
                    "t_s": f"{time.monotonic() - self.t0:.3f}",
                    "status": f"0x{raw:02x}",
                    "home": int(status.is_at_home),
                    "motor": int(status.is_motor_enabled),
                    "feed_done": int(status.is_feeding_finished),
                }
            )
        return status

    def mark(self, label: str) -> None:
        self.rows.append({"t_s": f"{time.monotonic() - self.t0:.3f}", "status": label, "home": "", "motor": "", "feed_done": ""})
        self._last = None

    def restore(self) -> None:
        self.asic.read_status_reliable = self._orig


class SeekLog(logging.Handler):
    """Collect the driver's reverse-seek summary lines."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "reverse" in msg or "recover" in msg or "HOMESNR" in msg:
            self.lines.append(f"{record.levelname}: {msg}")


def _write_csv(rows: list[dict], out_dir: Path, stage: str) -> Path | None:
    if not rows:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{datetime.now().astimezone():%Y%m%d-%H%M%S}-{stage}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _require_home(asic, stage: str) -> bool:
    if asic.is_at_home():
        return True
    print(f"Refusing {stage}: carriage is not at home. Run the 'recover' stage first.")
    return False


def _recover(asic, rec: StatusRecorder) -> float:
    rec.mark("recover-start")
    started = time.monotonic()
    moved = asic.recover_home(timeout_s=30.0)
    took = time.monotonic() - started
    rec.mark("recover-end")
    print(f"recover_home: {'moved' if moved else 'already home, no motion'} in {took:.2f}s")
    return took


def _feed_then_recover(asic, rec: StatusRecorder, steps: int, stage: str) -> None:
    if not _require_home(asic, stage):
        return
    rec.mark(f"feed-{steps}")
    # Slow ramp from standstill at home, like the first positioning feed.
    # Gl128.feed() uploads the fast ramp, which stalled and ground here.
    asic._feed_capture(steps, timeout_s=30.0, require_motion=True, use_slow_slope=True)
    print(f"Fed {steps} steps forward, at home: {asic.is_at_home()}")
    _recover(asic, rec)


def _report_shift(first, second, label: str) -> None:
    import numpy as np

    from pyopticfilm.pass_align import estimate_pass_shift

    a = np.asarray(first.rgb, dtype=np.float32)
    b = np.asarray(second.rgb, dtype=np.float32)
    h = min(a.shape[0], b.shape[0])
    w = min(a.shape[1], b.shape[1])
    dx, dy = estimate_pass_shift(a[:h, :w], b[:h, :w])
    print(f"Frame shift {label}: dx={dx:+.2f} px, dy={dy:+.2f} rows at {REPEAT_DPI} dpi")


def _baseline(scanner, rec: StatusRecorder) -> None:
    if not _require_home(scanner._asic, "baseline"):
        return
    rec.mark("scan-a")
    first = scanner.scan(resolution=REPEAT_DPI, mode="color")
    rec.mark("scan-b")
    second = scanner.scan(resolution=REPEAT_DPI, mode="color")
    rec.mark("scan-b-done")
    _report_shift(first, second, "between two scans, normal park only")


def _repeat(scanner, rec: StatusRecorder) -> None:
    asic = scanner._asic
    if not _require_home(asic, "repeat"):
        return
    rec.mark("scan-a")
    first = scanner.scan(resolution=REPEAT_DPI, mode="color")
    rec.mark("scan-a-done")
    # Leave the carriage where an interrupted scan would: the normal
    # positioning pair (warm prepare, slow reference feed, fast scan feed).
    rec.mark("position")
    asic.position_for_full_frame_scan()
    print(f"Positioned for a scan, at home: {asic.is_at_home()}")
    _recover(asic, rec)
    rec.mark("scan-b")
    second = scanner.scan(resolution=REPEAT_DPI, mode="color")
    rec.mark("scan-b-done")
    _report_shift(first, second, "after recovery")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=("backoff", "reference", "recover", "repeat", "baseline"))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="CSV output directory")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)
    seek_log = SeekLog()
    driver_log = logging.getLogger("pyopticfilm.asic.gl128")
    driver_log.setLevel(logging.INFO)
    driver_log.addHandler(seek_log)

    from pyopticfilm.scanner import Scanner

    scanner = Scanner.open()
    rec: StatusRecorder | None = None
    try:
        model = scanner.model
        if model.asic != "GL128":
            print(f"Refusing: {model.model} uses {model.asic}, this probe is GL128 only.")
            return 2
        asic = scanner._asic
        asic.init()
        rec = StatusRecorder(asic)
        first = asic.read_status_reliable()
        print(f"{model.model}: status 0x{int(first.raw) & 0xFF:02x}, at home: {first.is_at_home}")

        if args.stage == "backoff":
            _feed_then_recover(asic, rec, int(model.home_backoff_steps), args.stage)
        elif args.stage == "reference":
            _feed_then_recover(asic, rec, int(model.feed_to_reference_steps), args.stage)
        elif args.stage == "recover":
            _recover(asic, rec)
        elif args.stage == "baseline":
            _baseline(scanner, rec)
        else:
            _repeat(scanner, rec)

        final = asic.read_status_reliable()
        print(f"Final status 0x{int(final.raw) & 0xFF:02x}, at home: {final.is_at_home}")
    finally:
        if rec is not None:
            rec.restore()
        scanner.close()
        if seek_log.lines:
            print("\nDriver:")
            for line in seek_log.lines:
                print(f"  {line}")
        if rec is not None:
            path = _write_csv(rec.rows, args.out, args.stage)
            if path is not None:
                print(f"\nStatus walk CSV: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
