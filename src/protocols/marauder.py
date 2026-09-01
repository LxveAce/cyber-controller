"""Marauder protocol — serial parser for ESP32 Marauder firmware."""

from __future__ import annotations

import re
from typing import Any

# Reuse the ONE tolerant AP-line extractor. The real Marauder v1.12.3 `scanall` prints each AP as a single
# line with a BARE leading RSSI + mid-line BSSID + trailing metadata columns; that format was fixed in the
# wardrive parser (commit 81a6896) but never here, so this parser emitted `info` (not `ap_found`) for live
# scans and the shared TargetPool stayed empty (empty Targets / Macro-fill / Cross-Comm / network graph).
# Sharing the extractor keeps the pool feed and the wardrive CSV on ONE parser so they can't drift again.
# (wardrive imports only stdlib — no import cycle.)
from src.core.wardrive import _RSSI_LEAD_RE, _extract_ap_fields
from src.models.action import ActionCategory, TargetAction
from src.models.target import TargetType
from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# --- Regex patterns for Marauder serial output ---

_RE_AP = re.compile(
    # SSID capture bounded ({1,64}?) so a long line missing "BSSID:" can't drive O(n^2) regex
    # backtracking (the lazy dot and the trailing \s+ overlap on whitespace) and pin the serial
    # reader thread on a ~64 KiB un-terminated flush. A real SSID is <=32 octets.
    r"(?:AP|SSID):\s*(.{1,64}?)\s+"
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
        # Key lower-cased: Marauder prints a BSSID in different case across paths (AP-scan lines vs
        # the client line's AP MAC), so an un-normalized key would miss a known AP and drop the
        # client's `select -a` deauth. Matches the codebase `.lower()` MAC convention.
        key = bssid.lower()
        existing = self._ap_indices.get(key)
        if existing is not None:
            return existing
        idx = self._ap_index
        self._ap_indices[key] = idx
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

        # AP discovered — legacy single-line form (kept for back-compat / other tools).
        # _RE_AP.search() scans mid-line, and a BLE device's advertised Name (printed verbatim
        # after "Name:") is attacker-controlled, so a crafted name embedding
        # "SSID: x BSSID: <mac> Ch: <n> RSSI: <n>" would satisfy _RE_AP on a real BLE line and be
        # misrouted to ap_found with an attacker-chosen BSSID (phantom-target injection into the
        # shared TargetPool). Exclude genuine BLE/client lines here, exactly as the scanall branch
        # below does, so those lines fall through to their own branches. A real legacy AP line
        # carries neither "BLE:" nor "Client:", so this never suppresses a true AP.
        m = _RE_AP.search(line)
        if m and not _RE_BLE.search(line) and not _RE_CLIENT.search(line):
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

        # AP discovered — real v1.12.3 `scanall` SINGLE-LINE form: "<rssi> Ch: <n> <bssid> ESSID: <name> <m> <m>"
        # (bare leading RSSI, mid-line BSSID, NO field labels, trailing metadata columns). The legacy _RE_AP and
        # the anchored multi-line branches above cannot match it, so this used to fall through to `info` and never
        # reach the TargetPool. Guarded to require a BSSID plus either an SSID or the unambiguous bare-leading-RSSI
        # signature, so Client/BLE/status lines (which also carry a MAC) never misfire as APs. Isolated multi-line
        # ESSID:/BSSID: lines are handled by the branches above and never reach here.
        if (not _RE_BLE.search(line) and not _RE_CLIENT.search(line)
                and (_RSSI_LEAD_RE.search(line) or "ESSID" in line.upper())):
            fields = _extract_ap_fields(line)
            bssid = fields.get("bssid")
            if bssid and ("ssid" in fields or "rssi" in fields):
                data: dict[str, Any] = {
                    "ssid": str(fields.get("ssid", "")),
                    "bssid": str(bssid),
                    "rssi": int(fields.get("rssi", 0)),  # type: ignore[arg-type]
                    "index": self._assign_index(str(bssid)),
                }
                if "channel" in fields:
                    data["channel"] = int(fields["channel"])  # type: ignore[arg-type]
                return ParsedEvent(event_type="ap_found", data=data, raw=line)

        # Client discovered
        m = _RE_CLIENT.search(line)
        if m:
            ap_mac = m.group(2)
            data = {"client_mac": m.group(1), "ap_mac": ap_mac}
            # "Deauth Client" runs `select -a {index}` — Marauder can only deauth by AP, so the client is
            # acted on through its own AP's scan ordinal. Attach it only when that AP was seen this scan
            # (so the index is real); an unknown AP leaves it unset and the resolver drops the action
            # rather than firing `select -a` on a guessed/wrong AP.
            ap_idx = self._ap_indices.get(ap_mac.lower())  # match _assign_index's lower-cased key
            if ap_idx is not None:
                data["index"] = ap_idx
            return ParsedEvent(
                event_type="client_found",
                data=data,
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
            CommandInfo("packetcount", "Scanning", "Passive per-channel packet-rate monitor"),
            CommandInfo("stopscan", "Scanning", "Stop current scan"),
            CommandInfo("list -a", "Scanning", "List discovered APs"),
            CommandInfo("list -s", "Scanning", "List discovered stations"),
            CommandInfo("list -c", "Scanning", "List discovered clients"),
            # v1.14.0 added `list -b` (Bluetooth). Source-verified 2026-07-30.
            CommandInfo("list -b", "Scanning", "List discovered Bluetooth devices"),
            CommandInfo("clearlist -a", "Scanning", "Clear AP list"),
            CommandInfo("clearlist -s", "Scanning", "Clear station list"),
            # Manually add a target to the list (h:111 ADD_CMD; SAFE list-management).
            CommandInfo("add -a -b <mac>", "Scanning", "Add an AP to the list by BSSID", "mac"),
            CommandInfo("add -c -b <mac> -ap <idx>", "Scanning",
                        "Add a client (by MAC) to an AP index", "mac idx"),
            # ---- Network (LAN recon after joining an AP; CommandLine.h @ v1.12.3) ----
            # join connects marauder to an AP; the scans then enumerate that LAN (recon).
            CommandInfo("join -a <idx> -p <pwd>", "Network", "Join an AP by index + password",
                        "index password"),
            CommandInfo("pingscan", "Network", "Ping-sweep the joined LAN"),
            CommandInfo("arpscan", "Network", "ARP-scan the joined LAN (add -f for full)"),
            CommandInfo("portscan -s <service>", "Network",
                        "Port-scan a service (ssh/telnet/dns/http/smtp/https/rdp) across the LAN",
                        "service"),
            # ---- Selection ----
            CommandInfo("select -a <idx>", "Selection", "Select AP by index", "idx"),
            CommandInfo("select -s <idx>", "Selection", "Select station by index", "idx"),
            CommandInfo("select -a all", "Selection", "Select all APs"),
            CommandInfo("select -s all", "Selection", "Select all stations"),
            # Marauder has no `deselect` verb (checked CommandLine.h); use `clearlist -a/-s` above.
            # The old `deselect -a/-s` entries were phantom (the device ignored them).
            # ---- Attack ----
            CommandInfo("attack -t deauth", "Offensive", "Deauthentication attack on selected"),
            CommandInfo("attack -t deauth -c <ch>", "Offensive", "Deauth on a channel", "ch"),
            CommandInfo("attack -t beacon -l", "Offensive", "Beacon spam (AP list)"),
            CommandInfo("attack -t beacon -r", "Offensive", "Beacon spam (random SSIDs)"),
            CommandInfo("attack -t beacon -a", "Offensive", "Beacon spam (rickroll SSIDs)"),
            CommandInfo("attack -t probe", "Offensive", "Probe request flood"),
            CommandInfo("attack -t rickroll", "Offensive", "Rickroll beacon attack"),
            # More real attack subtypes (ATTACK_TYPE_* h:93-97); the "attack" keyword gates them,
            # explicit danger= makes it belt-and-suspenders.
            CommandInfo("attack -t funny", "Offensive", "Beacon spam (funny SSIDs)", danger="lab-only"),
            CommandInfo("attack -t badmsg", "Offensive", "Malformed-frame (bad-msg) attack",
                        danger="lab-only"),
            CommandInfo("attack -t sae", "Offensive", "WPA3 SAE-commit flood", danger="lab-only"),
            CommandInfo("attack -t csa", "Offensive", "Channel-switch-announcement attack",
                        danger="lab-only"),
            CommandInfo("attack -t quiet", "Offensive", "802.11 Quiet-element attack",
                        danger="lab-only"),
            CommandInfo("attack -t sleep", "Offensive", "Force-sleep (power-save) attack",
                        danger="lab-only"),
            CommandInfo("stopscan", "Offensive", "Stop current attack"),
            # ---- Evil Portal ----
            # Real Marauder verbs (CommandLine.cpp EVIL_PORTAL_CMD: -c start [-w <html>] / sethtml /
            # setap). A rogue captive-portal that harvests creds. stopscan (above) is the cease.
            # reset/ack are firmware no-op stubs; sethtmlstr streams HTML over serial, so neither
            # is a button here.
            CommandInfo("evilportal -c start", "Offensive", "Start the captive-portal attack",
                        danger="lab-only"),
            CommandInfo("evilportal -c setap <idx>", "Offensive", "Set portal target AP by index",
                        "idx", danger="lab-only"),
            CommandInfo("evilportal -c sethtml <file>", "Offensive", "Set the portal HTML from SD",
                        "file", danger="lab-only"),
            # ---- Sniffing ----
            CommandInfo("sniffbeacon", "Sniffing", "Sniff beacon frames"),
            CommandInfo("sniffdeauth", "Sniffing", "Sniff deauth frames"),
            CommandInfo("sniffpmkid", "Sniffing", "Sniff PMKID frames"),
            # RX-only: WIFI_SCAN_PWN is a WIFI_MODE_NULL promiscuous monitor of Pwnagotchi beacons
            # (no arg parsing, no esp_wifi_80211_tx). The old "sniff-then-deauth" label was a
            # firmware-mislabel that wrongly gated it (verified vs WiFiScan.cpp RunPwnScan).
            CommandInfo("sniffpwn", "Sniffing", "Passively sniff Pwnagotchi beacons/handshakes"),
            CommandInfo("sniffraw", "Sniffing", "Raw 802.11 packet sniffing"),
            # Passive frame sniffers, siblings of the above (CommandLine.h @ v1.12.3, no args).
            CommandInfo("sniffprobe", "Sniffing", "Sniff probe-request frames"),
            CommandInfo("sniffsae", "Sniffing", "Sniff WPA3 SAE-commit frames"),
            CommandInfo("sniffpinescan", "Sniffing", "Sniff Pinescan (Pwnagotchi) frames"),
            CommandInfo("sniffmultissid", "Sniffing", "Sniff multi-SSID (Karma-probe) frames"),
            CommandInfo("stopscan", "Sniffing", "Stop sniffing"),
            # ---- SSID list ----
            # v1.12.3: add/generate live under 'ssid -a' (-n name / -g count).
            CommandInfo("ssid -a -n <name>", "SSID", "Add named SSID to list", "name"),
            CommandInfo("ssid -r <idx>", "SSID", "Remove SSID by index", "idx"),
            CommandInfo("ssid -a -g <count>", "SSID", "Generate random SSIDs", "count"),
            # (removed phantom `ssid -l` / `ssid -c`: SSID_CMD parses only -a/-g/-n/-r — both no-op,
            #  verified vs ESP32Marauder CommandLine.cpp:1744-1780 @ v1.12.3)
            # ---- Channel ----
            CommandInfo("channel -s <ch>", "Channel", "Set Wi-Fi channel", "ch"),
            CommandInfo("channel", "Channel", "Show current channel"),
            # ---- Settings ----
            CommandInfo("settings", "Settings", "Show current settings"),
            CommandInfo("settings -s <key> enable", "Settings", "Enable a setting by key", "key"),
            CommandInfo("settings -s <key> disable", "Settings", "Disable a setting by key", "key"),
            CommandInfo("reboot", "Settings", "Reboot the device"),
            CommandInfo("update -s", "Settings", "Update firmware from SD card"),
            CommandInfo("brightness -s <level>", "Settings", "Set brightness 0-9 (-c cycles)",
                        "level"),
            # ---- GPS (all SAFE reads; CommandLine.h @ v1.12.3) ----
            CommandInfo("gpsdata", "GPS", "Show GPS data"),
            CommandInfo("nmea", "GPS", "Show raw NMEA data"),
            # gps queries ONE field (distinct from gpsdata's full dump).
            CommandInfo("gps -g <field>", "GPS",
                        "Query one GPS field (fix/sat/lon/lat/alt/date/accuracy/text/nmea)",
                        "field"),
            CommandInfo("gpspoi -s", "GPS", "GPS POI marker: -s start / -m mark / -e end", "s|m|e"),
            CommandInfo("wardrivepoi <label>", "GPS", "Tag a GPS POI during a wardrive", "label"),
            # gpstracker logs the DEVICE's own path to GPX (distinct from the POI markers above).
            CommandInfo("gpstracker -c <start|stop>", "GPS",
                        "Start/stop a GPX track log of the device's own path", "start|stop"),
            # ---- BLE ----
            CommandInfo("sniffbt", "BLE", "Scan / sniff for BLE devices"),
            CommandInfo("sniffbt -t airtag", "BLE", "Sniff for AirTag / tracker beacons"),
            # Device-type BLE detectors (cpp:989/993/996; passive scans, SAFE like the airtag one).
            CommandInfo("sniffbt -t flipper", "BLE", "Sniff for Flipper Zero devices"),
            CommandInfo("sniffbt -t flock", "BLE", "Sniff for Flock (ALPR) BLE"),
            CommandInfo("sniffbt -t meta", "BLE", "Sniff for Meta / Ray-Ban BLE"),
            CommandInfo("sniffskim", "BLE", "BLE skimmer detection"),
            # Explicit danger= (defense-in-depth): the "spam" keyword gates these today, but the
            # flag keeps them gated if that keyword changes or the verb is renamed (Atlas audit).
            CommandInfo("blespam -t sourapple", "Offensive", "BLE spam (Apple / SourApple)",
                        danger="lab-only"),
            CommandInfo("blespam -t applejuice", "Offensive", "BLE spam (Apple Juice)",
                        danger="lab-only"),
            CommandInfo("blespam -t samsung", "Offensive", "BLE spam (Samsung)", danger="lab-only"),
            CommandInfo("blespam -t google", "Offensive", "BLE spam (Google Fast Pair)",
                        danger="lab-only"),
            CommandInfo("blespam -t windows", "Offensive", "BLE spam (Windows Swift Pair)",
                        danger="lab-only"),
            CommandInfo("blespam -t flipper", "Offensive", "BLE spam (Flipper)", danger="lab-only"),
            CommandInfo("blespam -t all", "Offensive", "BLE spam (all vendors)", danger="lab-only"),
            # spoofat / findmy carry no danger keyword, so the explicit danger= is what gates them.
            CommandInfo("spoofat -t <idx>", "Offensive", "Spoof a selected AirTag (by index)",
                        "idx", danger="lab-only"),
            CommandInfo("findmy -t <idx>", "Offensive", "Trigger FindMy sound on a selected AirTag (by index)",
                        "idx", danger="lab-only"),
            CommandInfo("stopscan", "BLE", "Stop BLE operation"),
            # ---- Karma ----
            # Real form is `karma -p <index>` (HELP_KARMA_CMD:141); bare `karma` and `karma -s` both
            # no-op — dispatch reads only -p (CommandLine.cpp:566-571 @ v1.12.3).
            CommandInfo("karma -p <idx>", "Offensive", "Karma evil-twin AP (by index)", "idx"),
            # ---- MAC spoof (MAC_CMD_A..D h:107-110; active MAC forge -> lab-only) ----
            # randapmac/randstamac carry no danger keyword, so the explicit danger= gates them.
            CommandInfo("randapmac", "Offensive", "Randomize the AP MAC", danger="lab-only"),
            CommandInfo("randstamac", "Offensive", "Randomize the station MAC", danger="lab-only"),
            CommandInfo("cloneapmac -a <idx>", "Offensive", "Clone a scanned AP's MAC by index",
                        "idx", danger="lab-only"),
            CommandInfo("clonestamac -s <idx>", "Offensive", "Clone a scanned station MAC by index",
                        "idx", danger="lab-only"),
            # ---- Wardrive ----
            # (removed phantom `wardrive -s`: fw stop-arg is commented out; stop via `stopscan`)
            CommandInfo("wardrive", "Wardrive", "Start wardriving (GPS required)"),
            # v1.14.0 added `upload` (send wardrive logs to wardriver.app / WiGLE).
            CommandInfo("upload -d <wdg/wigle/both>", "Wardrive",
                        "Upload saved wardrive logs (wardriver.app / WiGLE / both)", "dest"),
            # ---- Signal Strength ----
            # The signal-strength fox-hunt verb is version-split: `sigmon` on <=v1.13.x, RENAMED to
            # `foxhunt` (-b BLE / -w Wi-Fi) in v1.14.0 (SIGSTREN_CMD). BOTH offered so either
            # firmware works (the wrong one just errors; older-fw keeps sigmon). Source-verified
            # 2026-07-30 vs justcallmekoko/ESP32Marauder (HW-unverified).
            CommandInfo("sigmon", "Signal", "Signal-strength fox-hunt (Marauder <=v1.13.x)"),
            CommandInfo("foxhunt -b", "Signal", "Fox-hunt a BLE device by RSSI (v1.14.0+)"),
            CommandInfo("foxhunt -w", "Signal", "Fox-hunt a Wi-Fi target by RSSI (v1.14.0+)"),
            CommandInfo("mactrack", "Signal", "Track a selected MAC by RSSI (proximity/fox-hunt)"),
            # ---- System / Misc ----
            CommandInfo("info", "System", "Show firmware info"),
            CommandInfo("help", "System", "Show help text"),
            CommandInfo("save", "System", "Save settings to flash"),
            CommandInfo("load", "System", "Load settings from flash"),
            # v1.15.x machine-protocol + SPIFFS backup verbs (CommandLine.cpp:516-575 @ v1.15.1,
            # source-verified 2026-09-01, HW-unverified). All SAFE: device-local filesystem + info,
            # no RF. restorespiffs overwrites the device's SPIFFS from the SD backup and reboots on
            # success (the firmware calls ESP.restart()), so it's labeled but not danger-gated.
            CommandInfo("protocolinfo", "System", "Query firmware machine-protocol + backup capabilities"),
            CommandInfo("backupspiffs", "System", "Back up the device SPIFFS (config/data) to SD"),
            CommandInfo("backupstatus", "System", "Report SPIFFS backup status / inspection"),
            CommandInfo("restorespiffs", "System",
                        "Restore device SPIFFS from an SD backup (device reboots on success)"),
            CommandInfo("ls <dir>", "System", "List a directory on the SD card", "dir"),
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
        TargetAction("Karma Clone", "karma -p {index}", "Karma evil-twin on this AP by index", ActionCategory.ATTACK),
        TargetAction("Evil Portal", "evilportal -c start", "Spin up a captive-portal clone of this AP to harvest credentials", ActionCategory.ATTACK, pre_commands=["evilportal -c setap {index}"]),
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
    BroadcastVerb.CAPTURE_HANDSHAKES: ((), "sniffpmkid"),
    BroadcastVerb.DEAUTH_ALL:         (("select -a all",), "attack -t deauth"),
    BroadcastVerb.BEACON_SPAM:        ((), "attack -t beacon -r"),
    BroadcastVerb.BLE_SPAM:           ((), "blespam -t all"),
    BroadcastVerb.STOP_ALL:           ((), "stopscan"),
}
