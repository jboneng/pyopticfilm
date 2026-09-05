# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan Lab preview conversion (no GUI window)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

from tools.scanlab.preview import MAX_DISPLAY_EDGE, downsample_for_display

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyqt6_gui_importable() -> bool:
    """True when PyQt6 is installed *and* its native libs load (needs libEGL on Linux CI)."""
    if importlib.util.find_spec("PyQt6") is None:
        return False
    try:
        from PyQt6.QtGui import QImage  # noqa: F401
    except ImportError:
        return False
    return True


def _pyqt6_widget_constructible() -> bool:
    """True when a bare QWidget() can actually be constructed in-process.

    Some CI/sandboxed environments can import PyQt6's GUI classes (QImage
    etc.) fine but abort (SIGABRT) the moment any QWidget is constructed
    inside a pytest worker process — reproduces even for an empty QWidget()
    with no custom code involved. MeControls tests below need real widgets,
    so they run in a subprocess instead (see _run_me_controls_script) and
    are skipped outright when even that can't work.
    """
    if not _pyqt6_gui_importable():
        return False
    probe = textwrap.dedent(
        """
        from PyQt6.QtWidgets import QApplication, QWidget
        app = QApplication.instance() or QApplication([])
        QWidget()
        print("OK")
        """
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env={"QT_QPA_PLATFORM": "offscreen", **_minimal_env()},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "OK" in result.stdout


def _minimal_env() -> dict[str, str]:
    import os

    return dict(os.environ)


def _run_me_controls_script(body: str) -> str:
    """Run ``body`` (indented Python using MeControls/MeMode) in a fresh
    subprocess under QT_QPA_PLATFORM=offscreen and return its stdout.

    Out-of-process because constructing any QWidget aborts in-process under
    pytest in this environment (see _pyqt6_widget_constructible) — this
    mirrors how the behavior was manually verified during development.
    """
    script = (
        "from PyQt6.QtWidgets import QApplication\n"
        "app = QApplication.instance() or QApplication([])\n"
        "from tools.scanlab.widgets import MeControls, MeMode\n"
        + textwrap.dedent(body)
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        env={"QT_QPA_PLATFORM": "offscreen", **_minimal_env()},
        check=False,
    )
    assert result.returncode == 0, (
        f"subprocess failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def test_downsample_for_display_caps_long_edge():
    arr = np.arange(80 * 200 * 3, dtype=np.uint16).reshape(80, 200, 3)
    out = downsample_for_display(arr, max_edge=50)
    assert max(out.shape[:2]) <= 50
    assert out.shape[2] == 3
    assert downsample_for_display(arr, max_edge=400) is arr


@pytest.mark.skipif(
    not _pyqt6_gui_importable(),
    reason="PyQt6 GUI libs unavailable (install lab extra + libEGL on Linux)",
)
def test_rgb16_to_qimage_odd_width_and_downsample():
    from tools.scanlab.widgets import rgb16_to_qimage

    rgb = np.full((32, 17, 3), 0x4000, dtype=np.uint16)
    rgb[:, 8, :] = 0xC000
    image = rgb16_to_qimage(rgb, auto_level=True)
    assert not image.isNull()
    assert image.width() == 17
    assert image.height() == 32

    wide = np.zeros((80, 5000, 3), dtype=np.uint16)
    wide[:, ::17] = 0x8000
    preview = rgb16_to_qimage(wide, auto_level=True)
    assert not preview.isNull()
    assert max(preview.width(), preview.height()) <= MAX_DISPLAY_EDGE


@pytest.mark.skipif(
    not _pyqt6_gui_importable(),
    reason="PyQt6 GUI libs unavailable (install lab extra + libEGL on Linux)",
)
def test_commit_crop_norm_keeps_current_when_candidate_is_none():
    from tools.scanlab.widgets import commit_crop_norm

    current = (0.1, 0.2, 0.9, 0.8)
    assert commit_crop_norm(current, None) == current
    assert commit_crop_norm(None, None) is None
    assert commit_crop_norm(current, (0.2, 0.3, 0.7, 0.6)) == (0.2, 0.3, 0.7, 0.6)


@pytest.mark.skipif(
    not _pyqt6_widget_constructible(),
    reason="QWidget cannot be constructed in this sandbox (PyQt6 GUI libs "
    "or offscreen platform plugin unavailable) — see "
    "_pyqt6_widget_constructible",
)
class TestMeControls:
    """MeControls: one mode enum as single source of truth, replacing the
    old me_pass/me_fixed_long/me_n_brackets checkbox+spinbox tangle.

    Each test runs its widget-constructing body in a fresh subprocess (see
    _run_me_controls_script) — constructing any QWidget aborts in-process
    under pytest in this sandbox, reproducing even for a bare QWidget()
    with no custom code involved.
    """

    def test_default_mode_is_off(self):
        out = _run_me_controls_script(
            """
            w = MeControls()
            assert w.mode() == MeMode.OFF
            assert w.me_pass_enabled() is False
            assert w.n_brackets_value() == 2
            assert w.me_exposure_mode_kwarg() is None
            print("PASS")
            """
        )
        assert "PASS" in out

    def test_dynamic_mode(self):
        out = _run_me_controls_script(
            """
            w = MeControls()
            w.set_mode(MeMode.DYNAMIC)
            assert w.me_pass_enabled() is True
            assert w.n_brackets_value() == 2
            assert w.me_exposure_mode_kwarg() is None
            assert w.n_brackets.isEnabled() is False
            print("PASS")
            """
        )
        assert "PASS" in out

    def test_fixed_fast_mode(self):
        out = _run_me_controls_script(
            """
            w = MeControls()
            w.set_mode(MeMode.FIXED_FAST)
            assert w.me_pass_enabled() is True
            assert w.n_brackets_value() == 2
            assert w.me_exposure_mode_kwarg() == "fixed"
            print("PASS")
            """
        )
        assert "PASS" in out

    def test_n_exposure_mode_enables_brackets_spinbox(self):
        out = _run_me_controls_script(
            """
            w = MeControls()
            w.set_mode(MeMode.N_EXPOSURE)
            assert w.me_pass_enabled() is True
            assert w.n_brackets.isEnabled() is True
            w.n_brackets.setValue(5)
            assert w.n_brackets_value() == 5
            # me_exposure_mode still None by default: N-Exposure defers to
            # the model's own me_default_exposure_mode, same as Dynamic.
            assert w.me_exposure_mode_kwarg() is None
            print("PASS")
            """
        )
        assert "PASS" in out

    def test_set_gl128_enabled_false_forces_off(self):
        out = _run_me_controls_script(
            """
            w = MeControls()
            w.set_mode(MeMode.N_EXPOSURE)
            w.set_gl128_enabled(False)
            assert w.mode() == MeMode.OFF
            assert w.isEnabled() is False
            print("PASS")
            """
        )
        assert "PASS" in out

    def test_manual_exposure_kwargs_empty_without_debug_toggle(self):
        out = _run_me_controls_script(
            """
            w = MeControls()
            w.set_mode(MeMode.DYNAMIC)
            w.me_short_exposure.setText("30000")  # typed, debug toggle off
            kw = w.manual_exposure_kwargs()
            assert kw == {
                "single_pass_exposure": None,
                "me_short_exposure": None,
                "me_long_exposure": None,
            }
            print("PASS")
            """
        )
        assert "PASS" in out

    def test_manual_exposure_kwargs_routes_by_me_active(self):
        out = _run_me_controls_script(
            """
            w = MeControls()
            w.debug_toggle.setChecked(True)

            w.set_mode(MeMode.OFF)
            w.single_pass_exposure.setText("12345")
            kw = w.manual_exposure_kwargs()
            assert kw["single_pass_exposure"] == 12345
            assert kw["me_short_exposure"] is None
            assert kw["me_long_exposure"] is None

            w.set_mode(MeMode.DYNAMIC)
            w.me_short_exposure.setText("6789")
            w.me_long_exposure.setText("99999")
            kw = w.manual_exposure_kwargs()
            # single_pass_exposure field itself is unaffected by mode, but
            # is not the active override once ME is on.
            assert kw["single_pass_exposure"] is None
            assert kw["me_short_exposure"] == 6789
            assert kw["me_long_exposure"] == 99999
            print("PASS")
            """
        )
        assert "PASS" in out

    def test_manual_exposure_kwargs_cleared_when_mode_returns_to_off(self):
        """A value typed while ME was active must not leak into a later
        non-ME scan once the mode flips back to Off — same guarantee the
        old checkbox-toggle stale-field-clearing gave, but derived from
        current widget state rather than an imperative clear() on toggle."""
        out = _run_me_controls_script(
            """
            w = MeControls()
            w.debug_toggle.setChecked(True)
            w.set_mode(MeMode.DYNAMIC)
            w.me_short_exposure.setText("6789")
            w.set_mode(MeMode.OFF)
            kw = w.manual_exposure_kwargs()
            assert kw["me_short_exposure"] is None
            assert kw["single_pass_exposure"] is None
            print("PASS")
            """
        )
        assert "PASS" in out

    def test_changed_signal_fires_on_mode_switch(self):
        out = _run_me_controls_script(
            """
            w = MeControls()
            fired = []
            w.changed.connect(lambda: fired.append(1))
            w.set_mode(MeMode.N_EXPOSURE)
            assert len(fired) >= 1
            print("PASS")
            """
        )
        assert "PASS" in out
