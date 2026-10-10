# SPDX-License-Identifier: GPL-3.0-or-later
"""OpticFilm 7600i v1 (GL843): every job must replay exactly SilverFast's captured traffic."""

from __future__ import annotations

import base64
import math

import numpy as np
import pytest

from pyopticfilm.asic.gl843_v1 import Gl843V1, Gl843V1Usb
from pyopticfilm.device.model_7600i_v1 import MODEL_7600I_V1, Model7600iV1
from pyopticfilm.device.select import create_asic, model_for_device, model_is_scan_ready
from pyopticfilm.scan.replay_gl843_v1 import (
    ReplayHooks,
    collapse_recorded_waits,
    derive_infrared,
    prepare_profile,
    run_profile,
    shading_table,
    validate_profile,
    white_shading,
    widen_window,
    widened_shading,
)
from pyopticfilm.scan.session import create_session
from pyopticfilm.scan.session_7600i_v1 import Gl843V1ScanSession, assemble, crop
from pyopticfilm.usb.device import PID_OPTICFILM_7600I
from pyopticfilm.usb.fake_gl843_v1 import CapturePlaybackTransport, SimulatedGl843V1Transport
from pyopticfilm.usb.protocol import GenesysUsbProtocol

JOBS = [("color", 1440), ("color", 3600), ("color", 7200), ("infrared", 3600)]


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def _main_start_write(profile: dict) -> list[int]:
    ops = profile["ops"]
    first = next(i for i, o in enumerate(ops) if o["kind"] == "read" and o["frame"] == profile["mainFrame"])
    for i in range(first - 1, -1, -1):
        o = ops[i]
        if o["kind"] == "control" and o["rt"] == 0x40 and o["value"] == 0x83 and len(o["data"]) > 1:
            d = o["data"]
            if any(d[j] == 0x0F and d[j + 1] == 1 for j in range(0, len(d), 2)):
                return d
    raise AssertionError("no start write")


# --------------------------------------------------------------------------- model / selection


def test_bcd_0400_selects_capture_derived_gl843_model():
    model = model_for_device(PID_OPTICFILM_7600I, 0x0400)
    assert model is MODEL_7600I_V1
    assert isinstance(model, Model7600iV1)
    assert model.asic == "GL843"
    assert model_is_scan_ready(model) is True
    asic = create_asic(GenesysUsbProtocol(SimulatedGl843V1Transport()), model)
    assert isinstance(asic, Gl843V1)
    assert isinstance(create_session(asic, model), Gl843V1ScanSession)


def test_data_file_has_every_job_with_provenance():
    data = MODEL_7600I_V1.replay_data()
    assert data["usb"] == {"vendor_id": 0x07B3, "product_id": 0x0C3B, "bcd_device": 0x0400}
    assert set(data["profiles"]) == {"color_1440", "color_3600", "color_7200", "infrared_3600"}
    for key, cap in data["provenance"]["captures"].items():
        assert len(cap["sha256"]) == 64, key
    for mode, dpi in JOBS:
        validate_profile(MODEL_7600I_V1.replay_profile(mode, dpi))


def test_unknown_job_is_refused():
    with pytest.raises(ValueError, match="no infrared sequence at 1440"):
        MODEL_7600I_V1.replay_profile("infrared", 1440)


# --------------------------------------------------------------------------- infrared 7200 (derived)


def _lamp_sequence(profile: dict) -> list[str]:
    out = []
    for o in profile["ops"]:
        if o["kind"] == "read" and (not out or out[-1] != f"read {o['frame']}"):
            out.append(f"read {o['frame']}")
        elif o["kind"] == "control" and o["rt"] == 0x40 and o["value"] == 0x83 and len(o["data"]) > 1:
            out += [f"{r:02x}={v:02x}" for r, v in zip(o["data"][::2], o["data"][1::2]) if r in (0x03, 0xA8)]
    return out


def test_infrared_derivation_reproduces_the_captured_3600_job():
    profiles = MODEL_7600I_V1.replay_data()["profiles"]
    derived = derive_infrared(profiles["color_3600"], profiles["color_3600"], profiles["infrared_3600"])

    def kept(ops):  # without polls, delays and shading data
        return [(o["kind"], o.get("data"), o.get("frame")) for o in ops if o["kind"] in ("control", "read")
                and not (o["kind"] == "control" and (o["rt"] == 0xC0 or o["data"] == [0x41]))]

    assert kept(derived["ops"]) == kept(profiles["infrared_3600"]["ops"])
    ir7200 = MODEL_7600I_V1.replay_profile("infrared", 7200)
    assert _lamp_sequence(ir7200) == _lamp_sequence(profiles["infrared_3600"])
    validate_profile(ir7200)


def test_infrared_shading_matches_silverfast():
    # unity-gain white of 59000 / 61000 -> gain 0x13000 * 0x2000 / white
    pixels = 43
    white = np.full((4, pixels, 3), 59000, "<u2")
    white[:, 42] = 61000
    words = np.frombuffer(white_shading(white.tobytes(), pixels), "<u2").reshape(-1, 256)
    assert words.shape == (2, 256) and not words[:, 252:].any()
    assert list(words[0, :6]) == [0, round(0x13000 * 0x2000 / 59000)] * 3
    assert list(words[1, :6]) == [0, round(0x13000 * 0x2000 / 61000)] * 3
    assert list(shading_table(np.full((1, 3), 0x2000), (962, 1054, 1362))[:12]) == list(
        np.array([962, 0x2000, 1054, 0x2000, 1362, 0x2000], "<u2").tobytes())


# --------------------------------------------------------------------------- vendor traffic


@pytest.mark.parametrize(("mode", "dpi"), JOBS)
def test_replay_is_exactly_the_vendor_traffic(mode, dpi):
    profile = MODEL_7600I_V1.replay_profile(mode, dpi)
    ops = collapse_recorded_waits(profile["ops"])
    transport = CapturePlaybackTransport(ops)
    clock = Clock()
    result = run_profile(profile, Gl843V1Usb(transport), ReplayHooks(sleep=clock.sleep, now=clock.now))
    assert transport.finished
    main = profile["frames"][profile["mainFrame"]]
    assert len(result.main) == main["bytes"] == main["pixels"] * main["lines"] * 6
    stop = next(o["ms"] for o in profile["ops"] if o.get("timedStop"))
    assert result.positioning_stop_ms == pytest.approx(stop, abs=1)


def test_boot_program_is_the_start_of_the_vendor_job():
    boot = [g for g in MODEL_7600I_V1.replay_data()["boot"] if g[0] != "8c"]
    profile = MODEL_7600I_V1.replay_profile("color", 3600)
    writes = [o["data"] for o in profile["ops"]
              if o["kind"] == "control" and o["rt"] == 0x40 and o["value"] == 0x83 and len(o["data"]) > 1]
    assert writes[: len(boot)] == boot


def test_only_the_infrared_job_has_a_stale_wait_to_collapse():
    for mode, dpi in JOBS:
        ops = MODEL_7600I_V1.replay_profile(mode, dpi)["ops"]
        collapsed = collapse_recorded_waits(ops)
        if mode == "infrared":
            waits = [o for o in collapsed if o.get("waitMotorIdle")]
            assert len(waits) == 1 and waits[0]["waitMotorIdle"] > 100
            assert waits[0]["expected"][0] & 0x01 == 0
        else:
            assert collapsed is ops


# --------------------------------------------------------------------------- dummy lines / exposure


@pytest.mark.parametrize(
    ("dpi", "setting", "line_sel", "cruise"),
    [(3600, "none", 0, 7000), (3600, "fewer", 0, 7000), (7200, "fewer", 1, 28000), (7200, "none", 0, 14000)],
)
def test_dummy_lines_scale_the_motor_cruise(dpi, setting, line_sel, cruise):
    recorded = MODEL_7600I_V1.replay_profile("color", dpi)
    p = prepare_profile(recorded, dummy_lines=setting)
    rec_sel = recorded["scan"]["lineSel"]
    assert p["scan"]["lineSel"] == line_sel
    assert p["motorCruise"]["used"] == cruise
    # C' = C * (L'+1) / (L+1)
    assert cruise == p["motorCruise"]["recorded"] * (line_sel + 1) // (rec_sel + 1)
    start = _main_start_write(p)
    i = start.index(0x1E)
    assert start[i + 1] & 0x0F == line_sel and start.index(0x0F) > i
    assert p["scan"]["lPeriod"] == recorded["scan"]["lPeriod"]  # exposure unchanged
    assert prepare_profile(recorded) is recorded


def test_single_sample_halves_the_lines():
    recorded = MODEL_7600I_V1.replay_profile("color", 7200)
    p = prepare_profile(recorded, dummy_lines="none", single_sample=True)
    validate_profile(p)
    f = p["frames"][p["mainFrame"]]
    assert (f["lines"], f["bytes"], p["yres"]) == (7061, 10248 * 7061 * 6, 7200)
    assert p["motorCruise"] == {"recorded": 42000, "used": 7000}  # 2 steps per line
    regs = dict(zip(_main_start_write(p)[::2], _main_start_write(p)[1::2], strict=True))
    assert regs[0x25] << 16 | regs[0x26] << 8 | regs[0x27] == 2 * 7061  # LINCNT
    assert regs[0x35] << 16 | regs[0x36] << 8 | regs[0x37] == 10248 * 3  # MAXWD: two lines
    assert (regs[0x20], regs[0x02] & 0x40) == (0x10, 0x40)  # BUFSEL, no backtracking
    assert p["scan"]["backtracking"] is False


def test_exposure_x3_reproduces_silverfast_multi_exposure_pass():
    p = prepare_profile(MODEL_7600I_V1.replay_profile("color", 3600), exposure_multiplier=3)
    start = _main_start_write(p)
    regs = {start[j]: start[j + 1] for j in range(0, len(start), 2)}
    assert (regs[0x38] << 8 | regs[0x39]) == 42000  # LPERIOD x3
    assert regs[0x1E] & 0x0F == 0  # no dummy lines
    assert regs[0x20] == 0x08  # BUFSEL
    assert p["motorCruise"] == {"recorded": 14000, "used": 21000}


@pytest.mark.parametrize(("mode", "dpi", "start", "pixels"), [
    ("color", 1440, 83, 2076), ("color", 3600, 82, 5188), ("color", 7200, 80, 10378),
    ("infrared", 3600, 82, 5188), ("infrared", 7200, 80, 10378),
])
def test_full_window_starts_at_the_first_lit_pixel(mode, dpi, start, pixels):
    p = widen_window(MODEL_7600I_V1.replay_profile(mode, dpi), 80)
    validate_profile(p)
    for i in (5, 6, 7):  # dark, white, image
        f = p["frames"][i]
        assert (f["regs"]["48"] << 8 | f["regs"]["49"], f["pixels"]) == (start, pixels)
    setup, payload = None, 0
    for o in p["ops"] + [{"kind": "control", "rt": 0x40, "value": 0x82, "data": [0] * 8}]:
        if o["kind"] == "control" and o["rt"] == 0x40 and o["value"] == 0x82:
            if setup is not None:  # a read gets its exact length, an upload its table before padding
                n = int.from_bytes(bytes(setup[4:8]), "little")
                assert n == payload if setup[0] == 0 else n <= payload < n + 512
            setup, payload = o["data"], 0
        elif o["kind"] in ("read", "write"):
            payload += o.get("length") or len(base64.b64decode(o["data"]))


def test_widened_shading_brings_added_pixels_to_the_recorded_level():
    p = widen_window(MODEL_7600I_V1.replay_profile("color", 3600), 80)
    w = next(o["shading"] for o in p["ops"] if "shading" in o)
    pixels = p["frames"][w["frame"]]["pixels"]
    white = np.full((2, pixels, 3), 50000, "<u2")
    white[:, : w["widen"]["extra"]] = 25000  # added pixels half as bright
    table = widened_shading(base64.b64decode(w["widen"]["table"]), w["widen"]["extra"], white.tobytes(), pixels)
    words = np.frombuffer(table, "<u2").reshape(-1, 256)[:, :252].reshape(-1, 6)
    assert np.allclose(words[0, 1::2], 2 * np.median(words[64:80, 1::2], axis=0), rtol=1e-3)


# --------------------------------------------------------------------------- register protocol


def test_register_writes_are_acknowledged_and_reads_addressed():
    sim = SimulatedGl843V1Transport()
    usb = Gl843V1Usb(sim)
    usb.write_register(0x5B, 0x58)
    usb.write_registers([(0x01, 0x22), (0x02, 0x38)])
    status = usb.status()
    ops = [(t.operation, t.request, t.value, t.index, t.data or t.length) for t in sim.transactions]
    assert ops[0] == ("control_write", 0x04, 0x83, 0, bytes([0x5B, 0x58]))
    assert ops[1] == ("control_read", 0x0C, 0x8E, 0x20, 1)  # write acknowledge
    assert ops[2] == ("control_write", 0x04, 0x83, 0, bytes([0x01, 0x22, 0x02, 0x38]))
    assert ops[-3] == ("control_write", 0x0C, 0x83, 0, bytes([0x41]))  # set address
    assert ops[-1] == ("control_read", 0x0C, 0x84, 0, 1)
    assert status & 0x40 and not status & 0x08  # buffer empty, carriage away from home
    assert usb.shadow[0x02] == 0x38


def test_homing_from_mid_travel_uses_vendor_primitives():
    sim = SimulatedGl843V1Transport(position=6000)
    asic = Gl843V1(GenesysUsbProtocol(sim), MODEL_7600I_V1)
    asic.motion().sleep = lambda s: None
    asic.home()
    assert sim.position <= 0 and asic.position_known
    writes = [t.data for t in sim.transactions if t.operation == "control_write" and t.value == 0x83]
    assert bytes([0x5B, 0x58]) in writes  # table into slot 3 (RAM 0x58000)
    bulk = [t.data for t in sim.transactions if t.operation == "bulk_write"]
    assert bulk and all(len(b) % 512 == 0 for b in bulk)


# --------------------------------------------------------------------------- image assembly


def _reference(raw, pixels, lines, dpi, yres, shifts, stagger, offsets, mirror):
    src = np.frombuffer(raw, "<u2").reshape(lines, pixels, 3).astype(np.float64)
    k = round(yres / dpi)
    height = (lines - math.ceil(max(shifts) - 1e-6) - (max(stagger) if stagger else 0)) // k
    out = np.zeros((height, pixels, 3), np.uint16)
    for c in range(3):
        i0 = math.floor(shifts[c] + 1e-6)
        fr = shifts[c] - i0
        fr = 0.0 if fr < 1e-3 else fr
        for y in range(height):
            for x in range(pixels):
                st = stagger[x & 1] if stagger else 0
                acc = 0.0
                for j in range(k):
                    r = y * k + j + i0 + st
                    acc += (1 - fr) / k * src[r, x, c]  # same summation order as the JS original
                    if fr:
                        acc += fr / k * src[r + 1, x, c]
                v = math.floor(acc - offsets[c] + 0.5)
                out[y, pixels - 1 - x if mirror else x, c] = min(65535, max(0, v))
    return out


@pytest.mark.parametrize(
    ("dpi", "yres", "stagger", "mirror"),
    [(3600, 7200, (), True), (7200, 14400, (8, 0), True), (1440, 2880, (), False)],
)
def test_assembly_matches_reference(dpi, yres, stagger, mirror):
    from pyopticfilm.device.model_7600i_v1 import CALIBRATED_SHIFTS

    rng = np.random.default_rng(dpi)
    pixels, lines = 11, 140 if yres == 14400 else 70
    raw = rng.integers(0, 65536, size=(lines, pixels, 3), dtype=np.uint16).astype("<u2").tobytes()
    shifts = CALIBRATED_SHIFTS[yres]
    offsets = (12.5, -3.0, 40.2)
    got = assemble(raw, pixels=pixels, lines=lines, dpi=dpi, yres=yres, shifts=shifts, stagger=stagger,
                   offsets=offsets, mirror=mirror)
    want = _reference(raw, pixels, lines, dpi, yres, shifts, stagger, offsets, mirror)
    np.testing.assert_array_equal(got, want)


def test_assembly_aligns_channels_and_stagger():
    # Every raw sample holds its own row number; aligned output row y must read row
    # 2y + shift (+8 on even columns) in each channel.
    pixels, lines = 4, 200
    rows = np.arange(lines, dtype=np.uint16)[:, None, None]
    raw = np.broadcast_to(rows, (lines, pixels, 3)).astype("<u2").tobytes()
    out = assemble(raw, pixels=pixels, lines=lines, dpi=7200, yres=14400, shifts=(0.0, 48.0, 96.0),
                   stagger=(8, 0), offsets=None, mirror=False)
    assert out.shape == ((lines - 96 - 8) // 2, pixels, 3)
    y = 10
    # averaged pair (2y, 2y+1) -> 2y + 0.5, rounded half up
    assert out[y, 1, 0] == 2 * y + 1 and out[y, 1, 1] == 2 * y + 48 + 1 and out[y, 1, 2] == 2 * y + 96 + 1
    assert out[y, 0, 0] == 2 * y + 8 + 1  # even column: 8 raw lines later


def test_default_frame_crop_is_the_whole_window():
    from pyopticfilm.scan.bringup import default_frame_crop_norm

    assert default_frame_crop_norm(MODEL_7600I_V1) == (0.0, 0.0, 1.0, 1.0)  # NegPy calls it for every model


def test_crop_uses_normalised_area():
    rgb = np.zeros((100, 200, 3), np.uint16)
    assert crop(rgb, (0.25, 0.1, 0.75, 0.6)).shape == (50, 100, 3)
    with pytest.raises(ValueError):
        crop(rgb, (0.5, 0, 0.5, 1))


# --------------------------------------------------------------------------- end to end (simulated)


def test_scanner_end_to_end_on_simulated_scanner(monkeypatch, tmp_path):
    import time

    from pyopticfilm import Scanner

    monkeypatch.setattr(time, "sleep", lambda s: None)
    sim = SimulatedGl843V1Transport(position=3000)
    with Scanner.open_fake(MODEL_7600I_V1, sim, calib_cache=tmp_path / "calib.json") as scanner:
        scanner.warmup()
        assert scanner.status().is_at_home
        image = scanner.scan(resolution=1440)
    assert image.rgb.shape == (1402, 2076, 3)  # sensor pixels 83-10463 and image.rgb.dtype == np.uint16
    assert image.dpi == 1440 and image.ir is None
    # Live shading owns black correction; do not subtract a global offset again.
    assert int(image.rgb[700, 1000, 1]) == sim.lit_rgb[1]


def test_unsupported_requests_are_refused(tmp_path):
    from pyopticfilm import Scanner

    with Scanner.open_fake(MODEL_7600I_V1, SimulatedGl843V1Transport(), calib_cache=tmp_path / "c.json") as s:
        with pytest.raises(ValueError, match="1440, 3600, 7200"):
            s.scan(resolution=1800)
        with pytest.raises(ValueError, match="infrared at 3600, 7200 dpi"):
            s.scan(resolution=1440, mode="infrared")
        with pytest.raises(NotImplementedError):
            s.scan(resolution=3600, multi_exposure=True)


def test_profile_tables_round_trip():
    # motor tables are little-endian uint16 in base64; the decoder must not drop a trailing entry
    profile = MODEL_7600I_V1.replay_profile("color", 3600)
    blob = next(o["data"] for o in profile["ops"] if o["kind"] == "write")
    assert len(base64.b64decode(blob)) % 2 == 0


# --------------------------------------------------------------------------- failure handling


def test_too_slow_transfer_stops_the_scan():
    from pyopticfilm.exceptions import ScanError

    profile = MODEL_7600I_V1.replay_profile("color", 3600)
    clock = Clock()

    class Slow(CapturePlaybackTransport):
        def bulk_read(self, size, *, timeout_ms=None):
            clock.t += 1.0  # 256 KB per second: far below the 2.7 MB/s this job needs
            return super().bulk_read(size, timeout_ms=timeout_ms)

    transport = Slow(collapse_recorded_waits(profile["ops"]))
    with pytest.raises(ScanError, match="MB/s"):
        run_profile(profile, Gl843V1Usb(transport), ReplayHooks(sleep=clock.sleep, now=clock.now))


def test_stalled_register_write_is_sent_again():
    from pyopticfilm.exceptions import UsbError

    class Stall(SimulatedGl843V1Transport):
        stalls = 2

        def control_msg(self, request_type, request, value, index, data_or_length, *, timeout_ms=None):
            if request_type == 0x40 and self.stalls:
                self.stalls -= 1
                raise UsbError("pipe stalled")
            return super().control_msg(request_type, request, value, index, data_or_length)

    sim = Stall()
    Gl843V1Usb(sim).write_register(0x03, 0x9F)
    assert sim.regs[0x03] == 0x9F and sim.stalls == 0


def test_cancelled_scan_stops_motor_and_drains_bulk(monkeypatch, tmp_path):
    import threading
    import time

    from pyopticfilm import Scanner
    from pyopticfilm.exceptions import ScanCancelled

    monkeypatch.setattr(time, "sleep", lambda s: None)
    sim = SimulatedGl843V1Transport(position=0)
    cancel = threading.Event()
    with Scanner.open_fake(MODEL_7600I_V1, sim, calib_cache=tmp_path / "c.json") as scanner:
        scanner.warmup()
        with pytest.raises(ScanCancelled):
            scanner.scan(resolution=1440, cancel=cancel, progress=lambda f: cancel.set() if f > 0.2 else None)
        writes = [t.data for t in sim.transactions if t.operation == "control_write" and t.value == 0x83]
        assert bytes([0x02, 0x08]) in writes[-6:]  # the vendor's stop
        assert sim.aborts == 1
        assert scanner.asic.position_known is False


def test_advanced_registers_use_gl843_protocol_and_calibrate_is_refused(tmp_path):
    from pyopticfilm import Scanner

    sim = SimulatedGl843V1Transport()
    with Scanner.open_fake(MODEL_7600I_V1, sim, calib_cache=tmp_path / "c.json") as scanner:
        scanner.advanced.write_register(0x6D, 0x12)
        assert scanner.advanced.read_register(0x41) & 0x80
        assert sim.regs[0x6D] == 0x12
        assert any(t.operation == "control_read" and t.value == 0x84 for t in sim.transactions)
        with pytest.raises(NotImplementedError):
            scanner.calibrate(resolution=3600)


def test_infrared_led_is_switched_off_after_the_ir_job(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda s: None)
    sim = SimulatedGl843V1Transport(position=0)
    asic = create_asic(GenesysUsbProtocol(sim), MODEL_7600I_V1)
    asic.init()
    session = create_session(asic, MODEL_7600I_V1)
    image = session.run(resolution=3600, mode="infrared")
    assert sim.regs[0xA8] == 0x20
    assert image.ir is not None and image.ir.shape == image.rgb.shape[:2]
    assert "issues" in session.last_scan_info["infrared"]
