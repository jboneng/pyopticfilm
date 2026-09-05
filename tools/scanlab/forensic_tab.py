# SPDX-License-Identifier: GPL-3.0-or-later
"""Forensic tab: connect/monitor/record workflow, a register lab, a
Reference page (the "status page" explaining what values mean), and a run
browser (milestones, anomalies, first-divergence diff, exports).

Layout follows the actual usage order top-to-bottom:
  1. Monitor (optional)  - watch status/probe, read-only, whenever you want
  2. Session             - capture everything that happens next as a
                           reviewable, exportable run (idle, register
                           experiments, or an ordinary Prescan/Scan)
  3. Register lab         - advanced/collapsed by default; raw reads/writes

This talks to ScanWorker (worker.py) exclusively via signals - no direct USB
access from the GUI thread, same rule as the rest of Scan Lab. The poll
loop and register ops are refused by the worker whenever a real scan is in
progress (see worker.py's _is_busy checks), so this tab can never interleave
foreign USB traffic into an active Prescan/Scan sequence.
"""

from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from tools.register_reference import parse_addr
from tools.scanlab.forensic_anomaly import detect_anomalies, format_anomalies
from tools.scanlab.forensic_diff import first_divergence, format_divergence
from tools.scanlab.forensic_event_inspector import format_event, load_decoded_events, load_event
from tools.scanlab.forensic_milestones import (
    build_milestones_for_run,
    collect_known_values,
    collect_unknown_registers,
    derive_states,
    format_milestones,
)
from tools.scanlab.forensic_pcap_import import import_pcap
from tools.scanlab.forensic_reference import (
    KNOWN_REGISTERS,
    STATUS_BITS,
    _addr_matches,
    explain_register,
    explain_status,
)
from tools.scanlab.forensic_report_export import build_ai_report
from tools.scanlab.forensic_session import export_run_zip, get_baseline, list_runs, set_baseline
from tools.scanlab.forensic_timeline_view import TimelineGraphView, legend_html
from tools.scanlab.forensic_values_panel import ValuesPanel

_IDLE_STOP_MS = 1000  # auto-record: stop this long after traffic goes idle

#: Reference-tab row colour per confidence level — CONFIRMED calm green,
#: INHERITED neutral blue/grey, SUSPECTED the same warning orange used
#: elsewhere in this tab, UNKNOWN a muted red distinct from
#: forensic_anomaly's critical-severity red (an unknown register is an
#: epistemic gap, not an active-danger signal on its own).
_CONFIDENCE_COLORS = {
    "CONFIRMED": QColor("#2e7d32"),
    "INHERITED": QColor("#5b7fa6"),
    "SUSPECTED": QColor("#c07a20"),
    "UNKNOWN": QColor("#8a4b4b"),
}

_STATUS_BIT_LABELS = [
    ("is_at_home", "At home"),
    ("is_motor_enabled", "Motor enabled"),
    ("is_lamp_on", "Lamp on"),
    ("is_front_end_busy", "AFE busy"),
    ("is_feeding_finished", "Feed finished"),
    ("is_scanning_finished", "Scan finished"),
    ("is_buffer_empty", "Buffer empty"),
]


class ForensicTabPage(QWidget):
    """No device access of its own - emits requests, renders worker signals."""

    connect_clicked = pyqtSignal()
    disconnect_clicked = pyqtSignal()
    poll_toggled = pyqtSignal(bool, int)  # enabled, interval_ms
    register_read_requested = pyqtSignal(int)
    register_write_requested = pyqtSignal(int, int, bool)  # addr, value, force
    recording_toggled = pyqtSignal(bool, str)  # enabled, name

    def __init__(self) -> None:
        super().__init__()
        root = QVBoxLayout(self)

        # --- top: connection badge ---
        top = QHBoxLayout()
        self.badge = QLabel("● Not connected")
        self.badge.setStyleSheet("color: #888; font-weight: 600;")
        self.btn_connect = QPushButton("Connect")
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.setEnabled(False)
        self.btn_connect.clicked.connect(self.connect_clicked.emit)
        self.btn_disconnect.clicked.connect(self.disconnect_clicked.emit)
        top.addWidget(self.badge)
        top.addSpacing(12)
        top.addWidget(self.btn_connect)
        top.addWidget(self.btn_disconnect)
        top.addStretch(1)
        root.addLayout(top)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # --- left: 1. Monitor / 2. Session / 3. Register lab ---
        left = QWidget()
        left_layout = QVBoxLayout(left)

        monitor_box = QGroupBox("1. Monitor (optional) — watch status, read-only")
        monitor_layout = QVBoxLayout(monitor_box)
        poll_row = QHBoxLayout()
        self.poll_enable = QCheckBox("Live poll (status + feed probe)")
        self.poll_interval = QSpinBox()
        self.poll_interval.setRange(50, 5000)
        self.poll_interval.setValue(250)
        self.poll_interval.setSuffix(" ms")
        self.poll_enable.toggled.connect(self._on_poll_toggled)
        self.poll_interval.valueChanged.connect(self._on_poll_toggled_reemit)
        poll_row.addWidget(self.poll_enable)
        poll_row.addWidget(self.poll_interval)
        poll_row.addStretch(1)
        monitor_layout.addLayout(poll_row)

        status_grid = QGridLayout()
        self._status_labels: dict[str, QLabel] = {}
        status_grid.addWidget(QLabel("status_raw"), 0, 0)
        self.lbl_status_raw = QLabel("—")
        status_grid.addWidget(self.lbl_status_raw, 0, 1)
        self.lbl_status_hint = QLabel("")
        self.lbl_status_hint.setWordWrap(True)
        self.lbl_status_hint.setStyleSheet("color: #666; font-style: italic;")
        status_grid.addWidget(self.lbl_status_hint, 0, 2)
        status_grid.addWidget(QLabel("probe_raw (0x21)"), 1, 0)
        self.lbl_probe_raw = QLabel("—")
        status_grid.addWidget(self.lbl_probe_raw, 1, 1)
        row = 2
        for key, label in _STATUS_BIT_LABELS:
            status_grid.addWidget(QLabel(label), row, 0)
            lbl = QLabel("—")
            self._status_labels[key] = lbl
            status_grid.addWidget(lbl, row, 1)
            row += 1
        monitor_layout.addLayout(status_grid)
        left_layout.addWidget(monitor_box)

        session_box = QGroupBox("2. Session — capture what happens next")
        session_layout = QVBoxLayout(session_box)
        session_desc = QLabel(
            "Captures raw + decoded USB traffic starting now — while idle, "
            "during register experiments below, or during an ordinary "
            "Prescan/Scan. Review it afterward in the Run browser tab."
        )
        session_desc.setWordWrap(True)
        session_desc.setStyleSheet("color: #666;")
        session_layout.addWidget(session_desc)
        session_form = QFormLayout()
        self.rec_name = QLineEdit("scanlab-session")
        session_form.addRow("Session name", self.rec_name)
        session_layout.addLayout(session_form)
        self.chk_auto_record = QCheckBox("Auto: clear + record on Prescan/Scan, stop 1s after idle")
        self.chk_auto_record.setChecked(True)
        self.chk_auto_record.setToolTip(
            "When a Prescan/Scan is clicked, clear the timeline and start a fresh "
            "session automatically; stop it 1s after USB traffic goes idle. Turn "
            "off to control Start/Stop session by hand instead."
        )
        session_layout.addWidget(self.chk_auto_record)
        self.btn_record = QPushButton("Start session")
        self.btn_record.setCheckable(True)
        self.btn_record.toggled.connect(self._on_record_toggled)
        session_layout.addWidget(self.btn_record)
        self.lbl_session_counters = QLabel("")
        self.lbl_session_counters.setStyleSheet("color: #666;")
        session_layout.addWidget(self.lbl_session_counters)
        left_layout.addWidget(session_box)

        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(_IDLE_STOP_MS)
        self._idle_timer.timeout.connect(self._on_idle_timeout)
        self._suppress_next_saved_uncheck = False

        reg_box = QGroupBox("3. Register lab (advanced)")
        reg_box.setCheckable(True)
        reg_box.setChecked(False)
        reg_form = QFormLayout(reg_box)
        warn = QLabel(
            "Direct register access. Writes are refused for motion/lamp-adjacent "
            "addresses and while the motor reports enabled, unless overridden."
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #a05a00;")
        reg_form.addRow(warn)
        self.reg_address = QLineEdit()
        self.reg_address.setPlaceholderText("0x101")
        self.reg_value = QLineEdit()
        self.reg_value.setPlaceholderText("0x0F (write only)")
        self.reg_force = QCheckBox("I understand the risk (bypass motion/idle guard)")
        reg_btn_row = QHBoxLayout()
        self.btn_reg_read = QPushButton("Read")
        self.btn_reg_write = QPushButton("Write")
        self.btn_reg_read.clicked.connect(self._on_read_clicked)
        self.btn_reg_write.clicked.connect(self._on_write_clicked)
        reg_btn_row.addWidget(self.btn_reg_read)
        reg_btn_row.addWidget(self.btn_reg_write)
        reg_form.addRow("Address", self.reg_address)
        reg_form.addRow("Value", self.reg_value)
        reg_form.addRow(self.reg_force)
        reg_form.addRow(reg_btn_row)
        left_layout.addWidget(reg_box)
        left_layout.addStretch(1)
        splitter.addWidget(left)

        # --- right: Live timeline / Reference / Run browser ---
        right_tabs = self.right_tabs = QTabWidget()
        self.timeline = QPlainTextEdit()
        self.timeline.setReadOnly(True)
        self.timeline.setMaximumBlockCount(20000)
        self.timeline.setPlaceholderText(
            "Decoded USB events appear here — during live poll, during a "
            "session, and during any ordinary Prescan/Scan while this tab is open."
        )
        right_tabs.addTab(self._build_live_timeline_tab(), "Live timeline")
        right_tabs.addTab(self._build_timeline_graph_tab(), "Timeline")
        right_tabs.addTab(self._build_reference_tab(), "Reference")
        right_tabs.addTab(self._build_run_browser_tab(), "Run browser")

        splitter.addWidget(right_tabs)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter)

        self._anomaly_count = 0
        self._session_event_count = 0
        self._is_connected = False
        self._inspector_run_dir: Path | None = None

    # -- "Live timeline" tab (plain-text log) -----------------------------------

    def _build_live_timeline_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self.timeline.clear)
        toolbar.addWidget(btn_clear)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)
        layout.addWidget(self.timeline)
        return page

    # -- "Timeline" tab (graphical timeline + Event Inspector) ------------------

    def _build_timeline_graph_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)

        toolbar = QHBoxLayout()
        btn_clear = QPushButton("Clear")
        btn_clear.setToolTip("Clear the graphical timeline and Event Inspector (does not stop or delete a saved run).")
        btn_clear.clicked.connect(self._on_timeline_clear_clicked)
        btn_fit = QPushButton("Fit all")
        btn_fit.clicked.connect(lambda: self.timeline_graph.fit_all())
        btn_live = QPushButton("Jump to live")
        btn_live.clicked.connect(lambda: self.timeline_graph.jump_to_live())
        toolbar.addWidget(btn_clear)
        toolbar.addWidget(btn_fit)
        toolbar.addWidget(btn_live)
        self.timeline_duration_label = QLabel("Total: --")
        toolbar.addWidget(self.timeline_duration_label)
        toolbar.addStretch(1)
        toolbar.addWidget(QLabel("Scroll to zoom, drag to pan, click a mark to inspect it below."))
        outer.addLayout(toolbar)

        legend = QLabel(legend_html())
        legend.setStyleSheet("color: #444;")
        outer.addWidget(legend)

        timeline_col = QWidget()
        layout = QVBoxLayout(timeline_col)
        layout.setContentsMargins(0, 0, 0, 0)

        self.timeline_graph = TimelineGraphView()
        self.timeline_graph.event_selected.connect(self._on_timeline_event_selected)

        inspector_col = QWidget()
        inspector_layout = QVBoxLayout(inspector_col)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_layout.addWidget(QLabel("Event Inspector — raw bytes, decoded fields, and register meaning together"))
        self.event_inspector = QPlainTextEdit()
        self.event_inspector.setReadOnly(True)
        self.event_inspector.setPlaceholderText("Click a mark on the timeline above, or a milestone/anomaly row.")
        inspector_layout.addWidget(self.event_inspector)

        # setStretchFactor alone only governs how EXTRA space is distributed
        # on resize, not the initial split - QSplitter defaults new children
        # to equal sizes otherwise, which is what was silently overriding
        # the intended timeline-heavy layout. setSizes() sets the initial
        # split; the stretch factors above still apply to later resizes.
        vertical_split = QSplitter(Qt.Orientation.Vertical)
        vertical_split.addWidget(self.timeline_graph)
        vertical_split.addWidget(inspector_col)
        vertical_split.setStretchFactor(0, 5)
        vertical_split.setStretchFactor(1, 1)
        vertical_split.setSizes([900, 160])
        layout.addWidget(vertical_split)

        self.values_panel = ValuesPanel()
        horizontal_split = QSplitter(Qt.Orientation.Horizontal)
        horizontal_split.addWidget(timeline_col)
        horizontal_split.addWidget(self.values_panel)
        horizontal_split.setStretchFactor(0, 4)
        horizontal_split.setStretchFactor(1, 1)
        horizontal_split.setSizes([1200, 300])
        # Stretch factor 1 here (vs. the toolbar/legend's implicit 0 above)
        # means Qt gives ALL leftover vertical space to the splitter and
        # leaves the toolbar/legend at their natural sizeHint - without
        # this, QVBoxLayout divides leftover space roughly evenly across
        # every item when all stretch factors are 0, which is what was
        # inflating the toolbar and legend rows to ~1/3 of the tab's height
        # each, squeezing the actual timeline into a sliver at the bottom.
        outer.addWidget(horizontal_split, 1)
        self.timeline_tab_page = page
        return page

    def _on_timeline_clear_clicked(self) -> None:
        self.timeline_graph.clear()
        self.event_inspector.clear()
        self.values_panel.clear()
        self.timeline_duration_label.setText("Total: --")

    def _on_timeline_event_selected(self, index: int) -> None:
        if self._inspector_run_dir is None:
            self.event_inspector.setPlainText("(no run associated with the timeline yet)")
            return
        try:
            event = load_event(self._inspector_run_dir, index)
        except Exception as exc:  # noqa: BLE001
            self.event_inspector.setPlainText(f"Failed to load event {index}: {exc}")
            return
        text = format_event(event)
        is_mock = self._run_is_mock(self._inspector_run_dir)
        if is_mock is True:
            text = "⚠ MOCK RUN — synthetic data, not real hardware.\n\n" + text
        self.event_inspector.setPlainText(text)

    # -- static "Reference" tab ------------------------------------------------

    def _build_reference_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.reference_search = QLineEdit()
        self.reference_search.setPlaceholderText("Filter…")
        self.reference_search.textChanged.connect(self._filter_reference_tables)
        layout.addWidget(self.reference_search)

        layout.addWidget(QLabel("Status register (0x101 / 0x41) bits"))
        self.status_bits_table = QTableWidget(len(STATUS_BITS), 4)
        self.status_bits_table.setHorizontalHeaderLabels(["Bit", "Name", "Meaning", "Confidence"])
        for i, ref in enumerate(STATUS_BITS):
            for col, value in enumerate((ref.bit, ref.name, ref.meaning, ref.confidence)):
                item = QTableWidgetItem(value)
                if col == 3:
                    self._apply_confidence_color(item, ref.confidence)
                self.status_bits_table.setItem(i, col, item)
        self.status_bits_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.status_bits_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.status_bits_table.setWordWrap(True)
        layout.addWidget(self.status_bits_table, 1)

        layout.addWidget(QLabel("Known registers"))
        self.registers_table = QTableWidget(len(KNOWN_REGISTERS), 5)
        self.registers_table.setHorizontalHeaderLabels(
            ["Address", "Name", "Meaning", "Confidence", "Scanner scope"]
        )
        for i, ref in enumerate(KNOWN_REGISTERS):
            for col, value in enumerate(
                (ref.addr, ref.name, ref.meaning, ref.confidence, ref.scanner_scope)
            ):
                item = QTableWidgetItem(value)
                if col == 3:
                    self._apply_confidence_color(item, ref.confidence)
                if col == 0 and ref.safety_note:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    item.setText(f"⚠ {value}")
                    item.setToolTip(ref.safety_note)
                self.registers_table.setItem(i, col, item)
        self.registers_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.registers_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.registers_table.setWordWrap(True)
        self.registers_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.registers_table, 1)

        jump_row = QHBoxLayout()
        self.btn_reference_jump_timeline = QPushButton("Jump to timeline for selected register")
        self.btn_reference_jump_timeline.clicked.connect(self._on_reference_jump_to_timeline)
        jump_row.addWidget(self.btn_reference_jump_timeline)
        self.reference_jump_status = QLabel("")
        self.reference_jump_status.setStyleSheet("color: #666;")
        jump_row.addWidget(self.reference_jump_status, 1)
        layout.addLayout(jump_row)

        # Row heights depend on the "Meaning" column's actual (stretched)
        # width, which isn't final until this widget has been laid out at
        # least once - resizeRowsToContents() here would measure against the
        # pre-stretch default column width and under-size every row.
        # QTimer.singleShot(0, ...) defers to right after that first layout
        # pass instead.
        QTimer.singleShot(0, self.status_bits_table.resizeRowsToContents)
        QTimer.singleShot(0, self.registers_table.resizeRowsToContents)
        return page

    def _apply_confidence_color(self, item: QTableWidgetItem, confidence: str) -> None:
        color = _CONFIDENCE_COLORS.get(confidence.strip().upper())
        if color is not None:
            item.setForeground(color)
            font = item.font()
            font.setBold(True)
            item.setFont(font)

    def _filter_reference_tables(self, text: str) -> None:
        text = text.strip().lower()
        for table in (self.status_bits_table, self.registers_table):
            for row in range(table.rowCount()):
                match = not text or any(
                    text in (table.item(row, col).text().lower() if table.item(row, col) else "")
                    for col in range(table.columnCount())
                )
                table.setRowHidden(row, not match)

    def _on_reference_jump_to_timeline(self) -> None:
        row = self.registers_table.currentRow()
        if row < 0:
            self.reference_jump_status.setText("Select a register row first.")
            return
        addr_spec = KNOWN_REGISTERS[row].addr
        name = KNOWN_REGISTERS[row].name
        # Switch this Forensic tab's own right-hand tab widget to "Timeline"
        # (the caller — app.py's ScanLabWindow — has its own outer QTabWidget
        # with a "Forensic" tab; switching to that one, if needed, is the
        # caller's responsibility, not this widget's). Uses the tab page
        # itself, not timeline_graph.parentWidget() - the graph now sits
        # inside a QSplitter, so its direct parent isn't the tab page.
        idx = self.right_tabs.indexOf(self.timeline_tab_page)
        if idx >= 0:
            self.right_tabs.setCurrentIndex(idx)
        if self._inspector_run_dir is None:
            self.reference_jump_status.setText(
                f"{name} ({addr_spec}): no run loaded — start/open a run to see matching events."
            )
            return
        events = load_decoded_events(self._inspector_run_dir)
        count = 0
        for ev in events:
            fields = ev.get("fields", {})
            addr = fields.get("addr") or fields.get("w_index")
            if addr is None:
                continue
            target = parse_addr(addr)
            if target is None:
                continue
            if _addr_matches(addr_spec, target):
                count += 1
        self.reference_jump_status.setText(
            f"{name} ({addr_spec}): {count} matching event(s) in the currently loaded run."
        )

    # -- "Run browser" tab -------------------------------------------------------

    def _build_run_browser_tab(self) -> QWidget:
        browser = QWidget()
        browser_outer = QVBoxLayout(browser)
        browser_layout = QHBoxLayout()
        list_col = QVBoxLayout()
        self.run_list = QListWidget()
        self.run_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.run_list.currentItemChanged.connect(self._on_run_selected)
        list_col.addWidget(self.run_list)

        self.lbl_baseline = QLabel("Baseline: (none set)")
        self.lbl_baseline.setStyleSheet("color: #666;")
        list_col.addWidget(self.lbl_baseline)

        list_btn_row = QHBoxLayout()
        self.btn_refresh_runs = QPushButton("Refresh runs")
        self.btn_refresh_runs.clicked.connect(self._refresh_run_list)
        self.btn_set_baseline = QPushButton("Set as baseline")
        self.btn_set_baseline.clicked.connect(self._on_set_baseline_clicked)
        self.btn_compare_runs = QPushButton("Compare (vs. baseline, or select 2)")
        self.btn_compare_runs.clicked.connect(self._on_compare_clicked)
        list_btn_row.addWidget(self.btn_refresh_runs)
        list_btn_row.addWidget(self.btn_set_baseline)
        list_col.addLayout(list_btn_row)
        list_col.addWidget(self.btn_compare_runs)

        import_box = QGroupBox("Import Wireshark/USBPcap capture")
        import_box.setToolTip(
            "Import a .pcap/.pcapng (e.g. captured with USBPcap/Wireshark while "
            "OTHER software drove the scanner) as a run. Our own live connection "
            "claims the USB interface exclusively and cannot sniff traffic from "
            "another application — Wireshark/USBPcap taps below both, so that's "
            "the supported way to observe a different program's traffic."
        )
        import_form = QFormLayout(import_box)
        self.import_name = QLineEdit("wireshark-import")
        self.btn_import_pcap = QPushButton("Import capture…")
        self.btn_import_pcap.clicked.connect(self._on_import_pcap_clicked)
        import_form.addRow("Session name", self.import_name)
        import_form.addRow(self.btn_import_pcap)
        list_col.addWidget(import_box)

        export_col = QVBoxLayout()
        self.btn_export_run = QPushButton("Export selected run (.zip)")
        self.btn_export_run.clicked.connect(self._on_export_run_clicked)
        self.btn_export_ai_report = QPushButton("Export AI bug report…")
        self.btn_export_ai_report.setToolTip(
            "Bundles this run's summary, phase durations, milestones, and "
            "anomalies (diffed against the baseline if one is set, or a second "
            "selected run) into one Markdown file meant to be handed to an AI "
            "assistant for the next debugging session."
        )
        self.btn_export_ai_report.clicked.connect(self._on_export_ai_report_clicked)
        export_col.addWidget(self.btn_export_run)
        export_col.addWidget(self.btn_export_ai_report)
        list_col.addLayout(export_col)

        browser_layout.addLayout(list_col, 1)
        self.run_detail = QPlainTextEdit()
        self.run_detail.setReadOnly(True)
        browser_layout.addWidget(self.run_detail, 2)
        browser_outer.addLayout(browser_layout)
        self._refresh_run_list()
        return browser

    # -- outbound (GUI -> worker) ---------------------------------------------

    def _on_poll_toggled(self, _checked: bool) -> None:
        self.poll_toggled.emit(self.poll_enable.isChecked(), self.poll_interval.value())

    def _on_poll_toggled_reemit(self, _value: int) -> None:
        if self.poll_enable.isChecked():
            self.poll_toggled.emit(True, self.poll_interval.value())

    def _parse_int(self, text: str, field: str) -> int | None:
        text = text.strip()
        if not text:
            QMessageBox.warning(self, "Forensic tab", f"{field} is required.")
            return None
        try:
            return int(text, 0)
        except ValueError:
            QMessageBox.warning(self, "Forensic tab", f"{field} must be an integer (e.g. 0x101 or 257).")
            return None

    def _set_register_lab_busy(self, busy: bool) -> None:
        """Visible feedback that a register op is in flight - important
        because these run synchronously on the worker thread; if the
        underlying USB call is unusually slow (or genuinely stuck below
        libusb's own per-call timeout), the buttons staying enabled with no
        indication would look identical to "nothing happened." This does
        NOT and cannot force a stuck call to return (Python can't preempt a
        blocked OS-level call in another thread) - it only makes the
        in-flight state honest instead of silent."""
        self.btn_reg_read.setEnabled(not busy)
        self.btn_reg_write.setEnabled(not busy)
        self.btn_reg_read.setText("Reading…" if busy else "Read")
        self.btn_reg_write.setText("Writing…" if busy else "Write")

    def _on_read_clicked(self) -> None:
        addr = self._parse_int(self.reg_address.text(), "Address")
        if addr is None:
            return
        self._set_register_lab_busy(True)
        self.register_read_requested.emit(addr)

    def _on_write_clicked(self) -> None:
        addr = self._parse_int(self.reg_address.text(), "Address")
        if addr is None:
            return
        val = self._parse_int(self.reg_value.text(), "Value")
        if val is None:
            return
        self._set_register_lab_busy(True)
        self.register_write_requested.emit(addr, val, self.reg_force.isChecked())

    def _on_record_toggled(self, checked: bool) -> None:
        self.btn_record.setText("Stop session" if checked else "Start session")
        self.rec_name.setEnabled(not checked)
        if checked:
            self._anomaly_count = 0
            self._session_event_count = 0
            self._update_session_counters()
        else:
            self._idle_timer.stop()
        self.recording_toggled.emit(checked, self.rec_name.text().strip())

    def _on_idle_timeout(self) -> None:
        """Auto-record's "stop 1s after idle" half - append_timeline_line()
        restarts this timer on every decoded event/anomaly/register result
        while recording, so it only fires after a genuine 1s traffic gap."""
        if self.chk_auto_record.isChecked() and self.btn_record.isChecked():
            self.append_timeline_line("[auto-record] idle for 1s — stopping session")
            self.btn_record.setChecked(False)

    def start_or_restart_auto_record(self) -> None:
        """Called by app.py right before a Prescan/Scan request, when
        auto-record is enabled: clears the timeline and starts a fresh
        session for this operation, stopping any session already in
        progress first - so each Prescan/Scan gets its own clean recording
        without the user manually toggling Start/Stop session every time."""
        if not self.chk_auto_record.isChecked():
            return
        self._idle_timer.stop()
        if self.btn_record.isChecked():
            # The eventual forensic_run_saved for THIS stop must not uncheck
            # the button once the new session (started right below) is
            # already running - see on_recording_saved().
            self._suppress_next_saved_uncheck = True
            self.btn_record.setChecked(False)
        self.timeline.clear()
        self.btn_record.setChecked(True)

    def _update_session_counters(self) -> None:
        events = getattr(self, "_session_event_count", 0)
        self.lbl_session_counters.setText(f"{events} events · {self._anomaly_count} anomalies flagged")

    # -- inbound (worker -> GUI) ----------------------------------------------

    def append_timeline_line(self, line: str) -> None:
        self.timeline.appendPlainText(line)
        if self.btn_record.isChecked():
            self._session_event_count = getattr(self, "_session_event_count", 0) + 1
            if self._session_event_count % 5 == 0:  # avoid re-laying-out the label on every line
                self._update_session_counters()
            if self.chk_auto_record.isChecked():
                self._idle_timer.start()

    def on_anomaly(self, anomaly: dict) -> None:
        self._anomaly_count += 1
        self._update_session_counters()
        self.append_timeline_line(
            f"⚠ ANOMALY [{anomaly.get('severity')}] {anomaly.get('rule_id')}: {anomaly.get('description')}"
        )
        rel_s = anomaly.get("rel_s")
        index = anomaly.get("index")
        if rel_s is not None and index is not None:
            self.timeline_graph.append_anomaly(rel_s, anomaly.get("severity", "info"), index)

    def on_run_started(self, path: str, is_mock: bool) -> None:
        """A Session just started - point the graphical timeline + Event
        Inspector at this run's (still-being-written) files, live. is_mock
        comes straight from the worker (manifest.json isn't written until
        the run finishes, so it can't be read from disk yet)."""
        self._inspector_run_dir = Path(path)
        self.timeline_graph.clear()
        if is_mock:
            self.append_timeline_line("⚠ MOCK SESSION — synthetic data, not real hardware")

    def on_timeline_event(self, rel_s: float, kind: str, index: int) -> None:
        self.timeline_graph.append_event(rel_s, kind, index)

    def on_timeline_marker(self, rel_s: float, label: str) -> None:
        self.timeline_graph.append_marker(rel_s, label)

    def set_status(self, status: dict) -> None:
        self.lbl_status_raw.setText(str(status.get("status_raw", "—")))
        self.lbl_probe_raw.setText(str(status.get("probe_raw", "—")))
        raw = status.get("status_raw")
        if isinstance(raw, str) and raw.startswith("0x"):
            try:
                self.lbl_status_hint.setText(explain_status(int(raw, 16)))
            except ValueError:
                self.lbl_status_hint.setText("")
        for key, lbl in self._status_labels.items():
            val = status.get(key)
            if val is None:
                lbl.setText("—")
            else:
                lbl.setText("yes" if val else "no")
                lbl.setStyleSheet("color: #c0392b;" if (key == "is_motor_enabled" and val) else "")

    def set_register_result(self, result: dict) -> None:
        self._set_register_lab_busy(False)
        addr = result.get("address")
        value = result.get("value") or result.get("readback_after")
        hint = None
        if addr is not None:
            try:
                hint = explain_register(addr, int(value, 16) if isinstance(value, str) else value)
            except (TypeError, ValueError):
                hint = None
        line = f"[register] {json.dumps(result)}"
        if hint:
            line += f"\n           ↳ {hint}"
        self.append_timeline_line(line)

    def set_connecting(self) -> None:
        """Immediate feedback on click, before the worker thread has
        actually confirmed anything - see app.py's _on_forensic_connect for
        why this is NOT the same as set_connected(True)."""
        self.btn_connect.setEnabled(False)
        self.badge.setText("● Connecting…")
        self.badge.setStyleSheet("color: #a05a00; font-weight: 600;")

    def set_connected(self, connected: bool) -> None:
        self._is_connected = connected
        self.btn_connect.setEnabled(not connected)
        self.btn_disconnect.setEnabled(connected)
        self.set_busy(False)

    def set_busy(self, busy: bool) -> None:
        # Track connection state explicitly (self._is_connected) rather than
        # reading btn_disconnect.isEnabled(): app.py's _on_busy() also calls
        # self.forensic_tab.setEnabled(not busy), which cascades an effective
        # isEnabled()==False onto every child widget (including buttons whose
        # own explicit enabled flag is still True) while busy - so the button
        # itself is a false signal for "are we connected" during that window.
        is_connected = getattr(self, "_is_connected", False)
        if busy:
            self.badge.setText("● Connected — scan running (forensic paused)")
            self.badge.setStyleSheet("color: #a05a00; font-weight: 600;")
        elif is_connected:
            self.badge.setText("● Connected — idle")
            self.badge.setStyleSheet("color: #1a7f37; font-weight: 600;")
        else:
            self.badge.setText("● Not connected")
            self.badge.setStyleSheet("color: #888; font-weight: 600;")

    def on_recording_saved(self, path: str) -> None:
        self.append_timeline_line(f"[session saved] {path}")
        if self._suppress_next_saved_uncheck:
            # This "saved" is the tail end of an auto-record restart's stop
            # half - a new session is already checked/running by the time
            # this (queued, from the worker thread) callback arrives, so
            # don't stomp on it.
            self._suppress_next_saved_uncheck = False
        else:
            self.btn_record.setChecked(False)
        self._refresh_run_list()

    def show_error(self, message: str) -> None:
        self._set_register_lab_busy(False)  # clears the Read/Write busy state on a refusal/failure too
        self.append_timeline_line(f"[error] {message}")

    # -- run browser -----------------------------------------------------------

    @staticmethod
    def _run_is_mock(run_dir: Path) -> bool | None:
        """True/False from manifest.json's device.mock, or None if unknown
        (manifest missing/unreadable - e.g. a very old run predating this
        field). Never assume real hardware when this can't be determined -
        callers should treat None as "can't confirm, don't claim either way,"
        not as "assume real." A run manually mixed up as real when it was
        actually MockScannerTransport output is exactly the kind of error
        this exists to prevent (see the 2026-09-05 session log: a mock CLI
        test run was briefly mistaken for real idle scanner traffic)."""
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        device = manifest.get("device")
        if isinstance(device, dict) and "mock" in device:
            return bool(device["mock"])
        return None

    def _refresh_run_list(self) -> None:
        self.run_list.clear()
        for run_dir in list_runs():
            label = f"{run_dir.parent.name} · {run_dir.name}"
            is_mock = self._run_is_mock(run_dir)
            if is_mock is True:
                label = f"⚠ MOCK — {label}"
            item = QListWidgetItem(label)
            if is_mock is True:
                item.setForeground(QColor("#a05a00"))
            item.setData(Qt.ItemDataRole.UserRole, str(run_dir))
            self.run_list.addItem(item)
        baseline = get_baseline()
        self.lbl_baseline.setText(
            f"Baseline: {' · '.join(baseline.parts[-2:])}" if baseline else "Baseline: (none set)"
        )

    def _on_set_baseline_clicked(self) -> None:
        selected = self.run_list.selectedItems()
        if len(selected) != 1:
            QMessageBox.information(self, "Set baseline", "Select exactly one run to mark as baseline.")
            return
        run_dir = Path(selected[0].data(Qt.ItemDataRole.UserRole))
        set_baseline(run_dir)
        self._refresh_run_list()
        self.append_timeline_line(f"[baseline] set to {run_dir}")

    def _selected_run_and_baseline(self) -> tuple[Path, Path | None] | None:
        """One run selected -> (run, baseline-if-set). Two selected -> (first, second)."""
        selected = self.run_list.selectedItems()
        if len(selected) == 2:
            return (
                Path(selected[0].data(Qt.ItemDataRole.UserRole)),
                Path(selected[1].data(Qt.ItemDataRole.UserRole)),
            )
        if len(selected) == 1:
            return Path(selected[0].data(Qt.ItemDataRole.UserRole)), get_baseline()
        return None

    def _on_compare_clicked(self) -> None:
        pair = self._selected_run_and_baseline()
        if pair is None:
            QMessageBox.information(
                self, "Compare runs", "Select one run (compares against the baseline) or two runs."
            )
            return
        run_a, run_b = pair
        if run_b is None:
            QMessageBox.information(
                self, "Compare runs", "No baseline is set — select two runs, or set a baseline first."
            )
            return
        result = first_divergence(run_a, run_b)
        label_a = " · ".join(run_a.parts[-2:])
        label_b = " · ".join(run_b.parts[-2:])
        text = f"A = {label_a}\nB = {label_b}\n\n" + format_divergence(result, label_a="A", label_b="B")
        self.run_detail.setPlainText(text)

    def _on_run_selected(self, current: QListWidgetItem | None, _previous) -> None:
        if current is None:
            self.run_detail.setPlainText("")
            return
        run_dir = Path(current.data(Qt.ItemDataRole.UserRole))
        is_mock = self._run_is_mock(run_dir)
        summary_path = run_dir / "summary.md"
        text = summary_path.read_text() if summary_path.exists() else "(no summary.md)"
        if is_mock is True:
            text = (
                "⚠⚠⚠ MOCK RUN — synthetic MockScannerTransport output, NOT from real hardware. "
                "Do not use this as evidence about actual scanner behavior. ⚠⚠⚠\n\n" + text
            )
        elif is_mock is None:
            text = "(mock/real status unknown for this run - manifest.json missing or has no device.mock field)\n\n" + text
        result_path = run_dir / "result.json"
        if result_path.exists():
            text += "\n\n## result.json\n" + result_path.read_text()

        decoded_events = self._read_decoded_events(run_dir)
        phase_markers = self._read_phase_markers(run_dir)
        try:
            milestones = build_milestones_for_run(run_dir)
        except Exception as exc:  # noqa: BLE001
            milestones = []
            text += f"\n\n## Milestones\n(failed to compute: {exc})"
        if milestones or phase_markers:
            text += "\n\n## Milestones (guessed — see confidence column; never treat as fact)\n"
            text += format_milestones(milestones, phase_markers=phase_markers)

        anomalies = []
        try:
            anomalies = detect_anomalies(decoded_events, phase_markers=phase_markers)
            text += "\n\n## Anomalies (heuristic flags, not confirmed defects)\n"
            text += format_anomalies(anomalies)
        except Exception as exc:  # noqa: BLE001
            text += f"\n\n## Anomalies\n(failed to compute: {exc})"

        self.run_detail.setPlainText(text)

        # Also drive the graphical Timeline tab + Event Inspector for this
        # run - post-hoc, whole-run render (as opposed to the live append_*
        # path used while a Session is still active). Feed-timing milestones
        # become duration brackets; LPERIOD/EXPOSURE/pixel-clock values go to
        # the Known-values panel instead of the timeline (that's what was
        # crowding the marker row) - only host-side phase markers stay there.
        feed_spans = [
            {
                "start_rel_s": m["evidence"].get("start_rel_s"),
                "end_rel_s": m["evidence"].get("end_rel_s"),
                # Terser than the milestone's own label (which can be wider
                # than a short span, overflowing into the next one) - the
                # full "Positioning feed FEEDL=..." text is still in the
                # Known-values panel and the text report.
                "label": f"{m['evidence'].get('slope_table') or '?'} {m['evidence'].get('duration_s', 0):.2f}s",
            }
            for m in milestones
            if m["kind"] == "feed_timing"
            and m["evidence"].get("start_rel_s") is not None
            and m["evidence"].get("end_rel_s") is not None
        ]
        states = derive_states(decoded_events)
        self._inspector_run_dir = run_dir
        self.timeline_graph.set_data(decoded_events, phase_markers, anomalies, feed_spans, states)
        duration = self.timeline_graph.total_duration_s()
        self.timeline_duration_label.setText(f"Total: {duration:.2f}s" if duration is not None else "Total: --")
        self.values_panel.set_data(collect_known_values(milestones), collect_unknown_registers(decoded_events))

    @staticmethod
    def _read_decoded_events(run_dir: Path) -> list[dict]:
        path = run_dir / "decoded_events.jsonl"
        if not path.exists():
            return []
        events = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    @staticmethod
    def _read_phase_markers(run_dir: Path) -> list[dict]:
        path = run_dir / "phase_markers.jsonl"
        if not path.exists():
            return []
        markers = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    markers.append(json.loads(line))
        return markers

    def _on_import_pcap_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Wireshark/USBPcap capture", "", "USBPcap (*.pcap *.pcapng);;All files (*.*)"
        )
        if not path:
            return
        try:
            out_dir = import_pcap(
                Path(path),
                name=self.import_name.text().strip() or "wireshark-import",
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Import capture", f"Failed to import:\n{exc}")
            return
        self.append_timeline_line(f"[import] {path} -> {out_dir}")
        self._refresh_run_list()

    def _on_export_run_clicked(self) -> None:
        selected = self.run_list.selectedItems()
        if len(selected) != 1:
            QMessageBox.information(self, "Export run", "Select exactly one run to export.")
            return
        run_dir = Path(selected[0].data(Qt.ItemDataRole.UserRole))
        default_name = "_".join(run_dir.parts[-2:]) + ".zip"
        dest, _ = QFileDialog.getSaveFileName(self, "Export run as .zip", default_name, "Zip (*.zip)")
        if not dest:
            return
        try:
            export_run_zip(run_dir, Path(dest))
        except OSError as exc:
            QMessageBox.warning(self, "Export run", f"Failed to export:\n{exc}")
            return
        self.append_timeline_line(f"[export] {run_dir} -> {dest}")

    def _on_export_ai_report_clicked(self) -> None:
        pair = self._selected_run_and_baseline()
        if pair is None:
            QMessageBox.information(self, "Export AI bug report", "Select one run (or two runs) first.")
            return
        run_dir, baseline_dir = pair
        default_name = "_".join(run_dir.parts[-2:]) + "_ai_report.md"
        dest, _ = QFileDialog.getSaveFileName(self, "Export AI bug report", default_name, "Markdown (*.md)")
        if not dest:
            return
        try:
            report = build_ai_report(run_dir, baseline_dir=baseline_dir)
            Path(dest).write_text(report, encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Export AI bug report", f"Failed to write file:\n{exc}")
            return
        self.append_timeline_line(f"[export] AI bug report -> {dest}")
