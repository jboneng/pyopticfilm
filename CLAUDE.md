# pyopticfilm — Project Rules for Claude

These rules always apply. They were migrated from `.cursor/rules/` (`changelog.mdc`, `model-lock.mdc`).

## Changelog policy

Follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Keep `## [Unreleased]` as the first version heading (immediately under the intro).

### Unreleased (default)

When completing **notable** work (user-facing features, fixes, behaviour changes, docs that users would notice, new examples/tools), **update `CHANGELOG.md` in the same change**:

- Add or extend bullets under `### Added`, `### Fixed`, `### Changed`, `### Removed`, or `### Notes for integrators` as they already appear in this file.
- Create a subsection only when it has at least one bullet; drop empty subsections.
- Merge into an existing bullet when it is the same change; do not duplicate.
- Skip trivial noise: formatting-only, ruff/lint, test-only internals, comment-only, or chore commits with no user-visible effect.
- **Do not** invent a versioned release (`## [x.y.z]`), dates, or version-bump prose while working under Unreleased.
- **Do not** bump `src/pyopticfilm/_version.py` unless the user asks for a version or release prep.

### Versioned releases (only when asked)

When the user **explicitly** asks to cut a release or write release notes:

- Move Unreleased items into a new `## [x.y.z] - YYYY-MM-DD` block **above** older versions.
- Leave `## [Unreleased]` in place (empty, no leftover subsections).
- Match existing heading names and Keep a Changelog style.

### Contributor credits

Credit **external** contributors where it is appropriate (merged PRs, hardware enablement, substantial patches).

- **Do not** credit the maintainer **jboneng**.
- Put credits under `### Contributors` in that **release** block (not Unreleased, unless the user asks).
- Format each name as a markdown link to that person's GitHub profile: `[@username](https://github.com/username)`.

```markdown
### Contributors

- [@TobbyTravel](https://github.com/TobbyTravel) for OpticFilm 8100 (V2) support.
```

## Model lock tests

Frozen model-lock tests must not be retargeted to fit other-model code.

`tests/model_lock/` holds frozen **driver-path** oracles per hardware-validated model:

- `tests/model_lock/opticfilm_8200i_se/`
- `tests/model_lock/opticfilm_8100_v2/`
- `tests/model_lock/opticfilm_8300i_se/`
- `tests/model_lock/opticfilm_7600i_v1/`
- `tests/model_lock/opticfilm_7600i_v2/`

### Never without an explicit user request

Do **not** edit, delete, skip, xfail, loosen, parametrize, or move files under `tests/model_lock/` unless the user **explicitly** asks to update **that model's** lock tests.

Do **not** copy lock tests into `tests/` with weaker asserts to get a green run.

Do **not** add a lock folder for another model unless the user asks.

### When lock tests fail

- Change is for a **different** model: specialize that model (flags, leaf class, session branch). Leave other lock oracles unchanged.
- User is working on the **lock model's** driver: **stop and ask** before changing expected values. A failure may be a real regression.

### Allowed without asking

Read lock tests. Run `uv run pytest -m model_lock`. Change production code so lock tests keep passing.
