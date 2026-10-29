# SPDX-License-Identifier: GPL-3.0-or-later
"""examples/scan.py: default full frame, and ME / IR prompts that follow the model."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from pyopticfilm.device.model_7600i_v1 import MODEL_7600I_V1
from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.image import ScanImage

_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "scan.py"


class _FakeScanner:
    def __init__(self, model, calls: list[dict]) -> None:
        self.model = model
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def warmup(self) -> None:
        pass

    def scan(self, **kwargs) -> ScanImage:
        self._calls.append(kwargs)
        ir = np.zeros((4, 4), dtype=np.uint16) if kwargs.get("infrared") else None
        return ScanImage(rgb=np.zeros((4, 4, 3), dtype=np.uint16), dpi=kwargs["resolution"], ir=ir)


def _run_example(monkeypatch, tmp_path: Path, model, answers: list[str]) -> tuple[list[dict], list[str]]:
    spec = importlib.util.spec_from_file_location("example_scan", _EXAMPLE)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    calls: list[dict] = []
    prompts: list[str] = []
    replies = iter(answers)

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(replies)

    info = SimpleNamespace(product_id=model.usb_product_id, bcd_device=0x0400, device_id="fake")
    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(example, "find_devices", lambda: [info])
    monkeypatch.setattr(example, "model_for_device", lambda *_a: model)
    monkeypatch.setattr(example, "Scanner", SimpleNamespace(open=lambda _id: _FakeScanner(model, calls)))
    monkeypatch.setattr("sys.argv", ["scan.py", "--out", str(tmp_path)])
    assert example.main() == 0
    return calls, prompts


@pytest.mark.parametrize(
    ("model", "answers", "multi_exposure", "infrared"),
    [
        (MODEL_8200I_SE, ["1", "1800", "y", "y"], True, True),
        (MODEL_8100_V2, ["1", "1800", "y"], True, False),  # no IR prompt
        (MODEL_7600I_V1, ["1", "1440"], False, False),  # no ME prompt; IR not offered at 1440
        (MODEL_7600I_V1, ["1", "3600", "y"], False, True),  # IR at 3600
    ],
)
def test_example_scans_default_frame_with_model_options(monkeypatch, tmp_path, model, answers, multi_exposure, infrared):
    calls, _prompts = _run_example(monkeypatch, tmp_path, model, answers)
    assert len(calls) == 1
    call = calls[0]
    # The default full frame, as in the README: no Lab bring-up geometry or area.
    assert "geometry" not in call and "area" not in call
    assert call["resolution"] == int(answers[1])
    assert call["multi_exposure"] is multi_exposure
    assert call["infrared"] is infrared
    assert (tmp_path / "colour.tif").exists()
    assert (tmp_path / "ir_plane.tif").exists() is infrared
