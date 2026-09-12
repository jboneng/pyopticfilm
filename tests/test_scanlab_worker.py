# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan Lab worker slot signatures (no display, no PyQt import)."""

from __future__ import annotations

import ast
from pathlib import Path

_WORKER = Path(__file__).resolve().parents[1] / "tools" / "scanlab" / "worker.py"


def _method_args(name: str) -> tuple[list[str], list[str]]:
    tree = ast.parse(_WORKER.read_text(encoding="utf-8"))
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == "ScanWorker"):
            continue
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == name:
                positional = [a.arg for a in item.args.args if a.arg != "self"]
                keyword_only = [a.arg for a in item.args.kwonlyargs]
                return positional, keyword_only
    raise AssertionError(f"ScanWorker.{name} not found")


def test_run_scan_accepts_positional_signal_args():
    positional, keyword_only = _method_args("run_scan")
    assert positional == ["request"]
    assert keyword_only == []


def test_run_prescan_accepts_positional_signal_args():
    # request_prescan = pyqtSignal(object, bool)
    positional, keyword_only = _method_args("run_prescan")
    assert positional == ["target", "apply_calib"]
    assert keyword_only == []


def test_request_scan_is_single_object_signal():
    tree = ast.parse(_WORKER.read_text(encoding="utf-8"))
    names = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    assert "ScanRequest" in names
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == "ScanWorker"):
            continue
        for item in node.body:
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id == "request_scan":
                        assert isinstance(item.value, ast.Call)
                        assert len(item.value.args) == 1
                        return
    raise AssertionError("ScanWorker.request_scan not found")


def _dataclass_field_defaults(name: str) -> dict[str, object]:
    tree = ast.parse(_WORKER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            defaults: dict[str, object] = {}
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    value = item.value
                    if isinstance(value, ast.Constant):
                        defaults[item.target.id] = value.value
                    else:
                        defaults[item.target.id] = ...  # non-constant default, present but unchecked
            return defaults
    raise AssertionError(f"{name} not found")


def test_scan_request_has_align_passes_defaulting_true():
    defaults = _dataclass_field_defaults("ScanRequest")
    assert defaults.get("align_passes") is True


def test_run_keyword_only_args_include_align_passes():
    _positional, keyword_only = _method_args("_run")
    assert "align_passes" in keyword_only
    assert "n_passes" in keyword_only
