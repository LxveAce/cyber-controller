"""
DeviceDetect — automatic device identification for the Universal Flasher.

Enumerates USB serial ports, identifies hardware by VID/PID, probes firmware
version over serial, and generates a cyberdeck manifest (JSON snapshot of
everything connected). Pure Python + pyserial.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    import serial
    from serial.tools import list_ports
    _HAVE_PYSERIAL = True
except Exception:
    _HAVE_PYSERIAL = False


# ── USB VID/PID → device type ──────────────────────────────────────────── #

USB_DEVICE_MAP: Dict[Tuple[int, Optional[int]], str] = {
    (0x1A86, 0x7523): "CH340/CH341 USB-Serial (ESP32 classic / Gold / WROOM)",
    (0x1A86, 0x55D4): "CH9102 USB-Serial (ESP32-S3 devkit)",
    (0x10C4, 0xEA60): "CP2102 USB-Serial (Heltec LoRa V3 / some ESP32)",
    (0x0403, 0x6001): "FTDI FT232R USB-Serial",
    (0x0403, 0x6015): "FTDI FT231X USB-Serial",
    (0x303A, 0x1001): "ESP32-S2/S3/C5 native USB CDC",
    (0x303A, 0x0002): "ESP32-S2 JTAG",
    (0x303A, 0x8000): "ESP32-S3 USB JTAG/serial debug",
    (0x1D6B, None):   "Linux USB gadget (Orbic RNDIS)",
    (0x18D1, 0xD00D): "Android ADB device (Orbic RC400L)",
    (0x0483, 0x5740): "Flipper Zero USB CDC",
    (0x0483, 0xDF11): "STM32 DFU bootloader (Flipper Zero DFU)",
}

# ── firmware signature patterns ─────────────────────────────────────────── #

FIRMWARE_SIGNATURES: Dict[str, str] = {
    "marauder":   r"(?:ESP32\s*)?Marauder\s+v?([\d.]+)",
    "ghostesp":   r"GhostESP\s+[Vv]?([\w.]+)",
    "bruce":      r"Bruce\s+[Vv]?([\d.]+)",
    "halehound":  r"HaleHound.*?[Vv]?([\d.]+)",
    "meshtastic": r"Meshtastic\s+[Vv]?([\d.]+)",
    "esp32-div":  r"ESP32.?DIV\s+[Vv]?([\d.]+)",
    "flipper":    r"Flipper\s+Zero\s+[Vv]?([\d.]+)",
    "evil-portal": r"Evil.?Portal\s+[Vv]?([\d.]+)",
}

# compiled once
_SIG_COMPILED: Dict[str, re.Pattern] = {
    name: re.compile(pat, re.IGNORECASE) for name, pat in FIRMWARE_SIGNATURES.items()
}

# chip identification from boot output
_CHIP_PATTERNS: Dict[str, re.Pattern] = {
    "esp32s3": re.compile(r"ESP32-S3", re.IGNORECASE),
    "esp32s2": re.compile(r"ESP32-S2", re.IGNORECASE),
    "esp32c6": re.compile(r"ESP32-C6", re.IGNORECASE),
    "esp32c5": re.compile(r"ESP32-C5", re.IGNORECASE),
    "esp32c3": re.compile(r"ESP32-C3", re.IGNORECASE),
    "esp32h2": re.compile(r"ESP32-H2", re.IGNORECASE),
    "esp32":   re.compile(r"\bESP32\b"),
    "stm32":   re.compile(r"\bSTM32\b", re.IGNORECASE),
}

# serial probe commands — sent in order, with expected firmware and timeout
_PROBE_COMMANDS: List[Tuple[str, float]] = [
    ("version\n",      1.5),
    ("\n",             0.8),
]

_DEFAULT_BAUD = 115200
_PROBE_BAUD_RATES = (115200, 9600, 921600)


# ── DeviceInfo ──────────────────────────────────────────────────────────── #

@dataclass
class DeviceInfo:
    port: str
    vid: Optional[int] = None
    pid: Optional[int] = None
    description: str = ""
    device_type: str = "unknown"
    firmware: Optional[str] = None
    version: Optional[str] = None
    chip: Optional[str] = None
    serial_number: Optional[str] = None


# ── VID/PID lookup ──────────────────────────────────────────────────────── #

def identify_usb(vid: Optional[int], pid: Optional[int]) -> str:
    if vid is None:
        return "unknown"
    exact = USB_DEVICE_MAP.get((vid, pid))
    if exact:
        return exact
    wildcard = USB_DEVICE_MAP.get((vid, None))
    if wildcard:
        return wildcard
    return f"USB {vid:04X}:{pid:04X}" if pid is not None else f"USB {vid:04X}:????"


# ── firmware detection from serial text ─────────────────────────────────── #

def match_firmware(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Match firmware name and version from arbitrary serial output.
    Returns (firmware_name, version) or (None, None)."""
    for name, pat in _SIG_COMPILED.items():
        m = pat.search(text)
        if m:
            ver = m.group(1) if m.lastindex and m.lastindex >= 1 else None
            return name, ver
    return None, None


def detect_chip_from_text(text: str) -> Optional[str]:
    for chip, pat in _CHIP_PATTERNS.items():
        if pat.search(text):
            return chip
    return None


# ── serial probe ────────────────────────────────────────────────────────── #

def _read_until_idle(ser: serial.Serial, timeout: float) -> str:
    """Read from serial until no new data arrives for `timeout` seconds."""
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waiting = ser.in_waiting
        if waiting:
            buf += ser.read(waiting)
            deadline = time.monotonic() + timeout
        else:
            time.sleep(0.05)
    return buf.decode("utf-8", "replace")


def probe_firmware(port: str, baud: int = _DEFAULT_BAUD,
                   timeout: float = 2.0) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Open `port`, send probe commands, and try to identify firmware.
    Returns (firmware, version, chip) — any field may be None."""
    if not _HAVE_PYSERIAL:
        return None, None, None

    try:
        ser = serial.Serial(port, baud, timeout=0.3)
    except (serial.SerialException, OSError):
        return None, None, None

    collected = ""
    try:
        # drain any pending boot output
        time.sleep(0.3)
        collected += _read_until_idle(ser, 0.5)

        for cmd, wait in _PROBE_COMMANDS:
            ser.write(cmd.encode())
            ser.flush()
            collected += _read_until_idle(ser, wait)

        fw, ver = match_firmware(collected)
        chip = detect_chip_from_text(collected)
        return fw, ver, chip
    except (serial.SerialException, OSError):
        fw, ver = match_firmware(collected)
        chip = detect_chip_from_text(collected)
        return fw, ver, chip
    finally:
        try:
            ser.close()
        except Exception:
            pass


def probe_firmware_multi_baud(port: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Try multiple baud rates to identify firmware. Stops on first match."""
    for baud in _PROBE_BAUD_RATES:
        fw, ver, chip = probe_firmware(port, baud=baud)
        if fw:
            return fw, ver, chip
    return None, None, None


# ── port scanner ────────────────────────────────────────────────────────── #

def enumerate_ports() -> List[DeviceInfo]:
    """List all serial ports with VID/PID identification (no serial probe)."""
    if not _HAVE_PYSERIAL:
        return []

    devices: List[DeviceInfo] = []
    for p in list_ports.comports():
        vid = p.vid
        pid = p.pid
        dev = DeviceInfo(
            port=p.device,
            vid=vid,
            pid=pid,
            description=p.description or "",
            device_type=identify_usb(vid, pid),
            serial_number=p.serial_number,
        )
        devices.append(dev)
    return devices


def scan_ports(probe: bool = True, multi_baud: bool = False) -> List[DeviceInfo]:
    """Enumerate all serial ports and optionally probe each for firmware.
    Set probe=False for fast hardware-only scan, or multi_baud=True to try
    multiple baud rates per port (slower but catches more firmware)."""
    devices = enumerate_ports()
    if not probe:
        return devices

    for dev in devices:
        if multi_baud:
            fw, ver, chip = probe_firmware_multi_baud(dev.port)
        else:
            fw, ver, chip = probe_firmware(dev.port)
        dev.firmware = fw
        dev.version = ver
        if chip:
            dev.chip = chip
    return devices


# ── manifest generation ─────────────────────────────────────────────────── #

def generate_manifest(probe: bool = True, multi_baud: bool = False) -> dict:
    """Scan all ports and return a JSON-serializable cyberdeck manifest."""
    scan_start = time.time()
    devices = scan_ports(probe=probe, multi_baud=multi_baud)
    scan_end = time.time()

    device_list = []
    for dev in devices:
        d = asdict(dev)
        if d["vid"] is not None:
            d["vid_hex"] = f"0x{d['vid']:04X}"
        if d["pid"] is not None:
            d["pid_hex"] = f"0x{d['pid']:04X}"
        device_list.append(d)

    return {
        "devices": device_list,
        "scan_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "scan_duration_s": round(scan_end - scan_start, 2),
        "total_devices": len(device_list),
    }


def save_manifest(path: str, **kwargs) -> str:
    """Generate manifest and write it to `path`. Returns the path."""
    manifest = generate_manifest(**kwargs)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    import os
    os.replace(tmp, path)
    return path
