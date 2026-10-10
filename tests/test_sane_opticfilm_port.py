# SPDX-License-Identifier: GPL-3.0-or-later
"""SANE OpticFilm port: dpi tables, session routing, MAXWD, gate."""

from __future__ import annotations

from unittest.mock import MagicMock

from pyopticfilm.device.model_7200 import MODEL_7200
from pyopticfilm.device.model_7200i import MODEL_7200I
from pyopticfilm.device.model_7300 import MODEL_7300
from pyopticfilm.device.model_7400 import MODEL_7400
from pyopticfilm.device.model_7500i import MODEL_7500I
from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
from pyopticfilm.device.select import KNOWN_MODELS, create_asic, model_is_scan_ready
from pyopticfilm.device.sensor_lookup import (
    dummy_pixel_for,
    exposure_lperiod_for,
    frontend_regs_for,
    maxwd_register_value,
)
from pyopticfilm.scan.geometry import compute_geometry
from pyopticfilm.scan.session import ScanSession, create_session
from pyopticfilm.scan.session_gl128 import Gl128ScanSession
from pyopticfilm.scan.session_gl842 import Gl842ScanSession
from pyopticfilm.scan.session_gl843 import Gl843ScanSession


def test_scan_ready_validate_set():
    from pyopticfilm.device.model_7600i_v1 import MODEL_7600I_V1
    from pyopticfilm.device.model_8100_v2 import MODEL_8100_V2

    for m in KNOWN_MODELS:
        if m in (MODEL_8200I_SE, MODEL_8100_V2, MODEL_7600I_V1):
            assert model_is_scan_ready(m) is True
        else:
            assert model_is_scan_ready(m) is False, m.model


def test_create_session_routes_by_asic():
    proto = MagicMock()
    assert isinstance(
        create_session(create_asic(proto, MODEL_8200I_SE), MODEL_8200I_SE),
        Gl128ScanSession,
    )
    assert isinstance(
        create_session(create_asic(proto, MODEL_8200I), MODEL_8200I), ScanSession
    )
    assert type(create_session(create_asic(proto, MODEL_8200I), MODEL_8200I)) is ScanSession
    assert isinstance(
        create_session(create_asic(proto, MODEL_7200I), MODEL_7200I), Gl843ScanSession
    )
    assert isinstance(
        create_session(create_asic(proto, MODEL_7500I), MODEL_7500I), Gl843ScanSession
    )
    assert isinstance(
        create_session(create_asic(proto, MODEL_7300), MODEL_7300), Gl843ScanSession
    )
    assert isinstance(
        create_session(create_asic(proto, MODEL_7200), MODEL_7200), Gl842ScanSession
    )
    assert isinstance(
        create_session(create_asic(proto, MODEL_7400), MODEL_7400), ScanSession
    )


def test_exposure_lperiod_dpi_keyed():
    assert exposure_lperiod_for(MODEL_7200I, 1800, method="transparency") == 0x2538
    assert exposure_lperiod_for(MODEL_7200I, 7200, method="transparency") == 0x19C8
    assert exposure_lperiod_for(MODEL_7200I, 1800, method="infrared") == 0x1F54
    assert exposure_lperiod_for(MODEL_7500I, 1800, method="infrared") == 0x2AF8
    assert exposure_lperiod_for(MODEL_7500I, 1800, method="transparency") == 0x2F44
    assert exposure_lperiod_for(MODEL_8200I, 1800) == 14000
    assert exposure_lperiod_for(MODEL_7200, 3600) == 0x694E
    assert dummy_pixel_for(MODEL_7200, 1800) == 19
    assert dummy_pixel_for(MODEL_8200I, 1800) == 20


def test_frontend_overlay_7200i_at_7200():
    fe = frontend_regs_for(MODEL_7200I, 7200, method="transparency")
    assert fe[0x02] == 0x1B
    assert fe[0x03] == 0x14
    assert fe[0x04] == 0x20
    # Lower dpi keeps base frontend gains
    fe_low = frontend_regs_for(MODEL_7200I, 1800, method="transparency")
    assert fe_low[0x02] == MODEL_7200I.frontend_regs[0x02]


def test_maxwd_per_asic():
    line_bytes = 3600  # e.g. 600 px * 3 * 2
    assert maxwd_register_value(MODEL_8200I, line_bytes=line_bytes, channels=3) == (
        line_bytes * 3
    ) >> 2
    assert maxwd_register_value(MODEL_7200I, line_bytes=line_bytes, channels=3) == (
        line_bytes >> 1
    )
    assert maxwd_register_value(MODEL_7200, line_bytes=line_bytes, channels=3) == line_bytes


def test_geometry_picks_dummy_and_lperiod():
    g7200 = compute_geometry(1800, model=MODEL_7200)
    assert g7200.dummy_pixel == 19
    assert g7200.exposure_lperiod == 0x694E
    g7200i = compute_geometry(7200, model=MODEL_7200I)
    assert g7200i.exposure_lperiod == 0x19C8


def test_gl845_configure_writes_expected_maxwd_and_strpixel():
    """Mocked register dump for GL845 CommandSetGl846 optical block."""
    writes: dict[int, int] = {}

    class _Proto:
        def write_register(self, addr: int, value: int) -> None:
            writes[int(addr)] = int(value) & 0xFF

        def write_ahb(self, *_a, **_k) -> None:
            return None

        def write_registers(self, pairs) -> None:
            for a, v in pairs:
                writes[int(a)] = int(v) & 0xFF

    asic = MagicMock()
    asic.protocol = _Proto()
    asic._reg_cache = dict(MODEL_8200I.boot_register_map())
    asic._scan_method = "transparency"
    asic.set_frontend_init = MagicMock()
    asic.apply_frontend_for_scan = MagicMock()

    geo = compute_geometry(1800, model=MODEL_8200I)
    session = ScanSession(asic, MODEL_8200I)
    session._configure(geo)

    # STRPIXEL / ENDPIXEL / DPISET / LPERIOD are 16-bit big-endian at 0x30 / 0x32 / 0x2C / 0x38
    strpixel = (writes[0x30] << 8) | writes[0x31]
    endpixel = (writes[0x32] << 8) | writes[0x33]
    dpiset = (writes[0x2C] << 8) | writes[0x2D]
    lperiod = (writes[0x38] << 8) | writes[0x39]
    maxwd = (writes[0x35] << 16) | (writes[0x36] << 8) | writes[0x37]
    assert strpixel == geo.pixel_startx
    assert endpixel == geo.pixel_endx
    assert dpiset == geo.register_dpiset
    assert lperiod == geo.exposure_lperiod
    assert maxwd == maxwd_register_value(
        MODEL_8200I, line_bytes=geo.line_bytes, channels=geo.channels
    )
    assert writes[0x01] & 0x02  # SHDAREA
    assert not (writes[0x01] & 0x20)  # DVDSET off for host calib
    assert not (writes[0x01] & 0x01)  # SCAN off until begin


def test_gl843_configure_maxwd_half_line_bytes():
    writes: dict[int, int] = {}

    class _Proto:
        def write_register(self, addr: int, value: int) -> None:
            writes[int(addr)] = int(value) & 0xFF

        def write_ahb(self, *_a, **_k) -> None:
            return None

    asic = MagicMock()
    asic.protocol = _Proto()
    asic._reg_cache = dict(MODEL_7200I.boot_register_map())
    asic._scan_method = "transparency"
    asic.set_frontend_init = MagicMock()
    asic.apply_frontend_for_scan = MagicMock()

    geo = compute_geometry(1800, model=MODEL_7200I)
    session = Gl843ScanSession(asic, MODEL_7200I)
    session._configure(geo)
    maxwd = (writes[0x35] << 16) | (writes[0x36] << 8) | writes[0x37]
    assert maxwd == geo.line_bytes >> 1


def test_gl842_configure_maxwd_line_bytes():
    writes: dict[int, int] = {}

    class _Proto:
        def write_register(self, addr: int, value: int) -> None:
            writes[int(addr)] = int(value) & 0xFF

        def write_ahb(self, *_a, **_k) -> None:
            return None

    asic = MagicMock()
    asic.protocol = _Proto()
    asic._reg_cache = dict(MODEL_7200.boot_register_map())
    asic._scan_method = "transparency"
    asic.set_frontend_init = MagicMock()
    asic.apply_frontend_for_scan = MagicMock()

    geo = compute_geometry(1800, model=MODEL_7200)
    session = Gl842ScanSession(asic, MODEL_7200)
    session._configure(geo)
    maxwd = (writes[0x35] << 16) | (writes[0x36] << 8) | writes[0x37]
    assert maxwd == geo.line_bytes
