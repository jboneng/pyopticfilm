# SPDX-License-Identifier: GPL-3.0-or-later
"""Background scan worker (never run USB I/O on the GUI thread)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal

from pyopticfilm.exceptions import ScanCancelled
from pyopticfilm.image import ScanImage
from pyopticfilm.usb.decode import decode_transaction, format_decoded_line
from pyopticfilm.usb.trace import UsbTransaction
from tools.scanlab.backend import (
    LabTarget,
    apply_lab_motor_acoustic,
    device_banner,
    format_scan_window_log,
    lab_scan_kwargs,
    open_lab_scanner,
    prescan_resolution,
    usb_log_divider,
    usb_log_line,
)
from tools.scanlab.forensic_anomaly import UNSAFE_ADDRESSES as _UNSAFE_ADDRESSES
from tools.scanlab.forensic_anomaly import detect_anomalies
from tools.scanlab.forensic_session import ForensicRun, ForensicRunResult
from tools.scanlab.forensic_timecode import format_timecode

# Status/probe registers used by the read-only forensic poll loop - the SAME
# primitives asic/gl128.py's own read_status()/read_request_register() use,
# not a reimplementation. See tools/register_reference.py for what 0x101/0x21
# mean and how confident the catalog is about each.
_REG_STATUS = 0x101
_FEED_PROBE_INDEX = 0x21

# How often (in recorded events) the live anomaly detector re-scans the
# rolling window - re-running the full rule set on every single event would
# be wasteful during a fast bulk image transfer; every _ANOMALY_CHECK_EVERY
# events is frequent enough for a human to notice within a fraction of a
# second while staying cheap.
_ANOMALY_CHECK_EVERY = 20
_ANOMALY_WINDOW = 200


@dataclass(frozen=True)
class ScanRequest:
    """Queued Scan payload. Geometry is computed on the GUI thread from crop_norm."""

    target: LabTarget
    dpi: int
    ir_pass: bool
    me_pass: bool
    apply_calib: bool
    #: None defers to the model's own default (Model.me_default_exposure_mode)
    #: at n_brackets > 2; always "adaptive" at n_brackets == 2 regardless.
    me_exposure_mode: str | None = None
    single_pass_exposure: int | None = None
    me_short_exposure: int | None = None
    me_long_exposure: int | None = None
    #: Clamped "manual" ME bracket target (held inside the model's own
    #: floor/ceiling envelope) — distinct from me_long_exposure, which is
    #: the raw, unrestricted Scan Lab debug override. Mutually exclusive
    #: with me_long_exposure (enforced by Scanner.scan()).
    me_target_exposure: int | None = None
    gl128_prime: bool | None = None
    crop_norm: tuple[float, float, float, float] | None = None
    scan_kw: dict[str, Any] | None = None
    n_brackets: int = 2


class ScanWorker(QObject):
    progress = pyqtSignal(float)
    status_changed = pyqtSignal(str)
    usb_line = pyqtSignal(str)
    banner = pyqtSignal(str)
    prescan_ready = pyqtSignal(object)
    scan_ready = pyqtSignal(object)
    me_debug_ready = pyqtSignal(object)
    failed = pyqtSignal(str)
    busy_changed = pyqtSignal(bool)
    calib_cleared = pyqtSignal(str)
    #: target, apply_calib, gl128_prime (bool or None for model default)
    request_prescan = pyqtSignal(object, bool, object)
    #: :class:`ScanRequest` (geometry already computed from crop_norm)
    request_scan = pyqtSignal(object)

    # --- Forensic tab -------------------------------------------------------
    #: decoded line for the live timeline view (raw txn always recorded to
    #: an active ForensicRun regardless of whether anything is connected to
    #: this signal - see _on_forensic_txn)
    forensic_line = pyqtSignal(str)
    #: rel_s, kind, index - one per decoded event, for the live graphical timeline
    forensic_timeline_event = pyqtSignal(float, str, int)
    #: rel_s, label - phase/button-press markers, same feed as phase_markers.jsonl
    forensic_timeline_marker = pyqtSignal(float, str)
    #: str(run.out_dir), is_mock - emitted the moment a session starts, so
    #: the GUI can point the Event Inspector at this run's files while
    #: still live, AND show the mock/real state immediately (manifest.json
    #: itself isn't written until the run finishes)
    forensic_run_started = pyqtSignal(str, bool)
    #: True only once ensure_open() has actually succeeded (not on request)
    forensic_connected = pyqtSignal(bool)
    forensic_status_ready = pyqtSignal(dict)
    forensic_register_result = pyqtSignal(dict)
    forensic_run_saved = pyqtSignal(str)
    forensic_error = pyqtSignal(str)
    #: one newly-detected Anomaly.to_json() dict - never re-emitted once seen
    forensic_anomaly = pyqtSignal(dict)
    #: target
    request_forensic_connect = pyqtSignal(object)
    request_forensic_disconnect = pyqtSignal()
    #: enabled, interval_ms
    request_forensic_poll = pyqtSignal(bool, int)
    #: address
    request_forensic_register_read = pyqtSignal(int)
    #: address, value, force
    request_forensic_register_write = pyqtSignal(int, int, bool)
    #: enabled, name
    request_forensic_recording = pyqtSignal(bool, str)
    #: label, details (dict; pass {} for none) - GUI-thread button presses
    #: recorded into the active ForensicRun's phase_markers.jsonl, if any
    request_forensic_mark_phase = pyqtSignal(str, dict)

    def __init__(self) -> None:
        super().__init__()
        self._target: LabTarget | None = None
        self._scanner: Any = None
        self._rec: Any = None
        self._last_align_shift_ir: tuple[float, float] | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._is_busy = False
        self._forensic_run: ForensicRun | None = None
        self._poll_timer: QTimer | None = None
        #: (enabled, interval_ms) last requested via the Live poll checkbox -
        #: distinct from the timer's current running state, so a scan's
        #: temporary pause can resume with the same settings afterward.
        self._forensic_poll_wanted: tuple[bool, int] = (False, 0)
        # Rolling buffer for the LIVE anomaly detector: bounded so a long
        # high-DPI scan can't grow this without limit - the FULL run is
        # always re-analyzed from disk in the Run browser regardless, this
        # is only for near-real-time feedback.
        self._forensic_live_events: list[dict] = []
        self._forensic_reported_anomaly_keys: set[tuple] = set()
        # Anchor for the Live timeline's timecode prefix - reset on connect
        # and on every new session start, so timecodes read "time since this
        # connection/session began" rather than an arbitrary process-wide
        # perf_counter origin.
        self._forensic_live_t0: float | None = None
        # Running count of decoded events since the current connection/
        # session began - matches decoded_events.jsonl's line numbers 1:1
        # for an active ForensicRun, so the live graphical timeline's marks
        # carry the same index the Event Inspector can look up later.
        self._forensic_live_index = 0
        # Deliberately NOT connected here. Per Qt's documented semantics, an
        # explicit `type=Qt.ConnectionType.QueuedConnection` should dispatch
        # to the receiver's thread at emit time regardless of when connect()
        # was called - but empirically, in this PyQt6 setup, connecting a
        # QObject's own signal to its own slot inside __init__ (i.e. before
        # the caller's moveToThread() runs) still delivered the slot call on
        # the emitting thread instead: verified with an isolated repro
        # (connect-before-moveToThread silently ignored the explicit type;
        # the identical connect() call made *after* moveToThread correctly
        # dispatched onto the worker's own thread). The exact root cause
        # inside PyQt6 wasn't tracked down further - this is a documented
        # empirical workaround, not a claim about how Qt is supposed to
        # behave in general. See connect_request_signals(), which the
        # caller must run after moveToThread()+start() for real queued
        # dispatch, and without which every request_*/run_prescan/run_scan
        # slot would silently execute on the GUI thread - defeating "never
        # run USB I/O on the GUI thread" (confirmed still happening before
        # this fix: a mock Prescan's click handler blocked the whole window
        # for its full duration).

    def connect_request_signals(self) -> None:
        """Wire every request_* signal to its slot with an explicit
        QueuedConnection. Call this AFTER moveToThread()+start() - see the
        note in __init__ for why connecting earlier silently breaks queued
        dispatch even with an explicit connection type."""
        self.request_prescan.connect(self.run_prescan, type=Qt.ConnectionType.QueuedConnection)
        self.request_scan.connect(self.run_scan, type=Qt.ConnectionType.QueuedConnection)
        self.request_forensic_connect.connect(self._forensic_connect, type=Qt.ConnectionType.QueuedConnection)
        self.request_forensic_disconnect.connect(self._forensic_disconnect, type=Qt.ConnectionType.QueuedConnection)
        self.request_forensic_poll.connect(self._forensic_set_poll, type=Qt.ConnectionType.QueuedConnection)
        self.request_forensic_register_read.connect(self._forensic_register_read, type=Qt.ConnectionType.QueuedConnection)
        self.request_forensic_register_write.connect(self._forensic_register_write, type=Qt.ConnectionType.QueuedConnection)
        self.request_forensic_recording.connect(self._forensic_set_recording, type=Qt.ConnectionType.QueuedConnection)
        self.request_forensic_mark_phase.connect(self._forensic_mark_phase, type=Qt.ConnectionType.QueuedConnection)

    @property
    def last_align_shift_ir(self) -> tuple[float, float] | None:
        return self._last_align_shift_ir

    def _on_usb(self, txn: UsbTransaction) -> None:
        self.usb_line.emit(usb_log_line(txn))
        self._on_forensic_txn(txn)

    def _on_forensic_txn(self, txn: UsbTransaction) -> None:
        """Decode + (if recording) persist every transaction on the shared
        connection - fires during ordinary Prescan/Scan too, not just while
        the forensic poll loop is running, so the Forensic tab's timeline
        shows real scan traffic decoded, for free."""
        decoded = decode_transaction(txn)
        decoded_json = decoded.to_json() if decoded is not None else None
        if self._forensic_live_t0 is None and txn.t0 is not None:
            self._forensic_live_t0 = txn.t0
        rel_s = (
            (txn.t0 - self._forensic_live_t0)
            if (txn.t0 is not None and self._forensic_live_t0 is not None)
            else None
        )
        base_line = format_decoded_line(decoded) if decoded is not None else usb_log_line(txn)
        self.forensic_line.emit(f"{format_timecode(rel_s)}  {base_line}")
        run = self._forensic_run
        if run is not None:
            run.record(txn.to_json(), decoded_json)

        if decoded_json is not None:
            if rel_s is not None:
                self.forensic_timeline_event.emit(rel_s, decoded_json.get("kind", ""), self._forensic_live_index)
            self._forensic_live_index += 1
            self._forensic_live_events.append(decoded_json)
            if len(self._forensic_live_events) > _ANOMALY_WINDOW:
                self._forensic_live_events = self._forensic_live_events[-_ANOMALY_WINDOW:]
            if len(self._forensic_live_events) % _ANOMALY_CHECK_EVERY == 0:
                self._forensic_check_anomalies()

    def _forensic_check_anomalies(self) -> None:
        try:
            found = detect_anomalies(self._forensic_live_events)
        except Exception as exc:  # noqa: BLE001 - never let detection crash a live session
            self.forensic_error.emit(f"Anomaly detection failed: {exc}")
            return
        for anomaly in found:
            key = anomaly.dedup_key()
            if key in self._forensic_reported_anomaly_keys:
                continue
            self._forensic_reported_anomaly_keys.add(key)
            self.forensic_anomaly.emit(anomaly.to_json())

    def _progress(self, value: float) -> None:
        self.progress.emit(float(value))

    def _on_status(self, status: str) -> None:
        if status == "priming":
            self._usb_divider("PRIMING")
        elif status == "prime_skipped":
            self._usb_divider("PRIMING SKIPPED (debug)")
        self.status_changed.emit(status)

    def _forensic_marker_rel_s(self) -> float:
        """Approximate rel_s for a marker at "now", on the SAME clock/anchor
        as forensic_timeline_event (self._forensic_live_t0) - not the
        ForensicRun's own separate monotonic clock, so live timeline marks
        and marker lines land on the same axis. The stored phase_markers.jsonl
        entry (written by ForensicRun.mark_phase) keeps its own exact rel_s
        regardless - this is only for live GUI positioning."""
        if self._forensic_live_t0 is None:
            return 0.0
        return time.perf_counter() - self._forensic_live_t0

    def _usb_divider(self, title: str) -> None:
        self.usb_line.emit("")
        self.usb_line.emit(usb_log_divider(title))
        run = self._forensic_run
        if run is not None:
            run.mark_phase(title)
            self.forensic_timeline_marker.emit(self._forensic_marker_rel_s(), title)

    def _forensic_mark_phase(self, label: str, details: dict) -> None:
        """GUI-thread button presses (Prescan/Scan clicked, image received)
        land here via request_forensic_mark_phase - the only safe way for
        app.py to write into the ForensicRun object that lives on this
        (worker) thread, same cross-thread pattern as every other forensic_*
        request signal in this class."""
        run = self._forensic_run
        if run is not None:
            run.mark_phase(label, details or None)
            self.forensic_timeline_marker.emit(self._forensic_marker_rel_s(), label)

    def close_scanner(self) -> None:
        # Do NOT touch self._poll_timer from this method: QTimer start/stop
        # must happen on the thread that owns it. _forensic_poll_tick (which
        # DOES run on the worker thread, as a QTimer.timeout slot) already
        # self-disables the timer once it sees the scanner is gone.
        with self._lock:
            scanner = self._scanner
            self._scanner = None
            self._target = None
            self._rec = None
        if scanner is not None:
            try:
                scanner.close()
            except Exception:  # noqa: BLE001, S110
                pass

    def ensure_open(self, target: LabTarget) -> Any:
        with self._lock:
            if self._scanner is not None and self._target == target:
                return self._scanner
        self.close_scanner()
        scanner, rec = open_lab_scanner(target, on_usb=self._on_usb)
        with self._lock:
            self._target = target
            self._scanner = scanner
            self._rec = rec
        self.banner.emit(device_banner(target))
        return scanner

    def clear_calib_cache(self) -> None:
        """Drop on-disk calib entries and close the scanner session."""
        with self._lock:
            scanner = self._scanner
        if scanner is None:
            self.calib_cleared.emit("")
            return
        calibrator = getattr(scanner, "_calibrator", None)
        path = ""
        if calibrator is not None:
            path = str(getattr(calibrator, "cache_path", None) or "")
            calibrator.clear()
        asic = getattr(scanner, "_asic", None)
        if asic is not None:
            asic.asic_shading_ready = False
        self.close_scanner()
        self.calib_cleared.emit(path)

    def cancel(self) -> None:
        self._cancel.set()

    def run_prescan(
        self,
        target: LabTarget,
        apply_calib: bool = False,
        gl128_prime: bool | None = None,
    ) -> None:
        self._run(
            target,
            kind="prescan",
            dpi=prescan_resolution(target.model),
            ir=False,
            me=False,
            crop=None,
            apply_calib=bool(apply_calib),
            gl128_prime=gl128_prime,
        )

    def run_scan(self, request: ScanRequest) -> None:
        self._run(
            request.target,
            kind="scan",
            dpi=request.dpi,
            ir=request.ir_pass,
            me=request.me_pass,
            crop=request.crop_norm,
            apply_calib=bool(request.apply_calib),
            # Preserve None (not "adaptive") — n_brackets > 2 defers to the
            # model's own default in that case; coercing to "adaptive" here
            # would silently override it, same as before this PR.
            me_exposure_mode=request.me_exposure_mode,
            single_pass_exposure=request.single_pass_exposure,
            me_short_exposure=request.me_short_exposure,
            me_long_exposure=request.me_long_exposure,
            me_target_exposure=request.me_target_exposure,
            gl128_prime=request.gl128_prime,
            scan_kw=request.scan_kw,
            n_brackets=int(request.n_brackets),
        )

    def _run(
        self,
        target: LabTarget,
        *,
        kind: str,
        dpi: int,
        ir: bool,
        me: bool,
        crop: tuple[float, float, float, float] | None,
        apply_calib: bool,
        me_exposure_mode: str | None = None,
        single_pass_exposure: int | None = None,
        me_short_exposure: int | None = None,
        me_long_exposure: int | None = None,
        me_target_exposure: int | None = None,
        gl128_prime: bool | None = None,
        scan_kw: dict[str, Any] | None = None,
        n_brackets: int = 2,
    ) -> None:
        self.busy_changed.emit(True)
        self._is_busy = True
        self._forensic_apply_poll(False, 0)  # never interleave poll reads with a real scan
        self._cancel.clear()
        self.progress.emit(0.0)
        try:
            scanner = self.ensure_open(target)
            apply_lab_motor_acoustic(scanner, target)
            if scan_kw is None:
                scan_kw = lab_scan_kwargs(target.model, dpi=dpi, kind=kind, crop_norm=crop)
            if kind == "prescan":
                self._usb_divider(f"PRESCAN {dpi} dpi")
                image = scanner.scan(
                    mode="color",
                    progress=self._progress,
                    cancel=self._cancel,
                    on_status=self._on_status,
                    apply_calib=apply_calib,
                    multi_exposure=me,
                    gl128_prime=gl128_prime,
                    **scan_kw,
                )
                self.prescan_ready.emit(image)
            else:
                self._usb_divider(f"SCAN {dpi} dpi")
                self.usb_line.emit(format_scan_window_log(crop, scan_kw))
                if me:
                    mode_label = me_exposure_mode or "model default"
                    brackets_label = f", {n_brackets} brackets" if n_brackets != 2 else ""
                    self._usb_divider(f"ME multi-pass ({mode_label}{brackets_label})")
                if ir:
                    self._usb_divider("IR pass")
                image: ScanImage = scanner.scan(
                    mode="color",
                    progress=self._progress,
                    cancel=self._cancel,
                    on_status=self._on_status,
                    apply_calib=apply_calib,
                    multi_exposure=me,
                    infrared=ir,
                    me_exposure_mode=me_exposure_mode,
                    single_pass_exposure=single_pass_exposure,
                    me_short_exposure=me_short_exposure,
                    me_long_exposure=me_long_exposure,
                    me_target_exposure=me_target_exposure,
                    gl128_prime=gl128_prime,
                    n_brackets=n_brackets,
                    **scan_kw,
                )
                self.me_debug_ready.emit(getattr(scanner, "last_me_debug", None))
                self._last_align_shift_ir = getattr(scanner, "last_align_shift_ir", None)
                self.scan_ready.emit(image)
        except ScanCancelled:
            self.busy_changed.emit(False)
            self.failed.emit("Scan cancelled")
        except Exception as exc:  # noqa: BLE001
            self.busy_changed.emit(False)
            self.failed.emit(str(exc))
        finally:
            self._is_busy = False
            self.busy_changed.emit(False)
            self.progress.emit(0.0)
            # Resume Live poll at whatever the user last asked for, if
            # anything - the scan-start pause above must not silently
            # leave polling off once the scan is done.
            self._forensic_apply_poll(*self._forensic_poll_wanted)

    # --- Forensic tab slots --------------------------------------------------

    def _forensic_connect(self, target: LabTarget) -> None:
        if self._is_busy:
            self.forensic_error.emit("Cannot connect: a scan is in progress.")
            return
        try:
            self.ensure_open(target)
        except Exception as exc:  # noqa: BLE001
            self.forensic_error.emit(str(exc))
            self.forensic_connected.emit(False)
            return
        self.forensic_connected.emit(True)
        self._forensic_live_t0 = time.perf_counter()
        # Connecting alone claims the device but sends zero USB traffic, so
        # without this the timeline/status dashboard stay empty until the
        # user separately enables Live poll or runs a Scan - one immediate
        # read gives instant feedback that the connection actually talks to
        # the scanner.
        self._forensic_poll_tick()

    def _forensic_disconnect(self) -> None:
        self._forensic_set_poll(False, 0)
        self.close_scanner()
        self.forensic_connected.emit(False)

    def _forensic_asic_protocol(self):
        with self._lock:
            scanner = self._scanner
        if scanner is None:
            self.forensic_error.emit("Not connected.")
            return None
        asic = getattr(scanner, "asic", None)
        protocol = getattr(asic, "protocol", None) if asic is not None else None
        if protocol is None:
            self.forensic_error.emit("Connected scanner has no accessible register protocol.")
            return None
        return protocol

    def _forensic_poll_tick(self) -> None:
        if self._is_busy:
            return  # a real scan started since the timer was armed - skip this tick, never interleave
        protocol = self._forensic_asic_protocol()
        if protocol is None:
            self._forensic_set_poll(False, 0)
            return
        try:
            status_raw = protocol.read_register(_REG_STATUS)
            probe_raw = protocol.read_request_register(_FEED_PROBE_INDEX)
        except Exception as exc:  # noqa: BLE001
            self.forensic_error.emit(f"Poll failed: {exc}")
            self._forensic_set_poll(False, 0)
            return
        from pyopticfilm.asic.status import ScannerStatus

        s = ScannerStatus.from_reg41(status_raw)
        self.forensic_status_ready.emit(
            {
                "status_raw": hex(status_raw),
                "probe_raw": hex(probe_raw),
                "is_at_home": s.is_at_home,
                "is_motor_enabled": s.is_motor_enabled,
                "is_lamp_on": s.is_lamp_on,
                "is_front_end_busy": s.is_front_end_busy,
                "is_feeding_finished": s.is_feeding_finished,
                "is_scanning_finished": s.is_scanning_finished,
                "is_buffer_empty": s.is_buffer_empty,
            }
        )

    def _forensic_set_poll(self, enabled: bool, interval_ms: int) -> None:
        """User-requested poll state, from the Forensic tab's Live poll
        checkbox (or a disconnect/poll-failure deciding to turn it off).
        Remembered separately from the timer's current running state so a
        scan's temporary pause (_forensic_apply_poll, which does not touch
        this) can resume with the same settings afterward."""
        self._forensic_poll_wanted = (enabled, interval_ms)
        self._forensic_apply_poll(enabled, interval_ms)

    def _forensic_apply_poll(self, enabled: bool, interval_ms: int) -> None:
        if self._poll_timer is None:
            self._poll_timer = QTimer(self)
            self._poll_timer.timeout.connect(self._forensic_poll_tick)
        self._poll_timer.stop()
        if enabled and interval_ms > 0:
            self._poll_timer.start(interval_ms)

    def _forensic_register_read(self, address: int) -> None:
        protocol = self._forensic_asic_protocol()
        if protocol is None:
            return
        try:
            value = protocol.read_register(address)
        except Exception as exc:  # noqa: BLE001
            self.forensic_error.emit(f"Register read failed: {exc}")
            return
        self.forensic_register_result.emit(
            {"op": "read", "address": hex(address), "value": hex(value)}
        )

    def _forensic_register_write(self, address: int, value: int, force: bool) -> None:
        addr8 = address & 0xFF
        if addr8 in _UNSAFE_ADDRESSES and not force:
            self.forensic_error.emit(
                f"Refused: 0x{address:04x} is motion/lamp-adjacent "
                f"({_UNSAFE_ADDRESSES[addr8]}). Check 'I understand the risk' to override."
            )
            return
        protocol = self._forensic_asic_protocol()
        if protocol is None:
            return
        try:
            status_raw = protocol.read_register(_REG_STATUS)
            from pyopticfilm.asic.status import ScannerStatus

            if ScannerStatus.from_reg41(status_raw).is_motor_enabled and not force:
                self.forensic_error.emit(
                    "Refused: scanner reports motor_enabled=True (not idle). "
                    "Check 'I understand the risk' to override."
                )
                return
            before = protocol.read_register(address)
            protocol.write_register(address, value)
            after = protocol.read_register(address)
        except Exception as exc:  # noqa: BLE001
            self.forensic_error.emit(f"Register write failed: {exc}")
            return
        self.forensic_register_result.emit(
            {
                "op": "write",
                "address": hex(address),
                "value_written": hex(value),
                "readback_before": hex(before),
                "readback_after": hex(after),
                "matched": after == (value & 0xFF),
            }
        )

    def _forensic_set_recording(self, enabled: bool, name: str) -> None:
        if enabled:
            if self._forensic_run is not None:
                return
            with self._lock:
                scanner = self._scanner
            device_info = None
            if scanner is not None:
                handle = getattr(scanner, "_handle", None) or getattr(scanner, "handle", None)
                info = getattr(handle, "info", None)
                if info is not None:
                    device_info = {
                        "device_id": info.device_id,
                        "vendor_id": hex(info.vendor_id),
                        "product_id": hex(info.product_id),
                    }
            # self._target (set by ensure_open) is the authoritative source
            # for mock vs. real - checking it explicitly here, rather than
            # only inferring from device_info, is what prevents a mock run
            # ever being mistaken for live real-hardware idle traffic.
            if self._target is not None:
                device_info = {**(device_info or {}), "mock": self._target.mock}
            self._forensic_run = ForensicRun(
                name=name or "scanlab-session",
                device_info=device_info,
            )
            self.forensic_run_started.emit(str(self._forensic_run.out_dir), bool(self._target and self._target.mock))
            self._forensic_live_index = 0
            self._forensic_live_events = []
            self._forensic_reported_anomaly_keys = set()
            self._forensic_live_t0 = time.perf_counter()
        else:
            run = self._forensic_run
            self._forensic_run = None
            if run is not None:
                out_dir = run.finish(ForensicRunResult(outcome="success", notes="Manual stop from Forensic tab."))
                self.forensic_run_saved.emit(str(out_dir))
