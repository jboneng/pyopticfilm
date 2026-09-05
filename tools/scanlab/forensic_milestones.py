# SPDX-License-Identifier: GPL-3.0-or-later
"""Milestone/phase "guessing system" for a Forensic run's decoded_events.jsonl.

Ported from tools/phase_segment.py (built for the pcap-ledger capture
analysis in docs/hw-ref/8100v2/) so the SAME heuristics classify traffic
regardless of source: a live Scan Lab recording, an imported Wireshark
capture, or old pcap-derived ledgers. Classification here is explicitly a
GUESS, not ground truth - every milestone below is tagged with a
confidence level and the exact register evidence it was derived from, per
this project's PROVEN/STRONG/LIKELY/SPECULATIVE/UNKNOWN convention. It
never replaces or edits the raw/decoded event it was derived from.

What's classified (identical bit meanings to phase_segment.py, same
caveats - "guess" is not "fact"):
  * buffer_preamble w_index/size -> RAM/calib, AHB upload, or IMAGE pass
    (image pass confirmed PROVEN by wIndex alone; the shading-strip vs.
    AFE-probe split within RAM/calib is a size+register heuristic, LIKELY)
  * FEEDL (0x3d-0x3f) writes with value != 1 -> a positioning feed (STRONG:
    FEEDL=1 during acquisition is a confirmed convention, see gl128.py docstring)
  * register 0x03 changes -> lamp on/off (STRONG: LAMPPWR bit, confirmed
    across every capture in this project)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pyopticfilm.asic.gl128 import _u16_table_bytes
from pyopticfilm.asic.registers import Gl128Registers
from pyopticfilm.device.tables_8200i_se import SLOPE_TABLE_FAST, SLOPE_TABLE_SLOW
from tools.scanlab.forensic_event_inspector import _read_jsonl, load_decoded_events
from tools.scanlab.forensic_reference import explain_register
from tools.scanlab.forensic_timecode import format_timecode

_R = Gl128Registers()
#: Sourced from Gl128Registers (single source of truth for these addresses/
#: bits) instead of re-declared bare hex literals.
REG_FEEDL_ADDRS = {hex(_R.REG_FEEDL), hex(_R.REG_FEEDL + 1), hex(_R.REG_FEEDL + 2)}
REG_LAMP = hex(_R.REG_0x03)
LAMPPWR = _R.LAMPPWR
REG_LPERIOD_ADDRS = {hex(_R.REG_LPERIOD), hex(_R.REG_LPERIOD + 1), hex(_R.REG_LPERIOD + 2)}
REG_EXPOSURE_ADDRS = {hex(_R.REG_EXPOSURE), hex(_R.REG_EXPOSURE + 1), hex(_R.REG_EXPOSURE + 2)}
#: Pixel-clock singles (clk_a/clk_b, see Gl128.run_asic_shading) - not a
#: 24-bit field like LPERIOD/EXPOSURE, each byte stands alone.
PIXEL_CLOCK_ADDRS = {hex(0xA5), hex(0xAB)}
AHB_SLOPE_ADDRS = {hex(_R.AHB_SLOPE_SCAN), hex(_R.AHB_SLOPE_FAST)}
_SLOPE_FAST_BYTES = _u16_table_bytes(SLOPE_TABLE_FAST)
_SLOPE_SLOW_BYTES = _u16_table_bytes(SLOPE_TABLE_SLOW)
#: 0x101 status register, MOTORENB bit (see forensic_anomaly.py's
#: motor_enabled_prolonged rule - same bit, reused here for feed timing).
MOTORENB = 0x01
#: Max events to search forward from a FEEDL write for its slope-table
#: upload and motor-enabled start/end - keeps an irrelevant small feed
#: (e.g. a per-line feed during image acquisition) from pairing with an
#: unrelated, much-later motor-enabled span.
_FEED_SEARCH_WINDOW = 1000
#: Smallest FEEDL treated as a real positioning move for feed-timing
#: purposes (observed real feeds are thousands of steps; smaller values
#: seen in traffic are incidental resets during image acquisition).
_MIN_POSITIONING_FEEDL = 500


def _registers_at(events: list[dict[str, Any]], upto_index: int) -> dict[str, int]:
    regs: dict[str, int] = {}
    for i, ev in enumerate(events):
        if i > upto_index:
            break
        if ev.get("kind") != "reg_write":
            continue
        for pair in ev.get("fields", {}).get("pairs", []):
            regs[pair["addr"]] = pair["value"]
    return regs


def _hex24(regs: dict[str, int], hi: int) -> int | None:
    a, b, c = regs.get(hex(hi)), regs.get(hex(hi + 1)), regs.get(hex(hi + 2))
    if a is None or b is None or c is None:
        return None
    return (a << 16) | (b << 8) | c


def _motor_enabled(value: Any) -> bool | None:
    if not isinstance(value, int):
        return None
    return bool(value & MOTORENB)


def _slope_table_for_upload(
    decoded_events: list[dict[str, Any]], raw_events: list[dict[str, Any]], preamble_idx: int
) -> str | None:
    """Identify FAST/SLOW/CUSTOM for an AHB slope-table upload at
    ``decoded_events[preamble_idx]`` by comparing the following bulk_out's
    raw payload (same index in usb_raw.jsonl - the two files are 1:1, same
    order, per forensic_event_inspector.py) against the two known tables.
    None when there's no matching bulk_out or no raw payload available
    (e.g. an imported Wireshark run with no usb_raw.jsonl)."""
    j = preamble_idx + 1
    if j >= len(decoded_events) or decoded_events[j].get("kind") != "bulk_out":
        return None
    if j >= len(raw_events):
        return None
    data_hex = raw_events[j].get("data")
    if not data_hex:
        return None
    payload = bytes.fromhex(data_hex)
    if payload == _SLOPE_FAST_BYTES:
        return "FAST"
    if payload == _SLOPE_SLOW_BYTES:
        return "SLOW"
    return "CUSTOM"


def _build_feed_timing_milestones(
    decoded_events: list[dict[str, Any]], raw_events: list[dict[str, Any]], t_first: float | None
) -> list[dict[str, Any]]:
    """One milestone per positioning feed: which slope table was uploaded
    for it, and how long the motor was actually enabled (0x101 MOTORENB)
    for that feed - the same span the motor_enabled_prolonged anomaly
    rule watches, here attributed to a specific feed instead of flagged
    as an anomaly in isolation.

    A single physical feed's 24-bit FEEDL is written one byte at a time,
    so ``_hex24`` sees several distinct intermediate values (e.g. 13057
    then 13128) for what is really one feed - all reconstructing to the
    same motor-enabled window. Keyed by that window (``start_idx, end_idx``)
    and overwritten as later, more-complete byte writes are seen, so only
    the final value survives instead of emitting one overlapping
    duplicate span per intermediate write.
    """
    milestones: dict[tuple[int, int], dict[str, Any]] = {}
    last_feedl: int | None = None

    for idx, ev in enumerate(decoded_events):
        if ev.get("kind") != "reg_write":
            continue
        regs_before = _registers_at(decoded_events, idx - 1)
        for pair in ev.get("fields", {}).get("pairs", []):
            addr, val = pair["addr"], pair["value"]
            if addr not in REG_FEEDL_ADDRS:
                continue
            merged = dict(regs_before)
            merged[addr] = val
            feedl = _hex24(merged, 0x3D)
            # feedl == 1 is the documented "no additional feed" convention
            # (see gl128.py); values under _MIN_POSITIONING_FEEDL are small
            # incidental resets seen during image acquisition, not a real
            # positioning move - both would otherwise pair with an unrelated
            # motor-enabled span and report a bogus duration.
            if feedl is None or feedl == 1 or feedl < _MIN_POSITIONING_FEEDL or feedl == last_feedl:
                continue
            last_feedl = feedl

            table = None
            for j in range(idx + 1, min(len(decoded_events), idx + 60)):
                e2 = decoded_events[j]
                if e2.get("kind") == "buffer_preamble" and e2.get("fields", {}).get("bulk_addr") in AHB_SLOPE_ADDRS:
                    table = _slope_table_for_upload(decoded_events, raw_events, j)
                    break

            # Bounded so a feed with no plausible motor-enabled window nearby
            # (e.g. a per-line feed during image acquisition, not a real
            # positioning move) can't pair with an unrelated, much-later
            # motor-enabled span and report a bogus multi-second duration.
            search_end = min(len(decoded_events), idx + _FEED_SEARCH_WINDOW)
            start_idx = next(
                (
                    j
                    for j in range(idx + 1, search_end)
                    if decoded_events[j].get("kind") == "reg_read"
                    and decoded_events[j].get("fields", {}).get("addr") == "0x101"
                    and _motor_enabled(decoded_events[j].get("fields", {}).get("value"))
                ),
                None,
            )
            if start_idx is None:
                continue
            end_idx = next(
                (
                    j
                    for j in range(start_idx + 1, search_end)
                    if decoded_events[j].get("kind") == "reg_read"
                    and decoded_events[j].get("fields", {}).get("addr") == "0x101"
                    and _motor_enabled(decoded_events[j].get("fields", {}).get("value")) is False
                ),
                None,
            )
            if end_idx is None:
                continue

            t0_start = decoded_events[start_idx].get("raw_t0")
            t0_end = decoded_events[end_idx].get("raw_t0")
            if t0_start is None or t0_end is None:
                continue
            duration_s = t0_end - t0_start
            rel_s = (t0_start - t_first) if t_first is not None else None
            table_label = table or "unknown (no raw payload)"
            milestones[(start_idx, end_idx)] = {
                "index": idx,
                "t0": t0_start,
                "rel_s": rel_s,
                "kind": "feed_timing",
                "label": f"Positioning feed FEEDL={feedl}: {table_label} table, {duration_s:.2f}s",
                "confidence": "STRONG" if table else "LIKELY",
                "evidence": {
                    "feedl": feedl,
                    "slope_table": table,
                    "start_rel_s": rel_s,
                    "end_rel_s": (t0_end - t_first) if t_first is not None else None,
                    "duration_s": round(duration_s, 3),
                },
            }

    return sorted(milestones.values(), key=lambda m: m["index"])


def _classify_preamble(w_index: str, bulk_size: int) -> tuple[str, str]:
    """Returns (label, confidence)."""
    if w_index == "0x8":
        return "IMAGE pass", "PROVEN"
    if w_index == "0x1":
        return "AHB upload (table)", "PROVEN"
    if w_index == "0x0":
        if bulk_size < 20000:
            return f"RAM/calib: AFE probe (small, {bulk_size}B)", "LIKELY"
        return f"RAM/calib: shading strip ({bulk_size}B)", "LIKELY"
    return f"unclassified w_index={w_index}", "UNKNOWN"


def build_milestones(
    decoded_events: list[dict[str, Any]], raw_events: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """One milestone per meaningful state change - not every raw event.

    Each milestone: {index, t0, rel_s, kind, label, confidence, evidence}.
    ``rel_s`` is seconds since the first event that has a timestamp (t0);
    None for sources without per-event timing (a plain pcap import without
    per-packet timestamps still gets index-ordered milestones).

    ``raw_events`` (usb_raw.jsonl, same index/order as ``decoded_events`` -
    see forensic_event_inspector.py) is optional and only needed to
    identify which slope table (FAST/SLOW/CUSTOM) a positioning feed used;
    without it, feed-timing milestones still appear but with an "unknown"
    table (e.g. for an imported Wireshark run with no raw payloads).
    """
    t_first = next((e.get("raw_t0") for e in decoded_events if e.get("raw_t0") is not None), None)
    milestones: list[dict[str, Any]] = []

    last_lamp: int | None = None
    last_feedl: int | None = None
    last_lperiod: int | None = None
    last_exposure: int | None = None
    last_pixel_clock: dict[str, int] = {}

    for idx, ev in enumerate(decoded_events):
        t0 = ev.get("raw_t0")
        rel_s = (t0 - t_first) if (t0 is not None and t_first is not None) else None
        kind = ev.get("kind")
        fields = ev.get("fields", {})

        if kind == "buffer_preamble":
            label, confidence = _classify_preamble(
                fields.get("w_index", "?"), int(fields.get("bulk_size") or 0)
            )
            milestones.append(
                {
                    "index": idx,
                    "t0": t0,
                    "rel_s": rel_s,
                    "kind": "preamble",
                    "label": label,
                    "confidence": confidence,
                    "evidence": fields,
                }
            )
        elif kind == "reg_write":
            regs_before = _registers_at(decoded_events, idx - 1)
            for pair in fields.get("pairs", []):
                addr, val = pair["addr"], pair["value"]
                if addr in REG_FEEDL_ADDRS:
                    merged = dict(regs_before)
                    merged[addr] = val
                    feedl = _hex24(merged, 0x3D)
                    if feedl is not None and feedl != last_feedl and feedl != 1:
                        last_feedl = feedl
                        milestones.append(
                            {
                                "index": idx,
                                "t0": t0,
                                "rel_s": rel_s,
                                "kind": "feedl_write",
                                "label": f"Positioning feed: FEEDL={feedl}",
                                "confidence": "STRONG",
                                "evidence": {"feedl": feedl},
                            }
                        )
                    elif feedl == 1:
                        last_feedl = 1
                if addr == REG_LAMP and val != last_lamp:
                    last_lamp = val
                    lamp_on = bool(val & LAMPPWR)
                    milestones.append(
                        {
                            "index": idx,
                            "t0": t0,
                            "rel_s": rel_s,
                            "kind": "lamp",
                            "label": f"Lamp {'ON' if lamp_on else 'OFF'} (0x03={hex(val)})",
                            "confidence": "STRONG",
                            "evidence": {"value": hex(val)},
                        }
                    )
                if addr in REG_LPERIOD_ADDRS:
                    merged = dict(regs_before)
                    merged[addr] = val
                    lperiod = _hex24(merged, _R.REG_LPERIOD)
                    if lperiod is not None and lperiod != last_lperiod:
                        last_lperiod = lperiod
                        meaning = explain_register(hex(_R.REG_LPERIOD), lperiod)
                        milestones.append(
                            {
                                "index": idx,
                                "t0": t0,
                                "rel_s": rel_s,
                                "kind": "lperiod",
                                "label": f"LPERIOD={lperiod}" + (f" ({meaning})" if meaning else ""),
                                "confidence": "STRONG",
                                "evidence": {"lperiod": lperiod},
                            }
                        )
                if addr in REG_EXPOSURE_ADDRS:
                    merged = dict(regs_before)
                    merged[addr] = val
                    exposure = _hex24(merged, _R.REG_EXPOSURE)
                    if exposure is not None and exposure != last_exposure:
                        last_exposure = exposure
                        meaning = explain_register(hex(_R.REG_EXPOSURE), exposure)
                        milestones.append(
                            {
                                "index": idx,
                                "t0": t0,
                                "rel_s": rel_s,
                                "kind": "exposure",
                                "label": f"EXPOSURE={exposure}" + (f" ({meaning})" if meaning else ""),
                                "confidence": "STRONG",
                                "evidence": {"exposure": exposure},
                            }
                        )
                if addr in PIXEL_CLOCK_ADDRS and last_pixel_clock.get(addr) != val:
                    last_pixel_clock[addr] = val
                    which = "clk_a" if addr == hex(0xA5) else "clk_b"
                    meaning = explain_register(addr, val)
                    milestones.append(
                        {
                            "index": idx,
                            "t0": t0,
                            "rel_s": rel_s,
                            "kind": "pixel_clock",
                            "label": f"Pixel clock {which} ({addr})={val}" + (f" ({meaning})" if meaning else ""),
                            "confidence": "STRONG",
                            "evidence": {which: val},
                        }
                    )

    milestones.extend(_build_feed_timing_milestones(decoded_events, raw_events or [], t_first))
    milestones.sort(key=lambda m: (m["index"], m["kind"]))
    return milestones


def build_milestones_for_run(run_dir: Path) -> list[dict[str, Any]]:
    return build_milestones(load_decoded_events(run_dir), _read_jsonl(run_dir / "usb_raw.jsonl"))


def _collapse_repeats(milestones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse consecutive milestones sharing (kind, label) into one summary
    row with a count and elapsed span - a single high-DPI image pass is
    otherwise one row per bulk chunk (hundreds to thousands). Never called
    on the raw data used elsewhere (diff, export) - display-only."""
    collapsed: list[dict[str, Any]] = []
    i = 0
    n = len(milestones)
    while i < n:
        j = i
        while j + 1 < n and milestones[j + 1]["kind"] == milestones[i]["kind"] and milestones[j + 1]["label"] == milestones[i]["label"]:
            j += 1
        if j > i:
            first, last = milestones[i], milestones[j]
            span = None
            if first["rel_s"] is not None and last["rel_s"] is not None:
                span = last["rel_s"] - first["rel_s"]
            collapsed.append(
                {
                    "index": f"{first['index']}-{last['index']}",
                    "rel_s": first["rel_s"],
                    "kind": first["kind"],
                    "label": f"{first['label']}  (×{j - i + 1}, span {span:.3f}s)" if span is not None else f"{first['label']} (×{j - i + 1})",
                    "confidence": first["confidence"],
                }
            )
        else:
            collapsed.append(milestones[i])
        i = j + 1
    return collapsed


def format_milestones(
    milestones: list[dict[str, Any]],
    *,
    phase_markers: list[dict[str, Any]] | None = None,
    collapse_repeats: bool = True,
) -> str:
    """Human-readable table, plus host-side phase durations if provided.

    ``collapse_repeats``: fold runs of identical (kind, label) milestones
    (e.g. hundreds of "IMAGE pass" bulk-chunk preambles) into one summary
    row with a count and time span - purely a display transform, the
    underlying milestone list (and decoded_events.jsonl) is untouched.
    """
    display = _collapse_repeats(milestones) if collapse_repeats else milestones
    lines = ["| idx | timecode | kind | label | confidence |", "|---|---|---|---|---|"]
    for m in display:
        rel = format_timecode(m["rel_s"])
        lines.append(f"| {m['index']} | {rel} | {m['kind']} | {m['label']} | {m['confidence']} |")
    if not milestones:
        lines.append("| - | - | (none found) | - | - |")

    if phase_markers:
        lines.append("")
        lines.append(
            "## Host-side phase/button markers (PROVEN - software's own record of what it did or "
            "what was clicked, not an inference from the wire)"
        )
        lines.append("| phase | timecode | duration_s | details |")
        lines.append("|---|---|---|---|")
        for i, marker in enumerate(phase_markers):
            dur = None
            if i + 1 < len(phase_markers):
                dur = phase_markers[i + 1]["t"] - marker["t"]
            dur_s = f"{dur:.3f}" if dur is not None else "(ongoing/last)"
            details = marker.get("details")
            details_s = json.dumps(details) if details else ""
            lines.append(
                f"| {marker['label']} | {format_timecode(marker['rel_s'])} | {dur_s} | {details_s} |"
            )
    return "\n".join(lines)


def derive_states(decoded_events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Motor/lamp on-off transitions for the timeline's state lanes - same
    bits as the feed-timing/lamp milestones above (0x101 MOTORENB, 0x03
    LAMPPWR), exposed as a plain per-name transition list instead of a
    milestone entry."""
    t_first = next((e.get("raw_t0") for e in decoded_events if e.get("raw_t0") is not None), None)
    motor: list[dict[str, Any]] = []
    lamp: list[dict[str, Any]] = []
    last_motor: bool | None = None
    last_lamp_on: bool | None = None

    for ev in decoded_events:
        t0 = ev.get("raw_t0")
        if t0 is None or t_first is None:
            continue
        rel_s = t0 - t_first
        if ev.get("kind") == "reg_read" and ev.get("fields", {}).get("addr") == "0x101":
            val = _motor_enabled(ev.get("fields", {}).get("value"))
            if val is not None and val != last_motor:
                last_motor = val
                motor.append({"rel_s": rel_s, "value": val})
        elif ev.get("kind") == "reg_write":
            for pair in ev.get("fields", {}).get("pairs", []):
                if pair["addr"] == REG_LAMP:
                    lamp_on = bool(pair["value"] & LAMPPWR)
                    if lamp_on != last_lamp_on:
                        last_lamp_on = lamp_on
                        lamp.append({"rel_s": rel_s, "value": lamp_on})

    return {"Motor": motor, "Lamp": lamp}


def collect_known_values(milestones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Distinct known register/table values seen this run, each with its
    first-seen timecode - the "don't make me scrub the timeline to find
    this" summary. One row per distinct label within a kind (a value that
    changes mid-run, e.g. two different feeds' slope tables, gets one row
    each)."""
    kinds = {"feed_timing", "lperiod", "exposure", "pixel_clock"}
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for m in milestones:
        if m["kind"] not in kinds:
            continue
        key = (m["kind"], m["label"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": m["kind"], "label": m["label"], "rel_s": m.get("rel_s")})
    return out


def collect_unknown_registers(decoded_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Distinct register addresses touched this run that
    ``explain_register()`` has nothing to say about - an honest "we don't
    know" list (matching that function's own convention) instead of
    silently dropping unexplained traffic. One row per distinct address,
    first-seen value and timecode."""
    t_first = next((e.get("raw_t0") for e in decoded_events if e.get("raw_t0") is not None), None)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for ev in decoded_events:
        t0 = ev.get("raw_t0")
        rel_s = (t0 - t_first) if (t0 is not None and t_first is not None) else None
        kind = ev.get("kind")
        fields = ev.get("fields", {})
        pairs = []
        if kind == "reg_write":
            pairs = [(p["addr"], p["value"]) for p in fields.get("pairs", [])]
        elif kind == "reg_read" and fields.get("addr"):
            pairs = [(fields["addr"], fields.get("value"))]
        for addr, value in pairs:
            if addr in seen:
                continue
            if explain_register(addr, value) is not None:
                continue
            seen.add(addr)
            out.append({"addr": addr, "value": value, "rel_s": rel_s})
    return out
