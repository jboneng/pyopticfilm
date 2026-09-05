# SPDX-License-Identifier: GPL-3.0-or-later
"""Known/Unknown values side panel for the Forensic tab's Timeline view.

Shows the register/table values collected for the selected run without
requiring the user to scrub the timeline to find them - "Known" lists
distinct milestone-derived values (slope table per feed, LPERIOD,
EXPOSURE, pixel clock); "Unknown" lists register addresses touched this
run that ``explain_register()`` has nothing to say about, so unexplained
traffic is visible rather than silently dropped.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtWidgets import QGroupBox, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from tools.scanlab.forensic_timecode import format_timecode


class ValuesPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        known_box = QGroupBox("Known values")
        known_layout = QVBoxLayout(known_box)
        self.known_list = QListWidget()
        known_layout.addWidget(self.known_list)
        layout.addWidget(known_box, 1)

        unknown_box = QGroupBox("Unknown registers (no catalog entry)")
        unknown_layout = QVBoxLayout(unknown_box)
        self.unknown_list = QListWidget()
        unknown_layout.addWidget(self.unknown_list)
        layout.addWidget(unknown_box, 1)

    def clear(self) -> None:
        self.known_list.clear()
        self.unknown_list.clear()

    def set_data(self, known: list[dict[str, Any]], unknown: list[dict[str, Any]]) -> None:
        self.clear()
        for entry in known:
            timecode = format_timecode(entry.get("rel_s"))
            item = QListWidgetItem(f"[{timecode}] {entry['label']}")
            item.setToolTip(entry["label"])
            self.known_list.addItem(item)
        if not known:
            self.known_list.addItem(QListWidgetItem("(no known values found for this run)"))

        for entry in unknown:
            timecode = format_timecode(entry.get("rel_s"))
            item = QListWidgetItem(f"[{timecode}] {entry['addr']} = {entry['value']}")
            self.unknown_list.addItem(item)
        if not unknown:
            self.unknown_list.addItem(QListWidgetItem("(nothing unexplained - or no run loaded)"))
