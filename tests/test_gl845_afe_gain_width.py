# SPDX-License-Identifier: GPL-3.0-or-later
"""GL845 FE registers are 8-bit; AfeSearchConfig.gain_max defaults to 0x1FF
(the GL128 width). force_dichotomy=True on GL845 must not search codes that
get silently truncated by the ``& 0xFF`` register write.
"""

from __future__ import annotations

from pyopticfilm.asic.gl845 import Gl845
from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.scan.calib_gl128 import AfeFrontend
from pyopticfilm.usb.fake import FakeUsbTransport
from pyopticfilm.usb.protocol import GenesysUsbProtocol


def _gl845() -> Gl845:
    usb = FakeUsbTransport()
    usb.registers[0x04] = 0x22
    proto = GenesysUsbProtocol(usb)
    asic = Gl845(proto, MODEL_8200I)
    asic._initialized = True
    asic._reg_cache = {0x04: 0x22}
    return asic


def test_force_dichotomy_default_config_does_not_exceed_8bit_gain():
    """With no config override, a bright strip must not push the search past 0xFF."""
    asic = _gl845()

    def measure(fe: AfeFrontend) -> tuple[float, float, float]:
        # Always-dim strip (target is 0xD000): dichotomy keeps climbing gain
        # toward code_max (0x1FF by default) looking for more signal.
        return (100.0, 100.0, 100.0)

    result = asic.search_afe(method="transparency", measure=measure, force_dichotomy=True)

    assert all(0 <= g <= 0xFF for g in result.gains), result.gains
    assert all(0 <= o <= 0xFF for o in result.offsets), result.offsets
    # What the search converged on must match what was actually written to
    # the 8-bit FE registers -- no silent truncation on apply.
    assert asic.last_afe_gains == result.gains
    assert asic.last_afe_offsets == result.offsets
