# SPDX-License-Identifier: GPL-3.0-or-later
"""GL128 driver for the OpticFilm 135i film loader.

Same chip, register map and boot path as :class:`~pyopticfilm.asic.gl128.Gl128`
(the model hooks select the 135i boot). What differs, all from the VueScan
usbmon captures of a 135i and the Plustek model INI
(``docs/gl128-model-comparison.md`` §11):

* **Moves** drive the film holder, not a carriage. Every move programs a
  255-step ramp at each end (``0xAE``-``0xAF``) and ``FEEDL = n - 510``, and
  ends on the ``0x101`` status alone (no ``wIndex = 0x21`` probe). Loading or
  homing a holder is one inch forward then a reverse seek of up to ten inches
  to the loader sensor (``HOMESNR``); a frame is one forward feed from there;
  the image pass parks with ``AGOHOME``. There is no host-side motor stop:
  only the armed loader sensor ends a reverse seek early.
* **Sensors**: ``HOMESNR`` reads the loaded position while ``0x32`` bit 1 is
  clear (stop armed) and "holder in the slot" while it is set, so every check
  sets bit 1 for the reading it needs. ``0x32`` bit 2 follows it, armed too.
* **Light**: the white lamp is ``0x03`` bit 4, the IR LED ``0x32`` bit 5 and
  the toggle bit ``0x19`` bit 3. ``0x37`` is never written after boot.
  ``0x32`` also carries the timing-set bit (7) and the loader outputs (0-4),
  so only the bits that change are written, read-modify-write.
* **Toggle mode**: every scan line is an IR line then a visible line. The
  calibration strips are VueScan's (two-line AFE strips, 256-line dark and
  stationary white), and shading fills two banks.
* **Depth**: ``0x33`` stays ``0x0E`` and ``0xAF`` is the ramp count, so the
  family ``DEPTH16`` pair is never written.
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from pyopticfilm.asic.gl128 import (
    _CALIB_STRIP_A6_A9,
    _STATIONARY_IDLE_WAIT_S,
    Gl128,
    _encode_offsets,
)
from pyopticfilm.asic.status import ScannerStatus
from pyopticfilm.device.protocol import Gl128Model
from pyopticfilm.device.tables_135i import (
    EJECT_EVENT,
    EJECT_LED_REGS,
    EJECT_REG_09,
    HOLDER_SENSOR_REG,
    LOADER_DELAY_S,
    LOADER_EJECT_STEPS,
    LOADER_HOME_SENSOR_BIT,
    LOADER_LOAD_FORWARD_STEPS,
    LOADER_LOAD_REVERSE_STEPS,
    LOADER_RECOVERY_SEEK_STEPS,
    LPERIOD_BASE,
    STATUS2_MOTMFLG,
    STATUS2_REG,
    WINDOW_ORIGIN,
    exposure_registers,
    feedl_for_move,
    ramp_steps_for_move,
)
from pyopticfilm.exceptions import AsicError, MotorTimeoutError, ScanError
from pyopticfilm.logging import get_logger
from pyopticfilm.scan.calib_gl128 import (
    AFE_BLACK_TARGET_COLOR,
    AFE_FALLBACK_OFFSETS_COLOR,
    AFE_FALLBACK_PRE_OFFSETS,
    AFE_OFFSET_PROBE_HIGH,
    AFE_OFFSET_PROBE_LOW,
    AFE_PRE_OFFSET_BLACK,
    AFE_WHITE_TARGETS_COLOR,
    COLOR_SHADING_DARK_SETTLE_S,
    AfeFrontend,
    coarse_gains_from_wide_means,
    offsets_for_black_level,
    shading_columns_mean,
    validate_color_shading_strip,
)
from pyopticfilm.scan.calib_gl128_loader import (
    AFE_DPISET,
    AFE_PROBE_CLOCKS,
    AFE_PROBE_PIXELS,
    AFE_STRIP_LINES,
    AFE_WIDE_CLOCKS,
    AHB_SHADING_IR,
    AHB_SHADING_VISIBLE,
    SHADING_LINES,
    SHADING_WHITE_TARGET_IR,
    SHADING_WHITE_TARGET_VISIBLE,
    lit_channel_means,
    measured_shading_table,
    optical_black_means,
    shading_gains_from_white,
    split_shading_blob,
    strip_bytes,
    strip_column_means,
    strip_line_period,
    unity_shading_table,
    validate_gains,
    validate_ir_white,
)

logger = get_logger(__name__)

#: IR LED (``IRMASK``), timing-set bit, toggle bit and shutter bit.
_IR_LED_BIT = 0x20
_TIMING_BIT = 0x80
_REG_TOGGLE = 0x19
_TOGGLE_BIT = 0x08
_REG_SHUTTER = 0x15
_SHUTTER_BIT = 0x10
_REG_09 = 0x09
#: Registers the light state owns: never rewritten from a register table.
LIVE_REGS: frozenset[int] = frozenset({0x03, HOLDER_SENSOR_REG, 0x37})

#: A move's own registers; a frame feed takes everything else from the image
#: pass, as VueScan's do.
_MOVE_OWN_REGS: frozenset[int] = frozenset(
    {0x02, 0x0D, 0x0F, 0x25, 0x26, 0x27, 0x3D, 0x3E, 0x3F, 0xAE, 0xAF}
)
#: Frame-feed values that differ from the image pass's (every positioning
#: feed in the previews and the 7200 dpi scan).
_FRAME_FEED_REGS: dict[int, int] = {0x01: 0x22, 0x05: 0x48, 0xA6: 0x00, 0xA7: 0x00, 0xA8: 0x00, 0xA9: 0x00}
#: Load reverse seek (``insert 35mm carrier``, second feed): ``0x1C = 0``,
#: exposure 30000 and ``0x8A``-``0x92`` cleared.
_REVERSE_SEEK_REGS: dict[int, int] = {
    0x1C: 0x00, 0x7D: 0x00, 0x7E: 0x75, 0x7F: 0x30,
    **{a: 0x00 for a in range(0x8A, 0x93)},
}
#: Calibration strip registers common to the AFE, dark and white strips.
_STRIP_REGS: dict[int, int] = {
    0x02: 0x00, 0x04: 0x42, 0x05: 0x40, 0x1C: 0x00, 0x1D: 0x00,
    0xA2: 0x00, 0xA4: 0x00, 0xAA: 0x00, 0xAC: 0x00, 0xAD: 0x01, 0xAE: 0x00, 0xAF: 0x00,
}
#: ``0x01`` while a strip is set up: ``SHDAREA`` (AFE, dark) or
#: ``SHDAREA | DVDSET`` (white). START sets ``SCAN`` on top.
_STRIP_REG01 = 0x02
_STRIP_REG01_DVDSET = 0x22

#: Image-pass end (``64bit rgbi 35mm film preview``, after each frame): lamp
#: pair, clear SCAN, shutter off, AGOHOME, lamp off. VueScan also writes two
#: GPIO requests here; they only drive the front-panel LEDs.
_END_LAMP_PRE: tuple[int, ...] = (0x30, 0x20)
_END_LAMP_POST: tuple[int, ...] = (0x10, 0x00)
_END_REG01 = 0x22
#: Interval between status samples while a move runs.
_MOVE_POLL_S = 0.015


class Gl128Loader(Gl128):
    """GL128 film-loader driver (OpticFilm 135i)."""

    def __init__(self, protocol, model: Gl128Model) -> None:  # type: ignore[no-untyped-def]
        super().__init__(protocol, model)
        #: Toggle mode keeps the visible and IR halves of every line apart, so
        #: USB is always chunky per line.
        self.usb_planar_rgb = False
        #: Holder sensor state at the last :meth:`poll_loader` (``None``: not polled).
        self._holder_seen: bool | None = None

    # --- light ------------------------------------------------------------

    def _write_bits(self, address: int, mask: int, value: int) -> int:
        """Read-modify-write ``mask`` bits of ``address`` to ``value``; skip a no-op."""
        current = int(self.protocol.read_register(address)) & 0xFF
        new = (current & ~mask & 0xFF) | (value & mask)
        if new != current:
            self._write(address, new)
        else:
            self._reg_cache[address] = new
        return new

    def _apply_infrared(self, *, enabled: bool) -> None:
        """IR LED on ``0x32`` bit 5; ``0x37`` keeps its boot value."""
        self._write_bits(HOLDER_SENSOR_REG, _IR_LED_BIT, _IR_LED_BIT if enabled else 0)

    def _set_toggle(self, *, enabled: bool) -> None:
        self._write_bits(_REG_TOGGLE, _TOGGLE_BIT, _TOGGLE_BIT if enabled else 0)

    def lamp_on(self) -> None:
        """White lamp, IR LED and toggle bit on: every pass is RGB+IR.

        The scan method does not change the light: the visible and IR lines
        of one pass need both sources (VueScan: ``03=30``, ``19`` bit 3,
        ``32`` bit 5 on every lit pass).
        """
        r = self.registers
        self._write(r.REG_0x03, r.XPASEL | r.LAMPPWR)
        self._set_toggle(enabled=True)
        self._apply_infrared(enabled=True)
        logger.info("GL128 loader lamp + IR LED on (toggle)")

    def lamp_off(self) -> None:
        if not self._initialized:
            logger.debug("GL128 loader lamp_off before init — nothing to do")
            return
        self._reassert_lamp_off()
        logger.info("GL128 loader lamp off")

    def _reassert_lamp_off(self) -> None:
        """Lamp, toggle bit and IR LED off: the dark strip state (``03=20 19=00 32`` bit 5 clear)."""
        r = self.registers
        self._write(r.REG_0x03, r.XPASEL)
        self._set_toggle(enabled=False)
        self._apply_infrared(enabled=False)

    def _strike_lamp_on(self) -> None:
        self.lamp_on()

    def apply_timing_gpio(self, resolution: int) -> None:
        """``0x32`` bit 7 for the timing set of ``resolution`` (``Speed2`` sets it)."""
        masked = self.model.scan_masked_regs_for(resolution)
        mask, value = masked.get(HOLDER_SENSOR_REG, (0, 0))
        mask &= _TIMING_BIT
        if mask:
            self._write_bits(HOLDER_SENSOR_REG, mask, value)

    # --- holder -----------------------------------------------------------

    def holder_present(self) -> bool:
        """True while a film holder is in the loader slot, wherever it sits.

        Reads ``HOMESNR`` with the stop disarmed: armed, it and ``0x32`` bit 2
        read 0 for a holder off the loader home. Idle only, since the read
        briefly disarms the stop.
        """
        if not self._initialized:
            self.init()
        return self._homesnr(armed=False)

    def _set_home_stop(self, *, armed: bool) -> None:
        """``0x32`` bit 1: clear arms the loader stop (``HOMESNR`` = loaded position), set disarms it."""
        self._write_bits(HOLDER_SENSOR_REG, LOADER_HOME_SENSOR_BIT, 0 if armed else LOADER_HOME_SENSOR_BIT)

    def _homesnr(self, *, armed: bool) -> bool:
        """``HOMESNR`` with the stop armed (loaded position) or not (holder in the slot).

        ``0x32`` bit 1 is restored afterwards.
        """
        before = int(self.protocol.read_register(HOLDER_SENSOR_REG)) & LOADER_HOME_SENSOR_BIT
        self._set_home_stop(armed=armed)
        try:
            return self.read_status_reliable().is_at_home
        finally:
            self._write_bits(HOLDER_SENSOR_REG, LOADER_HOME_SENSOR_BIT, before)

    def is_at_home(self) -> bool:
        """True while the holder sits at the loader home (``HOMESNR`` with the stop armed)."""
        return self._homesnr(armed=True)

    def _require_carriage_at_home(self, where: str) -> None:
        if not self.is_at_home():
            raise ScanError(
                f"{where}: the film holder is not at the loader home. Load it again "
                "(Scanner.load_holder) or eject and reinsert it."
            )

    def _motor_idle(self, status: ScannerStatus) -> bool:
        """Motor off on both status registers (``0x101`` bit 0 and ``0x100`` bit 1)."""
        if status.is_motor_enabled:
            return False
        return not int(self.protocol.read_register(STATUS2_REG)) & STATUS2_MOTMFLG

    def eject_requested(self, timeout_ms: int = 10) -> bool:
        """True when the interrupt endpoint reports the eject button (``0x48``).

        ``timeout_ms`` must be positive: libusb waits forever on 0.
        """
        data = self.protocol.read_intr(timeout_ms=max(1, int(timeout_ms)))
        return bool(data) and data[0] == EJECT_EVENT

    def poll_loader(self) -> str | None:
        """One idle sample of the loader: ``"inserted"``, ``"removed"``, ``"eject"`` or ``None``.

        The holder sensor (:meth:`holder_present`) going present is an
        insert and going absent a removal; interrupt byte ``0x48`` is the
        eject button (``holder eject``). The first call only records the
        sensor state.
        """
        present = self.holder_present()
        previous = self._holder_seen
        self._holder_seen = present
        if self.eject_requested():
            return "eject"
        if previous is None or present == previous:
            return None
        return "inserted" if present else "removed"

    def require_holder_loaded(self, where: str) -> None:
        """Refuse a scan unless a holder is in and parked at the loader home."""
        if not self.holder_present():
            raise ScanError(f"{where}: no film holder in the loader. Insert a holder and load it first.")
        self._require_carriage_at_home(where)

    def _feed_in_then_seek_home(self, *, timeout_s: float) -> bool:
        """The vendor's home: arm the stop, one inch in, then the ten-inch reverse seek.

        A reverse seek stops only on the armed sensor's edge, and only when it
        starts off the sensor; from the outward side it gets no edge and runs
        its full 72000 steps toward the entry. So the seek runs only when
        ``HOMESNR`` reads 0 after the inward inch, with the motor idle.
        Returns False, without seeking, when ``HOMESNR`` stayed set.
        """
        self._set_home_stop(armed=True)
        self._loader_move(LOADER_LOAD_FORWARD_STEPS, use_slow_slope=True, timeout_s=timeout_s)
        status = self.read_status_reliable()
        if status.is_at_home:
            return False
        if not self._motor_idle(status):
            raise MotorTimeoutError("Loader motor still running after the one-inch feed; reverse seek not started.")
        self._seek_home(timeout_s=timeout_s)
        return True

    def load_holder(self, *, timeout_s: float = 60.0) -> None:
        """Pull an inserted holder in and seek back to the loader sensor.

        ``insert 35mm carrier``: 0.5 s after the holder sensor changes, clear
        ``0x32`` bit 1, move 7200 steps forward (load ramp), then reverse with
        ``MTRREV`` until ``HOMESNR``. If the loader sensor does not clear on
        the forward move, the holder is ejected again.
        """
        self._require_motor_enabled()
        if not self._initialized:
            self.init()
        if not self.holder_present():
            raise AsicError("No film holder in the loader: insert one, then load it.")
        if not self._homesnr(armed=False):
            raise AsicError("Loader sensor does not see the holder; eject it and insert it again.")
        time.sleep(LOADER_DELAY_S)
        if not self._feed_in_then_seek_home(timeout_s=timeout_s):
            logger.warning("GL128 loader sensor did not clear on the load feed; ejecting the holder")
            self.eject_holder(timeout_s=timeout_s)
            raise AsicError("Film holder did not load (loader sensor stayed set); it was ejected.")
        logger.info("GL128 film holder loaded at the loader home")

    def eject_holder(self, *, timeout_s: float = 30.0) -> None:
        """Push the holder out, in the vendor's order (``holder eject``, QuickScan).

        1. arm the stop (``0x32`` bit 1 clear) and, off the loader home, seek
           back to it straight away (no inward inch: that is the generic home);
        2. holder LED (``36=fc``, ``33=8e``);
        3. disarm the stop: ``HOMESNR`` now reads "holder in the slot";
        4. only with a holder in: ``09 |= 08``, 3600 steps forward, ``09=00``.

        From the loader home 3600 steps bring the holder out; from a frame
        position they would not. The seek reaches the whole loader travel,
        since a failed park can leave the holder at the end of a strip.
        """
        self._require_motor_enabled()
        if not self._initialized:
            self.init()
        if not self._homesnr(armed=False):
            logger.info("GL128 loader eject: no holder in the slot")
            return
        self._set_home_stop(armed=True)
        if not self.read_status_reliable().is_at_home:
            self._seek_home(steps=LOADER_RECOVERY_SEEK_STEPS, timeout_s=timeout_s)
        for address, value in EJECT_LED_REGS:
            self._write(address, value)
        self._set_home_stop(armed=False)
        if not self.read_status_reliable().is_at_home:
            logger.warning("GL128 loader eject: holder no longer seen in the slot")
            return
        self._write_bits(_REG_09, EJECT_REG_09, EJECT_REG_09)
        try:
            self._loader_move(LOADER_EJECT_STEPS, timeout_s=timeout_s)
        finally:
            self._write(_REG_09, 0x00)
        logger.info("GL128 film holder ejected")

    # --- motion -----------------------------------------------------------

    def _move_regs(self, steps: int, *, reverse: bool = False) -> dict[int, int]:
        r = self.registers
        ramp = ramp_steps_for_move(steps)
        feedl = feedl_for_move(steps)
        motor = r.MTRPWR | r.FASTFED | (r.MTRREV if reverse else 0)
        return {
            r.REG_0x02: motor,
            0xAE: (ramp >> 8) & 0xFF,
            0xAF: ramp & 0xFF,
            0x3D: (feedl >> 16) & 0xFF,
            0x3E: (feedl >> 8) & 0xFF,
            0x3F: feedl & 0xFF,
        }

    def _start_move(self, regs: Mapping[int, int], *, use_slow_slope: bool) -> None:
        r = self.registers
        self.wait_motor_idle("before a loader move")
        self._write_many(dict(regs))
        self._upload_fast_slopes(use_slow=use_slow_slope)
        self._write(r.REG_START, r.START_GO)

    def _wait_move(self, *, timeout_s: float, until_home: bool, steps: int) -> ScannerStatus:
        """Poll ``0x101`` until the move ends: ``FEEDFSH`` (or ``HOMESNR``) with the motor off.

        A completion counts only after ``MOTORENB`` has been seen: the status
        bits of the previous move are still set when START is written.
        """
        deadline = time.monotonic() + timeout_s
        moving_seen = False
        started = time.monotonic()
        status: ScannerStatus | None = None
        while time.monotonic() < deadline:
            status = self.read_status_reliable()
            if status.is_motor_enabled:
                moving_seen = True
            elif moving_seen or time.monotonic() - started > _STATIONARY_IDLE_WAIT_S:
                if until_home and status.is_at_home:
                    return status
                if not until_home and status.is_feeding_finished:
                    return status
                if until_home and status.is_feeding_finished:
                    raise MotorTimeoutError(
                        f"Loader reverse seek ran its full {steps} steps without reaching "
                        "the loader sensor. Eject the holder and insert it again."
                    )
            time.sleep(_MOVE_POLL_S)
        # No host write is known to stop a 135i move (clearing MTRPWR may not),
        # so the motor is not treated as stopped and the position is unknown.
        self._park_ok = False
        raw = "?" if status is None else f"0x{int(status.raw) & 0xFF:02x}"
        logger.error("GL128 loader move of %d steps timed out after %.0fs (status=%s)", steps, timeout_s, raw)
        raise MotorTimeoutError(
            f"Loader move of {steps} steps did not finish within {timeout_s:.0f}s (status={raw}). "
            "The motor may still be running; wait for it to stop, then eject the holder and load it again."
        )

    def _loader_move(self, steps: int, *, use_slow_slope: bool = False, timeout_s: float = 30.0) -> None:
        """Forward move of ``steps`` (``0x02 = 0x18``) with no other register changes."""
        self._start_move(self._move_regs(steps), use_slow_slope=use_slow_slope)
        end = self._wait_move(timeout_s=timeout_s, until_home=False, steps=steps)
        logger.info("GL128 loader moved %d steps (status 0x%02x)", steps, int(end.raw) & 0xFF)

    def _seek_home(self, *, steps: int = LOADER_LOAD_REVERSE_STEPS, timeout_s: float = 30.0) -> None:
        """Reverse seek to the loader sensor (``insert 35mm carrier``, second feed)."""
        regs = dict(_REVERSE_SEEK_REGS)
        regs.update(self._move_regs(steps, reverse=True))
        self._start_move(regs, use_slow_slope=True)
        self._wait_move(timeout_s=timeout_s, until_home=True, steps=steps)
        self._park_ok = True
        logger.info("GL128 loader at home")

    def feed(self, steps: int, *, timeout_s: float = 30.0) -> None:
        """Forward loader move of ``steps`` (holder travel, FEED ramp)."""
        self._require_motor_enabled()
        self._loader_move(int(steps), timeout_s=timeout_s)

    def position_for_frame(
        self,
        *,
        feed_steps: int,
        scan_regs: Mapping[int, int],
        timeout_s: float = 30.0,
    ) -> None:
        """From the loader home, feed ``feed_steps`` (``FEEDL``) to the scan start.

        VueScan writes ``FEEDL`` = the frame feed itself (6438, 17190, 27942)
        with the 255-step ramps, under the image pass's registers.
        """
        self._require_motor_enabled()
        if not self._park_ok:
            raise AsicError(
                "GL128 loader park failed in the previous scan (AGOHOME timed out). "
                "Eject the holder and load it again before scanning."
            )
        # The feed and the AGOHOME park after the image pass both run armed.
        self._set_home_stop(armed=True)
        start = self.read_status_reliable()
        if not start.is_at_home:
            raise AsicError("Film holder is not at the loader home; load it again before scanning.")
        r = self.registers
        steps = int(feed_steps)
        regs = {a: v for a, v in scan_regs.items() if a not in _MOVE_OWN_REGS and a not in LIVE_REGS}
        regs.update(_FRAME_FEED_REGS)
        ramp = ramp_steps_for_move(steps)
        regs.update(
            {
                r.REG_0x02: r.MTRPWR | r.FASTFED,
                0xAE: (ramp >> 8) & 0xFF,
                0xAF: ramp & 0xFF,
                0x3D: (steps >> 16) & 0xFF,
                0x3E: (steps >> 8) & 0xFF,
                0x3F: steps & 0xFF,
            }
        )
        logger.info("GL128 loader frame feed FEEDL=%d from home (status 0x%02x)", steps, start.raw)
        self._start_move(regs, use_slow_slope=False)
        end = self._wait_move(timeout_s=timeout_s, until_home=False, steps=steps)
        if end.is_at_home:
            raise AsicError(
                f"GL128 loader still reads at home after a {steps}-step frame feed; "
                "the holder likely did not move."
            )

    def position_for_full_frame_scan(
        self,
        *,
        scan_steps: int | None = None,
        timeout_s: float = 60.0,
        scan_regs: Mapping[int, int] | None = None,
    ) -> None:
        """Loader models feed once from home: :meth:`position_for_frame`."""
        steps = int(self.model.feed_to_scan_steps) if scan_steps is None else int(scan_steps)
        self.position_for_frame(feed_steps=steps, scan_regs=dict(scan_regs or {}), timeout_s=timeout_s)

    def stop_motor(self) -> None:
        """End an image pass the way VueScan does, then wait for the AGOHOME park."""
        r = self.registers
        if not self._initialized:
            logger.debug("GL128 loader stop_motor before init — nothing to do")
            return
        try:
            reg02 = self._reg_cache.get(r.REG_0x02)
            if reg02 is None:
                reg02 = int(self.protocol.read_register(r.REG_0x02))
            # The init table's 0x02 = 0x78 already has AGOHOME: without an image
            # pass there is nothing to end and no park to start.
            if not reg02 & r.AGOHOME or not self.image_pass_since_init:
                self._update_bits(r.REG_0x01, clear_bits=r.SCAN)
                return
            for value in _END_LAMP_PRE:
                self._write(r.REG_0x03, value)
            self._write(r.REG_0x01, _END_REG01)
            self._write_bits(_REG_SHUTTER, _SHUTTER_BIT, 0)
            self._set_home_stop(armed=True)
            self._write(r.REG_0x02, r.MTRPWR | r.AGOHOME)
            for value in _END_LAMP_POST:
                self._write(r.REG_0x03, value)
            self._apply_infrared(enabled=False)
            self._set_toggle(enabled=False)
            try:
                self.wait_until_at_home(timeout_s=60.0)
                self._reg_cache[r.REG_0x02] = int(reg02) & ~r.AGOHOME
                logger.info("GL128 loader parked at home after the scan")
            except MotorTimeoutError as exc:
                self._park_ok = False
                logger.error("GL128 loader did not park at home: %s", exc)
        except Exception as exc:  # noqa: BLE001
            self._park_ok = False
            logger.error("GL128 loader stop_motor: %s (holder position unknown)", exc)

    def home(self, *, timeout_s: float = 30.0, wait: bool = True) -> None:
        """Bring a holder in the slot back to the loader home.

        Normally :meth:`_feed_in_then_seek_home`. After a lost park the holder
        may sit at the end of a strip, where another inch would pass the
        captured travel, so it seeks straight back.
        """
        del wait
        self._require_motor_enabled()
        if self.is_at_home():
            logger.debug("GL128 loader already at home")
            return
        if not self._homesnr(armed=False):
            logger.info("GL128 loader home: no holder in the slot, nothing to move")
            return
        if not self._park_ok:
            self._set_home_stop(armed=True)
            self._seek_home(steps=LOADER_RECOVERY_SEEK_STEPS, timeout_s=timeout_s)
            return
        if not self._feed_in_then_seek_home(timeout_s=timeout_s):
            raise AsicError(
                "Loader sensor stayed set after the one-inch feed; holder position unknown. "
                "Eject the holder and load it again."
            )

    def park(self, *, timeout_s: float = 30.0) -> None:
        self.home(timeout_s=timeout_s)
        self.lamp_off()

    # --- re-init ----------------------------------------------------------

    def reinit_before_stationary_strips(self) -> None:
        """Rewrite the register table and AFE defaults after an image pass.

        VueScan does this before every frame (register blast, frontend
        defaults, scan clock). There is no DRAM toggle, and the shading banks
        survive it: frames 2 and 3 of the preview reuse frame 1's tables.
        """
        if not self.image_pass_since_init:
            return
        logger.info("GL128 loader rewriting registers before stationary strips")
        self._write_many(dict(self.model.init_regs))
        self.set_frontend_init()
        self.image_pass_since_init = False
        self.enter_scan_clock()

    # --- calibration strips -------------------------------------------------

    def _strip_regs(
        self,
        *,
        resolution: int,
        pixels: int,
        lines: int,
        strpixel: int,
        endpixel: int,
        dpiset: int,
        lperiod: int,
        clocks: tuple[int, int, int],
        dark: bool,
    ) -> dict[int, int]:
        """Register block of one calibration strip, over the scan's timing set."""
        r = self.registers
        regs = dict(self.model.scan_regs_for(resolution))
        for addr, (mask, value) in self.model.scan_masked_regs_for(resolution).items():
            if addr in LIVE_REGS:
                continue
            base = regs.get(addr, self.model.init_regs.get(addr, 0))
            regs[addr] = (int(base) & ~mask & 0xFF) | (value & mask)
        regs.update(self.model.memory_layout_regs)
        regs.update(_STRIP_REGS)
        regs.update(_CALIB_STRIP_A6_A9)
        regs.update(exposure_registers(int(self.model.exposure_lperiod), 0))
        dummy, clk_a, clk_b = clocks
        regs[0x2B] = int(dummy)
        regs[0xA5] = int(clk_a)
        regs[0xAB] = int(clk_b)
        regs[0xA3] = 0x01 if dark else 0x00
        if dark:
            regs[_REG_TOGGLE] = int(regs.get(_REG_TOGGLE, 0)) & ~_TOGGLE_BIT
        for addr, width, value in (
            (r.REG_LINCNT, 3, int(lines)),
            (r.REG_DPISET, 2, int(dpiset)),
            (r.REG_STRPIXEL, 3, int(strpixel)),
            (r.REG_ENDPIXEL, 3, int(endpixel)),
            (r.REG_FEEDL, 3, 1),
            (r.REG_LPERIOD, 3, int(lperiod)),
        ):
            for i in range(width):
                regs[addr + i] = (value >> (8 * (width - 1 - i))) & 0xFF
        del pixels
        return regs

    def _acquire_loader_strip(
        self,
        regs: Mapping[int, int],
        *,
        size: int,
        dvdset: bool,
        where: str,
        timeout_s: float = 10.0,
    ) -> bytes:
        """Program a stationary strip, START it and read ``size`` bytes from RAM."""
        r = self.registers
        self._reset_stationary_scan_engine(where=where)
        regs = dict(regs)
        reg01 = _STRIP_REG01_DVDSET if dvdset else _STRIP_REG01
        regs[r.REG_0x01] = reg01
        self._write_many(regs)
        self._write(r.REG_CLRCNT, r.CLRCNT_ALL)
        self._write(r.REG_0x01, reg01 | r.SCAN)
        self._write(r.REG_START, r.START_GO)
        self._wait_stationary_data_ready(timeout_s, where=where)
        self.protocol.bulk_read_begin(size, index=r.BULK_INDEX_RAM, addr=r.AHB_RAM_BASE)
        buf = bytearray()
        while len(buf) < size:
            chunk = self.protocol.bulk_read_chunk(min(65536, size - len(buf)))
            if not chunk:
                break
            buf.extend(chunk)
        self._write(r.REG_0x01, reg01)
        if len(buf) < size:
            raise ScanError(f"{where}: short read {len(buf)} of {size} bytes")
        return bytes(buf[:size])

    def acquire_afe_strip(self, size: int = 0, *, timeout_s: float = 5.0, timing=None, wide: bool = False, resolution: int = 1200) -> bytes:  # type: ignore[override]
        """One two-line AFE strip: the 512-pixel probe, or the full width with ``wide``."""
        del size, timing
        if not self._initialized:
            self.init()
        units = self.model.full_width_units(resolution)
        pixels = units if wide else AFE_PROBE_PIXELS
        regs = self._strip_regs(
            resolution=resolution,
            pixels=pixels,
            lines=AFE_STRIP_LINES,
            strpixel=WINDOW_ORIGIN,
            endpixel=WINDOW_ORIGIN + pixels,
            dpiset=AFE_DPISET,
            lperiod=strip_line_period(pixels, LPERIOD_BASE),
            clocks=AFE_WIDE_CLOCKS if wide else AFE_PROBE_CLOCKS,
            dark=False,
        )
        self.apply_timing_gpio(resolution)
        return self._acquire_loader_strip(
            regs,
            size=strip_bytes(pixels, AFE_STRIP_LINES),
            dvdset=False,
            where="AFE strip",
            timeout_s=timeout_s,
        )

    def _probe_black(self, gains: tuple[int, int, int], target: float, *, fallback: tuple[int, int, int], resolution: int, stage: str) -> tuple[int, int, int]:
        black: list[tuple[float, float, float]] = []
        for code in (AFE_OFFSET_PROBE_LOW, AFE_OFFSET_PROBE_HIGH):
            self.apply_frontend(AfeFrontend(gains=gains, offsets=(code, code, code)))
            black.append(optical_black_means(self.acquire_afe_strip(resolution=resolution)))
        solved = offsets_for_black_level(black[0], black[1], target)
        offsets = tuple(fallback[c] if v is None else v for c, v in enumerate(solved))
        if None in solved:
            logger.warning(
                "GL128 loader AFE %s: optical black barely moved between offsets %#x and %#x "
                "(%s -> %s); using fallback codes %s on those channels",
                stage,
                AFE_OFFSET_PROBE_LOW,
                AFE_OFFSET_PROBE_HIGH,
                tuple(round(v) for v in black[0]),
                tuple(round(v) for v in black[1]),
                fallback,
            )
        return (offsets[0], offsets[1], offsets[2])

    def search_afe(self, *, method=None, resolution: int | None = None) -> AfeFrontend:  # type: ignore[override]
        """VueScan's AFE order on the 135i: probe pair, wide strip, probe pair.

        1. gains 0, optical black at offsets ``0x80`` / ``0xFF``: offsets that
           put black at ~48;
        2. full-width strip at those offsets: gain codes toward the white
           targets for the brighter of the visible and IR lines;
        3. the probe pair again under the gains: offsets for black ``0x500``.

        Lamp and IR LED stay on (toggle); the motor stays off.
        """
        if not self._initialized:
            self.init()
        self.reinit_before_stationary_strips()
        if method is not None:
            self.set_scan_method(method)
        dpi = int(resolution if resolution is not None else 1200)
        scan_dpi = self.model.asic_dpi_for(dpi)
        self.lamp_on()
        time.sleep(0.1)
        zero = (0, 0, 0)
        pre = self._probe_black(zero, AFE_PRE_OFFSET_BLACK, fallback=AFE_FALLBACK_PRE_OFFSETS, resolution=dpi, stage="pre-offset")
        self.apply_frontend(AfeFrontend(gains=zero, offsets=_encode_offsets(pre)))
        wide_pixels = self.model.full_width_units(dpi)
        wide = self.acquire_afe_strip(wide=True, resolution=dpi)
        means = lit_channel_means(wide, pixels=wide_pixels)
        gains = coarse_gains_from_wide_means(means, targets=AFE_WHITE_TARGETS_COLOR)
        offsets = self._probe_black(gains, AFE_BLACK_TARGET_COLOR, fallback=AFE_FALLBACK_OFFSETS_COLOR, resolution=dpi, stage="black offset")
        result = AfeFrontend(gains=gains, offsets=_encode_offsets(offsets))
        self.apply_frontend(result)
        self.last_afe = result
        self.last_afe_dpi = scan_dpi
        logger.info(
            "GL128 loader AFE dpi=%d pre_offsets=%s lit_means=(%.0f,%.0f,%.0f) gains=%s offsets=%s",
            scan_dpi,
            pre,
            means[0],
            means[1],
            means[2],
            gains,
            offsets,
        )
        return result

    def upload_shading_table(self, blob: bytes) -> None:
        """Write a two-bank blob: IR to ``0x10014000``, visible to ``0x10034000``."""
        ir, visible = split_shading_blob(bytes(blob))
        self.protocol.write_ahb(AHB_SHADING_IR, ir)
        self.protocol.write_ahb(AHB_SHADING_VISIBLE, visible)
        logger.info("GL128 loader uploaded shading tables (2 x %d bytes)", len(ir))

    def acquire_shading_strip(  # type: ignore[override]
        self,
        *,
        resolution: int,
        strpixel: int,
        endpixel: int,
        dpiset: int,
        dvdset: bool = False,
        lines: int = SHADING_LINES,
        timeout_s: float = 30.0,
        pixels: int | None = None,
    ) -> bytes:
        """256-line dark (``dvdset=False``) or white strip through the image window."""
        span = int(endpixel) - int(strpixel)
        n = int(pixels) if pixels is not None else span // self.model.sensor_units_per_pixel(resolution)
        regs = self._strip_regs(
            resolution=resolution,
            pixels=n,
            lines=lines,
            strpixel=strpixel,
            endpixel=endpixel,
            dpiset=dpiset,
            lperiod=self.model.line_period_for(resolution, span=span),
            clocks=self.model.shading_strip_clocks(resolution, dvdset=dvdset),
            dark=not dvdset,
        )
        self.apply_timing_gpio(resolution)
        return self._acquire_loader_strip(
            regs,
            size=strip_bytes(n, lines),
            dvdset=dvdset,
            where="Shading white strip" if dvdset else "Shading dark strip",
            timeout_s=timeout_s,
        )

    def run_asic_shading(  # type: ignore[override]
        self,
        *,
        resolution: int = 1200,
        method=None,
        strpixel: int | None = None,
        endpixel: int | None = None,
        dpiset: int | None = None,
    ) -> bytes:
        """Dark -> unity tables -> white -> measured tables, both banks, at home.

        Returns the two-bank blob (IR table then visible table) and sets
        ``asic_shading_ready`` when both validate. The image pass then keeps
        ``DVDSET`` and the ASIC flattens visible and IR lines alike.
        """
        if not self._initialized:
            self.init()
        if self._motor_moves_enabled:
            raise ScanError("ASIC shading requires motor disarmed — call disarm_bringup_motor before run_asic_shading")
        self.reinit_before_stationary_strips()
        if method is not None:
            self.set_scan_method(method)
        self._require_carriage_at_home("ASIC shading start")
        default_start, default_end = self.model.image_window(resolution)
        start = default_start if strpixel is None else int(strpixel)
        end = default_end if endpixel is None else int(endpixel)
        used_dpiset = int(dpiset if dpiset is not None else self.model.register_dpiset_by_dpi[int(resolution)])
        n = (end - start) // self.model.sensor_units_per_pixel(resolution)
        window = {"strpixel": start, "endpixel": end, "dpiset": used_dpiset, "pixels": n}
        self.asic_shading_ready = False
        self.ir_host_flatten_ready = False
        self.last_ir_host_white = None
        self.last_host_calib_dark = None
        self.last_host_calib_white = None
        self.last_color_shading_host_ok = False
        if self.last_afe is not None:
            self.apply_frontend(self.last_afe)

        self._reassert_lamp_off()
        time.sleep(COLOR_SHADING_DARK_SETTLE_S)
        dark_raw = self.acquire_shading_strip(resolution=resolution, **window)
        dark_vis, dark_ir = strip_column_means(dark_raw, pixels=n, lines=SHADING_LINES)
        unity = unity_shading_table(dark_ir) + unity_shading_table(dark_vis)
        self.upload_shading_table(unity)

        self.lamp_on()
        time.sleep(0.5)
        white_raw = self.acquire_shading_strip(resolution=resolution, dvdset=True, **window)
        white_vis, white_ir = strip_column_means(white_raw, pixels=n, lines=SHADING_LINES)
        gains_vis = shading_gains_from_white(white_vis, target=SHADING_WHITE_TARGET_VISIBLE)
        gains_ir = shading_gains_from_white(white_ir, target=SHADING_WHITE_TARGET_IR)

        strip_ok, reason = validate_color_shading_strip(dark_vis, white_vis, acquire_width=n)
        ok = strip_ok
        for check in (
            lambda: validate_gains(gains_vis),
            lambda: validate_ir_white(white_ir),
            lambda: validate_gains(gains_ir),
        ):
            if not ok:
                break
            ok, reason = check()
        self._require_carriage_at_home("ASIC shading end")
        if ok:
            blob = measured_shading_table(dark_ir, gains_ir) + measured_shading_table(dark_vis, gains_vis)
            self.upload_shading_table(blob)
            self.asic_shading_ready = True
            self.last_color_shading_reject_reason = None
            logger.info(
                "GL128 loader shading ready dpi=%d pixels=%d window=%d..%d dpiset=%d "
                "dark=%.0f/%.0f white=%.0f/%.0f (visible/IR)",
                resolution,
                n,
                start,
                end,
                used_dpiset,
                shading_columns_mean(dark_vis),
                shading_columns_mean(dark_ir),
                shading_columns_mean(white_vis),
                shading_columns_mean(white_ir),
            )
            return blob
        self.last_color_shading_reject_reason = reason
        if strip_ok:
            self.last_host_calib_dark = [(int(c[0]), int(c[1]), int(c[2])) for c in dark_vis]
            self.last_host_calib_white = [(int(c[0]), int(c[1]), int(c[2])) for c in white_vis]
            self.last_color_shading_host_ok = True
        logger.warning(
            "GL128 loader shading rejected (%s); DVDSET left off, unity tables kept, host_stretch=%s",
            reason,
            strip_ok,
        )
        return unity
