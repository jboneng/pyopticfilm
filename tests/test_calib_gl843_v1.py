"""Independent integer oracles and live USB substitution, including split reads/uploads."""
import base64

import numpy as np
import pytest

from pyopticfilm.asic.gl843_v1 import Gl843V1Usb
from pyopticfilm.exceptions import ScanError
from pyopticfilm.scan.calib_gl843_v1 import Calibration
from pyopticfilm.scan.replay_gl843_v1 import ReplayHooks, run_profile


def fixture(infrared=False):
    probes = [np.tile(v, (512, 1)).astype("<u2") for v in
              ([2200, 3200, 4200], [3470, 5740, 8010], [22000, 16000, 12000], [34700, 28700, 24700])]
    lamp = np.tile([10000, 40000, 30000], (10, 1)).astype("<u2")
    lamp[0, 0], lamp[8:, 0] = 65535, 50000  # isolated peak must lose to the final short block
    dark = np.full((128, 43, 3), 1000, "<u2")
    dark[:, 42, 1] = 2000
    white = np.full_like(dark, 50000)
    white[:8], white[-8:] = 0, 65535
    data = [x.tobytes() for x in [probes[0], probes[1], lamp, probes[2], probes[3], dark, white,
                                  np.zeros((1, 43, 3), "<u2")]]
    frames = [{"pixels": len(b) // (6 * (128 if i in (5, 6) else 1)), "lines": 128 if i in (5, 6) else 1,
               "bytes": len(b), "regs": {"168": 4 if infrared else 0}} for i, b in enumerate(data)]
    p = {"dpi": 7200, "mainFrame": 7, "frames": frames, "scan": {"bytesPerSecond": 1},
         "lamp": {"line": {"frame": 2}, "dark": {"frame": 5}, "shading": {"frame": 6}}, "ops": []}
    return p, data


def afe(addr, value=0):
    return {"kind": "control", "rt": 0x40, "request": 12, "value": 0x83, "index": 0,
            "data": [0x51, addr, 0x3A, value >> 8, 0x3B, value & 255]}


@pytest.mark.parametrize("infrared", [False, True])
def test_live_calibration_substitutes_usb_values(infrared):
    p, data = fixture(infrared)
    for i in range(8):
        if i in (2, 5, 6):
            for _ in range(2 if i == 5 else 1):
                p["ops"] += [afe(a) for a in (5, 6, 7)]
        if i in (3, 7):
            p["ops"] += [afe(a) for a in (2, 3, 4)]
        n = len(data[i])
        p["ops"] += [{"kind": "read", "frame": i, "length": n // 2},
                     {"kind": "read", "frame": i, "length": n - n // 2}]
        if i in (5, 6):
            p["ops"].append({"kind": "control", "rt": 0x40, "request": 12, "value": 0x83,
                             "index": 0, "data": [0x5B, 16, 0x5C, 0]})
            p["ops"] += [{"kind": "write", "data": base64.b64encode(bytes(512)).decode()}] * 2

    class USB:
        def __init__(self):
            self.frame = 0
            self.at = 0
            self.writes = []
            self.controls = []

        def control_msg(self, rt, req, value, index, payload):
            self.controls.append(list(payload))

        def bulk_read(self, n):
            b = data[self.frame][self.at:self.at + n]
            self.at += len(b)
            if self.at == len(data[self.frame]):
                self.frame, self.at = self.frame + 1, 0
            return b

        def bulk_write(self, b):
            self.writes.append(bytes(b))
            return len(b)

    usb = USB()
    result = run_profile(p, Gl843V1Usb(usb), ReplayHooks(), calibrate=True)
    assert result.main == data[7]
    values = [(d[1], d[3] * 256 + d[5]) for d in usb.controls if d[0] == 0x51]
    assert values[:3] == [(5, 348), (6, 288), (7, 268)]
    assert values[3:6] == [(2, 17), (3, 29), (4, 40)]
    assert values[-3:] == [(2, 17), (3, 29 if infrared else 30), (4, 40 if infrared else 42)]
    assert [v for _, v in values[9:12]] == ([348, 288, 8] if infrared else [336, 276, 20])
    assert [v for _, v in values[12:15]] == ([328, 268, 28] if infrared else [334, 274, 22])
    tables = [b"".join(usb.writes[i:i + 2]) for i in (0, 2)]
    words = [np.frombuffer(b, "<u2").reshape(2, 256) for b in tables]
    assert words[0][0, :6].tolist() == [1000, 8192, 1045, 8192, 1000, 8192]
    expected = [1000, 12750, 1045, 12750, 1000, 12750] if infrared else [1000, 13005, 1045, 12962, 1000, 13555]
    assert words[1][0, :6].tolist() == expected
    assert words[1][1, :6:2].tolist() == [1000, 2000, 1000]
    assert not words[1][:, 252:].any()


def test_bad_offset_probe_fails_before_lamp_selection():
    p, data = fixture()
    c = Calibration(p)
    c.frame_done(0, data[0])
    with pytest.raises(ScanError, match="nonpositive"):
        c.frame_done(1, data[0])


def test_zero_white_reference_is_rejected():
    p, data = fixture()
    c = Calibration(p)
    for i in range(6):
        c.frame_done(i, data[i])
    with pytest.raises(ScanError, match="zero white"):
        c.frame_done(6, bytes(len(data[6])))
