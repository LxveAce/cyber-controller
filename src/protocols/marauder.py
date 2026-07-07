"""Marauder protocol — serial parser for ESP32 Marauder firmware."""

from __future__ import annotations

import re
from typing import Any

from src.models.action import ActionCategory, TargetAction
from src.models.target import TargetType
from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# --- Regex patterns for Marauder serial output ---

_RE_AP = re.compile(
    r"(?:AP|SSID):\s*(.+?)\s+"
    r"BSSID:\s*([\da-fA-F:]{17})\s+"
    r"Ch:\s*(\d+)\s+"
    r"RSSI:\s*(-?\d+)"
)

# v1.12.3 prints each scanned AP across SEPARATE lines, e.g.
#     ESSID: MyNet
#     BSSID: aa:bb:cc:dd:ee:ff
#      RSSI: -52
# (some outputs add a 'Ch:' line). These anchored, single-field patterns feed the
# stateful accumulator in parse_line(). They are anchored so the live-scan one-liner
# ' Ch: 6  RSSI: -50  ESSID: MyNet' (which has NO BSSID) does NOT match any of them.
_RE_AP_ESSID = re.compile(r"^E?SSID:\s*(.*)$")
_RE_AP_BSSID = re.compile(r"^BSSID:\s*([\da-fA-F:]{17})\s*$")
_RE_AP_RSSI = re.compile(r"^RSSI:\s*(-?\d+)\s*$")
_RE_AP_CH = re.compile(r"^Ch(?:annel)?:\s*(\d+)\s*$")

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

    def __init__(self) -> None:
        super().__init__()
        # Accumulator for the multi-line AP record (see parse_line). Holds the
        # fields of the AP currently being read across separate serial lines, or
        # None when no record is in progress.
        self._ap_record: dict[str, Any] | None = None
        # Running AP ordinal so an ap_found carries the index that Marauder's own
        # `list -a` / `select -a <idx>` uses. The scanall stream does NOT print an
        # index (unlike BW16), so we assign one by discovery order, deduped by BSSID
        # (a re-seen AP keeps its first index — matching a stable list position).
        # reset_scan_index() lets the command layer clear this on `clearlist -a`.
        self._ap_index = 0
        self._ap_indices: dict[str, int] = {}

    def reset_scan_index(self) -> None:
        """Reset the AP ordinal (call when the device's AP list is cleared, e.g. `clearlist -a`).

        Whether `scanall` clears or appends to the firmware's list is bench-gated, so the boundary is signalled
        by the command layer rather than guessed from output — guessing wrong would bind `select -a {index}` to
        the wrong AP."""
        self._ap_index = 0
        self._ap_indices.clear()

    def _assign_index(self, bssid: str) -> int:
        """Index for *bssid*: its existing ordinal if seen this session, else the next one. Deduping by BSSID
        keeps a re-observed AP on its original index (its stable position in `list -a`)."""
        existing = self._ap_indices.get(bssid)
        if existing is not None:
            return existing
        idx = self._ap_index
        self._ap_indices[bssid] = idx
        self._ap_index += 1
        return idx

    @property
    def protocol_name(self) -> str:
        return "marauder"

    capabilities = frozenset({"ble", "deauth", "gps", "wifi"})

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        """Parse a single Marauder serial output line.

        AP discovery is STATEFUL: v1.12.3 prints each AP across separate
        ESSID / BSSID / RSSI lines (with an optional Ch line). We accumulate
        those into ``self._ap_record`` and emit a single ``ap_found`` event once
        the record is complete (ESSID seen + BSSID + RSSI). A BSSID is required,
        so the live-scan one-liner ' Ch: 6  RSSI: -50  ESSID: MyNet' (no BSSID)
        never becomes an ``ap_found`` — it falls through to an ``info`` line.
        """
        line = line.strip()
        if not line:
            return None

        # AP discovered — legacy single-line form (kept for back-compat / other tools)
        m = _RE_AP.search(line)
        if m:
            bssid = m.group(2)
            return ParsedEvent(
                event_type="ap_found",
                data={
                    "ssid": m.group(1).strip(),
                    "bssid": bssid,
                    "channel": int(m.group(3)),
                    "rssi": int(m.group(4)),
                    "index": self._assign_index(bssid),
                },
                raw=line,
            )

        # AP discovered — multi-line form (the v1.12.3 default). Accumulate
        # ESSID -> BSSID -> RSSI (+ optional Ch) into one record, emitting
        # ap_found only when BSSID + RSSI have both been seen for this ESSID.
        m = _RE_AP_ESSID.match(line)
        if m:
            # A fresh ESSID line starts a new record (drops any incomplete one).
            self._ap_record = {"ssid": m.group(1).strip()}
            return ParsedEvent(event_type="info", data={"message": line}, raw=line)

        m = _RE_AP_BSSID.match(line)
        if m and self._ap_record is not None:
            self._ap_record["bssid"] = m.group(1)
            done = self._complete_ap(line)
            return done if done is not None else ParsedEvent(
                event_type="info", data={"message": line}, raw=line
            )

        m = _RE_AP_CH.match(line)
        if m and self._ap_record is not None:
            self._ap_record["channel"] = int(m.group(1))
            return ParsedEvent(event_type="info", data={"message": line}, raw=line)

        m = _RE_AP_RSSI.match(line)
        if m and self._ap_record is not None:
            self._ap_record["rssi"] = int(m.group(1))
            done = self._complete_ap(line)
            return done if done is not None else ParsedEvent(
                event_type="info", data={"message": line}, raw=line
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

    def _complete_ap(self, raw: str) -> ParsedEvent | None:
        """Emit an ``ap_found`` event iff the in-progress record is complete.

        Complete = an ESSID record exists and both BSSID and RSSI have been
        captured. (Channel is optional and included when present.) Resets the
        accumulator on emit so the next ESSID starts a fresh record.
        """
        rec = self._ap_record
        if rec is not None and "ssid" in rec and "bssid" in rec and "rssi" in rec:
            data = {
                "ssid": rec["ssid"],
                "bssid": rec["bssid"],
                "rssi": rec["rssi"],
                "index": self._assign_index(rec["bssid"]),
            }
            if "channel" in rec:
                data["channel"] = rec["channel"]
            self._ap_record = None
            return ParsedEvent(event_type="ap_found", data=data, raw=raw)
        return None

    # ── Commands ─────────────────────────────────────────────────────

    def get_commands(self) -> list[CommandInfo]:
        """Return the Marauder v1.12.3 serial command set grouped by category."""
        return [
            # ---- Scanning ----
            # v1.12.3 removed scanap/scansta; the combined scan is 'scanall'.
            CommandInfo("scanall", "Scanning", "Scan for APs and stations (combined)"),
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
            CommandInfo("sniffpmkid", "Sniffing", "Sniff PMKID frames"),
            CommandInfo("sniffpwn", "Sniffing", "Sniff-then-deauth for handshakes"),
            CommandInfo("sniffraw", "Sniffing", "Raw 802.11 packet sniffing"),
            CommandInfo("stopscan", "Sniffing", "Stop sniffing"),
            # ---- SSID list ----
            # v1.12.3: add/generate live under 'ssid -a' (-n name / -g count).
            CommandInfo("ssid -a -n <name>", "SSID", "Add named SSID to list", "name"),
            CommandInfo("ssid -r <idx>", "SSID", "Remove SSID by index", "idx"),
            CommandInfo("ssid -a -g <count>", "SSID", "Generate random SSIDs", "count"),
            CommandInfo("ssid -l", "SSID", "List SSIDs"),
            CommandInfo("ssid -c", "SSID", "Clear SSID list"),
            # ---- Channel ----
            CommandInfo("channel -s <ch>", "Channel", "Set Wi-Fi channel", "ch"),
            CommandInfo("channel", "Channel", "Show current channel"),
            # ---- Settings ----
            CommandInfo("settings", "Settings", "Show current settings"),
            CommandInfo("settings -s <key> enable", "Settings", "Enable a setting by key", "key"),
            CommandInfo("settings -s <key> disable", "Settings", "Disable a setting by key", "key"),
            CommandInfo("reboot", "Settings", "Reboot the device"),
            CommandInfo("update -s", "Settings", "Update firmware from SD card"),
            CommandInfo("gpsdata", "Settings", "Show GPS data"),
            CommandInfo("nmea", "Settings", "Show raw NMEA data"),
            # ---- BLE ----
            CommandInfo("sniffbt", "BLE", "Scan / sniff for BLE devices"),
            CommandInfo("sniffbt -t airtag", "BLE", "Sniff for AirTag / tracker beacons"),
            CommandInfo("sniffskim", "BLE", "BLE skimmer detection"),
            CommandInfo("blespam -t sourapple", "BLE", "BLE spam (Apple / SourApple)"),
            CommandInfo("blespam -t samsung", "BLE", "BLE spam (Samsung)"),
            CommandInfo("blespam -t google", "BLE", "BLE spam (Google Fast Pair)"),
            CommandInfo("blespam -t windows", "BLE", "BLE spam (Windows / Microsoft Swift Pair)"),
            CommandInfo("blespam -t all", "BLE", "BLE spam (all vendors)"),
            CommandInfo("stopscan", "BLE", "Stop BLE operation"),
            # ---- Karma ----
            CommandInfo("karma", "Karma", "Start Karma AP attack"),
            CommandInfo("karma -s <ssid>", "Karma", "Karma with specific SSID", "ssid"),
            # ---- Wardrive ----
            CommandInfo("wardrive", "Wardrive", "Start wardriving (GPS required)"),
            CommandInfo("wardrive -s", "Wardrive", "Stop wardriving"),
            # ---- Signal Strength ----
            CommandInfo("sigmon", "Signal", "Signal strength monitor"),
            # ---- System / Misc ----
            CommandInfo("info", "System", "Show firmware info"),
            CommandInfo("help", "System", "Show help text"),
            CommandInfo("save", "System", "Save settings to flash"),
            CommandInfo("load", "System", "Load settings from flash"),
            CommandInfo("led -s <hexcolor>", "System", "Set LED colour (hex, e.g. FF0000)", "hexcolor"),
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
        """Return True if line looks like Marauder output.

        Markers must be Marauder-SPECIFIC — a shared token misfingerprints a sibling firmware during
        auto-detect (detect_firmware scores each protocol's identify() over the same 'help' reply and the
        first-registered protocol wins ties). So the old broad tokens are gone: 'scanap' is a GhostESP
        command (Marauder v1.12.3 renamed it 'scanall'), and 'BSSID:'/'Deauth sent' also appear verbatim in
        GhostESP / ESP32-DIV output. Rely on tokens only Marauder prints: its banner and 'scanall'/'sniffpmkid'.
        """
        markers = (
            "Marauder",
            "ESP32 Marauder",
            "WiFi Scan",
            "scanall",
            "sniffpmkid",
        )
        return any(m in line for m in markers)


# --- Target actions: what this protocol can do to each target type ---

TARGET_ACTIONS: dict[TargetType, list[TargetAction]] = {
    TargetType.AP: [
        TargetAction("Deauth AP", "attack -t deauth", "Disconnect all clients from this AP", ActionCategory.ATTACK, requires_selection=True, pre_commands=["select -a {index}"], chain_events=["deauth_detected"]),
        TargetAction("Beacon Clone", "attack -t beacon -l", "Broadcast cloned beacons of this AP", ActionCategory.ATTACK, pre_commands=["ssid -a -n {ssid}"]),
        TargetAction("Sniff PMKID", "sniffpmkid", "Capture PMKID handshakes on this channel", ActionCategory.CAPTURE, pre_commands=["channel -s {channel}"]),
        TargetAction("Monitor Channel", "sniffraw", "Raw-sniff all traffic on this AP's channel", ActionCategory.MONITOR, pre_commands=["channel -s {channel}"]),
        TargetAction("Probe Flood", "attack -t probe", "Flood probe requests for this SSID", ActionCategory.ATTACK),
        TargetAction("Rickroll Beacon", "attack -t rickroll", "Broadcast rickroll beacon spam", ActionCategory.ATTACK),
        TargetAction("Karma Clone", "karma -s {ssid}", "Start evil-twin karma attack for this SSID", ActionCategory.ATTACK),
        TargetAction("Wardrive Log", "wardrive", "Start wardrive logging (requires GPS)", ActionCategory.SCAN),
    ],
    TargetType.CLIENT: [
        TargetAction("Deauth Client", "attack -t deauth", "Disconnect this client from its AP", ActionCategory.ATTACK, requires_selection=True, pre_commands=["select -a {index}"]),
        TargetAction("Track Client", "sniffbeacon", "Sniff beacons to track this client's probes", ActionCategory.MONITOR),
    ],
    TargetType.BLE: [
        TargetAction("BLE Track", "sniffbt -t airtag", "Sniff for tracker/AirTag beacons", ActionCategory.MONITOR),
        TargetAction("BLE Skimmer Scan", "sniffskim", "Scan for BLE credit card skimmers", ActionCategory.SCAN),
    ],
}


# --- Unified Action Broadcast capability map (verb -> (pre_commands, command)).
# Commands are each firmware's NATIVE realization; absent verb == device skipped. ---
from src.core.broadcast import BroadcastVerb  # noqa: E402  (bottom import avoids a cycle)

BROADCAST_CAPABILITIES = {
    BroadcastVerb.FIND_APS:           ((), "scanall"),
    BroadcastVerb.SCAN_STATIONS:      ((), "scanall"),
    BroadcastVerb.BLE_SCAN:           ((), "sniffbt"),
    BroadcastVerb.CAPTURE_HANDSHAKES: ((), "sniffpwn"),
    BroadcastVerb.DEAUTH_ALL:         (("select -a all",), "attack -t deauth"),
    BroadcastVerb.BEACON_SPAM:        ((), "attack -t beacon -r"),
    BroadcastVerb.BLE_SPAM:           ((), "blespam -t all"),
    BroadcastVerb.STOP_ALL:           ((), "stopscan"),
}
