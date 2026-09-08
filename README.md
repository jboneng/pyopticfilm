# pyopticfilm

Python driver for Plustek OpticFilm USB film scanners, built on [PyUSB](https://github.com/pyusb/pyusb) and reverse-engineered from USB captures and the SANE `genesys` backend.

The library talks directly to the scanner’s Genesys ASIC (GL842, GL843, GL845, or GL128) over USB—no vendor Windows driver is required once the device is bound for libusb access.

## Supported hardware

**Only the OpticFilm 8200i SE and OpticFilm 8100 (V2) are hardware-tested for scanning in this release.**

Support is one of:

- **Hardware tested** — live scan + park on physical hardware
- **Protocol validated** — USB/register traces match a golden setup without hardware; motors stay locked
- **Experimental** — tables and session code exist; scan/home/park/calibrate stay locked

| Model | USB ID | ASIC | Support |
|-------|--------|------|---------|
| OpticFilm 8200i SE | `07b3:1825` | GL128 | **Hardware tested** |
| OpticFilm 8100 (V2) | `07b3:1824` | GL128 | **Hardware tested** (no IR) |
| OpticFilm 8200i | `07b3:130d` | GL845 | Protocol validated (setup traces; scan locked) |
| OpticFilm 8100 | `07b3:130c` | GL845 | Experimental |
| OpticFilm 7600i (v1 / v2) | `07b3:0c3b` | GL845 / GL843 | Experimental |
| OpticFilm 7500i | `07b3:0c13` | GL843 | Experimental |
| OpticFilm 7400 (v1 / v2) | `07b3:0c3a` | GL845 / GL843 | Experimental |
| OpticFilm 7300 | `07b3:0c12` | GL843 | Experimental |
| OpticFilm 7200i / 7200 | `07b3:0c04`, `07b3:0807`, `07b3:0c07` | GL843 / GL842 | Experimental |

The GL845 **OpticFilm 8100** (`07b3:130c`) is a different product from the GL128 **8100 (V2)** (`07b3:1824`).

Other OpticFilm models **enumerate and open**: you can read status, turn the lamp on/off (where implemented), and dump registers for bring-up. **`scan()`, `calibrate()`, `home()`, and `park()` stay gated** until a model is hardware-tested—calling them raises `AsicError` rather than risking carriage or lamp damage. Protocol validation does **not** flip that gate. See [docs/scanner-validation.md](docs/scanner-validation.md).

`Scanner.open()` prefers a scan-ready device (8200i SE or 8100 V2) when several Plustek film scanners are connected.

## Features 

- Color and infrared transparency scans at 150–7200 dpi (ASIC programs at ≥600 dpi; lower PPI shares the 600 dpi register set and is downsampled on the host; infrared is available only on supported hardware)
- Infrared as a dust plane on `ScanImage.ir` (`mode="infrared"`, or `infrared=True` with colour; 8200i SE only among the hardware-tested set)
- Multi-exposure (ME) on GL128 hardware-tested models (8200i SE and 8100 V2): short + adaptive long colour passes with host SNR/IVW merge into `ScanImage.rgb` (`multi_exposure=True`); bracket planes via `Scanner.last_me_debug`
- Multi-Pass on GL128: repeat an already-validated exposure `n_passes` times (1–9) and stack the aligned repeats for an SNR gain — no new exposure/speed value is ever introduced, only repeats of the short pass (`n_passes>1`) or, combined with `multi_exposure=True`, of both the short and long ME passes (Adaptive Multi-Pass); per-slot stacking stats via `Scanner.last_multi_pass_debug`
- Manual exposure overrides on GL128 (`single_pass_exposure` / `me_short_exposure` / `me_long_exposure`) for testing/debugging: bypass the adaptive/hardware-max clamps and write an exact `REG_EXPOSURE` value (24-bit register range)
- Optional crop via normalized `area` (`x1, y1, x2, y2` in 0–1)
- Dark/white shading calibration with on-disk cache (`~/.cache/pyopticfilm/calib_v2.json`)
- GL128 ASIC shading path (AFE codes + shading blob) aligned with SilverFast capture order
- Adaptive quiet USB drain on GL128 (line-aligned; keeps motor creep continuous at high PPI)
- Left–right orientation corrected in `ImagePipeline.assemble()` for `mirror_x` models
- 16-bit RGB `numpy` output; optional TIFF export via `tifffile`
- Progress and cancel hooks for long scans

Not implemented or out of scope here: iSRD infrared dust removal, SilverFast-style UI, or shipping a desktop app—applications own post-processing.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release notes. Latest release: [v1.3.3 on GitHub](https://github.com/jboneng/pyopticfilm/releases/tag/v1.3.3).

## Requirements

- Python ≥ 3.11
- `numpy`, `pyusb`
- **libusb 1.0** backend for PyUSB
  - **Windows:** `libusb-package` is installed automatically with `pip install pyopticfilm` and provides a bundled `libusb-1.0.dll`
  - **Linux:** system `libusb-1.0` (e.g. `libusb-1.0-0` on Debian/Ubuntu) and permission to access the device (udev rule or run as root—not recommended)
  - **macOS:** libusb via Homebrew or `libusb-package`

Optional: `tifffile` for `ScanImage.save_tiff()`.

## Installation

```bash
pip install pyopticfilm
```

From source:

```bash
git clone https://github.com/jboneng/pyopticfilm.git
cd pyopticfilm
uv sync --all-groups
```

## USB access

The Plustek vendor driver must **not** own the device when using this library.

### Windows

Use [Zadig](https://zadig.akeo.ie/) to replace the vendor driver with **WinUSB** (or libusbK) for the scanner’s USB interface. Step-by-step instructions, troubleshooting, and how to revert to the Plustek driver are in [docs/windows-setup.md](docs/windows-setup.md).

### Linux

Install libusb and add a udev rule granting your user access to Plustek film scanners, for example:

```
# /etc/udev/rules.d/99-plustek-opticfilm.rules
SUBSYSTEM=="usb", ATTR{idVendor}=="07b3", MODE="0666"
```

Reload udev rules and replug the scanner.

## Quick start

```python
from pyopticfilm import Scanner

with Scanner.open() as scanner:
    print(scanner.model.model, scanner.device_id)
    scanner.warmup()  # init, home, lamp on
    image = scanner.scan(resolution=1800, mode="color")
    print(image.rgb.shape, image.rgb.dtype)  # H×W×3 uint16

    image.save_tiff("frame.tif")  # requires tifffile
```

Infrared scan (8200i SE):

```python
with Scanner.open() as scanner:
    scanner.warmup()
    ir = scanner.scan(resolution=1800, mode="infrared")
```

Multi-exposure (GL128 / hardware-tested models): short colour pass, then a
**frame-adaptive** long pass (safety envelope 14k–64k, uniform at every PPI —
a margin under the AHB per-channel exposure table's 16-bit width; fallback
42000).
The SNR/IVW-merged deliverable with film-base makeup is in ``rgb``. Bracket
planes and fusion stats are on :attr:`~pyopticfilm.scanner.Scanner.last_me_debug`
(Scan Lab / audit tooling only — not part of the NegPy-facing ``ScanImage``).

```python
with Scanner.open() as scanner:
    scanner.warmup()
    image = scanner.scan(
        resolution=1800,
        mode="color",
        multi_exposure=True,
    )
    print(image.rgb.shape)  # SNR/IVW merged deliverable
    image.save_tiff("merged.tif")
    debug = scanner.last_me_debug
    if debug is not None:
        from pyopticfilm.image import save_rgb16_tiff

        save_rgb16_tiff(debug.rgb_short, "short.tif", dpi=image.dpi)
        save_rgb16_tiff(debug.rgb_long, "long.tif", dpi=image.dpi)
        print(debug.exposure_short, debug.exposure_long)  # e.g. 14000, 42000…64000
        print(debug.exposure_proposed, debug.exposure_reason)
```

Manual exposure overrides (GL128; debugging/testing only): send an exact
``REG_EXPOSURE`` value that bypasses adaptive selection and the hardware-max
clamp above — the value is written verbatim. Two limits still apply: the
24-bit register range (1–``0xFFFFFF``), and — at oversample == 1 resolutions
(e.g. 7200 dpi) — the AHB per-channel exposure table's 16-bit width
(1–65535); either is rejected with a clear error rather than clamped or
silently corrupted. All three default to ``None`` (unchanged behavior):

```python
image = scanner.scan(
    resolution=1800,
    mode="color",
    multi_exposure=True,
    me_short_exposure=14000,
    me_long_exposure=120000,  # above the normal 14k-64k envelope, on purpose
)
```

Multi-Pass (GL128): repeat the short pass (or, with `multi_exposure=True`,
both the short and long ME passes) `n_passes` times and stack the aligned
repeats for an SNR gain, without introducing any new exposure or scan-speed
value. `n_passes=1` (default) is unchanged behavior — the same Single-Pass or
Adaptive Multi-Exposure scan as today. Per-slot stacking stats (align shifts,
frames merged, outlier pixels) are on `Scanner.last_multi_pass_debug`:

```python
# Multi-Pass: 4 repeats of the single exposure, stacked.
image = scanner.scan(resolution=1800, mode="color", n_passes=4)

# Adaptive Multi-Pass: 4 repeats each of the short AND adaptive-long
# ME passes, each slot stacked, then fused exactly as today's 2-bracket ME.
image = scanner.scan(
    resolution=1800, mode="color", multi_exposure=True, n_passes=4
)
debug = scanner.last_multi_pass_debug
if debug is not None:
    print(debug.short.stack_stats.mean_confidence, debug.short.align_shifts)
```

**Common scan-mode combinations.** `multi_exposure` and `n_passes` are independent axes; a
simplified consumer UI typically only needs these four combinations, named as follows:

| Name                       | `multi_exposure` | `n_passes` |
|-----------------------------|:---:|:---:|
| Single-Pass                 | `False` | `1` |
| Multi-Pass                  | `False` | `2`–`9` |
| Adaptive Multi-Exposure      | `True`  | `1` |
| Adaptive Multi-Pass          | `True`  | `2`–`9` |

The three manual exposure overrides above are lab/debug-only —
Scan Lab (`tools/scanlab/`) is the reference implementation exposing the full, unrestricted
parameter set; NegPy is the reference implementation of the simplified 4-combination surface.

Colour + IR in one call (8200i SE; IR after the colour / ME passes):

```python
image = scanner.scan(
    resolution=1800,
    mode="color",
    multi_exposure=True,
    infrared=True,
)
# image.rgb is the merged deliverable; image.ir is HxW uint16
```

Crop (normalized coordinates on the transparency window):

```python
image = scanner.scan(
    resolution=2400,
    mode="color",
    area=(0.1, 0.1, 0.9, 0.9),  # x1, y1, x2, y2
)
```

List devices without opening:

```python
from pyopticfilm.usb.device import find_devices

for info in find_devices():
    print(info.device_id, info.product_id, info.asic_hint, info.is_supported)
```

## API overview

| Entry | Purpose |
|-------|---------|
| `Scanner.open(device_id=None)` | Open preferred or specified OpticFilm |
| `scanner.warmup(home=True, lamp=True)` | Boot ASIC, optional home + lamp |
| `scanner.scan(...)` | Run a full scan → `ScanImage` |
| `scanner.calibrate(...)` | Run shading; updates cache |
| `scanner.status()` | Read scanner status flags |
| `scanner.home()` / `scanner.park()` | Motor positioning |
| `scanner.lamp_on()` / `scanner.lamp_off()` | Lamp control (allowed on experimental models) |
| `scanner.advanced` | Low-level register read/write (bring-up) |
| `scanner.calibrator` | Direct access to calibration cache |

`ScanImage` fields: `rgb` (uint16 H×W×3), `dpi`, `device_model`, optional `ir`.
For ME scans, `rgb` is the SNR/IVW-merged deliverable (with film-base makeup).
Bracket planes live on `Scanner.last_me_debug`, not on `ScanImage`.

Scan modes: `"color"`, `"infrared"`. `"gray"` is not implemented.

`scan(..., multi_exposure=True)` is GL128 / hardware-tested models only (8200i SE
and 8100 V2). When ME is on, `rgb` is always SNR/IVW-merged (per-channel clip
confidence, soft highlight roll-off from ~80–95% FS; optional
`model.me_noise_alpha` / `me_noise_beta`). Pass `infrared=True` with
`mode="color"` for a dust/IR plane on the same `ScanImage` (8200i SE; the 8100
V2 has no IR). Inspect short/long via `scanner.last_me_debug` after the scan.

Audit a saved bracket (and optional SilverFast ME TIFF)::

```bash
PYTHONPATH=src python -m tools.audit_me_bracket short.tif long.tif --sf sf_merged.tif
```

Enable debug logging:

```python
from pyopticfilm.logging import enable_debug_logging

enable_debug_logging()
```

## Calibration

Shading runs automatically before scan when a matching cache entry exists. Force a new calibration with `scanner.calibrate(force=True)` or `scanner.scan(..., apply_calib=False)` to skip applying cached data.

The cache key includes resolution, crop geometry, and scan method (transparency vs infrared). GL128 colour shading uses ASIC-internal measurements at home; IR uses a white-only path suitable for stationary shading.

## Experimental / protocol-validated models

Code for additional OpticFilm variants is included so enumeration, model selection, SANE-derived geometry tables, and hardwareless USB traces can be exercised without hardware. These paths are **deliberately locked** for motor moves and image acquisition:

- `model.scan_ready` is `True` only for the 8200i SE and 8100 (V2); all other models stay `False`
- `Scanner._ensure_scan_ready()` blocks scan, calibrate, home, and park on non-scan-ready models
- GL128 motor moves stay disabled unless the model is scan-ready
- Protocol-validated (currently OpticFilm 8200i setup traces) is not hardware support

If you have a non-scan-ready OpticFilm and want to help validate scanning, open an issue with your exact USB IDs (`bcdDevice` matters for some models) and we can work through capture-based bring-up. How traces are recorded and compared is in [docs/scanner-validation.md](docs/scanner-validation.md).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for model-lock policy and how to specialize
GL128 siblings (8200i SE vs 8100 V2) without retargeting frozen oracles.

```bash
uv sync --all-groups
uv run ruff check .
uv run pytest -q
```

Optional PyQt6 scan lab (git checkout only — not on PyPI). From the repo root,
**Run against MOCK** is on by default; uncheck it to use a plugged-in scanner:

```bash
uv sync --group lab
uv run python -m tools.scanlab
```

Full UI walkthrough: [tools/scanlab/README.md](tools/scanlab/README.md).

USBPcap / Wireshark `.pcapng` recordings used during reverse-engineering (8200i SE
sessions, PPI ladder, bit-depth pairs, etc.) are published separately in
[pyopticfilm_captures](https://github.com/jboneng/pyopticfilm_captures). Use Scan
Lab **Open capture…** to decode them offline.

CI runs on Python 3.11–3.13 (lint + tests; no hardware in CI).

Project layout:

- `src/pyopticfilm/usb/` — enumeration, claim, Genesys USB protocol, mock/recording transports
- `src/pyopticfilm/asic/` — per-ASIC drivers (GL128, GL845, …)
- `src/pyopticfilm/device/` — per-model register/geometry tables
- `src/pyopticfilm/scan/` — geometry, calibration, scan session pipeline
- `tests/scanners/` — golden USB traces, SANE log parser
- `tools/scanlab/` — PyQt6 bring-up lab (repo only; not in the PyPI package); see [tools/scanlab/README.md](tools/scanlab/README.md)

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).

## Acknowledgements

Register and motor tables for GL845-family models are derived from the SANE `genesys` backend (see [NOTICE](NOTICE) and [docs/sane-opticfilm.md](docs/sane-opticfilm.md)). The 8200i SE (GL128) protocol was reconstructed from USB traffic captures of the Windows driver and SilverFast; it is not present in SANE.
