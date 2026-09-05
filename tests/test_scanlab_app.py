# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan Lab main window: bracket selector and ME mode wiring.

Runs each check in a subprocess (see tests/test_scanlab_widgets.py's
_pyqt6_widget_constructible/_run_me_controls_script) — constructing any
QWidget aborts in-process under pytest in this sandbox.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.test_scanlab_widgets import _pyqt6_widget_constructible

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_app_script(body: str) -> str:
    script = (
        "import os\n"
        "os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')\n"
        "from PyQt6.QtWidgets import QApplication\n"
        "app = QApplication.instance() or QApplication([])\n"
        "import numpy as np\n"
        "from pyopticfilm.image import ScanImage\n"
        "from pyopticfilm.scan.me_debug import BracketDebug, MeScanDebug\n"
        "from tools.scanlab.app import ScanLabWindow\n"
        "win = ScanLabWindow()\n"
        "gl128_idx = next(i for i, t in enumerate(win._targets) if t.model.asic == 'GL128')\n"
        "win.device.setCurrentIndex(gl128_idx)\n"
        + textwrap.dedent(body)
        + "\nwin.close()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        env={"QT_QPA_PLATFORM": "offscreen", **_env()},
        check=False,
    )
    assert result.returncode == 0, (
        f"subprocess failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def _env() -> dict[str, str]:
    import os

    return dict(os.environ)


def _make_brackets(n: int) -> str:
    return (
        f"            brackets = [BracketDebug(rgb=np.full((8,8,3), 100*i, dtype='uint16'), "
        f"exposure=14000*(i+1), align_shift=None if i==0 else (0.1*i, 0.2*i)) "
        f"for i in range({n})]\n"
    )


@pytest.mark.skipif(
    not _pyqt6_widget_constructible(),
    reason="QWidget cannot be constructed in this sandbox",
)
class TestBracketSelector:
    def test_n_exposure_scan_populates_and_shows_selector(self):
        out = _run_app_script(
            _make_brackets(5)
            + """
            debug = MeScanDebug(
                rgb_short=np.zeros((8,8,3), dtype='uint16'), rgb_long=brackets[-1].rgb,
                exposure_short=14000, exposure_long=70000, brackets=brackets,
            )
            win._on_me_debug_ready(debug)
            img = ScanImage(rgb=np.zeros((8,8,3), dtype='uint16'), dpi=1800)
            win._on_scan_ready(img)
            assert win.bracket_selector.count() == 5
            assert win.bracket_selector_row.isHidden() is False
            assert win.bracket_selector.currentIndex() == 4
            print("PASS")
            """
        )
        assert "PASS" in out

    def test_two_bracket_scan_hides_selector(self):
        out = _run_app_script(
            _make_brackets(2)
            + """
            debug = MeScanDebug(
                rgb_short=np.zeros((8,8,3), dtype='uint16'), rgb_long=brackets[-1].rgb,
                exposure_short=14000, exposure_long=70000, brackets=brackets,
            )
            win._on_me_debug_ready(debug)
            img = ScanImage(rgb=np.zeros((8,8,3), dtype='uint16'), dpi=1800)
            win._on_scan_ready(img)
            assert win.bracket_selector.count() == 2
            assert win.bracket_selector_row.isHidden() is True
            print("PASS")
            """
        )
        assert "PASS" in out

    def test_selecting_a_bracket_updates_the_view(self):
        out = _run_app_script(
            _make_brackets(5)
            + """
            debug = MeScanDebug(
                rgb_short=np.zeros((8,8,3), dtype='uint16'), rgb_long=brackets[-1].rgb,
                exposure_short=14000, exposure_long=70000, brackets=brackets,
            )
            win._on_me_debug_ready(debug)
            img = ScanImage(rgb=np.zeros((8,8,3), dtype='uint16'), dpi=1800)
            win._on_scan_ready(img)
            win.bracket_selector.setCurrentIndex(2)
            shown = win.me_long_view._view.raw_rgb()
            assert shown is not None
            assert int(shown[0, 0, 0]) == 200  # brackets[2].rgb fill value (100*2)
            print("PASS")
            """
        )
        assert "PASS" in out

    def test_non_me_scan_clears_selector(self):
        out = _run_app_script(
            _make_brackets(5)
            + """
            debug = MeScanDebug(
                rgb_short=np.zeros((8,8,3), dtype='uint16'), rgb_long=brackets[-1].rgb,
                exposure_short=14000, exposure_long=70000, brackets=brackets,
            )
            win._on_me_debug_ready(debug)
            img = ScanImage(rgb=np.zeros((8,8,3), dtype='uint16'), dpi=1800)
            win._on_scan_ready(img)
            assert win.bracket_selector.count() == 5

            win._on_me_debug_ready(None)
            img2 = ScanImage(rgb=np.zeros((8,8,3), dtype='uint16'), dpi=1800)
            win._on_scan_ready(img2)
            assert win.bracket_selector.count() == 0
            assert win.bracket_selector_row.isHidden() is True
            print("PASS")
            """
        )
        assert "PASS" in out
