"""ESP32-DIV protocol — serial parser for cifertech/ESP32-DIV firmware.

WARNING: ESP32-DIV is a penetration testing tool. Use ONLY in authorized
environments with explicit written permission. Unauthorized use of WiFi
deauthentication, packet capture, or wireless attacks is illegal under
the Computer Fraud and Abuse Act (18 U.S.C. § 1030) and equivalent laws
worldwide. This protocol parser enables lawful security research, CTF
competition, and authorized red-team engagements only.
"""

from __future__ import annotations

import re

from src.models.action import ActionCategory, TargetAction
from src.models.target import TargetType
from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# --- Regex patterns for ESP32-DIV serial output ---

_RE_AP = re.compile(
    r"(?:\[WiFi\]\s*)?AP:\s*SSID=(.+?)\s+BSSID=([\da-fA-F:]{17})\s+"
    r"CH=(\d+)\s+RSSI=(-?\d+)(?:\s+ENC=(\S+))?",
)

_RE_AP_ALT = re.compile(
    r"SSID:\s*(.+?)\s*\|\s*BSSID:\s*([\da-fA-F:]{17})\s*\|\s*"
    r"CH:\s*(\d+)\s*\|\s*RSSI:\s*(-?\d+)(?:\s*\|\s*ENC:\s*(\S+))?",
)

_RE_STA = re.compile(
    r"(?:\[WiFi\]\s*)?STA:\s*MAC=([\da-fA-F:]{17})\s+"
    r"BSSID=([\da-fA-F:]{17})\s+RSSI=(-?\d+)",
)

_RE_STA_ALT = re.compile(
    r"Client:\s*([\da-fA-F:]{17})\s+AP:\s*([\da-fA-F:]{17})\s+RSSI:\s*(-?\d+)",
    re.IGNORECASE,
)

_RE_BLE = re.compile(
    r"(?:\[BLE\]\s*)?(?:DEV|Device):\s*(?:MAC=)?([\da-fA-F:]{17})\s+"
    r"(?:Name=)?(.+?)\s+RSSI=(-?\d+)",
)

_RE_PMKID = re.compile(
    r"\[PMKID\]\s*([\da-fA-F:]{17})\s+(.+)",
    re.IGNORECASE,
)

_RE_HANDSHAKE = re.compile(
    r"(?:Handshake|EAPOL)\s+(?:captured|found).*?([\da-fA-F:]{17})",
    re.IGNORECASE,
)

_RE_DEAUTH = re.compile(
    r"(?:Deauth|DEAUTH)\s+(?:sent|frame|attack).*?([\da-fA-F:]{17})?",
    re.IGNORECASE,
)

_RE_BEACON = re.compile(r"Beacon\s+(?:spam|flood|sent)", re.IGNORECASE)

_RE_PACKET = re.compile(
    r"\[PKT\]\s*(.*)",
)

_RE_SPECTRUM = re.compile(
    r"\[2\.4G\]\s*CH=(\d+)\s+RSSI=(-?\d+)",
)

_RE_NRF = re.compile(
    r"\[NRF\]\s*(.*)",
    re.IGNORECASE,
)

_RE_STATUS = re.compile(r"\[DIV\]\s*(.*)")
_RE_WIFI_STATUS = re.compile(r"\[WiFi\]\s*(.*)")
_RE_BLE_STATUS = re.compile(r"\[BLE\]\s*(.*)")
_RE_ERROR = re.compile(r"(?:\[ERR\]|Error:)\s*(.*)", re.IGNORECASE)
_RE_VERSION = re.compile(r"(?:ESP32-DIV|DIV)\s+v?([\d.]+)", re.IGNORECASE)
_RE_SAVE = re.compile(r"(?:Saved|SD:)\s*(.*)", re.IGNORECASE)


class Esp32DivProtocol(BaseProtocol):
    """Parser and command formatter for ESP32-DIV firmware."""

    @property
    def protocol_name(self) -> str:
        return "esp32-div"

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None

        m = _RE_AP.search(line) or _RE_AP_ALT.search(line)
        if m:
            return ParsedEvent(
                event_type="ap_found",
                data={
                    "ssid": m.group(1).strip(),
                    "bssid": m.group(2),
                    "channel": int(m.group(3)),
                    "rssi": int(m.group(4)),
                    "encryption": (m.group(5) or "").strip(),
                },
                raw=line,
            )

        m = _RE_STA.search(line) or _RE_STA_ALT.search(line)
        if m:
            return ParsedEvent(
                event_type="client_found",
                data={
                    "mac": m.group(1),
                    "bssid": m.group(2),
                    "rssi": int(m.group(3)),
                },
                raw=line,
            )

        m = _RE_BLE.search(line)
        if m:
            return ParsedEvent(
                event_type="ble_found",
                data={
                    "mac": m.group(1),
                    "name": m.group(2).strip(),
                    "rssi": int(m.group(3)),
                },
                raw=line,
            )

        m = _RE_PMKID.search(line)
        if m:
            return ParsedEvent(
                event_type="pmkid_captured",
                data={"bssid": m.group(1), "pmkid": m.group(2).strip()},
                raw=line,
            )

        m = _RE_HANDSHAKE.search(line)
        if m:
            return ParsedEvent(
                event_type="handshake_captured",
                data={"bssid": m.group(1) or ""},
                raw=line,
            )

        m = _RE_DEAUTH.search(line)
        if m:
            return ParsedEvent(
                event_type="deauth_sent",
                data={"target": m.group(1) or ""},
                raw=line,
            )

        if _RE_BEACON.search(line):
            return ParsedEvent(event_type="beacon_flood", raw=line)

        m = _RE_PACKET.search(line)
        if m:
            return ParsedEvent(
                event_type="packet",
                data={"info": m.group(1).strip()},
                raw=line,
            )

        m = _RE_SPECTRUM.search(line)
        if m:
            return ParsedEvent(
                event_type="spectrum",
                data={"channel": int(m.group(1)), "rssi": int(m.group(2))},
                raw=line,
            )

        m = _RE_NRF.search(line)
        if m:
            return ParsedEvent(
                event_type="nrf_data",
                data={"message": m.group(1).strip()},
                raw=line,
            )

        m = _RE_VERSION.search(line)
        if m:
            return ParsedEvent(
                event_type="version",
                data={"version": m.group(1)},
                raw=line,
            )

        m = _RE_SAVE.search(line)
        if m:
            return ParsedEvent(
                event_type="save",
                data={"message": m.group(1).strip()},
                raw=line,
            )

        m = _RE_ERROR.search(line)
        if m:
            return ParsedEvent(
                event_type="error",
                data={"message": m.group(1).strip()},
                raw=line,
            )

        m = _RE_STATUS.search(line)
        if m:
            return ParsedEvent(
                event_type="status",
                data={"message": m.group(1).strip()},
                raw=line,
            )

        m = _RE_WIFI_STATUS.search(line) or _RE_BLE_STATUS.search(line)
        if m:
            return ParsedEvent(
                event_type="status",
                data={"message": m.group(1).strip()},
                raw=line,
            )

        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    # ── Commands ─────────────────────────────────────────────────────

    def get_commands(self) -> list[CommandInfo]:
        return [
            # ── WiFi Scanning ────────────────────────────────────────
            CommandInfo("scanwifi", "WiFi", "Scan for access points"),
            CommandInfo("scansta", "WiFi", "Scan for stations / clients"),
            CommandInfo("stopscan", "WiFi", "Stop current scan"),
            CommandInfo("list ap", "WiFi", "List discovered access points"),
            CommandInfo("list sta", "WiFi", "List discovered stations"),
            CommandInfo("setch <ch>", "WiFi", "Set WiFi channel (1-14)", "ch"),
            CommandInfo("getch", "WiFi", "Get current WiFi channel"),
            CommandInfo("hop start", "WiFi", "Start channel hopping"),
            CommandInfo("hop stop", "WiFi", "Stop channel hopping"),

            # ── WiFi Attacks ─────────────────────────────────────────
            CommandInfo("deauth", "Attack", "Deauthentication attack on selected target"),
            CommandInfo("deauth all", "Attack", "Deauth all discovered APs"),
            CommandInfo("beacon", "Attack", "Beacon spam (random SSIDs)"),
            CommandInfo("beacon list", "Attack", "Beacon spam from SSID list"),
            CommandInfo("beacon target", "Attack", "Clone target AP beacons"),
            CommandInfo("probe", "Attack", "Probe request flood"),
            CommandInfo("rickroll", "Attack", "Rickroll beacon spam"),
            CommandInfo("stopattack", "Attack", "Stop current attack"),

            # ── Packet Capture ───────────────────────────────────────
            CommandInfo("sniff", "Capture", "Start packet sniffer"),
            CommandInfo("sniff stop", "Capture", "Stop packet sniffer"),
            CommandInfo("pmkid", "Capture", "Capture PMKID hashes"),
            CommandInfo("pmkid stop", "Capture", "Stop PMKID capture"),
            CommandInfo("handshake", "Capture", "Capture WPA handshakes"),
            CommandInfo("handshake stop", "Capture", "Stop handshake capture"),
            CommandInfo("capture start", "Capture", "Start raw packet capture"),
            CommandInfo("capture stop", "Capture", "Stop raw packet capture"),
            CommandInfo("capture save", "Capture", "Save capture to SD card"),

            # ── BLE ─────────────────────────────────────────────────
            CommandInfo("scanble", "BLE", "Scan for BLE devices"),
            CommandInfo("blestop", "BLE", "Stop BLE scan"),
            CommandInfo("list ble", "BLE", "List discovered BLE devices"),
            CommandInfo("blespam", "BLE", "BLE notification spam (all)"),
            CommandInfo("blespam apple", "BLE", "BLE spam (Apple popups)"),
            CommandInfo("blespam samsung", "BLE", "BLE spam (Samsung)"),
            CommandInfo("blespam google", "BLE", "BLE spam (Google Fast Pair)"),
            CommandInfo("blespam windows", "BLE", "BLE spam (Windows Swift Pair)"),
            CommandInfo("blespam random", "BLE", "BLE spam (random)"),

            # ── 2.4GHz Spectrum ──────────────────────────────────────
            CommandInfo("scan24", "2.4GHz", "2.4GHz spectrum analysis"),
            CommandInfo("scan24 stop", "2.4GHz", "Stop spectrum analysis"),
            CommandInfo("nrf scan", "2.4GHz", "NRF24 device scan"),
            CommandInfo("nrf sniff", "2.4GHz", "NRF24 packet sniffing"),
            CommandInfo("nrf jam", "2.4GHz", "NRF24 channel jamming"),
            CommandInfo("nrf stop", "2.4GHz", "Stop NRF24 operations"),

            # ── Target Selection ─────────────────────────────────────
            CommandInfo("select ap <n>", "Target", "Select AP by index", "n"),
            CommandInfo("select sta <n>", "Target", "Select station by index", "n"),
            CommandInfo("select ble <n>", "Target", "Select BLE device by index", "n"),
            CommandInfo("clear", "Target", "Clear all discovered targets"),

            # ── Storage ─────────────────────────────────────────────
            CommandInfo("save", "Storage", "Save results to SD card"),
            CommandInfo("save pcap", "Storage", "Save packet capture as PCAP"),
            CommandInfo("save hashes", "Storage", "Save captured hashes"),
            CommandInfo("sd info", "Storage", "SD card status"),
            CommandInfo("sd ls", "Storage", "List SD card files"),

            # ── System ──────────────────────────────────────────────
            CommandInfo("info", "System", "Device info"),
            CommandInfo("version", "System", "Firmware version"),
            CommandInfo("status", "System", "Current operation status"),
            CommandInfo("stop", "System", "Stop all operations"),
            CommandInfo("reboot", "System", "Reboot device"),
            CommandInfo("led <r> <g> <b>", "System", "Set LED colour (0-255)", "r,g,b"),
            CommandInfo("led off", "System", "Turn off LED"),
            CommandInfo("settings", "System", "Show settings"),
            CommandInfo("help", "System", "Show help"),
        ]

    # ── Formatting ───────────────────────────────────────────────────

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{cmd} {arg_str}"
        return cmd

    # ── Auto-detection ───────────────────────────────────────────────

    def identify(self, line: str) -> bool:
        markers = ("[DIV]", "ESP32-DIV", "esp32-div", "CiferTech", "cifertech")
        return any(m in line for m in markers)


# ── Warning constant ────────────────────────────────────────────────

AUTH_WARNING = (
    "ESP32-DIV is a penetration testing tool. Use ONLY in authorized "
    "environments with explicit written permission. Unauthorized wireless "
    "attacks are illegal."
)

# ── Target actions: what ESP32-DIV can do to each target type ───────

TARGET_ACTIONS: dict[TargetType, list[TargetAction]] = {
    TargetType.AP: [
        TargetAction(
            "Deauth AP", "deauth",
            "Deauthenticate all clients from this AP",
            ActionCategory.ATTACK,
            requires_selection=True,
            pre_commands=["select ap {index}"],
        ),
        TargetAction(
            "Clone Beacons", "beacon target",
            "Clone and spam this AP's beacon frames",
            ActionCategory.ATTACK,
            requires_selection=True,
            pre_commands=["select ap {index}"],
        ),
        TargetAction(
            "Capture PMKID", "pmkid",
            "Capture PMKID hash from this AP",
            ActionCategory.CAPTURE,
            requires_selection=True,
            pre_commands=["select ap {index}", "setch {channel}"],
            chain_events=["pmkid_captured"],
        ),
        TargetAction(
            "Capture Handshake", "handshake",
            "Capture WPA handshake from this AP",
            ActionCategory.CAPTURE,
            requires_selection=True,
            pre_commands=["select ap {index}", "setch {channel}"],
            chain_events=["handshake_captured"],
        ),
        TargetAction(
            "Sniff Traffic", "sniff",
            "Sniff packets on this AP's channel",
            ActionCategory.CAPTURE,
            pre_commands=["setch {channel}"],
        ),
        TargetAction(
            "Probe Flood", "probe",
            "Flood probe requests near this AP",
            ActionCategory.ATTACK,
        ),
        TargetAction(
            "Monitor Channel", "setch {channel}",
            "Lock to this AP's channel for monitoring",
            ActionCategory.MONITOR,
        ),
    ],
    TargetType.CLIENT: [
        TargetAction(
            "Deauth Client", "deauth",
            "Disconnect this client from its AP",
            ActionCategory.ATTACK,
            requires_selection=True,
            pre_commands=["select sta {index}"],
        ),
        TargetAction(
            "Sniff Client", "sniff",
            "Sniff packets from this client's AP channel",
            ActionCategory.CAPTURE,
            pre_commands=["setch {channel}"],
        ),
    ],
    TargetType.BLE: [
        TargetAction(
            "BLE Spam All", "blespam",
            "Spam BLE notifications to disrupt this device",
            ActionCategory.ATTACK,
        ),
        TargetAction(
            "BLE Spam Apple", "blespam apple",
            "Spam Apple BLE popups",
            ActionCategory.ATTACK,
        ),
        TargetAction(
            "BLE Spam Samsung", "blespam samsung",
            "Spam Samsung BLE notifications",
            ActionCategory.ATTACK,
        ),
        TargetAction(
            "Rescan BLE", "scanble",
            "Rescan to update BLE device info",
            ActionCategory.SCAN,
        ),
    ],
}


# --- Unified Action Broadcast capability map (verb -> (pre_commands, command)).
# Commands are each firmware's NATIVE realization; absent verb == device skipped. ---
from src.core.broadcast import BroadcastVerb  # noqa: E402  (bottom import avoids a cycle)

BROADCAST_CAPABILITIES = {
    BroadcastVerb.FIND_APS:           ((), "scanwifi"),
    BroadcastVerb.SCAN_STATIONS:      ((), "scansta"),
    BroadcastVerb.BLE_SCAN:           ((), "scanble"),
    BroadcastVerb.CAPTURE_HANDSHAKES: ((), "handshake"),
    BroadcastVerb.DEAUTH_ALL:         ((), "deauth all"),
    BroadcastVerb.BEACON_SPAM:        ((), "beacon"),
    BroadcastVerb.BLE_SPAM:           ((), "blespam"),
    BroadcastVerb.STOP_ALL:           ((), "stop"),
}
