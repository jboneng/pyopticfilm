# SPDX-License-Identifier: GPL-3.0-or-later
"""Live GL843/AD9826 calibration recovered from eight colour/IR USB jobs."""

import numpy as np

from pyopticfilm.exceptions import ScanError

UNITY = 8192
WHITE_RGB = np.array([79380, 79119, 82739], dtype=np.int64)


def reference(data: bytes, pixels: int) -> np.ndarray:
    """128 lines: discard eight samples at each end, then truncate the mean."""
    a = np.frombuffer(data, "<u2").reshape(128, pixels, 3)
    return np.sort(a, axis=0)[8:120].astype(np.int64).sum(axis=0) // 112


def dark_reference(data: bytes, pixels: int, stride: int) -> np.ndarray:
    raw = reference(data, pixels)
    smooth = np.empty_like(raw)
    for parity in range(stride):
        a = raw[parity::stride]
        sums = np.vstack((np.zeros((1, 3), dtype=np.int64), a.cumsum(axis=0)))
        start = np.arange(len(a))
        end = np.minimum(start + 100, len(a))
        smooth[parity::stride] = (sums[end] - sums[start]) // (end - start)[:, None]
    # Inclusive 64 fits every capture; the driver's strict configurable gate
    # is only constrained to 64..68 (this setting corresponds to 65).
    return np.where(np.abs(raw - smooth) > 64, raw, smooth)


def shading_table(dark: np.ndarray, gain: np.ndarray) -> bytes:
    n = (len(dark) + 41) // 42
    words = np.zeros((n * 42, 6), "<u2")
    words[:len(dark), ::2], words[:len(dark), 1::2] = dark, gain
    blocks = np.zeros((n, 256), "<u2")
    blocks[:, :252] = words.reshape(n, 252)
    return blocks.tobytes()


def initial_gain(data: bytes) -> np.ndarray:
    a = np.frombuffer(data, "<u2").reshape(-1, 3).astype(np.int64)
    n = len(a) // 4 * 4
    blocks = a[:n].reshape(-1, 4, 3).sum(axis=1) // 4
    if n < len(a):
        blocks = np.vstack((blocks, a[n:].sum(axis=0) // (len(a) - n)))
    peak = blocks.max(axis=0)
    if np.any(peak == 0):
        raise ScanError("zero lamp response during AFE calibration")
    return np.clip(378 * (65535 - peak) // (5 * 65535), 0, 63)


class Calibration:
    """Replace values in the existing probe/AFE/shading sequence, without extra USB commands."""

    def __init__(self, profile: dict):
        lamp = profile["lamp"]
        self.first = lamp["line"]["frame"] - 2
        self.white = lamp["shading"]["frame"]
        self.dark_frame = lamp["dark"]["frame"]
        if (self.dark_frame, self.white) != (self.first + 5, self.first + 6):
            raise ScanError("unsupported GL843 calibration sequence")
        self.pixels = profile["frames"][self.white]["pixels"]
        self.infrared = bool(profile["frames"][self.white]["regs"]["168"] & 4)
        self.stride = 2 if profile["dpi"] == 7200 else 1
        self.phase = -1
        self.probes = {}
        self.offset_writes = [0, 0, 0]
        self.table = b""
        self.table_offset = 0

    def frame_done(self, i: int, data: bytes) -> None:
        self.phase = i - self.first
        if self.phase in (0, 1, 3, 4):
            self.probes[self.phase] = np.frombuffer(data, "<u2").reshape(512, 3)[19:51].astype(np.int64).sum(0) // 32
        if self.phase in (1, 4):
            low = self.probes[self.phase - 1]
            span = self.probes[self.phase] - low
            if np.any(span <= 0):
                raise ScanError("nonpositive AFE offset response")
            zero = 128 - low * 127 // span
            if self.phase == 1:
                self.zero_low = zero
            else:
                self.zero_high = zero
                self.dark_offset = zero + (0 if self.infrared else 1280) * 127 // span
                self.white_offset = self.dark_offset + (2048 if self.infrared else 256) * 127 // span
        elif self.phase == 2:
            self.gain = initial_gain(data)
        elif i == self.dark_frame:
            self.dark = dark_reference(data, self.pixels, self.stride)
            self.table = shading_table(self.dark, np.full_like(self.dark, UNITY))
        elif i == self.white:
            white = reference(data, self.pixels)  # already dark-corrected by the chip
            if np.any(white == 0):
                raise ScanError("zero white reference during shading calibration")
            target = 77824 if self.infrared else WHITE_RGB
            self.table = shading_table(self.dark, np.minimum(target * UNITY // white, 65535))
        self.table_offset = 0

    def control(self, op: dict) -> dict:
        if op.get("rt") != 0x40 or op.get("value") != 0x83 or op.get("data", [])[:1] != [0x51]:
            return op
        d = dict(zip(op["data"][::2], op["data"][1::2], strict=True))
        addr, value = d[0x51], None
        if 2 <= addr <= 4 and self.phase >= 2:
            c = addr - 2
            value = min(63, int(self.gain[c]) + (c if self.phase >= 6 and not self.infrared else 0))
        elif 5 <= addr <= 7:
            c = addr - 5
            offset = None
            if self.phase == 1:
                offset = self.zero_low[c]
            elif self.phase == 4:
                offset = (self.zero_high if self.offset_writes[c] == 0 else self.dark_offset)[c]
                self.offset_writes[c] += 1
            elif self.phase >= 5:
                offset = self.white_offset[c]
            if offset is not None:
                offset = int(offset)
                if abs(offset) > 255:
                    raise ScanError("AFE offset outside nine-bit sign-magnitude range")
                value = 256 | -offset if offset < 0 else offset
        if value is None:
            return op
        d[0x3A], d[0x3B] = value >> 8, value & 255
        return {**op, "data": [v for pair in d.items() for v in pair]}

    def upload(self, size: int) -> bytes:
        out = self.table[self.table_offset:self.table_offset + size]
        if len(out) != size:
            raise ScanError("incomplete live shading upload")
        self.table_offset += size
        return out
