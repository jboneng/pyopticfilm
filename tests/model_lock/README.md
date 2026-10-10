# Model lock tests

Frozen **driver-path** oracles for hardware-validated models. They encode how
that model's scan window, motor tables, crop mapping, and flags behaved when
the tests were written.

Current lock folders:

- [`opticfilm_8200i_se/`](opticfilm_8200i_se/) — OpticFilm 8200i SE (`07b3:1825`)
- [`opticfilm_8100_v2/`](opticfilm_8100_v2/) — OpticFilm 8100 (V2) (`07b3:1824`)
- [`opticfilm_7600i_v1/`](opticfilm_7600i_v1/) — OpticFilm 7600i v1 (`07b3:0c3b`, bcdDevice 4.00)

- Add a sibling folder only when that model is hardware-validated and the
  maintainer asks for a lock.
- Do not retarget assertions here to make another model's change pass. Specialize
  the other model (flags, session branch, `gl128_common` catalog) instead.
- Updating oracles for a model requires an explicit request from someone working
  on **that** model (see `.cursor/rules/model-lock.mdc` and
  [CONTRIBUTING.md](../../CONTRIBUTING.md)).
- Shared GL128 files serve **both** locked models. A change there must keep
  every folder in this tree green.

Run only this tree:

```bash
uv run pytest -m model_lock
```
