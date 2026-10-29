# SPDX-License-Identifier: GPL-3.0-or-later
"""Genesys register addresses and bit masks.

``Gl845Registers`` mirrors genesys ``gl846_registers.h``. ``Gl128Registers``
covers GL128 OpticFilm (map proven on the 8200i SE; also used by 8100 V2),
whose map is GL124-family; every address and bit there is annotated with the
capture that proves it (see ``captures/8200i-se/*/NOTES.md``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Gl845Registers:
    REG_0x01: int = 0x01
    REG_0x02: int = 0x02
    REG_0x03: int = 0x03
    REG_0x04: int = 0x04
    REG_0x05: int = 0x05
    REG_0x06: int = 0x06
    REG_0x0E: int = 0x0E
    REG_0x0F: int = 0x0F
    REG_0x40: int = 0x40
    REG_0x41: int = 0x41  # status
    REG_0x6C: int = 0x6C
    REG_0xA8: int = 0xA8

    # 0x01
    SCAN: int = 0x01
    SHDAREA: int = 0x02
    DVDSET: int = 0x20

    # 0x02 motor
    NOTHOME: int = 0x80
    ACDCDIS: int = 0x40
    AGOHOME: int = 0x20
    MTRPWR: int = 0x10
    FASTFED: int = 0x08
    MTRREV: int = 0x04
    HOMENEG: int = 0x02

    # 0x03 lamp
    LAMPPWR: int = 0x10
    XPASEL: int = 0x20

    # 0x04 frontend select
    FESET: int = 0x03
    FESET_ADI: int = 0x02

    # 0x06
    PWRBIT: int = 0x10

    # 0x40
    CHKVER: int = 0x10
    MOTMFLG: int = 0x02
    DATAENB: int = 0x01

    # 0x41 status bits (scanner_read_status)
    STATUS_PWRBIT: int = 0x80
    STATUS_BUFEMPTY: int = 0x40
    STATUS_FEEDFSH: int = 0x20
    STATUS_SCANFSH: int = 0x10
    STATUS_HOMESNR: int = 0x08
    STATUS_LAMPSTS: int = 0x04
    STATUS_FEBUSY: int = 0x02
    STATUS_MOTORENB: int = 0x01

    # IR lamp GPIO bit for 8200i (command_set_common.cpp)
    IR_LAMP_A8_MASK: int = 0x04


@dataclass(frozen=True, slots=True)
class Gl128Registers:
    """GL128 (GL124-family) addresses confirmed from SE USB captures.

    Also used by OpticFilm 8100 (V2). Status uses the same bit layout as GL845
    register ``0x41`` but lives at the high address ``0x101``; every value
    observed across the five capture sessions decodes consistently under that
    layout (session 04 NOTES).
    """

    REG_0x01: int = 0x01
    REG_0x02: int = 0x02
    REG_0x03: int = 0x03
    REG_CLRCNT: int = 0x0D  # write 0x07 to clear line/motor/feed counters
    REG_START: int = 0x0F  # write 0x01 to launch the configured operation
    REG_LINCNT: int = 0x25  # 24-bit BE, lines x image_lincnt_per_line (not 7200 dpi units)
    REG_LPERIOD: int = 0x28  # 24-bit BE line exposure period
    REG_DPISET: int = 0x2C  # 16-bit BE, equals dpi / 6
    REG_DEPTH_A: int = 0x33  # 0x04 = 16-bit output, 0x1F = 8-bit
    REG_IR: int = 0x37  # bit 2 enables the infrared LED (read-modify-write)
    REG_FEEDL: int = 0x3D  # 24-bit BE feed distance for move-only operations
    REG_FE_INDEX: int = 0x51  # frontend register index
    REG_FE_HIGH: int = 0x5D  # frontend value, high byte
    REG_FE_LOW: int = 0x5E  # frontend value, low byte
    REG_EXPOSURE: int = 0x7D  # 24-bit BE base exposure (14000)
    REG_STRPIXEL: int = 0x82  # 24-bit BE, native 7200 dpi units
    REG_ENDPIXEL: int = 0x85  # 24-bit BE, native 7200 dpi units
    REG_DEPTH_B: int = 0xAF  # 0x46 = 16-bit output, 0xFF = 8-bit
    REG_STATUS: int = 0x101  # high-address read; GL845 0x41 bit layout

    # 0x01 scan control
    SCAN: int = 0x01
    SHDAREA: int = 0x02
    STAGGER: int = 0x10
    DVDSET: int = 0x20

    # 0x02 motor
    AGOHOME: int = 0x20
    MTRPWR: int = 0x10
    FASTFED: int = 0x08
    MTRREV: int = 0x04

    # 0x03 lamp: XPASEL is held for every transparency operation, LAMPPWR gates
    # the white lamp. IR passes clear LAMPPWR and keep XPASEL (session 05).
    LAMPPWR: int = 0x10
    XPASEL: int = 0x20
    AVEENB: int = 0x40

    # 0x37 infrared LED enable (session 05: read 0xB0, write 0xB4)
    IR_LED: int = 0x04

    # Values written to REG_CLRCNT / REG_START to run one operation.
    CLRCNT_ALL: int = 0x07
    START_GO: int = 0x01

    # 16-bit vs 8-bit output pairs, verified by delivered byte counts.
    DEPTH16_A: int = 0x04
    DEPTH16_B: int = 0x46
    DEPTH8_A: int = 0x1F
    DEPTH8_B: int = 0xFF

    # Bulk preamble wIndex values: 0x00 for RAM/calibration reads, 0x08 for the
    # image stream, 0x01 for AHB table uploads.
    BULK_INDEX_RAM: int = 0x00
    BULK_INDEX_IMAGE: int = 0x08

    # AHB windows (session 03/04): per-channel exposure, motor slopes, shading.
    AHB_CHANNEL_R: int = 0x10000000
    AHB_CHANNEL_G: int = 0x10004000
    AHB_CHANNEL_B: int = 0x10008000
    AHB_SLOPE_SCAN: int = 0x1000C000
    AHB_SLOPE_FAST: int = 0x10010000
    AHB_SHADING: int = 0x10014000
