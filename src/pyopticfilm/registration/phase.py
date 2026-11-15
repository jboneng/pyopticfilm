# SPDX-License-Identifier: GPL-3.0-or-later
"""Windowed phase correlation (NumPy FFT; NegPy phase-correlate convention)."""

from __future__ import annotations

import numpy as np

from pyopticfilm.registration.dft import refine_peak_guizar, refine_peaks_guizar


def optimal_dft_size(n: int) -> int:
    """Smallest ``2^a 3^b 5^c`` at least ``n`` (FFT-friendly pad length)."""
    n = max(1, int(n))
    m = n
    while True:
        k = m
        for p in (2, 3, 5):
            while k % p == 0:
                k //= p
        if k == 1:
            return m
        m += 1


def hanning_window(h: int, w: int) -> np.ndarray:
    """Sqrt of separable Hann window, shape ``(h, w)``, float32."""
    h = int(h)
    w = int(w)
    if h < 2 or w < 2:
        return np.ones((max(h, 1), max(w, 1)), dtype=np.float32)
    tau = 2.0 * np.pi
    c0 = tau / (w - 1)
    c1 = tau / (h - 1)
    wc = 0.5 * (1.0 - np.cos(c0 * np.arange(w, dtype=np.float64)))
    wr = 0.5 * (1.0 - np.cos(c1 * np.arange(h, dtype=np.float64)))
    win = np.sqrt(np.outer(wr, wc)).astype(np.float32)
    return win


def _fftshift_peak_coords(corr: np.ndarray) -> tuple[float, float, float, float]:
    """fftShift, 5×5 weighted centroid, return ``(dx, dy, response, peak_ratio)``.

    Measure from the roll origin ``(n//2, m//2)`` (NegPy phase-correlate).
    """
    m, n = corr.shape
    ym, xm = m // 2, n // 2
    shifted = np.fft.fftshift(corr)
    peak = int(np.argmax(shifted))
    py, px = divmod(peak, n)
    y0, y1 = max(0, py - 2), min(m - 1, py + 2)
    x0, x1 = max(0, px - 2), min(n - 1, px + 2)
    patch = shifted[y0 : y1 + 1, x0 : x1 + 1].astype(np.float64)
    yy, xx = np.mgrid[y0 : y1 + 1, x0 : x1 + 1]
    weight_sum = float(patch.sum()) + np.finfo(np.float64).eps
    cx = float((xx * patch).sum() / weight_sum)
    cy = float((yy * patch).sum() / weight_sum)
    response = weight_sum / float(m * n)
    # Second peak margin for ambiguity detection (suppress 3×3 around primary).
    masked = shifted.copy()
    masked[max(0, py - 1) : min(m, py + 2), max(0, px - 1) : min(n, px + 2)] = -np.inf
    second = float(np.max(masked)) if masked.size else 0.0
    first = float(shifted[py, px])
    peak_ratio = (first - second) / (abs(first) + 1e-12) if first != 0 else 0.0
    dx = float(xm) - cx
    dy = float(ym) - cy
    return dx, dy, response, peak_ratio


def phase_correlate(
    a: np.ndarray,
    b: np.ndarray,
    *,
    window: np.ndarray | None = None,
    upsample: int = 1,
) -> tuple[float, float, float, float]:
    """Phase correlate ``a`` vs ``b`` → ``(dx, dy, response, peak_ratio)``.

    Returns the shift of ``b`` relative to ``a`` (NegPy convention).
    Callers that pass ``(moving, reference)`` therefore get the warp shift to
    apply to ``moving`` to land on ``reference``.
    """
    a = np.ascontiguousarray(a, dtype=np.float32)
    b = np.ascontiguousarray(b, dtype=np.float32)
    if a.shape != b.shape or a.ndim != 2:
        return 0.0, 0.0, 0.0, 0.0
    h, w = a.shape
    if h < 2 or w < 2:
        return 0.0, 0.0, 0.0, 0.0
    win = window if window is not None else hanning_window(h, w)
    m, n = optimal_dft_size(h), optimal_dft_size(w)
    # Zero-pad after windowing.
    wa = np.zeros((m, n), dtype=np.float32)
    wb = np.zeros((m, n), dtype=np.float32)
    wa[:h, :w] = a * win
    wb[:h, :w] = b * win
    # float32 FFTs — coarse/ROI sizes stay modest; matches speed targets.
    fa = np.fft.rfft2(wa)
    fb = np.fft.rfft2(wb)
    cross = fa * np.conj(fb)
    mag = np.abs(cross)
    eps = np.finfo(np.float32).eps * max(float(mag.max()), 1.0)
    cross = np.divide(cross, mag, out=np.zeros_like(cross), where=mag > eps)
    # Unnormalized inverse (norm="forward") so the 5×5 response scale is stable.
    corr = np.fft.irfft2(cross, s=(m, n), norm="forward")
    dx, dy, response, peak_ratio = _fftshift_peak_coords(corr)

    if upsample > 1:
        peak = int(np.argmax(corr))
        py, px = divmod(peak, n)
        ix = px if px < n / 2 else px - n
        iy = py if py < m / 2 else py - m
        # Ordinary cross-spectrum for Guizar (small ROI — float64 OK).
        fa2 = np.fft.fft2(wa.astype(np.float64))
        fb2 = np.fft.fft2(wb.astype(np.float64))
        # Peak of ifft(Fa*conj(Fb)) is shift of b relative to a; Guizar's
        # matrix-multiply DFT returns the opposite sign of the 5×5 centroid —
        # negate to match ``_fftshift_peak_coords``.
        cross_full = fa2 * np.conj(fb2)
        rdx, rdy = refine_peak_guizar(cross_full, iy, ix, upsample_factor=upsample)
        dx, dy = -float(rdx), -float(rdy)

    return dx, dy, response, peak_ratio


def phase_correlate_batch(a: np.ndarray, b: np.ndarray, *, upsample: int) -> tuple[np.ndarray, np.ndarray]:
    """:func:`phase_correlate` with ``upsample`` over ``(T, h, w)`` stacks of tiles.

    Returns ``(dx, dy)``, each of length ``T`` and equal to the per-tile call's.
    Hundreds of small tiles cost far less as one stacked FFT than as one call
    each, where the time goes to call overhead rather than the transforms.
    Only the refined (``upsample > 1``) shift is computed.
    """
    if upsample <= 1:
        raise ValueError("phase_correlate_batch needs upsample > 1")
    a = np.ascontiguousarray(a, dtype=np.float32)
    b = np.ascontiguousarray(b, dtype=np.float32)
    if a.shape != b.shape or a.ndim != 3:
        raise ValueError(f"expected matching (T, h, w) stacks, got {a.shape} and {b.shape}")
    count, h, w = a.shape
    win = hanning_window(h, w)
    m, n = optimal_dft_size(h), optimal_dft_size(w)
    wa = np.zeros((count, m, n), dtype=np.float32)
    wb = np.zeros((count, m, n), dtype=np.float32)
    wa[:, :h, :w] = a * win
    wb[:, :h, :w] = b * win
    cross = np.fft.rfft2(wa) * np.conj(np.fft.rfft2(wb))
    mag = np.abs(cross)
    eps = np.finfo(np.float32).eps * np.maximum(mag.max(axis=(1, 2)).astype(np.float64), 1.0)
    cross = np.divide(cross, mag, out=np.zeros_like(cross), where=mag > eps.astype(np.float32)[:, None, None])
    corr = np.fft.irfft2(cross, s=(m, n), norm="forward")
    py, px = np.divmod(corr.reshape(count, -1).argmax(axis=1), n)
    ix = np.where(px < n / 2, px, px - n)
    iy = np.where(py < m / 2, py, py - m)
    cross_full = np.fft.fft2(wa.astype(np.float64)) * np.conj(np.fft.fft2(wb.astype(np.float64)))
    rdx, rdy = refine_peaks_guizar(cross_full, iy, ix, upsample_factor=upsample)
    return -rdx, -rdy


def phase_correlate_shift(
    ref: np.ndarray,
    mov: np.ndarray,
    *,
    scale: float = 1.0,
    upsample: int = 1,
) -> tuple[float, float, float]:
    """Pass-align convention: ``(dx, dy, response)`` for ``moving`` → ``reference``.

    Matches ``pass_align._phase_correlate_shift``: phaseCorrelate(mov, ref).
    """
    dx, dy, resp, _ = phase_correlate(mov, ref, upsample=upsample)
    return float(dx * scale), float(dy * scale), float(resp)


def top_k_peaks(corr: np.ndarray, k: int = 5, *, min_separation: int = 3) -> list[tuple[int, int, float]]:
    """Unshifted correlation peaks as ``(iy, ix, value)`` with local suppression."""
    work = corr.copy()
    peaks: list[tuple[int, int, float]] = []
    m, n = work.shape
    for _ in range(k):
        idx = int(np.argmax(work))
        py, px = divmod(idx, n)
        val = float(work[py, px])
        if not np.isfinite(val):
            break
        peaks.append((py, px, val))
        y0, y1 = max(0, py - min_separation), min(m, py + min_separation + 1)
        x0, x1 = max(0, px - min_separation), min(n, px + min_separation + 1)
        work[y0:y1, x0:x1] = -np.inf
    return peaks
