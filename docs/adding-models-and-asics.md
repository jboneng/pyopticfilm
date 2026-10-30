# Adding a model or an ASIC

This guide describes where code for a new scanner goes and how to lay it out.
The structure is the same for every ASIC family: one driver and one scan
session per chip, a common base class holding the tables every model in the
family shares, and one leaf class per model holding only what differs. A leaf
can never change another model's behaviour.

The GL128 family (OpticFilm 8200i SE, 8100 V2 and 8300i SE) is the reference
implementation. Paths below use placeholders: `<chip>` / `<family>` for the
ASIC (`gl128`), `<Family>` for its class prefix (`Gl128`), `<FAMILY>` for its
constants (`GL128`) and `<model>` for the scanner (`8300i_se`). GL128 examples
follow each rule.

Read [CONTRIBUTING.md](../CONTRIBUTING.md) first. This guide puts its rules
("specialize, do not retarget", model-lock oracles, hardware sign-off) into
concrete file layouts.

There are two cases:

- **A new model on an ASIC the driver already supports.** This is the usual
  case: add a leaf to an existing family. The 8100 V2 and the 8300i SE were
  added this way. See [Adding a model](#adding-a-model-on-an-existing-asic).
- **A new ASIC, or a protocol the existing driver for that chip cannot speak.**
  Create a new family. The GL128 driver and the 7600i v1 GL843 driver were
  added this way. See [Adding an ASIC](#adding-an-asic).

## Where things live

| Layer | Path | Holds | GL128 |
|---|---|---|---|
| USB identity | `usb/device.py` | `PID_OPTICFILM_*` constants, `SUPPORTED_IDS`, user-facing "which models scan" messages | |
| Model contract | `device/protocol.py` | `FilmModel` (every model), a `<Family>Model` protocol listing the knobs the family's code requires, `AsicDriver` | `Gl128Model` |
| Family base | `device/<family>_common.py` | Base dataclass, tables identical across the family, shared/divergent field catalogs | `gl128_common.py`, `Gl128Common` |
| Model leaf | `device/model_<model>.py` | Identity, capture-proven divergences, per-model method overrides | `model_8300i_se.py` |
| Large tables | `device/tables_<model>.py` | Motor slope ROMs, generated tables | `tables_8300i_se.py` |
| Model selection | `device/select.py` | `KNOWN_MODELS`, `model_for_device()` (PID + bcdDevice), `create_asic()` | |
| Chip driver | `asic/<chip>.py` | Boot, lamp, motor, status, AFE, shading upload | `Gl128` |
| Register map | `asic/registers.py` | Named addresses and bits per chip | `Gl128Registers` |
| Scan session | `scan/session_<chip>.py` | Per-scan register programming, acquire, multi-pass; routed by `create_session()` in `scan/session.py` | `Gl128ScanSession` |
| Calibration | `scan/calib_<chip>.py` | AFE search and shading maths specific to the chip | `calib_gl128.py` |
| Scan gate | `scanner.py` | `_ensure_scan_ready()`, which blocks motor operations until `scan_ready` is true | |

All paths are under `src/pyopticfilm/`. Shared host-side code
(`scan/geometry.py`, `scan/pipeline.py`, `scan/calibrate.py`,
`scan/bringup.py`) serves every model. It reads model attributes and calls
model methods. It must not branch on model identity.

## Principles

1. **Captures are the source of truth.** Every register value comes from a USB
   capture of the vendor driver. Each constant names the capture (file,
   session, frame and register bytes) that proves it. When a table mixes
   measured and inferred entries, mark each entry `# measured` or
   `# filled (...)`, as the 8300i SE tables do. Paths of the form
   `captures/<model>/<session>/...` in code comments refer to the
   [pyopticfilm_captures](https://github.com/jboneng/pyopticfilm_captures)
   dataset, which is a separate repository, not a folder in this one.
2. **Leaves subclass the family base, never each other.** `Model8100V2` and
   `Model8300iSE` subclass `Gl128Common`, not `Model8200iSE`, so a fix to one
   model's constants cannot reach another model through inheritance. Each
   family has a test that enforces this (`tests/test_gl128_siblings.py`).
3. **Shared code calls a model hook. It does not check which model it has.**
   When the 8300i SE needed its own slope ROM, the GL128 driver stopped
   importing the SE table and called `self.model.slope_table_fast()` instead.
   Each leaf returns its own table. Do not add `if model.usb_product_id == ...`,
   `isinstance(model, Model<Name>)` or `getattr(model, "new_knob", default)` to
   family code. Older GL128 code does not follow this yet: `asic/gl128.py` and
   `scan/session_gl128.py` still read about 35 model knobs through `getattr`
   with a fallback (for example `image_read_chunk_bytes`, the `me_*` fields and
   `shading_strip_clocks`). Don't copy that pattern. When you touch one of those
   lines, read the attribute directly; the knob is already on `Gl128Model`.
4. **New knobs are required, not optional.** Add the field or method to the
   `<Family>Model` protocol and either declare it on every leaf or give it one
   shared value in the family base. A default that silently copies one model's
   value is how one model's fix breaks another.
5. **Do not touch other models' oracles.** Your new model has no lock folder
   until it is hardware-validated and the maintainer asks for one. If a lock
   test for another model fails, change your leaf, not the oracle.

## Adding a model on an existing ASIC

The 8300i SE is the most complete example. Its first commit added the leaf,
the slope ROM, registration and goldens with `scan_ready=False`. A second
commit, after hardware sign-off, flipped `scan_ready`, updated the user-facing
messages and added the lock folder.

### 1. Write the leaf class

Create `device/model_<model>.py`, subclassing the family base:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
"""OpticFilm <Name> model definition (<CHIP>).

The <Name> (``07b3:xxxx``) uses the <CHIP> ASIC and is a sibling of <models>.
It subclasses :class:`<Family>Common`, **not** any leaf, so sibling capture
constants cannot leak through inheritance.

**<divergent knob> = <value>**
    What it controls, what the siblings use, and what goes wrong on this
    hardware with a sibling's value.
    *Capture evidence*: ``<file>.pcapng`` frame N, registers 0x.. = ...
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pyopticfilm.device.<family>_common import ALL_PPI, <Family>Common

# Per-dpi tables: one entry per supported PPI, each marked measured or filled.
_LPERIOD_BY_DPI_<MODEL>: dict[int, int] = {
    150: 11067,  # measured
    720: 11110,  # filled (sibling ladder; no capture)
    ...
}
assert set(_LPERIOD_BY_DPI_<MODEL>) == set(ALL_PPI)


@dataclass(frozen=True)
class Model<Name>(<Family>Common):
    """OpticFilm <Name> — <CHIP> sibling; not yet scan_ready."""

    name: str = "plustek-opticfilm-<name>"
    model: str = "OpticFilm <Name>"
    usb_product_id: int = 0xXXXX
    supports_infrared: bool = ...
    scan_ready: bool = False

    # ...every other name in <FAMILY>_DIVERGENT_FIELDS, each with its evidence...

    # Method overrides only where captures prove different behaviour.


MODEL_<NAME> = Model<Name>()
```

Points to follow:

- **Declare every divergent field**, including fields whose value happens to
  match a sibling. The family's sibling test fails if one is missing, and
  spelling out a matching value (`exposure_lperiod: int = 14000`) records that
  someone checked it on this model.
- **Copy shared tables with `field(default_factory=lambda: dict(TABLE))`.** The
  dataclass is frozen but the mappings are not, so each leaf needs its own
  copy.
- **Assert key coverage for per-dpi tables** at import time, so a missing PPI
  fails immediately instead of falling back to a default in the middle of a
  scan.
- **Override methods when the rule differs, not only the value.** The 8300i SE
  overrides `feed_to_scan_steps_for_dpi` (its second feed depends on PPI),
  `channel_exposure_for` (it runs the motor at twice the speed), its slope
  tables and `shading_strip_clocks`. The 8100 V2 overrides only
  `shading_strip_clocks`, for a single 7200 dpi value, and calls `super()` for
  every other case.
- **Define host-assembly hooks explicitly.** `scan/geometry.py` looks up
  `stagger_y_for` and `scaled_ld_shift` with `getattr` because those hooks
  span families. Define both on every leaf that needs them, with the leaf's
  own table, so the behaviour is visible in the leaf.

### 2. Put large tables in their own module

When a model has its own motor slope ROM or another large generated table,
put it in `device/tables_<model>.py` and return it from the leaf's override.
The module docstring names the capture and the RAM addresses the table was
uploaded to, and says how it differs from the sibling tables. Generated modules
say how to regenerate them (see the header of `tables_8200i_se.py`).

### 3. Add a new knob correctly

If the new model differs in a way no existing field or method covers:

1. Add the field or method to `<Family>Model` in `device/protocol.py`, with a
   short comment explaining what it controls.
2. Decide where it belongs:
   - **One value for every model:** add it to the family base and to the
     shared field catalog.
   - **Varies by model:** add it to the divergent field catalog and declare it
     on **every** leaf, citing the capture on each one. For the existing
     models, write down the value they use today so their behaviour does not
     change. Their lock tests show whether it did.
3. Read it in family code as a plain attribute (`model.new_knob`).
4. Assert the per-model values in the family's sibling test.

On GL128, `asic_clock_boot_0x8c`, `asic_clock_scan_0x8c` and `asic_boot_0x0b`
were added this way when the 8300i SE showed that the boot and scan clocks
differ by model.

### 4. Register the model

| File | Change |
|---|---|
| `usb/device.py` | Add a `PID_OPTICFILM_<MODEL>` constant with a comment giving the ASIC and status, and add it to `SUPPORTED_IDS` |
| `device/select.py` | Import the singleton, add a `model_for_device()` branch (match on `bcd_device` if the PID is shared, as with `0x0C3A` / `0x0C3B`) and add it to `KNOWN_MODELS` |
| `device/__init__.py`, `pyopticfilm/__init__.py` | Export `MODEL_<NAME>` and `Model<Name>` |
| Family helpers | Any PID list or model table the family keeps outside the leaf (GL128: `is_gl128_opticfilm()` in `scan/bringup.py`, `tools/dump_gl128_setup_trace.py`) |
| `tools/scanlab/` | Check that capture decode uses the selected model's tables |

`create_asic()` and `create_session()` route on `model.asic`, so a new leaf in
an existing family needs no change there.

### 5. Add tests (no lock folder yet)

- **Family sibling test:** add the leaf to the list of leaves, check it does
  not subclass a sibling, and assert its divergent values.
- `tests/test_multi_model.py`: PID coverage in `SUPPORTED_IDS` and
  `model_for_device(PID) is MODEL_<NAME>`.
- `tests/test_sane_opticfilm_port.py`: `test_scan_ready_validate_set` lists
  the scan-ready models explicitly. Leave the new model out until sign-off.
- **Setup goldens:** dump the register program for `init()` plus the session's
  configure step at a few resolutions into `tests/traces/python/<model>/` and
  add the cases to the family's golden test (GL128:
  `tests/scanners/protocol/test_gl128_setup.py`). Before committing, compare
  the dump with the vendor capture. Never copy another model's JSON.
- If you change shared code for the new model, add parametrized invariant
  tests (even USB widths, IR refused when unsupported) in ordinary `tests/`.

Run the whole suite, including every existing lock oracle:

```bash
uv run ruff check .
uv run pytest -q
uv run pytest -m model_lock
```

### 6. Keep `scan_ready=False` until hardware sign-off

While `scan_ready` is false, `Scanner._ensure_scan_ready()` blocks scan,
calibrate, home and park, and motor moves stay disabled. Open, status, lamp,
register dumps and Scan Lab capture decode still work, which is enough to
compare register programming against captures.

Flip it only after the checklist in
[scanner-validation.md](scanner-validation.md#hardware-sign-off-scan-ready--lock-oracle-edits)
passes on that hardware: park; full-frame scans at representative low, middle,
and maximum supported resolutions; a crop; multi-exposure if implemented for
that model; and IR if the model supports it. The sign-off change then:

- sets `scan_ready: bool = True` and updates the leaf docstring;
- updates the model lists in the `_ensure_scan_ready()` error in `scanner.py`
  and in the `UnsupportedDeviceError` / `DeviceNotFoundError` messages and PID
  comment in `usb/device.py`;
- adds the model to `test_scan_ready_validate_set`;
- adds `tests/model_lock/opticfilm_<model>/`, **only when the maintainer asks
  for it**, plus its entry in `_LOCK_FOLDERS` in
  `tests/model_lock/test_lock_coverage.py` and in
  `tests/model_lock/README.md` and `.cursor/rules/model-lock.mdc`. The existing
  folders cover the model contract, bring-up geometry, geometry, slope tables,
  ME configure, Lab scan kwargs and USB end dummy; use them as the pattern.

### 7. Documentation

- The family's comparison doc: a section for the model covering identity,
  boot register differences, slope ROM, timing tables, geometry and open
  capture gaps (GL128: `docs/gl128-model-comparison.md` §10).
- `docs/scanner-validation.md`: the family section and golden paths.
- `README.md`: support table and the scan-ready lists.
- `CHANGELOG.md`: a bullet under `## [Unreleased]`.

## Adding an ASIC

Add a new family when the chip's register map, status location, frontend
access or command framing differs from every existing driver. The GL128
(status at `0x101`, frontend through `0x51`/`0x5D`/`0x5E`) needed its own
driver. So did the 7600i v1: its chip is a GL843, but it reads registers
through `0x83`/`0x84` and acknowledges every write.

If the new chip is close to an existing driver, subclass that driver and
override only what differs (`Gl842` and `Gl843` override little more than
`asic_boot()`). Otherwise write a standalone driver.

### Files

| File | Role | GL128 |
|---|---|---|
| `asic/<chip>.py` | Driver class that satisfies `AsicDriver` (`read_status`, `init`, `lamp_on`/`lamp_off`, `home`, `park`, `stop_motor`, …) plus chip-specific operations | `Gl128` |
| `asic/registers.py` | Frozen dataclass of named addresses and bits, each annotated with the capture that proves it | `Gl128Registers` |
| `device/protocol.py` | `<Family>Model(FilmModel, Protocol)` listing every knob the chip code requires | `Gl128Model` |
| `device/<family>_common.py` | Base dataclass with tables shared by the family, plus `<FAMILY>_SHARED_FIELDS` / `<FAMILY>_DIVERGENT_FIELDS` | `gl128_common.py` |
| `device/model_<model>.py` | First leaf, structured as above | `model_8200i_se.py` |
| `scan/session_<chip>.py` | `ScanSession` subclass that overrides only the chip-specific steps (`_configure`, `_begin_scan`, `_acquire`, `_end_scan`, …) | `session_gl128.py` |
| `scan/calib_<chip>.py` | AFE and shading maths, if it differs from `scan/calibrate.py` | `calib_gl128.py` |
| `tests/test_<family>_siblings.py` | Catalog test: base fields equal the shared catalog, leaf-only fields equal the divergent catalog, no leaf subclasses another | `test_gl128_siblings.py` |
| `docs/` | Register map and protocol synthesis | `register-reference.md`, `gl128-model-comparison.md` |

Start with a family base and one leaf, even when there is only one model. When
a second model arrives it becomes another leaf, and nothing has to be pulled
out of the first model's class.

### Wiring

- `device/select.py`, `create_asic()`: route on the `model.asic` string. Use
  `isinstance` on a leaf only when one chip name needs two drivers (the
  `Gl843V1` case).
- `scan/session.py`, `create_session()`: add the routing and extend the
  docstring list.
- `asic/__init__.py`: export the driver class.
- `scanner.py`: only if the driver needs a different transport or a
  simulated transport for `Scanner.open_fake` (see the `Model7600iV1`
  branches). Keep these branches few and tied to a leaf class.

### Testing without hardware

- **Register goldens:** run `init()` and the session's configure step over
  `MockScannerTransport` and compare against JSON traces
  (`tests/scanners/setup_gl128.py`).
- **Capture playback:** for job-replay drivers, add a fake transport that
  checks the driver's writes against the recorded sequence
  (`usb/fake_gl843_v1.py`).
- **Invariants:** routing in `create_asic` / `create_session`, PID and
  bcdDevice resolution, and refusal of unsupported modes such as IR.

The rest is the same as for a new model: keep `scan_ready=False` until
sign-off, add a lock folder only on request, and update the docs and
changelog.

## Pitfalls

These come from bugs and near misses while adding GL128 siblings. Each one
came from assuming a sibling's value instead of reading this model's capture:

- **Feed distance.** The 8200i SE's old full-frame second feed of 13704 steps
  puts the 8100 V2 head 576 steps (~1 mm) too deep and clips the top of the
  frame. The V2's own capture shows 13128.
- **Motor ramp order.** The vendor driver uploads the slow ramp for the
  reference feed and the fast ramp for the scan-start feed. A fix based on a
  misread capture used the fast ramp for both and caused a mechanical fault on
  hardware.
- **Line-count scale.** The 8200i SE and 8100 V2 program four `LINCNT` units
  per output line. The 8300i SE motor runs twice as fast and needs one unit per
  line. With the sibling factor its carriage travelled twice the frame height
  and hit the end stop.
- **Whole tables.** Running the 8300i SE on 8200i SE tables gave a hot white
  strip, clock-dependent stalls and a rattling motor
  (`docs/gl128-model-comparison.md` §10.8).
- **Filled entries.** The 8300i SE dummy and pixel-clock values at 900 and
  1800 dpi differ from neighbouring rungs. Nearest-band fills were wrong until
  those rungs were captured, so mark filled values as filled.

## Checklist

New model in an existing family:

- [ ] `device/model_<model>.py` subclasses the family base, declares every
      divergent field with capture evidence, `scan_ready=False`
- [ ] `device/tables_<model>.py` if the slope ROM or other large tables differ
- [ ] New knobs added to `<Family>Model`, the field catalog and every leaf
- [ ] PID in `usb/device.py`; `select.py` mapping and `KNOWN_MODELS`; package exports; family helpers
- [ ] Family sibling test, `test_multi_model.py`, setup goldens
- [ ] `uv run pytest -q` and `uv run pytest -m model_lock` pass with no edits to other models' oracles
- [ ] Docs: family comparison doc, scanner validation, README, CHANGELOG
- [ ] After hardware sign-off: `scan_ready=True`, user-facing messages, `test_scan_ready_validate_set`, lock folder if requested

New ASIC: everything above, plus the driver, register map, `<Family>Model`
protocol, family base and catalogs, sibling test, session, `create_asic` /
`create_session` routing and a hardwareless golden or playback test.
