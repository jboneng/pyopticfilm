# SPDX-License-Identifier: GPL-3.0-or-later
"""Normalize and compare scanner USB traces / register programs."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyopticfilm.usb.protocol import (
    REQUEST_BUFFER,
    REQUEST_TYPE_IN,
    VALUE_GET_REGISTER,
)
from scanners.fake_usb import UsbTransaction

# GL845 status register encoded in GET_REGISTER wIndex: 0x22 + (0x41 << 8).
_STATUS_READ_INDEX = (0x22 + (0x41 << 8)) & 0xFFFF

# GL128 optical / scan-setup registers (STR/END at 0x82/0x85, LPERIOD at 0x28).
GL128_OPTICAL_REGISTER_KEYS: tuple[int, ...] = (
    0x01,  # SCAN / SHDAREA / DVDSET
    0x02,  # motor / AGOHOME
    0x2B,  # dummy
    0x2C,
    0x2D,  # DPISET
    0x25,
    0x26,
    0x27,  # LINCNT
    0x28,
    0x29,
    0x2A,  # LPERIOD
    0x82,
    0x83,
    0x84,  # STRPIXEL
    0x85,
    0x86,
    0x87,  # ENDPIXEL
    0x7D,
    0x7E,
    0x7F,  # EXPOSURE
    0xA5,
    0xAB,  # pixel clock
)

#: Per-scan sensor timing (``sensor_custom_regs``) and ``0x1D``. These differ
#: per model (8300i SE overlay) and per pass (``0x1D=0x81`` on a 7200 single /
#: ME-short pass, ``0x80`` on the 7200 ME long pass).
GL128_SENSOR_REGISTER_KEYS: tuple[int, ...] = (
    0x06,
    0x0B,
    0x1D,
    0x52,
    0x53,
    0x54,
    0x55,
    0x56,
    0x57,
    0x5A,
    0x5B,
    0x70,
    0x71,
    0x72,
    0x73,
)

# Optical / scan-setup registers compared against SANE (not the full boot blast).
OPTICAL_REGISTER_KEYS: tuple[int, ...] = (
    0x01,  # SCAN / SHDAREA / DVDSET
    0x02,  # motor
    0x03,  # lamp
    0x04,  # bit depth / AFE mode
    0x05,  # dpihw / gamma
    0x2C,
    0x2D,  # DPISET
    0x30,
    0x31,  # STRPIXEL
    0x32,
    0x33,  # ENDPIXEL
    0x34,  # dummy pixel
    0x35,
    0x36,
    0x37,  # MAXWD
    0x38,
    0x39,  # LPERIOD
    0x25,
    0x26,
    0x27,  # LINCNT
    0x3D,
    0x3E,
    0x3F,  # FEEDL
)


@dataclass(frozen=True)
class TraceFile:
    meta: dict[str, Any]
    transactions: list[UsbTransaction]
    registers: dict[int, int]


def registers_to_json(regs: dict[int, int]) -> dict[str, str]:
    return {f"0x{addr:02x}": f"{int(value) & 0xFF:02x}" for addr, value in sorted(regs.items())}


def registers_from_json(obj: dict[str, Any]) -> dict[int, int]:
    out: dict[int, int] = {}
    for key, value in obj.items():
        addr = int(str(key), 0)
        out[addr] = int(str(value), 16) if isinstance(value, str) else int(value) & 0xFF
    return out


def dump_trace(
    path: Path | str,
    *,
    transactions: Iterable[UsbTransaction],
    registers: dict[int, int] | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    payload = {
        "meta": meta or {},
        "registers": registers_to_json(registers or {}),
        "transactions": [txn.to_json() for txn in transactions],
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def load_trace(path: Path | str) -> TraceFile:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    txns = [UsbTransaction.from_json(item) for item in raw.get("transactions", [])]
    return TraceFile(
        meta=dict(raw.get("meta") or {}),
        transactions=txns,
        registers=registers_from_json(raw.get("registers") or {}),
    )


def extract_register_writes(transactions: Iterable[UsbTransaction]) -> dict[int, int]:
    """Final register map from GL845 SET_REGISTER control writes."""
    regs: dict[int, int] = {}
    for txn in transactions:
        if txn.operation != "control_write":
            continue
        if txn.request != REQUEST_BUFFER:
            continue
        value = int(txn.value or 0)
        if (value & 0xFF) != 0x83:  # VALUE_SET_REGISTER
            continue
        payload = txn.data or b""
        high = 0x100 if value & 0x100 else 0
        for i in range(0, len(payload) - 1, 2):
            regs[high | payload[i]] = payload[i + 1]
    return regs


def _is_status_poll(txn: UsbTransaction) -> bool:
    return (
        txn.operation == "control_read"
        and txn.request_type == REQUEST_TYPE_IN
        and txn.request == REQUEST_BUFFER
        and (int(txn.value or 0) & 0xFF) == VALUE_GET_REGISTER
        and int(txn.index or 0) == _STATUS_READ_INDEX
    )


def collapse_status_polls(transactions: Iterable[UsbTransaction]) -> list[UsbTransaction]:
    """Keep the first of each consecutive GL845 status (0x41) read run."""
    out: list[UsbTransaction] = []
    in_run = False
    for txn in transactions:
        if _is_status_poll(txn):
            if in_run:
                continue
            in_run = True
            out.append(txn)
            continue
        in_run = False
        out.append(txn)
    return out


def _txn_compare_key(txn: UsbTransaction) -> tuple:
    return (
        txn.operation,
        txn.request_type,
        txn.request,
        txn.value,
        txn.index,
        txn.endpoint,
        txn.data,
        txn.length,
    )


def _hex_dump(data: bytes, *, width: int = 16) -> str:
    if not data:
        return "(empty)"
    lines: list[str] = []
    for offset in range(0, len(data), width):
        chunk = data[offset : offset + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        lines.append(f"{offset:08x}: {hex_part}")
    return "\n".join(lines)


def _first_byte_diff(expected: bytes, actual: bytes) -> tuple[int, int | None, int | None]:
    n = max(len(expected), len(actual))
    for i in range(n):
        e = expected[i] if i < len(expected) else None
        a = actual[i] if i < len(actual) else None
        if e != a:
            return i, e, a
    return -1, None, None


def format_transaction(txn: UsbTransaction) -> str:
    if txn.operation == "bulk_read":
        return f"bulk_read endpoint=0x{int(txn.endpoint or 0):02x} length={txn.length}"
    if txn.operation == "bulk_write":
        payload = txn.data or b""
        return f"bulk_write endpoint=0x{int(txn.endpoint or 0):02x}\n{_hex_dump(payload)}"
    bits = [
        txn.operation,
        f"type=0x{int(txn.request_type or 0):02x}",
        f"req=0x{int(txn.request or 0):02x}",
        f"value=0x{int(txn.value or 0):04x}",
        f"index=0x{int(txn.index or 0):04x}",
    ]
    if txn.length is not None:
        bits.append(f"length={txn.length}")
    if txn.data is not None:
        bits.append("\n" + _hex_dump(txn.data))
    return " ".join(bits) if txn.data is None else " ".join(bits[:-1]) + bits[-1]


def compare_transactions(
    expected: list[UsbTransaction],
    actual: list[UsbTransaction],
    *,
    collapse_polls: bool = True,
) -> str | None:
    """Return a human-readable first difference, or ``None`` if equal."""
    left = collapse_status_polls(expected) if collapse_polls else list(expected)
    right = collapse_status_polls(actual) if collapse_polls else list(actual)
    n = min(len(left), len(right))
    for i in range(n):
        if _txn_compare_key(left[i]) == _txn_compare_key(right[i]):
            continue
        exp, act = left[i], right[i]
        lines = [f"Transaction {i} differs", "", "Expected:", format_transaction(exp), "", "Actual:", format_transaction(act)]
        if exp.data is not None and act.data is not None and exp.data != act.data:
            off, e_byte, a_byte = _first_byte_diff(exp.data, act.data)
            caret = " " * (10 + 3 * (off % 16)) + "^^"
            lines.append("")
            lines.append(f"byte offset {off}: expected 0x{(e_byte or 0):02x} actual 0x{(a_byte or 0):02x}")
            lines.append(caret)
        elif exp.operation != act.operation:
            lines.append("")
            lines.append(f"operation {exp.operation} vs {act.operation}")
        return "\n".join(lines)
    if len(left) != len(right):
        longer = left if len(left) > len(right) else right
        label = "expected" if len(left) > len(right) else "actual"
        extra = longer[n]
        return (
            f"Trace length differs: expected {len(left)} transactions, "
            f"actual {len(right)}.\nFirst extra ({label}): {format_transaction(extra)}"
        )
    return None


def compare_registers(
    expected: dict[int, int],
    actual: dict[int, int],
    *,
    keys: Iterable[int] | None = None,
) -> str | None:
    addrs = list(keys) if keys is not None else sorted(set(expected) | set(actual))
    diffs: list[str] = []
    for addr in addrs:
        if addr not in expected or addr not in actual:
            if addr in expected and addr not in actual:
                diffs.append(f"register 0x{addr:02x}: expected 0x{expected[addr]:02x}, missing in actual")
            elif addr in actual and addr not in expected:
                diffs.append(f"register 0x{addr:02x}: unexpected 0x{actual[addr]:02x}")
            continue
        if (expected[addr] & 0xFF) != (actual[addr] & 0xFF):
            diffs.append(
                f"register 0x{addr:02x}: expected 0x{expected[addr]:02x} actual 0x{actual[addr]:02x}"
            )
    if not diffs:
        return None
    return "Register program differs\n" + "\n".join(diffs)
