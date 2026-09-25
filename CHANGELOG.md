# Changelog

All notable changes to pyopticfilm are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- **Cross-model GL128 safeguards**: required-field `Gl128Model` contract, sibling-diff catalog (`GL128_DIVERGENT_FIELDS`), and 8100 V2 model-lock oracles so a fix for one hardware-tested GL128 model cannot silently retarget the other. Contributor policy is in `CONTRIBUTING.md`.
- **GL128 setup goldens**: Mock-USB register programs for 8200i SE and 8100 V2 at 1200 / 1800 / 7200 dpi (`tests/traces/python/8200i_se/`, `tests/traces/python/8100_v2/`). Regenerate with `python tools/dump_gl128_setup_trace.py`.
- **Scan Lab Forensic tab**: guided evidence recording, live anomaly detection, USB timeline (Event Inspector, motor/lamp state lanes, duration brackets), Known/Unknown values panel, and a Reference page backed by a confidence-tagged register/bit catalog (`tools/register_reference.py`) for GL128 SE/V2 and GL845. Timeline milestones cover slope-table identity (FAST/SLOW/CUSTOM), feed timing, and LPERIOD/EXPOSURE/pixel-clock.
- **Scan Lab headless compare**: `list-runs` and `compare` on `python -m tools.scanlab.cli` wrap the same baseline-diff / AI bug-report path as the GUI Run browser (no Qt).
- **GL128 three-model comparison**: capture-backed tables of registers, timings, FEEDL, and geometry for 8100 V2, 8200i SE, and 135i (`docs/gl128-model-comparison.md`), with a same/different/unknown verdict per row.

### Changed

- **GL128 adaptive ME long clamp** is **14000–85000** on both 8200i SE and 8100 (V2), except **14000–64000** at 7200 dpi (oversample 1), where the 16-bit AHB exposure table would wrap a higher value. The adaptive selection floor remains 42000.
- **8100 V2 model class** no longer subclasses the 8200i SE dataclass. Shared identical GL128 tables and helpers live in `device/gl128_common.py`; capture-proven divergences are declared on each leaf. Scan behaviour is unchanged.
- **Register reference catalog**: V2 `REG_LPERIOD` at 7200 dpi and `REG_DEPTH_A`/`REG_DEPTH_B` image/shading pairs confirmed against a fresh 8100 V2 capture set; documents the V2 cancel/park recipe matching SE session 08.

### Fixed

- **Scan Lab CLI priming**: headless `scan` no longer forces `--gl128-prime` on by default; omit both flags to use the model default (V2 off), or pass `--gl128-prime` / `--no-gl128-prime` explicitly.
- **Scan Lab worker threading**: connect `request_*` signals after `moveToThread` so queued slots (prescan, scan, Forensic poll) run on the worker thread.

### Contributors

- [@TobbyTravel](https://github.com/TobbyTravel) for the Scan Lab Forensic tab and register reference catalog, Forensic timing milestones/timeline UX, headless `compare`, CLI priming default fix, worker threading fix, and fresh 8100 V2 capture-backed register-reference confirmations.

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
