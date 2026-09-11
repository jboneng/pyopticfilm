# SPDX-License-Identifier: GPL-3.0-or-later
"""Canonical register / bit-flag reference catalog for pyopticfilm's
supported scanners.

This is the single source of truth for "what does register/bit X mean, on
which scanner, how sure are we, and what's the evidence" — consolidating
knowledge that used to live independently in ``tools/scanlab/
forensic_reference.py`` (a 5-register list), ``tools/scanlab/
forensic_milestones.py``, ``tools/scanlab/forensic_anomaly.py``, and
``tools/phase_segment.py``. Those modules now import from here instead of
hand-maintaining their own copies (see each module's own docstring).

Repo-only (``tools/``, not shipped on PyPI) by design: this catalogs
debug/bring-up knowledge about the wire protocol, not runtime driver
behavior — the actual runtime source of truth remains
``src/pyopticfilm/asic/registers.py``, ``gl128.py``, and
``device/model_*.py``. This module documents and cites those, it does not
replace them.

Confidence scale
-----------------
Four levels, chosen to distinguish "assumed from the sibling model" from
"actively uncertain" — a distinction the confidence wording used elsewhere
in this project doesn't always make explicit:

- ``CONFIRMED``  — capture-cited, or already described as capture-proven by
  a ``registers.py``/``device/model_*.py`` docstring.
- ``INHERITED``  — value/meaning assumed from the sibling model or ASIC
  family, not independently confirmed for this specific model.
- ``SUSPECTED``  — real evidence exists but is incomplete or contradictory.
- ``UNKNOWN``    — present in traffic/code; meaning genuinely not understood.

Mapping from other confidence scales used elsewhere in this project, for
anyone cross-referencing:

- ``tools/scanlab/forensic_reference.py``'s PROVEN/STRONG -> ``CONFIRMED``;
  LIKELY -> ``SUSPECTED``; SPECULATIVE -> ``SUSPECTED`` or ``UNKNOWN``
  depending on citation strength.
- ``docs/hw-ref/8100v2/findings.md``'s "confirmed" -> ``CONFIRMED``;
  "contradicted"/"unverified"/"new" -> ``SUSPECTED`` (citations from both
  sides of a contradiction are kept, not just one).
- ``docs/hw-ref/8100v2/claims-inventory.md`` entries inherited from SE
  tables without independent V2 confirmation -> ``INHERITED``.

``safety_note`` is deliberately independent of ``confidence`` — a
``CONFIRMED`` register (FEEDL, the 0x21 feed-probe) can still be
hardware-dangerous; that's a load-bearing safety fact, not an epistemic
one, and must survive even if the confidence rating later changes.

Citations are free prose (``Citation.text``), not structured/validated
file+line objects or paths to capture files — the catalog must never
depend on the actual multi-GB pcap files being present, and shouldn't
break if a doc gets renamed or a capture file isn't shipped.

Extending to a future scanner
------------------------------
- Same ASIC family, new model: add its ``scope`` string (see
  ``SCOPE_8100_V2``/``SCOPE_8200I_SE`` for the pattern) — no enum change.
- New ASIC family: add one ``AsicFamily`` member plus a new
  ``SCOPE_ALL_<FAMILY>`` shared-scope marker, following the GL128/GL845
  pattern below.
- New entries for an unproven model start ``INHERITED`` or ``UNKNOWN``,
  upgraded to ``CONFIRMED`` once real captures exist for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE


class Confidence(str, Enum):
    CONFIRMED = "confirmed"
    INHERITED = "inherited"
    SUSPECTED = "suspected"
    UNKNOWN = "unknown"


class AsicFamily(str, Enum):
    GL128 = "gl128"  # 8100 V2, 8200i SE
    GL845 = "gl845"  # legacy/other models
    # Future families: add a new member here, never repurpose an existing one.


#: Shared-scope markers — "this applies to every model on the family, not
#: just one leaf". Per-model scope strings are each model's own ``.name``
#: identity (see SCOPE_8100_V2/SCOPE_8200I_SE), not invented slugs, so a
#: catalog scope value can always be traced back to a real model object.
SCOPE_ALL_GL128 = "gl128-shared"
SCOPE_ALL_GL845 = "gl845-shared"
SCOPE_8100_V2 = MODEL_8100_V2.name
SCOPE_8200I_SE = MODEL_8200I_SE.name

#: Every scope string this catalog is allowed to use — the shared markers
#: plus every real model's own identity. A test asserts every entry's scope
#: is a subset of this, so a typo'd scope string fails immediately.
KNOWN_SCOPES: frozenset[str] = frozenset(
    {SCOPE_ALL_GL128, SCOPE_ALL_GL845, SCOPE_8100_V2, SCOPE_8200I_SE}
)


@dataclass(frozen=True, slots=True)
class Citation:
    """Free-form evidence pointer: ``"model_8100_v2.py:23-30"``,
    ``"04_color_7200.pcapng frame 2999"``, ``"PR #40"``,
    ``"gl128.py:1707 (home() refusal)"``. Deliberately not validated or
    resolved against the filesystem — the catalog must not depend on
    capture files being present."""

    text: str


def _c(*texts: str) -> tuple[Citation, ...]:
    return tuple(Citation(t) for t in texts)


@dataclass(frozen=True, slots=True)
class BitEntry:
    """One bit/flag within a register's value."""

    mask: str  # "0x10", "0x20" — hex, matches _addr_matches's single-token form
    name: str
    meaning: str
    confidence: Confidence
    citations: tuple[Citation, ...] = ()


@dataclass(frozen=True, slots=True)
class RegisterEntry:
    """One catalog row: an address (or address range/discrete set) and what
    is known about it, for one model or a whole shared scope."""

    addr: str  # "0x03", "0x3D-0x3F" (range), "0x51/0x5D/0x5E" (discrete) —
    # exact syntax handled by _addr_matches, reused verbatim from the
    # original tools/scanlab/forensic_reference.py implementation.
    name: str
    asic: AsicFamily
    scope: tuple[str, ...]
    meaning: str
    confidence: Confidence
    citations: tuple[Citation, ...] = ()
    safety_note: str | None = None
    bits: tuple[BitEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class BehavioralNote:
    """A catalog entry for something that isn't a register address at all
    (a documented behavior, an inherited-but-seemingly-unused field, a USB
    protocol-level constant) but still needs a confidence/citation/safety
    record alongside the register catalog."""

    topic: str
    asic: AsicFamily
    scope: tuple[str, ...]
    meaning: str
    confidence: Confidence
    citations: tuple[Citation, ...] = ()
    safety_note: str | None = None


def parse_addr(addr: object) -> int | None:
    """A decoded event's ``addr``/``w_index`` field, normalized to ``int``.
    Returns ``None`` for anything unparseable (an honest "not an address")
    instead of raising - the single source for a hex-string-or-int
    normalization previously duplicated in forensic_tab.py,
    forensic_anomaly.py, and forensic_reference.py."""
    try:
        return int(addr, 16) if isinstance(addr, str) else int(addr)
    except (TypeError, ValueError):
        return None


def _addr_matches(spec: str, target: int) -> bool:
    """``spec`` is one of ``"0x03"``, ``"0x3D-0x3F"`` (range), or
    ``"0x51/0x5D/0x5E"`` (discrete addresses) — matches ``target`` against
    any of them. Moved here verbatim from the original
    ``tools/scanlab/forensic_reference.py`` implementation; that module now
    re-exports this one instead of keeping its own copy."""
    for token in spec.split("/"):
        token = token.strip()
        if "-" in token:
            lo_s, hi_s = token.split("-", 1)
            try:
                lo, hi = int(lo_s, 16), int(hi_s, 16)
            except ValueError:
                continue
            if lo <= target <= hi:
                return True
        else:
            try:
                if int(token, 16) == target:
                    return True
            except ValueError:
                continue
    return False


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------

REGISTERS: tuple[RegisterEntry, ...] = (
    # -- GL845 (legacy family; Gl845Registers in src/pyopticfilm/asic/registers.py) --
    RegisterEntry(
        addr="0x01",
        name="Scan control",
        asic=AsicFamily.GL845,
        scope=(SCOPE_ALL_GL845,),
        meaning="SCAN=0x01, SHDAREA=0x02, DVDSET=0x20.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl845Registers (ported from SANE genesys gl846_registers.h)"),
    ),
    RegisterEntry(
        addr="0x02",
        name="Motor control",
        asic=AsicFamily.GL845,
        scope=(SCOPE_ALL_GL845,),
        meaning=(
            "NOTHOME=0x80, ACDCDIS=0x40, AGOHOME=0x20, MTRPWR=0x10, "
            "FASTFED=0x08, MTRREV=0x04, HOMENEG=0x02."
        ),
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl845Registers"),
    ),
    RegisterEntry(
        addr="0x03",
        name="Lamp",
        asic=AsicFamily.GL845,
        scope=(SCOPE_ALL_GL845,),
        meaning="LAMPPWR=0x10, XPASEL=0x20.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl845Registers"),
    ),
    RegisterEntry(
        addr="0x04",
        name="Frontend select",
        asic=AsicFamily.GL845,
        scope=(SCOPE_ALL_GL845,),
        meaning="FESET=0x03, FESET_ADI=0x02.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl845Registers"),
    ),
    RegisterEntry(
        addr="0x05/0x0E/0x0F/0x6C",
        name="Present in Gl845Registers, no documented bit-level meaning here",
        asic=AsicFamily.GL845,
        scope=(SCOPE_ALL_GL845,),
        meaning="Address exists in the dataclass but this repo carries no bit/behavior documentation for it beyond the bare address.",
        confidence=Confidence.UNKNOWN,
        citations=_c("registers.py Gl845Registers"),
    ),
    RegisterEntry(
        addr="0x06",
        name="Power bit",
        asic=AsicFamily.GL845,
        scope=(SCOPE_ALL_GL845,),
        meaning="PWRBIT=0x10.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl845Registers"),
    ),
    RegisterEntry(
        addr="0x40",
        name="Version check",
        asic=AsicFamily.GL845,
        scope=(SCOPE_ALL_GL845,),
        meaning="CHKVER=0x10.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl845Registers"),
    ),
    RegisterEntry(
        addr="0x41",
        name="Status (scanner_read_status)",
        asic=AsicFamily.GL845,
        scope=(SCOPE_ALL_GL845,),
        meaning="8-bit status; see bits below. GL128's REG_STATUS (0x101) reuses this exact bit layout at a high address.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl845Registers", "asic/status.py ScannerStatus.from_reg41"),
        bits=(
            BitEntry("0x80", "PWRBIT", "Inverted: clear = replugged/cold since last check.", Confidence.CONFIRMED),
            BitEntry("0x40", "BUFEMPTY", "1 = image/scan buffer empty.", Confidence.CONFIRMED),
            BitEntry("0x20", "FEEDFSH", "1 = feed operation finished.", Confidence.CONFIRMED),
            BitEntry("0x10", "SCANFSH", "1 = scan operation finished.", Confidence.CONFIRMED),
            BitEntry("0x08", "HOMESNR", "1 = carriage is at the home sensor.", Confidence.CONFIRMED),
            BitEntry("0x04", "LAMPSTS", "1 = lamp on.", Confidence.CONFIRMED),
            BitEntry("0x02", "FEBUSY", "1 = analog front-end (AFE) busy.", Confidence.CONFIRMED),
            BitEntry("0x01", "MOTORENB", "1 = motor enabled/moving.", Confidence.CONFIRMED),
        ),
    ),
    RegisterEntry(
        addr="0xA8",
        name="IR lamp GPIO (8200i, non-SE)",
        asic=AsicFamily.GL845,
        scope=(SCOPE_ALL_GL845,),
        meaning="IR_LAMP_A8_MASK=0x04 — IR lamp GPIO bit, ported from SANE genesys command_set_common.cpp. Not used by the GL128 8200i SE (a different, GL128-family model despite the similar name).",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl845Registers"),
    ),
    # -- GL128 (8100 V2, 8200i SE — Gl128Registers) --
    RegisterEntry(
        addr="0x01",
        name="Scan control",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="SCAN=0x01, SHDAREA=0x02, STAGGER=0x10, DVDSET=0x20.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers (confirmed from SE USB captures; also used by 8100 V2)"),
    ),
    RegisterEntry(
        addr="0x02",
        name="Motor control",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="AGOHOME=0x20, MTRPWR=0x10, FASTFED=0x08, MTRREV=0x04.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers"),
        safety_note=(
            "Positioning-feed motor-ramp selection (which slope table gets "
            "uploaded before AGOHOME/FASTFED motion) previously caused a real "
            "mechanical fault on 8100 V2 hardware — see the 0x3D-0x3F FEEDL "
            "entry's safety_note for the full incident and fix."
        ),
    ),
    RegisterEntry(
        addr="0x03",
        name="Lamp",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning=(
            "LAMPPWR=0x10 (white lamp), XPASEL=0x20 (held for every "
            "transparency operation), AVEENB=0x40 (held during lamp-on). IR "
            "passes clear LAMPPWR and keep XPASEL."
        ),
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers", "gl128.py lamp_on/lamp_off/_strike_lamp_on"),
    ),
    RegisterEntry(
        addr="0x0D",
        name="REG_CLRCNT",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="Write 0x07 (CLRCNT_ALL) to clear line/motor/feed counters.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers"),
    ),
    RegisterEntry(
        addr="0x0F",
        name="REG_START",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="Write 0x01 (START_GO) to launch the configured operation.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers"),
    ),
    RegisterEntry(
        addr="0x21",
        name="Feed-probe index (vendor REQUEST_REGISTER probe, not an ASIC register address)",
        asic=AsicFamily.GL128,
        scope=(SCOPE_8100_V2,),
        meaning=(
            "pyopticfilm's own _FEED_PROBE_INDEX, polled during fast feeds "
            "until it returns 0x04 (_FEED_PROBE_DONE). On the 8100 V2, the "
            "real vendor driver never queries wIndex=0x21 in any capture "
            "with a real scan — it polls wIndex=0x20 (constant 0x55) and "
            "0x18 instead, neither of which pyopticfilm currently uses. The "
            "absence of 0x21 traffic in every real-scan capture is the "
            "confirmed observation; what the vendor driver's 0x20/0x18 "
            "probes actually mean was deliberately not guessed at."
        ),
        confidence=Confidence.CONFIRMED,
        citations=_c(
            "gl128.py:117 (_FEED_PROBE_INDEX=0x21), :1520-1531 (_read_feed_probe/_feed_done_indicated)",
            "Independent capture analysis (multiple real 8100 V2 USB captures with active scans): "
            "wIndex=0x21 never appears; wIndex=0x20 (const 0x55) dominates, wIndex=0x18 (2 or 18) less often",
        ),
        safety_note=(
            "A per-model 'skip this probe entirely' override for V2 was "
            "tried and independently confirmed hazardous: disabling it "
            "caused real hardware faults on repeated trials, even after an "
            "unrelated motor-ramp bug (see 0x3D-0x3F's safety_note) was "
            "fixed. Do not reintroduce a way to disable the 0x21 probe on "
            "V2 without new fault-capture evidence establishing it's safe — "
            "this repo intentionally has no such override today."
        ),
    ),
    RegisterEntry(
        addr="0x21",
        name="Feed-probe index — meaning on 8200i SE",
        asic=AsicFamily.GL128,
        scope=(SCOPE_8200I_SE,),
        meaning="Whether the real vendor driver uses wIndex=0x21 on the SE (as opposed to the V2, where it's confirmed unused) has not been independently checked.",
        confidence=Confidence.UNKNOWN,
        citations=_c("gl128.py:117 (_FEED_PROBE_INDEX=0x21) — SE-specific verification not done"),
    ),
    RegisterEntry(
        addr="0x25-0x27",
        name="REG_LINCNT",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="24-bit BE line count, native 7200 dpi units.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers"),
    ),
    RegisterEntry(
        addr="0x25-0x27",
        name="REG_LINCNT — V2 max-travel regression fixture",
        asic=AsicFamily.GL128,
        scope=(SCOPE_8100_V2,),
        meaning="max_image_lincnt_by_feed2={13128: 29012} — captured full-frame 7200 dpi LINCNT at second-feed distance 13128, kept as a regression fixture (not a general formula).",
        confidence=Confidence.CONFIRMED,
        citations=_c("device/model_8100_v2.py module docstring, item 4 — capture evidence: 04_color_7200.pcapng frame 3203, registers 0x25-0x27 = 0x00 0x71 0x54 = 29012"),
    ),
    RegisterEntry(
        addr="0x28-0x2A",
        name="REG_LPERIOD",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="24-bit BE line exposure period; per-DPI table (LPERIOD_BY_DPI in device/gl128_common.py).",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers", "device/gl128_common.py LPERIOD_BY_DPI (session 13_ppi_ladder)"),
    ),
    RegisterEntry(
        addr="0x28-0x2A",
        name="REG_LPERIOD — V2 7200dpi shipped value",
        asic=AsicFamily.GL128,
        scope=(SCOPE_8100_V2,),
        meaning="Shipped override: 16035 (vs the shared table's 15963), from a single capture session. See the SUSPECTED entry immediately below — a later, independent pair of captures disagrees with this value.",
        confidence=Confidence.CONFIRMED,
        citations=_c("device/model_8100_v2.py module docstring, item 2 — capture evidence: frames 1661/2257/3203, register 0x28-0x2A = 0x003EA3 = 16035"),
    ),
    RegisterEntry(
        addr="0x28-0x2A",
        name="REG_LPERIOD — V2 7200dpi discrepancy (resolved 2026-09-06)",
        asic=AsicFamily.GL128,
        scope=(SCOPE_8100_V2,),
        meaning=(
            "Previously SUSPECTED: two independent V2 captures showed "
            "LPERIOD=15914 and LPERIOD=15999 at 7200 dpi, disagreeing with "
            "each other by 85 and both disagreeing with the shipped "
            "constant of 16035 (see the CONFIRMED entry above). Resolved "
            "by a third, independent capture set (2026-09-05, 7 sessions, "
            "vendor SilverFast driver): LPERIOD=16035 reproduced identically "
            "at three separate occurrences — a standalone full-frame 7200dpi "
            "scan, and both brackets of a multi-exposure 7200dpi scan — each "
            "independently cross-checked against the raw USB register write "
            "by hand. The disputing 15914/15999 readings do not reproduce "
            "under this fresh, disinterested capture set. Kept as SUSPECTED "
            "history rather than deleted, per this catalog's convention of "
            "keeping both sides of a resolved contradiction."
        ),
        confidence=Confidence.CONFIRMED,
        citations=_c(
            "Prior dispute: independent capture analysis, register-program-"
            "by-dpi extraction (two separate capture files, LPERIOD=15914 "
            "and 15999)",
            "Resolution: TobbyTravel/pyopticfilm_captures branch "
            "add-8100-v2-captures, 04_color_7200.pcapng frames 1673/3219 and "
            "07_multi_exposure.pcapng frames 17745/36509, register 0x28-0x2A "
            "= 0x003EA3 = 16035 at every occurrence",
        ),
    ),
    RegisterEntry(
        addr="0x2B",
        name="Shading-strip dummy clock",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="Dummy-clock byte for the dark/white shading strips; per-DPI table (DUMMY_BY_DPI, SHADING_DARK_DUMMY_BY_DPI in device/gl128_common.py).",
        confidence=Confidence.CONFIRMED,
        citations=_c("device/gl128_common.py DUMMY_BY_DPI / SHADING_DARK_DUMMY_BY_DPI"),
    ),
    RegisterEntry(
        addr="0x2B",
        name="Shading-strip dummy clock — V2 7200dpi white-strip override",
        asic=AsicFamily.GL128,
        scope=(SCOPE_8100_V2,),
        meaning="V2's white shading strip at 7200dpi uses dummy=0x10, vs the SE-derived table's computed 0x17. Dark-strip dummy is 0x17 on both, confirmed on V2 too.",
        confidence=Confidence.CONFIRMED,
        citations=_c("device/model_8100_v2.py module docstring, item 3 — capture evidence: frame 2257 (white), frame 1661 (dark)"),
    ),
    RegisterEntry(
        addr="0x2C-0x2D",
        name="REG_DPISET",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="16-bit BE, equals dpi/6 at 600dpi and above; below 600 the ASIC is programmed like 600 (floors at 100).",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers", "device/gl128_common.py REGISTER_DPISET (session 13_ppi_ladder)"),
    ),
    RegisterEntry(
        addr="0x33",
        name="REG_DEPTH_A",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="0x04 = 16-bit output, 0x1F = 8-bit — paired with REG_DEPTH_B (0xAF).",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers"),
    ),
    RegisterEntry(
        addr="0x33/0xAF",
        name="REG_DEPTH_A/REG_DEPTH_B — V2 real-driver pairing (corrected 2026-09-06)",
        asic=AsicFamily.GL128,
        scope=(SCOPE_8100_V2,),
        meaning=(
            "Previously SUSPECTED as a mismatch: an earlier capture analysis "
            "claimed the real vendor driver's V2 image pass writes "
            "DEPTH_A=0x04/DEPTH_B=0xFF, a pairing matching neither of "
            "pyopticfilm's own programmed pairs. A fresh, independent "
            "capture set (2026-09-05) contradicts that claim: the real "
            "driver's V2 image pass writes DEPTH_A=0x1F/DEPTH_B=0xFF — "
            "exactly pyopticfilm's own DEPTH8_A/DEPTH8_B pair, not a "
            "mismatch. The shading pass separately writes DEPTH_A=0x04/"
            "DEPTH_B=0x46 — exactly pyopticfilm's own DEPTH16_A/DEPTH16_B "
            "pair. Both pairings match cleanly; there is no pairing "
            "mismatch on V2. (pyopticfilm intentionally scans 16-bit "
            "regardless of what the vendor driver chooses for its own "
            "image pass — that design choice is unaffected by this.)"
        ),
        confidence=Confidence.CONFIRMED,
        citations=_c(
            "Prior claim (contradicted): independent capture analysis, "
            "trace-vs-capture register comparison",
            "Resolution: TobbyTravel/pyopticfilm_captures branch "
            "add-8100-v2-captures, 04_color_7200.pcapng frames 2755/3015 "
            "(image pass, DEPTH_A=0x1F/DEPTH_B=0xFF) and shading-pass "
            "snapshot (DEPTH_A=0x04/DEPTH_B=0x46), each cross-checked "
            "against the raw USB register write by hand",
        ),
    ),
    RegisterEntry(
        addr="0x1D",
        name="Unnamed, constant in SCAN_REGS (0x80) except during shading at 3600/7200dpi and the image pass at 7200dpi",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning=(
            "Not in Gl128Registers or any per-DPI table; pyopticfilm always "
            "writes 0x80 (SCAN_REGS) regardless of DPI.\n\n"
            "CORRECTED 2026-09-11: this entry previously claimed the flip "
            "happens 'only for the 7200dpi image pass ... never during "
            "shading'. An independent re-decode (tools/capture_ledger.py, "
            "which preserves per-write pass identity via the DEPTH_A/DEPTH_B "
            "registers alongside 0x1D, rather than a bare per-DPI value diff) "
            "contradicts that: the real driver's actual pattern is the "
            "opposite emphasis — it flips MORE during shading than during "
            "the image pass. At DPI <=2400 (DPISET <=400) 0x1D stays 0x80 "
            "throughout, confirmed unchanged. At 3600dpi (DPISET=600) it "
            "reads 0x81 during BOTH the dark and white shading passes "
            "(DEPTH_A=0x04) and reverts to 0x80 for the image pass "
            "(DEPTH_A=0x1F). At 7200dpi (DPISET=1200) it reads 0x82 during "
            "both shading passes and 0x81 during the image pass only — "
            "independently reproduced on the 8100 V2 (04_color_7200) and "
            "the 8200i SE (13_7200ppi_color, across both back-to-back scans "
            "in that single file). Every session's write traffic for every "
            "GL128 address (all 37 pyopticfilm_captures sessions across both "
            "models, including the ME/IR/crop sessions this catalog "
            "previously flagged as unchecked) was re-swept for this pass; "
            "0x1D's DPI/pass dependence above is the only variation found "
            "for this address. Meaning still unknown. A real-hardware test that "
            "set this bit at 7200dpi (an experimental_reg1d_7200 opt-in flag, "
            "since removed) was run on the 8100 V2: the baseline (bit clear, "
            "today's shipped behavior) completed a full-frame 7200dpi scan "
            "cleanly; the immediately following scan with the bit set failed "
            "with a USB pipe error partway through the image transfer, and "
            "the operator reported a motor jam and cut power to the scanner. "
            "Whether the bit itself caused the jam, versus some other factor "
            "in that specific run, was not further isolated before hardware "
            "was powered off — but this is now the working assumption and "
            "this register must be treated accordingly.\n\n"
            "Same-ASIC-family corroboration (mainline SANE genesys, GL124 — "
            "the family GL128 is itself described as descending from): "
            "``backend/genesys/gl124_registers.h`` defines this exact address "
            "as REG_0x1D with CK4LOW=0x80, CK3LOW=0x40, CK1LOW=0x20, and a "
            "5-bit LINESEL field at bits 0-4 (0x1f). Our corrected "
            "observation reads cleanly as CK4LOW-held with LINESEL=0 "
            "(<=2400dpi), LINESEL=1 (3600dpi shading, 7200dpi image pass), "
            "or LINESEL=2 (7200dpi shading) — a monotonic count that scales "
            "with how far above 2400dpi the pass runs, larger during "
            "shading than during the image pass at the same DPI. As a sanity "
            "check that this really is the same register map (not another "
            "coincidental-address false lead — see the 98003/parallel-port "
            "plustek-pp_p12.c dead end this repo deliberately did NOT cite "
            "here): GL124's REG_0x01 bit layout (SCAN/SHDAREA/STAGGER/DVDSET) "
            "is an exact match to every bit this repo has independently "
            "confirmed for GL128's own 0x01 from real captures — real "
            "corroboration, not a guess. In ``gl124.cpp`` "
            "(gl124_init_motor_regs_scan), LINESEL is a motor line-"
            "accumulation count: when the requested output DPI is below the "
            "motor's mechanical minimum speed, the motor runs faster than "
            "the output needs and LINESEL tells the ASIC how many native "
            "lines to accumulate per output line, set inside the *motor* "
            "register block — consistent with 0x1D being motor/line-timing "
            "related, not cosmetic. It does NOT map cleanly onto our "
            "observation, though: that mainline driver only raises LINESEL "
            "for low DPI (motor too slow), while ours changes only at the "
            "top two DPI rungs (3600/7200, near-native resolution) — the "
            "opposite end of the range. Plustek's GL128 firmware and this film scanner's motor/"
            "gear train differ enough from a Canon flatbed CIS that the "
            "field may be reused for an analogous but distinct purpose (e.g. "
            "an internal data-path line-accumulation mode specific to native "
            "resolution) — this is inference from a related but non-identical "
            "product, not a confirmed match for our firmware."
        ),
        confidence=Confidence.SUSPECTED,
        citations=_c(
            "device/gl128_common.py:91 (SCAN_REGS)",
            "Superseded: earlier independent capture register-diff across "
            "pyopticfilm_captures 8100-v2/06_ppi_ladder + 04_color_7200 and "
            "8200i-se/13_ppi_ladder claimed image-pass-only, never-during-"
            "shading — corrected below",
            "Correction (2026-09-11, Claude Code session, tools/"
            "capture_ledger.py re-decode joined against DEPTH_A/DEPTH_B for "
            "pass identity): 8100-v2/06_ppi_ladder packet index 84946/85608 "
            "(3600dpi shading, 0x81) and 86252 (3600dpi image, 0x80); "
            "8100-v2/04_color_7200 packet index 1668/2264 (7200dpi shading, "
            "0x82) and 2836 (7200dpi image, 0x81); 8200i-se/13_7200ppi_color "
            "packet index 2182/2854 and 32208/32868 (7200dpi shading, 0x82, "
            "both scans in file) and 3502/33504 (7200dpi image, 0x81, both "
            "scans); every reg_write address across all 37 "
            "pyopticfilm_captures sessions (both models) re-swept and found "
            "to introduce no other previously-uncatalogued address",
            "2026-09-10 real-hardware A/B test on the 8100 V2 (Claude Code "
            "session): baseline 7200dpi scan succeeded; experimental-bit "
            "7200dpi scan immediately following failed mid-transfer (USB "
            "pipe error / device disconnect) with an operator-reported motor "
            "jam, requiring a hard power-off",
            "Mainline SANE genesys backend (GL124 family), "
            "github.com/enthdegree/sane-backends (mirrors upstream "
            "backend/genesys structure): gl124_registers.h REG_0x1D / "
            "REG_0x1D_LINESEL / REG_0x1D_CK4LOW etc.; gl124.cpp "
            "gl124_init_motor_regs_scan() LINESEL usage",
        ),
        safety_note=(
            "HARDWARE INCIDENT (2026-09-10, unresolved): setting bit 0 of "
            "0x1D for a 7200dpi image pass on real 8100 V2 hardware was "
            "immediately followed by a motor jam requiring a hard power-off "
            "mid-scan. The experimental_reg1d_7200 flag and its CLI/session "
            "plumbing that made this reachable have been removed entirely — "
            "there is intentionally no way to set this bit from pyopticfilm "
            "today. Do not reintroduce any path to setting this bit (or "
            "otherwise touching 0x1D from its SCAN_REGS default) without new "
            "capture or bench evidence establishing it's safe, isolating "
            "this one change from everything else per the 0x3D-0x3F FEEDL "
            "incident's own lesson."
        ),
    ),
    RegisterEntry(
        addr=(
            "0x04-0x0C/0x11-0x1C/0x1E-0x20/0x22-0x24/0x30-0x32/0x34-0x36/"
            "0x38-0x3C/0x4F/0x52-0x57/0x5A-0x5C/0x5F-0x61/0x63/0x67-0x69/"
            "0x70-0x7C/0x80-0x81/0x8A-0x95/0x9D/0xA0/0xA2-0xAE/0xB8-0xBA/"
            "0xBD-0xBF/0x114-0x115"
        ),
        name="Opaque boot/scan register blast — no per-address meaning known",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning=(
            "Every address in this span is written by INIT_REGS (cold-boot "
            "blast) and/or SCAN_REGS (constant overrides applied before every "
            "acquisition) and/or GPO_REGS in device/gl128_common.py, replayed "
            "byte-for-byte from capture with no bit-level interpretation. "
            "This entry exists so the gap is visible in one place rather "
            "than silently absent from the catalog, not because anything "
            "here is suspected of doing anything in particular.\n\n"
            "RE-CHECKED 2026-09-11 against ME/IR/crop variation (previously "
            "only checked across the PPI ladder): a full reg_write sweep of "
            "every pyopticfilm_captures session for both models (37 files, "
            "every GL128 address the real driver ever writes, not just this "
            "span) found nothing outside this catalog's existing address "
            "coverage — no new addresses to add anywhere. Two addresses "
            "inside this span, 0xA5 and 0xAB (paired, always written "
            "together), turned out NOT to be constant during a scan the way "
            "the rest of the span is: both progress boot-value 0x20 -> 0x01 "
            "-> 0x02 through a single scan's shading/image passes on every "
            "model and DPI checked, and in one multi-exposure SE capture "
            "(1800ppi_Scan_No_IR_ME) the second-in-file scan's final value "
            "differs from the first (ends 0x01 instead of the usual 0x02) — "
            "a real, reproducible difference, but the trigger (bracket "
            "position within an ME sequence? something else?) isn't "
            "isolated enough to state a meaning. IR-on-vs-off and crop-top-"
            "vs-bottom produced no observed difference anywhere in this "
            "span, on either model.\n\n"
            "Addresses where the SCAN_REGS value differs from the INIT_REGS "
            "boot value (the more likely candidates to matter, since a "
            "register the driver bothers to *change* before scanning is more "
            "likely load-bearing than one left at its boot default) — "
            "boot → scan: "
            "0x04: 0x02→0x42, 0x05: 0x48→0x40, 0x06: 0x18→0xF0, "
            "0x0B: 0x6C→0x4C, 0x1C: 0x00→0x20, 0x1E: 0x10→0x20, "
            "0x3B: 0xFF→0x01, 0x52: 0x07→0x0B, 0x53: 0x09→0x0D, "
            "0x54: 0x0B→0x0F, 0x56: 0x03→0x05, 0x57: 0x05→0x07, "
            "0x5A: 0x12→0x31, 0x5B: 0x00→0x79, 0x70: 0x01→0x0A, "
            "0x71: 0x02→0x0B, 0x72: 0x03→0x0C, 0x73: 0x04→0x0D, "
            "0x81: 0x22→0x40. "
            "SCAN_REGS-only (no INIT_REGS entry, i.e. left at hardware "
            "power-on default until the first scan): 0x8A-0x92 (all 0x00), "
            "0x114-0x115 (both 0x80). GPO_REGS-only (not in INIT_REGS or "
            "SCAN_REGS): 0xA2-0xA3 (0x00), 0xAC (0x00), 0xAD (0x01), 0xAE "
            "(0x00). Everything else in this span is held at the same "
            "constant value across INIT_REGS and SCAN_REGS — except 0xA5/"
            "0xAB, dynamic during a scan as described above and not "
            "meaningfully summarized by an INIT_REGS/SCAN_REGS pair.\n\n"
            "Not covered here: the FRONTEND_REGS table (AFE sub-indices "
            "0x00-0x07 reached indirectly through REG_FE_INDEX/0x51, a "
            "different address space entirely, not raw ASIC register "
            "addresses) — its docstring already documents indices 0x02-0x04 "
            "as per-channel offsets and 0x05-0x07 as per-channel gains, so "
            "it isn't blind the way this span is."
        ),
        confidence=Confidence.UNKNOWN,
        citations=_c(
            "device/gl128_common.py INIT_REGS, SCAN_REGS, GPO_REGS",
            "Re-check (2026-09-11, Claude Code session): tools/"
            "capture_ledger.py reg_write sweep of all 37 pyopticfilm_captures "
            "sessions (both models) against tools/register_reference's own "
            "_addr_matches — 0xA5/0xAB dynamism found via "
            "8200i-se/04_color_1800 vs 05_ir_1800 (IR, no difference), "
            "09a_crop_top_1800 vs 09b_crop_bottom_1800 (crop, no "
            "difference), and 1800ppi_Scan_No_IR_No_ME vs "
            "1800ppi_Scan_No_IR_ME (ME, 0xA5/0xAB differ)",
        ),
    ),
    RegisterEntry(
        addr="0xD0-0xD2/0xE0-0xF8",
        name="Memory layout block — no per-field meaning known",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning=(
            "MEMORY_LAYOUT_REGS in device/gl128_common.py: 28 bytes, written "
            "before every stationary calib acquire (Gl128._apply_stationary_"
            "scan_regs) and byte-identical at every resolution captured "
            "(sessions 03/04/06 per that table's own comment) — consistent "
            "with fixed ASIC RAM/DMA buffer-layout constants (start/end "
            "addresses or sizes for the shading, motor-slope, and per-"
            "channel-exposure AHB windows) rather than anything scan-"
            "parameter-dependent, but that's inference from the byte-"
            "identical-across-DPI pattern, not a confirmed field breakdown. "
            "No individual field within the block has been decoded."
        ),
        confidence=Confidence.UNKNOWN,
        citations=_c(
            "device/gl128_common.py MEMORY_LAYOUT_REGS",
            "device/model_8200i_se.py module docstring (byte-identical "
            "across resolutions, sessions 03/04/06)",
        ),
    ),
    RegisterEntry(
        addr="0x37",
        name="REG_IR",
        asic=AsicFamily.GL128,
        scope=(SCOPE_8200I_SE,),
        meaning="Bit 2 (IR_LED=0x04) enables the infrared LED, read-modify-write. The 8100 V2 has no infrared channel (supports_infrared=False) — this register is presumably unused/unwritten on V2, not independently verified either way.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers", "gl128.py _apply_infrared (session 05: read 0xB0, write 0xB4)"),
    ),
    RegisterEntry(
        addr="0x3D-0x3F",
        name="REG_FEEDL",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="24-bit BE feed distance for move-only operations. FEEDL=1 is the acquisition convention (no physical feed); any other value is a positioning feed.",
        confidence=Confidence.CONFIRMED,
        citations=_c(
            "registers.py Gl128Registers",
            "gl128.py module docstring",
            "Correction: jboneng/pyopticfilm#56, TobbyTravel/pyopticfilm_captures "
            "branch add-8100-v2-captures 04_color_7200.pcapng frames 2849/2857/"
            "3023/3031 and 06_ppi_ladder.pcapng frames 3103/3111/3277/3285, "
            "re-verified byte-exact against REG_FEEDL snapshots with independently"
            " written extraction code",
        ),
        safety_note=(
            "HARDWARE INCIDENT (already fixed upstream): applying several "
            "capture-derived register 'corrections' as simultaneous new "
            "defaults and testing on real 8100 V2 hardware produced a real "
            "motor overspeed/hard-stop, twice, each requiring a hard "
            "power-off. Root cause: Gl128._upload_fast_slopes() used the "
            "aggressive SLOPE_TABLE_FAST motor ramp for BOTH positioning "
            "feeds in position_for_full_frame_scan() instead of the gentler "
            "SLOPE_TABLE_SLOW on the final feed. Two independent captures "
            "(plus the original vendor capture) agreed the real driver uses "
            "FAST for the first feed and SLOW for the second. Fixed by "
            "adding a use_slow parameter to _upload_fast_slopes() and "
            "Model8100V2.use_slow_final_positioning_feed=True (SE stays "
            "False) — this fix is already merged and shipping today. "
            "Lesson: never apply multiple untested register corrections as "
            "simultaneous new defaults on real hardware; isolate one change "
            "at a time and expect the true cause to be somewhere other than "
            "the changes under test.\n\n"
            "CORRECTION (2026-09-06, jboneng/pyopticfilm#56): the FAST-"
            "first/SLOW-second pairing above is itself wrong. A third, "
            "independent 2026-09-05 capture set showed the opposite byte "
            "assignment; re-derived from scratch with fresh extraction code "
            "(not the tool that produced the original finding) and "
            "cross-checked against REG_FEEDL in both cited captures: FEEDL="
            "28292 (first/reference feed) uploads SLOPE_TABLE_SLOW, FEEDL="
            "13128 (second/final-positioning feed) uploads SLOPE_TABLE_FAST "
            "— i.e. slow-then-fast, matching the 8200i SE exactly. Both "
            "prior findings agreed with each other but were both wrong; "
            "treat repeated agreement between analyses of the same limited "
            "evidence as weaker corroboration than it feels like. Fix: "
            "position_for_full_frame_scan() now always uploads slow-then-"
            "fast and the per-model use_slow_final_positioning_feed flag "
            "was removed (both models are identical). CONFIRMED on real "
            "8100 V2 hardware 2026-09-06 (Scan Lab: park, full-frame "
            "1200/1800/7200dpi, a crop, ME — no motor fault; NegPy "
            "end-to-end review clean) — this change was isolated and "
            "verified before anything else was touched, per the lesson "
            "above."
        ),
    ),
    RegisterEntry(
        addr="0x3D-0x3F",
        name="REG_FEEDL — V2 full-frame second-feed distance",
        asic=AsicFamily.GL128,
        scope=(SCOPE_8100_V2,),
        meaning="V2's full-frame colour scan starts its second feed at 13128 steps (top of the TA window), vs the SE default of 13704.",
        confidence=Confidence.CONFIRMED,
        citations=_c(
            "device/model_8100_v2.py module docstring, items 1 and 5 — "
            "capture evidence: frame 2999, registers 0x3D-0x3F = 0x00 0x33 0x48 = 13128"
        ),
    ),
    RegisterEntry(
        addr="0x51/0x5D/0x5E",
        name="AFE register access (GL128 path)",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="0x51=AFE sub-address (frontend register index), 0x5D/0x5E=high/low byte of a 16-bit AFE register value. GL845 reaches the frontend through 0x3A/0x3B instead — do not conflate the two paths.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers", "usb/protocol.py write_fe_register_gl124"),
    ),
    RegisterEntry(
        addr="0x7D-0x7F",
        name="REG_EXPOSURE",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="24-bit BE base exposure. Baseline 14000 confirmed in every image pass checked, including a capture where a differing exposure was expected — the underlying question (whether pyopticfilm's non-14000 exposures are also faithful) remains untested since a needed reference capture is missing.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers", "Independent capture analysis, register-program-by-dpi extraction (7/7 files)"),
    ),
    RegisterEntry(
        addr="0x82-0x84",
        name="REG_STRPIXEL",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="24-bit BE, native 7200 dpi units — does not change with resolution for the same crop.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers", "device/model_8200i_se.py module docstring"),
    ),
    RegisterEntry(
        addr="0x85-0x87",
        name="REG_ENDPIXEL",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="24-bit BE, native 7200 dpi units — does not change with resolution for the same crop.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers", "device/model_8200i_se.py module docstring"),
    ),
    RegisterEntry(
        addr="0xAF",
        name="REG_DEPTH_B",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="0x46 = 16-bit output, 0xFF = 8-bit — paired with REG_DEPTH_A (0x33). See the SUSPECTED V2 pair-mismatch entry under 0x33/0xAF.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers"),
    ),
    RegisterEntry(
        addr="0x101",
        name="REG_STATUS",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="High-address read; reuses the GL845 0x41 status bit layout exactly (per Gl128Registers' own docstring, corroborated by direct decode across capture sessions). ScannerStatus.from_reg41() is the one decoder both ASIC families share — this entry documents the reuse, the 0x41 entry above carries the actual bit table.",
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers docstring", "asic/status.py ScannerStatus.from_reg41"),
        bits=(
            BitEntry("0x80", "PWRBIT", "Inverted: clear = replugged/cold since last check.", Confidence.CONFIRMED),
            BitEntry("0x40", "BUFEMPTY", "1 = image/scan buffer empty.", Confidence.CONFIRMED),
            BitEntry("0x20", "FEEDFSH", "1 = feed operation finished (idle sentinel when not feeding).", Confidence.CONFIRMED),
            BitEntry("0x10", "SCANFSH", "1 = scan operation finished (idle sentinel when not scanning).", Confidence.CONFIRMED),
            BitEntry("0x08", "HOMESNR", "1 = carriage is at the home sensor.", Confidence.CONFIRMED),
            BitEntry("0x04", "LAMPSTS", "1 = lamp on. Not the same signal as the driver's own lamp on/off ground truth (register 0x03's LAMPPWR bit) — this is a status readback, not the control bit.", Confidence.CONFIRMED),
            BitEntry("0x02", "FEBUSY", "1 = analog front-end (AFE) busy.", Confidence.CONFIRMED),
            BitEntry("0x01", "MOTORENB", "1 = motor enabled/moving.", Confidence.CONFIRMED),
        ),
    ),
    RegisterEntry(
        addr="0x10000000/0x10004000/0x10008000/0x1000C000/0x10010000/0x10014000",
        name="AHB memory windows (per-channel exposure, motor slopes, shading table)",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning=(
            "AHB_CHANNEL_R/G/B (per-channel RAM exposure), AHB_SLOPE_SCAN/"
            "AHB_SLOPE_FAST (motor ramp tables), AHB_SHADING (shading "
            "correction table) — bulk-upload memory windows, distinct from "
            "the ordinary 1-byte register address space."
        ),
        confidence=Confidence.CONFIRMED,
        citations=_c("registers.py Gl128Registers", "gl128.py upload_tables, upload_shading_table"),
    ),
    RegisterEntry(
        addr="0x000FFF00/0x000FFF01",
        name="Opaque boot blobs",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="Two blobs (34 bytes and 32 bytes, the latter ending 0x33 0x00) the Windows driver writes to these AHB addresses before the register blast. Their meaning is unknown; they are replayed byte-for-byte because boot is not reproducible without them.",
        confidence=Confidence.UNKNOWN,
        citations=_c("gl128.py:157-163 (_BOOT_BLOB_ADDR_A/_B, _BOOT_BLOB_A/_B)"),
    ),
)

#: Non-register behavioral facts worth cataloging alongside the register
#: table — a documented driver behavior, a likely-vestigial inherited
#: field, or a USB protocol-level constant, none of which is itself a
#: device register address.
BEHAVIORAL_NOTES: tuple[BehavioralNote, ...] = (
    BehavioralNote(
        topic="home()/park() standalone motion",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning=(
            "GL128's home()/park() refuse to run a standalone seek — "
            "captures only ever show the carriage returning home via "
            "AGOHOME on the image pass (0x02=0x30). A standalone FEEDL=0 "
            "seek is not proven by any capture."
        ),
        confidence=Confidence.UNKNOWN,
        citations=_c("gl128.py:1695-1719 (home()/park())"),
        safety_note="A standalone home-seek recipe previously caused grinding when invented. Do not add one without new capture evidence.",
    ),
    BehavioralNote(
        topic="Cancel-during-preview park recipe (V2, matches SE shape)",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning=(
            "A third parking scenario, distinct from both entries above "
            "(AGOHOME-on-image-pass, and the unproven standalone FEEDL=0 "
            "seek): pressing Cancel partway through a preview/prescan "
            "operation (not an image pass) triggers a lamp-strobe sequence "
            "on 0x03 (0x30→0x20→0x10→0x00→0x20→0x30→0x20→0x30), "
            "then 0x01=0x22 (clear SCAN), then register 0x101 walks "
            "motor-active→idle/home (0xa5→0xad→0xec). Independently "
            "confirmed on the 8100 V2 (2026-09-05 capture set) reproducing "
            "the exact same 8-value strobe sequence, same three register "
            "addresses (0x01, 0x03, 0x101), and same terminal status walk "
            "already documented for the 8200i SE. Only difference observed: "
            "the SCAN-clear write lands mid-strobe on V2 rather than after "
            "it — cosmetic ordering, not a structural or address difference."
        ),
        confidence=Confidence.CONFIRMED,
        citations=_c(
            "8200i SE: pyopticfilm_captures 8200i-se/08_midtravel_home session",
            "8100 V2: TobbyTravel/pyopticfilm_captures branch "
            "add-8100-v2-captures, 05_midtravel_home.pcapng frames "
            "4549-4789, each write cross-checked against the raw USB "
            "register write by hand",
        ),
    ),
    BehavioralNote(
        topic="feed_steps_for_mm()",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning="Converts millimetres to motor steps. Explicitly marked in its own docstring as experimental — prefer capture-derived step constants (feed_to_reference_steps, feed_to_scan_steps, etc.) over this conversion wherever one exists.",
        confidence=Confidence.UNKNOWN,
        citations=_c("gl128.py:1401-1402 (feed_steps_for_mm)"),
    ),
    BehavioralNote(
        topic="Gl128Common.motor_profile (DEFAULT_GL845_MOTOR)",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128,),
        meaning=(
            "Gl128Common.motor_profile defaults to DEFAULT_GL845_MOTOR, a "
            "GL845-family MotorProfile, with no GL128-specific override and "
            "no citation. GL128 motor motion actually appears to be driven "
            "by the capture-derived SLOPE_TABLE_FAST/SLOPE_TABLE_SLOW byte "
            "tables (see the 0x3D-0x3F FEEDL entry) rather than this "
            "profile — no reference to self.model.motor_profile was found "
            "in gl128.py's read/write logic. Treat as "
            "inherited-and-possibly-dead, not as confirmed behavior."
        ),
        confidence=Confidence.INHERITED,
        citations=_c("device/gl128_common.py:456 (Gl128Common.motor_profile field)", "device/protocol.py DEFAULT_GL845_MOTOR"),
    ),
    BehavioralNote(
        topic="USB control-transfer request codes",
        asic=AsicFamily.GL128,
        scope=(SCOPE_ALL_GL128, SCOPE_ALL_GL845),
        meaning=(
            "REQUEST_BUFFER=0x0C is used for the ordinary ASIC register "
            "read/write path (read_register/write_register) — despite the "
            "name, this is the path for 1-2 byte register access, wValue/"
            "wIndex encode the address. REQUEST_REGISTER=0x04 is used only "
            "for the 1-byte vendor-probe path (read_request_register), "
            "e.g. the GL128 feed-probe at wIndex=0x21. A 2-byte register "
            "read's second response byte is validated against "
            "REGISTER_LINK_OK=0x55 (a link-status sentinel, not part of "
            "the register's own value)."
        ),
        confidence=Confidence.CONFIRMED,
        citations=_c("usb/protocol.py REQUEST_BUFFER, REQUEST_REGISTER, REGISTER_LINK_OK, read_register, read_request_register"),
    ),
)


def entries_for(
    *, asic: AsicFamily | None = None, scope: str | None = None
) -> tuple[RegisterEntry, ...]:
    """Filter the catalog by ASIC family and/or scope (matches shared-scope
    entries too when a specific model scope is requested)."""
    result = REGISTERS
    if asic is not None:
        result = tuple(e for e in result if e.asic == asic)
    if scope is not None:
        result = tuple(e for e in result if scope in e.scope)
    return result


def _cell(text: str) -> str:
    """Sanitize a value for use inside a single markdown table row.

    GFM table rows must be one physical line each; any field containing
    literal newlines (e.g. a multi-paragraph ``meaning``) would otherwise
    split the row and corrupt the table. Blank lines become a visible
    paragraph break, other newlines a soft line break.
    """
    return text.replace("|", "\\|").replace("\n\n", "<br><br>").replace("\n", "<br>")


def render_markdown() -> str:
    """Render the full catalog as markdown, grouped by ASIC family then
    register address. Pure function of the module-level data — call this
    from ``python -m tools.register_reference`` to regenerate
    ``docs/register-reference.md``, or from a test to check it stays
    deterministic and non-empty."""
    lines = [
        "# Register Reference",
        "",
        "Generated by `tools/register_reference.py` — do not hand-edit.",
        "Regenerate with `python -m tools.register_reference`.",
        "",
        (
            "Confidence: CONFIRMED (capture-cited) / INHERITED (assumed from "
            "sibling model or ASIC family) / SUSPECTED (evidence incomplete or "
            "contradictory) / UNKNOWN (meaning genuinely not understood)."
        ),
        "",
    ]
    for asic in AsicFamily:
        family_entries = [e for e in REGISTERS if e.asic == asic]
        if not family_entries:
            continue
        lines.append(f"## {asic.value.upper()}")
        lines.append("")
        lines.append("| Address | Name | Scope | Confidence | Meaning | Safety | Citations |")
        lines.append("|---|---|---|---|---|---|---|")
        for e in family_entries:
            safety = _cell(e.safety_note or "")
            citations = _cell("; ".join(c.text for c in e.citations))
            scope = ", ".join(e.scope)
            lines.append(
                f"| {_cell(e.addr)} | {_cell(e.name)} | {scope} | {e.confidence.value} | "
                f"{_cell(e.meaning)} | {safety} | {citations} |"
            )
            for b in e.bits:
                lines.append(
                    f"| &nbsp;&nbsp;{b.mask} | {_cell(b.name)} | | {b.confidence.value} | "
                    f"{_cell(b.meaning)} | | {_cell('; '.join(c.text for c in b.citations))} |"
                )
        lines.append("")

    lines.append("## Behavioral notes (not register addresses)")
    lines.append("")
    lines.append("| Topic | Asic | Scope | Confidence | Meaning | Safety | Citations |")
    lines.append("|---|---|---|---|---|---|---|")
    for n in BEHAVIORAL_NOTES:
        safety = _cell(n.safety_note or "")
        citations = _cell("; ".join(c.text for c in n.citations))
        scope = ", ".join(n.scope)
        lines.append(
            f"| {_cell(n.topic)} | {n.asic.value} | {scope} | {n.confidence.value} | "
            f"{_cell(n.meaning)} | {safety} | {citations} |"
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    from pathlib import Path

    out = Path(__file__).resolve().parents[1] / "docs" / "register-reference.md"
    out.write_text(render_markdown(), encoding="utf-8")
    print(f"Wrote {out}")
