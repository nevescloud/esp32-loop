"""Host-side board discovery. Pure observation — no board cooperation needed,
which is the half that disambiguates 'UART-bridge vs native USB' before you ever touch
the chip. esptool chip probing (which DOES need download mode) is opt-in."""

from __future__ import annotations

import glob
import re
import subprocess


# USB-serial bridges + native USB-CDC. Excludes Bluetooth-audio SPP ports
# (cu.JBLCharge4, …) and the Mac's own pseudo-ports — never an ESP32.
USB_HINTS = ("usbserial", "usbmodem", "wchusbserial", "SLAB_USBtoUART", "ttyUSB", "ttyACM")


def list_ports() -> list[str]:
    ports = glob.glob("/dev/cu.*") + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
    return sorted(p for p in ports if any(h in p for h in USB_HINTS))


def classify(port: str) -> str:
    """Transport class from the port name alone (always works, no I/O)."""
    if "usbmodem" in port:
        return "native-usb"  # native USB-CDC / USB-Serial-JTAG (C3/S3)
    if "usbserial" in port or "ttyUSB" in port or "wchusbserial" in port:
        return "uart-bridge"  # external bridge: FTDI / CP210x / CH340
    return "unknown"


def usb_devices() -> dict[str, dict[str, str]]:
    """{/dev/cu.X -> {'serial', 'product'}} from ioreg (macOS, IOService plane).
    Correlates each serial *port* with its USB device's serial + product name by
    walking the tree: a device's "USB Serial Number" / "USB Product Name" print
    just above the nested "IOCalloutDevice". This is the robust board identity —
    it works even when the OS-assigned port name omits the serial (native-USB:
    cu.usbmodem<location> carries no serial, but the chip still has one). Best-
    effort: empty off macOS / if ioreg fails, and callers fall back to port_glob."""
    try:
        out = subprocess.run(
            ["ioreg", "-l", "-w", "0"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    devices: dict[str, dict[str, str]] = {}
    serial = product = None
    for line in out.splitlines():
        if m := re.search(r'"USB Product Name"\s*=\s*"([^"]+)"', line):
            product, serial = m.group(1), None  # new device node — drop the stale serial
        if m := re.search(r'"USB Serial Number"\s*=\s*"([^"]+)"', line):
            serial = m.group(1)
        if m := re.search(r'"IOCalloutDevice"\s*=\s*"(/dev/cu\.[^"]+)"', line):
            devices[m.group(1)] = {"serial": serial, "product": product}
    return devices


def port_serial(port: str, devices: dict[str, dict[str, str]]) -> str | None:
    return (devices.get(port) or {}).get("serial")


def port_product(port: str, devices: dict[str, dict[str, str]]) -> str | None:
    return (devices.get(port) or {}).get("product")


def bridge_name(port: str, devices: dict[str, dict[str, str]]) -> str:
    if product := port_product(port, devices):
        return product
    if "usbmodem" in port:
        return "native USB CDC (USB-Serial-JTAG)"
    return "USB-UART bridge"


def bridge_matches(board: dict, port: str, devices: dict[str, dict[str, str]]) -> bool:
    """The single port↔board narrowing rule, shared by detect's `_match_board` and
    `boards.resolve_port` so the two can never disagree about which board owns a
    port. True when the board declares a `bridge` model and the port's USB product
    name carries it. Matches the real product only — never the "USB-UART bridge"
    fallback — so a port with no ioreg data never reads as a positive identification."""
    hint = board.get("bridge")
    product = port_product(port, devices)
    return bool(hint and product) and hint.lower() in product.lower()
