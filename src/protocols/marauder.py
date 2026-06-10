"""Marauder protocol — serial parser for ESP32 Marauder firmware."""

from __future__ import annotations

import re
from typing import Any

from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# --- Regex patterns for Marauder serial output ---

_RE_AP = re.compile(
    r"(?:AP|SSID):\s*(.+?)\s+"
    r"BSSID:\s*([\da-fA-F:]{17})\s+"
    r"Ch:\s*(\d+)\s+"
    r"RSSI:\s*(-?\d+)"
)

_RE_CLIENT = re.compile(
    r"Client:\s*([\da-fA-F:]{17})\s+"
    r"AP:\s*([\da-fA-F:]{17})"
)

_RE_HANDSHAKE = re.compile(
    r"(?:Handshake|EAPOL)\s+(?:captured|found)\s+.*?([\da-fA-F:]{17})",
    re.IGNORECASE,
)

_RE_SCAN_COMPLETE = re.compile(r"Scan\s+(?:complete|finished)", re.IGNORECASE)
_RE_DEAUTH = re.compile(r"Deauth(?:entication)?\s+(?:sent|frame)", re.IGNORECASE)
_RE_BEACON = re.compile(r"Beacon\s+(?:spam|flood)", re.IGNORECASE)
_RE_PROBE = re.compile(r"Probe\s+(?:request|response)", re.IGNORECASE)
_RE_BLE = re.compile(
    r"BLE:\s*([\da-fA-F:]{17})\s+Name:\s*(.+?)\s+RSSI:\s*(-?\d+)",
)
_RE_KARMA = re.compile(r"Karma\s+(?:AP|attack)", re.IGNORECASE)
_RE_CHANNEL = re.compile(r"(?:Set|Changed)\s+channel\s+(\d+)", re.IGNORECASE)
_RE_STATUS = re.compile(r"^>\s*(.+)", re.MULTILINE)
_RE_ERROR = re.compile(r"(?:Error|FAIL|Failed):\s*(.*)", re.IGNORECASE)
_RE_PCAP = re.compile(r"PCAP\s+(?:saved|written)\s+to\s+(.+)", re.IGNORECASE)


class MarauderProtocol(BaseProtocol):
    """Parser and command formatter for ESP32 Marauder firmware.

    Covers the full Marauder v0.13+ serial command set (70+ commands)
    grouped by category.
    """

    @property
    def protocol_name(self) -> str:
        return "marauder"

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        """Parse a single Marauder serial output line."""
        line = line.strip()
        if not line:
            return None

        # AP discovered
        m = _RE_AP.search(line)
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

        # Client discovered
        m = _RE_CLIENT.search(line)
        if m:
            return ParsedEvent(
                event_type="client_found",
                data={"client_mac": m.group(1), "ap_mac": m.group(2)},
                raw=line,
            )

        # Handshake captured
        m = _RE_HANDSHAKE.search(line)
        if m:
            return ParsedEvent(
                event_type="handshake_captured",
                data={"bssid": m.group(1)},
                raw=line,
            )

        # BLE device
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

        # Scan complete
        if _RE_SCAN_COMPLETE.search(line):
            return ParsedEvent(event_type="scan_complete", raw=line)

        # Deauth sent
        if _RE_DEAUTH.search(line):
            return ParsedEvent(event_type="deauth_sent", raw=line)

        # Beacon spam
        if _RE_BEACON.search(line):
            return ParsedEvent(event_type="beacon_spam", raw=line)

        # Probe
        if _RE_PROBE.search(line):
            return ParsedEvent(event_type="probe_activity", raw=line)

        # Karma
        if _RE_KARMA.search(line):
            return ParsedEvent(event_type="karma_event", raw=line)

        # Channel change
        m = _RE_CHANNEL.search(line)
        if m:
            return ParsedEvent(
                event_type="channel_changed",
                data={"channel": int(m.group(1))},
                raw=line,
            )

        # PCAP saved
        m = _RE_PCAP.search(line)
        if m:
            return ParsedEvent(
                event_type="pcap_saved",
                data={"path": m.group(1).strip()},
                raw=line,
            )

        # Error
        m = _RE_ERROR.search(line)
        if m:
            return ParsedEvent(
                event_type="error",
                data={"message": m.group(1).strip()},
                raw=line,
            )

        # Generic prompt / status
        m = _RE_STATUS.match(line)
        if m:
            return ParsedEvent(
                event_type="status",
                data={"message": m.group(1).strip()},
                raw=line,
            )

        # Unrecognised but non-empty
        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    # ── Commands ─────────────────────────────────────────────────────

    def get_commands(self) -> list[CommandInfo]:
        """Return 70+ Marauder serial commands grouped by category."""
        return [
            # ---- Scanning ----
            CommandInfo("scanap", "Scanning", "Scan for access points"),
            CommandInfo("scansta", "Scanning", "Scan for client stations"),
            CommandInfo("scanap -c <ch>", "Scanning", "Scan APs on specific channel", "ch"),
            CommandInfo("stopscan", "Scanning", "Stop current scan"),
            CommandInfo("list -a", "Scanning", "List discovered APs"),
            CommandInfo("list -s", "Scanning", "List discovered stations"),
            CommandInfo("list -c", "Scanning", "List discovered clients"),
            CommandInfo("clearlist -a", "Scanning", "Clear AP list"),
            CommandInfo("clearlist -s", "Scanning", "Clear station list"),
            # ---- Selection ----
            CommandInfo("select -a <idx>", "Selection", "Select AP by index", "idx"),
            CommandInfo("select -s <idx>", "Selection", "Select station by index", "idx"),
            CommandInfo("select -a all", "Selection", "Select all APs"),
            CommandInfo("select -s all", "Selection", "Select all stations"),
            CommandInfo("deselect -a <idx>", "Selection", "Deselect AP by index", "idx"),
            CommandInfo("deselect -s <idx>", "Selection", "Deselect station by index", "idx"),
            # ---- Attack ----
            CommandInfo("attack -t deauth", "Attack", "Deauthentication attack on selected"),
            CommandInfo("attack -t deauth -c <ch>", "Attack", "Deauth on specific channel", "ch"),
            CommandInfo("attack -t beacon -l", "Attack", "Beacon spam (AP list)"),
            CommandInfo("attack -t beacon -r", "Attack", "Beacon spam (random SSIDs)"),
            CommandInfo("attack -t beacon -a", "Attack", "Beacon spam (rickroll SSIDs)"),
            CommandInfo("attack -t probe", "Attack", "Probe request flood"),
            CommandInfo("attack -t rickroll", "Attack", "Rickroll beacon attack"),
            CommandInfo("stopscan", "Attack", "Stop current attack"),
            # ---- Sniffing ----
            CommandInfo("sniffbeacon", "Sniffing", "Sniff beacon frames"),
            CommandInfo("sniffdeauth", "Sniffing", "Sniff deauth frames"),
            CommandInfo("sniffesp", "Sniffing", "Sniff ESP-NOW frames"),
            CommandInfo("sniffpmkid", "Sniffing", "Sniff PMKID frames"),
            CommandInfo("sniffpwn", "Sniffing", "Sniff-then-deauth for handshakes"),
            CommandInfo("sniffraw", "Sniffing", "Raw 802.11 packet sniffing"),
            CommandInfo("stopscan", "Sniffing", "Stop sniffing"),
            # ---- PCAP / Capture ----
            CommandInfo("ssid -a <name>", "SSID", "Add SSID to list", "name"),
            CommandInfo("ssid -r <idx>", "SSID", "Remove SSID by index", "idx"),
            CommandInfo("ssid -g <count>", "SSID", "Generate random SSIDs", "count"),
            CommandInfo("ssid -l", "SSID", "List SSIDs"),
            CommandInfo("ssid -c", "SSID", "Clear SSID list"),
            # ---- Channel ----
            CommandInfo("channel <ch>", "Channel", "Set Wi-Fi channel", "ch"),
            CommandInfo("channel", "Channel", "Show current channel"),
            # ---- Settings ----
            CommandInfo("settings", "Settings", "Show current settings"),
            CommandInfo("setsetting -e", "Settings", "Enable display"),
            CommandInfo("setsetting -d", "Settings", "Disable display"),
            CommandInfo("reboot", "Settings", "Reboot the device"),
            CommandInfo("update", "Settings", "Check for firmware updates"),
            CommandInfo("gps data", "Settings", "Show GPS data"),
            CommandInfo("gps nmea", "Settings", "Show raw NMEA data"),
            # ---- BLE ----
            CommandInfo("blescan", "BLE", "Scan for BLE devices"),
            CommandInfo("blespam -t apple", "BLE", "BLE spam (Apple notifications)"),
            CommandInfo("blespam -t samsung", "BLE", "BLE spam (Samsung)"),
            CommandInfo("blespam -t google", "BLE", "BLE spam (Google Fast Pair)"),
            CommandInfo("blespam -t microsoft", "BLE", "BLE spam (Microsoft Swift Pair)"),
            CommandInfo("blespam -t all", "BLE", "BLE spam (all vendors)"),
            CommandInfo("bletrack", "BLE", "Track BLE devices"),
            CommandInfo("bleskimmer", "BLE", "BLE skimmer detection"),
            CommandInfo("stopscan", "BLE", "Stop BLE operation"),
            # ---- Karma ----
            CommandInfo("karma", "Karma", "Start Karma AP attack"),
            CommandInfo("karma -s <ssid>", "Karma", "Karma with specific SSID", "ssid"),
            # ---- Packet Monitor ----
            CommandInfo("packetmonitor", "Monitor", "Start packet monitor"),
            CommandInfo("packetmonitor -c <ch>", "Monitor", "Packet monitor on channel", "ch"),
            CommandInfo("eapolmonitor", "Monitor", "Start EAPOL monitor"),
            # ---- Wardrive ----
            CommandInfo("wardrive", "Wardrive", "Start wardriving (GPS required)"),
            CommandInfo("wardrive -s", "Wardrive", "Stop wardriving"),
            # ---- Signal Strength ----
            CommandInfo("sigmon", "Signal", "Signal strength monitor"),
            # ---- Flipper Zero integration ----
            CommandInfo("fzgps", "Flipper", "Flipper Zero GPS bridge"),
            CommandInfo("fzbtscan", "Flipper", "Flipper Zero BT scan bridge"),
            # ---- System / Misc ----
            CommandInfo("status", "System", "Show system status"),
            CommandInfo("info", "System", "Show firmware info"),
            CommandInfo("version", "System", "Show firmware version"),
            CommandInfo("help", "System", "Show help text"),
            CommandInfo("save", "System", "Save settings to flash"),
            CommandInfo("load", "System", "Load settings from flash"),
            CommandInfo("clearap", "System", "Clear AP results"),
            CommandInfo("clearsta", "System", "Clear station results"),
            CommandInfo("led -r <v> -g <v> -b <v>", "System", "Set LED colour", "r,g,b"),
            CommandInfo("draw", "System", "Enter draw mode on display"),
        ]

    # ── Formatting ───────────────────────────────────────────────────

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        """Format a command string for serial transmission."""
        if args:
            parts = [cmd]
            for key, val in args.items():
                parts.append(f"-{key}" if len(key) == 1 else f"--{key}")
                parts.append(str(val))
            return " ".join(parts)
        return cmd

    # ── Auto-detection ───────────────────────────────────────────────

    def identify(self, line: str) -> bool:
        """Return True if line looks like Marauder output."""
        markers = (
            "Marauder",
            "WiFi Scan",
            "scanap",
            "ESP32 Marauder",
            "BSSID:",
            "Deauth sent",
            "sniffpmkid",
        )
        return any(m in line for m in markers)
