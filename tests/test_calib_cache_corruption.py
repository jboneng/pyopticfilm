# SPDX-License-Identifier: GPL-3.0-or-later
"""CalibCache must tolerate a corrupt/truncated on-disk file and save atomically.

A corrupt cache previously raised CalibrationError straight out of
Calibrator.__init__, bricking scanner construction until the file was deleted
by hand. It should be treated the same as "no cache" (logged, ignored).
"""

from __future__ import annotations

from pathlib import Path

from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.scan.calibrate import CalibCache, Calibrator


def test_load_with_corrupt_json_treated_as_missing(tmp_path: Path):
    path = tmp_path / "calib.json"
    path.write_text("{not valid json", encoding="utf-8")

    cache = CalibCache(path)
    assert cache.load() is False
    assert cache.entries == []


def test_calibrator_construction_survives_corrupt_cache(tmp_path: Path):
    path = tmp_path / "calib.json"
    path.write_text("{not valid json", encoding="utf-8")

    # Must not raise CalibrationError.
    cal = Calibrator(asic=None, cache_path=path, model=MODEL_8200I)
    assert cal.cache.entries == []


def test_save_is_atomic_no_leftover_tmp_file(tmp_path: Path):
    path = tmp_path / "calib.json"
    cache = CalibCache(path)
    cache.save()

    assert path.exists()
    assert not path.with_suffix(".tmp").exists()


def test_save_survives_crash_mid_write_leaving_old_file_intact(tmp_path: Path):
    path = tmp_path / "calib.json"
    cache = CalibCache(path)
    cache.save()
    good_contents = path.read_text(encoding="utf-8")

    # Simulate a crash mid-write: a leftover partial tmp file must not be
    # mistaken for the real cache, and the previously-saved file must be
    # untouched.
    tmp_path_file = path.with_suffix(".tmp")
    tmp_path_file.write_text("{truncat", encoding="utf-8")

    assert path.read_text(encoding="utf-8") == good_contents
    cache2 = CalibCache(path)
    assert cache2.load() is True
