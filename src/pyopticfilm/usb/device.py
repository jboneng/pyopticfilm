# SPDX-License-Identifier: GPL-3.0-or-later
"""USB enumeration, claim, and transfer helpers (Phase 1)."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from pyopticfilm.exceptions import (
    DeviceNotFoundError,
    DriverBindingError,
    UnsupportedDeviceError,
    UsbError,
)
from pyopticfilm.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

# OpticFilm 8200i (GL845) — probe-only in this release
VID_PLUSTEK = 0x07B3
PID_OPTICFILM_8200I = 0x130D

# OpticFilm 8200i SE (GL128) — scan-ready in this release
PID_OPTICFILM_8200I_SE = 0x1825

# OpticFilm 8100 V2 (GL128) — scan-ready in this release (no IR)
PID_OPTICFILM_8100_V2 = 0x1824

# OpticFilm 8300i SE (GL128) — enumerate / capture-decode; scan_ready=False
PID_OPTICFILM_8300I_SE = 0x181F

# Other SANE genesys :complete OpticFilm PIDs (probe-only until validated)
PID_OPTICFILM_7200 = 0x0807
PID_OPTICFILM_7200I = 0x0C04
PID_OPTICFILM_7200_V2 = 0x0C07
PID_OPTICFILM_7300 = 0x0C12
PID_OPTICFILM_7400 = 0x0C3A  # bcd 0x0400 → 7300 tables; bcd 0x0605 → 7400-v2
PID_OPTICFILM_7500I = 0x0C13
PID_OPTICFILM_7600I = 0x0C3B  # bcd 0x0400 → 7500i; bcd 0x0605 → 8200i
PID_OPTICFILM_8100 = 0x130C

SUPPORTED_IDS: frozenset[tuple[int, int]] = frozenset(
    {
        (VID_PLUSTEK, PID_OPTICFILM_8200I),
        (VID_PLUSTEK, PID_OPTICFILM_8200I_SE),
        (VID_PLUSTEK, PID_OPTICFILM_8100_V2),
        (VID_PLUSTEK, PID_OPTICFILM_8300I_SE),
        (VID_PLUSTEK, PID_OPTICFILM_7200),
        (VID_PLUSTEK, PID_OPTICFILM_7200I),
        (VID_PLUSTEK, PID_OPTICFILM_7200_V2),
        (VID_PLUSTEK, PID_OPTICFILM_7300),
        (VID_PLUSTEK, PID_OPTICFILM_7400),
        (VID_PLUSTEK, PID_OPTICFILM_7500I),
        (VID_PLUSTEK, PID_OPTICFILM_7600I),
        (VID_PLUSTEK, PID_OPTICFILM_8100),
    }
)

DEFAULT_TIMEOUT_MS = 5000
# SANE genesys bulk ceiling (low.h BULKOUT_MAXSIZE); GL845 uses per-chunk headers.
BULK_MAX_SIZE = 0xF000


@dataclass(frozen=True)
class UsbDeviceInfo:
    """Bus-visible device identity (no open handle)."""

    vendor_id: int
    product_id: int
    bus: int | None = None
    address: int | None = None
    manufacturer: str | None = None
    product: str | None = None
    bcd_device: int = 0

    @property
    def device_id(self) -> str:
        bus = self.bus if self.bus is not None else 0
        addr = self.address if self.address is not None else 0
        return f"plustek:usb:{self.vendor_id:04x}:{self.product_id:04x}:{bus:03d}:{addr:03d}"

    @property
    def is_supported(self) -> bool:
        return (self.vendor_id, self.product_id) in SUPPORTED_IDS

    @property
    def is_8200i_se(self) -> bool:
        """True for the OpticFilm 8200i SE (GL128)."""
        return (
            self.vendor_id == VID_PLUSTEK and self.product_id == PID_OPTICFILM_8200I_SE
        )

    @property
    def asic_hint(self) -> str:
        from pyopticfilm.device.select import model_for_device

        try:
            return model_for_device(self.product_id, self.bcd_device).asic
        except Exception:  # noqa: BLE001
            return "unknown"

    @property
    def is_known_unsupported(self) -> bool:
        """Deprecated alias: SE is no longer rejected on open."""
        return False


def _require_usb():
    try:
        import usb.core
        import usb.util
    except ImportError as exc:
        raise DeviceNotFoundError(
            "PyUSB is not available. Reinstall NegPy dependencies (pyusb is required)."
        ) from exc

    # Bundled libusb-1.0 for Windows/macOS when system libusb is not on PATH.
    with contextlib.suppress(ImportError):
        import libusb_package  # noqa: F401

    return usb.core, usb.util


def _usb_backend():
    """Return a libusb1 backend, preferring libusb-package's bundled DLL on Windows."""
    try:
        import libusb_package

        backend = libusb_package.get_libusb1_backend()
        if backend is not None:
            return backend
    except ImportError:
        pass
    return None


def _usb_find(usb_core, **kwargs):
    backend = _usb_backend()
    if backend is not None:
        kwargs["backend"] = backend
    try:
        return usb_core.find(**kwargs)
    except usb_core.NoBackendError as exc:
        raise _usb_backend_error(exc) from exc


def _usb_backend_error(exc: BaseException) -> DriverBindingError:
    return DriverBindingError(
        "PyUSB has no USB backend (libusb). Install pyopticfilm with libusb-package "
        "(pip install pyopticfilm) on Windows, or place libusb-1.0.dll on PATH."
    )


def _info_from_pyusb(dev, usb_util, *, read_strings: bool = False) -> UsbDeviceInfo:
    """Build :class:`UsbDeviceInfo` from a PyUSB device.

    String descriptors are skipped by default: ``get_string`` opens the device
    and can hang indefinitely on Windows (WinUSB / busy handle), which freezes
    PlustekLab's Refresh worker. VID/PID/bcd are enough for listing.
    """
    manufacturer = None
    product = None
    if read_strings:
        try:
            if dev.iManufacturer:
                manufacturer = usb_util.get_string(dev, dev.iManufacturer)
            if dev.iProduct:
                product = usb_util.get_string(dev, dev.iProduct)
        except (ValueError, OSError, NotImplementedError):
            # Reading strings opens the device, which fails while a vendor driver
            # owns it (libusb reports NotImplementedError on Windows). Listing must
            # still work so the user can see the PID and go bind WinUSB.
            pass
    bcd = int(getattr(dev, "bcdDevice", 0) or 0)
    return UsbDeviceInfo(
        vendor_id=int(dev.idVendor),
        product_id=int(dev.idProduct),
        bus=getattr(dev, "bus", None),
        address=getattr(dev, "address", None),
        manufacturer=manufacturer,
        product=product,
        bcd_device=bcd,
    )


def list_devices(*, read_strings: bool = False) -> list[UsbDeviceInfo]:
    """Return Plustek film scanners visible to libusb (supported and known-bad).

    By default does not open devices for USB string descriptors (safe / non-hanging
    enumerate). Pass ``read_strings=True`` only when a product name is required.
    """
    usb_core, usb_util = _require_usb()
    found: list[UsbDeviceInfo] = []
    devices = _usb_find(usb_core, find_all=True, idVendor=VID_PLUSTEK) or []
    for dev in devices:
        info = _info_from_pyusb(dev, usb_util, read_strings=read_strings)
        found.append(info)
        logger.debug("Found %s supported=%s", info.device_id, info.is_supported)
    return found


def find_devices(*, supported_only: bool = True) -> list[UsbDeviceInfo]:
    """List devices, optionally filtering to supported VID/PID pairs."""
    devices = list_devices()
    if not supported_only:
        return devices
    return [d for d in devices if d.is_supported]


def _map_usb_exception(exc: BaseException) -> Exception:
    """Translate PyUSB errors into pyopticfilm exceptions."""
    message = str(exc).strip() or exc.__class__.__name__
    lowered = message.lower()
    access_hints = (
        "access denied",
        "permission",
        "busy",
        "entity not found",
        "not supported",
        "winerror",
        "libusb0",
        "could not claim",
        "device not opened",
    )
    if any(h in lowered for h in access_hints):
        return DriverBindingError(
            f"{message}. On Windows, bind WinUSB with Zadig — see docs/windows-setup.md."
        )
    return UsbError(message)


def _pick_preferred_device(devices: list[UsbDeviceInfo]) -> UsbDeviceInfo:
    """Prefer a scan-ready model when several OpticFilms are present."""
    from pyopticfilm.device.select import model_for_device, model_is_scan_ready

    ready: list[UsbDeviceInfo] = []
    for info in devices:
        try:
            model = model_for_device(info.product_id, info.bcd_device)
        except Exception:  # noqa: BLE001, S112
            continue
        if model_is_scan_ready(model):
            ready.append(info)
    return ready[0] if ready else devices[0]


class UsbDeviceHandle:
    """Opened and claimed USB device with control + bulk transfers."""

    def __init__(self, info: UsbDeviceInfo) -> None:
        if not info.is_supported:
            raise UnsupportedDeviceError(
                f"Device {info.device_id} is not a known OpticFilm. "
                f"This release scans OpticFilm 8200i SE (07b3:1825) and "
                f"OpticFilm 8100 (V2) (07b3:1824); other PIDs may open for "
                f"probe/status only."
            )
        self.info = info
        self._dev = None
        self._interface = 0
        self._ep_in = None
        self._ep_out = None
        self._claimed = False
        self._needs_usb_reset = False
        self.timeout_ms = DEFAULT_TIMEOUT_MS

    @classmethod
    def open(cls, device_id: str | None = None) -> UsbDeviceHandle:
        """Open by ``device_id``, or prefer a scan-ready model among supported devices."""
        devices = find_devices(supported_only=True)
        if device_id is not None:
            match = next((d for d in devices if d.device_id == device_id), None)
            if match is None:
                all_devs = list_devices()
                bad = next((d for d in all_devs if d.device_id == device_id), None)
                if bad is not None:
                    return cls(bad)  # raises UnsupportedDeviceError in __init__
                raise DeviceNotFoundError(f"No device with id {device_id!r}")
            handle = cls(match)
        else:
            if not devices:
                raise DeviceNotFoundError(
                    "No OpticFilm found. This release scans OpticFilm 8200i SE "
                    "(07b3:1825) and OpticFilm 8100 (V2) (07b3:1824). Check "
                    "cabling and WinUSB binding — see docs/windows-setup.md."
                )
            handle = cls(_pick_preferred_device(devices))
        handle._open()
        return handle

    def _find_pyusb_device(self):
        usb_core, _usb_util = _require_usb()
        matches = list(
            _usb_find(
                usb_core,
                find_all=True,
                idVendor=self.info.vendor_id,
                idProduct=self.info.product_id,
            )
            or []
        )
        if not matches:
            raise DeviceNotFoundError(
                f"Device {self.info.device_id} disappeared before open."
            )

        if self.info.bus is not None and self.info.address is not None:
            for dev in matches:
                if getattr(dev, "bus", None) == self.info.bus and getattr(
                    dev, "address", None
                ) == self.info.address:
                    return dev

        if len(matches) == 1:
            return matches[0]

        raise DeviceNotFoundError(
            f"Ambiguous match for {self.info.device_id}; "
            f"found {len(matches)} devices with same VID/PID."
        )

    def _open(self) -> None:
        usb_core, usb_util = _require_usb()
        dev = self._find_pyusb_device()

        try:
            try:
                if dev.is_kernel_driver_active(0):
                    logger.debug("Detaching kernel driver on interface 0")
                    dev.detach_kernel_driver(0)
            except (NotImplementedError, AttributeError):
                pass
            except usb_core.USBError as exc:
                logger.debug("Kernel detach skipped: %s", exc)

            try:
                dev.set_configuration()
            except usb_core.USBError as exc:
                # Already configured is common and fine.
                logger.debug("set_configuration: %s", exc)

            cfg = dev.get_active_configuration()
            intf = cfg[(0, 0)]
            self._interface = int(intf.bInterfaceNumber)

            try:
                usb_util.claim_interface(dev, self._interface)
            except usb_core.USBError as exc:
                raise _map_usb_exception(exc) from exc

            self._ep_in = usb_util.find_descriptor(
                intf,
                custom_match=lambda e: usb_util.endpoint_direction(e.bEndpointAddress)
                == usb_util.ENDPOINT_IN
                and usb_util.endpoint_type(e.bmAttributes) == usb_util.ENDPOINT_TYPE_BULK,
            )
            self._ep_out = usb_util.find_descriptor(
                intf,
                custom_match=lambda e: usb_util.endpoint_direction(e.bEndpointAddress)
                == usb_util.ENDPOINT_OUT
                and usb_util.endpoint_type(e.bmAttributes) == usb_util.ENDPOINT_TYPE_BULK,
            )

            self._dev = dev
            self._claimed = True
            self._needs_usb_reset = False
            logger.info(
                "Opened %s (iface=%s ep_in=%s ep_out=%s)",
                self.info.device_id,
                self._interface,
                None if self._ep_in is None else hex(self._ep_in.bEndpointAddress),
                None if self._ep_out is None else hex(self._ep_out.bEndpointAddress),
            )
        except (DriverBindingError, DeviceNotFoundError, UnsupportedDeviceError):
            self.close()
            raise
        except usb_core.USBError as exc:
            self.close()
            raise _map_usb_exception(exc) from exc
        except Exception:
            self.close()
            raise

    @property
    def is_open(self) -> bool:
        return self._dev is not None and self._claimed

    def close(self) -> None:
        if self._dev is None:
            self._claimed = False
            return
        try:
            import usb.util

            # leave EP0 wedged for the next open unless we reset the device.
            if self._needs_usb_reset:
                try:
                    # Reset while still claimed — more reliable on WinUSB than
                    # after release_interface.
                    self._dev.reset()
                    logger.info("USB device reset after aborted bulk stream")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("USB reset after aborted bulk failed: %s", exc)
                self._needs_usb_reset = False
            if self._claimed:
                try:
                    usb.util.release_interface(self._dev, self._interface)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("release_interface: %s", exc)
            try:
                usb.util.dispose_resources(self._dev)
            except Exception as exc:  # noqa: BLE001
                logger.debug("dispose_resources: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.debug("close cleanup: %s", exc)
        finally:
            self._dev = None
            self._claimed = False
            self._ep_in = None
            self._ep_out = None
            self._needs_usb_reset = False
            logger.debug("Closed %s", self.info.device_id)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if not self.is_open:
            raise UsbError("USB device is not open.")

    def control_msg(
        self,
        request_type: int,
        request: int,
        value: int,
        index: int,
        data_or_length: int | Sequence[int] | bytes | bytearray,
        *,
        timeout_ms: int | None = None,
    ) -> bytes:
        """Vendor control transfer (SANE ``control_msg`` equivalent)."""
        self._ensure_open()
        assert self._dev is not None
        usb_core, _usb_util = _require_usb()
        timeout = self.timeout_ms if timeout_ms is None else timeout_ms
        try:
            result = self._dev.ctrl_transfer(
                request_type,
                request,
                value,
                index,
                data_or_length,
                timeout=timeout,
            )
        except usb_core.USBError as exc:
            mapped = _map_usb_exception(exc)
            logger.error(
                "control_msg failed type=0x%02x req=0x%02x val=0x%04x idx=0x%04x: %s",
                request_type,
                request,
                value,
                index,
                mapped,
            )
            raise mapped from exc

        if isinstance(data_or_length, int):
            return bytes(result)
        return b""

    def bulk_read(self, size: int, *, timeout_ms: int | None = None) -> bytes:
        """Read up to ``size`` bytes from the bulk IN endpoint."""
        self._ensure_open()
        if self._ep_in is None:
            raise UsbError("Device has no bulk IN endpoint.")
        usb_core, _usb_util = _require_usb()
        timeout = self.timeout_ms if timeout_ms is None else timeout_ms
        try:
            data = self._dev.read(  # type: ignore[union-attr]
                self._ep_in.bEndpointAddress,
                size,
                timeout=timeout,
            )
        except usb_core.USBError as exc:
            raise _map_usb_exception(exc) from exc
        return bytes(data)

    def clear_halt(self, *, endpoint: int | None = None) -> None:
        """Clear a stall on ``endpoint`` (default: bulk IN).

        Needed after abandoning a Genesys image bulk stream mid-transfer;
        otherwise the next open's control transfers can hang until power-cycle.
        """
        self._ensure_open()
        assert self._dev is not None
        if endpoint is None:
            if self._ep_in is None:
                return
            endpoint = int(self._ep_in.bEndpointAddress)
        usb_core, _usb_util = _require_usb()
        try:
            self._dev.clear_halt(endpoint)
            logger.info("clear_halt endpoint=0x%02x", endpoint)
        except usb_core.USBError as exc:
            logger.warning("clear_halt endpoint=0x%02x failed: %s", endpoint, exc)

    def abort_bulk_in(
        self,
        *,
        max_drain_bytes: int = BULK_MAX_SIZE * 4,
        chunk_size: int = BULK_MAX_SIZE,
        drain_timeout_ms: int = 50,
    ) -> int:
        """Drain residual bulk IN then :meth:`clear_halt`.

        Call after mid-scan cancel once SCAN is cleared so the ASIC stops
        producing. Returns bytes drained (best-effort).
        """
        self._ensure_open()
        if self._ep_in is None:
            return 0
        usb_core, _usb_util = _require_usb()
        drained = 0
        while drained < max_drain_bytes:
            try:
                data = self._dev.read(  # type: ignore[union-attr]
                    self._ep_in.bEndpointAddress,
                    min(chunk_size, max_drain_bytes - drained),
                    timeout=drain_timeout_ms,
                )
            except usb_core.USBError as exc:
                # Timeout / pipe errors mean the pipe is idle or stalled — halt clear next.
                logger.debug("abort_bulk_in drain stop after %d bytes: %s", drained, exc)
                break
            if not data:
                break
            drained += len(data)
        self.clear_halt()
        if self._ep_out is not None:
            try:
                self.clear_halt(endpoint=int(self._ep_out.bEndpointAddress))
            except Exception as exc:  # noqa: BLE001
                logger.debug("abort_bulk_in OUT clear_halt: %s", exc)
        self._needs_usb_reset = True
        logger.info("abort_bulk_in drained=%d bytes (usb reset on close)", drained)
        return drained

    def bulk_write(self, data: bytes | bytearray, *, timeout_ms: int | None = None) -> int:
        """Write bytes to the bulk OUT endpoint; returns bytes written."""
        self._ensure_open()
        if self._ep_out is None:
            raise UsbError("Device has no bulk OUT endpoint.")
        usb_core, _usb_util = _require_usb()
        timeout = self.timeout_ms if timeout_ms is None else timeout_ms
        payload = bytes(data)
        try:
            written = self._dev.write(  # type: ignore[union-attr]
                self._ep_out.bEndpointAddress,
                payload,
                timeout=timeout,
            )
        except usb_core.USBError as exc:
            raise _map_usb_exception(exc) from exc
        return int(written)
