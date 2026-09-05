# SPDX-License-Identifier: GPL-3.0-or-later
"""Horizontal graphical timeline: decoded events as marks on a time axis,
phase/button-press markers as labeled boundary lines, anomalies as
severity-colored triangles. Hand-painted QWidget (QPainter) - no new
dependency (PyQt6-Charts is not a project dependency and this avoids
adding one for what's fundamentally scatter points + vertical lines).

Works live (points appended incrementally as a Session runs) and post-hoc
(a saved run's full decoded_events.jsonl/phase_markers.jsonl/anomalies
loaded at once via set_data()) - the append_* and set_data() paths share
the same internal point lists, so there's exactly one rendering path.

The clustering math (cluster_points) is a pure function, independent of
Qt/painting, so it's unit-testable on its own (see tests/
test_scanlab_forensic.py) - the widget just calls it once per paint with
the current pixel-to-seconds ratio.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen, QPolygonF, QWheelEvent
from PyQt6.QtWidgets import QWidget

from tools.scanlab.forensic_timecode import format_timecode

_KIND_TO_LANE = {
    "reg_write": "reg_write",
    "reg_read": "reg_read",
    "probe_read": "probe_read",
    "buffer_preamble": "buffer_preamble",
    "bulk_in": "bulk_in",
    "bulk_out": "bulk_out",
}
LANES = ["reg_write", "reg_read", "probe_read", "buffer_preamble", "bulk_in", "bulk_out", "unclassified"]
LANE_COLORS = {
    "reg_write": QColor("#2f6fb0"),
    "reg_read": QColor("#3a9c5f"),
    "probe_read": QColor("#8a6fb0"),
    "buffer_preamble": QColor("#c07a20"),
    "bulk_in": QColor("#c0392b"),
    "bulk_out": QColor("#b0793a"),
    "unclassified": QColor("#888888"),
}
SEVERITY_COLORS = {"info": QColor("#888888"), "warning": QColor("#c07a20"), "critical": QColor("#c0392b")}

_LANE_H = 22
_TOP_MARGIN = 50  # room for rotated marker labels
_AXIS_H = 24
_SPAN_H = 16  # feed-timing / other duration brackets, between axis and lanes
_SPAN_COLOR = QColor("#1f7a4d")
_STATE_NAMES = ["Motor", "Lamp"]  # fixed order/rows for the on/off state band
_STATE_H = 14
_STATE_COLOR = QColor("#3a6fa5")


def lane_for_kind(kind: str) -> str:
    return _KIND_TO_LANE.get(kind, "unclassified")


def legend_html() -> str:
    """Rich-text legend for a QLabel placed above the timeline - answers
    "what does this color/shape mean" without a tooltip or a click."""
    chips = [f'<span style="color:{c.name()}">●</span> {name}' for name, c in LANE_COLORS.items()]
    chips.append(f'<span style="color:{_SPAN_COLOR.name()}">━</span> feed timing')
    chips += [f'<span style="color:{c.name()}">▲</span> {sev}' for sev, c in SEVERITY_COLORS.items()]
    return "&nbsp;&nbsp;".join(chips)


@dataclass
class ClusterPoint:
    t_center: float
    count: int
    indices: list[int] = field(default_factory=list)


def cluster_points(points: list[tuple[float, int]], bucket_s: float) -> list[ClusterPoint]:
    """Group ``(rel_s, index)`` points into fixed-width time buckets of
    ``bucket_s`` seconds - used to collapse many events that would land on
    the same pixel into one marker with a count, instead of an unreadable
    smear (or, at low event counts, a huge unnecessary paint cost).
    Pure function: no Qt, no widget state, independently testable.
    """
    if not points:
        return []
    if bucket_s <= 0:
        return [ClusterPoint(t, 1, [i]) for t, i in points]
    pts = sorted(points, key=lambda p: p[0])
    clusters: list[ClusterPoint] = []
    cur_bucket = int(pts[0][0] // bucket_s)
    cur_ts = [pts[0][0]]
    cur_idx = [pts[0][1]]
    for t, i in pts[1:]:
        b = int(t // bucket_s)
        if b == cur_bucket:
            cur_ts.append(t)
            cur_idx.append(i)
        else:
            clusters.append(ClusterPoint(sum(cur_ts) / len(cur_ts), len(cur_idx), cur_idx))
            cur_bucket, cur_ts, cur_idx = b, [t], [i]
    clusters.append(ClusterPoint(sum(cur_ts) / len(cur_ts), len(cur_idx), cur_idx))
    return clusters


class TimelineGraphView(QWidget):
    """rel_s=0 is always the left edge of the full recorded range, not of
    the current (possibly zoomed/panned) view."""

    event_selected = pyqtSignal(int)  # event index

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(
            _TOP_MARGIN + _AXIS_H + _SPAN_H + _STATE_H * len(_STATE_NAMES) + _LANE_H * len(LANES) + 10
        )
        self.setMouseTracking(True)
        self._points: dict[str, list[tuple[float, int]]] = {lane: [] for lane in LANES}
        self._markers: list[tuple[float, str]] = []  # (rel_s, label)
        self._anomalies: list[tuple[float, str, int]] = []  # (rel_s, severity, index)
        self._spans: list[tuple[float, float, str]] = []  # (start_s, end_s, label)
        self._states: dict[str, list[tuple[float, bool]]] = {name: [] for name in _STATE_NAMES}
        self._t_data_min: float | None = None
        self._t_data_max: float | None = None
        self._t_view_min = 0.0
        self._t_view_max = 1.0
        self._auto_follow = True
        self._drag_last_x: int | None = None
        self._hover_index: int | None = None

    # -- data ------------------------------------------------------------

    def clear(self) -> None:
        self._points = {lane: [] for lane in LANES}
        self._markers = []
        self._anomalies = []
        self._spans = []
        self._states = {name: [] for name in _STATE_NAMES}
        self._t_data_min = None
        self._t_data_max = None
        self._t_view_min = 0.0
        self._t_view_max = 1.0
        self._auto_follow = True
        self.update()

    def set_data(
        self,
        decoded_events: list[dict],
        phase_markers: list[dict] | None = None,
        anomalies: list | None = None,
        spans: list[dict] | None = None,
        states: dict[str, list[dict]] | None = None,
    ) -> None:
        """Post-hoc: render a whole saved run at once. ``decoded_events``
        entries carry ``raw_t0`` (absolute perf_counter, from usb/decode.py)
        rather than a pre-computed relative time - t_first anchors them to
        this run's own start, same convention as forensic_milestones.py /
        forensic_anomaly.py's ``_rel_s`` helper."""
        self.clear()
        t_first = next((e.get("raw_t0") for e in decoded_events if e.get("raw_t0") is not None), None)
        for i, ev in enumerate(decoded_events):
            t0 = ev.get("raw_t0")
            if t0 is None or t_first is None:
                continue
            self.append_event(t0 - t_first, ev.get("kind", ""), i)
        for m in phase_markers or []:
            if m.get("rel_s") is not None:
                self.append_marker(m["rel_s"], m["label"])
        for a in anomalies or []:
            rel_s = getattr(a, "rel_s", None) if not isinstance(a, dict) else a.get("rel_s")
            severity = getattr(a, "severity", None) if not isinstance(a, dict) else a.get("severity")
            index = getattr(a, "index", None) if not isinstance(a, dict) else a.get("index")
            if rel_s is not None and index is not None:
                self.append_anomaly(rel_s, severity or "info", index)
        for s in spans or []:
            if s.get("start_rel_s") is not None and s.get("end_rel_s") is not None:
                self.append_span(s["start_rel_s"], s["end_rel_s"], s.get("label", ""))
        for name, changes in (states or {}).items():
            for c in changes:
                if c.get("rel_s") is not None and c.get("value") is not None:
                    self.append_state_change(name, c["rel_s"], bool(c["value"]))
        self._auto_follow = False
        self._fit_all()

    def append_event(self, rel_s: float, kind: str, index: int) -> None:
        lane = lane_for_kind(kind)
        self._points[lane].append((rel_s, index))
        self._extend_range(rel_s)
        if self._auto_follow:
            self._follow_live(rel_s)
        self.update()

    def append_marker(self, rel_s: float, label: str) -> None:
        self._markers.append((rel_s, label))
        self._extend_range(rel_s)
        self.update()

    def append_anomaly(self, rel_s: float, severity: str, index: int) -> None:
        self._anomalies.append((rel_s, severity, index))
        self._extend_range(rel_s)
        self.update()

    def append_span(self, start_s: float, end_s: float, label: str) -> None:
        self._spans.append((start_s, end_s, label))
        self._extend_range(start_s)
        self._extend_range(end_s)
        self.update()

    def append_state_change(self, name: str, rel_s: float, value: bool) -> None:
        """Record a boolean signal transition (e.g. motor/lamp on-off) at
        ``rel_s``. Unrecognized ``name``s are ignored - ``_STATE_NAMES`` is
        the fixed set of rows this widget knows how to draw."""
        if name not in self._states:
            return
        self._states[name].append((rel_s, value))
        self._extend_range(rel_s)
        self.update()

    def _extend_range(self, t: float) -> None:
        self._t_data_min = t if self._t_data_min is None else min(self._t_data_min, t)
        self._t_data_max = t if self._t_data_max is None else max(self._t_data_max, t)
        if self._t_data_max - self._t_data_min < 0.001:
            self._t_data_max = self._t_data_min + 0.001

    def _follow_live(self, t: float) -> None:
        span = max(self._t_view_max - self._t_view_min, 1.0)
        self._t_view_max = t + span * 0.05
        self._t_view_min = self._t_view_max - span

    def fit_all(self) -> None:
        self._auto_follow = False
        self._fit_all()

    def _fit_all(self) -> None:
        self._t_view_min = self._t_data_min if self._t_data_min is not None else 0.0
        self._t_view_max = self._t_data_max if self._t_data_max is not None else 1.0
        self.update()

    def jump_to_live(self) -> None:
        self._auto_follow = True
        self._follow_live(self._t_data_max if self._t_data_max is not None else 0.0)
        self.update()

    # -- coordinate mapping ------------------------------------------------

    def _x_for_t(self, t: float, width: int) -> float:
        span = max(self._t_view_max - self._t_view_min, 1e-6)
        return (t - self._t_view_min) / span * width

    def _t_for_x(self, x: float, width: int) -> float:
        span = self._t_view_max - self._t_view_min
        return self._t_view_min + (x / max(width, 1)) * span

    def _lane_height(self) -> float:
        """Lane row height, stretched to fill whatever vertical space this
        widget is actually given (e.g. by a QSplitter) instead of leaving
        blank space below a fixed-size block of rows - never smaller than
        ``_LANE_H`` even in a cramped view."""
        fixed = _TOP_MARGIN + _AXIS_H + _SPAN_H + _STATE_H * len(_STATE_NAMES) + 10
        available = max(self.height() - fixed, _LANE_H * len(LANES))
        return available / len(LANES)

    def total_duration_s(self) -> float | None:
        """Full recorded span (last event minus first) - independent of
        the current zoom/pan, unlike ``_t_view_min``/``_t_view_max``."""
        if self._t_data_min is None or self._t_data_max is None:
            return None
        return self._t_data_max - self._t_data_min

    # -- painting ------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        height = self.height()
        painter.fillRect(self.rect(), QColor("#ffffff"))

        span = max(self._t_view_max - self._t_view_min, 1e-9)
        pixel_s = span / max(width, 1)

        # phase/button markers: vertical dashed lines + rotated label.
        # Labels are skipped (tick line stays) when they'd overlap the
        # previous one - a rotated label's own bounding box is awkward to
        # get exactly right, so horizontalAdvance is used as a conservative
        # stand-in for "would this collide," not a precise layout.
        painter.setFont(QFont("", 8))
        last_label_x: float | None = None
        for rel_s, label in self._markers:
            if not (self._t_view_min <= rel_s <= self._t_view_max):
                continue
            x = self._x_for_t(rel_s, width)
            pen = QPen(QColor("#aaaaaa"))
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(QPointF(x, _TOP_MARGIN), QPointF(x, height))
            shown = label[:40]
            text_w = painter.fontMetrics().horizontalAdvance(shown)
            if last_label_x is not None and x < last_label_x + text_w * 0.7:
                continue
            last_label_x = x
            painter.save()
            painter.translate(x + 2, _TOP_MARGIN - 6)
            painter.rotate(-30)
            painter.setPen(QColor("#555555"))
            painter.drawText(0, 0, shown)
            painter.restore()

        # axis
        painter.setPen(QColor("#333333"))
        painter.drawLine(0, _TOP_MARGIN + _AXIS_H, width, _TOP_MARGIN + _AXIS_H)
        n_ticks = max(2, width // 120)
        for i in range(n_ticks + 1):
            t = self._t_view_min + span * i / n_ticks
            x = self._x_for_t(t, width)
            painter.drawLine(QPointF(x, _TOP_MARGIN + _AXIS_H - 4), QPointF(x, _TOP_MARGIN + _AXIS_H))
            painter.drawText(QPointF(x + 2, _TOP_MARGIN + _AXIS_H - 6), format_timecode(t))

        # spans: duration brackets (e.g. feed-timing) between the axis and lanes
        span_y0 = _TOP_MARGIN + _AXIS_H
        span_mid = span_y0 + _SPAN_H / 2
        painter.setFont(QFont("", 7))
        for start_s, end_s, label in self._spans:
            if end_s < self._t_view_min or start_s > self._t_view_max:
                continue
            x1 = self._x_for_t(max(start_s, self._t_view_min), width)
            x2 = self._x_for_t(min(end_s, self._t_view_max), width)
            pen = QPen(_SPAN_COLOR)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(QPointF(x1, span_mid), QPointF(x2, span_mid))
            painter.drawLine(QPointF(x1, span_y0 + 2), QPointF(x1, span_y0 + _SPAN_H - 2))
            painter.drawLine(QPointF(x2, span_y0 + 2), QPointF(x2, span_y0 + _SPAN_H - 2))
            if label:
                painter.setPen(_SPAN_COLOR)
                painter.drawText(QPointF(x1 + 3, span_y0 + _SPAN_H - 4), label[:60])

        # states: on/off bars (Motor, Lamp) between spans and lanes
        state_y0 = _TOP_MARGIN + _AXIS_H + _SPAN_H
        painter.setFont(QFont("", 7))
        for si, name in enumerate(_STATE_NAMES):
            y = state_y0 + si * _STATE_H
            painter.setPen(QColor("#eeeeee"))
            painter.drawLine(0, y, width, y)
            painter.setPen(QColor("#999999"))
            painter.drawText(4, y + _STATE_H - 3, name)
            changes = sorted(self._states.get(name, []), key=lambda c: c[0])
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_STATE_COLOR)
            for (t0, val), (t1, _next_val) in zip(changes, changes[1:] + [(self._t_view_max, None)]):
                if not val or t1 < self._t_view_min or t0 > self._t_view_max:
                    continue
                x1 = self._x_for_t(max(t0, self._t_view_min), width)
                x2 = self._x_for_t(min(t1, self._t_view_max), width)
                painter.drawRect(QRectF(x1, y + 2, max(x2 - x1, 1.0), _STATE_H - 4))

        # lanes
        lane_y0 = state_y0 + _STATE_H * len(_STATE_NAMES)
        lane_h = self._lane_height()
        for li, lane in enumerate(LANES):
            y = lane_y0 + li * lane_h
            painter.setPen(QColor("#eeeeee"))
            painter.drawLine(0, int(y), width, int(y))
            painter.setPen(QColor("#999999"))
            painter.drawText(4, int(y + lane_h - 6), lane)

            visible = [(t, i) for t, i in self._points[lane] if self._t_view_min <= t <= self._t_view_max]
            color = LANE_COLORS[lane]
            for cluster in cluster_points(visible, pixel_s):
                x = self._x_for_t(cluster.t_center, width)
                cy = y + lane_h / 2
                r_max = max(3.0, lane_h / 2 - 2.0)
                r = min(3.0, r_max) if cluster.count == 1 else min(3.0 + cluster.count**0.5, r_max)
                painter.setBrush(color)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(QPointF(x, cy), r, r)
                if cluster.count > 1:
                    painter.setPen(QColor("#ffffff"))
                    painter.setFont(QFont("", 7))
                    painter.drawText(QRectF(x - r, cy - r, 2 * r, 2 * r), Qt.AlignmentFlag.AlignCenter, str(cluster.count))

        # anomalies: triangles above the lanes, near the axis
        visible_anomalies = [(t, sev, i) for t, sev, i in self._anomalies if self._t_view_min <= t <= self._t_view_max]
        for t, sev, idx in visible_anomalies:
            x = self._x_for_t(t, width)
            y = lane_y0 - 2
            color = SEVERITY_COLORS.get(sev, QColor("#888888"))
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            tri = QPolygonF([QPointF(x, y), QPointF(x - 5, y - 8), QPointF(x + 5, y - 8)])
            painter.drawPolygon(tri)

    # -- interaction ------------------------------------------------------------

    def wheelEvent(self, event: QWheelEvent) -> None:
        self._auto_follow = False
        factor = 0.85 if event.angleDelta().y() > 0 else 1.0 / 0.85
        cursor_t = self._t_for_x(event.position().x(), self.width())
        span = (self._t_view_max - self._t_view_min) * factor
        span = max(span, 1e-6)
        left_frac = (cursor_t - self._t_view_min) / max(self._t_view_max - self._t_view_min, 1e-9)
        self._t_view_min = cursor_t - left_frac * span
        self._t_view_max = self._t_view_min + span
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._auto_follow = False
            self._drag_last_x = int(event.position().x())
            index = self._index_near(event.position().x(), event.position().y())
            if index is not None:
                self.event_selected.emit(index)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_last_x is not None and event.buttons() & Qt.MouseButton.LeftButton:
            dx = int(event.position().x()) - self._drag_last_x
            self._drag_last_x = int(event.position().x())
            span = self._t_view_max - self._t_view_min
            dt = -dx / max(self.width(), 1) * span
            self._t_view_min += dt
            self._t_view_max += dt
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_last_x = None

    def _index_near(self, x: float, y: float) -> int | None:
        lane_y0 = _TOP_MARGIN + _AXIS_H + _SPAN_H + _STATE_H * len(_STATE_NAMES)
        lane_idx = int((y - lane_y0) // self._lane_height())
        if not (0 <= lane_idx < len(LANES)):
            return None
        lane = LANES[lane_idx]
        t_click = self._t_for_x(x, self.width())
        candidates = self._points[lane]
        if not candidates:
            return None
        nearest = min(candidates, key=lambda p: abs(p[0] - t_click))
        return nearest[1]
