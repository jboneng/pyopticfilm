# SANE genesys ↔ pyopticfilm OpticFilm map

This document maps every OpticFilm model in pyopticfilm to the SANE `genesys`
backend. Scan / home / park / calibrate remain **gated** (`scan_ready=False`)
until a model is **hardware-tested**. Protocol-validated traces (see
[scanner-validation.md](scanner-validation.md)) do not flip that gate. The
GL128 models (8200i SE and 8100 V2) are capture-derived and are not completed
from SANE.

## Model → ASIC → SANE sources

| pyopticfilm model | USB ID | ASIC | SANE sensor id | Command set |
|-------------------|--------|------|----------------|-------------|
| OpticFilm 8200i SE | `07b3:1825` | GL128 | *(none)* | capture / `Gl128` |
| OpticFilm 8100 (V2) | `07b3:1824` | GL128 | *(none)* | capture / `Gl128` (no IR) |
| OpticFilm 8200i | `07b3:130d` | GL845 | `CCD_PLUSTEK_OPTICFILM_8200I` | `CommandSetGl846` (`gl846.cpp`) |
| OpticFilm 8100 | `07b3:130c` | GL845 | same sensor as 7400/8200i family | `gl846.cpp` |
| OpticFilm 7600i v2 | `07b3:0c3b` bcd `0x0605` | GL845 | alias of 8200i tables | `gl846.cpp` |
| OpticFilm 7400 v2 | `07b3:0c3a` bcd `0x0605` | GL845 | `CCD_PLUSTEK_OPTICFILM_7400` | `gl846.cpp` |
| OpticFilm 7500i | `07b3:0c13` | GL843 | `CCD_PLUSTEK_OPTICFILM_7500I` | `gl843.cpp` |
| OpticFilm 7600i v1 | `07b3:0c3b` bcd `0x0400` | GL843 | *(none)* | capture / `Gl843V1` |
| OpticFilm 7300 | `07b3:0c12` | GL843 | `CCD_PLUSTEK_OPTICFILM_7300` | `gl843.cpp` |
| OpticFilm 7400 v1 | `07b3:0c3a` bcd `0x0400` | GL843 | 7300 tables | `gl843.cpp` |
| OpticFilm 7200i | `07b3:0c04` | GL843 | `CCD_PLUSTEK_OPTICFILM_7200I` | `gl843.cpp` |
| OpticFilm 7200 v2 | `07b3:0c07` | GL843 | 7200i tables, no IR | `gl843.cpp` |
| OpticFilm 7200 | `07b3:0807` | GL842 | `CCD_PLUSTEK_OPTICFILM_7200` | `gl842.cpp` |

## SANE file anchors (calib / optical)

| Topic | SANE location | pyopticfilm |
|-------|---------------|-------------|
| Optical init / scan session (GL845) | `gl846.cpp` `init_regs_for_scan_session`, `CommandSetGl846` | `scan/session.py`, `asic/gl845.py` |
| Optical init (GL843) | `gl843.cpp` (MAXWD / `tgtime`) | `scan/session_gl843.py` |
| Optical init (GL842) | `gl842.cpp` | `scan/session_gl842.py` |
| Offset then coarse gain AFE | `genesys.cpp` `scanner_offset_calibration` / `scanner_coarse_gain_calibration` (~1386+) | `Gl845.search_afe` → `run_afe_dichotomy` |
| ADI FESET skip | `genesys.cpp` ~1403–1410 (GL845/846 when `FESET==0x02`) | `Gl845.afe_dichotomy_applicable` |
| Dark/white host shading | `genesys.cpp` `genesys_dark_white_shading_calibration`; white Y via `y_offset_calib_white_ta` | `Calibrator.run` + `compute_calib_geometry` |
| ASIC shading upload | `gl846.cpp` `send_shading_data` | **Not ported** for GL845 first pass — host stretch only; follow-up if HW needs DVDSET |
| Model geometry tables | `tables_model.cpp` OpticFilm 8200i block | `device/model_8200i.py` |
| 16-bit sample swap | sensor `swap_16bit_data` (7200i) | `ImagePipeline.decode_rgb` when `model.swap_16bit_data` |

## pyopticfilm landing sites

| Concern | Module |
|---------|--------|
| USB vendor I/O | `src/pyopticfilm/usb/protocol.py` |
| Model tables | `src/pyopticfilm/device/model_*.py` |
| DPI / exposure / dummy lookup | `src/pyopticfilm/device/sensor_lookup.py` |
| GL845/GL846 scan session | `src/pyopticfilm/scan/session.py` |
| GL843 scan session | `src/pyopticfilm/scan/session_gl843.py` |
| GL842 scan session | `src/pyopticfilm/scan/session_gl842.py` |
| GL128 scan session | `src/pyopticfilm/scan/session_gl128.py` |
| Host dark/white calib | `src/pyopticfilm/scan/calibrate.py` (non-GL128 branch) |
| ASIC DVDSET shading | `src/pyopticfilm/scan/calib_gl128.py` (GL128: SE and 8100 V2) |

## MAXWD unit differences (SANE)

These matter when sizing the ASIC line buffer:

- **GL845 / GL846**: `(line_bytes * channels) >> 2` (4-word units; genesys still multiplies by channels — preserved as-is)
- **GL843**: `(line_bytes * optical_res / full_res) >> 1` (2-word units). For OpticFilm CCD entries where optical≈full, this is `line_bytes >> 1`
- **GL842** (CCD): `line_bytes` (CIS would multiply by channels; OpticFilm 7200 is CCD)

## Intentionally not ported from SANE

- Gamma LUT upload
- Grayscale / lineart modes
- Sheet-fed document detect
- Non-Plustek genesys scanners
- Any GL128 / 8200i SE / 8100 V2 behaviour (use captures instead)
- GL845 `send_shading_data` DVDSET shading (host stretch first; revisit after 8200i HW)

## Bring-up policy

1. Keep `scan_ready=False` except for the hardware-tested set (enforced by `tests/test_multi_model.py::test_scan_ready_validate_set`).
2. Hardwareless protocol traces may be added per [scanner-validation.md](scanner-validation.md); they change the documented support level, not `scan_ready`.
3. Lab sequence for GL845 8200i: `tools/bringup_gl845_8200i.py` (open → status → lamp → home → tiny scan → park → host calib → IR). Requires `--allow-unvalidated`; does **not** flip the gate.
4. Flip **that** model’s `scan_ready` only after a successful image + park on real hardware.
5. Sibling order after the GL128 pair (8200i SE, 8100 V2): GL845 aliases (8100, 7400 v2, 7600i v2) → GL843 (incl. `swap_16bit_data` decode for 7200i) → GL842.

## Experimental models (honest status)

| Model | Protocol golden | Host calib path | `scan_ready` |
|-------|-----------------|-----------------|--------------|
| 8200i SE | capture-derived | ASIC shading | **True** |
| 8100 (V2) | capture-derived (SE sibling; no IR) | ASIC shading | **True** |
| 8200i (GL845) | setup USB golden | wired | False until HW image+park |
| Other GL845 aliases | share 8200i session | wired | False |
| GL843 / GL842 | tables only | wired | False |

## SANE register fixture workflow

See [scanner-validation.md](scanner-validation.md). Drop converted JSON under
`tests/traces/sane/8200i/`; CI compares optical regs when
`1800_rgb16_setup.registers.json` is present (`test_sane_register_fixture_if_present`).
Home/feed goldens stay separate — do not overwrite the setup golden.
