"""GhostESP protocol — serial parser for GhostESP firmware."""

from __future__ import annotations

import re

from src.models.action import ActionCategory, TargetAction
from src.models.target import TargetType
from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# --- Regex patterns for GhostESP serial output ---

# SSID capture is a NEGATED class ([^|], not .+?) up to the first '|'. The old `\s*(.+?)\s*\|`
# put a lazy dot between two whitespace matchers before a required literal, so `SSID: <60k spaces>x`
# (no '|') drove catastrophic backtracking (~40 s at 4 KB) on the serial READER thread — a spoofed
# device could wedge the read path (ReDoS). [^|]+? can't overlap '|', so matching is linear; the
# SSID (with surrounding spaces) is .strip()'d at the call site.
_RE_AP = re.compile(
    r"SSID:([^|]+?)\|\s*BSSID:\s*([\da-fA-F:]{17})\s*\|\s*"
    r"CH:\s*(\d+)\s*\|\s*RSSI:\s*(-?\d+)"
)

# GhostESP-Revival's `scanap` streams each AP as FOUR consecutive lines rather than the single
# pipe-delimited line _RE_AP matches — e.g.
#     [0] SSID: MyNet,
#     BSSID: B4:BF:E9:11:19:AD,
#     RSSI: -23,
#     Channel: 1,
# (note "Channel:" not "CH:"). We accumulate the fields and emit one ap_found on the closing
# Channel line. The device's own ``[idx]`` is its ``select -a <idx>`` position, carried through.
# Verified on real silicon (COM4, GhostESP flashed via CC) — the old single-line pattern got 0 APs.
_RE_AP_ML_SSID = re.compile(r"^\[(\d+)\]\s*SSID:\s*(.*?),?\s*$")
_RE_AP_ML_BSSID = re.compile(r"^BSSID:\s*([\da-fA-F:]{17}),?\s*$")
_RE_AP_ML_RSSI = re.compile(r"^RSSI:\s*(-?\d+),?\s*$")
_RE_AP_ML_CH = re.compile(r"^Channel:\s*(\d+),?\s*$")

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
# Name capture is LENGTH-CAPPED ({1,255}?, not .+?), leading \s* folded in. The old
# `Name:\s*(.+?)\s+RSSI:` put a lazy dot between \s*/\s+ before the required `RSSI:`, so
# `Name: <60k spaces>x` (no RSSI:) drove catastrophic backtracking on the reader thread (ReDoS,
# twin of _RE_AP). A BLE GAP name is <= 248 bytes, so a 255-char cap bounds the lazy quantifier
# without dropping a real name; the leading space is .strip()'d at the call site.
_RE_BLE = re.compile(
    r"BLE\s+Device:\s*([\da-fA-F:]{17})\s+Name:(.{1,255}?)\s+RSSI:\s*(-?\d+)"
)
_RE_STATUS = re.compile(r"\[Ghost(?:ESP)?\]\s*(.*)", re.IGNORECASE)
_RE_ERROR = re.compile(r"(?:ERR|Error):\s*(.*)", re.IGNORECASE)
# Capture a real float shape (optional sign, digits, optional fractional part) so a device that
# streams a malformed coord like "Lat=1.2.3" / "Lat=." simply doesn't match here (falling through to
# a generic info event) instead of matching and raising ValueError out of the unguarded float() below.
_RE_GPS = re.compile(r"GPS:\s*Lat=(-?\d+(?:\.\d+)?)\s+Lon=(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_RE_SD = re.compile(r"SD:\s*(.*)", re.IGNORECASE)


class GhostESPProtocol(BaseProtocol):
    """Parser and command formatter for GhostESP firmware."""

    def __init__(self) -> None:
        super().__init__()
        # Discovery-order AP ordinal. GhostESP's scan stream prints no index, but `select -a <n>`
        # addresses the AP list by position, so we assign an ordinal by discovery order (deduped by
        # BSSID) — the same approach as the Marauder parser. Without it the per-AP "Deauth AP" action
        # (gated on `select -a {index}`) is dropped by the resolver and never offered.
        self._ap_index = 0
        self._ap_indices: dict[str, int] = {}
        # In-progress multi-line AP record (GhostESP-Revival streams SSID/BSSID/RSSI/Channel as
        # separate lines); filled across parse_line calls, emitted on the closing Channel line.
        self._ap_record: dict = {}

    def reset_scan_index(self) -> None:
        """Reset the AP scan ordinals — call when the device's AP list is cleared
        (`clearlist -a`/reboot) so the next scan restarts `select -a {index}` at 0. Wired from the
        command sink; a UI-only Clear that never reaches the device must NOT call this."""
        self._ap_index = 0
        self._ap_indices.clear()

    def _assign_ap_index(self, bssid: str) -> int:
        existing = self._ap_indices.get(bssid)
        if existing is not None:
            return existing
        idx = self._ap_index
        self._ap_indices[bssid] = idx
        self._ap_index += 1
        return idx

    @property
    def protocol_name(self) -> str:
        return "ghost-esp"

    capabilities = frozenset({"ble", "deauth", "gps", "wifi"})

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None

        # AP found
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
                    "index": self._assign_ap_index(bssid),
                },
                raw=line,
            )

        # Multi-line AP record (GhostESP-Revival). Fields arrive on separate lines; accumulate and
        # emit one ap_found when the closing Channel line lands. Intermediate lines return None
        # (else they fall through to a bogus `info`/`status` event).
        m = _RE_AP_ML_SSID.match(line)
        if m:
            self._ap_record = {"index": int(m.group(1)), "ssid": m.group(2).strip()}
            return None
        m = _RE_AP_ML_BSSID.match(line)
        if m:
            if self._ap_record:
                self._ap_record["bssid"] = m.group(1)
            return None
        m = _RE_AP_ML_RSSI.match(line)
        if m:
            if self._ap_record:
                self._ap_record["rssi"] = int(m.group(1))
            return None
        m = _RE_AP_ML_CH.match(line)
        if m:
            rec, self._ap_record = self._ap_record, {}
            if rec.get("bssid"):
                return ParsedEvent(
                    event_type="ap_found",
                    data={
                        "ssid": rec.get("ssid", ""),
                        "bssid": rec["bssid"],
                        "channel": int(m.group(1)),
                        "rssi": rec.get("rssi", 0),
                        # Conditional, NOT dict.get(k, default): a get() default is evaluated
                        # EAGERLY, so _assign_ap_index (which mutates _ap_indices/_ap_index) would
                        # fire on every multi-line AP even though rec["index"] — the device's own
                        # [idx] — is always present here (the record only exists because a
                        # "[i] SSID:" line created it). Eager firing corrupted the ordinal state
                        # (GHOSTESP-MLINE-INDEX-0713). Only fall back to _assign_ap_index if the
                        # device somehow gave no index.
                        "index": (
                            rec["index"] if "index" in rec
                            else self._assign_ap_index(rec["bssid"])
                        ),
                    },
                    raw=line,
                )
            return None

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
        """GhostESP command set.

        Verbs are the ones documented at docs.ghostesp.net / the Spooks4576 wiki. Offensive-TX verbs
        (deauth/EAPOL-logoff/SAE flood/beacon spam/probe flood/KARMA/BLE spam/AirTag spoof/DHCP starve)
        carry an explicit danger= so safety.classify() is authoritative rather than relying on the
        keyword-scan fallback. Scans/lists/captures/settings are receive-only or config -> safe.
        """
        return [
            # WiFi scanning / association
            CommandInfo("scanap", "WiFi", "Scan for access points"),
            CommandInfo("scansta", "WiFi", "Scan for stations"),
            CommandInfo("scanall", "WiFi", "Combined AP + station scan"),
            CommandInfo("stopscan", "WiFi", "Stop current scan"),
            CommandInfo("list -a", "WiFi", "List scanned APs"),
            CommandInfo("list -s", "WiFi", "List scanned stations"),
            CommandInfo("connect <ssid> [pass]", "WiFi", "Join an infrastructure network (enables on-LAN recon)", "ssid,pass"),
            CommandInfo("disconnect", "WiFi", "Leave the current network"),
            CommandInfo("listenprobes", "WiFi", "Passively monitor probe requests"),
            CommandInfo("listenprobes stop", "WiFi", "Stop the probe-request monitor"),
            CommandInfo("pineap", "WiFi", "Monitor for Wi-Fi Pineapple / rogue-AP beacons"),
            CommandInfo("pineap -s", "WiFi", "Stop the Pineapple monitor"),
            # On-LAN recon (needs a prior connect)
            CommandInfo("scanports", "WiFi", "Port-scan the joined LAN"),
            CommandInfo("scanarp", "WiFi", "ARP-sweep the joined LAN"),
            CommandInfo("scanlocal", "WiFi", "mDNS / host discovery on the joined LAN"),
            CommandInfo("scanssh", "WiFi", "Discover SSH hosts on the joined LAN"),
            # WiFi attacks
            CommandInfo("attack -d", "Attack", "Deauthentication attack (needs a prior select -a)", danger="lab-only"),
            CommandInfo("attack -e", "Attack", "EAPOL logoff (works where 802.11w PMF blocks classic deauth)", danger="lab-only"),
            CommandInfo("attack -s <password>", "Attack", "SAE flood vs WPA3 (needs ESP32-C5/C6 + target PSK)", "password", danger="lab-only"),
            CommandInfo("saeflood <password>", "Attack", "SAE association flood vs WPA3", "password", danger="lab-only"),
            CommandInfo("stopsaeflood", "Attack", "Stop the SAE flood"),
            CommandInfo("beaconspam -r", "Attack", "Beacon spam (random SSIDs)", danger="lab-only"),
            CommandInfo("beaconspam -rr", "Attack", "Rickroll beacon spam", danger="lab-only"),
            CommandInfo("beaconspam -l", "Attack", "Beacon spam cloning all visible SSIDs", danger="lab-only"),
            CommandInfo("beaconspam <name>", "Attack", "Beacon spam a specific SSID", "name", danger="lab-only"),
            CommandInfo("probe", "Attack", "Probe request flood", danger="lab-only"),
            CommandInfo("karma start", "Attack", "KARMA evil-twin: answer probes with the SSIDs clients ask for", danger="lab-only"),
            CommandInfo("karma stop", "Attack", "Stop KARMA"),
            CommandInfo("dhcpstarve start", "Attack", "DHCP-starvation flood (exhaust a LAN's address pool)", danger="lab-only"),
            CommandInfo("dhcpstarve stop", "Attack", "Stop DHCP starvation"),
            CommandInfo("dhcpstarve display", "Attack", "Show DHCP-starvation status"),
            CommandInfo("stop", "Attack", "Stop current attack"),
            # Evil portal
            CommandInfo("startportal", "Portal", "Start evil portal"),
            CommandInfo("stopportal", "Portal", "Stop evil portal"),
            CommandInfo("listportals", "Portal", "List installed portal bundles"),
            CommandInfo("evilportal -c <cmd>", "Portal", "Manage portal HTML (sethtmlstr / clear)", "cmd"),
            CommandInfo("webauth on", "Portal", "Enable web-UI auth"),
            CommandInfo("webauth off", "Portal", "Disable web-UI auth"),
            # BLE
            CommandInfo("blescan", "BLE", "Scan for BLE devices"),
            CommandInfo("blescan -s", "BLE", "Stop BLE operations"),
            CommandInfo("blescan -f", "BLE", "Scan for Flipper Zero devices"),
            CommandInfo("blescan -ds", "BLE", "Detect BLE-spam sources"),
            CommandInfo("blescan -r", "BLE", "Raw BLE traffic scan"),
            CommandInfo("bletrack", "BLE", "BLE device tracking"),
            CommandInfo("bleskimmer", "BLE", "BLE skimmer detection"),
            CommandInfo("blewardriving", "BLE", "BLE wardriving (GPS-tagged beacons)"),
            CommandInfo("blewardriving -s", "BLE", "Stop BLE wardriving"),
            CommandInfo("blespam", "BLE", "BLE advertisement spam (pairing popups)", danger="lab-only"),
            CommandInfo("blespam -s", "BLE", "Stop BLE spam"),
            CommandInfo("airtag scan", "BLE", "Scan for AirTags"),
            CommandInfo("listairtags", "BLE", "List detected AirTags"),
            CommandInfo("selectairtag <idx>", "BLE", "Select an AirTag by index", "idx"),
            CommandInfo("spoofairtag", "BLE", "Spoof an AirTag advertisement", danger="lab-only"),
            CommandInfo("stopspoof", "BLE", "Stop the AirTag spoof"),
            CommandInfo("listflippers", "BLE", "List nearby Flipper Zero devices"),
            CommandInfo("selectflipper <idx>", "BLE", "Select a Flipper by index", "idx"),
            # Packet capture (receive-only)
            CommandInfo("capture -eapol", "Capture", "Capture EAPOL / handshakes"),
            CommandInfo("capture -probe", "Capture", "Capture probe requests"),
            CommandInfo("capture -deauth", "Capture", "Capture deauth frames"),
            CommandInfo("capture -beacon", "Capture", "Capture beacon frames"),
            CommandInfo("capture -raw", "Capture", "Capture raw 802.11 traffic"),
            CommandInfo("capture -wps", "Capture", "Capture WPS traffic"),
            CommandInfo("capture -pwn", "Capture", "Capture Pwnagotchi frames"),
            CommandInfo("capture -stop", "Capture", "Stop packet capture"),
            # Wardrive
            CommandInfo("startwd", "Wardrive", "Start wardriving"),
            CommandInfo("startwd -s", "Wardrive", "Stop wardriving"),
            # Cast
            CommandInfo("dialconnect", "Cast", "DIAL / Chromecast control of LAN smart TVs"),
            # Print
            CommandInfo("powerprinter <ip> <text> <font> <align>", "Print", "Send a job to a LAN printer", "ip,text,font,align"),
            # Comm bridge (ESP-to-ESP over UART)
            CommandInfo("commdiscovery", "Comm", "Discover a peer ESP over the comm bridge"),
            CommandInfo("commconnect", "Comm", "Connect to a discovered peer ESP"),
            CommandInfo("commsend <cmd>", "Comm", "Relay a command to the peer ESP", "cmd"),
            CommandInfo("commstatus", "Comm", "Comm bridge status"),
            CommandInfo("commdisconnect", "Comm", "Disconnect the comm bridge"),
            CommandInfo("commsetpins <rx> <tx>", "Comm", "Set the comm-bridge UART pins", "rx,tx"),
            # System
            CommandInfo("chipinfo", "System", "Device / chip info"),
            CommandInfo("reboot", "System", "Reboot device"),
            CommandInfo("gpsinfo", "System", "GPS status"),
            CommandInfo("sd info", "System", "SD card info"),
            CommandInfo("led set <r> <g> <b>", "System", "Set LED colour", "r,g,b"),
            CommandInfo("settings", "System", "Show settings"),
            CommandInfo("settings list", "System", "List all settings"),
            CommandInfo("settings get <key>", "System", "Read a setting value", "key"),
            CommandInfo("settings set <key> <value>", "System", "Write a setting value", "key,value"),
            CommandInfo("settings reset", "System", "Reset settings to defaults"),
            CommandInfo("mem", "System", "Heap diagnostics"),
            CommandInfo("mem dump", "System", "Dump heap diagnostics"),
            CommandInfo("timezone <TZ>", "System", "Set the device timezone", "TZ"),
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


# --- Target actions: what this protocol can do to each target type ---

TARGET_ACTIONS: dict[TargetType, list[TargetAction]] = {
    TargetType.AP: [
        TargetAction("Deauth AP", "attack -d", "Disconnect all clients from this AP", ActionCategory.ATTACK, requires_selection=True, pre_commands=["select -a {index}"]),
        TargetAction("Beacon Spam", "beaconspam -r", "Broadcast beacon flood near this AP", ActionCategory.ATTACK),
        TargetAction("Evil Portal", "startportal", "Start evil portal captive page", ActionCategory.ATTACK, chain_events=["portal_cred"]),
        TargetAction("Capture Traffic", "capture -eapol", "Start packet capture on this channel", ActionCategory.CAPTURE),
        TargetAction("Probe Flood", "probe", "Flood probe requests", ActionCategory.ATTACK),
    ],
    TargetType.CLIENT: [
        TargetAction("Deauth Client", "attack -d", "Disconnect this client", ActionCategory.ATTACK, requires_selection=True, pre_commands=["select -a {index}"]),
    ],
    TargetType.BLE: [
        TargetAction("AirTag Scan", "airtag scan", "Scan for nearby AirTags", ActionCategory.SCAN),
        TargetAction("BLE Track", "bletrack", "Track this BLE device", ActionCategory.MONITOR),
    ],
}


# --- Unified Action Broadcast capability map (verb -> (pre_commands, command)).
# Commands are each firmware's NATIVE realization; absent verb == device skipped. ---
from src.core.broadcast import BroadcastVerb  # noqa: E402  (bottom import avoids a cycle)

BROADCAST_CAPABILITIES = {
    BroadcastVerb.FIND_APS:           ((), "scanap"),
    BroadcastVerb.SCAN_STATIONS:      ((), "scansta"),
    BroadcastVerb.BLE_SCAN:           ((), "blescan"),
    BroadcastVerb.CAPTURE_HANDSHAKES: ((), "capture -eapol"),
    BroadcastVerb.DEAUTH_ALL:         (("select -a all",), "attack -d"),
    BroadcastVerb.BEACON_SPAM:        ((), "beaconspam -r"),
    # `stop` is GhostESP's universal kill (stops attacks + scans + background tasks); `stopscan` only
    # halts a scan, so STOP ALL must NOT use it or an in-progress deauth/beacon flood keeps transmitting.
    BroadcastVerb.STOP_ALL:           ((), "stop"),
}
