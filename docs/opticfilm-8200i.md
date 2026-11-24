# OpticFilm 8200i (GL845)

`07b3:130d`. Genesys Logic GL845. This is not the
[8200i SE](gl128-model-comparison.md) (`07b3:1825`), which is a GL128 scanner on a
different driver path. To check an attached unit:

```bash
python -c "from pyopticfilm.usb.device import list_devices; [print(d.device_id, hex(d.bcd_device), d.asic_hint) for d in list_devices()]"
```

The model is `MODEL_8200I` in `device/model_8200i.py`, on the shared `asic/gl845.py`
/ `scan/session.py` path. The class defaults are the SANE GL846-derived tables
(the [7600i v2](opticfilm-7600i-v2.md) is built from them); the `MODEL_8200I`
instance adds overrides taken from SilverFast USB captures of a real unit and
from hardware testing. It is hardware-tested (`scan_ready=True`), with lock
oracles in `tests/model_lock/opticfilm_8200i/`. `Scanner.open()` scans,
calibrates and homes it without a lab override.

Hardware testing was done by [@moritzglb](https://github.com/moritzglb) on one
unit (bcdDevice `0x0605`) and is recorded in
[issue #181](https://github.com/jboneng/pyopticfilm/issues/181).

## Capabilities

| | |
|---|---|
| Colour | 900, 1800, 3600, 7200 dpi, 16-bit linear RGB, full frame (TA window 36.33 × 25.0 mm, e.g. 5136 × 3543 at 3600 dpi) or `area=` crop |
| Infrared | `mode="infrared"`, or `infrared=True` with colour (sequential colour → IR passes, IR aligned to RGB) |
| Multi-exposure | `multi_exposure=True`. Short pass at LPERIOD 14000, long pass up to 42000 |
| Exposure | Fixed LPERIOD 14000, or `single_pass_exposure=` as a manual LPERIOD clamped to 14000–42000. No auto-exposure prescan yet |
| Not implemented | `mode="gray"`, multi-pass averaging (`n_passes>1`), DNG |

## What was tested on hardware

| Check | Result |
|---|---|
| Open, status, init | Pass |
| Home and park | Pass. Every reverse home seek reached home in about 0.8 s |
| Calibration (dark, white, gain search) | Pass at 900, 1800, 3600 and 7200 dpi. Colour gains settle at about 36 / 21 / 32 |
| Full-frame colour | Pass at 900, 1800, 3600 and 7200 dpi; the carriage returned home after each |
| Colour + infrared | Pass at 1800 dpi, full frame. Infrared gains settle at about 51 / 54 / 53 |
| Cancel mid-scan | Pass. The carriage came home and the next run found it there |
| Multi-exposure | **Not run on hardware.** It uses the 7600i v2 code path with the conservative 42000 long-pass limit |

The sign-off rests on the tester's run logs and one returned image strip. The
full-frame output files were too large to attach to the issue, so framing and
image quality over the whole frame were judged by the tester, not reviewed here.

## Motion

- **Feed to the film:** one fast feed with scanning off (`0x02` FASTFED), on
  SilverFast's 256-step table (64102 → 625). It covers 3639 steps and stops 400
  steps short of the frame; the image pass feeds the rest on the scan slope.
  The fast feed homes first if the carriage is not at home, and does not start
  if homing fails.
- **Return after a scan:** image passes set AGOHOME, so the ASIC brings the
  carriage home by itself when the scan ends, also after a cancel or a failed
  read.
- **Homing:** the reverse home seek (`0x02 = 0xB4`), as on the 7600i v2. A
  forward AGOHOME home capture was tried first and never moved the carriage; it
  remains available as the `forward_home` model option.
- **Stationary captures:** dark frames run with motor power off (`0x02 = 0`,
  FEEDL 1), as SilverFast takes them. With motor power on, this scanner moves
  one line per scanned line even when no feed was started (about 30 mm during
  the 1024-line dark).

## Calibration

Host-side dark and white flat-field, as on the 7600i v2:

- White strip on the open light path 1.5 mm from home (`calib_white_home_y_mm`).
  The position is the 7600i v2 measurement; SilverFast calibrates at 0–0.2 mm.
- 1024-line dark frame, taken at home with the motor off.
- White-strip gain search to a 60000 target (`afe_white_target`). The table
  gains (40 / 32 / 40) saturate the white strip and are only the starting point.

## Register differences from the 7600i v2

| Register | 8200i | 7600i v2 |
|---|---|---|
| `0x58` / `0x59` (sensor sampling) | `0x2A` / `0x72` | `0x31` / `0x79` |
| `0x5F`, `0x6A` | `0x01`, `0xFF` | computed (`0x66`) |
| `0x67` / `0x68` | `0x80` | `0x7F` |
| `0x03` bit 7 (lamp watchdog) | cleared for the image pass | always set |
| `0x6C` | bit 0 not set (see [Open work](#open-work)) | bit 0 set (`0x4D`) |

## Open work

- **Multi-exposure on hardware.** Run it, then tune the long-pass limit and
  exposure ratio from measurements.
- **Auto-exposure prescan** is off (`auto_exposure_target=None`); the 7600i v2
  target has not been checked on this model.
- **Pass-to-pass position.** One colour + infrared run measured a 6-line
  offset between the two passes at 1800 dpi (about 0.085 mm); another measured
  none. The step counter also ends 3.5 % above the requested fast-feed distance
  (15066 against 14556). `fast_feed_offset_steps` is 0 until this is measured.
- **`0x6C` readback.** The register reads back `0xFF` and the scan-start update
  writes that value back, so it does not stay at SilverFast's `0x4C`. No effect
  was seen on hardware.
- **White reference gradient.** The white strip reads about 20 % brighter on one
  side than the other; the flat-field removes it.
- **Not tested:** a cold power-on start and a start with the carriage far from
  home.
- **Not implemented:** SilverFast samples Y at twice the X resolution and
  applies shading in the ASIC; pyopticfilm samples 1:1 and flat-fields on the host.
