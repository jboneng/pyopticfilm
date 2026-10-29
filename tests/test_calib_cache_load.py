# SPDX-License-Identifier: GPL-3.0-or-later
"""A damaged calibration cache file must never stop the scanner from opening."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pytest

from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.scan.calibrate import CACHE_IDENT, CACHE_VERSION, CalibCache, CalibEntry
from pyopticfilm.scanner import Scanner


def _good(resolution: int = 1800) -> dict:
    return CalibEntry(
        method="transparency",
        resolution=resolution,
        startx=0,
        pixels=4,
        dark=np.zeros((4, 3), dtype=np.uint16),
        white=np.full((4, 3), 65535, dtype=np.uint16),
    ).to_dict()


def _write(path: Path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _file(entries) -> dict:
    return {"ident": CACHE_IDENT, "version": CACHE_VERSION, "entries": entries}


_DAMAGED = [
    {**_good(), "dark": [0] * 11},  # wrong length: reshape fails
    {k: v for k, v in _good().items() if k != "pixels"},  # missing key
    {**_good(), "shading_blob_b64": "not base64!"},  # bad base64
    {**_good(), "pixels": "four"},  # wrong type
    ["not", "an", "entry"],
    None,
]


def test_damaged_entries_are_skipped_and_good_ones_kept(tmp_path: Path, caplog):
    path = tmp_path / "calib.json"
    _write(path, _file([_good(1800), *_DAMAGED, _good(3600)]))
    cache = CalibCache(path)
    with caplog.at_level(logging.WARNING, logger="pyopticfilm"):
        assert cache.load() is True
    assert sorted(e.resolution for e in cache.entries) == [1800, 3600]
    assert sum("Skipping damaged calib cache entry" in r.getMessage() for r in caplog.records) == len(_DAMAGED)

    cache.save()  # the damaged entries are not written back
    assert len(json.loads(path.read_text(encoding="utf-8"))["entries"]) == 2


@pytest.mark.parametrize(
    "payload",
    [
        ["a", "list"],
        42,
        {"ident": CACHE_IDENT, "version": "not a number", "entries": []},
        {"ident": CACHE_IDENT, "version": CACHE_VERSION, "entries": {"not": "a list"}},
    ],
)
def test_malformed_file_is_ignored(tmp_path: Path, payload):
    path = tmp_path / "calib.json"
    _write(path, payload)
    cache = CalibCache(path)
    assert cache.load() is False
    assert cache.entries == []


def test_scanner_opens_with_a_damaged_cache(tmp_path: Path):
    """The reported repro: one entry whose dark length does not match pixels."""
    path = tmp_path / "calib.json"
    _write(path, _file([{**_good(), "dark": [0] * 29, "pixels": 10}]))
    scanner = Scanner.open_fake(MODEL_8200I_SE, calib_cache=path)
    try:
        assert scanner.calibrator.cache.entries == []
    finally:
        scanner.close()
