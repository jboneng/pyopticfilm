# GL128 model comparison

Side-by-side capture values for Plustek GL128 OpticFilm scanners.
The goal is to see **which constants match** and **which differ**, without
inheriting a missing cell from a sibling.

GL845 products (`07b3:130c` 8100, `07b3:130d` 8200i) are a different ASIC
family and are not in these tables.

Most tables below still use the original three columns (8100 V2 / 8200i SE /
135i). **OpticFilm 8300i SE** (`07b3:181f`) is documented in
[§10](#10-opticfilm-8300i-se-07b3181f--capture-synthesis) from a dedicated
SilverFast full-frame capture set; that section is the source of truth for
the in-tree `Model8300iSE` leaf (`scan_ready=True` after hardware sign-off;
not an alias of the 8200i SE).

## How to read this

Every three-column table uses:

| Parameter | 8100 V2 | 8200i SE | 135i | Verdict |

- One row is one named constant, register, timing, or behaviour.
- `unknown` means not present in the captures decoded for this document, or
  not yet decoded. It is not a guess.
- **Verdict**
  - `same` — all known models in that row are equal
  - `different` — at least two known values disagree
  - `inconclusive` — fewer than two models are known, or the known pair
    matches but another is `unknown`

The 135i columns in §1–§9 started as notes on the VueScan captures.
[§11](#11-opticfilm-135i-07b31436--capture-synthesis) is the current synthesis
behind the in-tree `Model135i` leaf and corrects several of them (boot diffs,
`bcdDevice`, interrupt endpoint, IR, the `0x10034000` window).

Host software is not the same: **SilverFast 9** (8100 V2, 8200i SE, 8300i SE)
versus **VueScan** (135i). A VueScan number is still a real wire value, but
it is not automatically a SilverFast equivalent.

### Sources

| Model | USB | Captures | What was used here |
|-------|-----|----------|--------------------|
| 8100 V2 | `07b3:1824` | `pyopticfilm_captures/8100-v2` (SilverFast 9, USBPcap, 2026-09-05) | Session `02_cold_boot_open`, `04_color_7200`, `06_ppi_ladder` via `tools/capture_ledger.py`; leaf class `model_8100_v2.py`; `docs/register-reference.md` |
| 8200i SE | `07b3:1825` | `pyopticfilm_captures/8200i-se` (SilverFast 9, USBPcap, 2026-07/08) | `decoded/gl128_tables.json`, `decoded/ppi_lincnt_feed.json`, `13_ppi_ladder/decoded_ppi_ladder.json`; `model_8200i_se.py` / `gl128_common.py` |
| 8300i SE | `07b3:181f` (`bcdDevice=0x0702`, proven in the pcaps) | Local `8300i_captures/` set (SilverFast 9, USBPcap; full-frame 150/300/600/900/1200/1800/2400/3600/7200 + IR siblings + ME @1200/3600 + cold-boot startup) | §10 synthesis; leaf `model_8300i_se.py` (`scan_ready=True`) |
| 135i | `07b3:1436` | `plustek 135i captures` (VueScan, usbmon linktype 220, 2026-08-31) | `135i_protocol_analysis.md` (Scan Lab’s USBPcap importer does not read usbmon) |

In-tree shared vs divergent knobs for the hardware-tested GL128 models:
`GL128_SHARED_FIELDS` / `GL128_DIVERGENT_FIELDS` in
`src/pyopticfilm/device/gl128_common.py`.

### SE vs V2 — capture divergences

These are the rows where **8100 V2 and 8200i SE disagree** in the captures
below. Everything else in the shared GL128 map either matches or is not
independently proven on V2.

| # | Parameter | 8100 V2 | 8200i SE |
|---|---------|---------|----------|
| 1 | USB product ID | `0x1824` | `0x1825` |
| 2 | Infrared / iSRD | no | yes |
| 3 | Full-frame feed 2 | 13128 | 13128 (TA top; session 03 / #67) |
| 4 | PPI-ladder feed 2 | 13128 | 13560 |
| 5 | `LPERIOD` at every captured PPI | same window rule (§5); full frame | same rule; session 13 values are its crop |
| 6 | White shading dummy `0x2B` @ 7200 | `0x10` | `0x10` (same strip-width rule; was `0x17`) |
| 7 | Ladder image `LINCNT` | taller crop (see geometry) | session 13 table |

`LPERIOD` was once listed as a V2 difference (16035 vs 15963 at 7200). It is
not: every GL128 model follows one window rule (§5), and the SE numbers were
its session-13 crop.

Positioning slope **order** is not a divergence: both models upload
`SLOPE_TABLE_SLOW` then `SLOPE_TABLE_FAST` (PR #56). The old
`use_slow_final_positioning_feed` flag is gone.

### 8300i SE vs SE / V2 — headline (see §10)

| # | Parameter | 8300i SE (SF capture) | Closest sibling |
|---|---------|----------------------|-----------------|
| 1 | USB product ID | `0x181F` | neither (`1825` / `1824`) |
| 2 | Infrared | yes (`0x37` → `0xB4` on IR pass) | SE |
| 3 | Full-frame feed 2 | **PPI-dependent**, 13040…13128 (not SE 13560) | near V2 (13128) |
| 4 | Image height (`LINCNT`) | SilverFast's value, one unit and one bulk row per output line (V2 table / 4, same travel) | neither (SE/V2 program 4 per line) |
| 5 | Image-pass `LPERIOD` | **byte-identical to V2** at 300/1200/2400/3600/7200 | V2 |
| 6 | Image / feed exposure | **15000** | neither (SE/V2 use 14000) |
| 7 | Image-pass pixel clock `0xA5`/`0xAB` | different from SE/V2 (larger bytes; speed meaning unproven) | neither |
| 8 | Image-pass dummy `0x2B` | higher than SE `DUMMY_BY_DPI` at every measured PPI | neither |
| 9 | White shading dummy @ 7200 | `0x26` | neither (SE/V2 `0x10`) |
| 10 | Slope ROM payloads | CUSTOM heads `0x32BB` then `0x846A` (not SE `0x1FB4`/`0x16DE`) | neither |
| 11 | Per-scan sensor timing (`0x06`/`0x0B`/`0x52`–`0x57`/`0x5A`/`0x5B`/`0x70`–`0x73`) | own values (§10.7) | neither |

Using the 8200i SE leaf on `0x181F` therefore under-scans Y (SE ladder
`LINCNT` / feed2), runs the wrong exposure, sensor timing and shading clocks
(white-rail clip), and ignores a different motor-slope ROM. Image `0xA5`/`0x2B` also
differ; they are not proven to be the SilverFast speed advantage.

---

## 1. Identity and capabilities

| Parameter | 8100 V2 | 8200i SE | 135i | Verdict |
|-----------|---------|----------|------|---------|
| USB vendor | `0x07B3` | `0x07B3` | `0x07B3` | same |
| USB product ID | `0x1824` | `0x1825` | `0x1436` | different |
| USB product string | unknown | `Film Scanner (A2F)` | `Film Scanner(A81)` | different |
| `bcdDevice` | `0x0702` | `0x0702` | `0x0702` (`windows attach` device descriptor) | same |
| ASIC | GL128 | GL128 | GL128 | same |
| pyopticfilm `scan_ready` | yes | yes | no (`Model135i` in-tree, `scan_ready=False`) | different |
| Optical resolution | 7200 dpi | 7200 dpi | 7200 dpi | same |
| Output PPI ladder (SilverFast) | 150…3600 in session 06; 7200 in session 04 | 150, 300, 600, 720, 900, 1200, 1440, 1800, 2400, 3600, 7200 | VueScan only: 600 × 600 (panorama), 1200 × 1200 (preview), 7200 × 7200 | inconclusive |
| Manual strip feeder | yes | yes | no | different |
| Motorized auto-advance | no | no | yes | different |
| Panorama / long strip | no | no | yes | different |
| Infrared (iSRD) | no | yes (separate IR pass) | yes (RGB+IR toggle in one pass, §11.5) | different |
| Multi-exposure colour | yes (session 07 present) | yes (session 14) | unknown | inconclusive |
| `min_asic_dpi` (DPISET floor) | 600 | 600 | 600 in pyopticfilm (lowest captured scan 600) | same |
| Vendor image-pass `0x33`/`0xAF` | `0x1F`/`0xFF` | `0x1F`/`0xFF` (session 13) | `0x0E`/`0xFF` with 16-bit data (see §11.3 on `0xAE`–`0xAF`) | different |
| `mirror_x` | yes (pyopticfilm) | yes | unknown | inconclusive |

The three devices are the same ASIC family and the same USB vendor, with
three different product IDs. 135i is a motorized loader with panorama;
SE and V2 are manual feeders. The SE and 135i both scan IR, the SE in a
separate pass and the 135i interleaved with RGB (§11.5). VueScan vs
SilverFast means 135i PPI coverage cannot be compared to the SilverFast
eleven-rung ladder.

---

## 2. USB protocol

| Parameter | 8100 V2 | 8200i SE | 135i | Verdict |
|-----------|---------|----------|------|---------|
| Register write | `OUT 0x40`, `bRequest=0x04`, `wValue=0x83/0x183` | same | same | same |
| Register read | `IN 0xC0`, `bRequest=0x04`, `wValue=0x84/0x184` | same | same | same |
| Bulk image preamble | `wValue=0x82`, `wIndex=0x08` | same | same | same |
| AHB upload preamble | `wIndex=0x01` | same | same | same |
| RAM read preamble | `wIndex=0x00` | same | same | same |
| Status register | `0x101` | `0x101` | `0x101` | same |
| AFE path | `0x51` + `0x5D`/`0x5E` | same | same | same |
| Vendor probe `wIndex=0x21` in scan traffic | present, reference feed only (about 60 reads per feed until `0x04`) | present, reference feed only (same pattern) | present | same (V2/SE) |
| Vendor probe `wIndex=0x20` | present (263 reads in session 04) | unknown | unknown | inconclusive |
| `CLRCNT` before image (`0x0D`) | `0x07` | `0x07` | unknown | inconclusive |
| `START` launch (`0x0F`) | `0x01` | `0x01` | `0x01` | same |
| Bulk IN / OUT endpoints | unknown | `0x81` / `0x02` | unknown | inconclusive |
| Interrupt IN `0x83` present | unknown | yes (unused by pyopticfilm) | yes | inconclusive |
| Interrupt `0x83` used for holder | unknown | no | eject only: `0x48` (eject button); holder insert is GPIO `0x32` bit 2 (§11.6) | different |

Control, bulk-image, AHB, status `0x101`, and AFE indexing are the same
Genesys GL124/GL128 framing on all three. On the 135i nearly every
interrupt URB completes with status −2 (`ENOENT`, cancelled by the host);
the only real event is `0x48` on eject. On the V2 and SE, SilverFast polls `wIndex=0x21`
only on the first (reference) positioning feed and stops that feed when it
reads `0x04`. On the second (scan-start) feed it never reads `0x21`: it
waits on status `0x101` until `FEEDFSH` is set and the motor is off. The
8300i SE never reads `0x21`; its reference feed ends on interrupt `0x83`
returning `0x04` (see `docs/register-reference.md`, `0x21`).

---

## 3. Cold-boot registers

| Parameter | 8100 V2 | 8200i SE | 135i | Verdict |
|-----------|---------|----------|------|---------|
| Boot blob A `@ 0x000FFF00` | 34-byte preamble seen (session 02) | 34× `0x00` | not sent | different |
| Boot blob B `@ 0x000FFF01` | 34-byte preamble seen (session 02) | 32× `0x00` + `0x33 0x00` | not sent | different |
| `INIT_REGS` vs SE table (first write of each address) | **identical** (session 02, 0 diffs) | 116-register blast | same 116 addresses + `0x114`/`0x115`; 29 values differ | different |
| `_MEMORY_LAYOUT_REGS` `0xD0`–`0xF8` | addresses written in scan sessions | capture constant | own constant, written before each acquire, not at boot (§11.2) | different |
| `_FRONTEND_REGS` boot | AFE `0x51`/`0x5D` touched at open | offsets/gains zeroed | same 8 defaults | same |
| `_GPO_REGS` `0xA2`–`0xAE` | written in scan sessions | capture constant | not written at boot | different |
| `_SCAN_REGS` overlay pattern | `0x04=0x42` family; image `0x01=0x23` | same | same pattern; own timing sets (§11.4) | different |
| Boot `REG_EXPOSURE` (`0x7D`–`0x7F`) | 11000 (`0x002AF8`) | 11000 (`0x002AF8`) | 30000 (`0x007530`) | different |
| Image/feed exposure (not boot) | 14000 | 14000 | 14000 | same |

V2 cold boot is the SE `INIT_REGS` map. 135i is the same blast with a
block of model-specific constants, including a much higher boot exposure.
Do not confuse **boot** exposure (11000 on SE/V2) with the **image/feed**
value 14000.

### Boot register diffs (addresses that are not identical across models)

V2 first-writes in session `02_cold_boot_open` match the SE `INIT_REGS`
bytes, so the V2 column is that SE value. 135i values are the first
writes in `vuescan open.pcapng`.

| Reg | 8100 V2 | 8200i SE | 135i | Verdict |
|-----|---------|----------|------|---------|
| `0x0A` | `0x40` | `0x40` | `0x48` | different |
| `0x16` | `0x27` | `0x27` | `0x01` | different |
| `0x18` | `0x10` | `0x10` | `0x14` | different |
| `0x19` | `0x02` | `0x02` | `0x00` | different |
| `0x30` | `0x6F` | `0x6F` | `0xEE` | different |
| `0x31` | `0x00` | `0x00` | `0x80` | different |
| `0x32` | `0x22` | `0x22` | `0x1F` | different |
| `0x33` | `0x04` | `0x04` | `0x8E` | different |
| `0x35` | `0x2F` | `0x2F` | `0xFB` | different |
| `0x36` | `0x1C` | `0x1C` | `0xFC` | different |
| `0x37` (IR GPIO) | `0xC0` | `0xC0` | `0x21` | different |
| `0x38` | `0x44` | `0x44` | `0x60` | different |
| `0x39` | `0x00` | `0x00` | `0x02` | different |
| `0x3B` | `0xFF` | `0xFF` | `0x00` | different |
| `0x3C` | `0xFF` | `0xFF` | `0x00` | different |
| `0x4F` | `0x03` | `0x03` | `0x63` | different |
| `0x5C` | `0x40` | `0x40` | `0x00` | different |
| `0x5F` | `0x05` | `0x05` | `0x07` | different |
| `0x70` | `0x01` | `0x01` | `0x06` | different |
| `0x71` | `0x02` | `0x02` | `0x09` | different |
| `0x72` | `0x03` | `0x03` | `0x08` | different |
| `0x73` | `0x04` | `0x04` | `0x09` | different |
| `0x79` | `0x0F` | `0x0F` | `0x00` | different |
| `0x7A` | `0xFF` | `0xFF` | `0x00` | different |
| `0x7B` | `0xFF` | `0xFF` | `0x00` | different |
| `0x7C` | `0xFF` | `0xFF` | `0x00` | different |
| `0x7E` | `0x2A` | `0x2A` | `0x75` | different |
| `0x7F` | `0xF8` | `0xF8` | `0x30` | different |
| `0xA0` | `0x12` | `0x12` | `0x09` | different |

Twenty-nine addresses differ on 135i; none of those addresses differ
between V2 and SE at first write. The other 87 of the 116 SE addresses
match. An earlier version of this table took `0x03`, `0x13`, `0x31`,
`0x32`, `0x35` and `0x5E` from the register read-back that follows the
blast instead of the writes; `0x03`, `0x13` and `0x5E` are written with the
SE values.

---

## 4. Motor, FEEDL, and slope tables

### Slope ROM

| Parameter | 8100 V2 | 8200i SE | 135i | Verdict |
|-----------|---------|----------|------|---------|
| `SLOPE_TABLE_FAST` size | 512 B (same table in driver) | 256× u16 = 512 B | unknown | inconclusive |
| `SLOPE_TABLE_FAST` head word | unknown (payload not dumped here) | `0x16DE` | unknown | inconclusive |
| `SLOPE_TABLE_SLOW` size | 512 B (same table in driver) | 256× u16 = 512 B | unknown | inconclusive |
| `SLOPE_TABLE_SLOW` head word | unknown (payload not dumped here) | `0x1FB4` | unknown | inconclusive |
| Upload targets | `0x1000C000` and `0x10010000` (session 04/06) | `0x1000C000` + `0x10010000` | same addresses seen | same |
| Positioning slope order (feed 1 → feed 2) | SLOW → FAST | SLOW → FAST | unknown | inconclusive |

### `0x02` during operations

| Phase | 8100 V2 | 8200i SE | 135i | Verdict |
|-------|---------|----------|------|---------|
| Boot idle | `0x78` | `0x78` | unknown | inconclusive |
| Feed arm (before START) | `0x18` seen on feed 2 | `0x18` (MTRPWR \| FASTFED) | `0x18` | same |
| Image pass | `0x30` (MTRPWR \| AGOHOME) | `0x30` | `0x30` | same |
| Image-pass FEEDL | 1 | 1 | 1 | same |

### FEEDL map

| FEEDL (steps) | 8100 V2 | 8200i SE | 135i | Verdict |
|---------------|---------|----------|------|---------|
| 28292 | feed 1 reference (sessions 04, 06) | feed 1 reference | not observed | different |
| 13704 | not used as full-frame feed 2 | historical session 04 “full-ish” crop (not TA top; see #67) | not observed | different |
| 13128 | feed 2 all scan types (TA top) | feed 2 full-frame / preview top (session 03) | not observed | same |
| 13560 | not observed | PPI ladder crop origin (session 13; PPI-independent) | not observed | different |
| 20232 | not observed | lower-half crop (session 09b) | not observed | inconclusive |
| 27636 | scan-window end (model table) | scan-window end | unknown | inconclusive |
| 17631 | — | — | 7200 scan positioning | different |
| 6438 / 17190 / 27942 | — | — | 35 mm preview positioning | different |
| 6414 | — | — | panorama preview | different |
| 6690 / 71490 | — | — | carrier insert | different |
| 3090 | — | — | carrier eject | different |
| 1 | image-pass placeholder | image-pass placeholder | image-pass placeholder | same |

### Model motor constants

| Parameter | 8100 V2 | 8200i SE | 135i | Verdict |
|-----------|---------|----------|------|---------|
| `feed_steps_per_inch` | 14400 | 14400 | unknown | inconclusive |
| `feed_to_reference_steps` | 28292 | 28292 | unknown | inconclusive |
| `feed_to_scan_steps` | 13128 | 13128 | 17631 (7200 scan) | same (SE/V2) |
| `feed_to_scan_top_steps` | 13128 | 13128 | unknown | inconclusive |
| `feed_to_scan_bottom_steps` | 20232 | 20232 | unknown | inconclusive |
| `ladder_feed2_steps` | 13128 | 13560 | unknown | different |
| `scan_window_end_steps` | 27636 | 27636 | unknown | inconclusive |
| `max_feed_steps` | 28292 | 28292 | unknown (insert uses 71490) | different |

SE and V2 share the 28292 reference feed, the same slope-table AHB
windows, and full-frame feed 2 at the TA top (**13128**). They disagree on
the ladder crop origin (SE **13560** vs V2 **13128**). Session 04’s **13704**
was a “full-ish” crop only and is kept as a `max_image_lincnt_by_feed2`
fixture, not the model default (#67). 135i never shows those SE/V2
distances. Its 7200 analogue is 17631, and the loader adds
insert/eject/preview/panorama FEEDL values that do not exist on the
manual feeders. Slope **payload** identity on V2 and 135i is still
`unknown` at the byte level in this document (V2 is treated as the SE ROM
in pyopticfilm; 135i usbmon payloads were not decoded here).

### Fast-feed setup block (`_FEED_SETUP_REGS`, driver)

| Reg / field | 8100 V2 | 8200i SE | 135i | Verdict |
|-------------|---------|----------|------|---------|
| `0x01` | `0x22` | `0x22` | unknown | inconclusive |
| `0x04` / `0x05` | `0x42` / `0x48` | `0x42` / `0x48` | unknown | inconclusive |
| Exposure in feed setup | 14000 | 14000 | unknown | inconclusive |
| DPISET in feed setup | 200 | 200 | unknown | inconclusive |
| Window STR / END | 0 / `0x2972` | 0 / `0x2972` | unknown | inconclusive |
| Pixel clock `0xA5` / `0xAB` | `0x02` | `0x02` | unknown | inconclusive |

---

## 5. Timing — LPERIOD, DPISET, dummy, pixel clock, exposure

### `LPERIOD` (`0x28`, 24-bit BE) at image pass

SilverFast's value depends on the window, not the model:
`10851 + pixels / 4` (`/ 2` from 3600 ppi), `pixels = (END − STR) × ppi / 7200`
(`lperiod_for_window` in `device/gl128_common.py`). It reproduces every
SilverFast write in the captures: image passes and shading strips, full frames
(span 10368) and crops (session 13: 10224, session 04: 9912, session 14: 9960)
on all three models, and the AFE strips (512 / 10378 px). The two columns below
are the same rule at two widths.

| Output PPI | 8100 V2 (full frame, span 10368) | 8200i SE (session 13 crop, span 10224) | 135i | Verdict |
|------------|-------------------|----------------------|------|---------|
| 150 | 11067 (shared ASIC 600 programming) | 11064 | unknown | same rule |
| 300 | 11067 | 11064 | unknown | same rule |
| 600 | 11067 | 11064 | unknown | same rule |
| 720 | 11110 | 11106 | unknown | same rule |
| 900 | 11175 | 11170 | unknown | same rule |
| 1200 | 11283 | 11277 | unknown | same rule |
| 1440 | 11369 | 11362 | unknown | same rule |
| 1800 | 11499 | 11490 | unknown | same rule |
| 2400 | 11715 | 11703 | unknown | same rule |
| 3600 | 13443 | 13407 | unknown | same rule |
| 7200 | 16035 | 15963 | 15991 | same rule (135i different) |
| ~2400 preview (DPISET 400) | — | — | 11239 | different |
| ~1200 panorama (DPISET 200) | — | — | 11023 | different |

V2 values at 150–3600 are unique image-pass snapshots from
`06_ppi_ladder.pcapng` (150/300/600 collapse to one ASIC program). 7200
is session `04_color_7200` (also 16035 on dark and white shading). SE
values are session 13. 135i 7200 sits **between** them. pyopticfilm programs
the rule for each pass's own window on all three models.

### `DPISET` (`0x2C`, 16-bit BE)

Formula on SE/V2: `max(ppi, 600) / 6`.

| Output PPI | 8100 V2 | 8200i SE | 135i | Verdict |
|------------|---------|----------|------|---------|
| 150 / 300 / 600 | 100 | 100 | unknown | inconclusive |
| 720 | 120 | 120 | unknown | inconclusive |
| 900 | 150 | 150 | unknown | inconclusive |
| 1200 | 200 | 200 | unknown | inconclusive |
| 1440 | 240 | 240 | unknown | inconclusive |
| 1800 | 300 | 300 | unknown | inconclusive |
| 2400 | 400 | 400 | unknown | inconclusive |
| 3600 | 600 | 600 | unknown | inconclusive |
| 7200 | 1200 | 1200 | 1200 | same |
| 35 mm preview | — | — | 400 (1200 dpi) | different |
| panorama preview | — | — | 200 (600 dpi) | different |

The 7200 formula is the same on all three. Below 7200 the 135i runs its
`Speed2` timing set with `SENSORSPEED = 2`, so DPISET is X / 3, not X / 6
(§11.4).

### Image-pass dummy `0x2B`

| Output PPI | 8100 V2 | 8200i SE | 135i | Verdict |
|------------|---------|----------|------|---------|
| 150 / 300 / 600 | `0x01` | `0x01` | unknown | inconclusive |
| 720 / 900 | `0x01` | `0x01` | unknown | inconclusive |
| 1200 / 1440 / 1800 | `0x02` | `0x02` | unknown | inconclusive |
| 2400 | `0x03` | `0x03` | unknown | inconclusive |
| 3600 | `0x04` | `0x04` | unknown | inconclusive |
| 7200 | `0x17` | `0x17` | `0x17` | same |

Image dummy tracks the SE `DUMMY_BY_DPI` table on V2. 135i is only known
at 7200, where it matches.

### Shading-strip clocks `0x2B` / `0xA5` / `0xAB`

SilverFast's values depend on the strip width, on both models. With 6 bytes per
pixel and `pixels = (END − STR) × ppi / 7200`:

| Strip | `0x2B` | `0xA5` / `0xAB` |
|-------|--------|-----------------|
| Dark (DVDSET off) | `ceil(3 × line bytes / 8192)` | `01` / `30` |
| White (DVDSET on) | `ceil(line bytes / 8192)`, ×2 from 3600 ppi | `02` / `02`, `01` / `01` from 3600 ppi |

This reproduces every SilverFast strip in the SE sessions 03–14 and V2 sessions
03–07 (66 strips, full frames and crops from span 6552 to 10370), and the 8300i SE
dark strips (`shading_clocks_for_window` in `device/gl128_common.py`). At 7200 ppi
on a full frame it gives dark `0x17` and white `0x10` on both models. The old SE
white `0x17` was the image dummy, not a model difference. The image pass stays
`0x17` on both. The 8300i SE white strip uses its own clocks.

### Pixel clock `0xA5` / `0xAB` (image pass)

| Output PPI | 8100 V2 | 8200i SE | 135i | Verdict |
|------------|---------|----------|------|---------|
| 150–1800 | `0x02` | `0x02` | unknown | inconclusive |
| 2400 / 3600 / 7200 | `0x01` | `0x01` | unknown | inconclusive |

### Exposure (`0x7D`–`0x7F`) and ME (not boot)

| Use | 8100 V2 | 8200i SE | 135i | Verdict |
|-----|---------|----------|------|---------|
| Image pass | 14000 | 14000 | unknown | inconclusive |
| Feed setup | 14000 | 14000 | unknown | inconclusive |
| ME long default | 42000 | 42000 | unknown | inconclusive |
| ME adaptive min / max | 42000 / 85000 | 42000 / 85000 | unknown | inconclusive |
| ME long clamp (not 7200 dpi) | 14000–85000 | 14000–85000 | unknown | inconclusive |
| ME long clamp (7200 dpi, oversample 1) | 14000–64000 | 14000–64000 | unknown | inconclusive |

LPERIOD is not a model split: it follows the window width (§5); 135i 7200 is
a third constant. DPISET at 7200 and image
dummy at 7200 are shared. The shading-strip clocks are shared too (one
strip-width rule). 135i image-pass exposure is not in the decoded
notes used here.

---

## 6. Geometry and LINCNT

| Parameter | 8100 V2 | 8200i SE | 135i | Verdict |
|-----------|---------|----------|------|---------|
| `strpixel_native_units` | yes | yes | unknown | inconclusive |
| `optical_end_inactive_native` | 0 | 0 | unknown | same (8300i SE: 0; SilverFast USB lines carry no ENDPIXEL dummy, [#102](https://github.com/jboneng/pyopticfilm/issues/102)) |
| `image_lincnt_per_line` | 4 | 4 | unknown | inconclusive (8300i SE: 1, see §10.1) |
| TA window X | 36.58 mm (offset 0.43 mm) | 36.58 mm (offset 0.43 mm) | unknown | inconclusive |
| TA window Y | 25.59 mm | 25.59 mm | unknown (datasheet strip to 226 mm) | inconclusive |
| Channel shift R / G / B | 0 / 24 / 48 | 0 / 24 / 48 | unknown | inconclusive |
| `STAGGER` at captured PPI | clear | clear | unknown | inconclusive |

### STRPIXEL / ENDPIXEL (native 7200 clocks, image pass)

| Context | 8100 V2 | 8200i SE | 135i | Verdict |
|---------|---------|----------|------|---------|
| Full-frame / preview origin STR | 242 (session 04/06) | 242 (session 03 preview) | 35 (7200 and both previews) | different |
| Ladder STR | 242 (session 06, all rungs) | 386 (session 13, typical) | unknown | different |
| END @ 7200 | 10610 | 10610 (ladder / preview) | 10403 | different |
| END @ preview crop | 10610 | 10610 @ 1200 preview | 5219 | different |
| AFE search STR / END | unknown | `0x40` / `0x240` | unknown | inconclusive |

### Observed image `LINCNT`

| Configuration | 8100 V2 | 8200i SE | 135i | Verdict |
|---------------|---------|----------|------|---------|
| `max_image_lincnt_by_feed2[13128]` | 29012 (7200 full frame) | 4836 (1200 preview) | unknown | different |
| `max_image_lincnt_by_feed2[13704]` | — | 6628 @ 1800 | — | inconclusive |
| `max_image_lincnt_by_feed2[13560]` | — | 27476 @ 7200 ladder | — | inconclusive |
| `max_image_lincnt_by_feed2[20232]` | — | 3700 @ 1800 | — | inconclusive |
| 7200 full frame (observed) | 29012 @ feed2=13128 | 27476 (ladder @ 13560) | 20340 | different |
| Preview (observed) | 4836 @ 1200 in the ladder set | 4836 @ 1200 | 3982 | different |
| Panorama preview | — | — | 11218 | different |

### PPI ladder image `LINCNT` (fixed crop per model)

| PPI | 8100 V2 (`06_ppi_ladder` + 7200 from session 04) | 8200i SE (session 13) | 135i | Verdict |
|-----|--------------------------------------------------|----------------------|------|---------|
| 150 / 300 / 600 | 2420 | 2292 | unknown | different |
| 720 | 2904 | 2748 | unknown | different |
| 900 | 3628 | 3436 | unknown | different |
| 1200 | 4836 | 4580 | unknown | different |
| 1440 | 5804 | 5496 | unknown | different |
| 1800 | 7252 | 6868 | unknown | different |
| 2400 | 9668 | 9156 | unknown | different |
| 3600 | 14500 | 13732 | unknown | different |
| 7200 | 29012 | 27476 | 20340 (not the same crop) | different |

V2’s ladder crop starts 432 steps earlier than SE (feed2 13128 vs 13560),
so every rung is taller by `128 * asic_dpi / 600`. STR/END show V2 using
the session-03-style origin (STR 242) even on the ladder, while SE’s
session 13 crop is STR 386. 135i uses a much smaller STR (35) and a
shorter 7200 `LINCNT` (20340) — a different window, not a drop-in of
either manual-feeder crop.

---

## 7. AHB RAM map

| Address | Role | 8100 V2 | 8200i SE | 135i | Verdict |
|---------|------|---------|----------|------|---------|
| `0x10000000` | Channel R exposure / image window (scan motor step periods; see note) | yes | yes | yes | same |
| `0x10004000` | Channel G exposure | yes | yes | yes | same |
| `0x10008000` | Channel B exposure | yes | yes | yes | same |
| `0x1000C000` | Slope table (scan) | yes | yes | yes | same |
| `0x10010000` | Slope table (feeds) | yes | yes | yes | same |
| `0x10014000` | Shading coefficients (135i: IR bank 10) | yes | yes | yes | same |
| `0x10034000` | IR shading coefficients (135i bank 26, toggle mode) | no | no | yes (35 mm preview and panorama) | different |
| `0x000FFF00` / `0x000FFF01` | Boot blobs | yes | yes | not sent | different |

The six SE/V2 AHB windows are on 135i too. Toggle mode adds the IR
shading bank at `0x10034000` (`0xD1 = 0x1A`), in every 135i calibration,
not only panorama.

**Channel windows (note):** the 256-word tables at `0x10000000` /
`0x10004000` / `0x10008000` hold the scan motor's step periods, not an
exposure in the sensor sense. SE and V2 upload a flat table (the period, first
word +16). The 8300i SE uploads SilverFast's table: the period
`REG_EXPOSURE × ((0x1D & 0x1F) + 1) × asic_dpi / 14400` (1875 at 1800 dpi,
15000 at 7200), reached along the vendor acceleration ramp (head 40404, 4028,
3016, …). See `Gl128Model.scan_motor_table` and §10.1.

---

## 8. Shading, AFE, IR, cancel

| Parameter | 8100 V2 | 8200i SE | 135i | Verdict |
|-----------|---------|----------|------|---------|
| Shading lines | 128 (driver) | 128 | unknown | inconclusive |
| Shading DPISET | 1200 native (driver) | 1200 native | unknown | inconclusive |
| AFE gain fields (`0x02`-`0x04`) | 6-bit PGA (driver) | 6-bit PGA, wraps at `0x40` (probed) | unknown | inconclusive |
| AFE offset fields (`0x05`-`0x07`) | 9-bit sign-magnitude (driver) | 9-bit sign-magnitude, bit 8 = minus (probed) | unknown | inconclusive |
| AFE black target (optical black, columns 0-59 of the AFE strip) | `0x500` colour, 0 IR (SF capture pixel data) | same | unknown | same |
| AFE gain law / white targets (R,G,B) | AD9826 `trunc(75.6 − 75.6·mean/target)`, colour 47000/53575/53575, IR 51800 (SF capture pixel data) | same | unknown | same |
| SF colour AFE codes, 1800 full frame (gains; offsets) | `31/20/28`; `20/38/29` (06_ppi_ladder) | `35/25/32`; `15/23/19` (Alignment_test) | unknown | different (unit-specific) |
| Shading blob destination | `0x10014000` | `0x10014000` | `0x10014000` (IR) + `0x10034000` (visible) | different |
| IR LED on IR pass | N/A (no IR) | `0x37` bit 2 (`0xB0` → `0xB4` in session 05) | `0x32` bit 5 (`IRMASK`), toggle mode | different |
| `0x37` at boot / colour image | `0xC0` | `0xC0` | `0x21` at boot | different |
| Lamp colour `0x03` on image | `0x30` (XPASEL \| LAMPPWR) | `0x30` | unknown | inconclusive |
| Cancel lamp strobe on `0x03` | `0x30,0x20` then `0x10,0x00,0x20,0x30,0x20,0x30` | same 8-value recipe | unknown | inconclusive |
| Cancel `0x01` | `0x22` (clear SCAN) | `0x22` | unknown | inconclusive |
| Cancel SCAN-clear vs strobe order | mid-strobe | after strobe | unknown | different |

Calibration internals are well measured on the SE, only partially on V2
(dummy clocks and AHB shading window), and almost unmeasured on 135i
aside from seeing a shading upload. IR is an SE feature. Cancel-during-
preview on V2 matches the SE recipe with a cosmetic SCAN-clear ordering
difference (`docs/register-reference.md`).

---

## 9. Image-pass sequence (registers)

| Step | 8100 V2 | 8200i SE | 135i | Verdict |
|------|---------|----------|------|---------|
| Pre-scan `0x0D` | `0x07` | `0x07` | unknown | inconclusive |
| Arm scan `0x01` | `0x23` (`SCAN\|SHDAREA\|DVDSET`) | `0x23` | `0x01 \|= SCAN` seen | inconclusive |
| Launch `0x0F` | `0x01` | `0x01` | `0x01` | same |
| Image FEEDL | 1 | 1 | 1 | same |
| Image `0x02` | `0x30` | `0x30` | `0x30` | same |

The acquire launch is the same START + AGOHOME image pass on all three.
135i’s `0x0D` clear before image was not listed in the notes used here.

---

## 10. OpticFilm 8300i SE (`07b3:181f`) — capture synthesis

SilverFast 9 full-frame USBPcap captures (Windows). Product sticker /
marketing name is **8300i SE**; SilverFast may list the unit as **8300i**.
USB product string reported by hardware reporters: `Film Scanner(A2K)`.
`bcdDevice` from reporters / pcaps: `0x0702` (same family as SE/V2).

Captures used (expanded set):

| File | PPI | IR | ME |
|------|-----|----|----|
| `capture_plug-in+silverfast-startup.pcapng` | — | — | cold boot |
| `capture_150_full-frame_with-ir_no-me.pcapng` | 150 | yes (`0xF4`) | no |
| `capture_300_full-frame_no-ir_no-me.pcapng` | 300 | no | no |
| `capture_300_full-frame_with-ir_no-me.pcapng` | 300 | yes | no |
| `capture_600_full-frame_with-ir_no-me.pcapng` | 600 | yes | no |
| `capture_900_full-frame_with-ir_no-me.pcapng` | 900 | yes | no |
| `capture_1200_full-frame_with-ir_no-me.pcapng` | 1200 | yes | no |
| `capture_1200_full-frame_with-ir_with-me.pcapng` | 1200 | yes | yes |
| `capture_1800_full-frame_with-ir_no-me.pcapng` | 1800 | yes | no |
| `capture_2400_full-frame_no-ir_no-me.pcapng` | 2400 | no | no |
| `capture_2400_full-frame_with-ir_no-me.pcapng` | 2400 | yes (`0xF4`) | no |
| `capture_3600_full-frame_no-ir_with-me.pcapng` | 3600 | no | yes |
| `capture_3600_full-frame_with-ir_no-me.pcapng` | 3600 | yes | no |
| `capture_7200_full-frame_no-ir_no-me.pcapng` | 7200 | no | no |
| `capture_7200_full-frame_with-ir_no-me.pcapng` | 7200 | incomplete (no IR image preamble) | no |

Still missing: **720**, **1440**. Cold-boot startup first-writes match SE
`INIT_REGS` **116/116** — no 8300i-specific INIT leaf.

### 10.1 SilverFast `LINCNT` vs pyopticfilm

On every image pass, SilverFast programs a register `LINCNT` such that
`bulk_rows == LINCNT` and `bulk_size == LINCNT × host_width × 6` (16-bit
chunky RGB). That SF value is **exactly** the 8100 V2 pyopticfilm
`ladder_lincnt_by_dpi` / full-frame entry **divided by 4**:

| PPI | SF `LINCNT` | SF×4 (= pyopticfilm V2) | SE ladder |
|-----|-------------|-------------------------|-----------|
| 150/300/600 | 605 | **2420** | 2292 |
| 900 | 907 | **3628** | 3436 |
| 1200 | 1209 | **4836** | 4580 |
| 1800 | 1813 | **7252** | 6868 |
| 2400 | 2417 | **9668** | 9156 |
| 3600 | 3625 | **14500** | 13732 |
| 7200 | 7253 | **29012** | 27476 |

Physical height at 7200: `7253 / 7200 × 25.4 ≈ 25.59 mm` (= shared
`y_size_ta_mm`). The SF line count follows one rule on all three models:
`height + round(48 × dpi / 7200) + (5 at 7200, else 1)`, where `height` is one
inch at the ASIC dpi for a full frame and `floor(h × dpi)` for a crop (the SE
ladder: h = 0.946667 in at every rung). Image passes program it through
`Gl128Common.image_line_margin`, capped for crops at the travel SilverFast's full
frames use; `int(y_size_ta_mm × dpi / 25.4)` was up to one line off it. SF's
window: STR steps from 242 in whole output pixels and the width is
`round(w × dpi)` rounded down to even (1440 full frame: 2074 px, END 10612). The SE ladder is shorter by `128 × asic_dpi / 600` at every
rung — the same gap that forced the V2 leaf’s own ladder table.

**Leaf implication (since 1.3.7):** the 8300i SE programs the SF `LINCNT`
itself: `image_lincnt_per_line=1`, `usb_image_lincnt_half_lines=False`, and SF
values in `ladder_lincnt_by_dpi` / `max_image_lincnt_by_feed2` (7253 at
feed2 13128). This pairs with SilverFast's scan motor table at
`0x10000000`/`0x10004000`/`0x10008000` (`Model8300iSE.scan_motor_table`),
which steps the motor at twice the SE/V2 speed. Up to 1.3.6 the leaf stored
the V2-scale SF×4 value with the 8200i SE's 4-per-line convention. That only
covered the frame at the old half-speed table; with SilverFast's table the
carriage travelled twice the frame height and hit the end of travel just past
halfway ([#98](https://github.com/jboneng/pyopticfilm/issues/98)). The SF×4
column above is kept as the old value and the V2 cross-check.

### 10.2 Colour short image-pass table

Values at the first (or short-ME) `wIndex=0x08` image preamble. Feed2 is the
**settled** 24-bit FEEDL (complete `0x3D`/`0x3E`/`0x3F` trio) in 12000–14000
before that preamble — not a mid-byte write.

| PPI | feed2 (settled) | DPISET | SF `LINCNT` | SF×4 (py ≤1.3.6) | `LPERIOD` | vs SE | vs V2 | expo | `0xA5`/`0xAB` | SE `0xA5` | dummy `0x2B` | STR / END |
|-----|-----------------|--------|-------------|------------------|-----------|-------|-------|------|---------------|-----------|--------------|-----------|
| 150 | **13040** | 100 | 605 | 2420 | 11067 | +3 | 0 | 15000 | `0x59` | `0x02` | `0x06` | 242 / 10610 |
| 300 | **13040** | 100 | 605 | 2420 | 11067 | +3 | 0 | 15000 | `0x59` | `0x02` | `0x06` | 242 / 10610 |
| 600 | **13040** | 100 | 605 | 2420 | 11067 | +3 | 0 | 15000 | `0x59` | `0x02` | `0x06` | 242 / 10610 |
| 900 | **13096** | 150 | 907 | 3628 | 11175 | +8 | 0 | 15000 | **`0x22`** | `0x02` | `0x06` | 242 / 10610 |
| 1200 | **13112** | 200 | 1209 | 4836 | 11283 | +6 | 0 | 15000 | `0x12` | `0x02` | `0x07` | 242 / 10610 |
| 1800 | **13122** | 300 | 1813 | 7252 | 11499 | +9 | 0 | 15000 | **`0x08`** | `0x02` | **`0x08`** | 242 / 10610 |
| 2400 | **13124** | 400 | 2417 | 9668 | 11715 | +12 | 0 | 15000 | `0x05` | `0x01` | `0x0B` | 242 / 10610 |
| 3600 | **13126** | 600 | 3625 | 14500 | 13443 | +36 | 0 | 15000 | `0x03` | `0x01` | `0x10` | 242 / 10610 |
| 7200 | **13128** | 1200 | 7253 | 29012 | 16035 | +72 | 0 | 15000 | `0x02` | `0x01` | `0x1F` | 242 / 10610 |

Offsets from V2’s 13128: **88 / 88 / 88 / 32 / 16 / 6 / 4 / 2 / 0** steps
(150…7200). ME-long passes at 1200 and 3600 do write **13128**. Nearest-band
fills for clocks/feed2/dummy are **wrong** at 900/1800 — leaf tables use the
measured values above. Still no 720/1440 captures.

`DPISET` matches the shared `max(ppi, 600) / 6` formula. `LPERIOD` matches
the V2 capture table at every measured PPI (including the full V2 ladder
deltas that pyopticfilm has not yet promoted on the V2 leaf except at 7200).
Image dummy and pixel clock are **not** the shared SE/V2 maps.

### 10.3 IR and multi-exposure

From `capture_1200_full-frame_with-ir_with-me.pcapng`:

| Pass | expo | `0x37` | Notes |
|------|------|--------|-------|
| Colour short | 15000 | `0xC0` | same geometry as other PPIs |
| IR | 15000 | **`0xB4`** | IR LED bit set (same recipe as SE session 05: `0xB0` → `0xB4`) |
| Colour ME long | **60000** | `0xC0` | adaptive long; `0xA5`/`0xAB` = `0x02`; feed2 **13128** |

`capture_3600_full-frame_no-ir_with-me.pcapng` has colour short @ 15000 then
ME long @ **60000** (no IR; feed2 **13128**). Supports `supports_infrared=True`
and the existing GL128 ME path; long exposure observed is 60000 (within the
14000–85000 clamp), not the 42000 floor.

IR `0x37` is usually **`0xB4`**; at **150** and **2400** captures see **`0xF4`**
(IR bit `0x04` still set). The `7200_…with-ir` capture never announces a second
full image preamble — treat 7200 IR as incomplete.

### 10.4 Shading strips (large RAM reads)

Each colour (re)calib block has two large RAM reads at the scan DPISET
(`LINCNT=128`), then AFE-sized probes earlier at native DPISET 1200.

| PPI | Strip | `r01` | DVDSET | DPISET | `LPERIOD` | dummy `0x2B` | `0xA5` | `0xAB` | expo |
|-----|-------|-------|--------|--------|-----------|--------------|--------|--------|------|
| 150/300/600 | dark | `0x03` | off | 100 | 11067 | `0x02` | `0x01` | `0x30` | 15000 |
| 150/300/600 | white | `0x23` | on | 100 | 11067 | `0x03` | `0x03` | `0x03` | 15000 |
| 900 | dark | `0x03` | off | 150 | 11175 | **`0x03`** | `0x01` | `0x30` | 15000 |
| 900 | white | `0x23` | on | 150 | 11175 | **`0x04`** | `0x03` | `0x03` | 15000 |
| 1200 | dark | `0x03` | off | 200 | 11283 | `0x04` | `0x01` | `0x30` | 15000 |
| 1200 | white | `0x23` | on | 200 | 11283 | `0x06` | `0x03` | `0x03` | 15000 |
| 1800 | dark | `0x03` | off | 300 | 11499 | **`0x06`** | `0x01` | `0x30` | 15000 |
| 1800 | white | `0x23` | on | 300 | 11499 | **`0x08`** | `0x03` | `0x03` | 15000 |
| 2400 | dark | `0x03` | off | 400 | 11715 | `0x08` | `0x01` | `0x30` | 15000 |
| 2400 | white | `0x23` | on | 400 | 11715 | `0x0B` | `0x02` | `0x02` | 15000 |
| 3600 | dark | `0x03` | off | 600 | 13443 | `0x0C` | `0x01` | `0x30` | 15000 |
| 3600 | white | `0x23` | on | 600 | 13443 | `0x10` | `0x02` | `0x02` | 15000 |
| 7200 | dark | `0x03` | off | 1200 | 16035 | `0x17` | `0x01` | `0x30` | 15000 |
| 7200 | white | `0x23` | on | 1200 | 16035 | **`0x26`** | `0x02` | `0x02` | 15000 |

Dark @ 7200 dummy `0x17` matches SE/V2. White @ 7200 is **`0x26`**, not V2’s
`0x10` or SE’s `0x17`. All shading uses exposure **15000**. 900/1800 strip
dummies are **not** the nearest lower-band fills.

AFE probe traffic (DPISET 1200, sizes 3072 / 62268) appears before the
shading pair on every capture; wide-probe `LPERIOD` examples include 13445
and 16040 depending on PPI.

### 10.5 Motor FEEDL

| FEEDL | Role on 8300i SE |
|------:|------------------|
| 28292 | feed 1 reference (same as SE/V2) |
| 13040 / 13096 / 13112 / 13122 / 13124 / 13126 / 13128 | feed 2 full-frame origin (PPI-dependent; see §10.2) |
| 1 | image-pass placeholder |

SE ladder origin 13560 and session-04’s historical 13704 crop do **not** appear
as settled feed2 values on 8300i. `Model8300iSE.feed_to_scan_steps_for_dpi` programs
the PPI-dependent colour-short map (ME-long → 13128); `feed_to_scan_steps`
remains 13128 only as a dpi-less fallback.

### 10.6 Slope ROM (AHB `0x1000C000` / `0x10010000`)

Every capture uploads 512-byte slope tables to both AHB windows. Payloads
are **not** byte-identical to SE `SLOPE_TABLE_SLOW` / `SLOPE_TABLE_FAST`
(heads `0x1FB4` / `0x16DE`). Observed heads:

| When | Head word (LE u16) | Role (by FEEDL pairing) |
|------|--------------------|-------------------------|
| Reference feed (~28292) | `0x32BB` | SLOW substitute |
| Scan / feed2 (~13128) and early idle pair | `0x846A` | FAST substitute |

Order remains **slow then fast** (same choreography as SE/V2 PR #56). Full
512-byte tables are byte-identical across all six measured captures and both
AHB windows per upload pair; they live in
`src/pyopticfilm/device/tables_8300i_se.py` and are selected via
`Model8300iSE.slope_table_slow()` / `slope_table_fast()`. SE and V2 keep the
SE ROM through `Gl128Common` defaults.

### 10.7 Per-scan sensor timing overlay

Before every AFE, shading and image acquire, SilverFast rewrites a block of
sensor/AFE timing registers on top of the boot map (`sensor_custom_regs`).
The 8300i values are constant across every measured PPI and across the IR
and colour passes, and differ from the SE/V2 `SCAN_REGS` (SE sessions 04,
05 and 13 and V2 sessions 03 and 05 match `SCAN_REGS` exactly):

| Reg | 8200i SE / 8100 V2 | 8300i SE |
|-----|--------------------|----------|
| `0x06` | `0xF0` | `0x10` |
| `0x0B` | `0x4C` | `0x6C` |
| `0x52`–`0x57` | `0B 0D 0F 01 05 07` | `08 0A 00 02 04 06` |
| `0x5A` / `0x5B` | `31 79` | `22 4A` |
| `0x70`–`0x73` | `0A 0B 0C 0D` | `06 07 07 08` |

`0x36` stays `0x1C` on both (boot and scan). An independent USBPcap set from
another 8300i unit (SilverFast 9 HDRi and VueScan 9.8.44,
[NegPy #1444](https://github.com/marcinz606/NegPy/issues/1444)) shows the same
timing values but boots with `0x36=0x04`; see the firmware note below. Under the SE values that unit's colour white strip read ~2.3x hotter
than SilverFast at the same exposure and clipped (~92 % of samples ≥ 65000);
with the 8300i values its AFE search landed next to SilverFast's own gains.
Other units ([#79](https://github.com/jboneng/pyopticfilm/issues/79)) had
enough headroom to scan on the SE values.

**Clock `0x8C` (all GL128 models).** The vendor sends
`REQUEST_REGISTER` / `VALUE_BUF_ENDACCESS` writes (SANE `write_0x8c`) to
indices `0x10` and `0x13`, both with the same value, at two points:

| | Boot (after the 116-register blast) | Before every scan pass |
|---|---|---|
| 8200i SE | `0x0C` | `0x0C`, then `0x0C` |
| 8100 V2 | `0x0E` | `0x0E`, then `0x0C` |
| 8300i SE | `0x0E` | `0x0E`, then `0x0D` |

Before each pass (colour, IR, ME) the boot value goes out, then lamp toggles
on `0x03` and `0x33` writes, then the scan value, then the full register
reprogram and AFE init, so calibration and image both run on the scan value.
Sources: all 8200i SE sessions; V2 `02_cold_boot_open`, `03_prescan` and
`05_midtravel_home`; every 8300i SE full-frame capture. pyopticfilm sends the
boot value in `asic_boot` and the boot-then-scan pair in
`Gl128.enter_scan_clock()` before each pass and before `Scanner.calibrate`. The
lamp and `0x33` writes in between are not replicated.

`0x0B` moves with the clock. Its top bits follow `0x8C` in every capture
(`0x0C` with `0x4_`, `0x0D` with `0x6_`, `0x0E` with `0x8_`) and bit 3 enables
DRAM. After the boot clock the vendor writes `0x0B` with DRAM off, `0x13=0x0F`,
then `0x0B` with DRAM on (8200i SE `0x44`/`0x4C`, V2 and 8300i SE `0x84`/`0x8C`,
model field `asic_boot_0x0b`). The register blast after each per-pass clock
switch carries `0x0B` at its scan value (`0x4C` SE/V2, `0x6C` 8300i SE) and
`0x13=0x08`; `enter_scan_clock()` writes those two right after the switch.
A build that changed the clock without them stalled every few seconds with a
rattling motor on an 8200i SE at 7200 dpi.

Scanning an 8300i SE at `0x0E` calibrates and images normally, but the motor
runs fast with a lower pitch and a rattle, and the carriage stalls for a few
seconds about 60 % into each pass. Reporter measurements at 3600 dpi on the
#95 unit (same host code, only the clock changed):

| Clock | 28292-step feed | 13128-step feed | Pass |
|---|---|---|---|
| `0x0E` | 1.01 s | 1.28 s | 29 s, stalls |
| after SilverFast | 1.25 s | 1.57 s | 30 s, clean |
| `0x0D` | 1.22–1.28 s | 1.54–1.59 s | 30 s, clean |
| `0x0C` | 1.46 s | 1.89 s | 36 s |

Image levels were the same at all three values.

Before any of this, pyopticfilm never sent `0x8C`, so the clock kept whatever
the scanner powered up with (or whatever SilverFast last set). On a failing 8300i unit, SilverFast's first AFE probe (gain 0,
offset `0x80`) read 24326 / 32312 / 27550, and pyopticfilm's read 1.54x higher with
every register, frontend value and RAM upload equal apart from `0x37`, `0xA3`,
the `0x28`–`0x2B` SDRAM block and the scan motor tables at
`0x10000000`/`0x10004000`/`0x10008000` (both about 15000). `0x8C` was the only
vendor boot write pyopticfilm skipped. Sending any of the three values cured the
clipping on the reporter's unit.

**`0x37` for colour (all GL128 models).** The vendor writes `0x37=0xC0` for the
whole colour calibration and the colour image, `0xB4` for IR and `0xF0` for the
IR dark strip. The register reads back `0xB0` after the boot write of `0xC0`.
pyopticfilm used to clear the IR bit with a read-modify-write, which left colour
at `0xB0`. Colour now writes `0xC0`. On the failing 8300i unit this alone did
not change the white level.

**Firmware-dependent `0x36` (all GL128 models).** At open the vendor driver
reads the USB serial-number string (`iSerialNumber`, descriptor index 12), which
GL128 firmware sets to its build date. For `20241113` it loads the registry set
`VID_07b3&PID_xxxx-20241113`; for anything else it loads the plain
`VID_07b3&PID_xxxx` set. The two differ only in boot register `0x36`: `0x1C`
for `-20241113` and `0x04` for the plain set (vendor registry dumps for the 8200i SE,
8100 and 8300i; the date strings and the `PID_%04x-%s` key format are in
`Scndrvu_x64.drv`). Every captured unit (8200i SE, 8100 V2, 8300i SE) reports
`20241113`, so `INIT_REGS` has `0x1C`. The driver uses `0x04` when the serial
string is readable and is not `20241113` (`gl128_firmware_variant`). The
bit-level meaning of `0x36` is unknown: on a `20241113` 8300i unit, forcing
`0x04` before every acquire left the AFE probe and strip levels unchanged.

The IR AFE sequence has the same shape as colour on both IR models (probe at
gains 0 / offsets `0x80` and `0xFF`, one wide 62268-byte strip, then coarse
gains), with `0x03=0x20` (white lamp off) and `0x37` IR LED set. IR gains
differ per unit (SE session 05: 6/16/12; 8300i capture: 28/35/33).

### 10.8 Why SE-as-8300i fails (maps to reporter symptoms)

| Symptom | Capture-backed cause |
|---------|----------------------|
| Missing bottom of frame | SE ladder `LINCNT` too short vs V2/8300i full-frame height (~1.35 mm at 7200); SE ladder feed2 13560 vs 8300i ~13128 shifts Y |
| Missing right of frame | **Not** a STR/END mismatch (242/10610 on both). Resolved: the host ENDPIXEL dummy trim and 96-clock window shift (`optical_end_inactive_native=96`) are gone on all GL128 models, so the image is SilverFast's 2592 px at 1800 ([#102](https://github.com/jboneng/pyopticfilm/issues/102)) |
| Slower than SilverFast | Image `0xA5`/`0xAB` and dummy **differ** from SE; CUSTOM slope ROM. Larger `0xA5` is not proven to mean a faster clock |
| Colour ASIC shading white clip (≥65000) | SE per-scan sensor timing (§10.7); also SF shading/image exposure **15000** and different white-strip clocks/dummies vs SE 14000 + SE `shading_strip_clocks` |

### 10.9 Open gaps (8300i SE)

- Full PPI ladder still missing **720** and **1440** (image clocks / feed2 / shading still nearest-band fills — known-unreliable after 900/1800 corrections)
- Complete 7200 IR image capture (current with-IR file has no second image preamble)
- Product string / configuration descriptor from an in-tree capture (PID/`bcdDevice` are in the pcaps)

Closed by hardware sign-off / expanded captures: cold-boot = SE `INIT_REGS`
(116/116); measured 150/600/900/1800 image + shading rows; mid-ladder
dummy/clock/feed2 corrected at 900/1800; CUSTOM slope ROM accepted on HW;
edge green fringe / period-2 sawtooth fixed on HW at 1440/2400 (rounded
`ld_shift` + host unstagger `(1, 0)`); `scan_ready=True`.

Closed from capture pixel data, not yet hardware-verified: AFE gain/offset
search targets (the SilverFast algorithm in the `calib_gl128` AFE block; the
8300i SE PPI ladder fits the same targets as the SE and V2).

## 11. OpticFilm 135i (`07b3:1436`) — capture synthesis

Sources:
- the VueScan usbmon capture set (`windows attach`, `vuescan open`, `insert
  35mm carrier`, `holder eject`, 1200 dpi 35 mm preview, 600 dpi panorama
  preview, 7200 dpi frame scan; bulk image payloads are truncated by the
  capture, every control transfer and AHB upload is complete);
- the Plustek model INI SilverFast 9 installs for this scanner
  (`Scanapi_07b3_1436.ini`, key `VID_07b3&PID_1436`).

Every register program in the captures agrees with the INI. The in-tree leaf
is `Model135i` (`device/model_135i.py`, tables in `device/tables_135i.py`),
`scan_ready=False`.

### 11.1 Identity

`bcdDevice = 0x0702` (the same class as the 8200i SE and 8300i SE), product
string `Film Scanner(A81)`, serial string `1`. The INI has a single register
set, so the per-firmware `0x36` switch of the other GL128 models does not
apply (`Model135i.firmware_register_overrides` returns nothing).

### 11.2 Boot (`vuescan open`)

1. The 116 SE addresses plus `0x114`/`0x115`, 29 values different (§3), and
   no `0x000FFF00`/`0x000FFF01` blobs.
2. Clock `0x8C[0x10] = 0x8C[0x13] = 0x0D` (48 MHz).
3. The DRAM strobe `0x0B = 0x64`, `0x13 = 0x0F`, `0x0B = 0x6C`.
4. GPIO requests: write `0x8B` idx 7 `01`, idx 9 `00000000`; read `0x8A`
   idx `0x10` (3 bytes); write idx `0x0B` `00000000`; read idx `0x0F`
   (64 bytes).
5. Lamp off: `0x03 = 0x10`, then `0x00`.
6. The 8 frontend defaults (same as the SE).

No memory layout, GPO block or motor tables at boot. The memory layout
(`0xD0 = 0x0A`, `0xD1 = 0x1A`, `0xE0`–`0xF8`) is identical in all three
scans and is written before each acquire.

### 11.3 Motor

| Item | Value | Evidence |
|---|---|---|
| Step | 1/7200 inch: `MotorDPI 3600` with step shift 1 (`0xA0 = 0x09`, bits 2:0 and 5:3) | frame pitch 10752 steps = 37.9 mm |
| Move split | ramp `min(n/2, 255)` in `0xAE`–`0xAF` (always `00 FF`), `FEEDL = n − 510` | load 7200 → 6690, seek 72000 → 71490, eject 3600 → 3090 |
| Curve | `Motor\Curve = 30,2500,180`: `rate[i] = B·(i/255)^(100/180)`, `rate[0] = 0.3·rate[1]` | all five captured table shapes |
| Image-pass table | `B = 2500 << 1`, period `(10^9) // 266` (tick 16 at 60 MHz), clamped at the scan period | 600, 1200 and 7200 dpi tables, word for word |
| Feed tables (VueScan) | same curve at `B = 4250`: insert ramp at `10^9 / 500` (head 34482), positioning/eject ramp at `10^9 // 266` (head 64817) | insert, every positioning feed, eject |
| Scan period | `exposure · (0x1D + 1) · Ydpi / 3600` | 4666 (1200), 2333 (600), 56000 (7200 at exposure 28000) |

`0xAE`–`0xAF` is the 16-bit ramp-step count on this chip revision. The SE
leaves write `0xAF` as part of their feed and depth setup (`registers.py`
`REG_DEPTH_B`); the 135i captures write `0xFF` there with 16-bit data, so
that register name is worth re-checking on the SE.

### 11.4 Timing sets and per-scan registers

The Y resolution picks an INI subkey: the smallest of 300 / 600 / 1200 /
3600 / 7200 at or above it. Layering is root → `<Source><IR|Toggle>` →
`Color|Gray` → subkey (which includes `Speed1` or `Speed2`).

| | `Speed1` (7200) | `Speed2` (300–3600) |
|---|---|---|
| `SENSORSPEED` | 1 (`hw / sp` = 7200) | 2 (`hw / sp` = 3600) |
| DPISET | X / 6 | X / 3 |
| `0x18` | `0x10` | `0x00` |
| `0x52`–`0x57` | `0A 0D 0F 02 05 08` | `0F 02 05 08 0A 0D` |
| `0x5A`/`0x5B` | `2A 6A` | `62 22` |
| `0x70`–`0x73` | `0A 0B 0B 0C` | `00 01 01 02` |

Common to both: `0x06 = 0xF8`, `0x16 = 0x23`, `0x1F`–`0x24 = 00 19 00 05
00 0F`, `0x88`–`0x89 = 00 05`, `REG_EXPOSURE` 14000, 60 MHz. `0x3B`/`0x3C`
by subkey: 300 `00 00`, 600 `02 00`, 1200–7200 `00 01`. The resolved
overlays match the 600 and 1200 dpi image passes exactly; at 7200 dpi only
exposure (28000, the INI value × VueScan's density multiplier 2) and `0x90`
(11000 × (2 + 2) = 44000) differ.

Hardware X resolutions are 7200 and 3600 / D (3600, 1800, 1200, 900, 720,
600, …). 1440 and 2400 dpi have no native rung and are not in
`Model135i.resolutions_dpi`.

| Item | Rule | Measured |
|---|---|---|
| STRPIXEL | `r080 = 0x1F + 2·0x21 = 25 + 10 = 35` | 35 at 600 / 1200 / 7200 |
| ENDPIXEL | STR + 10368 (7200) or STR + 5184 (`Speed2`) | 10403 / 5219 |
| `LPERIOD` | `10807 + px/4` (`/2` from 3600): the family window rule with the SDRAM line buffer left after two shading tables | 11023 / 11239 / 15991 |
| Image `0x2B` | — | `0x0A` (600), `0x0D` (1200), `0x17` (7200) |
| Image `0xA5`/`0xAB` | — | `0x23`, `0x0B`, `0x01` |
| `0x1D` | — | 0 on every pass and strip |

### 11.5 Infrared (toggle mode)

Every VueScan pass except the lamp-off dark strip runs the INI
`NegativeToggle` mode: `0x0C[3:0] = 1`, `0x19` bit 3, IR LED on `0x32` bit 5
(`IRMASK = 0x32,0x20,0x20`). Each scan line is a full IR line followed
by a full visible line (3 channels × 16 bit each), so bulk bytes = `LINCNT` ×
one sensor line and `LINCNT` counts two lines per output line:

- 7200 dpi: 124 416 bytes per line pair, 4 pairs per 497 664-byte read,
  10 084 pairs (35.6 mm).
- 1200 dpi preview: 1975 pairs.

The IR line comes first. VueScan's two-line AFE probes were captured whole:
at gain 0, line 0 is neutral (channel spread ~4 %) and line 1 carries the
lamp's green cast (G/R ~1.25); after VueScan's gains, line 1 is the balanced
one. The same two lines keep their order through the white strip, where the
gains make line 0 read G/R ~0.73 and line 1 ~1.0 (panorama capture; line 0's
ratio falls 0.958 → 0.726 and line 1's 1.267 → 0.956, the same 0.76 factor).
So neutrality identifies the IR line only at gain 0. The QuickScan 600 dpi IR
scans agree (even rows neutral).

Shading has two banks: `0xD0 = 10` (`0x10014000`, IR, the first line of each
pair) and `0xD1 = 26` (`0x10034000`, visible). In the panorama capture each
final table divides out its own white line to 0.03 % across the sensor (5 %
against the other line), with `gain = target · 0x4000 / white`: target
`0xF000` on `0x10014000` (median gains R/G/B 17615 / 24098 / 20442, undoing
the cast the AFE gains put on the IR line) and `0x16000` on `0x10034000`
(about 28400). The toggle `PIXELGAIN` has these six values, three per bank. The
shading blob layout is the SE's (`calib_gl128.unpack_shading_table` decodes
both banks exactly). The IR line arrives as three channels; which channel
(or average) to use for the IR plane is a host choice.

### 11.6 Loader

| Event | Registers | Evidence |
|---|---|---|
| Holder in | GPIO `0x32` bit 2 goes high with the stop disarmed (it follows `HOMESNR`; `0x43` → `0x07`; QuickScan `0x43` → `0x05`; a 135i before init `0xc2` → `0xc6`, after init `0x1b` empty). Bit 6 only toggles in the VueScan capture | `insert 35mm carrier`, 12.78 s; tester readings |
| `HOMESNR` | `0x32` bit 1 clear (stop armed): a narrow band around the loaded position; set: holder in the slot | tester readings, QuickScan eject |
| Load | after 0.5 s (`LoaderDelayTime`): forward 1 inch (`HomeCarriageStep[0]`), then `0x02 = 0x1C` (MTRREV) up to 10 inches back until status `HOMESNR` | FEEDL 6690, then 71490 ending on status `d8` |
| Frame positioning | forward from the loader home, `0x02 = 0x18` | 6438, 17190, 27942 (pitch 10752); panorama 6414; 7200 scan 17631 |
| Image pass | `0x01 = 0x23`, `0x02 = 0x30`, FEEDL 1 | every scan |
| Eject | interrupt byte `0x48`; QuickScan: `0x32` bit 1 clear, home first when `HOMESNR` is 0, holder LED (`36=fc`, `33=8e`), `0x32` bit 1 set, then only with `HOMESNR` 1: `09 \|= 08`, forward `LoaderEjectStep = 500` (3600 steps), `09=00` | `holder eject`, FEEDL 3090; QuickScan capture |

There is no `0x21` feed probe and no reference feed; feeds end on status
`0x101`. VueScan also polls `0x31`/`0x100`/`0x101` and rewrites `0x32`,
`0x33`, `0x36`, `0x3A` about every 190 ms while idle.

### 11.7 What the leaf implements

`Model135i` carries the boot sequence (driven through the `Gl128Common` boot
hooks: `boot_blobs`, `boot_writes_layout_and_gpo`, `boot_vendor_steps`,
`boot_uploads_tables`), the motor tables, the timing sets and scan overlays
(`scan_regs_for`, `scan_masked_regs_for`), the window, `LPERIOD` and
exposure rules, the loader constants and the frame feeds.
`Gl128Common.film_loader` (a class flag, `True` only on `Model135i`) selects
the loader driver, session and geometry below; the flatbed GL128 path is not
used for this model. `scan_ready` stays `False`.

### 11.8 Session (`Gl128Loader`, `Gl128LoaderScanSession`)

`asic/gl128_loader.py` and `scan/session_gl128_loader.py` replay the VueScan
programs above. Every pass runs in toggle mode, so one pass returns RGB and
IR registered to the same scan line; `scan(infrared=False)` drops the IR
half.

| Step | What runs | Evidence |
|---|---|---|
| Holder | `holder_present()`: `HOMESNR` with `0x32` bit 1 set, then restored (`0x32` bit 2 and `0x100` bit 7 follow the same arm state). `is_at_home()`: `HOMESNR` with `0x32` bit 1 cleared, then restored. `eject_requested()`: interrupt byte `0x48` | `insert 35mm carrier`, `holder eject`, tester readings |
| Load | `load_holder()`: 0.5 s, clear `0x32` bit 1, 7200 forward (load ramp); reverse seek until `HOMESNR` (`0x1C = 0`, exposure 30000, `0x8A`–`0x92 = 0`) only when `HOMESNR` dropped and the motor is idle (`0x101` bit 0, `0x100` bit 1), otherwise eject | insert, both feeds |
| Eject | `eject_holder()`: the QuickScan order above; off the loader home, a reverse seek straight back, `FEEDL` = the 73722-step travel | `holder eject`, QuickScan capture |
| Home | `home()`: the load's two moves (1 inch in, then the reverse seek) when a holder is in the slot and off the loader home; after a lost park, the eject's seek alone; no move with the slot empty | insert, both feeds |
| AFE | probe 35–547 (2 lines) at offsets `0x80`/`0xFF`, full-width strip, probe pair again; `DPISET = 1200`, `LPERIOD = 10807 + px/4`, `0x2B` `0x01`/`0x04`, `0xA5 = 0x01`, `0xAB = 0x30`, `0xA6`–`0xA9 = 00 01 00 01` | both previews |
| Dark | lamp, toggle bit and IR LED off; 256 lines through the scan window, `0xA3 = 1` | both previews |
| White | lamp, toggle bit and IR LED on; 256 lines with `0x01 = 0x23`, motor off | both previews |
| Shading | per-pixel dark, unity `0x4000`, then gains `target · 0x4000 / white`; even lines (IR, target `0xF000`) fill `0x10014000`, odd lines (visible, target `0x16000`) `0x10034000`; `split_toggle_lines` returns the odd lines as the image and the even lines as IR | both previews (21064 bytes per bank at 1728 px) |
| Frame feed | from the loader home, `FEEDL = 6438 + 10752 · frame` (+ the crop offset), under the image registers with `0x01 = 0x22`, `0x05 = 0x48`, `0xA6`–`0xA9 = 0` | every positioning feed |
| Strip (panorama) | `scan(frame=None)`: `FEEDL = 6414` (+ crop offset), a slot 66600 steps tall (5550 line pairs at 600 dpi) | panorama preview |
| Image | `0x01 = 0x23`, `0x02 = 0x30`, `0x33 = 0x0E`, `0xAF = 0xFF`, scan motor tables after the feed; first chunk announced on `wIndex 0x08`, the rest on `0x00` | every image pass |
| End | after an image pass only: `03 = 30, 20`, `01 = 22`, `0x15` bit 4 off, stop armed, `02 = 30`, `03 = 10, 00`, IR LED and toggle bit off, wait for the AGOHOME park | after each preview frame |

The AFE reuses the SilverFast two-point probe and gain law
(`calib_gl128.offsets_for_black_level`, `coarse_gains_from_wide_means`) on
the 135i strips: optical black is columns 2–29 of both lines (captured probes
are dark to column 30), and the gain is sized for the brighter of the visible
and IR lines, since one AFE gain serves both. The calibration strips at the
loader home calibrate both banks, and the shading survives the per-frame
register rewrite (VueScan does not re-upload it for frames 2 and 3).

The frame slot is one frame pitch (10752 steps, 37.9 mm) by the full sensor
width (1.44 in); `area` is normalised over it (over the 66600-step strip for
`frame=None`). A feed plus the image pass must end inside the loader travel:
73722 steps, the panorama preview's own travel (feed 6414 + `LINCNT` 11218 =
5609 line pairs × 12 steps). That is past the INI `PageSize` height (72000),
so `PageSize` is not the travel limit.

**Multi-exposure and Multi-Pass** use the family pass sequence and merge
(`Gl128ScanSession._run_multi_pass`): each pass is its own feed from the
loader home and its own toggle-mode pass, and the IR plane comes from the
first short pass. Not at 7200 dpi (as on the flatbed models). The long
exposure is filled in (fixed: `me_fixed_long_exposure = 42000`, 3× short).
`0x90` follows the vendor rule `11000 · (n + 2)` with `n = round(E / 14000)`
(55000 at 42000; measured only at `n = 2`). The long cap is
`64000 · 3600 / Ydpi` (`me_long_exposure_cap`): the scan motor word here is
`E · Ydpi / 3600`, not `E / oversample`.

**Watcher**: `Scanner.watch_loader(stop=…)` polls the holder sensor and the
interrupt endpoint every 190 ms (VueScan's idle interval) and reports
`inserted` / `removed` / `eject`; by default it loads on insert and ejects
on the button. VueScan's idle register rewrites (`36=fc 3a=00 33=0e 32=15`)
are not replayed.

### 11.9 Open gaps (135i)

- No run on 135i hardware.
- Shading gain unity `0x4000` is VueScan's first upload on this model
  (`0x06 = 0xF8`); the SE family uses `0x2000` (`0x06 = 0xF0`). Confirm the
  scale on hardware.
- The AFE gain targets are the SE's; VueScan's own codes (`2A 1F 25`) are
  a little lower.
- The two front-panel GPIO requests VueScan sends at the end of each frame
  (`0x8B` idx `0x0B` and `0x0F`) and the `0x8D` request after the image read
  are not sent.
- `StaggeredLine = 4` (odd/even pixels at 7200 dpi) is in the line margin but
  not applied to the image, as on the other GL128 models.
- No 135i multi-exposure capture: the long exposure and its `0x90` are
  filled in. As on the flatbed models, the shading is measured again at the
  loader home before the first long pass.
- The image `LINCNT` margin is the INI rule (`ceil(48·Y/7200) + 1`, plus 4 at
  7200 dpi: 53 / 9 / 5); VueScan uses its own margins, so it is not checked
  on the wire.
- Dummy and pixel clocks at 720, 900, 1800 and 3600 dpi, and shading strips
  at 7200 dpi, are filled from the nearest measured rung.
- Multi-exposure and the plain (non-toggle) IR and colour modes are not
  captured; the session always scans in toggle mode.
- Bulk image payloads: a capture with a larger snap length would show the
  IR channel content and the visible/IR line registration on real pixels.

## Open capture gaps

### 8300i SE

See [§10.9](#109-open-gaps-8300i-se).

### 135i (usbmon set)

See [§11.9](#119-open-gaps-135i).

### 8100 V2

- Product string / configuration descriptor (session 01 did not fetch strings)
- Slope ROM payload byte-equality vs SE (driver assumes identical)
- Vendor `wIndex=0x20` / `0x18` meaning

Closed by hardware measurement (Scan Lab TIFFs, 2026-10-16): the 7200 dpi
"jaggies" are a period-2 column stagger of 4 rows (same CCD stagger the GL845
leaves correct); 1440/2400 show 0.8/1.35 rows. Fixed by host unstagger
(`(4, 0)` @7200, `(1, 0)` @1440/2400, pre-mirror) plus rounded `ld_shift` as
on the SE leaves; 1200/3600 flat. Full-frame 7200 loses 4 output rows.

The 8300i SE leaf is in-tree with `scan_ready=True`; §10 is the capture
synthesis behind that leaf. The 135i leaf is in-tree with
`scan_ready=False`; §11 is the synthesis behind it.
