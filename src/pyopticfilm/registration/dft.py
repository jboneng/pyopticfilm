# SPDX-License-Identifier: GPL-3.0-or-later
"""Guizar-Sicairos localized matrix-multiply DFT subpixel refinement."""

from __future__ import annotations

import threading

import numpy as np

# The localized DFT is a BLAS matrix product. Tiles are refined from worker threads,
# and BLAS calls made from several threads at once ran ten times slower than the
# same calls made in turn, so the product is taken one thread at a time.
_MATMUL_LOCK = threading.Lock()


def refine_peak_guizar(
    cross_spectrum: np.ndarray,
    peak_y: int,
    peak_x: int,
    *,
    upsample_factor: int = 16,
    upsample_region: int | None = None,
) -> tuple[float, float]:
    """Refine an integer correlation peak to a ``1/upsample_factor`` grid.

    ``cross_spectrum`` is the full complex cross-spectrum ``F(a) * conj(F(b))``
    (not necessarily phase-normalized), shaped ``(H, W)`` matching ``fft2``.

    Returns ``(dx, dy)`` in pixels (same sign convention as the integer peak
    expressed as a signed shift from the DFT origin).
    """
    u = max(1, int(upsample_factor))
    if u == 1:
        return float(peak_x), float(peak_y)

    h, w = cross_spectrum.shape
    # Neighborhood half-width in upsampled pixels (default: 1.5 input pixels).
    side = upsample_region if upsample_region is not None else u * 1.5
    # dftups evaluates a (noutr x noutc) patch of the upsampled IFFT.
    nout = int(np.ceil(side * 2)) | 1  # odd
    # Center the patch on the integer peak (in upsampled coordinates).
    # Guizar: shift coordinates so peak is at the patch center.
    # Initial estimate in DFT-shifted signed coords.
    dftshift = float(np.trunc(nout / 2.0))
    # Matrix-multiply DFT (Guizar-Sicairos eq. for localized IFFT).
    # Kernels along each axis:
    #   kernc[x, k] = exp(2πi * k * (x - dftshift - peak_x*u) / (u*W))  ...
    # Using the common skimage/Guizar formulation:
    nor = nout
    noc = nout
    # Offsets of the output grid relative to the (upsampled) peak.
    # row/col coordinates of the upsampled patch, centered on peak.
    r_offset = dftshift - peak_y * u
    c_offset = dftshift - peak_x * u

    # Build kernels: (nor, H) and (W, noc) for cross @ style multiply.
    # IFFT of cross: sum_{u,v} C[u,v] exp(2πi (uy/H + vx/W)) / (H W)
    # Localized: evaluate at y = (r - r_offset)/u, x = (c - c_offset)/u.
    ky = np.arange(h, dtype=np.float64)
    # Shift FFT frequencies to match fft2 layout (0..H/2, then negative).
    ky[ky >= (h + 1) // 2] -= h
    kx = np.arange(w, dtype=np.float64)
    kx[kx >= (w + 1) // 2] -= w

    ys = (np.arange(nor, dtype=np.float64) - r_offset) / u
    xs = (np.arange(noc, dtype=np.float64) - c_offset) / u

    # kern_y[r, fy] = exp(2πi * fy * ys[r] / H)
    kern_y = np.exp(2j * np.pi * np.outer(ys, ky) / h)  # (nor, H)
    kern_x = np.exp(2j * np.pi * np.outer(kx, xs) / w)  # (W, noc)

    # corr_ups = kern_y @ cross @ kern_x  / (H W)  — but we only need argmax.
    c = np.asarray(cross_spectrum, dtype=np.complex128)
    with _MATMUL_LOCK:
        patch = (kern_y @ c) @ kern_x
    patch = np.real(patch)
    imax = int(np.argmax(patch))
    pr, pc = divmod(imax, noc)
    # Convert patch index back to image-pixel shift.
    dy = (pr - dftshift) / u + peak_y
    dx = (pc - dftshift) / u + peak_x

    # Wrap to signed displacement consistent with fft peak indexing.
    if dx >= w / 2:
        dx -= w
    elif dx < -w / 2:
        dx += w
    if dy >= h / 2:
        dy -= h
    elif dy < -h / 2:
        dy += h
    return float(dx), float(dy)


def refine_peaks_guizar(
    cross_spectra: np.ndarray,
    peaks_y: np.ndarray,
    peaks_x: np.ndarray,
    *,
    upsample_factor: int = 16,
) -> tuple[np.ndarray, np.ndarray]:
    """:func:`refine_peak_guizar` over a ``(T, H, W)`` stack, one integer peak per spectrum.

    Returns ``(dx, dy)`` arrays of length ``T``, each entry as the single call gives.
    """
    u = int(upsample_factor)
    c = np.asarray(cross_spectra, dtype=np.complex128)
    _, h, w = c.shape
    peaks_y = np.asarray(peaks_y, dtype=np.float64)
    peaks_x = np.asarray(peaks_x, dtype=np.float64)
    nout = int(np.ceil(u * 1.5 * 2)) | 1
    dftshift = float(np.trunc(nout / 2.0))
    ky = np.arange(h, dtype=np.float64)
    ky[ky >= (h + 1) // 2] -= h
    kx = np.arange(w, dtype=np.float64)
    kx[kx >= (w + 1) // 2] -= w
    grid = np.arange(nout, dtype=np.float64)
    ys = (grid - (dftshift - peaks_y * u)[:, None]) / u
    xs = (grid - (dftshift - peaks_x * u)[:, None]) / u
    kern_y = np.exp(2j * np.pi * (ys[:, :, None] * ky) / h)  # (T, nout, H)
    kern_x = np.exp(2j * np.pi * (kx[:, None] * xs[:, None, :]) / w)  # (T, W, nout)
    with _MATMUL_LOCK:
        patch = np.real((kern_y @ c) @ kern_x)
    pr, pc = np.divmod(patch.reshape(patch.shape[0], -1).argmax(axis=1), nout)
    dy = (pr - dftshift) / u + peaks_y
    dx = (pc - dftshift) / u + peaks_x
    dx = np.where(dx >= w / 2, dx - w, np.where(dx < -w / 2, dx + w, dx))
    dy = np.where(dy >= h / 2, dy - h, np.where(dy < -h / 2, dy + h, dy))
    return dx, dy


def brute_force_upsample_peak(
    cross_spectrum: np.ndarray,
    *,
    upsample_factor: int,
    search_radius: float = 2.0,
) -> tuple[float, float]:
    """Reference: zero-pad IFFT around DC / peak for small arrays (tests only)."""
    u = int(upsample_factor)
    h, w = cross_spectrum.shape
    mh, mw = h * u, w * u
    padded = np.zeros((mh, mw), dtype=np.complex128)
    # Place spectrum with fftshift-style centering into padded array.
    c = np.fft.fftshift(cross_spectrum)
    y0 = (mh - h) // 2
    x0 = (mw - w) // 2
    padded[y0 : y0 + h, x0 : x0 + w] = c
    corr = np.fft.ifft2(np.fft.ifftshift(padded))
    corr = np.fft.fftshift(np.real(corr))
    cy, cx = mh // 2, mw // 2
    r = int(np.ceil(search_radius * u))
    roi = corr[cy - r : cy + r + 1, cx - r : cx + r + 1]
    imax = int(np.argmax(roi))
    pr, pc = divmod(imax, roi.shape[1])
    dy = (pr - r) / u
    dx = (pc - r) / u
    return float(dx), float(dy)
