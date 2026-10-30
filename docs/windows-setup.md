# Windows USB setup

pyopticfilm uses PyUSB and libusb to talk to the scanner directly. On Windows, the Plustek vendor driver (installed with SilverFast, VueScan, or Plustek utilities) normally owns the device. You must bind **WinUSB** (or libusbK) on the scanner’s USB interface so libusb can open and claim it.

This guide applies to all supported OpticFilm models (`07b3:…`). **Scanning is validated on the OpticFilm 8200i SE** (`07b3:1825`), **OpticFilm 8100 (V2)** (`07b3:1824`), **OpticFilm 8300i SE** (`07b3:181f`), **OpticFilm 7600i v1** (`07b3:0c3b`, bcdDevice 4.00), **and OpticFilm 7600i v2** (`07b3:0c3b`, bcdDevice 6.05); other models may open for status and register probes only.

## Prerequisites

1. Install pyopticfilm (bundled libusb on Windows):

   ```powershell
   pip install pyopticfilm
   ```

2. Connect the scanner with USB and power it on.

3. Download [Zadig](https://zadig.akeo.ie/) (portable, no installer required).

## Bind WinUSB with Zadig

1. Run Zadig **as Administrator** (right-click → Run as administrator).

2. Enable **Options → List All Devices** so the Plustek scanner appears even when another driver is loaded.

3. In the device dropdown, select your film scanner. Typical entries:

   | Product | USB ID | Notes |
   |---------|--------|--------|
   | OpticFilm 8200i SE | `07B3 1825` | GL128 — scan-ready |
   | OpticFilm 8100 (V2) | `07B3 1824` | GL128 — scan-ready (no IR) |
   | OpticFilm 8300i SE | `07B3 181F` | GL128 — scan-ready (IR capable) |
   | OpticFilm 7600i v1 | `07B3 0C3B` | GL843 — scan-ready (bcdDevice 4.00; IR capable) |
   | OpticFilm 7600i v2 | `07B3 0C3B` | GL845 — scan-ready (bcdDevice 6.05; IR capable) |
   | OpticFilm 8200i | `07B3 130D` | GL845 — probe only |
   | Other OpticFilm | `07B3 0Cxx` / `07B3 130C` | See README hardware table |

   Match **USB ID** (`07B3` vendor, product ID as above), not only the friendly name.
   The GL845 OpticFilm 8100 (`07B3 130C`) is not the same device as 8100 (V2).

4. Confirm the **USB ID** field shows `07B3` and the correct product ID.

5. Set the target driver to **WinUSB** (recommended). **libusbK** also works with PyUSB’s libusb backend.

6. Click **Replace Driver** (or **Install Driver** if no driver is present). Wait until Zadig reports success.

7. Unplug and replug the scanner, or power-cycle it.

You only need to do this once per Windows installation for that device. Windows Update may occasionally revert the binding; repeat Zadig if access errors return after an update.

## Verify from Python

```python
from pyopticfilm.usb.device import find_devices

for d in find_devices():
    print(d.device_id, f"pid=0x{d.product_id:04x}", d.asic_hint)
```

Then open the scanner:

```python
from pyopticfilm import Scanner

with Scanner.open() as s:
    print(s.model.model, s.status())
```

If `Scanner.open()` works, WinUSB binding is correct.

## Common errors

### `DriverBindingError` / access denied / could not claim

The Plustek vendor driver still owns the interface. Run Zadig again as Administrator, select the correct `07B3:…` device, and replace the driver with WinUSB.

Close SilverFast, VueScan, and Plustek utilities before binding or opening the device—they can hold the handle and block claim.

### `DeviceNotFoundError` / no OpticFilm found

- Check USB cable and power; wait for the scanner to finish booting.
- Confirm the device appears in Zadig with `07B3` vendor ID.
- For scanning, you need an **8200i SE** (`07b3:1825`), **8100 (V2)** (`07b3:1824`), **8300i SE** (`07b3:181f`), **7600i v1** (`07b3:0c3b`, bcdDevice 4.00), or **7600i v2** (`07b3:0c3b`, bcdDevice 6.05). Other OpticFilm PIDs are listed but not scan-validated.
- Run `find_devices()` as above to see what libusb enumerates.

### `NoBackendError` / no USB backend

Reinstall so `libusb-package` is present:

```powershell
pip install --force-reinstall pyopticfilm
```

Ensure `libusb-1.0.dll` is loadable (pip’s `libusb-package` places it where PyUSB expects on Windows).

### Enumeration hangs or “Refresh” never returns

pyopticfilm avoids reading USB string descriptors during normal listing because `get_string` can block on Windows when WinUSB is not bound or another program holds the device. Use VID/PID from Zadig or `find_devices()` without `read_strings=True`.

### Ambiguous match for same VID/PID

Two identical scanners on one PC require opening by explicit `device_id` from `find_devices()`:

```python
Scanner.open(device_id="plustek:usb:07b3:1825:001:002")
```

## Using SilverFast or the vendor driver again

To restore the Plustek driver:

1. Open **Device Manager**.
2. Find the scanner under **Universal Serial Bus devices** or **libusb-win32 devices** / **WinUsb Device**.
3. Right-click → **Update driver** → **Browse** → let Windows reinstall the manufacturer driver, or use Plustek/SilverFast installer repair.

Alternatively, use Zadig to replace WinUSB back to the original driver if it is still listed in the driver dropdown.

Switching drivers is safe for the hardware; you are only changing which software stack owns the USB interface.

## Security note

WinUSB binding allows any user-level program to access the scanner USB interface. That is required for libusb and is normal for developer tooling. Only bind devices you trust and only on machines where that exposure is acceptable.
