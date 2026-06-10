"""GhostESP protocol — serial parser for GhostESP firmware."""

from __future__ import annotations

import re

from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# --- Regex patterns for GhostESP serial output ---

_RE_AP = re.compile(
    r"SSID:\s*(.+?)\s*\|\s*BSSID:\s*([\da-fA-F:]{17})\s*\|\s*"
    r"CH:\s*(\d+)\s*\|\s*RSSI:\s*(-?\d+)"
)

_RE_PROBE = re.compile(
    r"Probe\s+from\s+([\da-fA-F:]{17})\s+for\s+['\"](.+?)['\"]",
    re.IGNORECASE,
)

_RE_DEAUTH = re.compile(
    r"Deauth\s+(?:detected|frame)\s+.*?([\da-fA-F:]{17})",
    re.IGNORECASE,
)

_RE_BEACON_SPAM = re.compile(r"Beacon\s+flood", re.IGNORECASE)
_RE_EVIL_PORTAL = re.compile(r"Evil\s+Portal\s+(\w+)", re.IGNORECASE)
_RE_CAPTURE = re.compile(
    r"Captured\s+(\w+)\s*:\s*(.*)",
    re.IGNORECASE,
)
_RE_BLE = re.compile(
    r"BLE\s+Device:\s*([\da-fA-F:]{17})\s+Name:\s*(.+?)\s+RSSI:\s*(-?\d+)"
)
_RE_STATUS = re.compile(r"\[Ghost(?:ESP)?\]\s*(.*)", re.IGNORECASE)
_RE_ERROR = re.compile(r"(?:ERR|Error):\s*(.*)", re.IGNORECASE)
_RE_GPS = re.compile(r"GPS:\s*Lat=([\d.\-]+)\s+Lon=([\d.\-]+)", re.IGNORECASE)
_RE_SD = re.compile(r"SD:\s*(.*)", re.IGNORECASE)


class GhostESPProtocol(BaseProtocol):
    """Parser and command formatter for GhostESP firmware."""

    @property
    def protocol_name(self) -> str:
        return "ghost-esp"

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None

        # AP found
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

        # Probe request
        m = _RE_PROBE.search(line)
        if m:
            return ParsedEvent(
                event_type="probe_request",
                data={"mac": m.group(1), "ssid": m.group(2)},
                raw=line,
            )

        # Deauth detected
        m = _RE_DEAUTH.search(line)
        if m:
            return ParsedEvent(
                event_type="deauth_detected",
                data={"bssid": m.group(1)},
                raw=line,
            )

        # Beacon flood
        if _RE_BEACON_SPAM.search(line):
            return ParsedEvent(event_type="beacon_flood", raw=line)

        # Evil portal
        m = _RE_EVIL_PORTAL.search(line)
        if m:
            return ParsedEvent(
                event_type="evil_portal",
                data={"action": m.group(1).lower()},
                raw=line,
            )

        # Credential capture
        m = _RE_CAPTURE.search(line)
        if m:
            return ParsedEvent(
                event_type="capture",
                data={"type": m.group(1), "value": m.group(2).strip()},
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

        # GPS data
        m = _RE_GPS.search(line)
        if m:
            return ParsedEvent(
                event_type="gps_fix",
                data={"lat": float(m.group(1)), "lon": float(m.group(2))},
                raw=line,
            )

        # SD card
        m = _RE_SD.search(line)
        if m:
            return ParsedEvent(
                event_type="sd_event",
                data={"message": m.group(1).strip()},
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

        # Generic status
        m = _RE_STATUS.search(line)
        if m:
            return ParsedEvent(
                event_type="status",
                data={"message": m.group(1).strip()},
                raw=line,
            )

        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    # ── Commands ─────────────────────────────────────────────────────

    def get_commands(self) -> list[CommandInfo]:
        """GhostESP command set."""
        return [
            # WiFi scanning
            CommandInfo("scanap", "WiFi", "Scan for access points"),
            CommandInfo("scansta", "WiFi", "Scan for stations"),
            CommandInfo("stopscan", "WiFi", "Stop current scan"),
            CommandInfo("list ap", "WiFi", "List scanned APs"),
            CommandInfo("list sta", "WiFi", "List scanned stations"),
            # WiFi attacks
            CommandInfo("deauth", "Attack", "Deauthentication attack"),
            CommandInfo("beacon", "Attack", "Beacon spam attack"),
            CommandInfo("probe", "Attack", "Probe request flood"),
            CommandInfo("rickroll", "Attack", "Rickroll beacon spam"),
            CommandInfo("stopattack", "Attack", "Stop current attack"),
            # Evil portal
            CommandInfo("portal start", "Portal", "Start evil portal"),
            CommandInfo("portal stop", "Portal", "Stop evil portal"),
            CommandInfo("portal sethtml <path>", "Portal", "Set portal HTML", "path"),
            CommandInfo("portal creds", "Portal", "Show captured credentials"),
            # BLE
            CommandInfo("blescan", "BLE", "Scan for BLE devices"),
            CommandInfo("blespam apple", "BLE", "BLE spam (Apple)"),
            CommandInfo("blespam samsung", "BLE", "BLE spam (Samsung)"),
            CommandInfo("blespam google", "BLE", "BLE spam (Google)"),
            CommandInfo("blespam windows", "BLE", "BLE spam (Windows)"),
            CommandInfo("blespam all", "BLE", "BLE spam (all)"),
            CommandInfo("blestop", "BLE", "Stop BLE operations"),
            CommandInfo("bletrack", "BLE", "BLE device tracking"),
            CommandInfo("bleskimmer", "BLE", "BLE skimmer detection"),
            CommandInfo("airtag scan", "BLE", "Scan for AirTags"),
            # Packet capture
            CommandInfo("capture start", "Capture", "Start packet capture"),
            CommandInfo("capture stop", "Capture", "Stop packet capture"),
            CommandInfo("capture save", "Capture", "Save capture to SD"),
            # Wardrive
            CommandInfo("wardrive start", "Wardrive", "Start wardriving"),
            CommandInfo("wardrive stop", "Wardrive", "Stop wardriving"),
            # System
            CommandInfo("info", "System", "Device info"),
            CommandInfo("version", "System", "Firmware version"),
            CommandInfo("reboot", "System", "Reboot device"),
            CommandInfo("gps info", "System", "GPS status"),
            CommandInfo("sd info", "System", "SD card info"),
            CommandInfo("led set <r> <g> <b>", "System", "Set LED colour", "r,g,b"),
            CommandInfo("settings", "System", "Show settings"),
            CommandInfo("help", "System", "Show help"),
            # Channel
            CommandInfo("setch <ch>", "Channel", "Set Wi-Fi channel", "ch"),
            CommandInfo("getch", "Channel", "Get current channel"),
            # Flipper bridge
            CommandInfo("flipper bt", "Flipper", "Flipper BT bridge"),
            CommandInfo("flipper gps", "Flipper", "Flipper GPS bridge"),
        ]

    # ── Formatting ───────────────────────────────────────────────────

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        """Format a command for GhostESP serial transmission."""
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{cmd} {arg_str}"
        return cmd

    # ── Auto-detection ───────────────────────────────────────────────

    def identify(self, line: str) -> bool:
        """Return True if line looks like GhostESP output."""
        markers = ("GhostESP", "[Ghost]", "Ghost ESP", "ghost_esp")
        return any(m in line for m in markers)
