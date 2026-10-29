#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Inspect USB descriptors for connected OpticFilm 7600i scanners."""
import sys

import usb.core
import usb.util

devices = list(usb.core.find(idVendor=0x07b3, idProduct=0x0c3b, find_all=True) or ())
if not devices:
    print("Device not found")
    sys.exit(1)

if len(devices) > 1:
    print(f"Found {len(devices)} matching scanners; check bus and address for the target device.")

for index, dev in enumerate(devices, start=1):
    print(f"Device {index}: bus={dev.bus} address={dev.address}")
    print(f"  Vendor ID: 0x{dev.idVendor:04x}")
    print(f"  Product ID: 0x{dev.idProduct:04x}")
    print(f"  bcdDevice: 0x{dev.bcdDevice:04x} ({dev.bcdDevice} decimal)")

    try:
        print(f"  Product string: {usb.util.get_string(dev, dev.iProduct)}")
    except usb.core.USBError as exc:
        print(f"  Could not read product string: {exc}")

    try:
        print(f"  Device configuration: {dev.get_active_configuration()}")
    except usb.core.USBError as exc:
        print(f"  Could not read device configuration: {exc}")
