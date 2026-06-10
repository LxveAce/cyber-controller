"""Bruce protocol — serial parser for Bruce multi-tool firmware.

Bruce is a multi-tool firmware for ESP32 (CYD and similar boards). It emits
structured, tagged serial output for WiFi / BLE / SubGHz / NFC / IR scans.
This parser is ported from the universal-flasher-ui DeviceProtocol port, but
adapted to cyber-controller's BaseProtocol contract: it returns ParsedEvent
objects (event_type, data, raw) in the same style as ghost_esp.py instead of
Target objects.

Example serial lines:
    [WIFI] AP: CoffeeShop | BSSID: AA:BB:CC:DD:EE:FF | CH: 1 | RSSI: -50 | AUTH: WPA2
    [BLE] Device: FitBand | ADDR: AA:BB:CC:DD:EE:FF | RSSI: -60
    [SUBGHZ] Freq: 433.92MHz | Protocol: Princeton | Data: 0x1234ABCD
    [NFC] Type: NTAG215 | UID: 04:AB:CD:EF:12:34:56
    [IR] Protocol: NEC | Address: 0x04 | Command: 0x08
"""

from __future__ import annotations

import re

from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# --- Regex patterns for Bruce serial output (ported verbatim) ---

# [WIFI] AP: ... | BSSID: ... | CH: ... | RSSI: ...
_RE_WIFI = re.compile(
    r"\[WIFI\]\s*AP:\s*(.+?)\s*\|\s*BSSID:\s*([0-9A-Fa-f:]{17})"
    r"\s*\|\s*CH:\s*(\d+)\s*\|\s*RSSI:\s*(-?\d+)"
)

# [BLE] Device: ... | ADDR: ... | RSSI: ...
_RE_BLE = re.compile(
    r"\[BLE\]\s*Device:\s*(.+?)\s*\|\s*ADDR:\s*([0-9A-Fa-f:]{17})\s*\|\s*RSSI:\s*(-?\d+)"
)

# [SUBGHZ] Freq: ... | Protocol: ... | Data: ...
_RE_SUBGHZ = re.compile(
    r"\[SUBGHZ\]\s*Freq:\s*([\d.]+\s*MHz)\s*\|\s*Protocol:\s*(\w+)\s*\|\s*Data:\s*(\S+)"
)

# [NFC] Type: ... | UID: ...
_RE_NFC = re.compile(
    r"\[NFC\]\s*Type:\s*(.+?)\s*\|\s*UID:\s*([0-9A-Fa-f:]+)"
)

# [IR] Protocol: ... | Address: ... | Command: ...
_RE_IR = re.compile(
    r"\[IR\]\s*Protocol:\s*(\w+)\s*\|\s*Address:\s*(\S+)\s*\|\s*Command:\s*(\S+)"
)


class BruceProtocol(BaseProtocol):
    """Parser and command formatter for Bruce firmware."""

    @property
    def protocol_name(self) -> str:
        return "bruce"

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None

        # WiFi AP
        m = _RE_WIFI.search(line)
        if m:
            return ParsedEvent(
                event_type="ap_found",
                data={
                    "ssid": m.group(1).strip(),
                    "bssid": m.group(2),
                    "channel": int(m.group(3)),
                    "rssi": int(m.group(4)),
                },
                raw=line,
            )

        # BLE device
        m = _RE_BLE.search(line)
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

        # SubGHz signal
        m = _RE_SUBGHZ.search(line)
        if m:
            return ParsedEvent(
                event_type="subghz_found",
                data={
                    "frequency": m.group(1).strip(),
                    "protocol": m.group(2),
                    "data": m.group(3),
                },
                raw=line,
            )

        # NFC tag
        m = _RE_NFC.search(line)
        if m:
            return ParsedEvent(
                event_type="nfc_found",
                data={"nfc_type": m.group(1).strip(), "uid": m.group(2)},
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

        # Unrecognised but non-empty
        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    # ── Commands ─────────────────────────────────────────────────────

    def get_commands(self) -> list[CommandInfo]:
        """Bruce command set (ported from source command catalog)."""
        return [
            # ---- WiFi ----
            CommandInfo("wifi scan", "WiFi", "Scan WiFi networks"),
            CommandInfo("wifi deauth", "WiFi", "Deauth attack"),
            CommandInfo("wifi beacon", "WiFi", "Beacon spam"),
            # ---- BLE ----
            CommandInfo("ble scan", "BLE", "Scan BLE devices"),
            CommandInfo("ble spam", "BLE", "BLE advertisement spam"),
            # ---- SubGHz ----
            CommandInfo("subghz scan", "SubGHz", "Scan SubGHz frequencies"),
            CommandInfo("subghz send", "SubGHz", "Send SubGHz signal"),
            CommandInfo("subghz replay", "SubGHz", "Replay captured SubGHz signal"),
            # ---- NFC ----
            CommandInfo("nfc read", "NFC", "Read NFC tag"),
            CommandInfo("nfc emulate", "NFC", "Emulate NFC tag"),
            # ---- IR ----
            CommandInfo("ir send", "IR", "Send IR signal"),
            CommandInfo("ir receive", "IR", "Receive IR signal"),
            # ---- BadUSB ----
            CommandInfo("badusb run <script>", "BadUSB", "Run a BadUSB/Ducky script", "script"),
            CommandInfo("badusb list", "BadUSB", "List available BadUSB scripts"),
            # ---- System ----
            CommandInfo("stop", "System", "Stop current operation"),
            CommandInfo("reboot", "System", "Reboot device"),
            CommandInfo("status", "System", "Device status"),
        ]

    # ── Formatting ───────────────────────────────────────────────────

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        """Format a command for Bruce serial transmission."""
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{cmd} {arg_str}"
        return cmd

    # ── Auto-detection ───────────────────────────────────────────────

    def identify(self, line: str) -> bool:
        """Return True if line looks like Bruce output."""
        markers = ("[WIFI]", "[SUBGHZ]", "Bruce", "[BLE] Device:", "[NFC] Type:")
        return any(m in line for m in markers)
