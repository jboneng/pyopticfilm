# SPDX-License-Identifier: GPL-3.0-or-later
"""Dark / white / shading calibration and on-disk cache."""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.device.protocol import AsicDriver, FilmModel
from pyopticfilm.exceptions import CalibrationError
from pyopticfilm.logging import get_logger
from pyopticfilm.scan.geometry import ScanGeometry, compute_calib_geometry, compute_geometry
from pyopticfilm.scan.pipeline import ImagePipeline

logger = get_logger(__name__)

CACHE_IDENT = "pyopticfilm"
CACHE_VERSION = 9
DARK_SETTLE_S = 0.2
WHITE_SETTLE_S = 0.5


def colour_shading_failure_message(reason: str) -> str:
    """User-facing CalibrationError text mapped from ASIC reject reason."""
    text = str(reason).strip() or "ASIC shading validation failed"
    lower = text.lower()
    if "white≈dark" in text or ("white" in lower and "dark" in lower and "≈" in text):
        return f"Colour ASIC shading failed ({text}). Lamp-on white did not rise above dark — check illumination and retry."
    if "dark mean" in lower or "lamp-off" in lower:
        return f"Colour ASIC shading failed ({text}). AFE black level or illumination failed — retry; power-cycle if it persists."
    if "film" in lower:
        return (
            f"Colour ASIC shading failed ({text}). Head is not on the clear home field — ensure the carriage can park at home, then retry."
        )
    if "home" in lower or "park" in lower:
        return f"Colour ASIC shading failed ({text}). Ensure the carriage can park at home, then retry the scan."
    return f"Colour ASIC shading failed ({text}). Retry the scan; power-cycle the scanner if it persists."


def default_cache_path() -> Path:
    return Path.home() / ".cache" / "pyopticfilm" / "calib_v2.json"


@dataclass
class CalibEntry:
    """One shading result keyed like SANE genesys cache.

    GL128 colour entries carry the packed AHB blob + AFE codes so a later
    process can re-upload without re-measuring strips (SilverFast order: shade
    at home, then feed to film).
    """

    method: str  # transparency | infrared
    resolution: int
    startx: int
    pixels: int
    dark: np.ndarray  # (pixels, 3) uint16
    white: np.ndarray  # (pixels, 3) uint16
    calibrated_at: float = 0.0
    #: True when this entry was produced by GL128 ASIC shading (skip host stretch).
    asic_shading: bool = False
    shading_blob: bytes | None = None
    afe_offsets: tuple[int, int, int] | None = None
    afe_gains: tuple[int, int, int] | None = None

    @property
    def has_asic_blob(self) -> bool:
        return bool(self.asic_shading and self.shading_blob and self.afe_offsets is not None and self.afe_gains is not None)

    def matches(
        self,
        *,
        method: str,
        resolution: int,
        startx: int,
        pixels: int,
    ) -> bool:
        return self.method == method and self.resolution == resolution and self.startx == startx and self.pixels == pixels

    def to_dict(self) -> dict:
        payload = {
            "method": self.method,
            "resolution": self.resolution,
            "startx": self.startx,
            "pixels": self.pixels,
            "calibrated_at": self.calibrated_at,
            "asic_shading": self.asic_shading,
            "dark": self.dark.astype(np.uint16).ravel().tolist(),
            "white": self.white.astype(np.uint16).ravel().tolist(),
        }
        if self.shading_blob is not None:
            payload["shading_blob_b64"] = base64.b64encode(self.shading_blob).decode("ascii")
        if self.afe_offsets is not None:
            payload["afe_offsets"] = [int(v) for v in self.afe_offsets]
        if self.afe_gains is not None:
            payload["afe_gains"] = [int(v) for v in self.afe_gains]
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> CalibEntry:
        pixels = int(data["pixels"])
        dark = np.asarray(data["dark"], dtype=np.uint16).reshape(pixels, 3)
        white = np.asarray(data["white"], dtype=np.uint16).reshape(pixels, 3)
        blob_b64 = data.get("shading_blob_b64")
        blob = base64.b64decode(blob_b64) if blob_b64 else None
        offs = data.get("afe_offsets")
        gains = data.get("afe_gains")
        return cls(
            method=str(data["method"]),
            resolution=int(data["resolution"]),
            startx=int(data["startx"]),
            pixels=pixels,
            dark=dark,
            white=white,
            calibrated_at=float(data.get("calibrated_at", 0.0)),
            asic_shading=bool(data.get("asic_shading", False)),
            shading_blob=blob,
            afe_offsets=tuple(int(v) for v in offs) if offs is not None else None,
            afe_gains=tuple(int(v) for v in gains) if gains is not None else None,
        )


class CalibCache:
    """Versioned JSON cache of shading entries."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_cache_path()
        self.entries: list[CalibEntry] = []

    def load(self) -> bool:
        if not self.path.exists():
            self.entries = []
            return False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable calib cache at %s: %s", self.path, exc)
            self.entries = []
            return False
        if raw.get("ident") != CACHE_IDENT or int(raw.get("version", 0)) != CACHE_VERSION:
            logger.warning("Ignoring incompatible calib cache at %s", self.path)
            self.entries = []
            return False
        self.entries = [CalibEntry.from_dict(e) for e in raw.get("entries", [])]
        logger.info("Loaded %d calib entr(y/ies) from %s", len(self.entries), self.path)
        return True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ident": CACHE_IDENT,
            "version": CACHE_VERSION,
            "entries": [e.to_dict() for e in self.entries],
        }
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp_path, self.path)
        logger.info("Wrote calib cache %s (%d entries)", self.path, len(self.entries))

    def find(
        self,
        *,
        method: str,
        resolution: int,
        startx: int,
        pixels: int,
    ) -> CalibEntry | None:
        for entry in self.entries:
            if entry.matches(method=method, resolution=resolution, startx=startx, pixels=pixels):
                return entry
        return None

    def upsert(self, entry: CalibEntry) -> None:
        self.entries = [
            e
            for e in self.entries
            if not e.matches(
                method=entry.method,
                resolution=entry.resolution,
                startx=entry.startx,
                pixels=entry.pixels,
            )
        ]
        self.entries.append(entry)

    def clear(self) -> None:
        self.entries = []
        if self.path.exists():
            self.path.unlink()
            logger.info("Cleared calibration cache %s", self.path)


class Calibrator:
    """Acquire dark/white averages and manage the on-disk cache."""

    def __init__(
        self,
        asic: AsicDriver | None = None,
        *,
        cache_path: Path | None = None,
        model: FilmModel = MODEL_8200I,
    ) -> None:
        self.asic = asic
        self.model = model
        self.cache = CalibCache(cache_path)
        self.cache.load()
        self.pipeline = ImagePipeline(model)
        self._active: CalibEntry | None = None
        #: GL128: after ASIC shading upload, skip host dark/white stretch on scan.
        self.prefer_asic_shading = False

    @property
    def cache_path(self) -> Path | None:
        return self.cache.path

    def should_apply_host_calib(self) -> bool:
        """False when the active entry is ASIC-shading (or prefer flag + ready)."""
        if self._active is not None and self._active.asic_shading:
            return False
        if not self.prefer_asic_shading:
            return True
        asic = self.asic
        return not bool(asic is not None and getattr(asic, "asic_shading_ready", False))

    def load(self) -> bool:
        return self.cache.load()

    def clear(self) -> None:
        self.cache.clear()
        self._active = None

    def find_for_scan(
        self,
        *,
        method: str,
        geometry: ScanGeometry,
    ) -> CalibEntry | None:
        entry = self.cache.find(
            method=method,
            resolution=geometry.resolution,
            startx=geometry.startx,
            pixels=geometry.pixels,
        )
        # Dummy v1 ASIC markers (no blob) are not usable — treat as miss.
        if entry is not None and entry.asic_shading and not entry.has_asic_blob:
            logger.info(
                "Ignoring ASIC calib marker without shading blob dpi=%d",
                geometry.resolution,
            )
            entry = None
        self._active = entry
        return entry

    @contextmanager
    def _stationary_motor(self) -> Iterator[None]:
        """Disarm GL128 motor moves for AFE/shading; restore previous arm state."""
        asic = self.asic
        if asic is None or not hasattr(asic, "_motor_moves_enabled"):
            yield
            return
        prev = bool(asic._motor_moves_enabled)
        asic._motor_moves_enabled = False
        try:
            yield
        finally:
            asic._motor_moves_enabled = prev

    def _ensure_at_home(self) -> None:
        assert self.asic is not None
        if getattr(self.asic, "is_at_home", lambda: True)():
            return
        home = getattr(self.asic, "home", None)
        if not callable(home):
            raise CalibrationError("Carriage not at home and home() unavailable")
        logger.info("GL128 shading: carriage off home — parking before measure/apply")
        home()

    def apply_colour_asic_shading(self, entry: CalibEntry) -> None:
        """Re-upload a cached AHB blob + AFE (no strip re-acquire)."""
        if self.asic is None:
            raise CalibrationError("Calibrator has no ASIC handle")
        if not entry.has_asic_blob:
            raise CalibrationError("Calib entry has no ASIC shading blob")
        assert entry.shading_blob is not None
        assert entry.afe_offsets is not None and entry.afe_gains is not None
        from pyopticfilm.scan.calib_gl128 import AfeFrontend

        with self._stationary_motor():
            self._ensure_at_home()
            fe = AfeFrontend(offsets=entry.afe_offsets, gains=entry.afe_gains)
            apply = getattr(self.asic, "apply_frontend", None)
            if callable(apply):
                apply(fe)
            self.asic.last_afe = fe  # type: ignore[attr-defined]
            upload = getattr(self.asic, "upload_shading_table", None)
            if not callable(upload):
                raise CalibrationError("ASIC has no upload_shading_table")
            upload(entry.shading_blob)
            self.asic.asic_shading_ready = True  # type: ignore[attr-defined]
        self.prefer_asic_shading = True
        self._active = entry
        logger.info(
            "GL128 applied cached ASIC shading dpi=%d blob=%d bytes",
            entry.resolution,
            len(entry.shading_blob),
        )

    def measure_colour_asic_shading(self, geometry: ScanGeometry) -> CalibEntry:
        """AFE + dark/white at home; save blob. Retries once after re-home on film-like fail."""
        if self.asic is None:
            raise CalibrationError("Calibrator has no ASIC handle")
        search = getattr(self.asic, "search_afe", None)
        shade = getattr(self.asic, "run_asic_shading", None)
        if not callable(shade):
            raise CalibrationError("ASIC has no run_asic_shading")

        last_reason = "unknown"
        for attempt in range(2):
            with self._stationary_motor():
                self._ensure_at_home()
                last_afe = getattr(self.asic, "last_afe", None)
                reuse_afe = (
                    last_afe is not None
                    and getattr(self.asic, "_scan_method", None) == "transparency"
                )
                if callable(search) and not reuse_afe:
                    logger.info("GL128 running stationary AFE search (attempt %d)", attempt + 1)
                    search(method="transparency")
                elif reuse_afe:
                    logger.info(
                        "GL128 reusing colour AFE codes for dpi=%d shading remesure (attempt %d)",
                        geometry.resolution,
                        attempt + 1,
                    )
                logger.info(
                    "GL128 measuring ASIC shading dpi=%d window=%d..%d dpiset=%d (attempt %d)",
                    geometry.resolution,
                    geometry.pixel_startx,
                    geometry.pixel_endx,
                    geometry.register_dpiset,
                    attempt + 1,
                )
                blob = shade(
                    resolution=geometry.resolution,
                    method="transparency",
                    strpixel=geometry.pixel_startx,
                    endpixel=geometry.pixel_endx,
                    dpiset=geometry.register_dpiset,
                )
            ready = bool(getattr(self.asic, "asic_shading_ready", False))
            if ready:
                last_afe = getattr(self.asic, "last_afe", None)
                if last_afe is None:
                    raise CalibrationError("ASIC shading ready but last_afe missing")
                entry = CalibEntry(
                    method="transparency",
                    resolution=geometry.resolution,
                    startx=geometry.startx,
                    pixels=geometry.pixels,
                    dark=np.zeros((geometry.pixels, 3), dtype=np.uint16),
                    white=np.full((geometry.pixels, 3), 65535, dtype=np.uint16),
                    calibrated_at=time.time(),
                    asic_shading=True,
                    shading_blob=bytes(blob),
                    afe_offsets=tuple(int(v) for v in last_afe.offsets),
                    afe_gains=tuple(int(v) for v in last_afe.gains),
                )
                self.cache.upsert(entry)
                self.cache.save()
                self._active = entry
                self.prefer_asic_shading = True
                logger.info(
                    "GL128 ASIC shading measured dpi=%d blob=%d bytes",
                    geometry.resolution,
                    len(entry.shading_blob or b""),
                )
                return entry
            if getattr(self.asic, "last_color_shading_host_ok", False) is True:
                last_afe = getattr(self.asic, "last_afe", None)
                if last_afe is None:
                    raise CalibrationError("Host shading fallback missing last_afe")
                dark_cols = getattr(self.asic, "last_host_calib_dark", None) or []
                white_cols = getattr(self.asic, "last_host_calib_white", None) or []
                if len(dark_cols) < 8 or len(white_cols) < 8:
                    raise CalibrationError("Host shading fallback missing strip columns")
                dark_arr = np.asarray(dark_cols, dtype=np.uint16)
                white_arr = np.asarray(white_cols, dtype=np.uint16)
                need = int(geometry.pixels)
                if dark_arr.shape[0] < need:
                    pad = need - dark_arr.shape[0]
                    dark_arr = np.vstack([dark_arr, np.repeat(dark_arr[-1:], pad, axis=0)])
                    white_arr = np.vstack([white_arr, np.repeat(white_arr[-1:], pad, axis=0)])
                else:
                    dark_arr = dark_arr[:need]
                    white_arr = white_arr[:need]
                entry = CalibEntry(
                    method="transparency",
                    resolution=geometry.resolution,
                    startx=geometry.startx,
                    pixels=geometry.pixels,
                    dark=dark_arr,
                    white=white_arr,
                    calibrated_at=time.time(),
                    asic_shading=False,
                    shading_blob=None,
                    afe_offsets=tuple(int(v) for v in last_afe.offsets),
                    afe_gains=tuple(int(v) for v in last_afe.gains),
                )
                self.cache.upsert(entry)
                self.cache.save()
                self._active = entry
                self.prefer_asic_shading = False
                logger.info(
                    "GL128 host dark/white calib dpi=%d pixels=%d (ASIC DVDSET off)",
                    geometry.resolution,
                    need,
                )
                return entry
            last_reason = str(getattr(self.asic, "last_color_shading_reject_reason", None) or "ASIC shading validation failed")
            if attempt == 0:
                logger.warning(
                    "GL128 colour shading rejected (%s) — re-homing and retrying once",
                    last_reason,
                )
                home = getattr(self.asic, "home", None)
                if callable(home):
                    # Temporarily need motor for home; stationary context already exited.
                    prev = bool(getattr(self.asic, "_motor_moves_enabled", True))
                    self.asic._motor_moves_enabled = True  # type: ignore[attr-defined]
                    try:
                        home()
                    finally:
                        self.asic._motor_moves_enabled = prev  # type: ignore[attr-defined]

        raise CalibrationError(colour_shading_failure_message(last_reason))

    def ensure_colour_asic_shading(self, geometry: ScanGeometry) -> CalibEntry:
        """Apply cached blob if present; otherwise measure at home (SF order)."""
        hit = self.find_for_scan(method="transparency", geometry=geometry)
        if hit is not None and hit.has_asic_blob:
            if not getattr(self.asic, "asic_shading_ready", False):
                self.apply_colour_asic_shading(hit)
            else:
                self.prefer_asic_shading = True
                self._active = hit
            return hit
        if (
            hit is not None
            and not hit.asic_shading
            and hit.afe_offsets is not None
            and hit.afe_gains is not None
            and hit.dark is not None
            and hit.white is not None
        ):
            from pyopticfilm.scan.calib_gl128 import AfeFrontend

            if self.asic is not None:
                fe = AfeFrontend(offsets=hit.afe_offsets, gains=hit.afe_gains)
                apply = getattr(self.asic, "apply_frontend", None)
                if callable(apply):
                    apply(fe)
                self.asic.last_afe = fe  # type: ignore[attr-defined]
                self.asic.asic_shading_ready = False  # type: ignore[attr-defined]
            self.prefer_asic_shading = False
            self._active = hit
            logger.info(
                "GL128 using cached host dark/white calib dpi=%d",
                geometry.resolution,
            )
            return hit
        return self.measure_colour_asic_shading(geometry)

    def _apply_host_afe(self, entry: CalibEntry) -> None:
        """Re-apply cached FE codes so image configure matches the calib strips."""
        if self.asic is None:
            return
        if entry.afe_offsets is None or entry.afe_gains is None:
            return
        apply = getattr(self.asic, "apply_afe_frontend", None)
        if callable(apply):
            from pyopticfilm.scan.calib_gl128 import AfeFrontend

            apply(
                AfeFrontend(offsets=entry.afe_offsets, gains=entry.afe_gains),
                method=entry.method,
                resolution=entry.resolution,
            )
            return
        # GL845 also accepts last_* + apply_frontend_for_scan.
        if hasattr(self.asic, "last_afe_gains"):
            self.asic.last_afe_gains = entry.afe_gains  # type: ignore[attr-defined]
            self.asic.last_afe_offsets = entry.afe_offsets  # type: ignore[attr-defined]
            apply_fe = getattr(self.asic, "apply_frontend_for_scan", None)
            if callable(apply_fe):
                apply_fe(resolution=entry.resolution, method=entry.method)

    def ensure_host_calib(
        self,
        geometry: ScanGeometry,
        *,
        method: str = "transparency",
        mode: str = "color",
    ) -> CalibEntry:
        """Genesys path: cached host dark/white, or measure via :meth:`run`.

        Never calls :meth:`ensure_colour_asic_shading` (SE / GL128 only).
        """
        if method not in {"transparency", "infrared"}:
            raise ValueError(f"Unsupported method {method!r}")
        if mode not in {"color", "infrared"}:
            raise ValueError(f"Unsupported mode {mode!r}")
        hit = self.find_for_scan(method=method, geometry=geometry)
        if hit is not None and not hit.asic_shading:
            self.prefer_asic_shading = False
            self._active = hit
            self._apply_host_afe(hit)
            logger.info(
                "Using cached host calib method=%s dpi=%d pixels=%d",
                method,
                geometry.resolution,
                geometry.pixels,
            )
            return hit
        return self.run(
            resolution=geometry.resolution,
            mode=mode,
            geometry=geometry,
        )

    def upload_asic_shading(
        self,
        geometry: ScanGeometry,
        *,
        method: str = "transparency",
    ) -> None:
        """Measure or apply colour ASIC shading for ``geometry``."""
        if method != "transparency":
            return
        self.ensure_colour_asic_shading(geometry)

    def run(
        self,
        *,
        resolution: int = 1800,
        mode: str = "color",
        force: bool = False,
        area: tuple[float, float, float, float] | None = None,
        geometry: ScanGeometry | None = None,
    ) -> CalibEntry:
        """Run dark (if color) + white shading for ``resolution`` / ``mode``."""
        if self.asic is None:
            raise CalibrationError("Calibrator has no ASIC handle")
        if mode == "gray":
            raise ValueError("Grayscale calibration is not implemented")
        if mode not in {"color", "infrared"}:
            raise ValueError(f"Unsupported mode {mode!r}")

        method = "infrared" if mode == "infrared" else "transparency"
        # Match scan geometry keys so apply is 1:1 for full-TA (or given area).
        scan_geo = geometry if geometry is not None else compute_geometry(resolution, model=self.model, area=area)
        resolution = scan_geo.resolution
        existing = self.cache.find(
            method=method,
            resolution=resolution,
            startx=scan_geo.startx,
            pixels=scan_geo.pixels,
        )
        if existing is not None and not force and (not existing.asic_shading or existing.has_asic_blob):
            logger.info(
                "Using cached calib method=%s dpi=%d pixels=%d",
                method,
                resolution,
                scan_geo.pixels,
            )
            self._active = existing
            if (
                existing.has_asic_blob
                and method == "transparency"
                and not getattr(self.asic, "asic_shading_ready", False)
            ):
                self.apply_colour_asic_shading(existing)
            return existing

        if not self.asic._initialized:
            self.asic.init()

        from pyopticfilm.scan.session import create_session

        session = create_session(self.asic, self.model)
        calib_geo = compute_calib_geometry(resolution, model=self.model)
        # Force the calib strip to match scan startx/pixels so shading applies
        # 1:1. Everything else — line count, stagger, oversampling — stays as
        # the calibration geometry computed it. USB calib depth stays 16-bit
        # even when the image stream is 8-bit (GL128).
        calib_depth = int(getattr(self.model, "usb_calib_depth", calib_geo.depth))
        from pyopticfilm.device.sensor_lookup import (
            dummy_pixel_for,
            exposure_lperiod_for,
        )

        calib_geo = replace(
            calib_geo,
            pixels=scan_geo.pixels,
            startx=scan_geo.startx,
            pixel_startx=scan_geo.pixel_startx,
            pixel_endx=scan_geo.pixel_endx,
            optical_pixels=scan_geo.optical_pixels,
            output_pixel_offset=scan_geo.output_pixel_offset,
            depth=calib_depth,
            line_bytes=scan_geo.pixels * calib_geo.channels * (calib_depth // 8),
            disable_buffer_full_move=True,
            exposure_lperiod=exposure_lperiod_for(
                self.model, resolution, method=method
            ),
            dummy_pixel=dummy_pixel_for(self.model, resolution),
        )

        self.asic.set_scan_method(method)

        is_gl128 = getattr(self.model, "asic", "") == "GL128"

        # SE colour: measure (or force re-measure) at home; persist blob+AFE.
        if is_gl128 and method == "transparency":
            if force:
                return self.measure_colour_asic_shading(scan_geo)
            return self.ensure_colour_asic_shading(scan_geo)

        # Genesys / non-GL128: AFE search before dark/white strips.
        # OpticFilm ADI FESET uses table defaults (SANE skip). When dichotomy
        # applies, measure strip means via a stationary capture.
        search = getattr(self.asic, "search_afe", None)
        if callable(search):
            measure = None
            applicable = getattr(self.asic, "afe_dichotomy_applicable", None)
            if callable(applicable) and applicable():

                def measure(fe: object) -> tuple[float, float, float]:
                    avg = self._capture_average(
                        session,
                        calib_geo,
                        method=method,
                        lamp_on=True,
                        start_motor=False,
                        settle_s=0.05,
                        label="afe",
                    )
                    return (
                        float(avg[:, 0].mean()),
                        float(avg[:, 1].mean()),
                        float(avg[:, 2].mean()),
                    )

            try:
                search(method=method, resolution=resolution, measure=measure)
            except TypeError:
                search(method=method)

        def _safe_home() -> None:
            """GL845 repark; SE has no reverse-home — noop only when already home."""
            if is_gl128:
                try:
                    if self.asic.is_at_home():
                        return
                except Exception:  # noqa: BLE001, S110
                    pass
                logger.debug("SE calib: skip home (no capture-proven reverse-home)")
                return
            self.asic.home()

        if mode == "infrared":
            dark = np.zeros((scan_geo.pixels, 3), dtype=np.uint16)
            logger.info("IR calib: skipping dark (genesys behaviour)")
        else:
            dark = self._capture_average(
                session,
                calib_geo,
                method=method,
                lamp_on=False,
                start_motor=False,
                settle_s=DARK_SETTLE_S,
                label="dark",
            )
            _safe_home()

        white = self._capture_average(
            session,
            calib_geo,
            method=method,
            lamp_on=True,
            # SE shading stays put; GL845 white strip may use a short feed.
            start_motor=not is_gl128,
            settle_s=WHITE_SETTLE_S,
            label="white",
        )
        _safe_home()

        afe_offsets = getattr(self.asic, "last_afe_offsets", None)
        afe_gains = getattr(self.asic, "last_afe_gains", None)
        if afe_offsets is None:
            last = getattr(self.asic, "last_afe", None)
            if last is not None:
                afe_offsets = tuple(int(v) for v in last.offsets)
                afe_gains = tuple(int(v) for v in last.gains)

        entry = CalibEntry(
            method=method,
            resolution=resolution,
            startx=scan_geo.startx,
            pixels=scan_geo.pixels,
            dark=dark,
            white=white,
            calibrated_at=time.time(),
            asic_shading=False,
            afe_offsets=tuple(int(v) for v in afe_offsets) if afe_offsets else None,
            afe_gains=tuple(int(v) for v in afe_gains) if afe_gains else None,
        )
        self.cache.upsert(entry)
        self.cache.save()
        self._active = entry
        self.prefer_asic_shading = False
        logger.info(
            "Calib done method=%s dpi=%d dark_mean=%.0f white_mean=%.0f afe_gains=%s",
            method,
            resolution,
            float(dark.mean()),
            float(white.mean()),
            entry.afe_gains,
        )
        return entry

    def _capture_average(
        self,
        session: object,
        geometry: ScanGeometry,
        *,
        method: str,
        lamp_on: bool,
        start_motor: bool,
        settle_s: float,
        label: str,
    ) -> np.ndarray:
        assert self.asic is not None
        from pyopticfilm.scan.session import ScanSession

        assert isinstance(session, ScanSession)  # Gl128ScanSession is a subclass
        self.asic.set_scan_method(method)
        if lamp_on:
            self.asic.lamp_on()
        else:
            self.asic.lamp_off()
        time.sleep(settle_s)

        raw = session.acquire_raw(
            geometry,
            method=method,
            lamp_on=lamp_on,
            start_motor=start_motor,
        )
        rgb = self.pipeline.assemble(raw, geometry)
        avg = np.median(rgb.astype(np.float32), axis=0)
        out = np.clip(np.rint(avg), 0, 65535).astype(np.uint16)
        logger.info(
            "%s calib average shape=%s mean=%.1f",
            label,
            out.shape,
            float(out.mean()),
        )
        return out
