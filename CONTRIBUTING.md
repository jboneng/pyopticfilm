# Contributing

Hardware-tested models today: the GL128 trio (OpticFilm 8200i SE, OpticFilm
8100 V2, OpticFilm 8300i SE) plus the GL843 OpticFilm 7600i v1. The GL128
models share one driver path (`asic/gl128.py`, `scan/session_gl128.py`,
geometry, pipeline, calibration). Slope ROMs are leaf-specific
(`tables_8200i_se.py` for SE/V2; `tables_8300i_se.py` for 8300i). The 7600i
v1 uses its own leaf and session (`device/model_7600i_v1.py`,
`scan/session_7600i_v1.py`). A change that is correct for one model is not
automatically correct for the others.

Adding a model or an ASIC? See
[docs/adding-models-and-asics.md](docs/adding-models-and-asics.md) for the file
layout, registration points and tests, using the GL128 siblings as the template.

## Specialize, do not retarget

- Put model-specific constants and flags on **that model's** class
  (`device/model_8200i_se.py`, `device/model_8100_v2.py`,
  `device/model_8300i_se.py`, or `device/model_7600i_v1.py`). Do not change a
  sibling's defaults to make a fix work.
- The 8100 V2 does **not** subclass the SE tables. Shared identical tables live
  in `device/gl128_common.py`. Capture-proven differences are listed in
  `GL128_DIVERGENT_FIELDS`. Adding a GL128 field requires updating that catalog
  (and both leaf classes when it is a divergent knob).
- Do not introduce `getattr(model, "new_knob", se_default)` in shared GL128
  code. Add a required field on `Gl128Model` / both leaves instead.

## Model-lock tests vs invariants

- **Oracles** live under `tests/model_lock/<model>/`. They freeze driver-path
  numbers for one hardware-validated model. Do not parametrize them across
  models, copy them into `tests/` with weaker asserts, or edit them to green
  another model's change. See [tests/model_lock/README.md](tests/model_lock/README.md).
- **Invariants** (even USB widths, IR refused when unsupported) may be
  parametrized over every GL128 model in ordinary tests.

Run lock tests with `uv run pytest -m model_lock`.

Add a lock folder only when a model is hardware-validated and the maintainer
asks. Every `scan_ready` model must have a lock folder.

## Hardware sign-off

CI has no scanner. Protocol goldens and lock tests do not prove motors, AFE, or
USB races. Before flipping `scan_ready` or changing a model's lock oracles,
confirm on that hardware: park; full-frame scans at representative low, middle,
and maximum supported resolutions; a crop; multi-exposure if implemented for
that model; and IR if the model supports it. See
[docs/scanner-validation.md](docs/scanner-validation.md).

## Development

```bash
uv sync --all-groups
uv run ruff check .
uv run pytest -q
```
