# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan buffer helpers and optional TIFF export."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import numpy as np

from pyopticfilm.exceptions import PlustekError


@dataclass
class ScanImage:
    """Host-side scan result.

    ``rgb`` is HxWx3 uint16 linear data (colour or infrared-illuminated CCD
    frame). For ``mode="infrared"``, ``ir`` is also filled with an HxW uint16
    plane (green CCD channel, host-flattened on GL128) for dust/iSRD use.

    All arrays are **linear film-negative measurements** (bright sensor values
    correspond to dense emulsion). Positive conversion is the host app's job.

    Traditional silver black-and-white film (HP5, Tri-X, ...) blocks infrared,
    so its ``ir`` plane records the picture rather than only dust. Check
    :attr:`ir_tracks_image` before using ``ir`` for dust removal.

    Multi-exposure (GL128 hardware-tested models): the SNR/IVW-merged
    deliverable with film-base makeup is in ``rgb``. Bracket planes and fusion
    stats are **not** on ``ScanImage`` — use
    :attr:`~pyopticfilm.scanner.Scanner.last_me_debug` for Scan Lab / bring-up
    inspection.
    """

    rgb: np.ndarray
    dpi: int
    device_model: str = "PLUSTEK OpticFilm 8200i"
    ir: np.ndarray | None = None

    @cached_property
    def ir_tracks_image(self) -> bool | None:
        """True when ``ir`` follows the picture (silver film), ``None`` without IR."""
        if self.ir is None:
            return None
        return infrared_tracks_image(self.rgb, self.ir)

    def save_tiff(self, path: str | Path) -> Path:
        """Write a 16-bit RGB TIFF (the scanned frame only; no ``*_IR`` sidecar)."""
        return save_rgb16_tiff(self.rgb, path, dpi=self.dpi)


#: IR p95-p5 spread over its median above which IR is not just a dust field.
#: Colour negative measured 0.036, silver HP5 Plus 0.76 (7600i v2).
IR_IMAGE_SPREAD = 0.2
#: Block-averaged IR vs visible correlation above which IR follows the picture.
#: Colour negative 0.60 (faint dye/shading), HP5 Plus and Tri-X 400 0.999.
IR_IMAGE_CORRELATION = 0.8
#: IR spread relative to the visible spread for low-contrast frames: silver
#: film copies the image into IR (HP5 0.995, Tri-X 1.047), colour does not (0.053).
IR_IMAGE_SPREAD_RATIO = 0.5
#: Visible spread below which the ratio test is skipped (spreads are noise).
VISIBLE_MIN_SPREAD = 0.1


def infrared_tracks_image(
    rgb: np.ndarray,
    ir: np.ndarray,
    *,
    inset: float = 0.06,
    block: int = 8,
) -> bool:
    """True when the IR plane records the picture, so it is unusable as a dust map.

    Dyes in colour (and chromogenic B&W) film are IR-transparent, so their IR
    plane is flat apart from dust. Silver grains block IR, so a silver B&W IR
    plane varies widely and tracks the visible image. Both tests run on
    ``block``-averaged pixels inside an ``inset`` border to ignore grain and
    the holder edge.
    """
    vis = np.asarray(rgb, dtype=np.float64)
    inf = np.asarray(ir, dtype=np.float64)
    if vis.ndim == 3:
        vis = vis.mean(axis=2)
    if vis.shape != inf.shape:
        raise ValueError(f"rgb {vis.shape} and ir {inf.shape} differ in size")
    h, w = inf.shape
    y0, y1 = int(h * inset), int(h * (1 - inset))
    x0, x1 = int(w * inset), int(w * (1 - inset))
    hh, ww = ((y1 - y0) // block) * block, ((x1 - x0) // block) * block
    if hh < 2 * block or ww < 2 * block:
        return False

    def pool(a: np.ndarray) -> np.ndarray:
        a = a[y0 : y0 + hh, x0 : x0 + ww]
        return a.reshape(hh // block, block, ww // block, block).mean(axis=(1, 3)).ravel()

    v, i = pool(vis), pool(inf)
    if float(np.median(i)) <= 0 or float(np.median(v)) <= 0 or v.std() == 0 or i.std() == 0:
        return False

    def spread(a: np.ndarray) -> float:
        return float(np.percentile(a, 95) - np.percentile(a, 5)) / float(np.median(a))

    ir_spread, vis_spread = spread(i), spread(v)
    # A thin, flat silver frame varies little in IR, but as much as it does in
    # the visible; colour IR stays flat whatever the image contrast.
    varies = ir_spread >= IR_IMAGE_SPREAD or (
        vis_spread >= VISIBLE_MIN_SPREAD and ir_spread >= IR_IMAGE_SPREAD_RATIO * vis_spread
    )
    return varies and float(np.corrcoef(v, i)[0, 1]) > IR_IMAGE_CORRELATION


def save_rgb16_tiff(rgb: np.ndarray, path: str | Path, *, dpi: int) -> Path:
    """Write HxWx3 uint16 linear RGB to a compressed TIFF with resolution tags."""
    arr = np.asarray(rgb)
    if arr.dtype != np.uint16:
        raise PlustekError(f"rgb must be uint16, got {arr.dtype}")
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise PlustekError(f"rgb must be HxWx3, got shape {arr.shape}")

    try:
        import tifffile
    except ImportError as exc:
        raise PlustekError("TIFF export requires tifffile") from exc

    out = Path(path)
    if out.suffix.lower() not in {".tif", ".tiff"}:
        out = out.with_suffix(".tif")

    tifffile.imwrite(
        out,
        arr,
        photometric="rgb",
        compression="zlib",
        predictor=True,
        resolution=(dpi, dpi),
        resolutionunit="inch",
    )
    return out


def save_gray16_tiff(gray: np.ndarray, path: str | Path, *, dpi: int) -> Path:
    """Write HxW uint16 grayscale to a compressed TIFF with resolution tags."""
    arr = np.asarray(gray)
    if arr.dtype != np.uint16:
        raise PlustekError(f"gray must be uint16, got {arr.dtype}")
    if arr.ndim != 2:
        raise PlustekError(f"gray must be HxW, got shape {arr.shape}")

    try:
        import tifffile
    except ImportError as exc:
        raise PlustekError("TIFF export requires tifffile") from exc

    out = Path(path)
    if out.suffix.lower() not in {".tif", ".tiff"}:
        out = out.with_suffix(".tif")

    tifffile.imwrite(
        out,
        arr,
        photometric="minisblack",
        compression="zlib",
        predictor=True,
        resolution=(dpi, dpi),
        resolutionunit="inch",
    )
    return out


def load_rgb16_tiff(path: str | Path, *, default_dpi: int = 1800) -> tuple[np.ndarray, int]:
    """Read a 16-bit RGB TIFF written by :func:`save_rgb16_tiff` or Scan Lab."""
    try:
        import tifffile
    except ImportError as exc:
        raise PlustekError("TIFF import requires tifffile") from exc

    src = Path(path)
    with tifffile.TiffFile(src) as tif:
        arr = np.asarray(tif.asarray())
        dpi = _tiff_dpi_from_page(tif.pages[0]) if tif.pages else None

    if arr.ndim == 2:
        arr = np.stack([arr, arr, arr], axis=-1)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise PlustekError(f"expected HxWx3 RGB TIFF, got shape {arr.shape}")
    if arr.dtype != np.uint16:
        if np.issubdtype(arr.dtype, np.floating):
            arr = np.clip(arr, 0, 65535).astype(np.uint16)
        elif arr.dtype == np.uint8:
            arr = (arr.astype(np.uint16) << 8) | arr.astype(np.uint16)
        else:
            arr = arr.astype(np.uint16)

    return np.ascontiguousarray(arr), int(dpi if dpi else default_dpi)


def _tiff_dpi_from_page(page) -> int | None:
    """Best-effort DPI from TIFF resolution tags."""
    try:
        xres = page.tags.get("XResolution")
        if xres is None:
            return None
        value = xres.value
        if isinstance(value, tuple) and len(value) == 2 and value[1]:
            x = float(value[0]) / float(value[1])
        else:
            x = float(value)
        unit = page.tags.get("ResolutionUnit")
        if unit is not None and int(unit.value) == 3:
            x *= 2.54
        if x <= 0:
            return None
        return round(x)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
