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
an upcoming `Model8300iSE` leaf (not an alias of the 8200i SE).

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

Host software is not the same: **SilverFast 9** (8100 V2, 8200i SE, 8300i SE)
versus **VueScan** (135i). A VueScan number is still a real wire value, but
it is not automatically a SilverFast equivalent.

### Sources

| Model | USB | Captures | What was used here |
|-------|-----|----------|--------------------|
| 8100 V2 | `07b3:1824` | `pyopticfilm_captures/8100-v2` (SilverFast 9, USBPcap, 2026-09-05) | Session `02_cold_boot_open`, `04_color_7200`, `06_ppi_ladder` via `tools/capture_ledger.py`; leaf class `model_8100_v2.py`; `docs/register-reference.md` |
| 8200i SE | `07b3:1825` | `pyopticfilm_captures/8200i-se` (SilverFast 9, USBPcap, 2026-07/08) | `decoded/gl128_tables.json`, `decoded/ppi_lincnt_feed.json`, `13_ppi_ladder/decoded_ppi_ladder.json`; `model_8200i_se.py` / `gl128_common.py` |
| 8300i SE | `07b3:181f` (`bcdDevice=0x0702`, proven in the pcaps) | Local `8300i_captures/` set (SilverFast 9, USBPcap; full-frame 300 / 1200+IR+ME / 2400 / 3600+ME / 7200) | §10 synthesis; leaf `model_8300i_se.py` (`scan_ready=False`) |
| 135i | `07b3:1436` | `plustek 135i captures` (VueScan, usbmon linktype 220, 2026-08-31) | `135i_protocol_analysis.md` (Scan Lab’s USBPcap importer does not read usbmon) |

In-tree shared vs divergent knobs for the two hardware-tested models:
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
| 3 | Full-frame feed 2 | 13128 | 13704 |
| 4 | PPI-ladder feed 2 | 13128 | 13560 |
| 5 | `LPERIOD` at every captured PPI | V2 higher (see timing table) | SE session 13 table |
| 6 | White shading dummy `0x2B` @ 7200 | `0x10` | `0x17` |
| 7 | Ladder image `LINCNT` | taller crop (see geometry) | session 13 table |

pyopticfilm currently ships only the 7200 `LPERIOD` override on V2
(16035 vs 15963). Session `06_ppi_ladder` shows the same “V2 slightly
higher” pattern at 150–3600 dpi as well; those rungs are **not** yet
model-table overrides.

Positioning slope **order** is not a divergence: both models upload
`SLOPE_TABLE_SLOW` then `SLOPE_TABLE_FAST` (PR #56). The old
`use_slow_final_positioning_feed` flag is gone.

### 8300i SE vs SE / V2 — headline (see §10)

| # | Parameter | 8300i SE (SF capture) | Closest sibling |
|---|---------|----------------------|-----------------|
| 1 | USB product ID | `0x181F` | neither (`1825` / `1824`) |
| 2 | Infrared | yes (`0x37` → `0xB4` on IR pass) | SE |
| 3 | Full-frame feed 2 | **PPI-dependent**, 13040…13128 (not SE 13560) | near V2 (13128) |
| 4 | Image height (pyopticfilm `LINCNT`) | V2 ladder / full-frame table (`SF×4`) | V2 |
| 5 | Image-pass `LPERIOD` | **byte-identical to V2** at 300/1200/2400/3600/7200 | V2 |
| 6 | Image / feed exposure | **15000** | neither (SE/V2 use 14000) |
| 7 | Image-pass pixel clock `0xA5`/`0xAB` | different from SE/V2 (larger bytes; speed meaning unproven) | neither |
| 8 | Image-pass dummy `0x2B` | higher than SE `DUMMY_BY_DPI` at every measured PPI | neither |
| 9 | White shading dummy @ 7200 | `0x26` | neither (V2 `0x10`, SE `0x17`) |
| 10 | Slope ROM payloads | CUSTOM heads `0x32BB` then `0x846A` (not SE `0x1FB4`/`0x16DE`) | neither |

Using the 8200i SE leaf on `0x181F` therefore under-scans Y (SE ladder
`LINCNT` / feed2), runs the wrong exposure and shading clocks (white-rail
clip), and ignores a different motor-slope ROM. Image `0xA5`/`0x2B` also
differ; they are not proven to be the SilverFast speed advantage.

---

## 1. Identity and capabilities

| Parameter | 8100 V2 | 8200i SE | 135i | Verdict |
|-----------|---------|----------|------|---------|
| USB vendor | `0x07B3` | `0x07B3` | `0x07B3` | same |
| USB product ID | `0x1824` | `0x1825` | `0x1436` | different |
| USB product string | unknown | `Film Scanner (A2F)` | `Film Scanner(A81)` | different |
| `bcdDevice` | `0x0702` | `0x0702` | unknown | inconclusive |
| ASIC | GL128 | GL128 | GL128 | same |
| pyopticfilm `scan_ready` | yes | yes | unknown | inconclusive |
| Optical resolution | 7200 dpi | 7200 dpi | 7200 dpi | same |
| Output PPI ladder (SilverFast) | 150…3600 in session 06; 7200 in session 04 | 150, 300, 600, 720, 900, 1200, 1440, 1800, 2400, 3600, 7200 | unknown (7200 + two preview modes only) | inconclusive |
| Manual strip feeder | yes | yes | no | different |
| Motorized auto-advance | no | no | yes | different |
| Panorama / long strip | no | no | yes | different |
| Infrared (iSRD) | no | yes | unknown | different |
| Multi-exposure colour | yes (session 07 present) | yes (session 14) | unknown | inconclusive |
| `min_asic_dpi` (DPISET floor) | 600 | 600 | unknown (preview DPISET 400 and 200 are consistent with the same floor) | inconclusive |
| Vendor image-pass depth pair (`0x33`/`0xAF`) | `0x1F`/`0xFF` (8-bit) | `0x1F`/`0xFF` (8-bit, session 13) | unknown (`64bit rgbi` in VueScan filenames) | inconclusive |
| `mirror_x` | yes (pyopticfilm) | yes | unknown | inconclusive |

The three devices are the same ASIC family and the same USB vendor, with
three different product IDs. 135i is a motorized loader with panorama;
SE and V2 are manual feeders. IR is proven only on the SE (V2 has no IR
channel; 135i RGBI filenames and a boot `0x37` of `0x21` are not enough
to call IR confirmed here). VueScan vs SilverFast means 135i PPI coverage
and bit-depth cannot be compared to the SilverFast eleven-rung ladder.

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
| Vendor probe `wIndex=0x21` in scan traffic | present (60 reads in session 04, with `0x20`/`0x18`) | unknown (pyopticfilm polls `0x21`; SE vendor use not separately checked) | present | inconclusive |
| Vendor probe `wIndex=0x20` | present (263 reads in session 04) | unknown | unknown | inconclusive |
| `CLRCNT` before image (`0x0D`) | `0x07` | `0x07` | unknown | inconclusive |
| `START` launch (`0x0F`) | `0x01` | `0x01` | `0x01` | same |
| Bulk IN / OUT endpoints | unknown | `0x81` / `0x02` | unknown | inconclusive |
| Interrupt IN `0x83` present | unknown | yes (unused by pyopticfilm) | yes | inconclusive |
| Interrupt `0x83` used for holder | unknown | no | yes (empty completions on insert; `0x48` seen on eject) | different |

Control, bulk-image, AHB, status `0x101`, and AFE indexing are the same
Genesys GL124/GL128 framing on all three. 135i is the only model whose
captures show the host actually consuming interrupt endpoint `0x83` for
carrier insert/eject. V2 scan captures do include `wIndex=0x21` probes
alongside the more frequent `0x20`/`0x18` polls; that does not by itself
prove the SE vendor driver uses `0x21`.

---

## 3. Cold-boot registers

| Parameter | 8100 V2 | 8200i SE | 135i | Verdict |
|-----------|---------|----------|------|---------|
| Boot blob A `@ 0x000FFF00` | 34-byte preamble seen (session 02) | 34× `0x00` | unknown | inconclusive |
| Boot blob B `@ 0x000FFF01` | 34-byte preamble seen (session 02) | 32× `0x00` + `0x33 0x00` | unknown | inconclusive |
| `INIT_REGS` vs SE table (first write of each address) | **identical** (session 02, 0 diffs) | 116-register blast | 84 of 116 match SE; 32 differ | different |
| `_MEMORY_LAYOUT_REGS` `0xD0`–`0xF8` | addresses written in scan sessions | capture constant | unknown | inconclusive |
| `_FRONTEND_REGS` boot | AFE `0x51`/`0x5D` touched at open | offsets/gains zeroed | unknown | inconclusive |
| `_GPO_REGS` `0xA2`–`0xAE` | written in scan sessions | capture constant | unknown | inconclusive |
| `_SCAN_REGS` overlay pattern | `0x04=0x42` family; image `0x01=0x23` | same | same pattern reported | inconclusive |
| Boot `REG_EXPOSURE` (`0x7D`–`0x7F`) | 11000 (`0x002AF8`) | 11000 (`0x002AF8`) | 30000 (`0x007530`) | different |
| Image/feed exposure (not boot) | 14000 | 14000 | unknown | inconclusive |

V2 cold boot is the SE `INIT_REGS` map. 135i is the same blast with a
block of model-specific constants, including a much higher boot exposure.
Do not confuse **boot** exposure (11000 on SE/V2) with the **image/feed**
value 14000.

### Boot register diffs (addresses that are not identical across models)

V2 first-writes in session `02_cold_boot_open` match the SE `INIT_REGS`
bytes, so the V2 column is that SE value. 135i values are from
`vuescan open.pcapng` as listed in the 135i protocol analysis.

| Reg | 8100 V2 | 8200i SE | 135i | Verdict |
|-----|---------|----------|------|---------|
| `0x03` | `0x20` | `0x20` | `0x00` | different |
| `0x0A` | `0x40` | `0x40` | `0x48` | different |
| `0x13` | `0x08` | `0x08` | `0x0F` | different |
| `0x16` | `0x27` | `0x27` | `0x01` | different |
| `0x18` | `0x10` | `0x10` | `0x14` | different |
| `0x19` | `0x02` | `0x02` | `0x00` | different |
| `0x30` | `0x6F` | `0x6F` | `0xEE` | different |
| `0x31` | `0x00` | `0x00` | `0xFC` | different |
| `0x32` | `0x22` | `0x22` | `0x03` | different |
| `0x33` | `0x04` | `0x04` | `0x8E` | different |
| `0x35` | `0x2F` | `0x2F` | `0xBB` | different |
| `0x36` | `0x1C` | `0x1C` | `0xFC` | different |
| `0x37` (IR GPIO) | `0xC0` | `0xC0` | `0x21` | different |
| `0x38` | `0x44` | `0x44` | `0x60` | different |
| `0x39` | `0x00` | `0x00` | `0x02` | different |
| `0x3B` | `0xFF` | `0xFF` | `0x00` | different |
| `0x3C` | `0xFF` | `0xFF` | `0x00` | different |
| `0x4F` | `0x03` | `0x03` | `0x63` | different |
| `0x5C` | `0x40` | `0x40` | `0x00` | different |
| `0x5E` | `0x1F` | `0x1F` | `0x00` | different |
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

Thirty-two addresses differ on 135i; none of those addresses differ
between V2 and SE at first write. The rest of the 116-register SE table
is reported as matching 135i in that analysis (84/116).

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
| 13704 | not used as full-frame feed 2 | feed 2 full-frame default | not observed | different |
| 13128 | feed 2 all scan types (TA top) | feed 2 top-of-window / preview | not observed | different |
| 13560 | not observed | PPI ladder crop origin | not observed | inconclusive |
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
| `feed_to_scan_steps` | 13128 | 13704 | 17631 (7200 scan) | different |
| `feed_to_scan_top_steps` | 13128 | 13128 | unknown | inconclusive |
| `feed_to_scan_bottom_steps` | 20232 | 20232 | unknown | inconclusive |
| `ladder_feed2_steps` | 13128 | 13560 | unknown | different |
| `scan_window_end_steps` | 27636 | 27636 | unknown | inconclusive |
| `max_feed_steps` | 28292 | 28292 | unknown (insert uses 71490) | different |

SE and V2 share the 28292 reference feed and the same slope-table AHB
windows; they disagree on where feed 2 puts the full frame (13704 vs
13128) and on the ladder crop origin (13560 vs 13128). 135i never shows
those SE/V2 distances. Its 7200 analogue is 17631, and the loader adds
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

| Output PPI | 8100 V2 (capture) | 8200i SE (session 13) | 135i | Verdict |
|------------|-------------------|----------------------|------|---------|
| 150 | 11067 (shared ASIC 600 programming) | 11064 | unknown | different |
| 300 | 11067 | 11064 | unknown | different |
| 600 | 11067 | 11064 | unknown | different |
| 720 | 11110 | 11106 | unknown | different |
| 900 | 11175 | 11170 | unknown | different |
| 1200 | 11283 | 11277 | unknown | different |
| 1440 | 11369 | 11362 | unknown | different |
| 1800 | 11499 | 11490 | unknown | different |
| 2400 | 11715 | 11703 | unknown | different |
| 3600 | 13443 | 13407 | unknown | different |
| 7200 | 16035 | 15963 | 15991 | different |
| ~2400 preview (DPISET 400) | — | — | 11239 | different |
| ~1200 panorama (DPISET 200) | — | — | 11023 | different |

V2 values at 150–3600 are unique image-pass snapshots from
`06_ppi_ladder.pcapng` (150/300/600 collapse to one ASIC program). 7200
is session `04_color_7200` (also 16035 on dark and white shading). SE
values are session 13. 135i 7200 sits **between** SE and V2. pyopticfilm’s
V2 model table still inherits the SE numbers except at 7200.

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
| 35 mm preview | — | — | 400 (~2400 effective) | different |
| panorama preview | — | — | 200 (~1200 effective) | different |

The 7200 formula is the same on all three. Lower-PPI 135i programming is
only known for VueScan preview/panorama, not a SilverFast ladder.

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

### Shading-strip dummy `0x2B` @ 7200

| Strip | 8100 V2 | 8200i SE | 135i | Verdict |
|-------|---------|----------|------|---------|
| Dark (DVDSET off) | `0x17` | `0x17` | unknown | inconclusive |
| White (DVDSET on) | `0x10` | `0x17` | unknown | different |

This is the V2-only shading override in `Model8100V2.shading_strip_clocks`.
The image pass stays `0x17` on both manual feeders.

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

LPERIOD is the clear timing split: V2 is a few counts above SE at every
measured PPI, and 135i 7200 is a third constant. DPISET at 7200 and image
dummy at 7200 are shared. White-shading dummy at 7200 is the other V2
timing/clock oddity. 135i image-pass exposure is not in the decoded
notes used here.

---

## 6. Geometry and LINCNT

| Parameter | 8100 V2 | 8200i SE | 135i | Verdict |
|-----------|---------|----------|------|---------|
| `strpixel_native_units` | yes | yes | unknown | inconclusive |
| `optical_end_inactive_native` | 96 | 96 | unknown | inconclusive |
| `image_lincnt_per_line` | 4 | 4 | unknown | inconclusive |
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
| `0x10000000` | Channel R exposure / image window | yes | yes | yes | same |
| `0x10004000` | Channel G exposure | yes | yes | yes | same |
| `0x10008000` | Channel B exposure | yes | yes | yes | same |
| `0x1000C000` | Slope table (scan) | yes | yes | yes | same |
| `0x10010000` | Slope table (feeds) | yes | yes | yes | same |
| `0x10014000` | Shading coefficients | yes | yes | yes | same |
| `0x10034000` | Panorama table | no | no | yes (panorama only) | different |
| `0x000FFF00` / `0x000FFF01` | Boot blobs | yes | yes | unknown | inconclusive |

The six SE/V2 AHB windows are on 135i too. Panorama adds
`0x10034000`, which does not appear on the manual feeders.

---

## 8. Shading, AFE, IR, cancel

| Parameter | 8100 V2 | 8200i SE | 135i | Verdict |
|-----------|---------|----------|------|---------|
| Shading lines | 128 (driver) | 128 | unknown | inconclusive |
| Shading DPISET | 1200 native (driver) | 1200 native | unknown | inconclusive |
| AFE offset target | `0x1000` (driver) | `0x1000` | unknown | inconclusive |
| AFE gain target | `0xD000` (driver) | `0xD000` | unknown | inconclusive |
| Session-04 colour AFE gains (R,G,B) | unknown (not extracted here) | `0x14`, `0x1F`, `0x17` | unknown | inconclusive |
| Shading blob destination | `0x10014000` | `0x10014000` | upload seen | same |
| IR LED `0x37` bit 2 on IR pass | N/A (no IR) | set (`0xB0` → `0xB4` in session 05) | unknown | different |
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

Phase-1 decode of five SilverFast 9 full-frame USBPcap captures (Windows VM,
USBPcap). Product sticker / marketing name is **8300i SE**; SilverFast may
list the unit as **8300i**. USB product string reported by hardware reporters:
`Film Scanner(A2K)`. `bcdDevice` from reporters: `0x0702` (same family as
SE/V2).

Captures used:

| File | PPI | IR | ME |
|------|-----|----|----|
| `capture_300_full-frame_no-ir_no-me.pcapng` | 300 | no | no |
| `capture_1200_full-frame_with-ir_with-me.pcapng` | 1200 | yes | yes |
| `capture_2400_full-frame_no-ir_no-me.pcapng` | 2400 | no | no |
| `capture_3600_full-frame_no-ir_with-me.pcapng` | 3600 | no | yes |
| `capture_7200_full-frame_no-ir_no-me.pcapng` | 7200 | no | no |

No dedicated cold-boot-only capture; boot/`INIT_REGS` identity vs SE is
**unknown** here. No 150/600/720/900/1440/1800 rungs in this set.

### 10.1 SilverFast `LINCNT` vs pyopticfilm

On every image pass, SilverFast programs a register `LINCNT` such that
`bulk_rows == LINCNT` and `bulk_size == LINCNT × host_width × 6` (16-bit
chunky RGB). That SF value is **exactly** the 8100 V2 pyopticfilm
`ladder_lincnt_by_dpi` / full-frame entry **divided by 4**:

| PPI | SF `LINCNT` | SF×4 (= pyopticfilm V2) | SE ladder |
|-----|-------------|-------------------------|-----------|
| 300 | 605 | **2420** | 2292 |
| 1200 | 1209 | **4836** | 4580 |
| 2400 | 2417 | **9668** | 9156 |
| 3600 | 3625 | **14500** | 13732 |
| 7200 | 7253 | **29012** | 27476 |

Physical height at 7200: `7253 / 7200 × 25.4 ≈ 25.59 mm` (= shared
`y_size_ta_mm`). The SE ladder is shorter by `128 × asic_dpi / 600` at every
rung — the same gap that forced the V2 leaf’s own ladder table.

**Leaf implication:** keep `image_lincnt_per_line=4` and
`usb_image_lincnt_half_lines=True`; store **V2-scale** `LINCNT` (SF×4) in
`ladder_lincnt_by_dpi` / `max_image_lincnt_by_feed2`. Do not program the raw
SF `LINCNT` into the shared GL128 session.

### 10.2 Colour short image-pass table

Values at the first (or short-ME) `wIndex=0x08` image preamble. Feed2 is the
**settled** 24-bit FEEDL (complete `0x3D`/`0x3E`/`0x3F` trio) in 12000–14000
before that preamble — not a mid-byte write.

| PPI | feed2 (settled) | DPISET | SF `LINCNT` | py `LINCNT` (×4) | `LPERIOD` | vs SE | vs V2 | expo | `0xA5`/`0xAB` | SE `0xA5` | dummy `0x2B` | STR / END |
|-----|-----------------|--------|-------------|------------------|-----------|-------|-------|------|---------------|-----------|--------------|-----------|
| 300 | **13040** | 100 | 605 | 2420 | 11067 | +3 | 0 | 15000 | `0x59` | `0x02` | `0x06` | 242 / 10610 |
| 1200 | **13112** | 200 | 1209 | 4836 | 11283 | +6 | 0 | 15000 | `0x12` | `0x02` | `0x07` | 242 / 10610 |
| 2400 | **13124** | 400 | 2417 | 9668 | 11715 | +12 | 0 | 15000 | `0x05` | `0x01` | `0x0B` | 242 / 10610 |
| 3600 | **13126** | 600 | 3625 | 14500 | 13443 | +36 | 0 | 15000 | `0x03` | `0x01` | `0x10` | 242 / 10610 |
| 7200 | **13128** | 1200 | 7253 | 29012 | 16035 | +72 | 0 | 15000 | `0x02` | `0x01` | `0x1F` | 242 / 10610 |

Offsets from V2’s 13128: 88 / 16 / 4 / 2 / 0 steps. ME-long passes at 1200
and 3600 do write **13128**. 300 and 2400 never write 13128.

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
| Colour ME long | **60000** | `0xC0` | adaptive long; `0xA5`/`0xAB` = `0x02` |

`capture_3600_full-frame_no-ir_with-me.pcapng` has colour short @ 15000 then
ME long @ **60000** (no IR). Supports `supports_infrared=True` and the
existing GL128 ME path; long exposure observed is 60000 (within the
14000–85000 clamp), not the 42000 floor.

### 10.4 Shading strips (large RAM reads)

Each colour (re)calib block has two large RAM reads at the scan DPISET
(`LINCNT=128`), then AFE-sized probes earlier at native DPISET 1200.

| PPI | Strip | `r01` | DVDSET | DPISET | `LPERIOD` | dummy `0x2B` | `0xA5` | `0xAB` | expo |
|-----|-------|-------|--------|--------|-----------|--------------|--------|--------|------|
| 300 | dark | `0x03` | off | 100 | 11067 | `0x02` | `0x01` | `0x30` | 15000 |
| 300 | white | `0x23` | on | 100 | 11067 | `0x03` | `0x03` | `0x03` | 15000 |
| 1200 | dark | `0x03` | off | 200 | 11283 | `0x04` | `0x01` | `0x30` | 15000 |
| 1200 | white | `0x23` | on | 200 | 11283 | `0x06` | `0x03` | `0x03` | 15000 |
| 2400 | dark | `0x03` | off | 400 | 11715 | `0x08` | `0x01` | `0x30` | 15000 |
| 2400 | white | `0x23` | on | 400 | 11715 | `0x0B` | `0x02` | `0x02` | 15000 |
| 3600 | dark | `0x03` | off | 600 | 13443 | `0x0C` | `0x01` | `0x30` | 15000 |
| 3600 | white | `0x23` | on | 600 | 13443 | `0x10` | `0x02` | `0x02` | 15000 |
| 7200 | dark | `0x03` | off | 1200 | 16035 | `0x17` | `0x01` | `0x30` | 15000 |
| 7200 | white | `0x23` | on | 1200 | 16035 | **`0x26`** | `0x02` | `0x02` | 15000 |

Dark @ 7200 dummy `0x17` matches SE/V2. White @ 7200 is **`0x26`**, not V2’s
`0x10` or SE’s `0x17`. All shading uses exposure **15000**.

AFE probe traffic (DPISET 1200, sizes 3072 / 62268) appears before the
shading pair on every capture; wide-probe `LPERIOD` examples include 13445
and 16040 depending on PPI.

### 10.5 Motor FEEDL

| FEEDL | Role on 8300i SE |
|------:|------------------|
| 28292 | feed 1 reference (same as SE/V2) |
| 13040 / 13112 / 13124 / 13126 / 13128 | feed 2 full-frame origin (PPI-dependent; see §10.2) |
| 1 | image-pass placeholder |

SE ladder origin 13560 and historical SE full-frame 13704 do **not** appear
as settled feed2 values. Do not treat 13128 as the only 8300i feed2.

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

### 10.7 Why SE-as-8300i fails (maps to reporter symptoms)

| Symptom | Capture-backed cause |
|---------|----------------------|
| Missing bottom of frame | SE ladder `LINCNT` too short vs V2/8300i full-frame height (~1.35 mm at 7200); SE ladder feed2 13560 vs 8300i ~13128 shifts Y |
| Missing right of frame | **Not** a STR/END mismatch (242/10610 on both). Remaining candidates: host dummy trim (`optical_end_inactive_native=96`) or crop, not a different USB window |
| Slower than SilverFast | Image `0xA5`/`0xAB` and dummy **differ** from SE; CUSTOM slope ROM. Larger `0xA5` is not proven to mean a faster clock |
| Colour ASIC shading white clip (≥65000) | SF shading/image exposure **15000** and different white-strip clocks/dummies vs SE 14000 + SE `shading_strip_clocks` |

### 10.8 Open gaps (8300i SE)

- Dedicated cold-boot-only capture. Scan captures’ first-writes are mostly SE `INIT_REGS` with some real-looking diffs (`0x7E`/`0x7F` already 15000; `0x52`–`0x5B` / `0x70`–`0x73`) plus scan-overlay contamination — not a clean boot oracle
- Full PPI ladder (missing 150/600/720/900/1440/1800)
- Hardware confirmation that CUSTOM slope ROM is accepted (park + feeds/scans; wrong slopes have caused hard motor stops on GL128)
- AFE gain/offset search targets vs SE session-04 defaults
- Product string / configuration descriptor from an in-tree capture (PID/`bcdDevice` are in the pcaps)
- Hardware sign-off checklist before `scan_ready`

## Open capture gaps

### 8300i SE

See [§10.8](#108-open-gaps-8300i-se).

### 135i (usbmon set)

- Slope table bytes (FAST/SLOW head words vs `0x16DE` / `0x1FB4`)
- Full SilverFast-style PPI ladder (only VueScan 7200 + two previews)
- White/dark shading dummy @ 7200
- `feed_to_reference_steps` / `feed_steps_per_inch` / scan-window end
- Positioning slope order on each FEEDL
- Multi-frame auto-advance FEEDL table
- Interrupt `0x83` semantics (empty vs `0x48`; holder type)
- USBPcap (linktype 249) re-export, or a usbmon importer in Scan Lab
- IR pass (`0x37`) and ME

### 8100 V2

- Product string / configuration descriptor (session 01 did not fetch strings)
- Slope ROM payload byte-equality vs SE (driver assumes identical)
- Vendor `wIndex=0x20` / `0x18` meaning
- Whether non-7200 `LPERIOD` deltas should become model-table overrides
  (captures say they differ; pyopticfilm currently inherits SE except 7200)

### 8200i SE

- Whether the real vendor driver polls `wIndex=0x21` (pyopticfilm does)

This document does not implement a 135i driver and does not change SE/V2
constants. The 8300i SE leaf is in-tree with `scan_ready=False` pending
hardware sign-off; §10 is the capture synthesis behind that leaf.
