# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a single Markdown "AI bug report" from a Forensic run (optionally
diffed against a baseline run) — meant to be hand-carried into a future
Claude/AI session to act on, without that session needing to open every
raw file itself first.

Deliberately conservative about what it asserts: milestones and anomalies
below are heuristic flags (see tools/scanlab/forensic_milestones.py and
forensic_anomaly.py), not confirmed defects. The report always points back
at the exact evidence files rather than inlining raw traffic, so a reader
can verify any claim.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools.scanlab.forensic_anomaly import detect_anomalies, format_anomalies
from tools.scanlab.forensic_diff import first_divergence, format_divergence
from tools.scanlab.forensic_milestones import build_milestones, format_milestones


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def build_ai_report(run_dir: Path, *, baseline_dir: Path | None = None) -> str:
    manifest = _load_json(run_dir / "manifest.json")
    result = _load_json(run_dir / "result.json")
    decoded_events = _load_jsonl(run_dir / "decoded_events.jsonl")
    phase_markers = _load_jsonl(run_dir / "phase_markers.jsonl")
    raw_events = _load_jsonl(run_dir / "usb_raw.jsonl")

    milestones = build_milestones(decoded_events, raw_events)
    anomalies = detect_anomalies(decoded_events, phase_markers=phase_markers)

    env = manifest.get("environment", {})
    lines = [
        "# Scan Lab AI bug report",
        "",
        (
            "> Milestones and anomalies below are heuristic flags derived from decoded "
            "USB traffic — not confirmed defects. Verify against the linked evidence "
            "files before treating any of this as fact."
        ),
        "",
        "## Run identity",
        f"- Run: `{run_dir.parent.name}/{run_dir.name}`",
        (
            f"- Git commit: {env.get('git_commit')}  (branch: {env.get('git_branch')}, "
            f"dirty: {env.get('git_dirty')})"
        ),
        f"- Scanner: {manifest.get('device')}",
        f"- Source: {manifest.get('source', 'live Scan Lab recording')}",
        f"- Parameters: {json.dumps(manifest.get('parameters', {}))}",
        (
            f"- Environment: Python {env.get('python_version', '?').split()[0]}, "
            f"{env.get('platform')}, USB backend: {env.get('usb_backend')}"
        ),
        "",
        "## Summary",
        (
            f"- Outcome: **{result.get('outcome', 'unknown')}** "
            f"({result.get('classification') or 'unclassified'})"
        ),
        f"- Notes: {result.get('notes') or '(none)'}",
        f"- Decoded USB events: {len(decoded_events)}",
        f"- Anomalies flagged: {len(anomalies)}",
        "",
        "## Phase durations (host-side, PROVEN — the software's own record)",
    ]
    if phase_markers:
        lines.append("| phase | started_at_s | duration_s |")
        lines.append("|---|---|---|")
        for i, marker in enumerate(phase_markers):
            dur = phase_markers[i + 1]["t"] - marker["t"] if i + 1 < len(phase_markers) else None
            dur_s = f"{dur:.3f}" if dur is not None else "(ongoing/last)"
            lines.append(f"| {marker['label']} | {marker['rel_s']:.3f} | {dur_s} |")
    else:
        lines.append("(no phase markers recorded for this run)")

    timing_kinds = {"feed_timing", "lperiod", "exposure", "pixel_clock"}
    timing_milestones = [m for m in milestones if m["kind"] in timing_kinds]
    lines += [
        "",
        "## Positioning & timing",
        (
            format_milestones(timing_milestones, collapse_repeats=False)
            if timing_milestones
            else "(no positioning-feed or timing-register milestones found)"
        ),
        "",
        "## Anomalies",
        format_anomalies(anomalies),
        "",
        "## Milestones (guessed — see confidence column)",
        format_milestones(milestones),
    ]

    if baseline_dir is not None:
        divergence = first_divergence(baseline_dir, run_dir)
        lines += [
            "",
            f"## Diff against baseline (`{baseline_dir.parent.name}/{baseline_dir.name}`)",
            format_divergence(divergence, label_a="baseline", label_b="this run"),
        ]

    evidence_files = [
        p.name for p in (
            run_dir / "usb_raw.jsonl",
            run_dir / "decoded_events.jsonl",
            run_dir / "phase_markers.jsonl",
            run_dir / "manifest.json",
        )
        if p.exists()
    ]
    lines += [
        "",
        "## Evidence files (in this run's directory)",
        *[f"- `{name}`" for name in evidence_files],
    ]

    return "\n".join(lines)
