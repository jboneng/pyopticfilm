# SPDX-License-Identifier: GPL-3.0-or-later
"""Image view with a normalized rubber-band crop."""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from PyQt6.QtCore import QEvent, QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QIntValidator, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pyopticfilm.scan.exposure_override import MAX_EXPOSURE_REGISTER
from tools.scanlab.preview import auto_level_u8, downsample_for_display

__all__ = [
    "CropImageView",
    "ImageTabPage",
    "MeControls",
    "MeMode",
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

    def __init__(self, parent: QWidget | None = None, *, allow_crop: bool = True) -> None:
        super().__init__(parent)
        self._allow_crop = allow_crop
        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._label.setMinimumSize(240, 180)
        self._label.setStyleSheet("background: #1a1a1a; color: #888;")
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
        canvas.fill(QColor("#1a1a1a"))
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
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._default_stem = default_stem
        self._dpi: int | None = None
        self._is_gray = False
        self._view = CropImageView(self, allow_crop=allow_crop)
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


class MeMode:
    """One enum, single source of truth for "what ME mode is selected" —
    replaces reading me_pass/me_fixed_long/me_n_brackets checkbox+spinbox
    state fresh at every call site. Values are the QComboBox item order."""

    OFF = "off"
    DYNAMIC = "dynamic"
    FIXED_FAST = "fixed_fast"
    N_EXPOSURE = "n_exposure"

    _LABELS: ClassVar[dict[str, str]] = {
        OFF: "Off",
        DYNAMIC: "Dynamic (adaptive long exposure)",
        FIXED_FAST: "Fixed Fast (long pinned at 42k)",
        N_EXPOSURE: "N-Exposure (2-9 brackets)",
    }
    #: Combo item order — OFF first so index 0 is always the safe default.
    ORDER: ClassVar[tuple[str, ...]] = (OFF, DYNAMIC, FIXED_FAST, N_EXPOSURE)


class MeControls(QWidget):
    """Multi-exposure (ME) controls: one mode dropdown replacing the
    me_pass/me_fixed_long/me_n_brackets checkbox+spinbox tangle, plus a
    collapsed-by-default manual-exposure disclosure for Scan Lab debugging.

    Mirrors the mode taxonomy the repo owner asked for on PR #47 —
    "Dynamic" / "Fixed Fast" / "N-Exposure" — as one QComboBox instead of
    three independently-reasoned controls, so "what mode is the user in" is
    always exactly ``self.mode()``, not something re-inferred from multiple
    widget states at each call site. Intended as the ScanLab-side reference
    for the equivalent NegPy control (this is UI *pattern*, not shared code
    — NegPy has its own stack).
    """

    #: Fires whenever mode, n_brackets, or a manual override field changes
    #: in a way that could affect the current scan request or preview tabs.
    changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._exposure_validator = QIntValidator(1, MAX_EXPOSURE_REGISTER, self)

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Multi-exposure"))
        self.mode_combo = QComboBox()
        for key in MeMode.ORDER:
            self.mode_combo.addItem(MeMode._LABELS[key], key)
        self.mode_combo.setToolTip(
            "Off: single-exposure Scan (today's non-ME path).\n"
            "Dynamic: two exposures, long picked per-frame from image "
            "content (today's ME default).\n"
            "Fixed Fast: two exposures, long pinned to the SilverFast-"
            "validated 42000 instead of adaptive.\n"
            "N-Exposure: 2-9 exposures geometrically spaced between the "
            "short floor and the (adaptive or fixed) top exposure — lower "
            "noise, more scan time per extra bracket."
        )
        mode_row.addWidget(self.mode_combo, 1)
        box.addLayout(mode_row)

        brackets_row = QHBoxLayout()
        self.brackets_label = QLabel("Brackets")
        brackets_row.addWidget(self.brackets_label)
        self.n_brackets = QSpinBox()
        self.n_brackets.setRange(2, 9)
        self.n_brackets.setValue(3)
        self.n_brackets.setToolTip(
            "Number of exposures fused per ME scan. 2 is equivalent to "
            "Dynamic/Fixed Fast's own short+long pair; each additional "
            "bracket is a full extra pass."
        )
        brackets_row.addWidget(self.n_brackets)
        brackets_row.addStretch(1)
        box.addLayout(brackets_row)

        self.debug_toggle = QCheckBox("Debug: manual exposure override")
        self.debug_toggle.setToolTip(
            "Advanced / Scan Lab only. Bypasses the normal driver-derived "
            "exposure selection entirely and writes REG_EXPOSURE from the "
            "fields below — no safety clamp. Leave off for normal scanning."
        )
        box.addWidget(self.debug_toggle)

        self.single_pass_exposure = QLineEdit()
        self.single_pass_exposure.setPlaceholderText("auto (non-ME Scan)")
        self.single_pass_exposure.setValidator(self._exposure_validator)
        self.single_pass_exposure.setToolTip(
            "REG_EXPOSURE for a single (non-ME) Scan pass. Empty = normal "
            "driver-derived exposure with the hardware-max clamp. A value "
            "here is written verbatim, bypassing that clamp — up to "
            f"{MAX_EXPOSURE_REGISTER} (0x{MAX_EXPOSURE_REGISTER:06X})."
        )
        box.addWidget(self.single_pass_exposure)

        self.me_short_exposure = QLineEdit()
        self.me_short_exposure.setPlaceholderText("auto (ME short)")
        self.me_short_exposure.setValidator(self._exposure_validator)
        self.me_short_exposure.setToolTip(
            "REG_EXPOSURE for the ME short pass. Empty = model-derived short "
            "exposure. A value here bypasses the hardware-max clamp."
        )
        box.addWidget(self.me_short_exposure)

        self.me_long_exposure = QLineEdit()
        self.me_long_exposure.setPlaceholderText("auto (ME long / top bracket)")
        self.me_long_exposure.setValidator(self._exposure_validator)
        self.me_long_exposure.setToolTip(
            "REG_EXPOSURE for the ME long (top bracket) pass. Empty = "
            "normal mode selection above. A value here overrides the mode "
            "entirely, skips the DPI/adaptive/hardware-max clamps, and is "
            "written verbatim."
        )
        box.addWidget(self.me_long_exposure)

        self.mode_combo.currentIndexChanged.connect(self._on_changed)
        self.n_brackets.valueChanged.connect(self._on_changed)
        self.debug_toggle.toggled.connect(self._on_changed)
        self.single_pass_exposure.textChanged.connect(self._on_changed)
        self.me_short_exposure.textChanged.connect(self._on_changed)
        self.me_long_exposure.textChanged.connect(self._on_changed)

        self._sync_enabled()

    # --- public API -------------------------------------------------------

    def mode(self) -> str:
        return self.mode_combo.currentData()

    def set_mode(self, mode: str) -> None:
        idx = self.mode_combo.findData(mode)
        if idx >= 0:
            self.mode_combo.setCurrentIndex(idx)

    def set_gl128_enabled(self, enabled: bool) -> None:
        """Non-GL128 targets have no ME path at all — force Off and disable
        the whole control, same as the old me_pass.setEnabled(is_gl128)."""
        self.setEnabled(enabled)
        if not enabled:
            self.set_mode(MeMode.OFF)

    def me_pass_enabled(self) -> bool:
        return self.isEnabled() and self.mode() != MeMode.OFF

    def n_brackets_value(self) -> int:
        return self.n_brackets.value() if self.mode() == MeMode.N_EXPOSURE else 2

    def me_exposure_mode_kwarg(self) -> str | None:
        """``me_exposure_mode`` for Scanner.scan(): "fixed" for Fixed Fast,
        None otherwise (Dynamic is explicit-adaptive at n_brackets==2 and
        defers to the model default at n_brackets>2 — see
        Model.me_default_exposure_mode)."""
        return "fixed" if self.mode() == MeMode.FIXED_FAST else None

    def manual_exposure_kwargs(self) -> dict[str, int | None]:
        """(single_pass_exposure, me_short_exposure, me_long_exposure) as a
        kwargs-ready dict — every value is None unless the debug disclosure
        is open, so a stale value typed before toggling debug off never
        leaks into a scan. Raises ValueError on a malformed (non-empty,
        non-integer) field — should not happen given the QIntValidator, but
        callers building a ScanRequest should still catch it.
        """
        if not self.debug_toggle.isChecked():
            return {
                "single_pass_exposure": None,
                "me_short_exposure": None,
                "me_long_exposure": None,
            }
        me_active = self.me_pass_enabled()
        return {
            "single_pass_exposure": self._value(self.single_pass_exposure)
            if not me_active
            else None,
            "me_short_exposure": self._value(self.me_short_exposure) if me_active else None,
            "me_long_exposure": self._value(self.me_long_exposure) if me_active else None,
        }

    @staticmethod
    def _value(edit: QLineEdit) -> int | None:
        text = edit.text().strip()
        return int(text) if text else None

    # --- internal -----------------------------------------------------

    def _on_changed(self, *_args: object) -> None:
        self._sync_enabled()
        self.changed.emit()

    def _sync_enabled(self) -> None:
        me_active = self.me_pass_enabled()
        is_n_exposure = self.mode() == MeMode.N_EXPOSURE
        self.brackets_label.setEnabled(is_n_exposure)
        self.n_brackets.setEnabled(is_n_exposure)
        debug_on = self.debug_toggle.isChecked()
        self.single_pass_exposure.setEnabled(debug_on and not me_active)
        self.me_short_exposure.setEnabled(debug_on and me_active)
        self.me_long_exposure.setEnabled(debug_on and me_active)
