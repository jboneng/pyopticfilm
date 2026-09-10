# SPDX-License-Identifier: GPL-3.0-or-later
"""Calibrator.run's search_afe dispatch: signature probe, not except TypeError.

Regression for a shim that caught bare TypeError to fall back from GL845's
search_afe(method, resolution, measure) shape to GL128's search_afe(config,
method) shape. That also swallowed a genuine TypeError raised inside
measure() or the dichotomy internals, silently degrading calibration to
table defaults instead of surfacing the bug.
"""

from __future__ import annotations

import pytest

from pyopticfilm.scan.calibrate import call_search_afe


def test_dispatches_gl845_shape_with_method_resolution_measure():
    calls: list[dict] = []

    def search_afe(*, method=None, resolution=None, measure=None):
        calls.append({"method": method, "resolution": resolution, "measure": measure})

    def measure(fe):
        return (0.0, 0.0, 0.0)

    call_search_afe(search_afe, method="transparency", resolution=1800, measure=measure)

    assert calls == [{"method": "transparency", "resolution": 1800, "measure": measure}]


def test_dispatches_gl128_shape_with_method_only():
    calls: list[dict] = []

    def search_afe(*, config=None, method=None):
        calls.append({"config": config, "method": method})

    call_search_afe(search_afe, method="transparency", resolution=1800, measure=lambda fe: (0.0, 0.0, 0.0))

    assert calls == [{"config": None, "method": "transparency"}]


def test_typeerror_inside_measure_propagates_instead_of_falling_back():
    """A real bug in measure() must not be mistaken for a signature mismatch."""
    fallback_calls: list[str] = []

    def search_afe(*, method=None, resolution=None, measure=None):
        measure(object())  # exercises the caller-supplied measure

    def broken_measure(fe):
        fallback_calls.append("called")
        raise TypeError("int() argument must be a string, not 'float32'")

    with pytest.raises(TypeError, match="float32"):
        call_search_afe(search_afe, method="transparency", resolution=1800, measure=broken_measure)

    assert fallback_calls == ["called"]
