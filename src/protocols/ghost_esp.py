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
# (note "Channel:" not "CH:"). We accumulate the fields and emit one ap_found — DEFERRED past
# Channel (see below) so the trailing Security/Band/Vendor lines land too. The device's own
# ``[idx]`` is its ``select -a <idx>`` position, carried through. The SSID/BSSID/RSSI/Channel
# layout was verified on real silicon (COM4) — the old single-line pattern got 0 APs.
_RE_AP_ML_SSID = re.compile(r"^\[(\d+)\]\s*SSID:\s*(.*?),?\s*$")
_RE_AP_ML_BSSID = re.compile(r"^BSSID:\s*([\da-fA-F:]{17}),?\s*$")
_RE_AP_ML_RSSI = re.compile(r"^RSSI:\s*(-?\d+),?\s*$")
_RE_AP_ML_CH = re.compile(r"^Channel:\s*(\d+),?\s*$")
# GhostESP-Revival prints per-AP Security / PMF / Band / Vendor lines AFTER the Channel line
# (ap_scan.c glog order: [n] SSID / BSSID / RSSI / Channel, then optional Band / Security / PMF /
# Vendor). So the AP record emits on a DEFERRED basis — flushed on the next `[n] SSID`, the next
# non-AP line, or flush(); NOT on Channel — to also capture the encryption. GROUNDED IN FIRMWARE
# SOURCE, UNVERIFIED AGAINST HARDWARE: Security/PMF/Band are C5/C6-only in the firmware's main path,
# so the AP-encryption field ships honest-labeled until a real GhostESP scan confirms it (final HIL
# validation is owner/hardware-gated).
_RE_AP_ML_SECURITY = re.compile(r"^Security:\s*(\S+)", re.IGNORECASE)
_RE_AP_ML_PMF = re.compile(r"^PMF:\s*(\S+)", re.IGNORECASE)
_RE_AP_ML_BAND = re.compile(r"^Band:\s*(.+?),?\s*$", re.IGNORECASE)
_RE_AP_ML_VENDOR = re.compile(r"^Vendor:\s*(.+?)\s*$")

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

# Station (client) scan — `scansta` / `list -s`. GhostESP-Revival streams each associated station as
# FIVE consecutive lines (twin of the multi-line AP scan), grounded in the firmware's own
# station_scan.c glog + webui/src/parsers.js — e.g.
#     [0] Station MAC: AA:BB:CC:DD:EE:FF,
#          Station Vendor: Apple,
#          Associated AP: MyNet,
#          AP BSSID: 11:22:33:44:55:66,
#          AP Vendor: Netgear
# (the non-indexed live form uses "Station:" / "STA Vendor:"). Only "Station MAC:" + a 17-char MAC
# starts a record; accumulate + emit client_found on the closing "AP Vendor" line. Captures are
# bounded ([^,\n], a MAC-length class) so a wedged device can't drive catastrophic backtracking.
_RE_STA_START = re.compile(r"Station(?:\s*MAC)?:\s*([\da-fA-F:]{17})", re.IGNORECASE)
_RE_STA_INDEX = re.compile(r"^\[(\d+)\]\s*Station\s*MAC:", re.IGNORECASE)
_RE_STA_VENDOR = re.compile(r"(?:Station|STA)\s*Vendor:\s*([^,\n]*)", re.IGNORECASE)
_RE_STA_AP_SSID = re.compile(r"Associated\s*AP:\s*([^,\n]*)", re.IGNORECASE)
_RE_STA_AP_BSSID = re.compile(r"AP\s*BSSID:\s*([\da-fA-F:]{17})", re.IGNORECASE)
_RE_STA_AP_VENDOR = re.compile(r"AP\s*Vendor:\s*([^,\n]*)", re.IGNORECASE)

# Handshake capture — GhostESP's `main/core/callbacks.c` prints a three-line record on a successful
# EAPOL capture: GhostESP prints "Handshake found!\nAP=<bssid>\nPair=<pairwise>/<group>", matched by
# the firmware's own webui parser (/Handshake found/i + /AP=([0-9A-Fa-f:]{17})/i + /Pair=(\S+)/i).
# Without a handler all three lines became bogus `info`. Emit handshake_captured (Marauder parity).
_RE_HS_TRIGGER = re.compile(r"Handshake\s+found", re.IGNORECASE)
_RE_HS_AP = re.compile(r"AP=\s*([\da-fA-F:]{17})", re.IGNORECASE)
_RE_HS_PAIR = re.compile(r"Pair=\s*(\S+)", re.IGNORECASE)

# BLE tracker scans — Flipper Zero (`blescan -f`, flipper_scan.c) + Apple AirTag (`aerialscan`,
# airtag_scan.c). Both stream a short multi-line record closing on the "RSSI: N dBm" line;
# grounded in the firmware glog + webui/parsers.js FLIPPER_*/AIRTAG_* patterns. e.g.
#     [0] White Flipper Found:        [0] AirTag Found (Total: 3)
#          MAC: AA:BB:CC:DD:EE:F0,          MAC: AA:BB:CC:DD:EE:F1,
#          Name: Flipper Zynq,              RSSI: -55 dBm (Near),
#          RSSI: -60 dBm
# Both surface as ble_found (a Flipper/AirTag IS a BLE device) with a `kind` discriminator; a Flipper
# carries its advertised name, an AirTag its total-seen count. RX/awareness-only.
_RE_FLIPPER_START = re.compile(
    r"^\[(\d+)\]\s*(White|Black|Transparent)?\s*Flipper\s+Found", re.IGNORECASE)
_RE_AIRTAG_START = re.compile(
    r"^\[(\d+)\]\s*AirTag\s+Found(?:\s*\(Total:\s*(\d+)\))?", re.IGNORECASE)
_RE_TRK_MAC = re.compile(r"^MAC:\s*([\da-fA-F:]{17})", re.IGNORECASE)
_RE_TRK_NAME = re.compile(r"^Name:\s*([^,\n]*)", re.IGNORECASE)
_RE_TRK_RSSI = re.compile(r"^RSSI:\s*(-?\d+)\s*dBm", re.IGNORECASE)


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
        # In-progress multi-line AP record (GhostESP-Revival streams SSID/BSSID/RSSI/Channel + optional
        # Security/Band/PMF/Vendor as separate lines); filled across calls, DEFERRED-emitted on the next
        # `[n] SSID` / the next non-AP line / flush(). Complete = has bssid + channel.
        self._ap_record: dict = {}
        # In-progress multi-line STATION record (scansta/list -s streams each client across five
        # lines); filled across calls, emitted on the closing "AP Vendor" line. See _RE_STA_* below.
        self._sta_record: dict | None = None
        # Handshake-capture stage machine (None -> "ap" -> "pair"). GhostESP prints the capture as three
        # lines ("Handshake found!" / "AP=<bssid>" / "Pair=<x>/<y>"); we emit on the AP line and swallow
        # the Pair line. See _RE_HS_* below.
        self._handshake_stage: str | None = None
        # In-progress BLE tracker-scan records (blescan -f / aerialscan). Flipper Zero prints a 4-line
        # record, AirTag a 3-line one; both close on the "RSSI: N dBm" line -> a ble_found. See _RE_FLIPPER_*
        # / _RE_AIRTAG_* / _RE_TRK_* below.
        self._flipper_record: dict | None = None
        self._airtag_record: dict | None = None

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

    def _fill_ap_record(self, line: str) -> bool:
        """Add a BSSID/RSSI/Channel/Security/PMF/Band/Vendor continuation line to the open AP record.
        Returns True if *line* was a continuation (and was captured), False otherwise."""
        rec = self._ap_record
        m = _RE_AP_ML_BSSID.match(line)
        if m:
            rec["bssid"] = m.group(1)
            return True
        m = _RE_AP_ML_RSSI.match(line)
        if m:
            rec["rssi"] = int(m.group(1))
            return True
        m = _RE_AP_ML_CH.match(line)
        if m:
            rec["channel"] = int(m.group(1))   # completes the required fields; does NOT emit here
            rec["_raw"] = line
            return True
        m = _RE_AP_ML_SECURITY.match(line)
        if m:
            rec["encryption"] = m.group(1).strip().rstrip(",")  # trailing AP-encryption field (HW-unverified)
            return True
        m = _RE_AP_ML_PMF.match(line)
        if m:
            rec["pmf"] = m.group(1).strip().rstrip(",")
            return True
        m = _RE_AP_ML_BAND.match(line)
        if m:
            rec["band"] = m.group(1).strip().rstrip(",").strip()
            return True
        m = _RE_AP_ML_VENDOR.match(line)
        if m:
            rec["vendor"] = m.group(1).strip()
            return True
        return False

    def _flush_ap(self) -> "ParsedEvent | None":
        """Emit the accumulated AP record (or None if incomplete), clearing it. Same emit CONTENT as the
        old emit-on-Channel path — ssid/bssid/rssi/channel/index are byte-identical — plus the optional
        trailing encryption/band/pmf/vendor when the firmware reported them."""
        rec, self._ap_record = self._ap_record, {}
        if not rec.get("bssid") or "channel" not in rec:
            return None   # never had a BSSID (or never reached Channel) — drop, as the old path did
        data: dict = {
            "ssid": rec.get("ssid", ""),
            "bssid": rec["bssid"],
            "channel": rec["channel"],
            "rssi": rec.get("rssi", 0),
            # rec["index"] is the device's own [idx] (always present); only fall back to the mutating
            # _assign_ap_index if the device somehow gave none (GHOSTESP-MLINE-INDEX-0713).
            "index": rec["index"] if "index" in rec else self._assign_ap_index(rec["bssid"]),
        }
        for key in ("encryption", "pmf", "band", "vendor"):
            if key in rec:
                data[key] = rec[key]
        return ParsedEvent(event_type="ap_found", data=data, raw=rec.get("_raw", ""))

    def flush(self) -> "ParsedEvent | None":
        """Emit any AP record still being accumulated (end-of-stream / idle). The deferred AP path emits
        on the next `[n] SSID` or the next non-AP line; this flushes the LAST AP when nothing follows."""
        return self._flush_ap()

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

        # Multi-line AP record (GhostESP-Revival) — DEFERRED emit (see _RE_AP_ML_SECURITY above). GhostESP
        # prints Security/PMF/Band/Vendor AFTER the Channel line, so the record is flushed on the next
        # `[n] SSID`, the next non-AP line, or flush() — NOT on Channel. The ap_found CONTENT for the
        # existing fields is unchanged; only WHEN it emits defers, to also capture the trailing fields.
        m = _RE_AP_ML_SSID.match(line)
        if m:
            flushed = self._flush_ap()     # emit the previous AP (if complete) before starting this one
            self._ap_record = {"index": int(m.group(1)), "ssid": m.group(2).strip(), "_raw": line}
            return flushed
        if self._ap_record and self._fill_ap_record(line):
            return None
        # Not an AP-continuation line: flush any pending complete AP; the flushed AP supersedes this line's
        # terminal event (the line's own record-start side-effects in _parse_after_ap still run). In
        # practice the first line after the last AP is a low-value scan terminator/prompt, so nothing
        # useful is lost.
        flushed = self._flush_ap() if self._ap_record else None
        body = self._parse_after_ap(line)
        return flushed if flushed is not None else body

    def _parse_after_ap(self, line: str) -> "ParsedEvent | None":
        """Handlers for everything that is NOT the multi-line AP record — station / handshake / tracker /
        probe / deauth / … and the info fallthrough. Split out so a deferred AP flush (in parse_line) can
        supersede this line's terminal event while its own record-start side-effects still run."""
        # Station (client) found — accumulate the five-line record; emit on the closing AP Vendor line.
        # (Placed after the AP multi-line block: station lines never match the ^SSID/^BSSID/^RSSI/^Channel
        # AP patterns, and "AP BSSID:" != "^BSSID:", so the two accumulators can't cross-consume.)
        m = _RE_STA_START.search(line)
        if m:
            rec: dict = {"client_mac": m.group(1)}
            mi = _RE_STA_INDEX.match(line)
            if mi:
                rec["index"] = int(mi.group(1))
            self._sta_record = rec
            return None
        if self._sta_record is not None:
            m = _RE_STA_VENDOR.search(line)
            if m:
                self._sta_record["vendor"] = m.group(1).strip().rstrip(",").strip()
                return None
            m = _RE_STA_AP_SSID.search(line)
            if m:
                self._sta_record["ap_ssid"] = m.group(1).strip().rstrip(",").strip()
                return None
            m = _RE_STA_AP_BSSID.search(line)
            if m:
                self._sta_record["ap_mac"] = m.group(1)
                return None
            m = _RE_STA_AP_VENDOR.search(line)
            if m:
                rec, self._sta_record = self._sta_record, None
                rec["ap_vendor"] = m.group(1).strip().rstrip(",").strip()
                return ParsedEvent(event_type="client_found", data=rec, raw=line)

        # Handshake captured (WPA/EAPOL) — three-line record; emit on the AP line, swallow the Pair line
        # so none of the three fall through to a bogus `info`. (See _RE_HS_* / callbacks.c above.)
        if _RE_HS_TRIGGER.search(line):
            self._handshake_stage = "ap"
            return None
        if self._handshake_stage == "ap":
            m = _RE_HS_AP.search(line)
            if m:
                self._handshake_stage = "pair"
                return ParsedEvent(
                    event_type="handshake_captured",
                    data={"ap_mac": m.group(1), "bssid": m.group(1)},
                    raw=line,
                )
            self._handshake_stage = None  # unexpected line after the trigger — abandon, handle normally
        elif self._handshake_stage == "pair":
            self._handshake_stage = None
            if _RE_HS_PAIR.search(line):
                return None  # swallow the Pair line that follows the emitted handshake

        # BLE tracker scans (Flipper / AirTag) — multi-line records closing on "RSSI: N dBm"; both emit
        # ble_found with a `kind` discriminator. Start lines are distinct ("[n] .. Flipper Found" /
        # "[n] AirTag Found"); the MAC/Name/RSSI continuation lines are shared by whichever is active.
        m = _RE_FLIPPER_START.match(line)
        if m:
            self._flipper_record = {"kind": "flipper", "index": int(m.group(1)),
                                    "flipper_type": (m.group(2) or "Unknown").strip().title()}
            return None
        m = _RE_AIRTAG_START.match(line)
        if m:
            rec = {"kind": "airtag", "index": int(m.group(1))}
            if m.group(2):
                rec["total"] = int(m.group(2))
            self._airtag_record = rec
            return None
        active = self._flipper_record if self._flipper_record is not None else self._airtag_record
        if active is not None:
            m = _RE_TRK_MAC.match(line)
            if m:
                active["mac"] = m.group(1)
                return None
            m = _RE_TRK_NAME.match(line)
            if m:
                active["name"] = m.group(1).strip().rstrip(",").strip()
                return None
            m = _RE_TRK_RSSI.match(line)
            if m:
                active["rssi"] = int(m.group(1))
                if self._flipper_record is active:  # clear whichever record just closed
                    self._flipper_record = None
                else:
                    self._airtag_record = None
                if active.get("mac"):  # a record with no MAC is malformed — drop, don't emit empty
                    return ParsedEvent(event_type="ble_found", data=active, raw=line)
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
            # On-LAN recon (needs a prior connect); commandline.c @ Development-deki, all SAFE.
            CommandInfo("scanports", "WiFi", "Port-scan the joined LAN"),
            CommandInfo("scanarp", "WiFi", "ARP-sweep the joined LAN"),
            CommandInfo("scanlocal", "WiFi", "mDNS / host discovery on the joined LAN"),
            CommandInfo("scanssh", "WiFi", "Discover SSH hosts on the joined LAN"),
            CommandInfo("netbiosscan", "WiFi", "NetBIOS name scan on the joined LAN"),
            CommandInfo("httpbannerscan", "WiFi", "Grab HTTP banners on the joined LAN"),
            CommandInfo("snmpprobe", "WiFi", "Probe for SNMP hosts on the joined LAN"),
            CommandInfo("enumscan", "WiFi", "Enumerate services/hosts on the joined LAN"),
            CommandInfo("congestion", "WiFi", "Channel-congestion analyzer"),
            # More WiFi recon/status (commandline.c @ Development-deki, all SAFE reads).
            CommandInfo("sweep", "WiFi", "Combined AP + station + BLE sweep, saved to SD"),
            CommandInfo("wpa3check", "WiFi", "Check WPA3/PMF compliance of the selected AP"),
            CommandInfo("trackap", "WiFi", "Track the selected AP by RSSI (needs select -a)"),
            CommandInfo("tracksta", "WiFi", "Track the selected station by RSSI (needs select -s)"),
            CommandInfo("wifistatus", "WiFi", "Show Wi-Fi STA status (SSID/RSSI/BSSID/channel)"),
            CommandInfo("autoreconnect <on|off>", "WiFi", "Toggle Wi-Fi auto-reconnect", "on|off"),
            # tplinktest actively toggles a Kasa smart-plug — no keyword, so explicit gate.
            CommandInfo("tplinktest <on|off|loop>", "WiFi", "Toggle a TP-Link Kasa smart plug",
                        "on|off|loop", danger="lab-only"),
            # Flock ALPR (surveillance-camera) detection — on-device GhostESP verbs (commandline.c
            # @ Development-deki), distinct from CC's OSM/DeFlock map import. All SAFE detection.
            CommandInfo("flockscan", "Flock", "Scan for Flock Safety ALPR cameras"),
            CommandInfo("flocklist", "Flock", "List detected Flock ALPR cameras"),
            CommandInfo("flockstop", "Flock", "Stop the Flock ALPR scan"),
            # DNS sinkhole (cmd_portal.c) — a filtering DNS server; start/reload actively intercept
            # DNS for pointed clients (MITM-capable), so they carry explicit danger="lab-only". The
            # blocklist-management + query verbs are SAFE.
            CommandInfo("sinkhole start", "Sinkhole", "Start the DNS sinkhole on :53",
                        danger="lab-only"),
            CommandInfo("sinkhole reload", "Sinkhole", "Reload the sinkhole blocklist (live)",
                        danger="lab-only"),
            CommandInfo("sinkhole stop", "Sinkhole", "Stop the DNS sinkhole"),
            CommandInfo("sinkhole status", "Sinkhole", "Sinkhole running state + config"),
            CommandInfo("sinkhole stats", "Sinkhole", "Sinkhole query/block counters"),
            CommandInfo("sinkhole add <domain>", "Sinkhole", "Add a blocklist domain", "domain"),
            CommandInfo("sinkhole remove <domain>", "Sinkhole", "Remove a blocklist domain",
                        "domain"),
            CommandInfo("sinkhole log <state>", "Sinkhole", "Toggle query logging", "on|off"),
            CommandInfo("sinkhole download", "Sinkhole", "Download the configured blocklist"),
            # WiFi attacks
            CommandInfo("attack -d", "Offensive", "Deauthentication attack (needs a prior select -a)", danger="lab-only"),
            CommandInfo("attack -e", "Offensive", "EAPOL logoff (works where 802.11w PMF blocks classic deauth)", danger="lab-only"),
            CommandInfo("attack -s <password>", "Offensive", "SAE flood vs WPA3 (needs ESP32-C5/C6 + target PSK)", "password", danger="lab-only"),
            CommandInfo("saeflood <password>", "Offensive", "SAE association flood vs WPA3", "password", danger="lab-only"),
            CommandInfo("stopsaeflood", "Offensive", "Stop the SAE flood"),
            CommandInfo("beaconspam -r", "Offensive", "Beacon spam (random SSIDs)", danger="lab-only"),
            CommandInfo("beaconspam -rr", "Offensive", "Rickroll beacon spam", danger="lab-only"),
            CommandInfo("beaconspam -l", "Offensive", "Beacon spam cloning all visible SSIDs", danger="lab-only"),
            CommandInfo("beaconspam <name>", "Offensive", "Beacon spam a specific SSID", "name", danger="lab-only"),
            # (removed phantom `probe`: no probe-flood verb in GhostESP; verified vs commandline.c)
            CommandInfo("karma start", "Offensive", "KARMA evil-twin: answer probes with the SSIDs clients ask for", danger="lab-only"),
            CommandInfo("karma stop", "Offensive", "Stop KARMA"),
            CommandInfo("dhcpstarve start", "Offensive", "DHCP-starvation flood (exhaust a LAN's address pool)", danger="lab-only"),
            CommandInfo("dhcpstarve stop", "Offensive", "Stop DHCP starvation", danger="lab-only"),
            CommandInfo("dhcpstarve display", "Offensive", "Show DHCP-starvation status",
                        danger="lab-only"),
            CommandInfo("stop", "Offensive", "Stop current attack"),
            # Evil portal
            CommandInfo("startportal", "Offensive", "Start evil portal", danger="lab-only"),
            CommandInfo("stopportal", "Offensive", "Stop evil portal"),
            CommandInfo("listportals", "Offensive", "Installed portal bundles", danger="lab-only"),
            CommandInfo("evilportal -c <cmd>", "Offensive", "Portal HTML: sethtmlstr / clear",
                        "cmd", danger="lab-only"),
            CommandInfo("webauth on", "Offensive", "Enable web-UI auth", danger="lab-only"),
            CommandInfo("webauth off", "Offensive", "Disable web-UI auth", danger="lab-only"),
            # BadUSB HID injection (cmd_badusb.c) — active keystroke/mouse injection into the host.
            # No danger keyword in the names, so the explicit danger= is what gates them.
            CommandInfo("badusb run <file>", "Offensive", "Run a DuckyScript (HID injection)",
                        "file", danger="lab-only"),
            CommandInfo("badusb exec <size>", "Offensive", "Receive a DuckyScript then inject it",
                        "size", danger="lab-only"),
            CommandInfo("badusb type <text>", "Offensive", "Type text via the USB keyboard",
                        "text", danger="lab-only"),
            CommandInfo("badusb keysend <mod> <key>", "Offensive", "Send one HID keypress",
                        "mod key", danger="lab-only"),
            CommandInfo("badusb jiggle_start", "Offensive", "Start the USB mouse jiggler",
                        danger="lab-only"),
            CommandInfo("badusb keyboard_start", "Offensive", "Enter USB keyboard (HID) mode",
                        danger="lab-only"),
            CommandInfo("badusb trackpad_start", "Offensive", "Enter USB trackpad (HID) mode",
                        danger="lab-only"),
            # BLE
            CommandInfo("blescan", "BLE", "Scan for BLE devices"),
            CommandInfo("blescan -s", "BLE", "Stop BLE operations"),
            CommandInfo("blescan -f", "BLE", "Scan for Flipper Zero devices"),
            CommandInfo("blescan -ds", "BLE", "Detect BLE-spam sources"),
            CommandInfo("blescan -r", "BLE", "Raw BLE traffic scan"),
            CommandInfo("trackgatt", "BLE", "Track a BLE (GATT) device by RSSI"),
            # (removed phantom `bleskimmer`: skimmer detection is Marauder's sniffskim, not this fw)
            CommandInfo("blewardriving", "BLE", "BLE wardriving (GPS-tagged beacons)"),
            CommandInfo("blewardriving -s", "BLE", "Stop BLE wardriving"),
            CommandInfo("blespam", "Offensive", "BLE advertisement spam (pairing popups)", danger="lab-only"),
            CommandInfo("blespam -s", "Offensive", "Stop BLE spam"),
            CommandInfo("aerialscan", "BLE", "Scan for AirTags / aerial trackers"),
            CommandInfo("listairtags", "BLE", "List detected AirTags"),
            CommandInfo("selectairtag <idx>", "BLE", "Select an AirTag by index", "idx"),
            CommandInfo("spoofairtag", "Offensive", "Spoof an AirTag advertisement",
                        danger="lab-only"),
            CommandInfo("stopspoof", "Offensive", "Stop the AirTag spoof"),
            CommandInfo("listflippers", "BLE", "List nearby Flipper Zero devices"),
            CommandInfo("selectflipper <idx>", "BLE", "Select a Flipper by index", "idx"),
            # BLE GATT recon (commandline.c; #ifndef ESP32-S2 — real on non-S2, all SAFE reads).
            CommandInfo("listgatt", "BLE", "List discovered BLE GATT devices"),
            CommandInfo("selectgatt <idx>", "BLE", "Select a GATT device by index", "idx"),
            CommandInfo("enumgatt", "BLE", "Enumerate GATT services on the selected device"),
            CommandInfo("listadv", "BLE", "List detected BLE advertisers"),
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
            # WiGLE upload integration (commandline.c @ Development-deki, all SAFE).
            CommandInfo("wigle api <token>", "Wardrive", "Set the WiGLE API key", "token"),
            CommandInfo("wigle auto <on|off>", "Wardrive", "Toggle auto-upload at boot", "on|off"),
            CommandInfo("wigle donate <on|off>", "Wardrive", "Toggle donating to WiGLE", "on|off"),
            CommandInfo("wigle show", "Wardrive", "Show current WiGLE settings"),
            CommandInfo("wigle list", "Wardrive", "List stored uploaded-CSV memory"),
            CommandInfo("wigle files <page>", "Wardrive", "List GPS CSVs to upload", "page"),
            CommandInfo("wigle upload all", "Wardrive", "Upload all pending wardrive CSVs"),
            CommandInfo("wigle upload <file>", "Wardrive", "Upload one wardrive CSV", "file"),
            CommandInfo("wigle stats", "Wardrive", "Show WiGLE account stats"),
            # Streaming wardrive (passive AP/BLE+GPS records; commandline.c, SAFE).
            CommandInfo("wdstream start", "Wardrive", "Start the streaming wardrive"),
            CommandInfo("wdstream stop", "Wardrive", "Stop the streaming wardrive"),
            CommandInfo("wdstream status", "Wardrive", "Streaming-wardrive status"),
            # Radio spectrum analyzers (add-on modules) — RX-ONLY, verified no CLI TX path
            # (cmd_nrf24.c: no tx verb, no W_TX_PAYLOAD; cmd_subghz.c load/replay only displays).
            CommandInfo("nrf24 start", "NRF24", "Start the nRF24 2.4GHz analyzer (RX-only)"),
            CommandInfo("nrf24 stop", "NRF24", "Stop the nRF24 analyzer"),
            CommandInfo("nrf24 status", "NRF24", "nRF24 analyzer state + SPI pins"),
            CommandInfo("subghz start", "SubGHz", "Start the CC1101 sub-GHz scanner (RX-only)"),
            CommandInfo("subghz stop", "SubGHz", "Stop the SubGHz scanner"),
            CommandInfo("subghz status", "SubGHz", "SubGHz scanner state + snapshot + pins"),
            CommandInfo("subghz capture", "SubGHz", "Capture a spectrum snapshot in RAM", "name"),
            CommandInfo("subghz save", "SubGHz", "Save the current snapshot to SD", "name"),
            CommandInfo("subghz load <name>", "SubGHz", "Load a saved snapshot", "name"),
            CommandInfo("subghz list", "SubGHz", "List saved SubGHz snapshots on SD"),
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
            CommandInfo("badusb list", "System", "List BadUSB scripts on the SD card"),
            CommandInfo("badusb stop", "System", "Stop the current BadUSB script"),
            CommandInfo("chipinfo", "System", "Device / chip info"),
            CommandInfo("reboot", "System", "Reboot device"),
            CommandInfo("gpsinfo", "System", "GPS status"),
            CommandInfo("sd info", "System", "SD card info"),
            CommandInfo("rgbmode", "System", "Set the RGB LED mode"),
            CommandInfo("settings", "System", "Show settings"),
            CommandInfo("settings list", "System", "List all settings"),
            CommandInfo("settings get <key>", "System", "Read a setting value", "key"),
            CommandInfo("settings set <key> <value>", "System", "Write a setting value", "key,value"),
            CommandInfo("settings reset", "System", "Reset settings to defaults"),
            CommandInfo("mem", "System", "Heap diagnostics"),
            CommandInfo("mem dump", "System", "Dump heap diagnostics"),
            CommandInfo("timezone <TZ>", "System", "Set the device timezone", "TZ"),
            # Config / apps / SoftAP management (commandline.c @ Development-deki, all SAFE).
            CommandInfo("gpspin <pin>", "System", "Set the GPS UART RX pin (no arg = show)", "pin"),
            CommandInfo("gpsbaud <rate>", "System", "Set the GPS UART baud rate", "rate"),
            CommandInfo("loadconfig", "System", "Load config.cfg from SD and apply"),
            CommandInfo("apps list", "System", "List installed SD-card plugin apps"),
            CommandInfo("apps reload", "System", "Rescan the SD card + reload app manifests"),
            CommandInfo("apps info <id>", "System", "Show details for an installed app", "id"),
            CommandInfo("apps run <id>", "System", "Launch an installed app by id", "id"),
            CommandInfo("apps stop", "System", "Stop the currently running app"),
            CommandInfo("apps reset <id>", "System", "Reset an app's saved state", "id"),
            CommandInfo("apenable on", "System", "Enable the device SoftAP on boot"),
            CommandInfo("apenable off", "System", "Disable the device SoftAP on boot"),
            CommandInfo("apcred <ssid> <pwd>", "System", "Set SoftAP SSID/password", "ssid pwd"),
            CommandInfo("webuiap on", "System", "Restrict the Web UI to the AP interface"),
            CommandInfo("webuiap off", "System", "Allow the Web UI on all interfaces"),
            CommandInfo("webuiap status", "System", "Show whether the Web UI is AP-only"),
            CommandInfo("help", "System", "Show help"),
            # (removed phantom `setch`/`getch`: GhostESP has no standalone channel verb)
            # Flipper bridge (list/select are in the BLE group; only a BT bridge verb exists)
            CommandInfo("blebridge", "Flipper", "Bridge to a selected Flipper over BLE"),
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
        # (removed "Probe Flood" -> phantom `probe` verb; GhostESP has no probe-flood command)
    ],
    TargetType.CLIENT: [
        TargetAction("Deauth Client", "attack -d", "Disconnect this client", ActionCategory.ATTACK, requires_selection=True, pre_commands=["select -a {index}"]),
    ],
    TargetType.BLE: [
        TargetAction("AirTag Scan", "aerialscan", "Scan for nearby AirTags", ActionCategory.SCAN),
        TargetAction("BLE Track", "trackgatt", "Track this device (RSSI)", ActionCategory.MONITOR),
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
