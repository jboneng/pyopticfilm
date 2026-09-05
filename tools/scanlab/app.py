# SPDX-License-Identifier: GPL-3.0-or-later
"""PyQt6 main window for the pyopticfilm scan lab."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QThread
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pyopticfilm.image import ScanImage
from pyopticfilm.scan.exposure_override import MAX_EXPOSURE_REGISTER
from tools.scanlab.backend import (
    LabTarget,
    device_banner,
    format_crop_status,
    format_scan_window_note,
    lab_crop_scan_meta,
    lab_scan_kwargs,
    lab_scan_needs_motor_warning,
    list_lab_targets,
    nonse_safe_y_fraction,
    usb_log_section_key,
    with_hw_override,
    with_mock_mode,
    with_motor_acoustic,
    with_usb_planar,
)
from tools.scanlab.capture_pcap import (
    CaptureAnalysis,
    analyze_usbpcap,
    decode_all_capture_passes,
    format_capture_usb_log_lines,
    model_for_capture_decode,
    motor_register_diff,
)
from tools.scanlab.forensic_tab import ForensicTabPage
from tools.scanlab.widgets import ImageTabPage, MeControls, MeMode
from tools.scanlab.worker import ScanRequest, ScanWorker


class ScanLabWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("pyopticfilm Scan Lab")
        self.resize(1100, 720)

        self._targets: list[LabTarget] = []
        self._usb_sections: dict[str, int] = {}
        self._capture: CaptureAnalysis | None = None
        self._last_scan: ScanImage | None = None
        self._me_debug = None
        self._loaded_me_short = None
        self._loaded_me_long = None
        #: Plane per MeScanDebug.brackets entry, indexed by bracket_selector.
        self._bracket_planes: list = []
        self._bracket_dpi: int | None = None
        self._last_prescan_dpi: int | None = None
        self._pending_crop_meta: dict | None = None
        self._pending_crop_norm: tuple[float, float, float, float] | None = None
        self._thread = QThread(self)
        self._worker = ScanWorker()
        self._worker.moveToThread(self._thread)
        self._thread.start()
        # Must run after moveToThread()+start() - see ScanWorker.__init__'s
        # note on why connecting these earlier silently breaks queued
        # dispatch even with an explicit connection type.
        self._worker.connect_request_signals()

        root = QWidget(self)
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)

        controls = QWidget()
        form = QVBoxLayout(controls)
        controls.setFixedWidth(280)

        form.addWidget(QLabel("Device"))
        self.device = QComboBox()
        form.addWidget(self.device)

        self.run_mock = QCheckBox("Run against MOCK")
        self.run_mock.setChecked(True)
        self.run_mock.toggled.connect(self._refresh_banner)
        form.addWidget(self.run_mock)

        self.override_hw_gate = QCheckBox("Override safety HW gate")
        self.override_hw_gate.setChecked(False)
        self.override_hw_gate.toggled.connect(self._on_override_hw_gate)
        form.addWidget(self.override_hw_gate)

        self.apply_calib = QCheckBox("Apply calib")
        self.apply_calib.setChecked(True)
        self.apply_calib.setToolTip(
            "ASIC shading before colour scans. First prescan/scan at each PPI "
            "measures once; results are cached in ~/.cache/pyopticfilm/calib_v2.json."
        )
        form.addWidget(self.apply_calib)

        self.btn_clear_calib = QPushButton("Clear calib cache")
        self.btn_clear_calib.clicked.connect(self._on_clear_calib_cache)
        form.addWidget(self.btn_clear_calib)

        self.usb_planar = QCheckBox("USB planar RGB")
        self.usb_planar.setChecked(False)
        self.usb_planar.toggled.connect(self._on_usb_planar)
        form.addWidget(self.usb_planar)

        self.quiet_usb_pace = QCheckBox("Adaptive quiet drain")
        self.quiet_usb_pace.setChecked(True)
        self.quiet_usb_pace.setToolTip(
            "Rate-limit host bulk reads to the ASIC line rate (no fixed pause "
            "before each chunk). Keeps motor creep continuous at 7200 dpi; "
            "uncheck for fastest drain (louder)."
        )
        form.addWidget(self.quiet_usb_pace)

        self.slow_image_slope = QCheckBox("Slow image slope")
        self.slow_image_slope.setChecked(False)
        self.slow_image_slope.setToolTip(
            "Upload the shading/slow motor ramp for the image pass (acoustic probe). "
            "Feeds still use the fast ramp."
        )
        form.addWidget(self.slow_image_slope)

        self.disable_gl128_prime = QCheckBox("Disable priming pass (debug)")
        self.disable_gl128_prime.setChecked(False)
        self.disable_gl128_prime.setToolTip(
            "GL128 only. Off (default): use the model's priming default "
            "(currently off for 8200i SE and 8100 V2). On: skip the discarded "
            "first-scan AGOHOME-park pass — for debugging/testing only. "
            "Explicit priming is still available via Scanner.scan(gl128_prime=True)."
        )
        form.addWidget(self.disable_gl128_prime)

        refresh = QPushButton("Refresh devices")
        refresh.clicked.connect(self.reload_devices)
        form.addWidget(refresh)

        form.addWidget(QLabel("PPI"))
        self.ppi = QComboBox()
        form.addWidget(self.ppi)

        self.ir_pass = QCheckBox("IR pass (second scan)")
        form.addWidget(self.ir_pass)

        self.me_controls = MeControls()
        form.addWidget(self.me_controls)
        self.me_controls.changed.connect(self._on_me_controls_changed)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("color: #c9a227; font-weight: 600;")
        form.addWidget(self.banner)

        self.btn_prescan = QPushButton("Prescan")
        self.btn_scan = QPushButton("Scan")
        self.btn_cancel = QPushButton("Cancel")
        self.btn_open_capture = QPushButton("Open capture…")
        self.btn_cancel.setEnabled(False)
        self.btn_prescan.clicked.connect(self._on_prescan)
        self.btn_scan.clicked.connect(self._on_scan)
        self.btn_cancel.clicked.connect(self._worker.cancel)
        self.btn_open_capture.clicked.connect(self._on_open_capture)
        form.addWidget(self.btn_prescan)
        form.addWidget(self.btn_scan)
        form.addWidget(self.btn_cancel)
        form.addWidget(self.btn_open_capture)
        form.addStretch(1)

        tabs = QTabWidget()
        self.prescan_view = ImageTabPage(default_stem="prescan", allow_crop=True)
        self.scan_view = ImageTabPage(default_stem="color_short", allow_load=True)
        self.me_long_view = ImageTabPage(default_stem="color_long", allow_load=True)
        # Bracket selector shown above the "Color long"/"Brackets" tab —
        # surfaces MeScanDebug.brackets (every captured exposure, not just
        # the top one) for N-Exposure scans instead of only ever showing
        # the last bracket. Hidden (1 item) for n_brackets==2 / no debug.
        self.bracket_selector_row = QWidget()
        bracket_row_layout = QHBoxLayout(self.bracket_selector_row)
        bracket_row_layout.setContentsMargins(4, 4, 4, 0)
        bracket_row_layout.addWidget(QLabel("Bracket"))
        self.bracket_selector = QComboBox()
        self.bracket_selector.currentIndexChanged.connect(self._on_bracket_selected)
        bracket_row_layout.addWidget(self.bracket_selector, 1)
        self.bracket_selector_row.setVisible(False)
        self.me_long_container = QWidget()
        me_long_layout = QVBoxLayout(self.me_long_container)
        me_long_layout.setContentsMargins(0, 0, 0, 0)
        me_long_layout.addWidget(self.bracket_selector_row)
        me_long_layout.addWidget(self.me_long_view)
        self.merged_view = ImageTabPage(default_stem="merged")
        self.ir_view = ImageTabPage(default_stem="ir")
        self.capture_diff = QPlainTextEdit()
        self.capture_diff.setReadOnly(True)
        self.capture_diff.setPlaceholderText(
            "Open a USBPcap .pcap / .pcapng to decode the image bulk and compare "
            "FEEDL / LINCNT / DPISET to Lab geometry…"
        )
        self.usb_log = QPlainTextEdit()
        self.usb_log.setReadOnly(True)
        self.usb_log.setPlaceholderText("USB transactions appear here…")
        jump_row = QHBoxLayout()
        jump_row.addWidget(QLabel("Jump"))
        self.btn_jump_prescan = QPushButton("Prescan")
        self.btn_jump_scan = QPushButton("Scan")
        self.btn_jump_ir = QPushButton("IR")
        self.btn_jump_capture = QPushButton("Capture")
        self.btn_jump_prescan.clicked.connect(lambda: self._jump_usb_section("PRESCAN"))
        self.btn_jump_scan.clicked.connect(lambda: self._jump_usb_section("SCAN"))
        self.btn_jump_ir.clicked.connect(lambda: self._jump_usb_section("IR"))
        self.btn_jump_capture.clicked.connect(lambda: self._jump_usb_section("CAPTURE"))
        jump_row.addWidget(self.btn_jump_prescan)
        jump_row.addWidget(self.btn_jump_scan)
        jump_row.addWidget(self.btn_jump_ir)
        jump_row.addWidget(self.btn_jump_capture)
        jump_row.addStretch(1)
        clear_log = QPushButton("Clear USB log")
        clear_log.clicked.connect(self._clear_usb_log)
        jump_row.addWidget(clear_log)
        log_page = QWidget()
        log_layout = QVBoxLayout(log_page)
        log_layout.addLayout(jump_row)
        log_layout.addWidget(self.usb_log)
        self._update_usb_jump_buttons()

        tabs.addTab(self.prescan_view, "Prescan")
        tabs.addTab(self.scan_view, "Color short")
        tabs.addTab(self.me_long_container, "Color long")
        tabs.addTab(self.merged_view, "Merged")
        tabs.addTab(self.ir_view, "IR")
        tabs.addTab(self.capture_diff, "Capture")
        tabs.addTab(log_page, "USB log")
        self.forensic_tab = ForensicTabPage()
        tabs.addTab(self.forensic_tab, "Forensic")
        self.tabs = tabs

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(controls)
        splitter.addWidget(tabs)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.statusBar().addPermanentWidget(self.progress, 1)

        self.device.currentIndexChanged.connect(self._on_device_changed)
        self.ppi.currentIndexChanged.connect(self._on_ppi_changed)

        self._worker.progress.connect(self._on_progress)
        self._worker.status_changed.connect(self._on_scan_status)
        self._worker.usb_line.connect(self._append_usb)
        self._worker.banner.connect(self.banner.setText)
        self._worker.prescan_ready.connect(self._on_prescan_ready)
        self._worker.scan_ready.connect(self._on_scan_ready)
        self._worker.me_debug_ready.connect(self._on_me_debug_ready)
        self._worker.failed.connect(self._on_failed)
        self._worker.busy_changed.connect(self._on_busy)
        self._worker.calib_cleared.connect(self._on_calib_cleared)

        self._worker.forensic_line.connect(self.forensic_tab.append_timeline_line)
        self._worker.forensic_status_ready.connect(self.forensic_tab.set_status)
        self._worker.forensic_register_result.connect(self.forensic_tab.set_register_result)
        self._worker.forensic_run_saved.connect(self.forensic_tab.on_recording_saved)
        self._worker.forensic_error.connect(self.forensic_tab.show_error)
        self._worker.forensic_anomaly.connect(self.forensic_tab.on_anomaly)
        self._worker.forensic_run_started.connect(self.forensic_tab.on_run_started)
        self._worker.forensic_connected.connect(self.forensic_tab.set_connected)
        self._worker.forensic_timeline_event.connect(self.forensic_tab.on_timeline_event)
        self._worker.forensic_timeline_marker.connect(self.forensic_tab.on_timeline_marker)
        self.forensic_tab.connect_clicked.connect(self._on_forensic_connect)
        self.forensic_tab.disconnect_clicked.connect(self._on_forensic_disconnect)
        self.forensic_tab.poll_toggled.connect(self._worker.request_forensic_poll.emit)
        self.forensic_tab.register_read_requested.connect(
            self._worker.request_forensic_register_read.emit
        )
        self.forensic_tab.register_write_requested.connect(
            self._worker.request_forensic_register_write.emit
        )
        self.forensic_tab.recording_toggled.connect(
            self._worker.request_forensic_recording.emit
        )

        self.scan_view.load_clicked.connect(lambda: self._load_me_plane("short"))
        self.me_long_view.load_clicked.connect(lambda: self._load_me_plane("long"))

        self.reload_devices()
        self._update_me_tabs_visible()

    def closeEvent(self, event) -> None:
        self._worker.cancel()
        self._worker.close_scanner()
        self._thread.quit()
        self._thread.wait(3000)
        super().closeEvent(event)

    def reload_devices(self) -> None:
        self._targets = list_lab_targets()
        self.device.blockSignals(True)
        self.device.clear()
        for target in self._targets:
            self.device.addItem(target.label)
        self.device.blockSignals(False)
        if self._targets:
            self._on_device_changed(0)

    def _current_target(self) -> LabTarget | None:
        idx = self.device.currentIndex()
        if idx < 0 or idx >= len(self._targets):
            return None
        return self._targets[idx]

    def _on_device_changed(self, _index: int) -> None:
        target = self._current_target()
        if target is None:
            return
        self.ppi.blockSignals(True)
        self.ppi.clear()
        for dpi in target.model.resolutions_dpi:
            self.ppi.addItem(str(dpi), dpi)
        preferred = 1800 if 1800 in target.model.resolutions_dpi else target.model.resolutions_dpi[0]
        self.ppi.setCurrentIndex(list(target.model.resolutions_dpi).index(preferred))
        self.ppi.blockSignals(False)
        self.ir_pass.setEnabled(bool(getattr(target.model, "supports_infrared", False)))
        if not self.ir_pass.isEnabled():
            self.ir_pass.setChecked(False)
        is_gl128 = getattr(target.model, "asic", "") == "GL128"
        self.me_controls.set_gl128_enabled(is_gl128)
        self._update_me_tabs_visible()
        self._refresh_banner()
        self.prescan_view.clear_crop()
        if self._capture is not None:
            self._decode_loaded_capture()

    def _on_ppi_changed(self, _index: int) -> None:
        if self._capture is not None:
            self._decode_loaded_capture()

    def _refresh_merged_preview(self) -> None:
        """Re-merge loaded bracket TIFFs for the Merged tab (offline only)."""
        short = self._loaded_me_short
        long = self._loaded_me_long
        if short is None or long is None:
            self.merged_view.set_caption("")
            return
        try:
            from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
            from pyopticfilm.scan.exposure_merge import merge_exposures_result
            from pyopticfilm.scan.pipeline import ImagePipeline

            exp_short, exp_long = self._default_exposure_pair()
            result = merge_exposures_result(
                short,
                long,
                exposure_short=exp_short,
                exposure_long=exp_long,
            )
            pipe = ImagePipeline(MODEL_8200I_SE)
            rgb = pipe.expose_film_base(
                result.rgb, source="me preview", preserve_headroom=True
            )
            rgb = pipe.clamp_border_highlights(rgb)
            dpi = self._last_scan.dpi if self._last_scan else self._default_dpi()
            self.merged_view.set_rgb(rgb, dpi=dpi, auto_level=False)
            if result.fusion_stats is not None:
                self.merged_view.set_caption(
                    self._format_fusion_caption(result.fusion_stats)
                )
            else:
                self.merged_view.set_caption("Merge: SNR / IVW (short scale + makeup)")
        except Exception:  # noqa: BLE001
            self.merged_view.set_rgb(None)
            self.merged_view.set_caption("")

    @staticmethod
    def _format_fusion_caption(stats) -> str:
        # Absolute IVW weights are tiny (÷ variance); ratio matters.
        ws = float(stats.mean_short_weight)
        wl = float(stats.mean_long_weight)
        ratio_w = wl / max(ws, 1e-30)
        msg = (
            f"SNR / IVW — short-scale output; "
            f"w_long/w_short={ratio_w:.2f} "
            f"(short {ws:.4g}, long {wl:.4g})"
        )
        if stats.zero_weight_fraction > 0:
            msg += f"; {stats.zero_weight_fraction:.2%} both-zero (black)"
        mean_res = getattr(stats, "mean_residual_confidence", None)
        if mean_res is not None:
            msg += f"; residual conf {mean_res:.2f}"
        ratio = getattr(stats, "exposure_ratio_used", None)
        if ratio is not None:
            msg += f"; r={ratio:.3f}"
        return msg

    @staticmethod
    def _format_align_shift(shift: tuple[float, float] | None) -> str:
        if shift is None:
            return ""
        dx, dy = float(shift[0]), float(shift[1])
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return "align (0, 0)"
        return f"align dx={dx:.2f} dy={dy:.2f}"

    def _fusion_stats_message(self, debug) -> str:
        stats = getattr(debug, "fusion_stats", None)
        if stats is None:
            return ""
        msg = (
            f"; SNR/IVW w_short={stats.mean_short_weight:.4g} "
            f"w_long={stats.mean_long_weight:.4g}"
        )
        if stats.zero_weight_fraction:
            msg += f" both-zero={stats.zero_weight_fraction:.2%}"
        align = getattr(debug, "align_shift_long", None)
        if align is not None:
            msg += f"; long {self._format_align_shift(align)}"
        ir_align = getattr(debug, "align_shift_ir", None)
        if ir_align is not None:
            msg += f"; IR {self._format_align_shift(ir_align)}"
        return msg

    def _on_me_controls_changed(self) -> None:
        """MeControls owns its own enable-cascade (mode -> brackets/manual
        override fields) internally; this only needs to react to whatever
        else in the window depends on "what ME mode is selected"."""
        self._update_me_tabs_visible()

    def _default_dpi(self) -> int:
        data = self.ppi.currentData()
        if data is not None:
            return int(data)
        return 1800

    def _default_exposure_pair(self) -> tuple[int, int]:
        target = self._current_target()
        if target is not None:
            model = target.model
            short = int(getattr(model, "exposure_short", 14000))
            long = int(getattr(model, "exposure_long", short * 3))
            return short, long
        return 14000, 42000

    def _me_planes_ready(self) -> bool:
        if self._me_debug is not None:
            return True
        return self._loaded_me_short is not None and self._loaded_me_long is not None

    def _me_short_plane(self):
        if self._me_debug is not None:
            return self._me_debug.rgb_short
        return self._loaded_me_short

    def _me_long_plane(self):
        if self._me_debug is not None:
            return self._me_debug.rgb_long
        return self._loaded_me_long

    def _load_me_plane(self, which: str) -> None:
        from pyopticfilm.exceptions import PlustekError
        from pyopticfilm.image import load_rgb16_tiff

        title = "Load color short TIFF" if which == "short" else "Load color long TIFF"
        path, _ = QFileDialog.getOpenFileName(
            self,
            title,
            "",
            "TIFF (*.tif *.tiff);;All files (*.*)",
        )
        if not path:
            return
        try:
            rgb, dpi = load_rgb16_tiff(path, default_dpi=self._default_dpi())
        except (PlustekError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "Load TIFF", str(exc))
            return

        self._me_debug = None
        self._clear_bracket_selector()
        short = self._loaded_me_short
        long = self._loaded_me_long

        if which == "short":
            if long is not None and long.shape != rgb.shape:
                QMessageBox.warning(
                    self,
                    "Load TIFF",
                    f"Shape mismatch: short {rgb.shape[:2]} vs long {long.shape[:2]}",
                )
                return
            self._loaded_me_short = rgb
            self._loaded_me_long = long
            self.scan_view.set_rgb(rgb, dpi=dpi, auto_level=False)
        else:
            if short is not None and short.shape != rgb.shape:
                QMessageBox.warning(
                    self,
                    "Load TIFF",
                    f"Shape mismatch: long {rgb.shape[:2]} vs short {short.shape[:2]}",
                )
                return
            self._loaded_me_long = rgb
            self._loaded_me_short = short
            self.me_long_view.set_rgb(rgb, dpi=dpi, auto_level=False)

        short = self._loaded_me_short
        long = self._loaded_me_long
        if short is None and long is None:
            return
        if self._last_scan is None:
            primary = short if short is not None else long
            self._last_scan = ScanImage(rgb=primary, dpi=dpi)
        self._update_me_tabs_visible()
        if self._me_planes_ready() and self._me_debug is None:
            self._refresh_merged_preview()
            self.tabs.setCurrentWidget(self.merged_view)
        # Status mean is on the raw uint16 (not the 8-bit preview).
        mean_y = float(rgb.astype("float64").mean() / 65535.0)
        self.statusBar().showMessage(
            f"Loaded {Path(path).name} — {rgb.shape[1]}×{rgb.shape[0]} @ {dpi} dpi "
            f"(mean Y={mean_y:.3f}, linear preview)"
        )

    def _update_me_tabs_visible(self) -> None:
        has_me_result = self._me_planes_ready()
        me = self.me_controls.me_pass_enabled() or has_me_result
        idx_long = self.tabs.indexOf(self.me_long_container)
        idx_merged = self.tabs.indexOf(self.merged_view)
        if idx_long >= 0:
            self.tabs.setTabVisible(idx_long, me)
        if idx_merged >= 0:
            self.tabs.setTabVisible(idx_merged, me)
        scan_label = "Color short" if me else "Scan"
        idx_scan = self.tabs.indexOf(self.scan_view)
        if idx_scan >= 0:
            self.tabs.setTabText(idx_scan, scan_label)

    def _on_override_hw_gate(self, checked: bool) -> None:
        if checked:
            reply = QMessageBox.warning(
                self,
                "Override safety HW gate",
                "This unlocks unverified scan/home/park pipelines against real "
                "hardware. Motors and the lamp can move on models that are not "
                "hardware-validated (scan_ready stays False).\n\n"
                "Keep a hand near the power switch. Continue only if you intend "
                "to run bring-up on a connected OpticFilm.",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Ok:
                self.override_hw_gate.blockSignals(True)
                self.override_hw_gate.setChecked(False)
                self.override_hw_gate.blockSignals(False)
                return
            # Extra calib motor moves are risky on first bring-up.
            self.apply_calib.setChecked(False)
        else:
            self._worker.close_scanner()
        self._refresh_banner()

    def _on_usb_planar(self, _checked: bool) -> None:
        self._worker.close_scanner()
        self._refresh_banner()
        if self._capture is not None:
            self._decode_loaded_capture()

    def _on_open_capture(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open USB capture",
            "",
            "USBPcap (*.pcapng *.pcap);;All files (*.*)",
        )
        if not path:
            return
        try:
            self._capture = analyze_usbpcap(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Scan lab", f"Failed to parse capture:\n{exc}")
            return
        self._populate_usb_log_from_capture(self._capture)
        self._decode_loaded_capture()

    def _populate_usb_log_from_capture(self, analysis: CaptureAnalysis) -> None:
        """Replace the USB log with a collapsed transcript of the capture."""
        self._clear_usb_log()
        log_lines = format_capture_usb_log_lines(analysis)
        # One setPlainText is much faster than thousands of appendPlainText calls.
        self.usb_log.setPlainText("\n".join(log_lines))
        self._usb_sections.clear()
        # Re-scan for section dividers (CAPTURE / any others).
        doc = self.usb_log.document()
        block = doc.begin()
        while block.isValid():
            key = usb_log_section_key(block.text())
            if key is not None:
                self._usb_sections[key] = block.position()
            block = block.next()
        self._update_usb_jump_buttons()

    def _decode_loaded_capture(self) -> None:
        analysis = self._capture
        target = self._current_target()
        if analysis is None or target is None:
            return
        dpi = int(self.ppi.currentData() or target.model.resolutions_dpi[0])
        planar = self.usb_planar.isChecked()
        decode_model = model_for_capture_decode(analysis, target.model)
        lines = [
            f"Capture: {analysis.path.name}",
            f"Lab target: {target.model.model} ({target.model.asic})",
            f"Decode model: {decode_model.model} ({decode_model.asic})",
            (
                f"Packets: {len(analysis.packets)}  bulk INs: {len(analysis.bulk_ins)}  "
                f"register writes: {len(analysis.register_writes)}"
            ),
            "",
            *motor_register_diff(
                decode_model,
                analysis,
                dpi=dpi,
                crop_norm=self.prescan_view.crop_norm,
            ),
            "",
        ]
        decoded_ok = False
        decoded = None
        try:
            # Decode uses capture DPISET for width; Lab PPI is only for the diff above.
            decoded = decode_all_capture_passes(
                decode_model,
                analysis,
                planar=planar,
            )
            if decoded.prescan is not None:
                rgb, geo = decoded.prescan
                self.prescan_view.set_rgb(rgb, dpi=geo.resolution, auto_level=True)
            else:
                self.prescan_view.set_rgb(None)
            if decoded.color is not None:
                rgb, geo = decoded.color
                self.scan_view.set_rgb(rgb, dpi=geo.resolution, auto_level=True)
            else:
                self.scan_view.set_rgb(None)
            if decoded.color_me is not None:
                rgb_me, geo = decoded.color_me
                self.me_long_view.set_rgb(rgb_me, dpi=geo.resolution, auto_level=True)
                if self.me_controls.mode() == MeMode.OFF:
                    self.me_controls.set_mode(MeMode.DYNAMIC)
                self._update_me_tabs_visible()
            else:
                self.me_long_view.set_rgb(None)
            if decoded.color is not None and decoded.color_me is not None:
                try:
                    from pyopticfilm.scan.exposure_merge import merge_exposures_result

                    short_rgb, geo = decoded.color
                    long_rgb, _ = decoded.color_me
                    merged = merge_exposures_result(short_rgb, long_rgb)
                    from pyopticfilm.device.model_8200i_se import MODEL_8200I_SE
                    from pyopticfilm.scan.pipeline import ImagePipeline

                    pipe = ImagePipeline(MODEL_8200I_SE)
                    rgb = pipe.expose_film_base(
                        merged.rgb, source="capture me preview", preserve_headroom=True
                    )
                    rgb = pipe.clamp_border_highlights(rgb)
                    self.merged_view.set_rgb(rgb, dpi=geo.resolution, auto_level=False)
                    if merged.fusion_stats is not None:
                        self.merged_view.set_caption(
                            self._format_fusion_caption(merged.fusion_stats)
                        )
                    self._update_me_tabs_visible()
                    lines.append("Merged tab: capture preview (SNR / IVW)")
                except Exception as merge_exc:  # noqa: BLE001
                    lines.append(f"Merged preview failed: {merge_exc}")
                    self.merged_view.set_rgb(None)
            else:
                self.merged_view.set_rgb(None)
            if decoded.ir is not None:
                ir_rgb, geo = decoded.ir
                plane = ir_rgb[:, :, 1]
                self.ir_view.set_gray(plane, dpi=geo.resolution)
            else:
                self.ir_view.set_rgb(None)

            lines.extend(decoded.log_lines)
            if decoded.color is not None:
                rgb, geo = decoded.color
                lines.append(
                    f"Scan tab: {rgb.shape[1]}×{rgb.shape[0]} @ {geo.resolution} dpi "
                    f"ld_shift=({geo.shift_r},{geo.shift_g},{geo.shift_b})"
                )
            if decoded.prescan is not None:
                rgb, geo = decoded.prescan
                lines.append(
                    f"Prescan tab: {rgb.shape[1]}×{rgb.shape[0]} @ {geo.resolution} dpi"
                )
            if decoded.ir is not None:
                rgb, geo = decoded.ir
                lines.append(
                    f"IR tab: {rgb.shape[1]}×{rgb.shape[0]} @ {geo.resolution} dpi"
                )
            if decoded.color_me is not None:
                rgb, geo = decoded.color_me
                lines.append(
                    f"Color long tab: {rgb.shape[1]}×{rgb.shape[0]} @ {geo.resolution} dpi"
                )
            lines.append(
                "Decode ignores the Lab PPI spinner (uses capture DPISET). "
                "Toggle USB planar RGB to re-decode without reopening the file."
            )
            if decode_model.asic != target.model.asic:
                lines.append(
                    f"Note: capture looks like {decode_model.asic}; "
                    f"used {decode_model.model} tables instead of {target.model.model}."
                )
            decoded_ok = decoded.prescan is not None or decoded.color is not None
            if decoded.color is not None:
                rgb, _ = decoded.color
                self.statusBar().showMessage(
                    f"Capture decode scan {rgb.shape[1]}×{rgb.shape[0]} planar={planar}"
                )
            elif decoded.prescan is not None:
                rgb, _ = decoded.prescan
                self.statusBar().showMessage(
                    f"Capture decode prescan {rgb.shape[1]}×{rgb.shape[0]} planar={planar}"
                )
        except Exception as exc:  # noqa: BLE001
            lines.append(f"Decode failed: {exc}")
            lines.append("Register diff above may still help with FEEDL/LINCNT.")
            self.statusBar().showMessage(f"Capture decode failed: {exc}")
            QMessageBox.warning(self, "Scan lab", f"Could not decode image bulk:\n{exc}")
        self.capture_diff.setPlainText("\n".join(lines))
        if decoded_ok:
            if decoded.color is not None:
                self.tabs.setCurrentWidget(self.scan_view)
            elif decoded.prescan is not None:
                self.tabs.setCurrentWidget(self.prescan_view)
        else:
            self.tabs.setCurrentWidget(self.capture_diff)

    def _refresh_banner(self) -> None:
        target = self._current_target()
        if target is None:
            self.banner.setText("")
            return
        mock = self.run_mock.isChecked()
        if not mock and not target.device_id:
            self.banner.setText(
                f"No scanner connected for {target.model.model}. "
                "Plug it in or keep MOCK enabled."
            )
            return
        resolved = with_motor_acoustic(
            with_usb_planar(
                with_hw_override(
                    with_mock_mode(target, mock),
                    self.override_hw_gate.isChecked(),
                ),
                self.usb_planar.isChecked(),
            ),
            quiet_usb_pace=self.quiet_usb_pace.isChecked(),
            slow_image_slope=self.slow_image_slope.isChecked(),
        )
        self.banner.setText(device_banner(resolved))

    def _resolved_target(self, *, warn_if_missing: bool = True) -> LabTarget | None:
        target = self._current_target()
        if target is None:
            return None
        mock = self.run_mock.isChecked()
        if not mock and not target.device_id:
            if warn_if_missing:
                QMessageBox.warning(
                    self,
                    "Scan lab",
                    "No matching scanner is connected. Plug in the device or keep MOCK enabled.",
                )
            return None
        return with_motor_acoustic(
            with_usb_planar(
                with_hw_override(
                    with_mock_mode(target, mock),
                    self.override_hw_gate.isChecked(),
                ),
                self.usb_planar.isChecked(),
            ),
            quiet_usb_pace=self.quiet_usb_pace.isChecked(),
            slow_image_slope=self.slow_image_slope.isChecked(),
        )

    def _gl128_prime_arg(self) -> bool | None:
        """False when the debug box is on; otherwise leave the model default."""
        if self.disable_gl128_prime.isChecked():
            return False
        return None

    def _clear_scan_tabs(self) -> None:
        """Drop prior prescan/scan results so a new Prescan starts a fresh session."""
        self._last_scan = None
        self._me_debug = None
        self._loaded_me_short = None
        self._loaded_me_long = None
        self._last_prescan_dpi = None
        self.prescan_view.set_rgb(None)
        self.prescan_view.clear_crop()
        self.prescan_view.set_caption("")
        self.scan_view.set_rgb(None)
        self.scan_view.set_caption("")
        self.me_long_view.set_rgb(None)
        self.me_long_view.set_caption("")
        self.merged_view.set_rgb(None)
        self.merged_view.set_caption("")
        self.ir_view.set_rgb(None)
        self.ir_view.set_caption("")
        self._update_me_tabs_visible()
        self.tabs.setCurrentWidget(self.prescan_view)

    def _on_prescan(self) -> None:
        target = self._resolved_target()
        if target is None:
            return
        self._clear_usb_log()
        self._clear_scan_tabs()
        self.statusBar().showMessage("Prescanning…")
        self.forensic_tab.start_or_restart_auto_record()
        self._worker.request_forensic_mark_phase.emit(
            "BUTTON: Prescan clicked",
            {
                "model": target.model.model,
                "mock": target.mock,
                "apply_calib": self.apply_calib.isChecked(),
                "gl128_prime": not self.disable_gl128_prime.isChecked(),
            },
        )
        self._worker.request_prescan.emit(
            target,
            self.apply_calib.isChecked(),
            self._gl128_prime_arg(),
        )

    def _on_scan(self) -> None:
        target = self._resolved_target()
        if target is None:
            return
        dpi = int(self.ppi.currentData())
        crop = self.prescan_view.crop_norm
        if (
            self.override_hw_gate.isChecked()
            and not target.mock
            and lab_scan_needs_motor_warning(target.model, dpi=dpi, crop_norm=crop)
        ):
            frac = nonse_safe_y_fraction(target.model)
            reply = QMessageBox.warning(
                self,
                "High-PPI Scan",
                f"{dpi} dpi without a short crop can grind the motor on "
                f"unverified models.\n\n"
                f"Scan Lab will clamp travel to about {frac:.0%} of the TA "
                f"height (~{frac * float(getattr(target.model, 'y_size_ta_mm', 25)):.1f} mm). "
                "Prefer a small rubber-band crop and lower PPI for bring-up.\n\n"
                "Continue with the clamped short window?",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Ok:
                return
        try:
            manual = self.me_controls.manual_exposure_kwargs()
        except ValueError:
            QMessageBox.warning(
                self,
                "Manual exposure",
                "Manual exposure overrides must be empty or a whole number "
                f"between 1 and {MAX_EXPOSURE_REGISTER}.",
            )
            return
        self._pending_crop_meta = None
        self._pending_crop_norm = crop
        if crop is not None:
            self._pending_crop_meta = lab_crop_scan_meta(
                target.model, dpi=dpi, crop_norm=crop
            )
        self.forensic_tab.start_or_restart_auto_record()
        self._worker.request_forensic_mark_phase.emit(
            "BUTTON: Scan clicked",
            {
                "model": target.model.model,
                "mock": target.mock,
                "dpi": dpi,
                "crop": list(crop) if crop is not None else None,
                "ir_pass": self.ir_pass.isChecked(),
                "me_pass": self.me_pass.isChecked(),
                "apply_calib": self.apply_calib.isChecked(),
                "gl128_prime": not self.disable_gl128_prime.isChecked(),
                "override_hw_gate": self.override_hw_gate.isChecked(),
            },
        )
        scan_kw = lab_scan_kwargs(target.model, dpi=dpi, kind="scan", crop_norm=crop)
        self._worker.request_scan.emit(
            ScanRequest(
                target=target,
                dpi=dpi,
                ir_pass=self.ir_pass.isChecked(),
                me_pass=self.me_controls.me_pass_enabled(),
                apply_calib=self.apply_calib.isChecked(),
                # None (not "adaptive") for Dynamic/N-Exposure: lets
                # n_brackets > 2 defer to the model's own default (8100 V2:
                # fixed; 8200i SE: adaptive) instead of forcing "adaptive"
                # regardless of model. Behaviorally identical to explicit
                # "adaptive" at n_brackets == 2, since that model default is
                # always "adaptive" there too.
                me_exposure_mode=self.me_controls.me_exposure_mode_kwarg(),
                gl128_prime=self._gl128_prime_arg(),
                crop_norm=crop,
                scan_kw=scan_kw,
                n_brackets=self.me_controls.n_brackets_value(),
                **manual,
            )
        )

    def _on_progress(self, value: float) -> None:
        self.progress.setValue(int(max(0.0, min(1.0, value)) * 1000))

    def _on_scan_status(self, status: str) -> None:
        if status == "priming":
            self.statusBar().showMessage("Priming scanner…")
        elif status == "prime_skipped":
            self.statusBar().showMessage("Priming skipped (debug)…")
        elif status == "scanning":
            self.statusBar().showMessage("Scanning…")

    def _append_usb(self, line: str) -> None:
        self.usb_log.appendPlainText(line)
        key = usb_log_section_key(line)
        if key is None:
            return
        self._usb_sections[key] = self.usb_log.document().lastBlock().position()
        self._update_usb_jump_buttons()

    def _clear_usb_log(self) -> None:
        self.usb_log.clear()
        self._clear_usb_sections()

    def _clear_usb_sections(self) -> None:
        self._usb_sections.clear()
        self._update_usb_jump_buttons()

    def _update_usb_jump_buttons(self) -> None:
        self.btn_jump_prescan.setEnabled("PRESCAN" in self._usb_sections)
        self.btn_jump_scan.setEnabled("SCAN" in self._usb_sections)
        self.btn_jump_ir.setEnabled("IR" in self._usb_sections)
        self.btn_jump_capture.setEnabled("CAPTURE" in self._usb_sections)

    def _jump_usb_section(self, key: str) -> None:
        pos = self._usb_sections.get(key)
        if pos is None:
            return
        cursor = self.usb_log.textCursor()
        cursor.setPosition(pos)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        self.usb_log.setTextCursor(cursor)
        self.usb_log.ensureCursorVisible()
        bar = self.usb_log.verticalScrollBar()
        bar.setValue(bar.value() + self.usb_log.cursorRect().top())
        self.tabs.setCurrentWidget(self.usb_log.parentWidget())

    def _on_prescan_ready(self, image: ScanImage) -> None:
        self._last_prescan_dpi = image.dpi
        self.prescan_view.set_rgb(image.rgb, dpi=image.dpi)
        self.tabs.setCurrentWidget(self.prescan_view)
        self.statusBar().showMessage(
            f"Prescan {image.rgb.shape[1]}×{image.rgb.shape[0]} @ {image.dpi} dpi — drag a crop"
        )
        self._worker.request_forensic_mark_phase.emit(
            "Prescan received",
            {"shape": list(image.rgb.shape), "dpi": image.dpi},
        )

    def _on_me_debug_ready(self, debug) -> None:
        self._me_debug = debug
        if debug is None:
            return
        proposed = getattr(debug, "exposure_proposed", None)
        reason = getattr(debug, "exposure_reason", None) or ""
        line = (
            f"ME long exposure: short={debug.exposure_short} "
            f"selected={debug.exposure_long}"
        )
        if proposed is not None:
            line += f" proposed={proposed}"
        if reason:
            line += f" ({reason})"
        self._append_usb(line)
        self.statusBar().showMessage(line)
        # Per-bracket exposure + align_shift (populated for every ME scan,
        # not just N-Exposure — see MeScanDebug.brackets). At n_brackets==2
        # this duplicates the short/long line above, so only log it once
        # there is a genuinely intermediate bracket to see.
        brackets = getattr(debug, "brackets", None)
        if brackets and len(brackets) > 2:
            self._append_usb(f"ME brackets ({len(brackets)}):")
            for i, b in enumerate(brackets):
                shift = self._format_align_shift(b.align_shift) if b.align_shift else "ref"
                self._append_usb(f"  [{i}] exposure={b.exposure} {shift}")

    def _populate_bracket_selector(self, debug, *, dpi: int) -> None:
        """Fill the "Color long" tab's bracket dropdown from
        MeScanDebug.brackets so any captured exposure is viewable, not just
        the top one — surfaces data every ME scan already collects
        (session_gl128.py populates it for n_brackets==2 as well) but the
        UI previously never read."""
        self._bracket_dpi = dpi
        brackets = getattr(debug, "brackets", None)
        self.bracket_selector.blockSignals(True)
        self.bracket_selector.clear()
        if brackets:
            for i, b in enumerate(brackets):
                role = " (short)" if i == 0 else " (top)" if i == len(brackets) - 1 else ""
                self.bracket_selector.addItem(f"[{i}] {b.exposure}{role}", i)
            self._bracket_planes = [b.rgb for b in brackets]
        else:
            # Legacy/no-brackets debug: fall back to the single long plane.
            self.bracket_selector.addItem(f"long {debug.exposure_long}", 0)
            self._bracket_planes = [debug.rgb_long]
        self.bracket_selector.blockSignals(False)
        self.bracket_selector_row.setVisible(len(self._bracket_planes) > 2)
        self.bracket_selector.setCurrentIndex(len(self._bracket_planes) - 1)
        self._on_bracket_selected(len(self._bracket_planes) - 1)

    def _clear_bracket_selector(self) -> None:
        self._bracket_planes = []
        self.bracket_selector.blockSignals(True)
        self.bracket_selector.clear()
        self.bracket_selector.blockSignals(False)
        self.bracket_selector_row.setVisible(False)
        self.me_long_view.set_rgb(None)

    def _on_bracket_selected(self, index: int) -> None:
        if not self._bracket_planes or not (0 <= index < len(self._bracket_planes)):
            return
        self.me_long_view.set_rgb(
            self._bracket_planes[index], dpi=self._bracket_dpi, auto_level=False
        )

    def _on_scan_ready(self, image: ScanImage) -> None:
        self._last_scan = image
        self._worker.request_forensic_mark_phase.emit(
            "Scan received",
            {"shape": list(image.rgb.shape), "dpi": image.dpi, "has_ir": image.ir is not None},
        )
        self._loaded_me_short = None
        self._loaded_me_long = None
        debug = self._me_debug
        if debug is not None:
            self.scan_view.set_rgb(debug.rgb_short, dpi=image.dpi, auto_level=False)
            self._populate_bracket_selector(debug, dpi=image.dpi)
            self.merged_view.set_rgb(image.rgb, dpi=image.dpi, auto_level=False)
            stats = debug.fusion_stats
            if stats is not None:
                cap = self._format_fusion_caption(stats).replace(
                    "short-scale output", "short-scale + makeup"
                )
                align = debug.align_shift_long
                if align is not None:
                    cap += f"; long {self._format_align_shift(align)}"
                ir_align = getattr(debug, "align_shift_ir", None)
                if ir_align is not None:
                    cap += f"; IR {self._format_align_shift(ir_align)}"
                self.merged_view.set_caption(cap)
            else:
                self.merged_view.set_caption("Merge: SNR / IVW")
        else:
            self.scan_view.set_rgb(image.rgb, dpi=image.dpi)
            self._clear_bracket_selector()
            self.merged_view.set_rgb(None)
            self.merged_view.set_caption("")
        if image.ir is not None:
            self.ir_view.set_gray(image.ir, dpi=image.dpi)
            ir_align = self._resolve_ir_align_shift(debug)
            if ir_align is not None:
                self.ir_view.set_caption(f"IR {self._format_align_shift(ir_align)}")
            else:
                self.ir_view.set_caption("IR plane")
        else:
            self.ir_view.set_rgb(None)
            self.ir_view.set_caption("")
        self._update_me_tabs_visible()
        if debug is not None:
            self.tabs.setCurrentWidget(self.merged_view)
        else:
            self.tabs.setCurrentWidget(self.scan_view)
        msg = f"Scan {image.rgb.shape[1]}×{image.rgb.shape[0]} @ {image.dpi} dpi"
        if debug is not None:
            msg += "; ME"
            msg += self._fusion_stats_message(debug)
        if image.ir is not None:
            msg += f"; IR {image.ir.shape[1]}×{image.ir.shape[0]}"
            ir_align = self._resolve_ir_align_shift(debug)
            if ir_align is not None:
                msg += f"; {self._format_align_shift(ir_align)}"
        msg += "; " + format_scan_window_note(
            crop_norm=self._pending_crop_norm,
            width=int(image.rgb.shape[1]),
            height=int(image.rgb.shape[0]),
        )
        crop_note = format_crop_status(self._pending_crop_meta)
        if crop_note:
            msg += f"; {crop_note}"
        self.statusBar().showMessage(msg)

    def _resolve_ir_align_shift(self, debug) -> tuple[float, float] | None:
        if debug is not None:
            shift = getattr(debug, "align_shift_ir", None)
            if shift is not None:
                return shift
        return self._worker.last_align_shift_ir

    def _on_clear_calib_cache(self) -> None:
        reply = QMessageBox.question(
            self,
            "Clear calib cache",
            "Delete all cached ASIC shading entries on disk?\n\n"
            "The next prescan or scan will re-measure shading at home.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._worker.clear_calib_cache()

    def _on_calib_cleared(self, path: str) -> None:
        if path:
            self.statusBar().showMessage(f"Cleared calibration cache ({path})")
        else:
            self.statusBar().showMessage("No open scanner — calib cache unchanged")

    def _on_failed(self, message: str) -> None:
        self.statusBar().showMessage(message)
        if message != "Scan cancelled":
            QMessageBox.warning(self, "Scan lab", message)

    def _on_forensic_connect(self) -> None:
        target = self._resolved_target()
        if target is None:
            return
        # Do NOT mark "Connected" here - request_forensic_connect.emit() is
        # fire-and-forget; ensure_open() runs on the worker thread and can
        # take a while (or fail outright). Show "Connecting..." immediately
        # for feedback, but only forensic_connected(True) (emitted by the
        # worker after ensure_open() actually succeeds) flips the badge to
        # Connected - otherwise a slow or failed connect would leave the
        # badge falsely claiming success the whole time.
        self.forensic_tab.set_connecting()
        self._worker.request_forensic_connect.emit(target)

    def _on_forensic_disconnect(self) -> None:
        self._worker.request_forensic_disconnect.emit()
        # set_connected(False) arrives via forensic_connected once
        # close_scanner() actually runs on the worker thread - not called
        # directly here, for the same reason as _on_forensic_connect.

    def _on_busy(self, busy: bool) -> None:
        self.btn_prescan.setEnabled(not busy)
        self.btn_scan.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)
        self.device.setEnabled(not busy)
        self.ppi.setEnabled(not busy)
        self.ir_pass.setEnabled(not busy and bool(
            getattr(self._current_target().model, "supports_infrared", False)
            if self._current_target()
            else False
        ))
        is_gl128 = (
            self._current_target() is not None
            and getattr(self._current_target().model, "asic", "") == "GL128"
        )
        # Temporary (busy) disable only — do not reset mode the way
        # set_gl128_enabled(False) would on an actual device change.
        self.me_controls.setEnabled(not busy and is_gl128)
        self.run_mock.setEnabled(not busy)
        self.override_hw_gate.setEnabled(not busy)
        self.apply_calib.setEnabled(not busy)
        self.btn_clear_calib.setEnabled(not busy)
        self.usb_planar.setEnabled(not busy)
        self.quiet_usb_pace.setEnabled(not busy)
        self.slow_image_slope.setEnabled(not busy)
        self.disable_gl128_prime.setEnabled(not busy)
        self.btn_open_capture.setEnabled(not busy)
        self.forensic_tab.set_busy(busy)
        self.forensic_tab.setEnabled(not busy)


def run() -> int:
    import sys

    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    win = ScanLabWindow()
    win.show()
    return app.exec()
