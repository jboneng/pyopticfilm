# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side image reconstruction (RGB16, shifts, stagger, shading)."""

from __future__ import annotations

import numpy as np

from pyopticfilm.device.model_8200i import MODEL_8200I
from pyopticfilm.device.protocol import FilmModel
from pyopticfilm.logging import get_logger
from pyopticfilm.scan.geometry import ScanGeometry

logger = get_logger(__name__)

#: After dark/white stretch, push frame highlights toward sensor white when the
#: home-chrome white reference is brighter than anything in the film window.
#: NegPy metering expects film base near full scale on a negative scan.
HOST_CALIB_PEAK_PERCENTILE = 99.7
HOST_CALIB_PEAK_TARGET = 0xF000
HOST_CALIB_PEAK_TRIGGER = 0.85
#: Cap post-ME makeup so already-bright pixels are not driven into hard clip.
HOST_CALIB_HIGHLIGHT_CEILING = int(0.92 * 65535)
HOST_CALIB_HIGHLIGHT_PERCENTILE = 99.9
#: Drop this fraction from each edge when estimating film highlights / clamping
#: holder chrome so NegPy auto bounds do not latch onto Full-window margins.
HOST_CALIB_BORDER_INSET = 0.04

#: Row slabs for host calib / makeup — avoids full-frame float temps at 7200 dpi.
_HOST_CALIB_CHUNK_ROWS = 256
_FS = np.float32(65535.0)


def _edge_column_means(rgb: np.ndarray, n: int = 16) -> tuple[list[float], list[float]]:
    """Mean DN of the first/last ``n`` USB columns (all channels)."""
    arr = np.asarray(rgb)
    if arr.ndim != 3 or arr.shape[1] < 2:
        return [], []
    count = min(int(n), max(1, arr.shape[1] // 2))
    head = [float(arr[:, i, :3].mean()) for i in range(count)]
    tail = [float(arr[:, arr.shape[1] - count + i, :3].mean()) for i in range(count)]
    return head, tail


def _fmt_means(values: list[float], *, n: int = 8) -> str:
    shown = values[:n]
    return "[" + ", ".join(f"{v:.0f}" for v in shown) + "]"


#: Linear-negative USB dummy is near-zero DN; film / gate is far brighter.
_USB_END_DUMMY_MEAN_MAX = 2048.0


def count_usb_end_dummy_columns(
    rgb: np.ndarray,
    *,
    max_drop: int,
    dummy_mean_max: float = _USB_END_DUMMY_MEAN_MAX,
) -> int:
    """Count ENDPIXEL-side columns that look like USB dummy, capped at ``max_drop``.

    Walks inward from the USB ENDPIXEL edge (last columns, pre-``mirror_x``)
    while the column mean stays near zero. Stops at the first film/frame
    column so the gate is not trimmed. Never returns more than ``max_drop``.
    """
    arr = np.asarray(rgb)
    if arr.ndim != 3 or int(max_drop) <= 0 or arr.shape[1] < 16:
        return 0
    cap = min(int(max_drop), arr.shape[1] - 8)
    if cap <= 0:
        return 0
    threshold = float(dummy_mean_max)
    channels = min(3, arr.shape[2])
    dropped = 0
    for i in range(1, cap + 1):
        if float(arr[:, -i, :channels].mean()) > threshold:
            break
        dropped = i
    return dropped


def trim_to_optical_span(rgb: np.ndarray, geometry: ScanGeometry) -> np.ndarray:
    """Drop trailing columns past the programmed STR/END output width.

    Mirrors the pcap 7200 path where URBs can include dummy columns beyond
    ``ENDPIXEL−STRPIXEL``. At matching live geometry this is a no-op.
    """
    arr = np.asarray(rgb)
    expected = int(geometry.pixels)
    if expected < 8 or arr.shape[1] <= expected:
        return arr
    logger.info(
        "trimmed optical span width %d → %d (USB wider than geometry.pixels)",
        arr.shape[1],
        expected,
    )
    if arr.ndim == 3:
        return np.ascontiguousarray(arr[:, :expected, :])
    return np.ascontiguousarray(arr[:, :expected])


class ImagePipeline:
    """Convert raw scanner bytes into HxWx3 uint16 RGB."""

    def __init__(self, model: FilmModel = MODEL_8200I) -> None:
        self.model = model
        #: Columns dropped from the USB ENDPIXEL side on the last :meth:`assemble`.
        #: Multi-pass callers lock the short-plane value for long/IR assemble.
        self.last_usb_end_drop: int = 0

    @staticmethod
    def _inset_slice(h: int, w: int, inset: float) -> tuple[slice, slice] | None:
        """Return ``(ys, xs)`` for a centered inset, or ``None`` if too small."""
        frac = min(max(float(inset), 0.0), 0.2)
        if frac <= 0 or h < 8 or w < 8:
            return None
        cut_h = max(1, round(h * frac))
        cut_w = max(1, round(w * frac))
        if cut_h * 2 >= h or cut_w * 2 >= w:
            return None
        return slice(cut_h, h - cut_h), slice(cut_w, w - cut_w)

    def decode_rgb(
        self,
        raw: bytes,
        *,
        geometry: ScanGeometry,
        planar: bool | None = None,
    ) -> np.ndarray:
        """Decode USB RGB optical buffer → uint16 HxWx3.

        8-bit USB image streams (GL128 colour/IR) are upsampled to 16-bit
        host samples (``value * 257``). Calib/shading buffers stay native 16-bit.

        Layout is model-dependent: GL845 and GL128 film images are chunky
        ``RGBRGB…`` per line (SE session 11). Pass ``planar`` to override.
        AFE strip probes may differ; do not confuse them with image layout.
        """
        expected = geometry.total_bytes
        if len(raw) < expected:
            raise ValueError(f"Short scan buffer: got {len(raw)} want {expected}")
        h = geometry.optical_line_count
        w = geometry.pixels
        c = geometry.channels
        if planar is None:
            planar = bool(getattr(self.model, "usb_planar_rgb", False))
        if geometry.depth == 8:
            flat = np.frombuffer(raw[:expected], dtype=np.uint8)
            arr = flat.reshape(h, c, w).transpose(0, 2, 1) if planar else flat.reshape(h, w, c)
            return (arr.astype(np.uint16) * np.uint16(257)).copy()
        if geometry.depth != 16:
            raise ValueError(f"Unsupported geometry depth {geometry.depth}")
        words = np.frombuffer(raw[:expected], dtype="<u2").copy()
        if bool(getattr(self.model, "swap_16bit_data", False)):
            # SANE genesys sensor flag: byteswap each 16-bit sample (GL843 7200i).
            words.byteswap(inplace=True)
        arr = (
            words.reshape(h, c, w).transpose(0, 2, 1)
            if planar
            else words.reshape(h, w, c)
        )
        return np.array(arr, dtype=np.uint16, copy=True)

    def decode_rgb16(
        self,
        raw: bytes,
        *,
        geometry: ScanGeometry,
        planar: bool | None = None,
    ) -> np.ndarray:
        """Backward-compatible alias for :meth:`decode_rgb`."""
        return self.decode_rgb(raw, geometry=geometry, planar=planar)

    def reduce_y_oversample(self, rgb: np.ndarray, geometry: ScanGeometry) -> np.ndarray:
        """Average USB rows down to ``geometry.lines``.

        Only the group size actually present in the buffer is collapsed:
        ``optical_line_count // lines``. The GL128 image path samples Y at
        twice the programmed dpi and delivers ``LINCNT/2`` rows for ``LINCNT/4``
        lines, so pairs are averaged here — without it the image comes out
        stretched 2x vertically. Shading passes average ``y_oversample`` rows
        because their ``LINCNT`` is in native units.
        """
        if geometry.lines <= 0:
            return rgb
        n = geometry.optical_line_count // geometry.lines
        if n <= 1:
            return rgb

        height, width, channels = rgb.shape
        groups = height // n
        if groups < 1:
            raise ValueError(f"Buffer of {height} rows is shorter than one {n}-row group")
        trimmed = rgb[: groups * n].reshape(groups, n, width, channels)
        # Integer sum avoids a full-resolution float64 mean (OOM at 7200 dpi).
        sums = trimmed.astype(np.uint32).sum(axis=1, dtype=np.uint32)
        out = (sums + n // 2) // n
        logger.debug("averaged %d rows -> %d (oversample=%d)", height, groups, n)
        return out.astype(np.uint16)

    def apply_host_downsample(self, rgb: np.ndarray, geometry: ScanGeometry) -> np.ndarray:
        """Block-average when the ASIC ran hotter than the requested PPI (SE <600)."""
        factor = int(getattr(geometry, "host_downsample", 1) or 1)
        if factor <= 1:
            return rgb
        h, w, c = rgb.shape
        nh = (h // factor) * factor
        nw = (w // factor) * factor
        if nh == 0 or nw == 0:
            return rgb
        block = rgb[:nh, :nw].reshape(nh // factor, factor, nw // factor, factor, c)
        count = factor * factor
        sums = block.astype(np.uint32).sum(axis=(1, 3), dtype=np.uint32)
        out = (sums + count // 2) // count
        logger.debug(
            "host downsample %dx%d -> %dx%d (factor=%d)",
            h,
            w,
            out.shape[0],
            out.shape[1],
            factor,
        )
        return out.astype(np.uint16)

    def apply_line_shifts(self, rgb: np.ndarray, geometry: ScanGeometry) -> np.ndarray:
        """Align R/G/B using genesys ld_shift scaled to yres.

        Height is ``lines + num_staggered_lines`` when the buffer carries the
        extra ``max_shift`` lines (GL845). Models that size ``LINCNT`` for the
        crop alone — GL128 OpticFilm, which has no travel to spare — lose
        ``max_shift`` lines off the bottom instead.
        """
        shifts = (geometry.shift_r, geometry.shift_g, geometry.shift_b)
        out_h = geometry.lines + geometry.num_staggered_lines
        if max(shifts) == 0:
            return rgb[:out_h].copy()

        height, width, channels = rgb.shape
        assert channels == 3
        out_h = min(out_h, height - max(shifts))
        if out_h < 1:
            raise ValueError("Optical buffer shorter than required after shift")
        out = np.zeros((out_h, width, 3), dtype=np.uint16)
        for c, shift in enumerate(shifts):
            out[:, :, c] = rgb[shift : shift + out_h, :, c]
        logger.debug("applied line shifts r/g/b=%s", shifts)
        return out

    def apply_y_stagger(self, rgb: np.ndarray, geometry: ScanGeometry) -> np.ndarray:
        """Unstagger alternating columns (7200 dpi ``StaggerConfig{4,0}``).

        Prefer a buffer of ``lines + num_staggered_lines``. Models that size
        ``LINCNT`` for the crop alone — GL128 OpticFilm under ladder override —
        lose ``max(shift)`` lines off the bottom instead (same clamp as
        :meth:`apply_line_shifts`).
        """
        shifts = geometry.stagger_y
        if not shifts or geometry.num_staggered_lines == 0:
            return rgb[: geometry.lines]

        height, width, channels = rgb.shape
        assert channels == 3
        max_shift = max(shifts)
        out_h = min(geometry.lines, height - max_shift)
        if out_h < 1:
            raise ValueError("Buffer shorter than required for Y stagger")

        out = np.empty((out_h, width, 3), dtype=np.uint16)
        n = len(shifts)
        for x in range(width):
            shift = shifts[x % n]
            out[:, x, :] = rgb[shift : shift + out_h, x, :]
        logger.debug("applied y stagger shifts=%s out_h=%d", shifts, out_h)
        return out

    def clamp_border_highlights(
        self,
        rgb: np.ndarray,
        *,
        inset: float = HOST_CALIB_BORDER_INSET,
        peak_percentile: float = HOST_CALIB_PEAK_PERCENTILE,
    ) -> np.ndarray:
        """Pull Full-window holder chrome down to the film-window highlight peak.

        NegPy auto Dmin/bounds treat near-white negative margins as film base;
        chrome brighter than the framed film skews the positive until the user
        crops. Needed on both paths: host stretch maps the home strip to white,
        and ASIC DVDSET flattens that same chrome to full scale by construction.
        Interior pixels are unchanged.

        The ceiling is per channel. One joint percentile leaves the margin above
        the dimmest channel's own film peak, so auto Dmin meters that channel off
        neutral chrome instead of the orange base and the positive takes the
        complementary cast (green, when a 27.4k joint ceiling met a 19.3k green).
        """
        if rgb.ndim != 3 or rgb.shape[2] != 3:
            raise ValueError(f"rgb must be HxWx3, got {rgb.shape}")
        h, w, _ = rgb.shape
        region = self._inset_slice(h, w, inset)
        if region is None:
            return rgb
        ys, xs = region
        peaks = np.percentile(rgb[ys, xs], float(peak_percentile), axis=(0, 1))
        if float(peaks.max()) <= 0:
            return rgb
        peaks = np.maximum(peaks.astype(np.float32), 1.0)
        border = np.ones((h, w), dtype=bool)
        border[ys, xs] = False
        if not border.any():
            return rgb
        peaks_u16 = np.clip(np.rint(peaks), 0, 65535).astype(np.uint16)
        hot = border & (rgb > peaks_u16).any(axis=2)
        if not hot.any():
            return rgb
        out = rgb.copy()
        out[hot] = np.minimum(out[hot], peaks_u16)
        logger.info(
            "border highlight clamp inset=%.2f peak_p%.1f=(%.0f,%.0f,%.0f) pixels=%d",
            float(inset),
            float(peak_percentile),
            peaks[0],
            peaks[1],
            peaks[2],
            int(hot.sum()),
        )
        return out

    def expose_film_base(
        self,
        rgb: np.ndarray,
        *,
        source: str,
        peak_target: int = HOST_CALIB_PEAK_TARGET,
        peak_percentile: float = HOST_CALIB_PEAK_PERCENTILE,
        preserve_headroom: bool = False,
        highlight_ceiling: int = HOST_CALIB_HIGHLIGHT_CEILING,
        highlight_percentile: float = HOST_CALIB_HIGHLIGHT_PERCENTILE,
    ) -> np.ndarray:
        """Lift the film window toward sensor white with one scalar gain.

        Both shading paths reference *home* chrome, which is brighter than the
        light at the scan position, so the negative lands well below full scale
        (~42% at 1800 dpi) and NegPy meters a thin negative into a washed-bright
        positive. The gain is scalar and keyed to the brightest channel: a
        per-channel lift would neutralize the orange mask that inversion needs.

        When ``preserve_headroom`` is set (ME deliverable), gain is also capped
        so the high highlight percentile does not exceed ``highlight_ceiling`` —
        avoiding a blunt stretch that undoes IVW highlight recovery.
        """
        target = int(peak_target)
        if target <= 0:
            return rgb
        h, w, _ = rgb.shape
        region = self._inset_slice(h, w, HOST_CALIB_BORDER_INSET)
        sample = rgb[region[0], region[1]] if region is not None else rgb
        peaks = np.percentile(sample, float(peak_percentile), axis=(0, 1))
        peak = float(np.max(peaks))
        if peak <= 1.0 or peak >= float(target) * float(HOST_CALIB_PEAK_TRIGGER):
            return rgb
        gain = float(target) / peak
        if preserve_headroom and highlight_ceiling > 0:
            hi = float(
                np.percentile(sample, float(highlight_percentile), axis=(0, 1)).max()
            )
            if hi > 1.0:
                gain = min(gain, float(highlight_ceiling) / hi)
        if gain <= 1.0 + 1e-6:
            return rgb
        logger.info(
            "%s exposure makeup gain=%.3f peak_p%.1f=%.0f → %d%s",
            source,
            gain,
            float(peak_percentile),
            peak,
            target,
            " (headroom)" if preserve_headroom else "",
        )
        gain_f = np.float32(gain)
        out = np.empty_like(rgb)
        for y0 in range(0, h, _HOST_CALIB_CHUNK_ROWS):
            y1 = min(h, y0 + _HOST_CALIB_CHUNK_ROWS)
            slab = rgb[y0:y1].astype(np.float32)
            np.multiply(slab, gain_f, out=slab)
            np.clip(slab, 0, 65535, out=slab)
            out[y0:y1] = np.rint(slab).astype(np.uint16)
        return out

    def apply_host_calib(
        self,
        rgb: np.ndarray,
        *,
        dark: np.ndarray,
        white: np.ndarray,
        peak_target: int = HOST_CALIB_PEAK_TARGET,
        peak_percentile: float = HOST_CALIB_PEAK_PERCENTILE,
        expose_base: bool = True,
    ) -> np.ndarray:
        """Host-side shading: column flat-field, then optionally expose film base.

        ``dark`` / ``white`` are (pixels, 3) uint16 column averages from the home
        chrome strip. Mapping that strip to 65535 leaves the film window dark when
        scan-position light is lower than home — NegPy then meters a thin-looking
        positive. A percentile makeup brings frame highlights up to ``peak_target``.
        Border chrome brighter than the film inset is then clamped so auto bounds
        do not latch onto holder margins.

        Pass ``expose_base=False`` for ME bracket planes so short/long stay linear
        for merge (makeup runs once on the final deliverable).
        """
        if dark.shape != white.shape:
            raise ValueError(f"dark/white shape mismatch: {dark.shape} vs {white.shape}")
        if rgb.ndim != 3 or rgb.shape[2] != 3:
            raise ValueError(f"rgb must be HxWx3, got {rgb.shape}")
        if dark.shape[0] < rgb.shape[1] or dark.shape[1] != 3:
            raise ValueError(f"calib width {dark.shape} incompatible with image width {rgb.shape[1]}")

        dark_f = dark[: rgb.shape[1]].astype(np.float32)
        white_f = white[: rgb.shape[1]].astype(np.float32)
        denom = white_f - dark_f
        bad = denom <= 0
        denom = np.where(bad, np.float32(1.0), denom)
        offset = dark_f / _FS
        mult = _FS / denom

        h = rgb.shape[0]
        out = np.empty_like(rgb)
        for y0 in range(0, h, _HOST_CALIB_CHUNK_ROWS):
            y1 = min(h, y0 + _HOST_CALIB_CHUNK_ROWS)
            slab = rgb[y0:y1].astype(np.float32) / _FS
            corrected = (slab - offset) * mult
            np.clip(corrected * _FS, 0, 65535, out=corrected)
            chunk = np.rint(corrected).astype(np.uint16)
            if bad.any():
                mask = np.broadcast_to(bad, chunk.shape)
                out[y0:y1] = np.where(mask, rgb[y0:y1], chunk)
            else:
                out[y0:y1] = chunk

        if not expose_base:
            return out
        exposed = self.expose_film_base(
            out,
            source="host calib",
            peak_target=peak_target,
            peak_percentile=peak_percentile,
        )
        return self.clamp_border_highlights(exposed, peak_percentile=peak_percentile)

    def assemble(
        self,
        raw: bytes,
        geometry: ScanGeometry,
        *,
        dark: np.ndarray | None = None,
        white: np.ndarray | None = None,
        planar: bool | None = None,
        expose_base: bool = True,
        usb_end_drop: int | None = None,
    ) -> np.ndarray:
        """Decode USB RGB and apply calib / optional film-base makeup.

        ``expose_base=False`` skips scalar peak stretch and border clamp so ME
        short/long planes stay linear (SilverFast-wire scale) for host merge.

        Image width equals the programmed STR/END span minus an ENDPIXEL dummy
        trim (capped at ``geometry.usb_end_drop``). With ``usb_end_drop=None``
        the trim is content-aware; pass an ``int`` to reuse a locked drop from
        an earlier plane (ME short → long/IR) so widths match. Pcap 7200 may
        still drop dummy URB columns via :func:`trim_to_optical_span`.
        """
        rgb = self.decode_rgb(raw, geometry=geometry, planar=planar)
        rgb = self.reduce_y_oversample(rgb, geometry)
        rgb = self.apply_line_shifts(rgb, geometry)
        rgb = self.apply_y_stagger(rgb, geometry)
        frame_lines = int(getattr(geometry, "frame_lines", 0) or 0)
        if 0 < frame_lines < rgb.shape[0]:
            rgb = rgb[:frame_lines]
        rgb = self.apply_host_downsample(rgb, geometry)
        rgb = trim_to_optical_span(rgb, geometry)
        cap = 0 if geometry.disable_buffer_full_move else int(getattr(geometry, "usb_end_drop", 0) or 0)
        if usb_end_drop is None:
            drop = count_usb_end_dummy_columns(rgb, max_drop=cap)
        else:
            drop = max(0, min(int(usb_end_drop), cap, max(0, rgb.shape[1] - 8)))
        self.last_usb_end_drop = drop
        if drop > 0:
            rgb = np.ascontiguousarray(rgb[:, : rgb.shape[1] - drop, :])
            logger.info(
                "dropped %d USB-end columns (ENDPIXEL dummy, cap %d) → width %d",
                drop,
                cap,
                rgb.shape[1],
            )
        host_calib = dark is not None and white is not None
        if host_calib:
            rgb = self.apply_host_calib(
                rgb, dark=dark, white=white, expose_base=expose_base
            )
        elif expose_base:
            rgb = self.expose_film_base(rgb, source="asic shading")
            rgb = self.clamp_border_highlights(rgb)
        pre_head, pre_tail = _edge_column_means(rgb)
        if getattr(self.model, "mirror_x", False):
            rgb = np.ascontiguousarray(rgb[:, ::-1, :])
        post_head, _post_tail = _edge_column_means(rgb)
        logger.info(
            "assemble %ddpi str=%d end=%d pixels=%d optical=%d offset=%d "
            "dpiset=%d usb_end_drop=%d area=%s host_calib=%s "
            "pre_usb[:,:8]=%s pre_usb[:,-8:]=%s post_mirror[:,:8]=%s",
            geometry.resolution,
            geometry.pixel_startx,
            geometry.pixel_endx,
            geometry.pixels,
            geometry.optical_pixels,
            geometry.output_pixel_offset,
            geometry.register_dpiset,
            cap,
            geometry.area,
            host_calib,
            _fmt_means(pre_head),
            _fmt_means(pre_tail),
            _fmt_means(post_head),
        )
        return rgb