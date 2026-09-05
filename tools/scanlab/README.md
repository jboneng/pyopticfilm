# Scan Lab

PyQt6 bring-up GUI for exercising `pyopticfilm` without NegPy.

Scan Lab is **repo-only** (not shipped on PyPI). Use it to try mock USB for any
known OpticFilm model, or real scan-ready hardware (8200i SE or 8100 V2) when
plugged in.

It does **not** flip `scan_ready`. Non-scan-ready models stay scan-locked on
real USB unless you explicitly enable **Override safety HW gate** (lab bring-up
only). The mock path is the usual way to drive those pipelines without hardware.

## Requirements

- A checkout of this repository
- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- PyQt6 (via the `lab` dependency group)
- For real scans: WinUSB/libusb access to the scanner — see
  [docs/windows-setup.md](../../docs/windows-setup.md)

## Install and run

From the repository root:

```powershell
uv sync --group lab
uv run python -m tools.scanlab
```

On first launch you should see the main window with a device list, controls on
the left, and image / USB log tabs on the right.

## Quick start (mock)

1. Leave **Run against MOCK** checked (default).
2. Pick any model in **Device** (for example `OpticFilm 8200i SE (GL128)`).
3. Click **Prescan** — a synthetic pattern appears on the Prescan tab.
4. Drag a crop rectangle on the prescan image (optional).
5. Choose **PPI**, optionally enable **IR pass**, then click **Scan**.
6. Open the **USB log** tab to see control/bulk traffic with section markers.

Mock frames are **not** film. The fake USB layer fills RGB16 with a deterministic
pattern (`R=x`, `G=y`, `B=x XOR y`) so the pipeline and UI can be checked without
a scanner.

## Quick start (real scan-ready: 8200i SE or 8100 V2)

1. Bind the scanner for libusb (Windows: WinUSB via Zadig — see windows-setup).
2. Click **Refresh devices**. A connected scan-ready unit should appear with
   `— connected` (SE `07b3:1825` or 8100 V2 `07b3:1824`).
3. Select that device.
4. **Uncheck** **Run against MOCK**.
5. **Prescan** at 1200 dpi (full safe window), drag a crop if you want, then
   **Scan** at the PPI you care about.
6. With **IR pass** enabled (8200i SE only — 8100 V2 has no IR), Scan Lab runs
   colour then infrared. The IR tab shows a grayscale plane (green CCD channel,
   host-flattened), not a magenta RGB preview.

Real non-scan-ready OpticFilms can appear as connected, but the library will
refuse to scan them until that model is hardware-validated (`scan_ready`) —
unless you check **Override safety HW gate** (warning dialog; motors/lamp can
run).

## Quick start (real non-scan-ready bring-up, e.g. GL845 8100)

Lesson from early GL845 8100 HW (`07b3:130c`): override unlocks the gate; it
does **not** make full-TA high-PPI Scan safe. Do not confuse this with the
scan-ready GL128 **8100 (V2)** (`07b3:1824`).

1. Uncheck **Run against MOCK**, check **Override safety HW gate** (accept warning).
2. Leave **Apply calib** on (default). Use **Clear calib cache** if you change film or lamp.
3. **Prescan** (uses the model’s lowest PPI, e.g. 600 on 8100) — Lab clamps to a
   short Y strip, not full TA.
4. If the image is rainbow-striped with a recognizable scene, toggle **USB planar RGB**
   and Prescan again (chunky vs planar decode).
5. Drag a **small** crop, pick a low/mid PPI, then **Scan**. At ≥2400 dpi without a
   short crop, Lab warns and still clamps travel.
6. Do **not** expect full-frame 3600 until decode is correct and short Scans park cleanly.
   `scan_ready` stays False until that is proven.

## Controls

| Control | Purpose |
|---------|---------|
| **Device** | Connected Plustek scanners first, then every known model. |
| **Run against MOCK** | On (default): fake USB. Off: open the selected connected device. |
| **Override safety HW gate** | Off (default). On: after a warning, unlock scan/home/park on real USB for models with `scan_ready=False`. Does not flip `scan_ready`. |
| **Apply calib** | **On** (default). ASIC shading before colour prescan/scan; first run at each PPI/geometry measures once, then reuses `~/.cache/pyopticfilm/calib_v2.json`. Forced off when **Override safety HW gate** is on. |
| **Clear calib cache** | Deletes cached shading entries; next scan re-measures at home. |
| **USB planar RGB** | Off = chunky `RGBRGB…` (default). On = planar planes — try this if Prescan is rainbow-striped. |
| **Adaptive quiet drain** | On (default). Rate-limits host bulk reads to the ASIC `LPERIOD` line rate (including 7200 dpi, ~3.55 ms/line) so motor creep stays continuous. Uncheck for fastest drain (louder). |
| **Slow image slope** | Off (default). On: shading/slow motor ramp on the image pass (feeds stay fast). |
| **Disable priming pass (debug)** | GL128 only. Off (default): use the model's priming default (off for 8200i SE and 8100 V2). On: skip the discarded first-scan AGOHOME-park pass on both Prescan and Scan. |
| **Refresh devices** | Re-enumerate USB and rebuild the device list. |
| **PPI** | Resolutions from the selected model’s `resolutions_dpi` (Scan only; Prescan uses a fixed low dpi). |
| **IR pass** | After colour Scan, run a second infrared pass (disabled if the model has no IR). |
| **Multi-exposure (ME)** | GL128 / hardware-tested models: short + adaptive long colour passes (42k–85k safety envelope, capped at 42000 at 7200 dpi; fallback 42000); host SNR/IVW merge into ``rgb``. Bracket planes on ``Scanner.last_me_debug`` (not ``ScanImage``). |
| **Fixed 42k long (A/B)** | When ME is on: force SilverFast-style long exposure 42000 instead of adaptive selection. |
| **Manual exposure overrides** | GL128 debug/testing only — see [Manual exposure overrides](#manual-exposure-overrides) below. |
| **Prescan** | Low-res preview (GL128: 1200 dpi safe window; non-scan-ready: lowest dpi + short Y strip). |
| **Scan** | Colour scan at the chosen PPI; optional IR and/or ME. Uses the prescan crop when one is set (clamped on non-scan-ready). |
| **Open capture…** | Open a USBPcap ``.pcap`` / ``.pcapng``. Decodes the largest bulk IN through the selected model’s image pipeline and diffs FEEDL / LINCNT / DPISET vs Lab geometry (Capture tab). |
| **Cancel** | Sets the scan cancel event (busy scans only). |

The yellow banner states MOCK vs REAL for the current selection (HW gate override
and USB RGB layout when relevant).

## Manual exposure overrides

Three GL128-only text fields let Scan Lab send an exact `REG_EXPOSURE` value for
debugging/testing, bypassing the driver's normal soft limits:

| Field | Controls | Empty (default) |
|-------|----------|------------------|
| **Single-pass exposure** | The retained Scan pass when ME is off. | Model-derived exposure, still clamped to the hardware max. |
| **ME short exposure** | The ME short pass only. | Model-derived short exposure, still clamped to the hardware max. |
| **ME long exposure** | The ME long pass only; overrides **Adaptive**/**Fixed 42k long** entirely. | Normal Adaptive/Fixed selection (42k–85k envelope, DPI clamp). |

Each field is grayed out when it does not apply to the current pass selection
(e.g. the single-pass field while ME is on). A value is written to
`REG_EXPOSURE` **verbatim** — it skips the adaptive selection, the DPI clamp,
and the hardware-max clamp that apply to normal (automatic) exposure. The only
limit enforced is the actual 24-bit register range (1–16777215 / `0xFFFFFF`);
an out-of-range value is rejected with a clear error rather than clamped.

These overrides exist for hardware/debugging experiments — an excessive value
can produce severe overexposure/clipping, and the software intentionally does
not prevent that. They apply only to the retained Scan pass, never to the
discarded GL128 priming pass.

## Tabs

### Prescan

Full-window preview. Drag with the left mouse button to set a normalized crop.
Scan uses that crop in the same image coordinates as the prescan preview
(orientation corrected in ``ImagePipeline.assemble()``).
A click or rubber-band too small to keep leaves the current crop in place.
Right-click the preview to clear it; changing device or running a new Prescan
also clears the crop. The status bar reports ``crop applied W×H`` or
``full window`` after Scan.

### Color short / Scan

Colour result of the last Scan (short exposure when ME is enabled). ME short/long
tabs read **linear** bracket planes from ``Scanner.last_me_debug`` after a live
ME scan (expected: long ≈ 3× brighter mean) — same for **Load 16-bit TIFF…**
(no per-tab auto-level; Explorer brightness differences stay visible). Opens a
saved short plane from disk (e.g. exported earlier or from NegPy).

### Color long

Long ME exposure frame when **Multi-exposure** was enabled (linear; should look
brighter than Color short). **Load 16-bit TIFF…** opens a saved long plane. When
both short and long are loaded (from scan or disk), the **Merged** tab shows the
SNR/IVW host merge without rescanning.

### Merged

SNR/IVW merge of short+long whenever ME planes are present (per-channel
clip confidence, soft highlight roll-off). The merged/`rgb` deliverable may
include film-base peak makeup with a highlight headroom cap; short/long tabs
do not. Offline audit: ``python -m tools.audit_me_bracket short.tif long.tif``.

### IR

Infrared result when **IR pass** was enabled. Displayed as **grayscale** from
the driver’s IR plane (`ScanImage.ir`), which is the green CCD channel after
host flatten on GL128.

### Capture

Passive analysis of a USBPcap recording (VueScan / SilverFast / SANE / Lab):

1. Select the model that matches the capture (e.g. OpticFilm 8100).
2. **Open capture…** and choose a `.pcapng` / `.pcap` from USBPcap/Wireshark.
3. The **Scan** tab shows the decoded image. Lab finds the SilverFast
   ``VALUE_BUFFER`` preamble (``wIndex=0x08``), carves that many bulk-IN bytes,
   and for **GL128** (8200i SE / 8100 V2) decodes as 16-bit chunky RGB using
   capture STRPIXEL/ENDPIXEL/LINCNT/DPISET (Lab PPI is ignored for decode — a
   mismatch shears the image). Use **USB planar RGB** only if rainbow-striped.
   Select the matching GL128 model for those captures. The **Capture** tab
   reports preamble size and register diffs.
4. The **Capture** tab lists FEEDL / LINCNT / DPISET from the capture versus what
   Lab would program at the current PPI / crop — useful for motor-grind diagnosis.

This does **not** replay the foreign driver’s full USB conversation through
`Scanner.scan()`; it only decodes image data and compares key registers.

### USB log

Live truncated log of every control and bulk transfer through the recording
USB wrapper. **Open capture…** also fills this tab from the pcap (identical
line format; repeated bulk-IN lengths are collapsed as ``×N``).

- Dividers mark `PRIMING` (or `PRIMING SKIPPED (debug)` when priming is skipped), `PRESCAN`, `SCAN`, `IR`, and `CAPTURE` sections.
- **Jump** buttons scroll to those dividers when present.
- **Clear USB log** empties the buffer (Prescan also clears the log).

Progress for the active pass is shown in the status bar. On the first scan
after open (GL128), the status bar shows **Priming scanner…** if an explicit
prime was requested, then **Scanning…** — or **Priming skipped (debug)…**
when the model default skips priming or **Disable priming pass (debug)** is
checked.

### Forensic

A separate module set (`tools/scanlab/forensic_*.py`) providing a guided
evidence-recording workflow, live anomaly detection, a graphical USB
timeline with an Event Inspector, and a register/status **Reference** page
— all built on top of `tools/register_reference.py`'s confidence-tagged
register catalog (CONFIRMED/INHERITED/SUSPECTED/UNKNOWN, cited, one entry
per model or shared across the GL128 family). Sub-pages:

- **Live timeline** — plain-text scrolling log of every decoded USB event.
- **Timeline** — a graphical, colour-lane timeline (per event kind) with
  click-to-inspect; the Event Inspector shows raw bytes, decoded fields,
  and the matching register's catalog meaning together. A legend above the
  graph explains every lane colour, the anomaly severity markers, and the
  feed-timing span colour. Below the axis, duration brackets mark each
  positioning feed — labelled with the slope table actually uploaded for it
  (**FAST**/**SLOW**/**CUSTOM**, identified by comparing the raw AHB upload
  payload byte-for-byte against the known tables) and how long the motor was
  enabled for that feed — above a Motor/Lamp on-off state band. A toolbar
  label reports the run's total recorded duration. A **Known/Unknown
  values** panel alongside the graph lists every distinct register value
  the run touched (slope table per feed, LPERIOD, EXPOSURE, pixel clock)
  next to any register addresses seen with no catalog entry to explain
  them, so unrecognized traffic stays visible instead of silently dropped.
- **Reference** — every known status bit and register, filterable, rows
  coloured by confidence; a register flagged with a hardware-safety note
  (e.g. the FEEDL positioning-feed slope tables, or the 8100 V2 feed-probe
  at wIndex 0x21) shows a bold ⚠ marker with the safety note as its
  tooltip. **Jump to timeline for selected register** switches to the
  Timeline tab and reports how many events in the currently loaded run
  reference that register.
- **Run browser** — saved run list, baseline/compare, milestones and
  anomalies for a selected run, and Wireshark/USBPcap import. The exported
  AI bug report includes a **Positioning & timing** section listing the
  feed-timing, LPERIOD, EXPOSURE, and pixel-clock milestones for the run.

Regenerate the standalone markdown catalog (`docs/register-reference.md`)
with `python -m tools.register_reference` after editing the catalog.

## Geometry notes

### GL128 (8200i SE and 8100 V2)

- Prescan / uncropped Scan use the capture-safe **preview** window (feed2 at
  the top of the TA window), not a raw full-TA `area=None` request that can
  overrun the motor window.
- Rubber-band crops are clamped so image `LINCNT` cannot past the scan-window
  end (see `captures/8200i-se/MOTOR.md` in the repo if present).
- High PPI (≥2400) uses line-aligned bulk drain; **Adaptive quiet drain** keeps
  motor creep continuous at 7200 by matching host reads to `LPERIOD` even when
  that is above 3 ms/line (no fixed pause before each USB chunk).
  Uncheck for fastest/loudest drain. **Slow image slope** is an optional acoustic
  probe (feeds stay fast).
- 8100 V2 has no IR; leave **IR pass** off on that model.

### Unvalidated non-scan-ready (GL845 8100 / aliases, …)

- Prescan and uncropped Scan use a **short Y strip** (~5 mm / ≤18% of TA), never
  full TA — full-frame high PPI was observed to grind the motor on a GL845 8100.
- Tall rubber-band crops are clamped to that same height budget.
- High-PPI Scan (≥2400) with override on prompts a warning before the clamped move.
- Prefer **Apply calib** off and **USB planar RGB** experiments before long travels.

Calibration is controlled by **Apply calib** (default on) and **Clear calib cache**.

## Motor grind recovery

If the carriage grinds, chatters against a stop, or stops mid-travel during Lab
bring-up:

1. **Power off** the scanner (front/power switch).
2. **Unplug the power cord** from the scanner (or wall), wait a few seconds.
3. **Reconnect** power and turn the scanner back on.
4. **Home** the head with a known-good tool (for example **VueScan** or
   **SilverFast**) before using Scan Lab or pyopticfilm again.

This recovery sequence has only been **tested on the OpticFilm 8200i SE**. Treat
it as a starting point on 8100 V2 and other models; keep a hand near power and
do not force the carriage by hand.

## What Scan Lab is not

- Not part of the PyPI package or wheel/sdist.
- Not a NegPy replacement (no iSRD retouch UI, no roll workflow). Each image tab can export 16-bit TIFF for use in NegPy.
- Not a substitute for flipping `scan_ready` — override is lab-only unlock.

For protocol / support levels, see
[docs/scanner-validation.md](../../docs/scanner-validation.md).

## Layout

```
tools/scanlab/
  __main__.py   # uv run python -m tools.scanlab
  app.py        # main window
  backend.py    # device list, open real/mock, GL128 geometry helpers
  capture_pcap.py  # USBPcap parse + bulk decode / register diff
  preview.py    # numpy downsample / auto-level (no Qt)
  worker.py     # QThread scan worker + USB log dividers
  widgets.py    # crop view + RGB/gray preview
  forensic_tab.py            # Forensic tab: Monitor/Session/Register lab + Live timeline/Timeline/Reference/Run browser
  forensic_reference.py      # adapter over tools/register_reference.py for the Reference tab and inline hints
  forensic_timeline_view.py  # graphical, colour-lane USB timeline widget
  forensic_event_inspector.py  # per-event raw+decoded+register-meaning view
  forensic_milestones.py     # buffer-preamble/FEEDL/lamp milestone classifier
  forensic_anomaly.py        # stale-state/long-gap/unsafe-write anomaly rules
  forensic_session.py        # per-run evidence recording (usb_raw/decoded_events/phase_markers jsonl)
  forensic_diff.py           # first-divergence diff between two runs
  forensic_pcap_import.py    # import a Wireshark/USBPcap capture as a run
  forensic_report_export.py  # AI-bug-report export
  cli.py        # headless Prescan/Scan CLI
  README.md     # this guide

tools/register_reference.py  # canonical register/bit catalog (both scanners); see docs/register-reference.md
```

Scans never run on the GUI thread; USB I/O goes through `ScanWorker` on a
`QThread`.
