# Changelog

All notable changes to pyopticfilm are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- **GL128 frames were 24 px narrower than SilverFast at 1800 dpi and lost holder/film on the left** (8200i SE, 8100 V2, 8300i SE) ([#102](https://github.com/jboneng/pyopticfilm/issues/102)): pyopticfilm shifted the image window by 96 native clocks (STR/END 338/10706 instead of SilverFast's 242/10610) and trimmed up to 24 dark columns from the USB ENDPIXEL side, which is the image left after mirroring. The trim was added for a near-zero band that pyopticfilm, not SilverFast, produced at that edge. SilverFast's raw USB lines from the 8200i SE and 8300i SE have no such band, and a live 8200i SE scan with the current driver now shows the holder edge there instead. The trim was cutting real holder and film columns. All three models now program SilverFast's window and keep every column: 2592 px at 1800 dpi for a full frame, the same as SilverFast.

### Changed

- **GL128 default full frames are exactly one inch tall, like SilverFast: 1800 rows at 1800 ppi** instead of 1801 ([#102](https://github.com/jboneng/pyopticfilm/issues/102)). SilverFast reads one inch plus the colour line shift plus one row, and returns the inch. pyopticfilm returned whatever was left after the line shift, so from 1800 ppi upwards the frame came out 1 to 5 rows too tall (for example 1801 at 1800 ppi and 3602 at 3600 ppi). On the 8200i SE, 8100 V2 and 8300i SE, a full-frame scan with no `area` is now cut to the inch after line shift and unstagger. Scanner registers and travel are unchanged. Crops and Scan Lab's Full window keep their current height.
- **Scan Lab prescan background** is light grey instead of near-black, so dark film-holder edges stand out against the canvas. The other image tabs keep the dark background.

### Notes for integrators

- GL128 images are wider by `96 × dpi / 7200` px (16 / 24 / 48 at 1200 / 1800 / 3600 dpi) and show slightly more of the frame on the left. Colour shading cached for the old window is re-measured automatically on the first scan, because the cache is keyed by window start and width.

## [1.3.7] - 2026-10-23

### Added

- **GL128 AFE field probe** (`tools/probe_afe_fields.py`): stationary lab tool (carriage at home, motor off, calibration cache untouched) that sweeps FE indices (default `0x02` and `0x05`, any of `0x02`–`0x07` via `--fields`) with the lamp on and off and reports whether each behaves as a gain (AD9826 / 6-bit wrap at `0x40`) or an offset. `--layout planar|chunky` overrides the strip decode. Writes a CSV under `tools/scanlab/runs/afe-probe/`.
- **Calibration diagnostics bundle** (`tools/diag_calibration.py`): runs one forced colour calibration at home (no film, throwaway cache) and writes a zipped folder for bug reports: `info.json` (version, install path, model, USB serial-number string and the `0x36` in use, exposure, sensor registers, cache version and fingerprint), a full DEBUG log, and a USB trace that includes the AFE and shading strip data. A rejected calibration is logged rather than raised, so the bundle is complete either way. `--resolution` picks the dpi; `--reg ADDR=VALUE` overlays a per-scan sensor register for A/B experiments (this run only); `--force ADDR=VALUE` rewrites every write to a register on the wire, including lamp, LED and GPO writes the driver computes itself; `--fake` runs against mock USB.

### Fixed

- **GL128 AFE calibration** (8200i SE, 8100 V2, 8300i SE): the analog frontend follows the AD9826 layout. FE indices `0x02`–`0x04` are 6-bit PGA gains and `0x05`–`0x07` are 9-bit sign-magnitude offsets, probed on the 8200i SE. The driver had the two named the other way round. Fixes on top of the rename:
  - AFE strips are now always decoded as chunky RGB. The lag test often picked planar, which blended all three channels, so the search could not balance them independently.
  - The offset search stays within `0`–`+255`. It used to step into codes `0x100` and up (negative offsets), where the strip gets darker as the code rises, so it ran away to the top and hit the "pegged at max" fallback.
  - PGA gains are capped at `0x3F`. The infrared gain search used to run to `0xFF`, crossing the 6-bit wrap three times.
- **OpticFilm 8300i SE colour calibration** failing with `CalibrationError: Colour ASIC shading failed (white clipped at the rail …)` on some units ([#95](https://github.com/jboneng/pyopticfilm/issues/95), [NegPy #1444](https://github.com/marcinz606/NegPy/issues/1444)): the 8300i inherited the 8200i SE per-scan sensor/AFE timing registers (`0x06`, `0x0B`, `0x52`–`0x57`, `0x5A`/`0x5B`, `0x70`–`0x73`). SilverFast and VueScan program different values on the 8300i at every resolution, and under the SE values the white strip read about twice as hot and clipped. The 8300i SE now uses its own captured values. 8200i SE and 8100 V2 are unchanged (their captures match the shared table).
- **GL128 never set the ASIC clock** (8200i SE, 8100 V2, 8300i SE), the remaining cause of `white clipped at the rail` on 8300i SE units ([#95](https://github.com/jboneng/pyopticfilm/issues/95)): the vendor driver writes the clock through `0x8C` (indices `0x10` and `0x13`) once at boot and again before every scan pass, first the boot value, then the scan value. Boot/scan values are `0x0C`/`0x0C` on the 8200i SE, `0x0E`/`0x0C` on the 8100 V2 and `0x0E`/`0x0D` on the 8300i SE. pyopticfilm skipped both, so the scanner ran on whatever clock it powered up with. On a failing 8300i SE every register, frontend value and RAM upload otherwise matched SilverFast, yet the same AFE probe read 1.54x hotter and the white strip clipped even at the lowest gain. pyopticfilm now sends the same sequence before each colour, IR and multi-exposure pass and before `Scanner.calibrate`. Like the vendor, boot then toggles DRAM enable on `0x0B` (`0x44`/`0x4C` on the 8200i SE, `0x84`/`0x8C` on the V2 and 8300i SE), and each pass rewrites `0x0B` and `0x13` to their scan values right after the clock switch: `0x0B`'s clock bits must agree with `0x8C`. Scanning the 8300i SE on the boot value instead (`0x0E`) ran the motor fast with a rattle and stalled each pass at about 60 %. Calibration caches from earlier versions are discarded.
- **GL128 colour writes `0x37=0xC0`** like the vendor driver, instead of rewriting the `0xB0` readback when switching the IR LED off. Infrared passes are unchanged (`0xB4`, as in the vendor captures).
- **GL128 infrared AFE search** (8200i SE, 8300i SE): the IR gain search switched the IR LED off as well as the white lamp, so it measured a dark strip and drove the gains up until the IR white strip clipped. IR gains now come from a wide strip under the IR LED, the same way SilverFast does it for colour and IR.
- **GL128 infrared scans were never calibrated** (8200i SE, 8300i SE): the IR pass only looked up an IR calibration entry that normal scans never created, so it ran on the colour AFE codes with no white reference. Before each IR pass the driver now runs the IR AFE search and an IR white strip at home (about 10–15 s) and uses the white profile to flatten the IR plane. If the profile is rejected, the IR pass falls back to the colour AFE codes and the per-image flatten, as before.
- **GL128 scans paused every few seconds and rattled at 7200 dpi** on large crops (8200i SE, 8100 V2, 8300i SE) ([#98](https://github.com/jboneng/pyopticfilm/issues/98)): the per-channel exposure table for a native 7200 dpi image held the plain `REG_EXPOSURE` value, half of what SilverFast uploads on the 8200i SE and 8100 V2 (28000, with `REG_EXPOSURE` still 14000). Below 7200 dpi the table already matched the vendor. At the lower value a full-width line came out faster than USB could drain it, so the scanner buffer filled and the motor stopped and restarted. Single-pass colour and IR images at 7200 dpi on the 8200i SE and 8100 V2 now use twice `REG_EXPOSURE` (28000); the 8300i SE follows its own SilverFast rule (see the 8300i SE motor-speed entry). 7200 dpi image passes also write `0x1D=0x81` like SilverFast on all three models (pyopticfilm always wrote `0x80`); with the table alone the line still ran at about 8 ms. Manual exposure overrides and calibration strips are unchanged. Hardware-verified on the 8200i SE across the ppi ladder and on the 8100 V2 (full-frame 7200 dpi single pass at 5.56 MB/s, longest read 23 ms, no motor stops).
- **OpticFilm 8300i SE scanned at half SilverFast's speed at every resolution**, slower and lower-pitched, with motor rattle at 7200 dpi ([#98](https://github.com/jboneng/pyopticfilm/issues/98)). The AHB tables at `0x10000000`/`0x10004000`/`0x10008000`, which pyopticfilm calls per-channel exposure, are the scan motor's step periods. SilverFast programs `REG_EXPOSURE × ((0x1D & 0x1F) + 1) × asic_dpi / 14400` there: 1875 at 1800 dpi, 3750 at 3600 and 15000 at 7200. It reaches that speed along its acceleration curve (registry `CURVE=10,2500,240`, head 40404, 4028, 3016, …). pyopticfilm used the 8200i SE rule, twice that period, and started at full speed with no ramp. The 8300i SE now uploads SilverFast's table, which matches every captured image pass from 600 to 7200 dpi, colour, IR and multi-exposure. At that speed the image `LINCNT` is also SilverFast's: one unit and one bulk row per output line (1813 for a full frame at 1800 dpi, 7253 at 7200), a quarter of the 8200i SE / 8100 V2 convention pyopticfilm used to program. With the old line count the carriage travelled twice the frame height and ran into the end of travel just past halfway. 8200i SE and 8100 V2 are unchanged. Hardware-verified on the 8300i SE at 1800 dpi.

### Changed

- **GL128 image read** fills one preallocated buffer instead of a growing one plus two full copies (about 1.7 GB each for a full 7200 dpi frame). The largest image announce and bulk request is a new per-model field, `image_read_chunk_bytes`; all three models use 60 KiB (one full-width 7200 dpi line per announce). Each image pass logs a `GL128 image read:` line (MB/s, instant reads, longest read, longest gap); `PYOPTICFILM_ACQUIRE_TIMING=<path.csv>` appends per-chunk timing (`dpi,offset,bytes,read_s,gap_s`). Scan Lab prints those INFO lines to the terminal it was started from.
- **GL128 boot register `0x36` follows the firmware** (8200i SE, 8100 V2, 8300i SE), as in the vendor driver: it reads the USB serial-number string, which these scanners set to their firmware date, and uses `0x1C` for `20241113` and `0x04` for anything else. pyopticfilm always used `0x1C`. Units that report `20241113`, including every unit pyopticfilm was validated on, are unchanged. On a tested 8300i SE, `0x36` made no measurable difference to signal level.
- **GL128 multi-exposure and Multi-Pass are disabled at 7200 dpi** (8200i SE, 8100 V2, 8300i SE). On the 8200i SE the multi-exposure long pass (64000) read at about 1 MB/s, stopped after 501 of 809 MB, and the scanner dropped off USB with the carriage out of position. `Scanner.scan` and `Gl128ScanSession.run` now raise `ScanError` for `multi_exposure=True` or `n_passes > 1` at 7200 dpi before touching the scanner, and Scan Lab greys out **Multi-exposure (ME)** and **Passes** at 7200. Single-pass scans (colour, IR, manual exposure) at 7200 dpi are unaffected; every lower resolution keeps both modes. Host apps can read `multi_exposure_resolutions_dpi` / `multi_pass_resolutions_dpi` on the model (see Notes for integrators).
- **Calibration cache v12**: entries are tagged with a fingerprint of the model, its exposure, its boot registers and its per-scan sensor registers, and are only reused when that matches. A table measured with different timing (or on another model sharing the cache file) is no longer applied silently, which caused banding. Older cache files (including the AFE rename-era v10 drop) are ignored, so the first scan after upgrading recalibrates once.

### Removed

- **GL128 adaptive quiet USB drain** and the Scan Lab **Adaptive quiet drain** checkbox. The host used to sleep about one line between USB chunks to keep motor creep continuous at high PPI. The 7200 dpi pauses it was meant to hide came from the line rate outrunning USB, which the 7200 dpi exposure table and `0x1D=0x81` now fix, so image reads drain as fast as USB allows at every resolution.

### Notes for integrators

- GL128 models have two new read-only properties, `multi_exposure_resolutions_dpi` and `multi_pass_resolutions_dpi`: the resolutions where `multi_exposure=True` and `n_passes > 1` are accepted (every resolution except 7200 dpi on the 8200i SE, 8100 V2 and 8300i SE). Front ends should offer those modes only at the listed resolutions, for example `dpi in getattr(model, "multi_exposure_resolutions_dpi", ())`; models without the attributes don't support the mode. `model.multi_pass_allowed_at(dpi)` and `pyopticfilm.scan.session_gl128.gl128_multi_pass_allowed(model, dpi)` answer the same question for one resolution, and `check_gl128_multi_pass` raises the `ScanError`.
- NegPy's Plustek backend imports `DEFAULT_IMAGE_USB_PACE_S` (removed below) and builds `multi_exposure` / `max_n_passes` capabilities without a resolution; it needs updating for this release.
- `pyopticfilm.asic.gl128.DEFAULT_IMAGE_USB_PACE_S`, `Gl128.image_usb_pace_s`, `pyopticfilm.scan.session_gl128.IMAGE_USB_PACE_S` and Scan Lab's `LabTarget.quiet_usb_pace` / `with_motor_acoustic(quiet_usb_pace=...)` are gone. Setting `image_usb_pace_s` on a `Gl128` no longer has any effect.
- `AfeFrontend.gains` / `.offsets` and the cache's `afe_gains` / `afe_offsets` now mean the same registers on GL128 as on GL845: gains are `0x02`–`0x04` and offsets are `0x05`–`0x07`. `FE_GAIN_*` / `FE_OFFSET_*`, `coarse_gains_from_wide_means`, `adaptive_afe_white_target`, `AFE_OFFSET_MIN`, `COLOR_AFE_SESSION04_OFFSETS` and `AFE_WHITE_TARGET_*` replace the old names. `encode_afe_offset` / `decode_afe_offset` convert signed offsets to and from the FE code. `AFE_GAIN_MAX` (and the `AfeSearchConfig.gain_max` default) is now `0x3F`, which also applies to GL845's forced dichotomy.
- GL128 `sensor_custom_regs` and the new `asic_clock_boot_0x8c` / `asic_clock_scan_0x8c` / `asic_boot_0x0b` are leaf-divergent fields (each of `Model8200iSE`, `Model8100V2` and `Model8300iSE` declares them; `Gl128Model` requires all three). Code that drives a GL128 `Calibrator` or session directly should call `Gl128.enter_scan_clock()` first; `Gl128ScanSession.run` and `Scanner.calibrate` already do. `CalibCache` takes an optional `fingerprint`; `Calibrator` passes `calib_fingerprint(model)`. GL128 IR scans call the new `Calibrator.measure_ir_host_flatten(geometry)` instead of `find_for_scan(method="infrared")`.
- GL128 models have a new leaf-divergent field `native_image_exposure_factor` (`2` on the 8200i SE and 8100 V2; `1` on the 8300i SE, whose `channel_exposure_for` already applies the `0x1D=0x81` doubling at 7200 dpi; `1` keeps the table at `REG_EXPOSURE`): at oversample 1, `Gl128ScanSession._configure` raises the per-channel exposure table of every non-manual image pass to at least `exposure_short * native_image_exposure_factor` (`native_line_floor(model)` in `pyopticfilm.scan.session_gl128`). `channel_exposure_for` itself is unchanged.
- GL128 models have a new method `scan_motor_table(period)` (the words uploaded to `0x10000000`/`0x10004000`/`0x10008000`; flat on the 8200i SE and 8100 V2, SilverFast's acceleration ramp on the 8300i SE) and a new leaf-divergent field `image_read_chunk_bytes` (`0xF000` on all three). `image_lincnt_per_line` and `usb_image_lincnt_half_lines` moved from `Gl128Common` to the leaves (`4` / `True` on the 8200i SE and 8100 V2, `1` / `False` on the 8300i SE), so 8300i SE `ladder_lincnt_by_dpi`, `max_image_lincnt_by_feed2`, `max_lincnt_for` and `lincnt_for_travel_mm` now return SilverFast's values, a quarter of the old ones.
- `Gl128ScanSession._acquire` (and so `acquire_raw` on GL128) returns a `bytearray` instead of `bytes`. Use it as a bytes-like object (`np.frombuffer`, slicing, `len`); call `bytes()` on it only if you need an immutable copy.
- `UsbDeviceInfo.serial_number` holds the USB serial-number string, read by `UsbDeviceHandle` on open (`None` from listing or when unreadable). `Scanner` passes it to `gl128_firmware_variant(model, serial)`, which returns a GL128 model with the vendor's per-firmware `init_regs[0x36]`. Pass `model=` as before; the variant is applied on top of it.

### Contributors

- [@microdoseonthebeat](https://github.com/microdoseonthebeat) for the OpticFilm 8300i SE research and patches behind the sensor-timing, ASIC-clock, infrared calibration and calibration-cache fixes: SilverFast / VueScan USB captures from a second unit, isolating the 8300i sensor/AFE timing registers, diagnosing the uncalibrated IR channel and the dpi-only cache key, AFE probe measurements that showed the remaining 1.5× hot read without `0x8C`, first pointing out that the 8300i SE motor ran slower and lower-pitched than under SilverFast, and hardware testing ([#95](https://github.com/jboneng/pyopticfilm/issues/95), [NegPy #1444](https://github.com/marcinz606/NegPy/issues/1444)).
- [@theaino](https://github.com/theaino) for reporting and diagnosing the 8300i SE 7200 dpi pause / motor-rattle issue and the half-speed scan motor ([#98](https://github.com/jboneng/pyopticfilm/issues/98)): confirming it was 7200-only in Scan Lab, motor-audio comparisons against SilverFast, SilverFast 7200 dpi colour + IR and 1800 dpi USB captures, retesting successive clock, timing, motor-table and read-path builds, read-timing logs from crashed runs, and spotting that the carriage travelled past SilverFast's stopping point, which led to the `LINCNT` fix.
- [@TobbyTravel](https://github.com/TobbyTravel) for validating [#96](https://github.com/jboneng/pyopticfilm/pull/96) on the OpticFilm 8100 V2: a 3600 dpi colour A/B against `main` showing the ASIC-clock and sensor-timing changes leave V2 colour unchanged apart from the one-time recalibration, and a full-frame 7200 dpi scan confirming the pause fix and `0x1D=0x81` on V2.

## [1.3.6] - 2026-10-14

### Fixed

- **GL128 intermittent positioning stall** (high-pitched motor whine at scan start; the scan came back almost black, with a white band at the top that fades into the black): the second positioning feed was armed, and its slope tables uploaded, while the first feed's motor was still decelerating (vendor probe `0x21=0x04` arrives with `MOTORENB` still set). The driver now follows SilverFast's handoff: drop `MTRPWR` (`0x02=0x08`), `FEEDL=1`, then poll `0x101` until `MOTORENB` clears before any feed or image-pass `START`. A motor that stays enabled for 2 s raises `MotorTimeoutError` instead of starting the next move. Shared by all GL128 leaves; hardware-verified on the 8200i SE.

## [1.3.5] - 2026-10-13

### Added

- **OpticFilm 8300i SE** (`07b3:181f`): GL128 leaf (`Model8300iSE`) with capture-derived geometry (V2-scale `LINCNT`, PPI-dependent colour-short feed2 13040…13128), V2-matched `LPERIOD`, exposure 15000, image dummy/pixel-clock maps, shading-strip clocks (white @7200 dummy `0x26`), and CUSTOM motor slope ROM (`tables_8300i_se.py`, heads `0x32BB` / `0x846A`). IR capable. Hardware-tested (`scan_ready=True`); lock oracles at `tests/model_lock/opticfilm_8300i_se/`. Setup goldens at `tests/traces/python/8300i_se/`. See `docs/gl128-model-comparison.md` §10.
- **OpticFilm 7600i v1 (GL843)** (`07b3:0c3b`, bcdDevice `0x0400`): capture-derived driver that replays SilverFast scan sequences (1440 / 3600 / 7200 dpi colour, 3600 / 7200 dpi infrared) with live AFE/shading calibration per scan. Hardware-tested (`scan_ready=True`); lock oracles at `tests/model_lock/opticfilm_7600i_v1/`. See [docs/opticfilm-7600i-v1.md](docs/opticfilm-7600i-v1.md). The GL845 7600i v2 alias is unchanged.

### Changed

- **GL128 sibling catalog**: `scan_ready`, `exposure_lperiod` / `exposure_short`, and image `pixel_clock_*` / `dummy_by_dpi` are leaf-divergent fields (SE and V2 keep their previous values). Scan Lab capture decode keeps the selected GL128 leaf instead of always forcing the 8200i SE.
- **GL128 slope uploads** read `model.slope_table_slow()` / `slope_table_fast()` instead of hard-importing the SE ROM, so the 8300i SE can use its CUSTOM tables without retargeting SE/V2.
- **8300i SE full-frame feed2** follows the capture PPI map via `feed_to_scan_steps_for_dpi` (ME-long → 13128); SE/V2 still use a constant `feed_to_scan_steps`.
- **Scan Lab capture ME labels** use the selected leaf’s `exposure_short` (and treat any higher non-IR exposure as ME-long), so 8300i 15000 / ~60000 brackets label correctly.
- **8300i SE mid-ladder** (expanded captures): correct 900/1800 image dummy, pixel clock, feed2, and shading-strip clocks (nearest-band fills were wrong); promote measured 150/600/900/1800 rows. Cold-boot startup matches SE `INIT_REGS` (116/116).
- **8300i SE edge artifacts** (Scan Lab raw TIFF analysis): round `ld_shift` scaling (clears ~1-line G/B residual at 1440) and host column unstagger `(1, 0)` at 1440/2400 for period-2 sawtooth. ASIC `STAGGER` bit stays clear.
- **8200i SE edge artifacts** (Scan Lab raw TIFF analysis): same host assemble fixes as 8300i — round `ld_shift` and column unstagger `(1, 0)` at 1440/2400. V2 unchanged; no 8300i register-table port. ASIC `STAGGER` bit stays clear.
- **Scan validation set** is now **8200i SE + 8100 (V2) + 8300i SE + 7600i v1**.
- OpticFilm 7600i v1: the main scan, colour and infrared, by default is sampled once vertically, with one CCD dummy line at 7200 dpi to avoid scanner stalls (zero at lower resolutions), a two-line buffer threshold and no backtracking.
- OpticFilm 7600i v1: shading gains are computed from each scan's white frame; the recorded gains left ~1 % unevenness across the width.
- OpticFilm 7600i v1: the scan window is the full area, 36.61 mm wide from the first lit sensor pixel (was SilverFast's 36.15 mm).
- `MODEL_7600I_V1` moved to `device/model_7600i_v1.py` (was a 7500i SANE-table alias).

### Fixed

- **GL128 stale ASIC shading across DPI**: `ensure_colour_asic_shading` no longer skips blob upload when `asic_shading_ready` is set from a prior narrower window (e.g. 1200 → 1440/2400). That left DVDSET coefficients short of the image width and painted END-side columns (displayed left after `mirror_x`) solid black.
- **8300i SE 1440/2400 assemble crash**: ladder `LINCNT` override no longer folds host Y-unstagger spare into `geometry.lines`; `apply_y_stagger` clamps short GL128 buffers like `apply_line_shifts` instead of raising.
- `default_frame_crop_norm` (called by NegPy for every scan-ready model) no longer fails on the OpticFilm 7600i v1 with "no attribute 'feed_to_scan_top_steps'": models other than the GL128 ones get the whole window.
- USB open no longer sets the configuration of an already configured device. On the OpticFilm 7600i v1 this reset only the host's bulk data toggles, so every second session lost its first packet and timed out.

### Notes for integrators

- GL128 log `host_stretch=` means **radiometric** host dark/white calib after ASIC DVDSET reject (not geometric downsample). Synthetic repro of the 8300i near-horizontal green edge fringe is in `tests/test_host_calib_edge_fringe.py`; see `docs/gl128-model-comparison.md` §10.8.
- OpticFilm 7600i v1 calibrates live inside each scan (no shading cache); `scanner.calibrate()` is unsupported on that leaf.

### Contributors

- [@bazfp](https://github.com/bazfp) for the OpticFilm 7600i v1 (GL843) capture-derived driver, SilverFast USB captures / reverse engineering, live AFE and shading calibration, and [OpenOptic](https://github.com/bazfp/OpenOptic) research ([#81](https://github.com/jboneng/pyopticfilm/pull/81)).
- [@tsoulard](https://github.com/tsoulard) for OpticFilm 7600i v1 hardware testing (including NegPy), banding / calibration feedback, and SilverFast comparison captures.
- [@theaino](https://github.com/theaino) for requesting OpticFilm 8300i SE support ([#78](https://github.com/jboneng/pyopticfilm/issues/78)), early hardware testing, and SilverFast / IR capture uploads.
- [@xmx1024](https://github.com/xmx1024) for OpticFilm 8300i SE hardware validation (NegPy and Scan Lab), fringe / sawtooth regression reports, raw TIFF and run captures, and the full colour / IR / crop / ME sign-off matrix.

## [1.3.4] - 2026-09-25

### Added

- **Cross-model GL128 safeguards**: required-field `Gl128Model` contract, sibling-diff catalog (`GL128_DIVERGENT_FIELDS`), and 8100 V2 model-lock oracles so a fix for one hardware-tested GL128 model cannot silently retarget the other. Contributor policy is in `CONTRIBUTING.md`.
- **GL128 setup goldens**: Mock-USB register programs for 8200i SE and 8100 V2 at 1200 / 1800 / 7200 dpi (`tests/traces/python/8200i_se/`, `tests/traces/python/8100_v2/`). Regenerate with `python tools/dump_gl128_setup_trace.py`.
- **Scan Lab Forensic tab**: guided evidence recording, live anomaly detection, USB timeline (Event Inspector, motor/lamp state lanes, duration brackets), Known/Unknown values panel, and a Reference page backed by a confidence-tagged register/bit catalog (`tools/register_reference.py`) for GL128 SE/V2 and GL845. Timeline milestones cover slope-table identity (FAST/SLOW/CUSTOM), feed timing, and LPERIOD/EXPOSURE/pixel-clock.
- **Scan Lab headless compare**: `list-runs` and `compare` on `python -m tools.scanlab.cli` wrap the same baseline-diff / AI bug-report path as the GUI Run browser (no Qt).
- **GL128 three-model comparison**: capture-backed tables of registers, timings, FEEDL, and geometry for 8100 V2, 8200i SE, and 135i (`docs/gl128-model-comparison.md`), with a same/different/unknown verdict per row.
- **GL128 Multi-Pass**: `Scanner.scan(n_passes=1..9)` repeats an already-validated exposure and stacks the aligned repeats. `n_passes=1` is unchanged. With `multi_exposure=True`, both the short and adaptive-long passes are stacked (Adaptive Multi-Pass). `infrared=True` with `n_passes>1` raises `ScanError`. Per-slot stats are on `Scanner.last_multi_pass_debug`. Scan Lab has a Multi-Pass N control (disabled when IR is on) and the headless CLI matches (`n_passes`, `align_passes`).

### Changed

- **GL128 adaptive ME long clamp** is **14000–85000** on both 8200i SE and 8100 (V2), except **14000–64000** at 7200 dpi (oversample 1), where the 16-bit AHB exposure table would wrap a higher value. The adaptive selection floor remains 42000. Adaptive is the only automatic long-exposure mode.
- **8100 V2 model class** no longer subclasses the 8200i SE dataclass. Shared identical GL128 tables and helpers live in `device/gl128_common.py`; capture-proven divergences are declared on each leaf.
- **8100 V2 positioning slopes** are slow-then-fast, matching the 8200i SE. The `use_slow_final_positioning_feed` flag is gone. Confirmed on V2 hardware.
- **Register reference catalog**: V2 `REG_LPERIOD` at 7200 dpi and `REG_DEPTH_A`/`REG_DEPTH_B` image/shading pairs confirmed against a fresh 8100 V2 capture set; documents the V2 cancel/park recipe matching SE session 08. `0x1D` is documented as a hazard whose real pattern is shading-vs-image (not "7200 dpi image pass only"). The opaque boot/scan blast was re-checked across ME/IR/crop captures.
- **Scan Lab headless CLI** `scan` accepts `--crop`, `--multi-exposure`, and `--save-tiff-dir`.

### Fixed

- **ME AHB exposure table**: `exposure_table()` raises `ValueError` for a channel word outside 0–65535 instead of silently wrapping it. A wrapped table desynced from 24-bit `REG_EXPOSURE` and jammed the motor at 7200 dpi ([#66](https://github.com/jboneng/pyopticfilm/issues/66)).
- **8200i SE full-frame origin**: `feed_to_scan_steps` is **13128** (true scan-window top). The old 13704 overran the window at high DPI and aborted full-frame scans ([#67](https://github.com/jboneng/pyopticfilm/issues/67)).
- **8100 V2 ladder crop height**: `ladder_lincnt_by_dpi` is V2's own table. The shared SE table was too short because the V2 ladder crop starts 432 feed steps earlier.
- **Calibration cache**: a corrupt or truncated cache is treated as empty instead of raising out of `Calibrator` construction. Saves are atomic (write temp, then replace).
- **AFE search errors**: a `TypeError` raised inside measurement is no longer swallowed and replaced with table-default calibration.
- **Scan Lab worker threading**: connect `request_*` signals after `moveToThread` so queued slots (prescan, scan, Forensic poll) run on the worker thread.

### Removed

- **GL128 priming pass**: the discarded first-scan prime, `gl128_prime`, and the Scan Lab / CLI priming controls. Both models already defaulted it off, and it does not appear in vendor captures.
- **Fixed ME long-exposure mode**: `me_exposure_mode`, `fixed_long_exposure()`, and Scan Lab's "Fixed 42k long" control. A static 42000 long exposure is the adaptive floor.

### Contributors

- [@TobbyTravel](https://github.com/TobbyTravel) for the Scan Lab Forensic tab and register reference catalog, Forensic timing milestones/timeline UX, headless `compare` and scan flags, worker threading fix, 8100 V2 slope-table order, ladder `LINCNT`, priming removal, 8200i SE scan-window top, and the `0x1D` register-catalog correction.

## [1.3.3] - 2026-08-30

### Added

- **Model-lock tests** (`tests/model_lock/`): frozen 8200i SE driver-path oracles (geometry, crop mapping, dummy trim, slope-table feed order, ME configure, Scan Lab window kwargs). Other-model fixes must specialize that model rather than retarget these asserts. Run with `uv run pytest -m model_lock`.

### Fixed

- **ME short/long width mismatch**: content-aware ENDPIXEL dummy trim could drop a different column count on short vs long (and IR) because edge DN differs — especially at 3600 dpi where the cap is larger. Multi-pass now discovers the drop on the short/first colour plane and reuses it for long and IR assemble so merge/align see matching widths. Single-pass stays content-aware.

## [1.3.2] - 2026-08-30

### Fixed

- **Scan Lab Prescan crop**: a click or too-small rubber-band no longer clears a committed crop (right-click to clear). Scan builds the hardware window on the GUI thread and logs `crop applied` vs `full window` so a dropped crop cannot silently scan the safe window (including ME/IR passes).
- **Scan Lab Prescan crop X mapping**: the rubber-band is in mirrored display space. Mapping now flips X back to TA so Color short / ME / IR match the box (cropping the right film base no longer cuts the left).
- **Left-edge frame clip**: a fixed ENDPIXEL dummy drop was removing 16/24 px of film gate after `mirror_x`. Assemble now trims only columns that look like USB dummy, capped at the old width.
- **8200i SE horizontal framing**: image STR/END are shifted by 96 native clocks (`optical_end_inactive_native`) toward ENDPIXEL so dummy-trim + `mirror_x` no longer clips the film gate on the left while showing extra holder on the right. USB span / `line_bytes` stay the same; shading still tracks the image window.
- **8200i SE positioning slope tables**: SilverFast uploads `SLOPE_TABLE_SLOW` for the first (reference) feed and `SLOPE_TABLE_FAST` for the second (final positioning) feed — 39/39 capture pairs. pyopticfilm previously used the fast ramp for both. The V2-only `use_slow_final_positioning_feed` flag is no longer on `Model8200iSE` (8100 V2 still uses fast-then-slow).

### Changed

- **8200i SE priming default**: `default_gl128_prime` is now `False` (same as the 8100 V2). `Scanner.scan()` with `gl128_prime` unset skips the discarded first-scan AGOHOME-park pass. Explicit `gl128_prime=True` still primes.

### Contributors

- [@TobbyTravel](https://github.com/TobbyTravel) for default priming off on the 8100 V2 and for scoping the slow-second-feed slope-table fix to V2.

## [1.3.1] - 2026-08-29

### Added

- **Manual exposure overrides** (GL128): `Scanner.scan(single_pass_exposure=..., me_short_exposure=..., me_long_exposure=...)` send an exact `REG_EXPOSURE` for the retained single-pass, ME short, or ME long pass, bypassing adaptive selection, PPI clamp, and `me_hardware_max_exposure`. `me_long_exposure` takes precedence over `me_exposure_mode`. Values are validated for the 24-bit register range (1–`0xFFFFFF`); out-of-range values raise `ValueError`. All three default to `None` (unchanged behavior) and never apply to the discarded GL128 priming pass. Scan Lab exposes matching **Manual exposure overrides** fields.
- **GL128 priming-pass override**: `Scanner.scan(gl128_prime=False)` skips the discarded first-scan AGOHOME-park pass for that call (debug/testing only — expect ~30 px of first-scan position drift). Skipping does not mark the scanner as primed, so a later call without the override still primes normally. `ScanStatus` / `on_status` gains `"prime_skipped"`. Defaults to `True` (unchanged behavior). Scan Lab **Disable priming pass (debug)** applies to Prescan and Scan.

### Fixed

- **Cropped 8200i SE ENDPIXEL dummy suffix**: STR/END are computed in native 7200 dpi clocks (session 03 origin STR 242 / END 10610; same crop keeps STR/END at 1800 and 3600). The last ~24 output columns at 1800 (96 native clocks; image left after `mirror_x`) are a per-line USB dummy suffix plus the invert-white transition — shrinking `ENDPIXEL` does not remove them. Decode drops that fixed width after shading; USB `line_bytes` stays locked to the programmed span. Heuristic post-decode edge trim is not used (pcap 7200 URB-wider-than-span trim stays). IR column flatten still uses inset edge levels and a gain cap so residual edge samples cannot blow up to ~56k. Pass registration fills shift borders from interior columns instead of replicating padding (which widened the IR bright band and caused NegPy ICE over-repaint). Scan Lab surfaces IR align shift in the status bar / IR caption.
- **OpticFilm 8100 (V2)** geometry/timing: five constants inherited from the 8200i SE without V2 capture proof are now V2-specific — `feed_to_scan_steps` **13128** (was 13704; fixes ~1 mm clipped at the top of full-frame scans), 7200 dpi `LPERIOD` **16035** (was 15963), 7200 white-shading strip dummy clock **0x10** (was 0x17), `max_image_lincnt_by_feed2` **{13128: 29012}** (was SE preview entry 4836), and `ladder_feed2_steps` **13128** (was SE 13560).
- **GL128 consecutive-scan AFE strip timeout**: after an image pass, stationary AFE START could hang for 5s at status `0xcd` (home, buffer empty, lamp on, motor still enabled). Scan Lab reported `AFE strip: no stationary data ready within 5s (last status=0xcd)` — including 1800→3600 with the same crop (DPI cache miss remesures calib). Stationary acquires now abort START, confirm SCAN/motor-off from hardware (park leaves `0x02` armed), program session-03 dark-strip clocks, write SCAN absolutely, and retry START once. Colour shading remesure reuses existing AFE codes when the last search was colour.

### Changed

- GL128 AFE zero-collapse recovery logs now name which offset/gain components collapsed, which fallback was used, and warn when image colour may be degraded.

### Contributors

- [@TobbyTravel](https://github.com/TobbyTravel) for OpticFilm 8100 (V2) capture-derived geometry/timing fixes and for GL128 manual exposure overrides, priming-pass skip, and improved AFE zero-collapse diagnostics.

## [1.3.0] - 2026-08-27

### Added

- **GL128 first-scan priming**: before the first retained scan after open, run a discarded small pass (default **600 dpi**, area `(0, 0, 1, 0.12)`) so image-pass `AGOHOME` parks the carriage for repeatable start position. Override with `POF_GL128_PRIME` (`full` or `<dpi>:x0,y0,x1,y1`).
- **Adaptive ME long exposure** (GL128): after the short colour pass, choose a frame-specific long `REG_EXPOSURE` from RGB dense percentiles, then clamp through a separate safety envelope (42k–85k, max ratio 7×). Failures fall back to fixed 42000. Opt out with `me_exposure_mode="fixed"`.
- Scan Lab **Fixed 42k long (A/B)** checkbox; USB/status log shows proposed / selected long exposure and clamp reason.
- `MeScanDebug.exposure_proposed` / `exposure_reason` for lab observability.
- Optional `on_status` callback on `Scanner.scan` and exported `ScanStatus` (`"priming"` / `"scanning"`) so hosts can show GL128 priming; Scan Lab status bar and USB log surface it.

### Fixed

- GL128 discarded priming pass always forces `geometry=None`, `apply_calib=False`, and `mode="color"`, so hosts that pass bring-up `geometry` (e.g. Scan Lab) no longer stretch the prime into a full request-PPI shading+scan cycle.
- GL128 adaptive quiet USB drain now sleeps the full `LPERIOD` deficit (plus a small lag) instead of capping at 3 ms/line, so 7200 dpi image creep is no longer ~15% ahead of the ASIC line clock.

### Changed

- GL128 multi-exposure colour-long `REG_EXPOSURE` is clamped by PPI (8200i SE + 8100 V2): max **42000** at 7200 dpi (SilverFast known-good); **14000–85000** at other resolutions.
- PyPI development status classifier is now **4 - Beta** (was **3 - Alpha**).
- README now points the "Latest release" link at [v1.3.0](https://github.com/jboneng/pyopticfilm/releases/tag/v1.3.0).
- README and `docs/` now document the hardware-tested set as **8200i SE + 8100 (V2)** (and distinguish GL845 8100 `07b3:130c` from GL128 8100 V2 `07b3:1824`).
- Comments, docstrings, and user-facing errors now describe the two-model scan-ready set; Scan Lab README matches. Internal bring-up helper renamed `is_opticfilm_8200i_se` → `is_gl128_opticfilm`.
- ME long-pass pixel clocks are selected via an explicit long-pass flag (not only `exposure >= 42000`); `REG_EXPOSURE` is hard-clamped to `me_hardware_max_exposure` at configure time.

### Contributors

- [@TobbyTravel](https://github.com/TobbyTravel) for GL128 first-scan priming and for OpticFilm 8100 (V2) exposure-ladder research showing `REG_EXPOSURE` has no hardware ceiling and that the multi-exposure long bin should be raised toward film-dependent ~56–64k targets (not a 42k limit).

## [1.2.0] - 2026-08-24

### Added

- **OpticFilm 8100 (V2)** (`07b3:1824`) support: GL128 sibling of the 8200i SE, hardware-validated for single-pass and multi-exposure colour scans. The 8100 V2 has no infrared channel / iSRD, so `infrared=True` and `mode="infrared"` raise a clear `ScanError` (multi-exposure short+long bracket remains available).
- `Model8100V2` / `MODEL_8100_V2` (subclass of `Model8200iSE`; register tables, geometry and motor clamps inherited from the capture-validated SE).
- IR capability guard in `Gl128ScanSession.run` for models with `supports_infrared=False`.
- `examples/scan.py`: barebones interactive Python CLI example for enumerating scanners, selecting a device, choosing PPI, optionally enabling ME / IR where supported, and saving 16-bit TIFF output.

### Changed

- Scan validation set is now **8200i SE + 8100 (V2)**; tests updated from "SE only" to the two-model validated set.
- README hardware support docs now cover the newly validated 8100 V2 path.

### Contributors

- [@TobbyTravel](https://github.com/TobbyTravel) for the OpticFilm 8100 (V2) enablement, validation updates, version bump, and release documentation polish.

## [1.1.2] - 2026-08-22

### Added

- **Multi-exposure (ME) scanning** for OpticFilm 8200i SE (GL128): short and long colour passes with host SNR/IVW merge into the deliverable `ScanImage.rgb`.
- **`MeScanDebug`** and **`Scanner.last_me_debug`** for lab-only bracket planes, fusion stats, and pass alignment shifts (not part of the public `ScanImage` type).
- **Scan Lab** (`tools/scanlab/`): PyQt6 bring-up GUI (mock or real SE), prescan crop, IR pass, ME tabs, USB log, pcap decode, calib cache controls, and adaptive quiet-drain toggle.
- **Adaptive quiet USB drain** default on GL128 (`DEFAULT_IMAGE_USB_PACE_S`, line-aligned throttle in bulk acquire).
- **`tools/audit_me_bracket`**: bracket TIFF audit; **`--align`** reports estimated pass shift and post-align luma residual.
- **Mock scanner tests** and expanded geometry / pipeline test coverage.
- **`opencv-python-headless`** in the `lab` dependency group for ME/IR pass registration in Scan Lab.

### Fixed

- **Scan orientation** on `mirror_x` models (8200i SE): left–right correction in `ImagePipeline.assemble()`; prescan crop no longer applies a compensating X flip.
- **ME shadow colour fringes** from short/long misregistration: improved OpenCV phase-correlation alignment (AREA downsampling, ROI refinement), merge guard at misaligned edges, and chunked IVW merge to avoid float32 OOM at 3600+ dpi.
- **High-PPI memory use** in the image pipeline (integer oversample sums, chunked host calib / film-base makeup / border clamp).
- **8200i SE IR scans**: correct colour DVDSET handling, flattened green IR plane on `ScanImage.ir`, grayscale preview in Scan Lab.
- **Scan Lab**: crop UI limited to Prescan tab; missing `busy_changed` worker signal; effective crop status in status bar.

### Changed

- **`ScanImage`** slimmed to NegPy-facing fields only (`rgb`, `dpi`, `device_model`, optional `ir`). ME bracket data moved to `Scanner.last_me_debug`.
- ME short/long planes stay **linear** on the bracket; film-base makeup runs **once** on the merged deliverable.
- Non-SE bring-up: short Y travel clamps, USB planar RGB toggle, and SANE-aligned session improvements for experimental models.

### Notes for integrators

- **Forward-only orientation fix:** scans saved before 1.1.2 may still appear mirrored on disk; rescan if orientation matters.
- **NegPy / other apps:** consume `ScanImage.rgb` / `ir` as returned; do not apply an extra `[:, ::-1]` flip or prescan crop mirror compensation.
- **Breaking:** removed `ScanImage` ME fields (`rgb_short`, `rgb_long`, `exposure_*`, `merge_*`, `align_shift_*`). Use `Scanner.last_me_debug` for bracket inspection.

## [1.1.1] - 2026-08-12

Prior release. See the [v1.1.1](https://github.com/jboneng/pyopticfilm/releases/tag/v1.1.1) tag for details.
