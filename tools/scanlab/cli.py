# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless, scriptable Scan Lab — the AI-friendly / remote-controllable
entry point requested alongside the GUI: run a real (or mock) Prescan/Scan
without touching Qt at all, get a single JSON result on stdout, and
optionally an AI bug report file. Meant to be invoked directly by an
automation harness, a CI job, or a future Claude session via the Bash
tool — no human clicking required.

Reuses exactly the same primitives the GUI's ScanWorker and run-browser use
(open_lab_scanner/lab_scan_kwargs from backend.py, ForensicRun from
forensic_session.py, decode_transaction from usb/decode.py,
detect_anomalies, build_ai_report, first_divergence) - this is a second,
independent *driver* of that shared pipeline, not a reimplementation of it.

``compare`` is the headless equivalent of the run-browser's "Set as
baseline" + "Compare" + "Export AI bug report..." sequence: two runs
recorded under the same RUNS_ROOT (including from two different
checkouts/branches sharing a runs directory, e.g. via a directory
junction, to compare a PR's behavior against main) can be diffed without
opening the GUI at all.

``scan``'s ``--crop``/``--multi-exposure`` (``--kind scan`` only) and
``--save-tiff-dir`` cover the rest of CONTRIBUTING.md's hardware sign-off
checklist (a crop, ME) and let a human visually confirm output without
opening the GUI.

Usage:
    python -m tools.scanlab.cli scan --model "OpticFilm 8100 (V2)" --mock \
        --kind prescan --dpi 1200 --name ci-smoke

    python -m tools.scanlab.cli scan --model "OpticFilm 8100 (V2)" --real \
        --kind scan --dpi 1800 --ai-report

    python -m tools.scanlab.cli scan --model "OpticFilm 8100 (V2)" --real \
        --kind scan --dpi 7200 --crop 0.25,0.25,0.75,0.75 --multi-exposure \
        --save-tiff-dir ./review

    python -m tools.scanlab.cli list-models
    python -m tools.scanlab.cli list-runs
    python -m tools.scanlab.cli compare --baseline main-run/2026-... \
        --run pr-run/2026-... --out compare.md
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from pyopticfilm.usb.decode import decode_transaction
from tools.scanlab.backend import (
    lab_scan_kwargs,
    list_lab_targets,
    open_lab_scanner,
    prescan_resolution,
    with_mock_mode,
)
from tools.scanlab.forensic_anomaly import detect_anomalies
from tools.scanlab.forensic_diff import first_divergence, format_divergence
from tools.scanlab.forensic_report_export import build_ai_report
from tools.scanlab.forensic_session import RUNS_ROOT, ForensicRun, ForensicRunResult, get_baseline


def cmd_list_models(_args: argparse.Namespace) -> int:
    targets = list_lab_targets()
    print(json.dumps([{"model": t.model.model, "label": t.label, "connected": t.device_id is not None} for t in targets], indent=2))
    return 0


def _find_target(model_name: str, mock: bool):
    targets = list_lab_targets()
    target = next((t for t in targets if t.model.model == model_name), None)
    if target is None:
        available = [t.model.model for t in targets]
        raise SystemExit(f"Unknown model {model_name!r}. Available: {available}")
    return with_mock_mode(target, mock)


def _resolve_run_dir(raw: str) -> Path:
    """Accept either an absolute/relative run directory, or the
    ``<name>/<run_id>`` shorthand printed by ``list-runs`` and ``scan``
    (resolved against ``RUNS_ROOT``, same convention the GUI's run browser
    uses)."""
    candidate = Path(raw)
    if candidate.exists():
        return candidate
    shorthand = RUNS_ROOT / raw
    if shorthand.exists():
        return shorthand
    raise SystemExit(f"Run directory not found: {raw!r} (checked {candidate} and {shorthand})")


def cmd_list_runs(_args: argparse.Namespace) -> int:
    baseline = get_baseline()
    runs = [
        {
            "run": f"{run_dir.parent.name}/{run_dir.name}",
            "path": str(run_dir),
            "is_baseline": run_dir == baseline,
        }
        for run_dir in sorted(RUNS_ROOT.glob("*/*")) if run_dir.is_dir()
    ]
    print(json.dumps(runs, indent=2))
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    target = _find_target(args.model, mock=not args.real)

    run = ForensicRun(
        name=args.name,
        device_info={"model": target.model.model, "mock": target.mock},
    )
    events_buffer: list[dict] = []

    def sink(txn) -> None:
        decoded = decode_transaction(txn)
        decoded_json = decoded.to_json() if decoded is not None else None
        run.record(txn.to_json(), decoded_json)
        if decoded_json is not None:
            events_buffer.append(decoded_json)

    outcome, notes, image_info = "error", "", None
    try:
        scanner, _rec = open_lab_scanner(target, on_usb=sink)
    except Exception as exc:  # noqa: BLE001
        out_dir = run.finish(ForensicRunResult(outcome="error", notes=f"failed to open device: {exc}"))
        print(json.dumps({"outcome": "error", "notes": str(exc), "run_dir": str(out_dir)}, indent=2))
        return 1

    crop_norm = None
    if args.crop:
        if args.kind != "scan":
            raise SystemExit("--crop requires --kind scan")
        parts = [float(v) for v in args.crop.split(",")]
        if len(parts) != 4:
            raise SystemExit("--crop expects 'x0,y0,x1,y1'")
        crop_norm = tuple(parts)

    if args.multi_exposure and args.kind != "scan":
        raise SystemExit("--multi-exposure requires --kind scan")

    try:
        dpi = args.dpi or prescan_resolution(target.model)
        kw = lab_scan_kwargs(target.model, dpi=dpi, kind=args.kind, crop_norm=crop_norm)
        run.mark_phase(f"CLI: {args.kind} started", {"dpi": dpi, "mock": target.mock, "model": target.model.model})
        image = scanner.scan(
            mode="color",
            apply_calib=args.apply_calib,
            gl128_prime=args.gl128_prime,
            multi_exposure=args.multi_exposure,
            **kw,
        )
        image_info = {"shape": list(image.rgb.shape), "dpi": image.dpi}
        run.mark_phase(f"CLI: {args.kind} received", image_info)
        outcome = "success"
        if args.save_tiff_dir:
            out = Path(args.save_tiff_dir)
            out.mkdir(parents=True, exist_ok=True)
            tiff_path = out / f"{args.name}.tiff"
            image.save_tiff(tiff_path)
            image_info["tiff_path"] = str(tiff_path)
    except Exception as exc:  # noqa: BLE001
        notes = f"{type(exc).__name__}: {exc}"
        if args.traceback:
            notes += "\n" + traceback.format_exc()
    finally:
        try:
            scanner.close()
        except Exception:  # noqa: BLE001, S110
            pass

    out_dir = run.finish(ForensicRunResult(outcome=outcome, notes=notes, extra=image_info))

    try:
        anomalies = detect_anomalies(events_buffer)
    except Exception as exc:  # noqa: BLE001
        anomalies = []
        notes += f" (anomaly detection failed: {exc})"

    severity_counts: dict[str, int] = {}
    for a in anomalies:
        severity_counts[a.severity] = severity_counts.get(a.severity, 0) + 1

    result = {
        "outcome": outcome,
        "notes": notes,
        "run_dir": str(out_dir),
        "image": image_info,
        "n_events": len(events_buffer),
        "n_anomalies": len(anomalies),
        "anomaly_severity_counts": severity_counts,
        # Compact by design - an AI consuming this should read run_dir's
        # anomalies.json / ai_report.md for full detail, not parse a huge
        # inline array here. Only non-"info" anomalies are worth surfacing
        # immediately; "info" (e.g. routine unsafe-address writes during a
        # normal scan) would otherwise dominate and bury anything unusual.
        "notable_anomalies": [a.to_json() for a in anomalies if a.severity != "info"][:20],
    }
    (out_dir / "anomalies.json").write_text(json.dumps([a.to_json() for a in anomalies], indent=2))
    result["anomalies_file"] = str(out_dir / "anomalies.json")

    if args.ai_report:
        try:
            report = build_ai_report(out_dir)
            report_path = out_dir / "ai_report.md"
            report_path.write_text(report, encoding="utf-8")
            result["ai_report_path"] = str(report_path)
        except Exception as exc:  # noqa: BLE001
            result["ai_report_error"] = str(exc)

    print(json.dumps(result, indent=2))
    return 0 if outcome == "success" else 1


def cmd_compare(args: argparse.Namespace) -> int:
    """Headless equivalent of the Forensic tab's run-browser "Set as
    baseline" + "Compare" + "Export AI bug report..." sequence — same
    underlying functions (first_divergence, build_ai_report), no Qt.

    Runs are addressed by ``<name>/<run_id>`` (as printed by ``scan`` and
    ``list-runs``) or a full path — both resolved relative to RUNS_ROOT so
    two checkouts sharing one runs directory (e.g. via a directory
    junction) can compare each other's recordings without either knowing
    the other's filesystem layout.
    """
    run_dir = _resolve_run_dir(args.run) if args.run is not None else None
    if run_dir is None:
        runs = sorted(RUNS_ROOT.glob("*/*"))
        if not runs:
            raise SystemExit("No recorded runs found and --run was not given.")
        run_dir = runs[-1]

    if args.baseline is not None:
        baseline_dir = _resolve_run_dir(args.baseline)
    else:
        baseline_dir = get_baseline()
        if baseline_dir is None:
            raise SystemExit("No --baseline given and no baseline is set (see the GUI's 'Set as baseline').")

    divergence = first_divergence(baseline_dir, run_dir)

    report = build_ai_report(run_dir, baseline_dir=baseline_dir)
    out_path = Path(args.out) if args.out else run_dir / "ai_report.md"
    out_path.write_text(report, encoding="utf-8")

    result = {
        "baseline_dir": str(baseline_dir),
        "run_dir": str(run_dir),
        "divergence": {
            "index": divergence.index,
            "kind": divergence.kind,
            "note": divergence.note,
            "baseline_len": divergence.a_len,
            "run_len": divergence.b_len,
        },
        "diverges": divergence.index is not None,
        "report_path": str(out_path),
    }
    print(json.dumps(result, indent=2))
    if args.text:
        print()
        print(format_divergence(divergence, label_a="baseline", label_b="run"))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-models", help="list known/connected models as JSON").set_defaults(func=cmd_list_models)

    p_scan = sub.add_parser("scan", help="run one Prescan/Scan headlessly, print a JSON result")
    p_scan.add_argument("--model", required=True, help="exact model name, see list-models")
    mock_group = p_scan.add_mutually_exclusive_group()
    mock_group.add_argument("--mock", action="store_true", default=True, help="MockScannerTransport (default, safe)")
    mock_group.add_argument("--real", action="store_true", help="real connected hardware - requires explicit intent")
    p_scan.add_argument("--kind", choices=["prescan", "scan"], default="prescan")
    p_scan.add_argument("--dpi", type=int, default=None, help="default: model's prescan resolution")
    p_scan.add_argument("--name", default="cli-session")
    p_scan.add_argument("--apply-calib", action="store_true", default=True)
    p_scan.add_argument("--no-apply-calib", dest="apply_calib", action="store_false")
    p_scan.add_argument(
        "--gl128-prime",
        dest="gl128_prime",
        action="store_true",
        default=None,
        help="force the discarded priming pass on (default: model default, e.g. off on the 8100 V2)",
    )
    p_scan.add_argument(
        "--no-gl128-prime",
        dest="gl128_prime",
        action="store_false",
        help="force priming off (default: model default)",
    )
    p_scan.add_argument("--ai-report", action="store_true", help="also write ai_report.md into the run directory")
    p_scan.add_argument("--traceback", action="store_true", help="include a Python traceback in notes on failure")
    p_scan.add_argument(
        "--crop",
        default=None,
        help="normalized crop 'x0,y0,x1,y1' (0..1), scan kind only",
    )
    p_scan.add_argument(
        "--multi-exposure",
        action="store_true",
        help="2-bracket multi-exposure color scan (scan kind only)",
    )
    p_scan.add_argument(
        "--save-tiff-dir",
        default=None,
        help="write the resulting image as a 16-bit TIFF into this directory (human review only, not stored in the run dir)",
    )
    p_scan.set_defaults(func=cmd_scan)

    sub.add_parser("list-runs", help="list recorded runs (RUNS_ROOT) as JSON").set_defaults(func=cmd_list_runs)

    p_compare = sub.add_parser(
        "compare",
        help="diff two recorded runs headlessly (run-browser Compare, no Qt)",
    )
    p_compare.add_argument(
        "--run", default=None, help="run to compare, '<name>/<run_id>' or a path; default: most recent run"
    )
    p_compare.add_argument(
        "--baseline", default=None, help="baseline run, '<name>/<run_id>' or a path; default: the GUI-set baseline"
    )
    p_compare.add_argument("--out", default=None, help="report path; default: <run_dir>/ai_report.md")
    p_compare.add_argument("--text", action="store_true", help="also print the human-readable divergence text")
    p_compare.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
