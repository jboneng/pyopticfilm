# SPDX-License-Identifier: GPL-3.0-or-later
"""GL845 hardware bring-up CLI safety and dry-run tests."""

from __future__ import annotations

import pytest

from tools.bringup_gl845_8200i import _save_rgb, main


def test_7600i_v2_dry_run_names_target_and_steps(capsys):
    assert main(["--model", "7600i-v2", "--steps", "open,status", "--dry-run"]) == 0

    output = capsys.readouterr().out
    assert "GL845 OpticFilm 7600i (v2)" in output
    assert "1. open" in output
    assert "2. status" in output


@pytest.mark.parametrize("step", ["init", "home", "tiny", "park", "calib", "ir", "combo", "me"])
def test_hardware_steps_require_explicit_unlock(step):
    with pytest.raises(SystemExit, match="ASIC init, motor, and image steps need --allow-unvalidated"):
        main(["--model", "7600i-v2", "--steps", step])


def test_8200i_remains_default_target(capsys):
    assert main(["--steps", "open,status", "--dry-run"]) == 0

    assert "GL845 OpticFilm 8200i" in capsys.readouterr().out


def test_rgb_output_names_match_saved_files(tmp_path):
    import numpy as np

    image = np.arange(18, dtype=np.uint16).reshape(2, 3, 3)
    output = tmp_path / "tiny_900"

    _save_rgb(image, output)

    assert np.array_equal(np.load(output.with_suffix(".npy")), image)
    assert output.with_suffix(".ppm").read_bytes().startswith(b"P6\n3 2\n255\n")
    assert not output.with_suffix(".tif").exists()
