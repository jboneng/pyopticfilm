# SPDX-License-Identifier: GPL-3.0-or-later
"""Validation for explicit (manual) ``REG_EXPOSURE`` overrides.

Manual overrides exist so Scan Lab / debugging can send exposure values that
exceed the driver's normal adaptive/safety clamps (see
:mod:`pyopticfilm.scan.me_exposure` and ``FilmModel.me_hardware_max_exposure``).
"Unrestricted" means unrestricted with respect to those *soft* limits — the
only hard limit is the actual register representation, so this module rejects
values that cannot be written to the 24-bit ``REG_EXPOSURE`` register rather
than clamping or wrapping them.
"""

from __future__ import annotations

from numbers import Integral

#: Largest value representable in the 24-bit REG_EXPOSURE register.
MAX_EXPOSURE_REGISTER = 0xFFFFFF

#: Largest Multi-Pass repeat count — parity with PR #52's tested N-bracket
#: ceiling (2-9), even though Multi-Pass is a different mechanism.
MAX_N_PASSES = 9


def _is_integer(value: object) -> bool:
    """An integer, including NumPy's, but not ``bool`` (``True`` is not a count or an exposure)."""
    return isinstance(value, Integral) and not isinstance(value, bool)


def validate_n_passes(value: int) -> int:
    """Return ``value`` as an ``int`` if it is a valid ``n_passes``, else raise."""
    if not _is_integer(value):
        raise TypeError(f"n_passes must be an int, got {type(value).__name__}")
    if not (1 <= value <= MAX_N_PASSES):
        raise ValueError(f"n_passes must be between 1 and {MAX_N_PASSES}, got {value}")
    return int(value)


def validate_manual_exposure(value: int | None, *, label: str) -> int | None:
    """Return ``value`` as an ``int`` if it is a valid manual exposure, else raise.

    ``None`` (no override requested) is always valid and preserves today's
    behavior. Otherwise ``value`` must be a positive integer that fits
    ``REG_EXPOSURE`` (1..0xFFFFFF) — invalid values raise ``ValueError``
    rather than being silently clamped or wrapped. NumPy integers are
    accepted; ``bool`` is not (``True`` would otherwise mean exposure 1).
    """
    if value is None:
        return None
    if not _is_integer(value):
        raise TypeError(f"{label} must be an int or None, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{label} must be a positive integer, got {value}")
    if value > MAX_EXPOSURE_REGISTER:
        raise ValueError(
            f"{label}={value} exceeds the 24-bit REG_EXPOSURE maximum "
            f"(0x{MAX_EXPOSURE_REGISTER:06X} = {MAX_EXPOSURE_REGISTER})"
        )
    return int(value)
