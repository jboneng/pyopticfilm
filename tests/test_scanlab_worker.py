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
    # request_prescan = pyqtSignal(object, bool, object)
    positional, keyword_only = _method_args("run_prescan")
    assert positional == ["target", "apply_calib", "gl128_prime"]
    assert keyword_only == []


def test_scan_request_has_n_brackets_field():
    tree = ast.parse(_WORKER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ScanRequest":
            fields = {
                t.target.id
                for t in node.body
                if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)
            }
            assert "n_brackets" in fields
            return
    raise AssertionError("ScanRequest not found")


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
