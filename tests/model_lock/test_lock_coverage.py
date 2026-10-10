# SPDX-License-Identifier: GPL-3.0-or-later
"""Every scan-ready model must have a frozen lock folder."""

from __future__ import annotations

from pathlib import Path

from pyopticfilm.device.select import KNOWN_MODELS

_LOCK_ROOT = Path(__file__).resolve().parent

# Folder names under tests/model_lock/ keyed by FilmModel.name.
_LOCK_FOLDERS = {
    "plustek-opticfilm-8200i-se": "opticfilm_8200i_se",
    "plustek-opticfilm-8100-v2": "opticfilm_8100_v2",
    "plustek-opticfilm-7600i-v1": "opticfilm_7600i_v1",
}


def test_every_scan_ready_model_has_a_lock_folder():
    ready = [m for m in KNOWN_MODELS if m.scan_ready]
    assert ready, "expected at least one scan_ready model"
    missing = []
    for model in ready:
        folder = _LOCK_FOLDERS.get(model.name)
        if folder is None or not (_LOCK_ROOT / folder).is_dir():
            missing.append(model.name)
    assert not missing, f"scan_ready models without tests/model_lock/<folder>: {missing}"


def test_lock_folders_are_nonempty():
    for folder in _LOCK_FOLDERS.values():
        tests = list((_LOCK_ROOT / folder).glob("test_*.py"))
        assert tests, f"{folder} has no test_*.py lock files"
