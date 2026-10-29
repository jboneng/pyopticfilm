# SPDX-License-Identifier: GPL-3.0-or-later
"""Calib cache entries are only reused on the model/timing they were measured with."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np

from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.model_8300i_se import MODEL_8300I_SE
from pyopticfilm.scan.calibrate import CACHE_IDENT, CalibCache, CalibEntry, Calibrator, calib_fingerprint


def _entry(blob: bytes) -> CalibEntry:
    return CalibEntry(
        method="transparency",
        resolution=1800,
        startx=0,
        pixels=8,
        dark=np.zeros((8, 3), dtype=np.uint16),
        white=np.full((8, 3), 65535, dtype=np.uint16),
        asic_shading=True,
        shading_blob=blob,
        afe_gains=(38, 30, 36),
        afe_offsets=(19, 31, 23),
    )


def _find(cache: CalibCache) -> CalibEntry | None:
    return cache.find(method="transparency", resolution=1800, startx=0, pixels=8)


def test_fingerprint_tracks_model_exposure_and_sensor_regs():
    prints = {calib_fingerprint(m) for m in (MODEL_8200I_SE, MODEL_8100_V2, MODEL_8300I_SE)}
    assert len(prints) == 3
    assert calib_fingerprint(MODEL_8300I_SE) == calib_fingerprint(replace(MODEL_8300I_SE))
    assert calib_fingerprint(MODEL_8300I_SE) != calib_fingerprint(replace(MODEL_8300I_SE, exposure_short=14000))
    old_timing = replace(MODEL_8300I_SE, sensor_custom_regs=dict(MODEL_8200I_SE.sensor_custom_regs))
    assert calib_fingerprint(MODEL_8300I_SE) != calib_fingerprint(old_timing)


def test_entries_from_other_timing_are_ignored_but_kept(tmp_path: Path):
    path = tmp_path / "calib.json"
    old_timing = replace(MODEL_8300I_SE, sensor_custom_regs=dict(MODEL_8200I_SE.sensor_custom_regs))
    stale = Calibrator(None, cache_path=path, model=old_timing)
    stale.cache.upsert(_entry(b"OLD"))
    stale.cache.save()

    current = Calibrator(None, cache_path=path, model=MODEL_8300I_SE, reuse_saved=True)
    assert _find(current.cache) is None

    current.cache.upsert(_entry(b"NEW"))
    current.cache.save()
    reloaded = Calibrator(None, cache_path=path, model=MODEL_8300I_SE, reuse_saved=True)
    hit = _find(reloaded.cache)
    assert hit is not None and hit.shading_blob == b"NEW"
    assert {e.shading_blob for e in reloaded.cache.entries} == {b"OLD", b"NEW"}
    assert _find(Calibrator(None, cache_path=path, model=old_timing, reuse_saved=True).cache).shading_blob == b"OLD"


def test_pre_fingerprint_cache_file_is_ignored(tmp_path: Path):
    path = tmp_path / "calib.json"
    legacy = _entry(b"LEGACY").to_dict()
    legacy.pop("fingerprint")
    path.write_text(json.dumps({"ident": CACHE_IDENT, "version": 10, "entries": [legacy]}), encoding="utf-8")

    cal = Calibrator(None, cache_path=path, model=MODEL_8200I_SE)
    assert cal.cache.entries == []


def test_saved_entries_are_not_reused_by_a_new_session(tmp_path: Path):
    """GL128 units share one USB serial and drift between sessions: measure once per session."""
    path = tmp_path / "calib.json"
    first = Calibrator(None, cache_path=path, model=MODEL_8200I_SE)
    first.cache.upsert(_entry(b"SESSION1"))
    first.cache.save()
    assert _find(first.cache).shading_blob == b"SESSION1"  # same session: reused

    second = Calibrator(None, cache_path=path, model=MODEL_8200I_SE)
    assert _find(second.cache) is None
    second.cache.upsert(_entry(b"SESSION2"))
    second.cache.save()
    assert _find(second.cache).shading_blob == b"SESSION2"
    # Replaced, not appended: the file keeps one entry per window.
    reloaded = Calibrator(None, cache_path=path, model=MODEL_8200I_SE, reuse_saved=True)
    assert [e.shading_blob for e in reloaded.cache.entries] == [b"SESSION2"]
