# SPDX-License-Identifier: GPL-3.0-or-later
"""OpticFilm 7600i v1 scan session: replay the vendor job, then assemble square-pixel RGB16."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

import numpy as np

from pyopticfilm.device.model_7600i_v1 import MODEL_7600I_V1, shifts_for
from pyopticfilm.exceptions import ScanError
from pyopticfilm.image import ScanImage
from pyopticfilm.logging import get_logger
from pyopticfilm.scan.replay_gl843_v1 import ReplayHooks, check_cancel, prepare_profile, run_profile, widen_window

logger = get_logger(__name__)

#: Calibration frames vs the vendor reference: white level %, G/R and B/R balance %, white ripple %.
LAMP_LIMITS = {"level": 10.0, "balance": 5.0, "ripple": 0.6}
RETURN_HOME_TIMEOUT_S = 60.0


def frame_stats(data: bytes, pixels: int, lines: int) -> tuple[np.ndarray, float]:
    """Per-channel mean of the central 80 % of columns, and the worst line-to-line variation (%)."""
    a = np.frombuffer(data, "<u2").reshape(lines, pixels, 3)[:, pixels // 10 : pixels - pixels // 10]
    rows = a.mean(axis=1)
    mean = rows.mean(axis=0)
    return mean, float((rows.std(axis=0) / np.maximum(mean, 1e-9)).max() * 100) if lines > 1 else 0.0


def lamp_issues(mean: np.ndarray, ripple: float, ref: dict) -> list[str]:
    r = np.asarray(ref["mean"], float)
    level = (mean.sum() / r.sum() - 1) * 100
    gr = (mean[1] / mean[0]) / (r[1] / r[0]) * 100 - 100
    br = (mean[2] / mean[0]) / (r[2] / r[0]) * 100 - 100
    issues = []
    if abs(level) > LAMP_LIMITS["level"]:
        issues.append(f"level {level:+.1f} %")
    if max(abs(gr), abs(br)) > LAMP_LIMITS["balance"]:
        issues.append(f"G/R {gr:+.1f} %, B/R {br:+.1f} %")
    if ripple > LAMP_LIMITS["ripple"]:
        issues.append(f"ripple {ripple:.2f} %")
    return issues


def assemble(raw, *, pixels: int, lines: int, dpi: int, yres: int, shifts, stagger=(), offsets=None,
             mirror: bool = True) -> np.ndarray:
    """Interleaved RGB16 lines -> aligned square pixels: fractional R/G/B delays, column stagger,
    line pairs averaged, black offsets subtracted, mirrored."""
    src = np.frombuffer(raw, "<u2").reshape(lines, pixels, 3)
    k = max(1, round(yres / dpi))
    height = (lines - math.ceil(max(shifts) - 1e-6) - max(stagger, default=0)) // k
    if height <= 0:
        raise ScanError("frame too short to align")
    out = np.empty((height, pixels, 3), np.uint16)
    parities = [(slice(0, None, 2), stagger[0]), (slice(1, None, 2), stagger[1])] if stagger else [(slice(None), 0)]
    for c in range(3):
        i0 = math.floor(shifts[c] + 1e-6)
        fr = shifts[c] - i0 if shifts[c] - i0 >= 1e-3 else 0.0
        for cols, st in parities:
            for y0 in range(0, height, 512):
                rows = np.arange(y0, min(height, y0 + 512)) * k + i0 + st
                acc = np.zeros((len(rows), src[0, cols, c].shape[0]))
                for j in range(k):
                    acc += (1 - fr) / k * src[rows + j, cols, c]
                    if fr:
                        acc += fr / k * src[rows + j + 1, cols, c]
                acc -= offsets[c] if offsets else 0.0
                out[y0 : y0 + len(rows), cols, c] = np.clip(np.floor(acc + 0.5), 0, 65535)  # round half up
    return np.ascontiguousarray(out[:, ::-1] if mirror else out)


def crop(rgb: np.ndarray, area: tuple[float, float, float, float] | None) -> np.ndarray:
    if area is None:
        return rgb
    x1, y1, x2, y2 = area
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        raise ValueError("area must be (x1, y1, x2, y2) within 0..1")
    h, w = rgb.shape[:2]
    return np.ascontiguousarray(rgb[int(y1 * h) : max(int(y1 * h) + 1, round(y2 * h)),
                                    int(x1 * w) : max(int(x1 * w) + 1, round(x2 * w))])


class Gl843V1ScanSession:
    """Colour (1440 / 3600 / 7200 dpi), infrared and colour + infrared (3600 / 7200 dpi) scans."""

    def __init__(self, asic, model=MODEL_7600I_V1, calibrator=None) -> None:
        self.asic = asic
        self.model = model
        self.last_scan_info: dict = {}
        self.sleep: Callable[[float], None] = time.sleep
        self.now: Callable[[], float] = time.monotonic

    def run(
        self,
        *,
        resolution: int = 3600,
        mode: str = "color",
        area: tuple[float, float, float, float] | None = None,
        geometry: object | None = None,
        progress: Callable[[float], None] | None = None,
        cancel: threading.Event | None = None,
        apply_calib: bool = True,
        multi_exposure: bool = False,
        infrared: bool = False,
        align_passes: bool = True,
        single_pass_exposure: int | None = None,
        me_short_exposure: int | None = None,
        me_long_exposure: int | None = None,
        n_passes: int = 1,
        dummy_lines: str | None = None,
    ) -> ScanImage:
        if multi_exposure or n_passes > 1 or single_pass_exposure is not None:
            raise NotImplementedError(f"Multi-exposure, Multi-Pass and manual exposure: not for {self.model.model}")
        if geometry is not None or mode not in {"color", "infrared"}:
            raise ValueError("Unsupported geometry or mode (use area= to crop; mode color|infrared)")
        if resolution not in self.model.resolutions_dpi:
            raise ValueError(f"{self.model.model} scans at {', '.join(map(str, sorted(self.model.resolutions_dpi)))} dpi")
        if (mode == "infrared" or infrared) and resolution not in self.model.infrared_resolutions_dpi:
            dpis = ", ".join(map(str, sorted(self.model.infrared_resolutions_dpi)))
            raise ValueError(f"{self.model.model} scans infrared at {dpis} dpi")
        if not self.asic._initialized:
            self.asic.init()

        jobs = ["infrared"] if mode == "infrared" else ["color", "infrared"] if infrared else ["color"]
        self.last_scan_info = {}
        planes = {}
        for n, job in enumerate(jobs):
            def report(f: float, n: int = n) -> None:
                if progress is not None:
                    progress((n + f) / len(jobs))

            planes[job] = self._scan_job(job, resolution, report, cancel, apply_calib, dummy_lines)

        device = f"{self.model.vendor} {self.model.model}"
        ir = planes["infrared"][:, :, 0] if "infrared" in planes else None  # read by the red row
        rgb = planes[jobs[0]]
        if mode == "color" and ir is not None:
            h = min(rgb.shape[0], ir.shape[0])
            rgb, ir = rgb[:h], ir[:h]
            if align_passes:
                from pyopticfilm.pass_align import align_ir_to_rgb

                ir = align_ir_to_rgb(rgb, np.ascontiguousarray(ir))
        return ScanImage(rgb=crop(rgb, area), dpi=resolution, device_model=device,
                         ir=None if ir is None else crop(np.ascontiguousarray(ir), area))

    def _scan_job(self, job, resolution, report, cancel, apply_calib, dummy_lines) -> np.ndarray:
        asic = self.asic
        profile = self.model.replay_profile(job, resolution)
        if self.model.window_start is not None:
            profile = widen_window(profile, self.model.window_start)
        profile = prepare_profile(profile, dummy_lines=dummy_lines or self.model.dummy_lines,
                                  single_sample=self.model.single_sample)
        if not asic.position_known or not asic.is_at_home():
            asic.home()
        asic.position_known = False
        if asic.lamp_on_at is None or not asic.usb.shadow.get(0x03, 0) & 0x10:
            asic.white_led(True)
        left = self.model.lamp_warmup_s - (self.now() - asic.lamp_on_at)
        if left > 0:
            self.sleep(left)

        main = profile["mainFrame"]
        lamp = profile["lamp"]
        roles = {lamp[k]["frame"]: k for k in ("line", "shading", "dark")}
        info: dict = {"issues": []}

        def frame_done(i: int, data: bytes) -> None:
            kind = roles[i]
            mean, ripple = frame_stats(data, profile["frames"][i]["pixels"], profile["frames"][i]["lines"])
            if kind == "dark":
                info["dark_delta"] = [round(float(m - r), 1) for m, r in zip(mean, lamp["dark"]["mean"], strict=True)]
                return
            for issue in lamp_issues(mean, ripple, lamp[kind]):
                info["issues"].append(f"{kind}: {issue}")
                logger.warning("%s calibration %s from the vendor reference: %s", job, kind, issue)

        hooks = ReplayHooks(check=check_cancel(cancel), sleep=self.sleep, now=self.now, frame_done=frame_done,
                            progress=lambda i, got, total: report(got / total) if i == main else None)
        try:
            result = run_profile(profile, asic.usb, hooks, calibrate=apply_calib)
        except BaseException:
            asic.forget_motor_tables()
            for cleanup in (asic.stop_motor, asic.protocol.abort_bulk_stream):  # stop, then drain bulk IN
                try:
                    cleanup()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("cleanup after failed scan: %s", exc)
            raise
        asic.forget_motor_tables()
        if job == "infrared":
            asic.usb.write_register(0xA8, 0x20)  # the job ends with the infrared LED on
        check = check_cancel(cancel)
        deadline = self.now() + RETURN_HOME_TIMEOUT_S
        while not asic.is_at_home():  # the chip returns the carriage after the scan
            check()
            if self.now() > deadline:
                raise ScanError("carriage did not return home")
            self.sleep(0.1)
        asic.position_known = True
        asic.lamp_on_at = (asic.lamp_on_at or self.now()) if asic.usb.shadow.get(0x03, 0) & 0x10 else None

        info["calibration"] = "live" if apply_calib else "recorded"
        info.update(black_offsets=None, positioning_stop_ms=result.positioning_stop_ms)
        self.last_scan_info[job] = info
        yres = profile["yres"]
        frame = profile["frames"][main]
        return assemble(result.main, pixels=frame["pixels"], lines=frame["lines"], dpi=resolution, yres=yres,
                        shifts=shifts_for(yres), mirror=self.model.mirror_x,
                        stagger=tuple(round(v * yres / 7200) for v in self.model.stagger_y_by_dpi[resolution]))
