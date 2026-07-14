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

# The target MAC is captured when present, but the whole ".*? MAC" tail is optional so a target-less
# deauth (e.g. a broadcast "Deauth sent") still registers. The MAC group itself must NOT be optional or the
# lazy ".*?" matches empty and group(1) is always None — the bug this replaces.
_RE_DEAUTH = re.compile(
    r"(?:Deauth|DEAUTH)\s+(?:sent|frame|attack)\b(?:.*?([\da-fA-F:]{17}))?",
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

    def __init__(self) -> None:
        super().__init__()
        # Discovery-order scan ordinals. The DIV stream prints no per-entry index, but its own
        # `select ap <n>` / `select sta <n>` address the scan list by position — so, exactly like the
        # Marauder parser, we assign an ordinal by discovery order (deduped by MAC, so a re-seen entry
        # keeps its first position). Without this the AP's Deauth / PMKID / Handshake actions and the
        # client's Deauth action (all pre-gated on `select ... {index}`) are dropped by the resolver.
        self._ap_index = 0
        self._ap_indices: dict[str, int] = {}
        self._sta_index = 0
        self._sta_indices: dict[str, int] = {}

    def reset_scan_index(self) -> None:
        """Reset the AP scan ordinals — call when the device's AP list is cleared
        (`clearlist -a`/reboot) so `select ap {index}` restarts at 0. Wired from the command sink;
        a UI-only Clear that never reaches the device must NOT call this."""
        self._ap_index = 0
        self._ap_indices.clear()

    def reset_station_index(self) -> None:
        """Reset the station scan ordinals — call on `clearlist -s`/reboot. DIV keeps a separate
        station list, so `select sta {index}` restarts at 0 when the STATION list is cleared."""
        self._sta_index = 0
        self._sta_indices.clear()

    def _assign_ap_index(self, bssid: str) -> int:
        existing = self._ap_indices.get(bssid)
        if existing is not None:
            return existing
        idx = self._ap_index
        self._ap_indices[bssid] = idx
        self._ap_index += 1
        return idx

    def _assign_sta_index(self, mac: str) -> int:
        existing = self._sta_indices.get(mac)
        if existing is not None:
            return existing
        idx = self._sta_index
        self._sta_indices[mac] = idx
        self._sta_index += 1
        return idx

    @property
    def protocol_name(self) -> str:
        return "esp32-div"

    capabilities = frozenset({"ble", "nrf24", "wifi"})

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None

        # A BLE line carries a MAC + RSSI and an attacker-chosen Name. If that Name embeds an
        # "AP: SSID=.. BSSID=.." / "STA:.." / "Client:.." substring, the unanchored _RE_AP/_RE_STA
        # .search below would claim it first and mint a phantom AP/STA with the attacker's BSSID,
        # consuming a real select-ordinal and desyncing later `select ap/sta {index}` (the marauder
        # BLE-name -> ap_found twin, marauder.py:179). Skip the AP/STA branches for a BLE line — a
        # genuine AP/STA line has no "Device:"/"DEV:" MAC token, so this never steals a real one.
        ble = _RE_BLE.search(line)

        m = None if ble else (_RE_AP.search(line) or _RE_AP_ALT.search(line))
        if m:
            bssid = m.group(2)
            return ParsedEvent(
                event_type="ap_found",
                data={
                    "ssid": m.group(1).strip(),
                    "bssid": bssid,
                    "channel": int(m.group(3)),
                    "rssi": int(m.group(4)),
                    "encryption": (m.group(5) or "").strip(),
                    "index": self._assign_ap_index(bssid),
                },
                raw=line,
            )

        m = None if ble else (_RE_STA.search(line) or _RE_STA_ALT.search(line))
        if m:
            mac = m.group(1)
            return ParsedEvent(
                event_type="client_found",
                data={
                    "mac": mac,
                    "bssid": m.group(2),
                    "rssi": int(m.group(3)),
                    "index": self._assign_sta_index(mac),
                },
                raw=line,
            )

        m = ble
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
