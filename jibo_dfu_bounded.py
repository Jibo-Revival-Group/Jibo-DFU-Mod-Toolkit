"""Read the complete, read-only GPT marker through bounded DFU uploads.

The marker is 34 sectors: protective MBR, GPT header, and partition entries.
Its final short upload resets this U-Boot loader's entity cursor, so repeated
checks can run in the same DFU session. No file or DFU_DNLOAD is created.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from pathlib import Path
import re
import sys


VID = 0x0955
PID = 0x701A
MAX_UPLOAD_BYTES = 32 * 1024
MARKER_BYTES = 34 * 512
DEFAULT_ALT_NAME = "jibo-dfu-v1"
DFU_INTERFACE = (0xFE, 0x01, 0x02)

_USB_IN = 0x80
_USB_OUT = 0x00
_USB_STANDARD = 0x00
_USB_CLASS = 0x20
_USB_INTERFACE = 0x01
_GET_DESCRIPTOR = 0x06
_DFU_UPLOAD = 0x02
_DFU_GETSTATUS = 0x03
_DFU_ABORT = 0x06
_DFU_FUNCTIONAL_DESCRIPTOR = 0x21
_DFU_CAN_UPLOAD = 0x02
_DFU_IDLE = 2
_TIMEOUT_MS = 30000
_LIBUSB_CONTROL_MAX = 4096


class BoundedDfuError(RuntimeError):
    """The selected DFU device or bounded read did not pass its checks."""


class _Device(ctypes.Structure):
    pass


class _Handle(ctypes.Structure):
    pass


_DeviceP = ctypes.POINTER(_Device)
_HandleP = ctypes.POINTER(_Handle)


class _DeviceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bcdUSB", ctypes.c_uint16),
        ("bDeviceClass", ctypes.c_uint8),
        ("bDeviceSubClass", ctypes.c_uint8),
        ("bDeviceProtocol", ctypes.c_uint8),
        ("bMaxPacketSize0", ctypes.c_uint8),
        ("idVendor", ctypes.c_uint16),
        ("idProduct", ctypes.c_uint16),
        ("bcdDevice", ctypes.c_uint16),
        ("iManufacturer", ctypes.c_uint8),
        ("iProduct", ctypes.c_uint8),
        ("iSerialNumber", ctypes.c_uint8),
        ("bNumConfigurations", ctypes.c_uint8),
    ]


class _InterfaceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bInterfaceNumber", ctypes.c_uint8),
        ("bAlternateSetting", ctypes.c_uint8),
        ("bNumEndpoints", ctypes.c_uint8),
        ("bInterfaceClass", ctypes.c_uint8),
        ("bInterfaceSubClass", ctypes.c_uint8),
        ("bInterfaceProtocol", ctypes.c_uint8),
        ("iInterface", ctypes.c_uint8),
        ("endpoint", ctypes.c_void_p),
        ("extra", ctypes.POINTER(ctypes.c_ubyte)),
        ("extra_length", ctypes.c_int),
    ]


class _Interface(ctypes.Structure):
    _fields_ = [
        ("altsetting", ctypes.POINTER(_InterfaceDescriptor)),
        ("num_altsetting", ctypes.c_int),
    ]


class _ConfigDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("wTotalLength", ctypes.c_uint16),
        ("bNumInterfaces", ctypes.c_uint8),
        ("bConfigurationValue", ctypes.c_uint8),
        ("iConfiguration", ctypes.c_uint8),
        ("bmAttributes", ctypes.c_uint8),
        ("MaxPower", ctypes.c_uint8),
        ("interface", ctypes.POINTER(_Interface)),
        ("extra", ctypes.POINTER(ctypes.c_ubyte)),
        ("extra_length", ctypes.c_int),
    ]


_ConfigDescriptorP = ctypes.POINTER(_ConfigDescriptor)


def _load_libusb():
    override = os.environ.get("JIBO_LIBUSB")
    candidates = [override] if override else [ctypes.util.find_library("usb-1.0")]
    if not override and sys.platform == "darwin":
        # dyld/find_library may not search Homebrew's keg on either architecture.
        candidates.extend(str(Path(prefix) / "lib/libusb-1.0.dylib") for prefix in (
            "/opt/homebrew/opt/libusb", "/usr/local/opt/libusb", "/opt/local"))
    errors = []
    lib = None
    for library_name in dict.fromkeys(name for name in candidates if name):
        try:
            lib = ctypes.CDLL(library_name)
            break
        except OSError as exc:
            errors.append(str(exc))
    if lib is None:
        hint = ("Install libusb with Homebrew or MacPorts and use Python and libusb of the same architecture."
                if sys.platform == "darwin" else "Install the system libusb runtime.")
        detail = " " + "; ".join(errors) if errors else ""
        raise BoundedDfuError("Could not load libusb-1.0. " + hint + detail)

    lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    lib.libusb_init.restype = ctypes.c_int
    if sys.platform == "win32":
        # Windows: use the signed UsbDk driver so no per-device driver install is needed.
        raw_init = lib.libusb_init
        def init_with_usbdk(reference):
            rc = raw_init(reference)
            if rc == 0:
                lib.libusb_set_option(reference._obj, ctypes.c_int(1))  # LIBUSB_OPTION_USE_USBDK
            return rc
        lib.libusb_init = init_with_usbdk
    lib.libusb_exit.argtypes = [ctypes.c_void_p]
    lib.libusb_exit.restype = None
    lib.libusb_get_device_list.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(_DeviceP))]
    lib.libusb_get_device_list.restype = ctypes.c_ssize_t
    lib.libusb_free_device_list.argtypes = [ctypes.POINTER(_DeviceP), ctypes.c_int]
    lib.libusb_free_device_list.restype = None
    lib.libusb_get_device_descriptor.argtypes = [_DeviceP, ctypes.POINTER(_DeviceDescriptor)]
    lib.libusb_get_device_descriptor.restype = ctypes.c_int
    lib.libusb_get_bus_number.argtypes = [_DeviceP]
    lib.libusb_get_bus_number.restype = ctypes.c_uint8
    lib.libusb_get_port_numbers.argtypes = [_DeviceP, ctypes.POINTER(ctypes.c_uint8), ctypes.c_int]
    lib.libusb_get_port_numbers.restype = ctypes.c_int
    lib.libusb_open.argtypes = [_DeviceP, ctypes.POINTER(_HandleP)]
    lib.libusb_open.restype = ctypes.c_int
    lib.libusb_close.argtypes = [_HandleP]
    lib.libusb_close.restype = None
    lib.libusb_get_active_config_descriptor.argtypes = [_DeviceP, ctypes.POINTER(_ConfigDescriptorP)]
    lib.libusb_get_active_config_descriptor.restype = ctypes.c_int
    lib.libusb_free_config_descriptor.argtypes = [_ConfigDescriptorP]
    lib.libusb_free_config_descriptor.restype = None
    lib.libusb_get_string_descriptor_ascii.argtypes = [
        _HandleP, ctypes.c_uint8, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int
    ]
    lib.libusb_get_string_descriptor_ascii.restype = ctypes.c_int
    lib.libusb_claim_interface.argtypes = [_HandleP, ctypes.c_int]
    lib.libusb_claim_interface.restype = ctypes.c_int
    lib.libusb_release_interface.argtypes = [_HandleP, ctypes.c_int]
    lib.libusb_release_interface.restype = ctypes.c_int
    lib.libusb_set_interface_alt_setting.argtypes = [_HandleP, ctypes.c_int, ctypes.c_int]
    lib.libusb_set_interface_alt_setting.restype = ctypes.c_int
    lib.libusb_control_transfer.argtypes = [
        _HandleP, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint16,
        ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint16, ctypes.c_uint
    ]
    lib.libusb_control_transfer.restype = ctypes.c_int
    try:
        lib.libusb_error_name.argtypes = [ctypes.c_int]
        lib.libusb_error_name.restype = ctypes.c_char_p
    except AttributeError:
        pass
    try:
        lib.libusb_strerror.argtypes = [ctypes.c_int]
        lib.libusb_strerror.restype = ctypes.c_char_p
    except AttributeError:
        pass
    return lib


def _error_text(lib, rc):
    for name in ("libusb_error_name", "libusb_strerror"):
        try:
            raw = getattr(lib, name)(rc)
            if raw:
                return raw.decode("ascii", "replace")
        except (AttributeError, OSError, ValueError):
            continue
    return "libusb error " + str(rc)


def _check_rc(lib, rc, label):
    if rc < 0:
        raise BoundedDfuError(label + ": " + _error_text(lib, rc))


def _parse_port(port):
    match = re.fullmatch(r"([0-9]+)-([0-9]+(?:\.[0-9]+)*)", str(port))
    if not match:
        raise BoundedDfuError("USB port must be a bus-port path such as 1-1 or 1-1.2.")
    bus = int(match.group(1))
    ports = tuple(int(item) for item in match.group(2).split("."))
    minimum_bus = 0 if sys.platform == "darwin" else 1
    if bus < minimum_bus or bus > 255 or not ports or any(number < 1 or number > 255 for number in ports):
        raise BoundedDfuError("USB port path is outside the supported USB topology range.")
    return bus, ports


def list_recovery_devices(*, libusb=None):
    """List Jibo USB identities and libusb topology without opening a device."""
    lib = libusb if libusb is not None else _load_libusb()
    context = ctypes.c_void_p()
    _check_rc(lib, lib.libusb_init(ctypes.byref(context)), "Could not initialize libusb")
    device_list = ctypes.POINTER(_DeviceP)()
    try:
        count = lib.libusb_get_device_list(context, ctypes.byref(device_list))
        _check_rc(lib, count, "Could not list USB devices")
        found = []
        for index in range(count):
            device = device_list[index]
            descriptor = _DeviceDescriptor()
            rc = lib.libusb_get_device_descriptor(device, ctypes.byref(descriptor))
            if rc < 0 or descriptor.idVendor != VID or descriptor.idProduct not in (PID, 0x7740):
                continue
            bus = int(lib.libusb_get_bus_number(device))
            port_numbers = (ctypes.c_uint8 * 8)()
            length = int(lib.libusb_get_port_numbers(device, port_numbers, len(port_numbers)))
            _check_rc(lib, length, "Could not read the Jibo USB port path")
            if not 0 < length <= len(port_numbers):
                raise BoundedDfuError("The Jibo USB device has no usable port path.")
            port = str(bus) + "-" + ".".join(str(number) for number in port_numbers[:length])
            _parse_port(port)
            found.append({"port": port, "state": "dfu" if descriptor.idProduct == PID else "rcm"})
        return sorted(found, key=lambda device: _parse_port(device["port"]))
    finally:
        if device_list:
            lib.libusb_free_device_list(device_list, 1)
        lib.libusb_exit(context)


def _verify_sysfs_device(port, sysfs_root):
    _parse_port(port)
    node = Path(sysfs_root) / str(port)
    try:
        vendor = (node / "idVendor").read_text().strip().lower()
        product = (node / "idProduct").read_text().strip().lower()
    except OSError as exc:
        raise BoundedDfuError("Could not verify DFU device at sysfs port " + str(port) + ": " + str(exc)) from exc
    if vendor != format(VID, "04x") or product != format(PID, "04x"):
        raise BoundedDfuError("Sysfs port " + str(port) + " is not Jibo DFU (0955:701a).")


def _find_device(lib, device_list, count, port):
    wanted_bus, wanted_ports = _parse_port(port)
    matches = []
    for index in range(count):
        device = device_list[index]
        descriptor = _DeviceDescriptor()
        rc = lib.libusb_get_device_descriptor(device, ctypes.byref(descriptor))
        if rc < 0 or descriptor.idVendor != VID or descriptor.idProduct != PID:
            continue
        bus = int(lib.libusb_get_bus_number(device))
        port_numbers = (ctypes.c_uint8 * 8)()
        ports_count = int(lib.libusb_get_port_numbers(device, port_numbers, len(port_numbers)))
        if ports_count >= 0 and bus == wanted_bus and tuple(port_numbers[:ports_count]) == wanted_ports:
            matches.append(device)
    if len(matches) != 1:
        if not matches:
            raise BoundedDfuError("No Jibo DFU device was found on USB port " + str(port) + ".")
        raise BoundedDfuError("More than one Jibo DFU device matched USB port " + str(port) + ".")
    return matches[0]


def dfu_alternates(lib, device, handle):
    """Map every DFU alternate-setting name to its (interface, setting) pairs."""
    config = _ConfigDescriptorP()
    rc = lib.libusb_get_active_config_descriptor(device, ctypes.byref(config))
    _check_rc(lib, rc, "Could not read the active USB configuration")
    try:
        found = {}
        for interface_index in range(min(int(config.contents.bNumInterfaces), 32)):
            interface = config.contents.interface[interface_index]
            for alt_index in range(min(int(interface.num_altsetting), 256)):
                descriptor = interface.altsetting[alt_index]
                class_tuple = (descriptor.bInterfaceClass, descriptor.bInterfaceSubClass,
                               descriptor.bInterfaceProtocol)
                if class_tuple != DFU_INTERFACE or not descriptor.iInterface:
                    continue
                string = (ctypes.c_ubyte * 256)()
                size = lib.libusb_get_string_descriptor_ascii(
                    handle, descriptor.iInterface, string, len(string)
                )
                _check_rc(lib, size, "Could not read a DFU alternate-setting name")
                name = bytes(string[:size]).decode("ascii", "replace")
                found.setdefault(name, []).append(
                    (int(descriptor.bInterfaceNumber), int(descriptor.bAlternateSetting)))
        return found
    finally:
        lib.libusb_free_config_descriptor(config)


def _interface_alt(lib, device, handle, wanted_name, alternates=None):
    if alternates is None:
        alternates = dfu_alternates(lib, device, handle)
    found = alternates.get(wanted_name, [])
    if len(found) != 1:
        if not found:
            raise BoundedDfuError(
                "The selected device does not expose the named DFU alternate " + repr(wanted_name) + "."
            )
        raise BoundedDfuError("The named DFU alternate appears more than once on the selected device.")
    return found[0]


def _control(lib, handle, request_type, request, value, interface, buffer, length, label):
    rc = lib.libusb_control_transfer(
        handle, request_type, request, value, interface, buffer, length, _TIMEOUT_MS
    )
    if rc < 0:
        raise BoundedDfuError(label + ": " + _error_text(lib, rc))
    return int(rc)


def _dfu_transfer_size(lib, handle, interface):
    descriptor = (ctypes.c_ubyte * 9)()
    length = _control(
        lib, handle, _USB_IN | _USB_STANDARD | _USB_INTERFACE, _GET_DESCRIPTOR,
        (_DFU_FUNCTIONAL_DESCRIPTOR << 8), interface, descriptor, len(descriptor),
        "Could not read the DFU functional descriptor"
    )
    if length < 9 or descriptor[0] != 9 or descriptor[1] != _DFU_FUNCTIONAL_DESCRIPTOR:
        raise BoundedDfuError("The DFU functional descriptor is missing or truncated.")
    if not (descriptor[2] & _DFU_CAN_UPLOAD):
        raise BoundedDfuError("The selected DFU interface does not advertise upload support.")
    transfer_size = descriptor[5] | (descriptor[6] << 8)
    if transfer_size < 1:
        raise BoundedDfuError("The DFU descriptor reports an invalid transfer size.")
    # libusb's Linux backend refuses control transfers over 4 KiB. Loaders that
    # advertise more (jibo-dfu-queue-v1) honour the smaller wLength.
    return min(transfer_size, MAX_UPLOAD_BYTES, _LIBUSB_CONTROL_MAX)


def _upload_bounded(lib, handle, interface, transfer_size, max_bytes=MAX_UPLOAD_BYTES):
    """Read the whole marker, then ABORT and confirm the idle state."""
    if max_bytes != MAX_UPLOAD_BYTES:
        raise BoundedDfuError("This helper is fixed to a 32 KiB maximum upload.")
    if transfer_size < 1 or transfer_size > MAX_UPLOAD_BYTES:
        raise BoundedDfuError("The DFU transfer size is outside the bounded probe range.")

    result = bytearray()
    primary_error = None
    cleanup_errors = []
    try:
        block = 0
        reached_end = False
        while len(result) < MAX_UPLOAD_BYTES:
            request_length = min(transfer_size, MAX_UPLOAD_BYTES - len(result))
            buffer = (ctypes.c_ubyte * request_length)()
            received = _control(
                lib, handle, _USB_IN | _USB_CLASS | _USB_INTERFACE, _DFU_UPLOAD,
                block, interface, buffer, request_length, "DFU_UPLOAD failed"
            )
            if received > request_length:
                raise BoundedDfuError("The DFU device returned more bytes than the bounded request.")
            result.extend(bytes(buffer[:received]))
            if received < request_length:
                reached_end = True
                break
            block += 1
        if not reached_end or len(result) != MARKER_BYTES:
            raise BoundedDfuError(
                "The GPT marker did not finish at its expected {} bytes (received {}).".format(
                    MARKER_BYTES, len(result)))
    except BaseException as exc:
        primary_error = exc

    try:
        _control(
            lib, handle, _USB_OUT | _USB_CLASS | _USB_INTERFACE, _DFU_ABORT,
            0, interface, None, 0, "DFU_ABORT cleanup failed"
        )
    except Exception as exc:
        cleanup_errors.append(exc)

    try:
        status = (ctypes.c_ubyte * 6)()
        length = _control(
            lib, handle, _USB_IN | _USB_CLASS | _USB_INTERFACE, _DFU_GETSTATUS,
            0, interface, status, len(status), "DFU_GETSTATUS cleanup failed"
        )
        if length != len(status) or status[0] != 0 or status[4] != _DFU_IDLE:
            cleanup_errors.append(BoundedDfuError(
                "DFU cleanup returned {} status byte(s), status {}, state {}; expected status 0 and dfuIDLE (2).".format(
                    length, status[0], status[4])
            ))
    except Exception as exc:
        cleanup_errors.append(exc)

    if primary_error is not None:
        if not isinstance(primary_error, Exception):
            raise primary_error
        if cleanup_errors:
            raise BoundedDfuError(str(primary_error) + "; cleanup: " + "; ".join(map(str, cleanup_errors))) from primary_error
        raise primary_error
    if cleanup_errors:
        raise BoundedDfuError("; ".join(map(str, cleanup_errors))) from cleanup_errors[0]
    return bytes(result)


def read_dfu_alt_prefix(port, alternate=DEFAULT_ALT_NAME, *, sysfs_root="/sys/bus/usb/devices",
                        libusb=None):
    """Read the complete GPT marker into memory without sending DFU_DNLOAD."""
    if alternate != DEFAULT_ALT_NAME:
        raise BoundedDfuError("The bounded GPT helper only permits the read-only Jibo marker.")
    if libusb is None:
        libusb = _load_libusb()
    _parse_port(port)
    if sys.platform.startswith("linux"):
        _verify_sysfs_device(port, sysfs_root)

    context = ctypes.c_void_p()
    rc = libusb.libusb_init(ctypes.byref(context))
    _check_rc(libusb, rc, "Could not initialize libusb")
    device_list = ctypes.POINTER(_DeviceP)()
    handle = _HandleP()
    interface_number = None
    interface_claimed = False
    try:
        count = libusb.libusb_get_device_list(context, ctypes.byref(device_list))
        if count < 0:
            raise BoundedDfuError("Could not list USB devices: " + _error_text(libusb, int(count)))
        device = _find_device(libusb, device_list, count, port)
        rc = libusb.libusb_open(device, ctypes.byref(handle))
        _check_rc(libusb, rc, "Could not open Jibo DFU device on port " + str(port))
        interface_number, alternate_setting = _interface_alt(libusb, device, handle, alternate)
        rc = libusb.libusb_claim_interface(handle, interface_number)
        _check_rc(libusb, rc, "Could not claim the selected DFU interface")
        interface_claimed = True
        rc = libusb.libusb_set_interface_alt_setting(handle, interface_number, alternate_setting)
        _check_rc(libusb, rc, "Could not select DFU alternate " + alternate)
        transfer_size = _dfu_transfer_size(libusb, handle, interface_number)
        return _upload_bounded(libusb, handle, interface_number, transfer_size)
    finally:
        if interface_claimed:
            libusb.libusb_release_interface(handle, interface_number)
        if handle:
            libusb.libusb_close(handle)
        if device_list:
            libusb.libusb_free_device_list(device_list, 1)
        libusb.libusb_exit(context)
