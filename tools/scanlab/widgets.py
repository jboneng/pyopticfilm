# SPDX-License-Identifier: GPL-3.0-or-later
"""Image view with a normalized rubber-band crop."""

from __future__ import annotations

import numpy as np
from PyQt6.QtCore import QEvent, QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from tools.scanlab.preview import auto_level_u8, downsample_for_display

__all__ = [
    "CropImageView",
    "ImageTabPage",
    "commit_crop_norm",
    "downsample_for_display",
    "gray16_to_qimage",
    "rgb16_to_qimage",
]


def commit_crop_norm(
    current: tuple[float, float, float, float] | None,
    candidate: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    """Keep ``current`` when the rubber-band was too small to commit."""
    return current if candidate is None else candidate


def _u8_rgb_to_qimage(u8: np.ndarray) -> QImage:
    """Build a QImage whose scanlines are 4-byte aligned (required by Qt)."""
    height, width, _ = u8.shape
    # RGB32 is B,G,R,x on little-endian and is what QPixmap uses on Windows.
    bgra = np.empty((height, width, 4), dtype=np.uint8)
    bgra[:, :, 0] = u8[:, :, 2]
    bgra[:, :, 1] = u8[:, :, 1]
    bgra[:, :, 2] = u8[:, :, 0]
    bgra[:, :, 3] = 255
    bgra = np.ascontiguousarray(bgra)
    image = QImage(bgra.data, width, height, 4 * width, QImage.Format.Format_RGB32)
    return image.copy()


def rgb16_to_qimage(rgb: np.ndarray, *, auto_level: bool = False) -> QImage:
    """Convert HxWx3 uint16 to an 8-bit RGB QImage (owned copy).

    ``auto_level`` uses a 1–99% percentile stretch (useful for raw capture
    decode where ``>> 8`` alone looks crushed). Large arrays are subsampled
    first so 3600/7200 ppi previews stay displayable.
    """
    arr = downsample_for_display(np.asarray(rgb))
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected HxWx3, got {arr.shape}")
    if auto_level:
        u8 = auto_level_u8(arr)
    else:
        u8 = np.ascontiguousarray(
            (arr.astype(np.uint32) >> 8).clip(0, 255).astype(np.uint8)
        )
    return _u8_rgb_to_qimage(u8)


def gray16_to_qimage(gray: np.ndarray) -> QImage:
    """Convert HxW uint16 to an 8-bit grayscale QImage (owned copy)."""
    arr = downsample_for_display(np.asarray(gray))
    if arr.ndim != 2:
        raise ValueError(f"expected HxW, got {arr.shape}")
    u8 = np.ascontiguousarray((arr.astype(np.uint32) >> 8).clip(0, 255).astype(np.uint8))
    # Reuse the RGB32 path so grayscale IR at 7200 has the same stride safety.
    rgb = np.repeat(u8[:, :, None], 3, axis=2)
    return _u8_rgb_to_qimage(rgb)


class CropImageView(QWidget):
    """Scales a scan preview and optionally lets the user drag a crop rectangle (0..1)."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        allow_crop: bool = True,
        background: str = "#1a1a1a",
    ) -> None:
        super().__init__(parent)
        self._allow_crop = allow_crop
        self._background = QColor(background)
        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._label.setMinimumSize(240, 180)
        self._label.setStyleSheet(f"background: {background}; color: #888;")
        self._label.setText("No image")
        self._pixmap = QPixmap()
        self._raw_rgb: np.ndarray | None = None
        self._raw_gray: np.ndarray | None = None
        self._crop: tuple[float, float, float, float] | None = None
        self._drag_origin: QPoint | None = None
        self._drag_rect: QRect | None = None
        self._label.installEventFilter(self)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self._label)

    @property
    def crop_norm(self) -> tuple[float, float, float, float] | None:
        return self._crop

    def has_image(self) -> bool:
        return self._raw_rgb is not None or self._raw_gray is not None

    def raw_rgb(self) -> np.ndarray | None:
        return self._raw_rgb

    def raw_gray(self) -> np.ndarray | None:
        return self._raw_gray

    def clear_crop(self) -> None:
        self._crop = None
        self._refresh()

    def set_rgb(self, rgb: np.ndarray | None, *, auto_level: bool = False) -> None:
        if rgb is None:
            self._raw_rgb = None
            self._raw_gray = None
            self._pixmap = QPixmap()
            self._label.setPixmap(QPixmap())
            self._label.setText("No image")
            return
        self._raw_rgb = np.asarray(rgb)
        self._raw_gray = None
        image = rgb16_to_qimage(rgb, auto_level=auto_level)
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            self._pixmap = QPixmap()
            self._label.setPixmap(QPixmap())
            self._label.setText("Image too large to display")
            return
        self._label.setText("")
        self._pixmap = pixmap
        self._refresh()

    def set_gray(self, gray: np.ndarray | None) -> None:
        if gray is None:
            self.set_rgb(None)
            return
        self._raw_gray = np.asarray(gray)
        self._raw_rgb = None
        image = gray16_to_qimage(gray)
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            self._pixmap = QPixmap()
            self._label.setPixmap(QPixmap())
            self._label.setText("Image too large to display")
            return
        self._label.setText("")
        self._pixmap = pixmap
        self._refresh()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh()

    def eventFilter(self, watched, event) -> bool:
        if not self._allow_crop:
            return super().eventFilter(watched, event)
        if watched is not self._label or self._pixmap.isNull():
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.position().toPoint()
            self._drag_rect = QRect(self._drag_origin, self._drag_origin)
            return True
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.RightButton:
            if self._crop is not None:
                self.clear_crop()
            return True
        if event.type() == QEvent.Type.MouseMove and self._drag_origin is not None:
            self._drag_rect = QRect(self._drag_origin, event.position().toPoint()).normalized()
            self._refresh()
            return True
        if event.type() == QEvent.Type.MouseButtonRelease and self._drag_origin is not None:
            rect = QRect(self._drag_origin, event.position().toPoint()).normalized()
            self._drag_origin = None
            self._drag_rect = None
            self._crop = commit_crop_norm(self._crop, self._rect_to_norm(rect))
            self._refresh()
            return True
        return super().eventFilter(watched, event)

    def _image_rect(self) -> QRect:
        if self._pixmap.isNull():
            return QRect()
        label_w, label_h = self._label.width(), self._label.height()
        pix_w, pix_h = self._pixmap.width(), self._pixmap.height()
        if label_w < 1 or label_h < 1 or pix_w < 1 or pix_h < 1:
            return QRect()
        scale = min(label_w / pix_w, label_h / pix_h)
        draw_w = max(1, int(pix_w * scale))
        draw_h = max(1, int(pix_h * scale))
        x = (label_w - draw_w) // 2
        y = (label_h - draw_h) // 2
        return QRect(x, y, draw_w, draw_h)

    def _rect_to_norm(self, rect: QRect) -> tuple[float, float, float, float] | None:
        ir = self._image_rect()
        if ir.width() < 8 or ir.height() < 8:
            return None
        inter = rect.intersected(ir)
        if inter.width() < 4 or inter.height() < 4:
            return None
        x1 = (inter.left() - ir.left()) / ir.width()
        y1 = (inter.top() - ir.top()) / ir.height()
        x2 = (inter.right() - ir.left()) / ir.width()
        y2 = (inter.bottom() - ir.top()) / ir.height()
        return (
            max(0.0, min(1.0, x1)),
            max(0.0, min(1.0, y1)),
            max(0.0, min(1.0, x2)),
            max(0.0, min(1.0, y2)),
        )

    def _refresh(self) -> None:
        if self._pixmap.isNull():
            return
        ir = self._image_rect()
        if ir.isEmpty():
            return
        scaled = self._pixmap.scaled(
            ir.size(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        canvas = QPixmap(self._label.size())
        canvas.fill(self._background)
        painter = QPainter(canvas)
        painter.drawPixmap(ir.topLeft(), scaled)
        if self._allow_crop:
            crop_rect = self._drag_rect
            if crop_rect is None and self._crop is not None:
                x1, y1, x2, y2 = self._crop
                crop_rect = QRect(
                    ir.left() + int(x1 * ir.width()),
                    ir.top() + int(y1 * ir.height()),
                    max(1, int((x2 - x1) * ir.width())),
                    max(1, int((y2 - y1) * ir.height())),
                )
            if crop_rect is not None:
                painter.setPen(QPen(QColor("#4fc3f7"), 2))
                painter.setBrush(QColor(79, 195, 247, 40))
                painter.drawRect(crop_rect.intersected(ir))
        painter.end()
        self._label.setPixmap(canvas)


class ImageTabPage(QWidget):
    """Image preview tab with optional 16-bit TIFF load/save."""

    load_clicked = pyqtSignal()

    def __init__(
        self,
        *,
        default_stem: str = "scan",
        allow_load: bool = False,
        allow_crop: bool = False,
        background: str = "#1a1a1a",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._default_stem = default_stem
        self._dpi: int | None = None
        self._is_gray = False
        self._view = CropImageView(self, allow_crop=allow_crop, background=background)
        self._caption = QLabel("")
        self._caption.setWordWrap(True)
        self._caption.setStyleSheet("color: #aaa; padding: 2px 6px;")
        self._caption.hide()
        self._save_btn = QPushButton("Save 16-bit TIFF…")
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._on_save)
        row = QHBoxLayout()
        if allow_load:
            self._load_btn = QPushButton("Load 16-bit TIFF…")
            self._load_btn.clicked.connect(self.load_clicked.emit)
            row.addWidget(self._load_btn)
        row.addStretch(1)
        row.addWidget(self._save_btn)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self._view, 1)
        box.addWidget(self._caption)
        box.addLayout(row)

    @property
    def crop_norm(self) -> tuple[float, float, float, float] | None:
        return self._view.crop_norm

    def clear_crop(self) -> None:
        self._view.clear_crop()

    def set_caption(self, text: str) -> None:
        self._caption.setText(text)
        self._caption.setVisible(bool(text))

    def set_rgb(
        self,
        rgb: np.ndarray | None,
        *,
        dpi: int | None = None,
        auto_level: bool = False,
    ) -> None:
        self._is_gray = False
        if dpi is not None:
            self._dpi = dpi
        self._view.set_rgb(rgb, auto_level=auto_level)
        self._save_btn.setEnabled(self._view.has_image())

    def set_gray(self, gray: np.ndarray | None, *, dpi: int | None = None) -> None:
        self._is_gray = True
        if dpi is not None:
            self._dpi = dpi
        self._view.set_gray(gray)
        self._save_btn.setEnabled(self._view.has_image())

    def _default_filename(self) -> str:
        stem = self._default_stem
        if self._dpi is not None:
            return f"{stem}_{self._dpi}dpi.tif"
        return f"{stem}.tif"

    def _on_save(self) -> None:
        from pyopticfilm.exceptions import PlustekError
        from pyopticfilm.image import save_gray16_tiff, save_rgb16_tiff

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save 16-bit TIFF",
            self._default_filename(),
            "TIFF (*.tif *.tiff);;All files (*.*)",
        )
        if not path:
            return
        dpi = self._dpi or 1800
        try:
            if self._is_gray:
                gray = self._view.raw_gray()
                if gray is None:
                    return
                saved = save_gray16_tiff(gray, path, dpi=dpi)
            else:
                rgb = self._view.raw_rgb()
                if rgb is None:
                    return
                saved = save_rgb16_tiff(rgb, path, dpi=dpi)
        except PlustekError as exc:
            QMessageBox.warning(self, "Save TIFF", str(exc))
            return
        except OSError as exc:
            QMessageBox.warning(self, "Save TIFF", f"Could not write file:\n{exc}")
            return
        win = self.window()
        if hasattr(win, "statusBar"):
            win.statusBar().showMessage(f"Saved {saved.name}")
