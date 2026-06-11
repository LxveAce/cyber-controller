"""HaleHound protocol — serial parser for HaleHound multi-protocol firmware.

HaleHound is a multi-protocol offensive security firmware supporting WiFi,
BLE, SubGHz (CC1101), NFC (PN532), NRF24, and MouseJack modules, plus a
Guardian rogue-AP detector. It is the broadest of the recovered parsers.

This parser is ported from the universal-flasher-ui DeviceProtocol port, but
adapted to cyber-controller's BaseProtocol contract: it returns ParsedEvent
objects (event_type, data, raw) in the same style as ghost_esp.py instead of
Target objects.

Example serial lines:
    [WIFI] SSID: NetworkName | BSSID: AA:BB:CC:DD:EE:FF | CH: 6 | RSSI: -42 | ENC: WPA2
    [WIFI_STA] MAC: AA:BB:CC:DD:EE:FF | RSSI: -55 | AP_BSSID: 11:22:33:44:55:66
    [BLE] Name: Device | ADDR: AA:BB:CC:DD:EE:FF | RSSI: -60 | Type: Random
    [SUBGHZ] Freq: 315.00MHz | Mod: ASK | Data: AA BB CC DD | RSSI: -30
    [NFC] UID: 04:AB:CD:EF:12:34:56 | ATQA: 0044 | SAK: 00 | Type: NTAG215
    [NRF24] Channel: 76 | Addr: AA:BB:CC:DD:EE | Payload: 48656C6C6F
    [MOUSEJACK] Device: Logitech | Addr: AA:BB:CC:DD:EE | Type: Mouse
    [IOT] IP: 192.168.1.50 | MAC: AA:BB:CC:DD:EE:FF | Service: HTTP | Port: 80
    [GUARDIAN] ROGUE AP: EvilTwin | BSSID: AA:BB:CC:DD:EE:FF | CH: 6 | RSSI: -30
"""

from __future__ import annotations

import re

from src.models.action import ActionCategory, TargetAction
from src.models.target import TargetType
from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# --- Regex patterns for HaleHound serial output (ported verbatim) ---

# WiFi AP
_RE_WIFI_AP = re.compile(
    r"\[WIFI\]\s*SSID:\s*(.+?)\s*\|\s*BSSID:\s*([0-9A-Fa-f:]{17})"
    r"\s*\|\s*CH:\s*(\d+)\s*\|\s*RSSI:\s*(-?\d+)"
)

# WiFi Station
_RE_WIFI_STA = re.compile(
    r"\[WIFI_STA\]\s*MAC:\s*([0-9A-Fa-f:]{17})\s*\|\s*RSSI:\s*(-?\d+)"
)

# BLE device
_RE_BLE = re.compile(
    r"\[BLE\]\s*Name:\s*(.+?)\s*\|\s*ADDR:\s*([0-9A-Fa-f:]{17})\s*\|\s*RSSI:\s*(-?\d+)"
)

# SubGHz signal (with RSSI)
_RE_SUBGHZ = re.compile(
    r"\[SUBGHZ\]\s*Freq:\s*([\d.]+\s*MHz)\s*\|\s*Mod:\s*(\w+)\s*\|\s*Data:\s*(.+?)\s*\|\s*RSSI:\s*(-?\d+)"
)

# SubGHz without RSSI
_RE_SUBGHZ_NORSSI = re.compile(
    r"\[SUBGHZ\]\s*Freq:\s*([\d.]+\s*MHz)\s*\|\s*Mod:\s*(\w+)\s*\|\s*Data:\s*(.+)"
)

# NFC tag
_RE_NFC = re.compile(
    r"\[NFC\]\s*UID:\s*([0-9A-Fa-f:]+)\s*\|\s*ATQA:\s*(\w+)\s*\|\s*SAK:\s*(\w+)"
)

# NRF24 packet
_RE_NRF24 = re.compile(
    r"\[NRF24\]\s*Channel:\s*(\d+)\s*\|\s*Addr:\s*([0-9A-Fa-f:]+)\s*\|\s*Payload:\s*(\S+)"
)

# MouseJack device
_RE_MOUSEJACK = re.compile(
    r"\[MOUSEJACK\]\s*Device:\s*(.+?)\s*\|\s*Addr:\s*([0-9A-Fa-f:]+)\s*\|\s*Type:\s*(\w+)"
)

# IoT Recon result
_RE_IOT = re.compile(
    r"\[IOT\]\s*IP:\s*([\d.]+)\s*\|\s*MAC:\s*([0-9A-Fa-f:]{17})\s*\|\s*Service:\s*(\w+)"
)

# Guardian rogue AP
_RE_GUARDIAN = re.compile(
    r"\[GUARDIAN\]\s*ROGUE\s*AP:\s*(.+?)\s*\|\s*BSSID:\s*([0-9A-Fa-f:]{17})"
    r"\s*\|\s*CH:\s*(\d+)\s*\|\s*RSSI:\s*(-?\d+)"
)


class HaleHoundProtocol(BaseProtocol):
    """Parser and command formatter for HaleHound firmware."""

    @property
    def protocol_name(self) -> str:
        return "halehound"

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None

        # WiFi AP
        m = _RE_WIFI_AP.search(line)
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

        # WiFi Station
        m = _RE_WIFI_STA.search(line)
        if m:
            return ParsedEvent(
                event_type="client_found",
                data={"mac": m.group(1), "rssi": int(m.group(2))},
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

        # SubGHz with RSSI
        m = _RE_SUBGHZ.search(line)
        if m:
            return ParsedEvent(
                event_type="subghz_found",
                data={
                    "frequency": m.group(1).strip(),
                    "modulation": m.group(2),
                    "data": m.group(3).strip(),
                    "rssi": int(m.group(4)),
                },
                raw=line,
            )

        # SubGHz without RSSI
        m = _RE_SUBGHZ_NORSSI.search(line)
        if m:
            return ParsedEvent(
                event_type="subghz_found",
                data={
                    "frequency": m.group(1).strip(),
                    "modulation": m.group(2),
                    "data": m.group(3).strip(),
                },
                raw=line,
            )

        # NFC tag
        m = _RE_NFC.search(line)
        if m:
            return ParsedEvent(
                event_type="nfc_found",
                data={"uid": m.group(1), "atqa": m.group(2), "sak": m.group(3)},
                raw=line,
            )

        # NRF24 packet
        m = _RE_NRF24.search(line)
        if m:
            return ParsedEvent(
                event_type="nrf24_found",
                data={
                    "channel": int(m.group(1)),
                    "addr": m.group(2),
                    "payload": m.group(3),
                },
                raw=line,
            )

        # MouseJack device
        m = _RE_MOUSEJACK.search(line)
        if m:
            return ParsedEvent(
                event_type="mousejack",
                data={
                    "device": m.group(1).strip(),
                    "addr": m.group(2),
                    "device_type": m.group(3),
                },
                raw=line,
            )

        # IoT device
        m = _RE_IOT.search(line)
        if m:
            return ParsedEvent(
                event_type="iot_found",
                data={
                    "ip": m.group(1),
                    "mac": m.group(2),
                    "service": m.group(3),
                },
                raw=line,
            )

        # Guardian rogue AP
        m = _RE_GUARDIAN.search(line)
        if m:
            return ParsedEvent(
                event_type="rogue_ap",
                data={
                    "ssid": m.group(1).strip(),
                    "bssid": m.group(2),
                    "channel": int(m.group(3)),
                    "rssi": int(m.group(4)),
                },
                raw=line,
            )

        # Unrecognised but non-empty
        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    # ── Commands ─────────────────────────────────────────────────────

    def get_commands(self) -> list[CommandInfo]:
        """HaleHound command set (ported from source command catalog)."""
        return [
            # ---- WiFi ----
            CommandInfo("wifi_scan", "WiFi", "Scan WiFi access points"),
            CommandInfo("wifi_deauth", "WiFi", "WiFi deauth attack"),
            # ---- IoT ----
            CommandInfo(
                "iot_recon",
                "IoT",
                "IoT Recon -- automated LAN scan + credential brute force",
            ),
            # ---- BLE ----
            CommandInfo("ble_scan", "BLE", "BLE device scan"),
            CommandInfo("ble_cinder", "BLE", "BLE Cinder attack"),
            # ---- SubGHz ----
            CommandInfo("subghz_scan", "SubGHz", "SubGHz spectrum scan (CC1101)"),
            CommandInfo("subghz_replay", "SubGHz", "SubGHz replay attack"),
            CommandInfo("subghz_brute", "SubGHz", "SubGHz brute force"),
            CommandInfo(
                "tesla_charge", "SubGHz", "Tesla charge port opener (315/433MHz)"
            ),
            # ---- NFC ----
            CommandInfo("nfc_scan", "NFC", "NFC card scan (PN532)"),
            CommandInfo("nfc_read", "NFC", "NFC card read"),
            CommandInfo("nfc_clone", "NFC", "NFC card clone"),
            # ---- NRF24 ----
            CommandInfo("nrf_scan", "NRF24", "NRF24 2.4GHz scan"),
            # ---- MouseJack ----
            CommandInfo("mousejack", "MouseJack", "MouseJack keystroke injection"),
            # ---- Guardian ----
            CommandInfo("guardian", "Guardian", "WiFi Guardian -- rogue AP detection"),
            CommandInfo("stalkerware", "Guardian", "Stalkerware Detect"),
            # ---- System ----
            CommandInfo("stop", "System", "Stop current operation"),
            CommandInfo("status", "System", "Device status"),
            CommandInfo("reboot", "System", "Reboot device"),
        ]

    # ── Formatting ───────────────────────────────────────────────────

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        """Format a command for HaleHound serial transmission."""
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{cmd} {arg_str}"
        return cmd

    # ── Auto-detection ───────────────────────────────────────────────

    def identify(self, line: str) -> bool:
        """Return True if line looks like HaleHound output."""
        markers = (
            "[GUARDIAN]",
            "[MOUSEJACK]",
            "[NRF24]",
            "[IOT]",
            "[WIFI_STA]",
            "HaleHound",
        )
        return any(m in line for m in markers)


# --- Target actions: what this protocol can do to each target type ---

TARGET_ACTIONS: dict[TargetType, list[TargetAction]] = {
    TargetType.AP: [
        TargetAction("Signal Analysis", "analyze {channel}", "Analyze RF signals on this channel", ActionCategory.MONITOR),
    ],
}
