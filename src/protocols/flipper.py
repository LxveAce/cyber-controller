"""Flipper protocol — serial parser for Flipper Zero CLI.

Flipper Zero communicates over a serial CLI with structured, tagged output.
This parser is ported from the universal-flasher-ui DeviceProtocol port, but
adapted to cyber-controller's BaseProtocol contract: it returns ParsedEvent
objects (event_type, data, raw) in the same style as ghost_esp.py instead of
Target objects.

Example serial lines:
    SubGhz: Protocol: Princeton | Bit: 24 | Key: 0x001234 | Freq: 433.92MHz | RSSI: -40.5
    NFC: Type: Mifare Classic 1K | UID: 04:AB:CD:EF | ATQA: 0004 | SAK: 08
    RFID: Type: EM4100 | Data: 01 02 03 04 05
    IR: Protocol: NEC | Address: 0x04 | Command: 0x08
    BT: Name: MyDevice | MAC: AA:BB:CC:DD:EE:FF | RSSI: -55
    Power: Battery: 85% | Charging: No | Voltage: 4.1V
"""

from __future__ import annotations

import re

from src.models.action import ActionCategory, TargetAction
from src.models.target import TargetType
from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# --- Regex patterns for Flipper CLI output (ported verbatim) ---

# SubGHz: Protocol: ... | Key: ... | Freq: ...
_RE_SUBGHZ = re.compile(
    r"SubGhz:\s*Protocol:\s*(\w+)\s*\|.*?Key:\s*(\S+)\s*\|\s*Freq:\s*([\d.]+\s*MHz)"
)

# SubGHz with RSSI
_RE_SUBGHZ_RSSI = re.compile(
    r"SubGhz:.*Freq:\s*([\d.]+\s*MHz)\s*\|\s*RSSI:\s*(-?[\d.]+)"
)

# NFC: Type: ... | UID: ...
_RE_NFC = re.compile(
    r"NFC:\s*Type:\s*(.+?)\s*\|\s*UID:\s*([0-9A-Fa-f:]+)"
)

# NFC with ATQA+SAK
_RE_NFC_FULL = re.compile(
    r"NFC:\s*Type:\s*(.+?)\s*\|\s*UID:\s*([0-9A-Fa-f:]+)\s*\|\s*ATQA:\s*(\w+)\s*\|\s*SAK:\s*(\w+)"
)

# RFID: Type: ... | Data: ...
_RE_RFID = re.compile(
    r"RFID:\s*Type:\s*(\w+)\s*\|\s*Data:\s*(.+)"
)

# IR: Protocol: ... | Address: ... | Command: ...
_RE_IR = re.compile(
    r"IR:\s*Protocol:\s*(\w+)\s*\|\s*Address:\s*(\S+)\s*\|\s*Command:\s*(\S+)"
)

# BT: Name: ... | MAC: ... | RSSI: ...
_RE_BT = re.compile(
    r"BT:\s*Name:\s*(.+?)\s*\|\s*MAC:\s*([0-9A-Fa-f:]{17})\s*\|\s*RSSI:\s*(-?\d+)"
)

# Power: Battery: ... | Charging: ... | Voltage: ...
_RE_POWER = re.compile(
    r"Power:\s*Battery:\s*(\d+%)\s*\|\s*Charging:\s*(\w+)\s*\|\s*Voltage:\s*([\d.]+V)",
    re.IGNORECASE,
)


class FlipperProtocol(BaseProtocol):
    """Parser and command formatter for Flipper Zero CLI."""

    @property
    def protocol_name(self) -> str:
        return "flipper"

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None

        # SubGHz signal
        m = _RE_SUBGHZ.search(line)
        if m:
            rssi: float | None = None
            rssi_m = _RE_SUBGHZ_RSSI.search(line)
            if rssi_m:
                rssi = int(float(rssi_m.group(2)))
            data = {
                "protocol": m.group(1),
                "key": m.group(2),
                "frequency": m.group(3).strip(),
            }
            if rssi is not None:
                data["rssi"] = rssi
            return ParsedEvent(event_type="subghz_found", data=data, raw=line)

        # NFC tag (full with ATQA/SAK) — try before the basic pattern
        m = _RE_NFC_FULL.search(line)
        if m:
            return ParsedEvent(
                event_type="nfc_found",
                data={
                    "nfc_type": m.group(1).strip(),
                    "uid": m.group(2),
                    "atqa": m.group(3),
                    "sak": m.group(4),
                },
                raw=line,
            )

        # NFC tag (basic)
        m = _RE_NFC.search(line)
        if m:
            return ParsedEvent(
                event_type="nfc_found",
                data={"nfc_type": m.group(1).strip(), "uid": m.group(2)},
                raw=line,
            )

        # RFID tag
        m = _RE_RFID.search(line)
        if m:
            return ParsedEvent(
                event_type="nfc_found",
                data={"rfid_type": m.group(1), "data": m.group(2).strip()},
                raw=line,
            )

        # IR signal
        m = _RE_IR.search(line)
        if m:
            return ParsedEvent(
                event_type="ir_found",
                data={
                    "protocol": m.group(1),
                    "address": m.group(2),
                    "command": m.group(3),
                },
                raw=line,
            )

        # Bluetooth device
        m = _RE_BT.search(line)
        if m:
            return ParsedEvent(
                event_type="ble_found",
                data={
                    "name": m.group(1).strip(),
                    "mac": m.group(2),
                    "rssi": int(m.group(3)),
                },
                raw=line,
            )

        # Power / battery status
        m = _RE_POWER.search(line)
        if m:
            return ParsedEvent(
                event_type="status",
                data={
                    "battery": m.group(1),
                    "charging": m.group(2),
                    "voltage": m.group(3),
                },
                raw=line,
            )

        # Unrecognised but non-empty
        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    # ── Commands ─────────────────────────────────────────────────────

    def get_commands(self) -> list[CommandInfo]:
        """Flipper Zero CLI command set (ported from source command catalog)."""
        return [
            # ---- SubGHz ----
            CommandInfo("subghz rx", "SubGHz", "Receive SubGHz signals"),
            CommandInfo("subghz tx", "SubGHz", "Transmit SubGHz signal"),
            CommandInfo("subghz decode_raw", "SubGHz", "Decode raw SubGHz recording"),
            # ---- NFC ----
            CommandInfo("nfc detect", "NFC", "Detect NFC tags"),
            CommandInfo("nfc read", "NFC", "Read NFC tag data"),
            CommandInfo("nfc emulate", "NFC", "Emulate NFC tag"),
            # ---- RFID ----
            CommandInfo("rfid read", "RFID", "Read 125kHz RFID"),
            CommandInfo("rfid emulate", "RFID", "Emulate RFID tag"),
            # ---- IR ----
            CommandInfo("ir rx", "IR", "Receive IR signal"),
            CommandInfo("ir tx", "IR", "Transmit IR signal"),
            # ---- Bluetooth ----
            CommandInfo("bt info", "Bluetooth", "Bluetooth info"),
            # ---- GPIO ----
            CommandInfo("gpio set", "GPIO", "Set GPIO pin state"),
            CommandInfo("gpio read", "GPIO", "Read GPIO pin state"),
            # ---- Storage ----
            CommandInfo("storage list", "Storage", "List storage contents"),
            CommandInfo("storage read", "Storage", "Read file from storage"),
            # ---- Power ----
            CommandInfo("power info", "Power", "Battery and power info"),
            CommandInfo("power reboot", "Power", "Reboot Flipper"),
            CommandInfo("update", "Power", "Start firmware update"),
        ]

    # ── Formatting ───────────────────────────────────────────────────

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        """Format a command for Flipper Zero serial CLI transmission."""
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{cmd} {arg_str}"
        return cmd

    # ── Auto-detection ───────────────────────────────────────────────

    def identify(self, line: str) -> bool:
        """Return True if line looks like Flipper Zero CLI output."""
        markers = (
            "SubGhz:",
            "NFC:",
            "RFID:",
            "Flipper",
            ">: ",
            "Power: Battery:",
        )
        return any(m in line for m in markers)


# --- Target actions: what this protocol can do to each target type ---

TARGET_ACTIONS: dict[TargetType, list[TargetAction]] = {
    TargetType.SUBGHZ: [
        TargetAction("SubGHz Replay", "subghz tx", "Replay SubGHz signal via Flipper", ActionCategory.ATTACK),
    ],
    TargetType.NFC: [
        TargetAction("NFC Emulate", "nfc emulate", "Emulate NFC tag via Flipper", ActionCategory.ATTACK),
    ],
    TargetType.BLE: [
        TargetAction("BT Spam", "bt spam", "Bluetooth spam via Flipper", ActionCategory.ATTACK),
    ],
}
