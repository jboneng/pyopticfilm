# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan Lab CLI option parsing and passthrough to Scanner.scan()."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from tools.scanlab import cli
from tools.scanlab.cli import build_parser


def _parse(argv: list[str]):
    return build_parser().parse_args(argv)


_BASE = ["scan", "--model", "OpticFilm 8200i SE", "--mock", "--kind", "scan"]


def test_defaults_match_scanner_scan_defaults():
    args = _parse(_BASE)
    assert args.n_passes == 1
    assert args.align_passes is True
    assert args.single_pass_exposure is None
    assert args.me_short_exposure is None
    assert args.me_long_exposure is None


def test_no_align_passes_flag():
    args = _parse([*_BASE, "--no-align-passes"])
    assert args.align_passes is False


def test_n_passes_and_manual_overrides_parse_as_ints():
    args = _parse(
        [
            *_BASE,
            "--n-passes",
            "5",
            "--single-pass-exposure",
            "30000",
            "--me-short-exposure",
            "14000",
            "--me-long-exposure",
            "42000",
        ]
    )
    assert args.n_passes == 5
    assert args.single_pass_exposure == 30000
    assert args.me_short_exposure == 14000
    assert args.me_long_exposure == 42000


class _FakeTarget:
    def __init__(self):
        self.model = SimpleNamespace(model="OpticFilm 8200i SE", asic="GL128")
        self.mock = True


class _FakeScanner:
    def __init__(self):
        self.scan = MagicMock(
            return_value=SimpleNamespace(rgb=np.zeros((4, 4, 3), dtype=np.uint16), dpi=300)
        )
        self.close = MagicMock()


def _patch_scan_pipeline(monkeypatch, fake_scanner):
    monkeypatch.setattr(cli, "_find_target", lambda *_a, **_k: _FakeTarget())
    monkeypatch.setattr(cli, "open_lab_scanner", lambda *_a, **_k: (fake_scanner, MagicMock()))
    monkeypatch.setattr(cli, "lab_scan_kwargs", lambda *_a, **_k: {})
    monkeypatch.setattr(cli, "prescan_resolution", lambda *_a, **_k: 300)
    monkeypatch.setattr(cli, "detect_anomalies", lambda *_a, **_k: [])
    fake_run = MagicMock()
    fake_run.finish.return_value = MagicMock(__str__=lambda self: "fake-run-dir")
    monkeypatch.setattr(cli, "ForensicRun", lambda *_a, **_k: fake_run)


def test_n_passes_out_of_range_is_rejected(monkeypatch):
    fake_scanner = _FakeScanner()
    _patch_scan_pipeline(monkeypatch, fake_scanner)
    with pytest.raises(SystemExit):
        cli.main(["scanlab", *_BASE, "--n-passes", "20"])
    fake_scanner.scan.assert_not_called()


def test_new_flags_reach_scanner_scan(monkeypatch):
    fake_scanner = _FakeScanner()
    _patch_scan_pipeline(monkeypatch, fake_scanner)
    rc = cli.main(
        [
            "scanlab",
            *_BASE,
            "--multi-exposure",
            "--n-passes",
            "5",
            "--no-align-passes",
            "--single-pass-exposure",
            "30000",
            "--me-short-exposure",
            "14000",
            "--me-long-exposure",
            "42000",
        ]
    )
    assert rc == 0
    fake_scanner.scan.assert_called_once()
    kwargs = fake_scanner.scan.call_args.kwargs
    assert kwargs["multi_exposure"] is True
    assert kwargs["n_passes"] == 5
    assert kwargs["align_passes"] is False
    assert kwargs["single_pass_exposure"] == 30000
    assert kwargs["me_short_exposure"] == 14000
    assert kwargs["me_long_exposure"] == 42000
