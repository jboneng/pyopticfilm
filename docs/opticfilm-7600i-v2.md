# OpticFilm 7600i v2 (GL845)

`07b3:0c3b`, bcdDevice `0x0605` (1541). Genesys Logic GL845. Same USB ID as the
[7600i v1](opticfilm-7600i-v1.md) (bcdDevice `0x0400`, GL843, a different driver).
`model_for_device()` routes by bcdDevice. To check an attached unit:

```bash
python -c "from pyopticfilm.usb.device import list_devices; [print(d.device_id, hex(d.bcd_device), d.asic_hint) for d in list_devices()]"
```

A v2 prints `0x605 GL845`, a v1 `0x400 GL843`.

The model is `MODEL_7600I_V2` in `device/select.py`: an instance of the 8200i model
class (`device/model_8200i.py`) with v2-specific overrides. It shares the SANE
GL846-derived 8200i register, sensor and motor tables and the `ScanSession`
(`scan/session.py`) path. It is hardware-tested (`scan_ready=True`), with lock
oracles in `tests/model_lock/opticfilm_7600i_v2/`. `Scanner.open()` scans,
calibrates and homes it without a lab override.

Unless marked otherwise, every number below was measured on one physical 7600i v2.

## Capabilities

| | |
|---|---|
| Colour | 900, 1800, 3600, 7200 dpi, 16-bit linear RGB, full frame (TA window 36.33 × 25.0 mm, e.g. 5136 × 3543 at 3600 dpi) or `area=` crop |
| Infrared | Same ladder. `mode="infrared"`, or `infrared=True` with colour (sequential colour → IR passes, park between, IR aligned to RGB) |
| Multi-exposure | `multi_exposure=True` (+ optional IR). Short pass at LPERIOD 14000, adaptive long pass up to 65000, shared SNR/IVW merge. Brackets on `Scanner.last_me_debug` |
| Exposure | Single-pass auto exposure (prescan), or `single_pass_exposure=` as a manual LPERIOD clamped to 14000–65000 |
| Not implemented | `mode="gray"` (see [B&W](#black-and-white-film)), multi-pass averaging (`n_passes>1`), DNG |

## Physical layout along Y

The light path and sensor are fixed. The motor moves the film holder, and the home
sensor marks the holder's home position. This is a linear profile at 900 dpi,
measured from home:

| Y from home | What the sensor sees | Level at table AFE gains (R/G/B) |
|---|---|---|
| 0.5–5 mm | Open light path, past the holder end | 58k / 64k / 65.5k (B saturated), ~5% column variation |
| 6.6–28.5 mm | Black holder plastic | ~1.7k / 1.8k / 2.8k |
| 29–53 mm | First frame window (film) | Scene-dependent |

The SANE TA geometry starts the film area at `y_offset_ta_mm = 28.5`. SANE's white
calibration strip (`y_offset_calib_white_ta = 0`) therefore lands on the holder
edge. That produced dim, uneven "white" and column banding, so the 7600i v2
calibrates white in the open zone instead (`calib_white_home_y_mm = 1.5`).

## Analogue front end and noise

- The AFE is ADI (`reg 0x04 FESET = 0x02`) with AD9826 PGA codes:
  `gain = 6 / (6 − 5·code/63)` (code 0 = 1×, 63 = 6×). Measured gain ratios matched
  this within 0.1%.
- SANE skips coarse offset/gain for ADI FESET and uses the table codes (40/32/40),
  which saturate blue on the open path. The v2 runs a per-channel PGA search on the
  white strip (`afe_white_target = 60000`, the white p99). It converges to about
  38/24/27 for colour and 56/58/56 for IR, and stops within 3% of target. Offsets
  keep the table values. Dark reads about 1.6–1.8k at colour gains and 3.2–5.4k at
  IR gains.
- **Read noise enters before the PGA.** Input-referred dark noise was identical at
  gains (38,24,27) and (63,63,63): about 10/8/13 × 10⁻⁴ of white. Raising PGA gain
  to "use the ADC range" does not improve SNR, and the ADC is not the limit (noise
  is 45–72 DN). **Longer exposure (LPERIOD) is the only SNR lever.**
- On a colour-negative sky at LPERIOD 14000, scanner noise per pixel was
  R 2.1% / G 2.7% / B 5.5%, against film grain of 4.9 / 6.2 / 4.3%. Blue (behind
  the orange mask) is the only scanner-limited channel.

## Exposure and the motor

- LPERIOD is register `0x38` (16-bit, pixel-clock units, **not** µs). The tables use
  14000 at every resolution and method. The scan slope table scales motor speed with
  LPERIOD, so exposure and scan time rise together.
- **Validated envelope:** LPERIOD 42000, 56000 and 65000 ran at 1800, 3600 and
  7200 dpi with no stall. Every run parked, and levels were linear where unclipped
  (`me_long_clamp_max = 65000`). The CCD is linear in integration time to within
  ~2% (ME long/short measured 4.55–4.76 for a nominal 4.60).
- **Feed time ∝ dpi × LPERIOD.** The feed from home to the film runs on the scan
  slope: about 5.85 × 10⁻⁷ × dpi × LPERIOD seconds for a 5279-step feed (FEEDL
  21113 microsteps), which is 274 s at 7200 dpi and LPERIOD 65000. `starty` and
  the pre-feed margin are in 3600-dpi motor steps; FEEDL is
  `starty << step_type` minus the slope length. SANE's GL846 path feeds the same way.
  - The feed wait fails after 10 s without step progress (`FEED_STALL_S`), and its
    overall timeout scales with LPERIOD.
  - **Pre-feed** (`prefeed_margin_steps = 400`): before a slow image pass, a
    few-line 900-dpi scan covers the feed and stops 400 steps short. FEEDL is
    relative, so the image pass feeds the remainder. The mini-scan coasts about
    5.9 steps past its nominal stop (`prefeed_offset_steps = 6`). Position against
    a direct feed was +0.01 px at 1800 dpi and −0.42 px at 7200 dpi. A 7200-dpi
    crop dropped from 132 s to 20 s, and a 7200-dpi full frame takes 239 s.
- **Auto exposure** (`auto_exposure_target = 48000`; the colour pass of single
  and colour + IR scans, not multi-exposure). A
  900-dpi prescan of the same area measures the brightest channel's p99.5 above
  dark. LPERIOD then rises so that peak nears the target, capped at 65000. The
  host white is scaled by the same ratio (`_calib_for_exposure`), so the flat-field
  stays in open-light units. The 900-dpi prescan reads about 9% below 1800 dpi,
  which is why the target is 48000.
  - Measured choices: ~3.2× on colour negatives, ~1.6–1.75× on silver B&W.
  - On the same frame: blue scanner noise 0.50×, total blue noise 6.9 → 5.1%.

## Calibration (`scan/calibrate.py`, GL845 host path)

The calibration order is: AFE table seed → PGA white search → dark → re-home →
white → re-home.

- **Dark** is lamp-off (and IR-LED-off). It is a **mean over 1024 lines**
  (`calib_dark_lines`), which brings per-column error to 1.5–2.4 DN. The old 2 mm
  (~141-line) median left 5–8 DN, which showed as blue column banding. The 1024
  lines add about 5 s.
- **White** is the median over the 2 mm strip at `calib_white_home_y_mm`.
- **Calibration captures are assembled linearly** (`expose_base=False`). A dark
  frame brighter than half the white is rejected (`DARK_WHITE_MAX_RATIO`).
- **IR** also measures a real dark on GL845. SANE assumes zero, which flattened
  dust contrast by 13–18%.
- **"Stationary" line captures move the holder about 2 mm**: the motor steps
  while lines are clocked, even without a feed start. Always re-home after a dark
  capture, before a white feed.
- The cache is `~/.cache/pyopticfilm/calib_v2.json`, keyed by model fingerprint,
  method, dpi, startx and pixels (not exposure). Bump `CACHE_VERSION` whenever
  the meaning of an entry changes.
- **Result:** on a colour-negative sky, residual single-column banding is about
  0.1% (R/G) and below measurement in blue.

## Home, park and stop (`asic/gl845.py`)

- **Home** is a SANE-style reverse mini-scan (`ScanSession.start_home_seek`): the
  lowest dpi, 50 px × 3 lines, `MTRPWR|AGOHOME|NOTHOME|MTRREV`, starty 40000, then a
  wait for the home sensor with a 30 s timeout. It leaves the lamp state alone, so
  a capture that relights the lamp waits `lamp_settle_s = 0.5`.
- **Stop** clears SCAN, then polls until the status shows motor idle and `0x40`
  `MOTMFLG|DATAENB` are clear. It raises `MotorTimeoutError` after 1 s, and never
  hides an earlier capture error.
- A direct AGOHOME/FEEDL seek (the pre-branch code) timed out after a 3600-dpi
  full scan and left the motor enabled. A SilverFast prescan recovered it. GL843
  and GL842 subclasses still use that legacy path (`REVERSE_SCAN_HOME = False`).
- Multi-pass scans park between passes and after cancel or failure. Single-pass
  GL845 scans park after a failure.
- **A scan abandoned mid-run** (host process killed) leaves SCAN set and data in
  the buffer. Sending home/motor commands into that state hung the controller
  until a power cycle. `Gl845.init()` now checks a warm ASIC for this first:
  it clears SCAN, drains the bulk stream and soft-resets. Cold boots skip the
  check, which keeps the 8200i golden trace intact.
- **Recovery** from `is_motor_enabled=True` / not at home: `scanner.warmup()`
  (init + home), or
  `tools/bringup_gl845_8200i.py --model 7600i-v2 --allow-unvalidated --steps init,home`.

## Infrared

- The IR LED is GPIO `0xA8` bit `0x04` (`IR_LAMP_A8_MASK`). IR passes clear
  `LAMPPWR` (`0x03` bit `0x10`). The IR plane is the green CCD row.
- IR in colour + IR scans is aligned to RGB with one registration estimate. The
  measured shift was 0.05–0.1 px. On a colour sky, IR over sky and over film base
  was 1.001: dyes are IR-transparent.
- **Silver B&W blocks IR**, so its IR plane is a copy of the picture.
  `ScanImage.ir_tracks_image` flags this, and GL845 logs a warning. The check needs
  block-averaged IR-vs-visible correlation > 0.8, plus either IR spread > 0.2 or IR
  spread ≥ 0.5 × visible spread.

| Frame | IR spread | IR/visible spread | Correlation | Flagged |
|---|---|---|---|---|
| Colour negative | 0.036 | 0.053 | 0.60 | no |
| Ilford HP5 Plus | 0.76 | 0.995 | 0.999 | yes |
| Kodak Tri-X 400 | 1.36 | 1.047 | 0.999 | yes |

Chromogenic B&W (XP2, BW400CN) is expected to behave like colour; it is untested.

## Multi-exposure

- The short pass is at the model LPERIOD; `me_short_exposure` overrides it. The
  long pass is chosen by `select_long_exposure` inside the 4% border inset,
  clamped to 28000–65000 and a 4.6× ratio (`me_max_exposure_ratio`). A
  `me_long_exposure` override is clamped too. The planes stay linear and the
  film-base makeup runs once on the merge.
- The noise model is not pinned on the model, so the merge's default applies (a
  per-scan fit once upstream #149 lands).
- **Known limits:**
  - Red clips in the long pass under the orange mask. With the current merge,
    ME was no better than auto exposure on a sky frame. The same bracket through
    the open #148→#150 merge cut blue noise 5.64 → 4.89%, which beats auto
    exposure.
  - ME shifts the film-base B/R by 2–4%: R comes from the short pass, G/B from the
    long one.
  - At 7200 dpi the short and long passes start within ~0.25 px of each other,
    varying ±0.15 px between runs. Three repeats on detailed film measured
    (−0.18,−0.18), (−0.23,−0.17) and (+0.08,−0.11) px. One earlier outlier
    was −0.94. There is no constant offset to pre-shift. The merge's aligner
    is accurate to ~0.1–0.2 px when it locks. On smooth frames it rejects and
    applies no shift, which is invisible there.

## Black-and-white film

R and B register to G within 0.06 px. On HP5, the high-pass residual is 83–90%
correlated across channels, because grain is common to all three. An
inverse-variance RGB average (4.24%) was no better than the best single channel
(3.75%). A grey mode would add no quality, so B&W is scanned as RGB and converted
downstream. Hardware grey speed is unmeasured.

## Data format and pipeline

USB data is chunky 16-bit RGB (`usb_planar_rgb` False). `ImagePipeline.assemble`
then runs these steps in order:

1. Decode.
2. Line shifts R/G/B 0/12/24.
3. Y stagger (7200 dpi only, `(4, 0)`).
4. Trim to the optical span.
5. ENDPIXEL dummy-column drop.
6. Host flat-field `(raw − dark) / (white − dark)`.
7. Film-base makeup: scalar gain to p99.7 = `0xF000`, skipped for ME planes.
8. Border clamp.

`ScanImage.rgb` is linear negative data (HxWx3 uint16), and `ir` is HxW uint16.

## Published specifications (Plustek; not measured here)

The figures below are Plustek's published specifications:

- CCD sensor, white LED plus IR LED.
- 7200 dpi optical.
- 48-bit colour, 16-bit grey input.
- Dmax 3.5 (some sheets say 3.6).
- Maximum scan area 36.8 × 25.4 mm.
- USB 2.0 Hi-Speed.
- 15 V DC / 1.0 A supply.
- Manually advanced film and slide holders.

Independent reviews (filmscanner.info) report an effective resolution of about
3250 dpi.

## Measurement methods (for re-running)

- **Pass-to-pass shift:** FFT phase correlation with a parabolic sub-pixel peak.
  Repeat scans register to under 0.02 px. The library aligner rejects smooth sky
  ("low response"), so use phase correlation for measurement.
- **Scanner noise vs grain:** difference two registered repeat scans (same film,
  same grain) to get temporal noise. Grain² = total high-pass² − temporal².
- **Column banding:** remove a narrow (7-column) moving average from the column
  means, then take the covariance of the top-half and bottom-half profiles (it
  repeats if it is sensor banding). Wide windows (~100+ columns) count vertical
  scene structure as banding.
- **Linear levels:** `pipeline.assemble(raw, geo, expose_base=False)` with no
  dark/white returns linear counts. `Scanner.status()` after every motor run should
  show `is_at_home=True, is_motor_enabled=False`.

## Open work

- **Chromogenic B&W:** confirm that the IR detector does not flag it.
