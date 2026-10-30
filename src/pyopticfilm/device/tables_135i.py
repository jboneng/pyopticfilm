# SPDX-License-Identifier: GPL-3.0-or-later
"""OpticFilm 135i timing tables, scan modes, loader constants and motor ramps (GL128).

Sources:

* VueScan usbmon captures of an OpticFilm 135i (``07b3:1436``,
  ``bcdDevice=0x0702``; set ``plustek 135i captures``, 2026-08-31): ``windows
  attach``, ``vuescan open``, ``insert 35mm carrier``, ``holder eject``, the
  1200 dpi 35 mm preview, the 600 dpi panorama preview and the 7200 dpi frame
  scan. "measured" below means one of these captures.
* The Plustek model data SilverFast 9 installs for this scanner,
  ``Scanapi_07b3_1436.ini`` (key ``VID_07b3&PID_1436``): register overlays per
  scan mode and Y resolution, the two sensor timing sets, ``Motor\\Curve``,
  ``MotorDPI``, ``MotorBackRatio``, ``HomeCarriageStep``, ``LoaderEjectStep``,
  ``LineSpace``, ``StaggeredLine``. Every capture register program agrees with
  it (``docs/gl128-model-comparison.md`` §11).

The 135i is a motorised film loader, not a flatbed carriage: there is no
reference feed or ``0x21`` feed probe. Loading a holder moves the film forward
and seeks back to the loader sensor; frames are then reached with forward feeds
from that home.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

# --------------------------------------------------------------------------
# Motor ramps
# --------------------------------------------------------------------------

#: Entries per AHB motor table (512-byte window of 16-bit words).
MOTOR_TABLE_LENGTH = 256
#: ``Motor\Curve`` (start percent, peak steps/s, shape) from the model INI.
MOTOR_CURVE: tuple[int, int, int] = (30, 2500, 180)
#: Motor step shift: ``0xA0 = 0x09`` in the boot blast gives 1 for both the
#: scan (bits 2:0) and fast (bits 5:3) fields, so one step is 1/7200 inch.
MOTOR_STEP_SHIFT = 1
#: ``MotorDPI`` (first value) from the model INI.
MOTOR_DPI = 3600
#: Acceleration and deceleration steps per move: ``0xAE``-``0xAF`` = ``00 FF``
#: on every captured feed and image pass. A move of ``n`` steps programs
#: ``FEEDL = n - 2 * min(n // 2, 255)``.
MOTOR_RAMP_STEPS = 255


def curve_rates(start_pct: int, peak: int, shape: int, length: int = MOTOR_TABLE_LENGTH) -> list[int]:
    """Step rates of the analytic acceleration curve ``(start_pct, peak, shape)``.

    ``rate[i] = int(peak * (i / (n - 1)) ** (100 / shape))`` for ``i >= 1`` and
    ``rate[0] = rate[1] * start_pct // 100``, the same curve as the 8300i SE
    scan ramp (:func:`pyopticfilm.device.tables_8300i_se._vendor_scan_ramp`).
    """
    n = int(length)
    rate = [0] * n
    for i in range(n - 1, 0, -1):
        rate[i] = int(int(peak) * math.exp(math.log(i / (n - 1)) * 100.0 / int(shape)))
    rate[0] = max(1, rate[1] * int(start_pct) // 100)
    return rate


def curve_periods(start_pct: int, peak: int, shape: int, divider: int) -> tuple[int, ...]:
    """Step periods ``divider // rate`` of :func:`curve_rates`."""
    return tuple(int(divider) // r for r in curve_rates(start_pct, peak, shape))


def motor_tick(reg06: int, reg09: int = 0) -> int:
    """Clock ticks per motor timer unit: ``0x09[7:6] + 1`` when set, else by ``0x06[7:5]``."""
    high = (int(reg09) >> 6) & 0x03
    if high:
        return high + 1
    return {4: 6, 5: 15, 6: 18, 7: 16}.get((int(reg06) >> 5) & 0x07, 12)


def motor_table_divider(reg06: int, system_clock_mhz: int, *, step_time_shift: int = 0) -> int:
    """Period numerator ``(10**9 >> 0x1C[2:0]) // (tick * 1000 // MHz)``."""
    clock = motor_tick(reg06) * 1000 // int(system_clock_mhz)
    return (10**9 >> int(step_time_shift)) // clock


#: Boot timing (``0x06 = 0x18``, 48 MHz): 250 per unit, halved once
#: (``0x1C[2:0] = 1``) so the first period fits in 16 bits.
_DIVIDER_BOOT = motor_table_divider(0x18, 48, step_time_shift=1)
#: Scan timing (``0x06 = 0xF8``, ``SYSTEMCLOCK = 60``): 266 per unit.
_DIVIDER_SCAN = motor_table_divider(0xF8, 60)
#: Peak rate of the fast ramps VueScan uploads for feeds (the INI curve shape
#: at 4250 steps/s).
_FEED_PEAK = 4250

#: Insert / reverse-seek ramp, head ``34482``, tail ``470`` (measured: ``insert
#: 35mm carrier``, both feeds, AHB ``0x1000C000`` and ``0x10010000``).
SLOPE_TABLE_LOADER: tuple[int, ...] = curve_periods(30, _FEED_PEAK, 180, _DIVIDER_BOOT)
#: Frame-positioning and eject ramp, head ``64817``, tail ``884`` (measured:
#: every positioning feed in the previews and the 7200 dpi scan, and ``holder
#: eject``).
SLOPE_TABLE_FEED: tuple[int, ...] = curve_periods(30, _FEED_PEAK, 180, _DIVIDER_SCAN)
#: Scan motor acceleration ramp: the INI curve at ``2500 << MOTOR_STEP_SHIFT``
#: with the scan divider. Head ``54484``; clamped by :func:`scan_motor_table`
#: it gives every measured image-pass table (600, 1200 and 7200 dpi).
SCAN_RAMP: tuple[int, ...] = curve_periods(
    MOTOR_CURVE[0], MOTOR_CURVE[1] << MOTOR_STEP_SHIFT, MOTOR_CURVE[2], _DIVIDER_SCAN
)


def scan_motor_table(period: int) -> tuple[int, ...]:
    """AHB ``0x10000000``/``0x10004000``/``0x10008000`` table for a scan move.

    Every entry is ``max(SCAN_RAMP[i], period)``: 4666 at 1200 dpi (10
    ramp entries), 2333 at 600 dpi (34), a flat 56000 at 7200 dpi.
    """
    body = int(period)
    if not 0 <= body <= 0xFFFF:
        raise ValueError(
            f"scan motor period {body} does not fit the AHB table's 16-bit words (0-65535)"
        )
    return tuple(max(v, body) for v in SCAN_RAMP)


def scan_motor_period(exposure: int, y_dpi: int, *, reg_1d_lines: int = 0) -> int:
    """Scan motor step period ``exposure * (0x1D[4:0] + 1) * Ydpi / MotorDPI``.

    Measured: 14000 at 1200 dpi gives 4666, at 600 dpi 2333; 28000 at 7200 dpi
    gives 56000.
    """
    return int(exposure) * (int(reg_1d_lines) + 1) * int(y_dpi) // MOTOR_DPI


def feedl_for_move(steps: int) -> int:
    """``FEEDL`` for a move of ``steps``: the ramps take up to 255 steps at each end."""
    n = int(steps)
    ramp = min(n // 2, MOTOR_RAMP_STEPS)
    return max(1, n - 2 * ramp)


# --------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------

#: GPIO register with the loader outputs, the IR LED and the holder bits.
#: Bit 2 changes with ``HOMESNR`` and ``0x100`` bit 7 in every reading, so it
#: follows the arm state too (:data:`LOADER_HOME_SENSOR_BIT`): with the stop
#: disarmed it is the holder in the slot, armed it reads 0 off the loader
#: home. ``insert 35mm carrier`` (``0x43`` -> ``0x07``), QuickScan (``0x43`` ->
#: ``0x05``), a 135i before init (``0xc2`` empty, ``0xc6`` holder in) and after
#: it (``0x1b`` empty). Bit 6 is not an input: after the init table's ``0x1F``
#: it reads 0 with the slot empty.
HOLDER_SENSOR_REG = 0x32
#: Second status register: ``MOTMFLG`` (bit 1) while the motor still turns.
STATUS2_REG = 0x100
STATUS2_MOTMFLG = 0x02
#: Interrupt endpoint ``0x83`` byte sent when the eject button is pressed
#: (``holder eject``: the only completed interrupt, 2.6 s before the eject feed).
EJECT_EVENT = 0x48
#: ``LoaderDelayTime``: wait after the holder sensor changes before moving.
LOADER_DELAY_S = 0.5
#: Load: forward one inch (``HomeCarriageStep[0] = 1``), measured FEEDL 6690.
LOADER_LOAD_FORWARD_STEPS = 7200
#: Load: back ten inches (``HomeCarriageStep[1] = -10``) with ``MTRREV``
#: (``0x02 = 0x1C``) until the loader sensor (status ``HOMESNR``) is reached;
#: measured FEEDL 71490.
LOADER_LOAD_REVERSE_STEPS = 72000
#: Eject: ``LoaderEjectStep = 500`` thousandths of an inch, measured FEEDL 3090.
LOADER_EJECT_STEPS = 3600
#: Positioning feed of the first 35 mm frame from the loader home (measured:
#: 1200 dpi preview, frame 1). The panorama preview starts at 6414.
FIRST_FRAME_FEED_STEPS = 6438
#: Feed difference between neighbouring 35 mm frames (measured: preview frames
#: at 6438, 17190 and 27942; 10752 steps = 37.9 mm).
FRAME_PITCH_STEPS = 10752
#: Positioning feed of the panorama (whole-strip) preview from the loader home.
PANORAMA_FEED_STEPS = 6414
#: Height of the panorama preview: 5550 line pairs read at 600 dpi (111 reads
#: of 518400 bytes, 864 px), 66600 steps (9.25 in).
PANORAMA_HEIGHT_STEPS = 66600
#: Longest holder travel captured: the panorama preview's feed (6414) plus its
#: image pass (``LINCNT`` 11218 = 5609 line pairs at 12 steps each) ends 73722
#: steps from home, past the INI ``PageSize`` height of 72000 (10 in). A feed
#: plus the image pass must end inside it.
LOADER_TRAVEL_STEPS = PANORAMA_FEED_STEPS + 5609 * 12
#: Reverse seek off the loader home in an eject or after a lost park. The
#: load's 72000 stops ~1700 short from the end of a strip; ``FEEDL`` here is
#: the whole travel, and the armed sensor still ends the seek.
LOADER_RECOVERY_SEEK_STEPS = LOADER_TRAVEL_STEPS + 510
#: ``LoaderHomeSensorReg = 0x32,0x02,0``: an output that selects what
#: ``HOMESNR`` (``0x101`` bit 3) reads. Cleared, the loader stop is armed and
#: ``HOMESNR`` reads a narrow band around the loaded position; set, it reads
#: "holder in the slot". The load clears it (``insert 35mm carrier``: ``0x32``
#: ``07`` -> ``05``); the init table sets it (``0x1F``).
LOADER_HOME_SENSOR_BIT = 0x02
#: ``0x09`` bit set while the holder is ejected (``holder eject``: ``09=08``
#: before the feed, ``09=00`` after it).
EJECT_REG_09 = 0x08
#: Holder LED, written before the eject (QuickScan: ``36=fc``, ``33=8e``; the
#: init table's values).
EJECT_LED_REGS: tuple[tuple[int, int], ...] = ((0x36, 0xFC), (0x33, 0x8E))


def ramp_steps_for_move(steps: int) -> int:
    """``0xAE``-``0xAF`` ramp-step count for a move of ``steps`` (see :func:`feedl_for_move`)."""
    return min(int(steps) // 2, MOTOR_RAMP_STEPS)


# --------------------------------------------------------------------------
# Sensor timing sets and scan modes (model INI)
# --------------------------------------------------------------------------

#: One masked register write: ``address -> (mask, value)``.
MaskedRegs = Mapping[int, tuple[int, int]]

#: ``Speed1``: 7200 dpi Y subkey. ``SENSORSPEED = 1`` (``hw / sp = 7200``),
#: ``SYSTEMCLOCK = 60``. Measured: the 7200 dpi frame scan.
SPEED1_REGS: dict[int, int] = {
    0x06: 0xF8, 0x16: 0x23, 0x18: 0x10,
    0x1F: 0x00, 0x20: 0x19, 0x21: 0x00, 0x22: 0x05, 0x23: 0x00, 0x24: 0x0F,
    0x52: 0x0A, 0x53: 0x0D, 0x54: 0x0F, 0x55: 0x02, 0x56: 0x05, 0x57: 0x08,
    0x5A: 0x2A, 0x5B: 0x6A, 0x5C: 0x00,
    0x70: 0x0A, 0x71: 0x0B, 0x72: 0x0B, 0x73: 0x0C,
    0x7D: 0x00, 0x7E: 0x36, 0x7F: 0xB0,
    0x88: 0x00, 0x89: 0x05,
}
SPEED1_MASKED: dict[int, tuple[int, int]] = {
    0x32: (0x80, 0x00), 0x33: (0x80, 0x00), 0x35: (0x80, 0x80), 0x3A: (0x10, 0x10),
}
#: ``Speed2``: every other Y subkey (300-3600). ``SENSORSPEED = 2``
#: (``hw / sp = 3600``), ``SYSTEMCLOCK = 60``. Measured: both previews.
SPEED2_REGS: dict[int, int] = {
    0x06: 0xF8, 0x16: 0x23, 0x18: 0x00,
    0x1F: 0x00, 0x20: 0x19, 0x21: 0x00, 0x22: 0x05, 0x23: 0x00, 0x24: 0x0F,
    0x52: 0x0F, 0x53: 0x02, 0x54: 0x05, 0x55: 0x08, 0x56: 0x0A, 0x57: 0x0D,
    0x5A: 0x62, 0x5B: 0x22, 0x5C: 0x00,
    0x70: 0x00, 0x71: 0x01, 0x72: 0x01, 0x73: 0x02,
    0x7D: 0x00, 0x7E: 0x36, 0x7F: 0xB0,
    0x88: 0x00, 0x89: 0x05,
}
SPEED2_MASKED: dict[int, tuple[int, int]] = {
    0x32: (0x80, 0x80), 0x33: (0x88, 0x08), 0x35: (0x80, 0x80), 0x3A: (0x10, 0x10),
}
#: ``SENSORSPEED`` per timing set.
SENSOR_SPEED: dict[str, int] = {"Speed1": 1, "Speed2": 2}

#: Toggle mode (``NegativeToggle`` / ``PositiveToggle``): RGB and IR lines
#: alternate in one pass; ``0x0C[3:0] = 1``, ``0x19`` bit 3, IR LED on
#: (``IRMASK``: ``0x32`` bit 5). ``0x23`` from the timing set wins. Measured:
#: every VueScan pass except the lamp-off dark strip.
TOGGLE_REGS: dict[int, int] = {0x3B: 0x00, 0x3C: 0x00, 0x90: 0x00, 0x91: 0x2A, 0x92: 0xF8}
TOGGLE_MASKED: dict[int, tuple[int, int]] = {
    0x03: (0x10, 0x10), 0x0C: (0x0F, 0x01), 0x19: (0x08, 0x08),
    0x32: (0x20, 0x20), 0x15: (0x10, 0x10),
}
#: Plain colour (``Negative`` / ``Positive``): IR LED off, no toggle.
COLOUR_REGS: dict[int, int] = {0x3B: 0x00, 0x3C: 0x00, 0x90: 0x00, 0x91: 0x2A, 0x92: 0xF8}
COLOUR_MASKED: dict[int, tuple[int, int]] = {
    0x03: (0x10, 0x10), 0x32: (0x20, 0x00), 0x15: (0x10, 0x10),
}

#: Y-resolution subkeys in the model INI (Color, every mode).
DPI_SUBKEYS: tuple[int, ...] = (300, 600, 1200, 3600, 7200)
#: ``0x3B``/``0x3C`` per subkey; 300 keeps the mode's ``00 00``. Measured:
#: 600 (panorama) ``02 00``, 1200 and 7200 ``00 01``.
REG_3B_BY_SUBKEY: dict[int, tuple[int, int]] = {
    300: (0x00, 0x00),
    600: (0x02, 0x00),
    1200: (0x00, 0x01),
    3600: (0x00, 0x01),
    7200: (0x00, 0x01),
}


def dpi_subkey(y_dpi: int) -> int:
    """INI subkey for a Y resolution: the smallest listed ``>= y_dpi``, else the largest."""
    for key in DPI_SUBKEYS:
        if key >= int(y_dpi):
            return key
    return DPI_SUBKEYS[-1]


def timing_set_for(y_dpi: int) -> str:
    """``"Speed1"`` for the 7200 subkey, ``"Speed2"`` below it."""
    return "Speed1" if dpi_subkey(y_dpi) == 7200 else "Speed2"


def hardware_x_dpi(x_dpi: int, sensor_speed: int, *, hw_dpi: int = 7200) -> int:
    """Hardware X resolution: smallest ``(hw / sp) / D >= x_dpi`` with ``D`` dividing ``hw / sp``."""
    base = int(hw_dpi) // int(sensor_speed)
    for d in (15, 12, 10, 8, 6, 5, 4, 3, 2, 1):
        if base % d == 0 and base // d >= int(x_dpi):
            return base // d
    return base


#: ``r080`` = ``DummyPixel`` (0) + ``0x1F``-``0x20`` (25) + 2 x ``0x21``-``0x22``
#: (5): the first sensor pixel of every window. Measured STRPIXEL 35 at 600,
#: 1200 and 7200 dpi.
WINDOW_ORIGIN = 35
#: Full sensor width at 7200 dpi (1.44 in): measured END - STR = 10368 at
#: 7200 dpi, 5184 sensor units at ``hw / sp = 3600``.
FULL_WIDTH_7200 = 10368
#: ``LPERIOD`` base: ``O / 4`` where ``O = 16 v - 4`` and ``v`` is the SDRAM
#: line buffer left after two shading tables (visible and IR): 10807 in every
#: measured pass (the SE family, with one table, has 10851).
LPERIOD_BASE = 10807
#: Native line distance between colour rows (``LineSpace = -24``) and the
#: odd/even pixel offset at 7200 dpi (``StaggeredLine = 4``).
LINE_SPACE = 24
STAGGERED_LINE = 4

#: ``0x90``-``0x92`` with no exposure multiplier: 11000 in every 600 and 1200
#: dpi pass and every calibration strip.
REG_90_BASE = 11000
#: Exposure multiplier ``n`` by timing set: ``REG_EXPOSURE = 7d * n`` and
#: ``0x90 = 11000 * (n + 2)`` when ``n > 0``. Measured: 0 (14000 / 11000) at
#: 600 and 1200 dpi, 2 (28000 / 44000) at 7200 dpi.
EXPOSURE_MULTIPLIER: dict[str, int] = {"Speed1": 2, "Speed2": 0}


def exposure_registers(exposure: int, multiplier: int) -> dict[int, int]:
    """``0x7D``-``0x7F`` and ``0x8A``-``0x92`` for an image pass or feed.

    ``0x8A``-``0x8F`` are zero in every capture. ``0x90`` follows the
    multiplier that produced ``exposure``.
    """
    n = int(multiplier)
    reg90 = REG_90_BASE * (n + 2) if n > 0 else REG_90_BASE
    out = {0x7D: (exposure >> 16) & 0xFF, 0x7E: (exposure >> 8) & 0xFF, 0x7F: exposure & 0xFF}
    out.update({a: 0x00 for a in range(0x8A, 0x90)})
    out.update({0x90: (reg90 >> 16) & 0xFF, 0x91: (reg90 >> 8) & 0xFF, 0x92: reg90 & 0xFF})
    return out


def image_line_margin(x_dpi: int, y_dpi: int) -> int:
    """Extra sensor lines the vendor scans past the requested height.

    ``ceil(2 * LineSpace * Y / 7200) + 1``, plus ``StaggeredLine`` when the
    hardware X resolution is the native 7200 dpi: 53 at 7200, 9 at 1200, 5 at
    600. Not measured on the wire (the captures come from VueScan, which uses
    its own margins).
    """
    y = int(y_dpi)
    margin = math.ceil(2 * LINE_SPACE * y / 7200) + 1
    if int(x_dpi) == 7200:
        margin += math.ceil(STAGGERED_LINE * y / 7200)
    return margin


#: VueScan ``vuescan open`` boot blast: the SE map with 29 different values,
#: plus ``0x114``/``0x115``. Identical to the model INI ``"0"`` table after its
#: ``4f.f0``, ``7e`` and ``3b`` overrides.
INIT_REGS_135I: dict[int, int] = {
    0x01: 0x22, 0x02: 0x78, 0x03: 0x20, 0x04: 0x02, 0x05: 0x48, 0x06: 0x18,
    0x07: 0x00, 0x08: 0x00, 0x09: 0x00, 0x0A: 0x48, 0x0B: 0x6C, 0x0C: 0x00,
    0x0D: 0x00, 0x11: 0x00, 0x12: 0x04, 0x13: 0x08, 0x14: 0x01, 0x15: 0x80,
    0x16: 0x01, 0x17: 0x0C, 0x18: 0x14, 0x19: 0x00, 0x1A: 0x00, 0x1B: 0x00,
    0x1C: 0x00, 0x1D: 0x00, 0x1E: 0x10, 0x1F: 0x00, 0x20: 0x0C, 0x21: 0x00,
    0x22: 0x1A, 0x23: 0x00, 0x24: 0x1A, 0x25: 0x00, 0x26: 0x00, 0x27: 0x00,
    0x2B: 0x20, 0x2C: 0x12, 0x2D: 0xC0, 0x30: 0xEE, 0x31: 0x80, 0x32: 0x1F,
    0x33: 0x8E, 0x34: 0x80, 0x35: 0xFB, 0x36: 0xFC, 0x37: 0x21, 0x38: 0x60,
    0x39: 0x02, 0x3A: 0x00, 0x3B: 0x00, 0x3C: 0x00, 0x3D: 0x00, 0x3E: 0x00,
    0x3F: 0x01, 0x4F: 0x63, 0x52: 0x07, 0x53: 0x09, 0x54: 0x0B, 0x55: 0x01,
    0x56: 0x03, 0x57: 0x05, 0x5A: 0x12, 0x5B: 0x00, 0x5C: 0x00, 0x5E: 0x1F,
    0x5F: 0x07, 0x60: 0x00, 0x61: 0x00, 0x63: 0x20, 0x67: 0x7F, 0x68: 0x7F,
    0x69: 0x01, 0x70: 0x06, 0x71: 0x09, 0x72: 0x08, 0x73: 0x09, 0x74: 0x00,
    0x75: 0x00, 0x76: 0x00, 0x77: 0x00, 0x78: 0x00, 0x79: 0x00, 0x7A: 0x00,
    0x7B: 0x00, 0x7C: 0x00, 0x7D: 0x00, 0x7E: 0x75, 0x7F: 0x30, 0x80: 0x00,
    0x81: 0x22, 0x82: 0x00, 0x83: 0x01, 0x84: 0x18, 0x85: 0x00, 0x86: 0x00,
    0x87: 0x00, 0x93: 0x00, 0x94: 0x00, 0x95: 0x00, 0x9D: 0x08, 0xA0: 0x09,
    0xA4: 0x00, 0xA5: 0x20, 0xA6: 0x00, 0xA7: 0x00, 0xA8: 0x00, 0xA9: 0x00,
    0xAA: 0x00, 0xAB: 0x30, 0xB8: 0x00, 0xB9: 0x38, 0xBA: 0x00, 0xBD: 0x00,
    0xBE: 0x00, 0xBF: 0x00,
    0x114: 0x80, 0x115: 0x80,
}

#: Memory layout block written before every acquire; identical in all three
#: measured scans. ``0xD0``/``0xD1`` select shading banks 10 (``0x10014000``,
#: IR) and 26 (``0x10034000``, visible) in toggle mode.
MEMORY_LAYOUT_REGS_135I: dict[int, int] = {
    0xD0: 0x0A, 0xD1: 0x1A,
    0xE0: 0x00, 0xE1: 0xA8, 0xE2: 0x0B, 0xE3: 0x35, 0xE4: 0x0B, 0xE5: 0x36,
    0xE6: 0x15, 0xE7: 0xC3, 0xE8: 0x15, 0xE9: 0xC4, 0xEA: 0x20, 0xEB: 0x51,
    0xEC: 0x20, 0xED: 0x52, 0xEE: 0x2A, 0xEF: 0xDF, 0xF0: 0x2A, 0xF1: 0xE0,
    0xF2: 0x35, 0xF3: 0x6D, 0xF4: 0x35, 0xF5: 0x6E, 0xF6: 0x3F, 0xF7: 0xFB,
    0xF8: 0x05,
}

#: ``vuescan open`` traffic between the DRAM strobe and the frontend init:
#: GPIO requests (``0x8B`` writes, ``0x8A`` reads) and the lamp-off pair on
#: ``0x03``. There are no ``0x000FFF00``/``0x000FFF01`` blobs on this model.
BOOT_VENDOR_STEPS_135I: tuple[tuple[object, ...], ...] = (
    ("out", 0x8B, 0x07, b"\x01"),
    ("out", 0x8B, 0x09, bytes(4)),
    ("in", 0x8A, 0x10, 3),
    ("out", 0x8B, 0x0B, bytes(4)),
    ("in", 0x8A, 0x0F, 64),
    ("reg", 0x03, 0x10),
    ("reg", 0x03, 0x00),
)
