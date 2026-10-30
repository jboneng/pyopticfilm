# Scanner protocol validation (hardwareless)

This document describes how pyopticfilm tests scanner protocol behaviour
without a physical device, and how that differs from hardware bring-up.

Protocol tests **do not** prove that a physical scanner works. Motor timing,
lamp/AFE analogue behaviour, firmware quirks, and real USB races are out of
scope. The OpticFilm 8200i SE, OpticFilm 8100 (V2), OpticFilm 8300i SE, and
OpticFilm 7600i v1 are hardware-tested (`scan_ready=True`). Every other model
keeps `scan_ready=False` until it has produced a real image and a successful
park.

## Support levels

| Level | Meaning |
|-------|---------|
| **Hardware tested** | Live scan + park on physical hardware. Currently the 8200i SE, 8100 (V2), 8300i SE, and 7600i v1. |
| **Protocol validated** | Python USB traffic for a documented setup matches a golden trace, and optical registers match independently computed geometry. A SANE genesys register dump, when present, is an additional oracle. |
| **Experimental** | Tables and session code exist; `scan()`, `home()`, `park()`, and `calibrate()` stay locked. |

The GL128 models (8200i SE, 8100 V2, and 8300i SE) are capture-derived (not a
SANE port). SANE is not an oracle for them. Register-program goldens for
`init` + configure are under `tests/traces/python/8200i_se/`,
`tests/traces/python/8100_v2/`, and `tests/traces/python/8300i_se/`. The 8100
V2 and 8300i SE share SE-identical tables via `Gl128Common` where captures
match; each leaf declares capture-proven divergences. The 8100 V2 has no IR;
the 8300i SE has IR and is hardware-tested (`scan_ready=True`).

## Architecture

```text
ScanSession / Gl845
        │
        ▼
GenesysUsbProtocol
        │
        ▼
UsbTransport (Protocol)
        ├──────────────► UsbDeviceHandle  → PyUSB
        └──────────────► FakeUsbTransport / MockScannerTransport → tests + Scan Lab
```

`UsbTransport` already existed on `GenesysUsbProtocol`. Tests construct
`create_asic(GenesysUsbProtocol(fake), model)` and call ASIC / `ScanSession`
directly. They do **not** go through `Scanner.scan()` (the `scan_ready` gate is
intentional).

Helpers:

| Path | Role |
|------|------|
| `tests/scanners/fake_usb.py` | Recording fake USB device |
| `tests/scanners/trace_compare.py` | JSON traces, poll collapsing, first-difference diffs |
| `tests/scanners/sane_debug.py` | Parse `SANE_DEBUG_SANEI_USB` / genesys logs |
| `tests/traces/python/` | Golden Python USB traces (CI) |
| `tests/traces/sane/` | Independently generated SANE fixtures (optional) |
| `tools/dump_python_setup_trace.py` | Regenerate a Python golden trace |
| `tools/compare_scanner_trace.py` | Compare two JSON traces |
| `tools/sane_debug_to_trace.py` | Convert a SANE debug log to JSON |
| `tools/scanlab/` | PyQt6 bring-up GUI (mock or real scan-ready hardware); [user guide](../tools/scanlab/README.md) |

## GL128 multi-exposure long exposure limits

Adaptive ME colour-long `REG_EXPOSURE` is clamped before the long pass runs
(`clamp_me_long()` in `session_gl128.py`). 8200i SE, 8100 (V2) and 8300i SE
share the same envelope (`Gl128Common` defaults). Since 1.3.7, `Scanner.scan`
and `Gl128ScanSession.run` reject multi-exposure and Multi-Pass at 7200 dpi on
all three, so the 7200 row is no longer reached by ME; it is kept for the clamp
logic and the 16-bit note below:

| PPI | Long clamp |
| --- | --- |
| not 7200 | 14000–85000 |
| 7200 (oversample 1) | 14000–64000 |

The shared fields are `me_long_clamp_min` / `me_long_clamp_max`. The 7200 dpi
cap is not a second model constant: the ceiling is
`min(model max, 64000 * oversample)`. Oversample is 1 only at native optical
resolution, so 85000 is left intact at 3600 dpi and below.

64000 is a safety margin under 65536. The AHB scan motor table
(`tables_8200i_se.exposure_table`) is 16-bit, and at oversample == 1
`channel_exposure_for()` passes the exposure straight through into it
unmasked — a value at or above 65536 would silently wrap there while
`REG_EXPOSURE` (24-bit) does not, desyncing the two and jamming the motor on
real hardware (observed; `exposure_table()` now raises instead of wrapping).

The 16-bit limit still applies to manual exposure overrides on single-pass
7200 dpi scans. The short bin is unchanged. Single-pass (non-ME) scans are not
affected by the clamp.
The adaptive selection floor stays `me_adaptive_min_exposure` (42000), which
is separate from the hard floor of 14000. Stock `exposure_long` of 42000 is
within range. Manual `me_long_exposure` still bypasses this clamp.

## Current golden: OpticFilm 8200i, 1800 dpi, RGB16

Phase recorded: ASIC `init()` + `ScanSession._configure()` (no home poll, no
lamp, no image bulk, no calibration AHB). Status register `0x41` is scripted
idle (at home, buffer empty, frontend ready) so poll loops exit immediately.

CI checks:

1. USB-decoded DPISET / STRPIXEL / ENDPIXEL / LPERIOD / dummy / MAXWD / LINCNT
   match `compute_geometry(1800, model=MODEL_8200I)`.
2. Full USB transaction list matches `tests/traces/python/8200i/1800_rgb16_setup.json`.
3. If `tests/traces/sane/8200i/1800_rgb16_setup.registers.json` exists, optical
   registers are compared to that SANE dump.

Regenerate the Python fixture after an intentional protocol change:

```bash
python tools/dump_python_setup_trace.py
```

Review the diff. Do not copy Python tables into a second Python file and assert
they are equal.

## SANE as oracle (spike notes)

Byte-for-byte USB against a live `sane_start` is **not** the first oracle.
SANE genesys is a full device state machine. Even a correct port diverges on:

- Home / feed / buffer-empty poll counts
- Gamma LUT AHB uploads (not ported)
- Calibration shading blobs
- Warm vs cold boot and slope-table bulk payloads if generation differs

Prefer the **register program after scan setup** (`DPISET`, `STRPIXEL`,
`ENDPIXEL`, `LINCNT`, `LPERIOD`, `MAXWD`, lamp/scan bits).

### Interception points (lowest practical first)

1. **Genesys register log** (preferred). `SANE_DEBUG_GENESYS=255` emits
   `write_register (0xNN, 0xVV)`, `reg[0xNN] = 0xVV`, and
   `address: 0xNNNN, value: 0xVV` from `ScannerInterfaceUsb::write_register`.
2. **`sanei_usb_control_msg`**. `SANE_DEBUG_SANEI_USB=255` logs `rtype`, `req`,
   `value`, `index`, `len`, then hex dumps. This is the USB wrapper below
   genesys and above libusb.
3. **libusb / umockdev / USB gadget**. Needed for a true hardwareless SANE
   *run*. Not required to *compare* a log captured on a machine that has the
   scanner, and not the starting point.

A hardwareless SANE process still needs a responding USB device (or a patched
`scanner_interface_usb.cpp`). Until that exists, do not commit a USB JSON file
claiming to be from SANE.

### Generating a SANE register fixture (Linux, genesys built with debug)

```bash
export SANE_DEBUG_GENESYS=255
export SANE_DEBUG_SANEI_USB=255
scanimage -d genesys --resolution 1800 --mode Color \
    -x 36.33 -y 25 --format=pnm > /dev/null 2> sane-8200i-1800.log

python tools/sane_debug_to_trace.py sane-8200i-1800.log \
    --out tests/traces/sane/8200i/1800_rgb16_setup.json \
    --model "OpticFilm 8200i" --dpi 1800 --revision "$(git -C sane-backends rev-parse HEAD)"

# Register-only file is enough for CI (see test_sane_register_fixture_if_present):
python tools/sane_debug_to_trace.py sane-8200i-1800.log \
    --out tests/traces/sane/8200i/1800_rgb16_setup.registers.json \
    --model "OpticFilm 8200i" --dpi 1800 --revision "$(git -C sane-backends rev-parse HEAD)"

python tools/compare_scanner_trace.py --registers-only \
    tests/traces/sane/8200i/1800_rgb16_setup.json \
    tests/traces/python/8200i/1800_rgb16_setup.json
```

SANE file anchors for optical init and calib order are listed in
[sane-opticfilm.md](sane-opticfilm.md). Keep the Python setup golden intact;
add home/feed sequences as separate fixtures once those paths are stable.

Record the SANE backends git revision in the JSON `meta.revision` field so
fixture drift is explainable.

Parser coverage is exercised in CI against
`tests/data/sane_debug_sample.log` (a canned snippet, not a full scan).

### Lab bring-up (GL845 8200i / 7600i v2, does not flip `scan_ready`)

```bash
uv run python tools/bringup_gl845_8200i.py --dry-run
uv run python tools/bringup_gl845_8200i.py --model 7600i-v2 --steps open,status
uv run python tools/bringup_gl845_8200i.py --model 7600i-v2 \
    --allow-unvalidated --steps init,home,tiny,park
```

The 7600i v2 shares the GL845 8200i session/tables in this release. The tool
checks the selected model identity before issuing commands. Use `--device-id`
when multiple Plustek scanners are connected. ASIC init and motor/image steps
require the explicit lab override and do not change `scan_ready`.

Flip only the tested model's `scan_ready` flag after its hardware sign-off
checklist below passes; a tiny scan alone is not sufficient.

### USB Device Identification (7600i v1 vs v2)

Both the OpticFilm 7600i v1 and 7600i v2 share the same USB Vendor ID (`0x07b3`)
and Product ID (`0x0c3b`). The hardware revision is distinguished by the USB
device release number (`bcdDevice`):

- **7600i v1**: `bcdDevice == 0x0400`, powered by the Genesys Logic **GL843** ASIC.
- **7600i v2**: `bcdDevice == 0x0605` (1541 decimal), powered by the Genesys
  Logic **GL845** ASIC.

`pyopticfilm.device.select.model_for_device(product_id, bcd_device)` inspects
`bcdDevice` to disambiguate: `0x0400` resolves to `MODEL_7600I_V1`, while
`0x0605` resolves to `MODEL_7600I_V2`. To verify `bcdDevice` on an attached
scanner, use `tools/inspect_bcddevice.py` or inspect PyUSB device descriptors.

### 7600i v2 bring-up status

Hardware findings, settings and open work for this model are in
[opticfilm-7600i-v2.md](opticfilm-7600i-v2.md).

Tested on physical OpticFilm 7600i v2 hardware (`07b3:0c3b`, `bcdDevice=0x0605`):

- **Open & Status**: Successful (device detected, status registers read).
- **Init**: Successful (GL845 registers configured). Note: `init()` must be
  called before `home()`.
- **Lamp**: Successful (white LED toggles on/off).
- **Home / Park**: The old direct AGOHOME/FEEDL seek timed out after a full-area
  3600-dpi scan; status reported `is_at_home=False` and `is_motor_enabled=True`.
  A SilverFast prescan recovered the carriage. The replacement SANE-style
  reverse mini-scan and stop verification passed supervised tests from a
  replugged/not-home state, after a 900-dpi crop, and after full-area 900-,
  3600-, and 7200-dpi scans. Each ended at home with motor idle and lamp off.
- **Tiny scan**: Successful at 900 dpi (88x193 RGB16), 1800 dpi, and 3600 dpi (354x768 RGB16).
- **Full-area scans**: Successful at 900 dpi (1287x885 RGB16), 3600 dpi
  (5136x3543 RGB16), and 7200 dpi (10288x7086 RGB16). The 3600-dpi TIFF and raw
  NumPy capture were verified locally. Large captures are bring-up artifacts,
  not CI fixtures.
- **Calibration**: The earlier 900/1800-dpi "passes" were invalid: the dark
  strip was captured with the lamp on, so dark ≈ white. After the fix, dark
  reads about 1.6–1.8k against a white near 55k. White is now taken on the open
  light path 1.5 mm from home (`calib_white_home_y_mm`), not on the holder edge
  at the SANE TA offset. A per-channel PGA search puts the white p99 near 60k
  (gains ≈ 38/24/27). Full-frame 1800- and 3600-dpi calibrated scans show no
  column banding or colour cast.
- **Exposure**: The input-referred read noise is the same at 1.5× and 6× PGA
  gain, so exposure is the SNR lever. Single-pass auto exposure (900-dpi
  prescan) raised LPERIOD to about 3.2× on a dense negative and cut densest-blue
  noise by 36%. LPERIOD up to 65000 ran clean at 1800, 3600 and 7200 dpi
  (stall-checked feed, parked).
- **IR testing**: Full-area combined color + IR succeeded at 900 dpi; RGB and IR
  planes were both uint16 and 1287x885. Home between passes and final park
  succeeded. The hardware IR LED switches via GPIO 0xA8 (`IR_LAMP_A8_MASK`).
- **Sign-off matrix (Wolfen NC400, auto exposure, pre-feed)**: Full-frame
  calibrated colour at 900 dpi (1287x885, 75 s) and 7200 dpi (10288x7086, 239
  s). Multi-exposure full frame at 900, 1800 and 3600 dpi, plus a 7200-dpi 50%
  crop. The ME long pass was 64400 (4.6x), and measured long/short ratios ran
  4.55–4.76 per channel. Clipping was at most 0.024%. Every scan ended at home
  with the motor idle.
- **Pre-feed**: The feed to the film runs at scan speed, so its time scales with
  dpi x LPERIOD. A few-line 900-dpi pre-feed covers it and stops 400 steps
  short. With a measured 6-step coast correction, it lands within +0.01 px (1800
  dpi) and -0.42 px (7200 dpi) of a direct feed. At 7200 dpi, feed plus crop
  time drops from 132 s to 20 s.
- **Multi-exposure**: At 1800 dpi, long/short measured 4.69/4.72/4.66 against a
  nominal 4.60 (LPERIOD 14000/64400), with a sub-pixel pass shift. Full-frame
  3600-dpi colour + IR with auto exposure: IR shift 0.05 px, the head parked,
  and the IR plane shows dust on a flat field.

## What protocol validation can establish

- Register configuration and command ordering
- Endpoint / vendor-request framing
- Transfer sizes and bulk preambles
- Model table application (DPISET, exposure, dummy, MAXWD units)
- Host image decode (channel order, endian, 8/16-bit, line shifts)

## What it cannot establish

- Physical motor / sensor / lamp timing
- ASIC hardware quirks and analogue frontend behaviour
- Calibration quality
- Firmware-specific behaviour
- Real USB timing and disconnect races

## Hardware sign-off (scan-ready / lock-oracle edits)

CI cannot run a physical scanner. Before flipping `scan_ready` or changing that
model's files under `tests/model_lock/`, confirm on **that** hardware:

- Park / AGOHOME return to home
- Full-frame colour at the model's supported low, middle, and maximum resolutions
- A cropped window
- Multi-exposure colour only where implemented for that ASIC/model
- Infrared, if the model has an IR channel

For the 7600i v2, the supported resolution ladder is 900, 1800, 3600, and 7200
dpi. GL845 multi-exposure is experimental. Before ME is listed as supported,
record a supervised ME scan at each validated resolution that confirms the long
LPERIOD envelope (`me_long_clamp_max`).

See [CONTRIBUTING.md](../CONTRIBUTING.md).

## OpticFilm 7600i v1 (GL843)

Capture-derived: strict playback checks the four captured profiles against their SilverFast
sequences (`usb/fake_gl843_v1.py`). Derived 7200 dpi IR and transformed defaults have separate
structural/simulation coverage; playback alone does not establish optical accuracy.
See [opticfilm-7600i-v1.md](opticfilm-7600i-v1.md).

## GL128 (8200i SE, 8100 V2, 8300i SE)

Use USB captures → golden traces when converting existing PCAP/PCAPNG files.
Do not compare the GL128 path to SANE genesys (there is no GL128 command set).
The 8100 V2 and 8300i SE are capture-derived siblings of the 8200i SE (shared
`Gl128Common` tables). They do not subclass the SE model class. The 8300i SE
has IR and is hardware-tested (`scan_ready=True`).

CI goldens for ASIC `init()` + `Gl128ScanSession._configure()` (motors gated)
live at:

- `tests/traces/python/8200i_se/{1200,1800,7200}_rgb16_setup.json`
- `tests/traces/python/8100_v2/{1200,1800,7200}_rgb16_setup.json`
- `tests/traces/python/8300i_se/{1200,1800,7200}_rgb16_setup.json`

Regenerate after an intentional GL128 setup-register change:

```bash
python tools/dump_gl128_setup_trace.py
```

Review the optical-register diff. Do not copy one model's JSON over another
to make CI green.
