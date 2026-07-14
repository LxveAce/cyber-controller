"""BW16 protocol — serial parser for the RTL8720DN "Vampire Deauther" firmware.

The BW16 module (Realtek RTL8720DN, dual-band 2.4/5 GHz) running the Vampire
Deauther firmware exposes a real serial CLI built around ``AT+`` style
commands. This sets it apart from receive-only or display-only firmware: the
BW16 can both scan and transmit, so its offensive commands are annotated with
the appropriate danger class for the safety/disclaimer system.

Confirmed command set (Vampire Deauther):
    AT+SCAN              -- scan WiFi networks (2.4 + 5 GHz)
    AT+DEAUTHIDX=<n>     -- deauth the network at scan index n   (lab-only)
    AT+DEAUTHIDX=ALL     -- deauth all scanned networks          (lab-only)
    AT+BEACONRANDOM=<n>  -- beacon spam n random SSIDs           (lab-only)
    AT+STOP             -- stop the current operation

Command formatting (``format_command``) follows the AT+ convention: with no
args the command name is sent verbatim (``AT+SCAN``); with args the first value
is appended as ``=value`` (``AT+DEAUTHIDX=ALL``, ``AT+BEACONRANDOM=5``).

Parsing notes:
    The Vampire scan output was CONFIRMED on a real RTL8720DN at 115200 baud::

        [SCAN] Starting...
        [SCAN] Complete.
        [SCAN] Results:
        0: KashPatels007 (CH 1, RSSI -42)
        2: KashPatels007 (CH 44, RSSI -46)      # CH 36-165 == 5 GHz (dual-band)
        14:  (CH 136, RSSI -77)                 # empty SSID == hidden network

    Each result is ``<index>: <SSID> (CH <channel>, RSSI <rssi>)`` — the SSID may
    contain spaces or be empty, and channel/RSSI are always present (no BSSID is
    printed). ``parse_line`` matches that confirmed format first, with a tolerant
    bracketed layout (``[0] SSID ch:6 -42dBm BSSID``) kept as a fallback for other
    RTL deauther forks. Bracketed status tags map to events: ``[ERROR] ...`` ->
    ``status`` (ok=False), and ``[SCAN]`` / ``[SYS]`` / other tags -> ``info``
    (with the tag preserved). RTL8720 boot / Ameba SDK banner noise maps to
    ``info`` as well.
"""

from __future__ import annotations

import re

from src.models.action import ActionCategory, TargetAction
from src.models.target import TargetType
from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# --- Regex patterns for BW16 / Vampire Deauther serial output ---

# Vampire Deauther scan line — CONFIRMED on RTL8720DN hardware (115200 baud):
#   "0: KashPatels007 (CH 1, RSSI -42)"
#   "14:  (CH 136, RSSI -77)"   <- empty SSID (hidden net); CH 36-165 == 5 GHz
# The SSID may contain spaces or be empty; channel + RSSI are always present.
_RE_AP_VAMPIRE = re.compile(
    # SSID capture bounded ({0,64}?) so a long line missing "(CH ..." can't drive O(n^2) regex
    # backtracking (the lazy dot and the trailing \s* overlap on whitespace) and pin the serial
    # reader thread on a ~64 KiB un-terminated flush. A real SSID is <=32 octets (may be empty).
    r"^(?P<index>\d+):\s(?P<ssid>.{0,64}?)\s*"
    r"\(CH\s+(?P<channel>\d+),\s*RSSI\s+(?P<rssi>-?\d+)\)\s*$",
    re.IGNORECASE,
)

# Fallback layout seen on some other RTL deauther forks (bracketed index,
# labelled fields, BSSID last): "[0] MySSID  ch:6  -42dBm  AA:BB:CC:DD:EE:FF".
# Channel, RSSI and BSSID are all optional so a sparse fork still parses.
_RE_AP_BRACKET = re.compile(
    r"^\[(?P<index>\d+)\]\s+"
    r"(?P<ssid>.{1,64}?)"   # bounded: see _RE_AP_VAMPIRE (same O(n^2)-backtracking guard)
    r"(?:\s+ch:\s*(?P<channel>\d+))?"
    r"(?:\s+(?P<rssi>-?\d+)\s*dBm)?"
    r"(?:\s+(?P<bssid>[0-9A-Fa-f:]{17}))?"
    r"\s*$",
    re.IGNORECASE,
)

# Bracketed status tags emitted by the Vampire firmware — CONFIRMED:
#   "[SCAN] Starting...", "[SCAN] Complete.", "[SCAN] Results:",
#   "[ERROR] Unknown command: AT", "[SYS] ..."
_RE_BRACKET_TAG = re.compile(r"^\[(?P<tag>[A-Za-z0-9_]+)\]\s*(?P<msg>.*)$")

# Bare AT acknowledgements seen on some forks: "OK" / "ERROR: reason".
_RE_STATUS = re.compile(r"^(OK|ERROR)\b\s*:?\s*(.*)$", re.IGNORECASE)


class BW16Protocol(BaseProtocol):
    """Parser and command formatter for the BW16 Vampire Deauther firmware.

    The BW16 (Realtek RTL8720DN, dual-band 2.4/5 GHz) speaks an ``AT+`` serial
    CLI. This protocol formats those commands, parses the numbered scan list
    plus the ``OK`` / ``ERROR`` acknowledgements, and treats Ameba-D / rltk_wlan
    SDK boot banners as informational noise.
    """

    @property
    def protocol_name(self) -> str:
        return "bw16"

    capabilities = frozenset({"deauth", "wifi"})

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        """Parse a single BW16 serial output line.

        Returns an ``ap_found`` event for a recognised scan-list entry, a
        ``status`` event for ``OK`` / ``ERROR`` acknowledgements, and an
        ``info`` event for any other non-empty line (boot / SDK noise, unknown
        output). Blank lines return ``None``.
        """
        line = line.strip()
        if not line:
            return None

        # Scan-list AP entry (confirmed Vampire format first, then fork fallback).
        for pattern in (_RE_AP_VAMPIRE, _RE_AP_BRACKET):
            m = pattern.match(line)
            if m:
                return self._ap_event(m, line)

        # Bracketed status tags: [ERROR] -> failed status; [SCAN]/[SYS]/... -> info.
        m = _RE_BRACKET_TAG.match(line)
        if m:
            tag = m.group("tag").upper()
            msg = m.group("msg").strip()
            if tag == "ERROR":
                err: dict[str, object] = {"ok": False, "tag": tag}
                if msg:
                    err["message"] = msg
                return ParsedEvent(event_type="status", data=err, raw=line)
            info: dict[str, object] = {"tag": tag}
            if msg:
                info["message"] = msg
            return ParsedEvent(event_type="info", data=info, raw=line)

        # Bare AT acknowledgement: OK / ERROR.
        m = _RE_STATUS.match(line)
        if m:
            data: dict[str, object] = {"ok": m.group(1).upper() == "OK"}
            detail = m.group(2).strip()
            if detail:
                data["message"] = detail
            return ParsedEvent(event_type="status", data=data, raw=line)

        # Everything else (RTL8720 boot banner, Ameba SDK noise, unknowns).
        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    @staticmethod
    def _ap_event(m: re.Match[str], line: str) -> ParsedEvent:
        """Build an ``ap_found`` event from a matched scan-list line.

        Only the fields the firmware actually printed are populated; the index
        and SSID are always present, while BSSID / channel / RSSI are optional.
        """
        data: dict[str, object] = {
            "index": int(m.group("index")),
            "ssid": m.group("ssid").strip(),
        }
        groups = m.groupdict()
        if groups.get("bssid"):
            data["bssid"] = groups["bssid"]
        if groups.get("channel"):
            data["channel"] = int(groups["channel"])
        if groups.get("rssi"):
            data["rssi"] = int(groups["rssi"])
        return ParsedEvent(event_type="ap_found", data=data, raw=line)

    # ── Commands ─────────────────────────────────────────────────────

    def get_commands(self) -> list[CommandInfo]:
        """Return the confirmed BW16 Vampire Deauther command set.

        Transmit operations (deauth, beacon spam) are annotated ``lab-only``:
        they emit 802.11 frames and must only be run in an authorized,
        controlled environment.
        """
        return [
            # ---- Scanning ----
            CommandInfo(
                "AT+SCAN",
                "Scanning",
                "Scan for WiFi networks (2.4 + 5 GHz)",
            ),
            # ---- Attack ----
            CommandInfo(
                # Placeholder embedded in the name so the device-tab arg-entry prompts for the index
                # and sends "AT+DEAUTHIDX=<value>"; a bare "AT+DEAUTHIDX" would be sent value-less.
                "AT+DEAUTHIDX=<idx>",
                "Attack",
                "Deauth the network at scan index n",
                "idx",
                danger="lab-only",
            ),
            CommandInfo(
                "AT+DEAUTHIDX=ALL",
                "Attack",
                "Deauth all scanned networks",
                danger="lab-only",
            ),
            CommandInfo(
                "AT+BEACONRANDOM=<count>",
                "Attack",
                "Beacon spam n random SSIDs",
                "count",
                danger="lab-only",
            ),
            # ---- System ----
            CommandInfo(
                "AT+STOP",
                "System",
                "Stop the current operation",
            ),
        ]

    # ── Formatting ───────────────────────────────────────────────────

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        """Format a command string for serial transmission (AT+ convention).

        With no args the command name is returned verbatim
        (``format_command("AT+SCAN") -> "AT+SCAN"``). With args, the first
        value is appended after ``=``
        (``format_command("AT+DEAUTHIDX", {"idx": "ALL"}) -> "AT+DEAUTHIDX=ALL"``).
        Empty / falsy arg values are ignored so the bare command name is sent.
        """
        if args:
            for val in args.values():
                text = str(val).strip()
                if text:
                    return f"{cmd}={text}"
        return cmd

    # ── Auto-detection ───────────────────────────────────────────────

    def identify(self, line: str) -> bool:
        """Return True if the line looks like BW16 / RTL8720DN output.

        Matches the AT+ command echo plus the distinctive Realtek Ameba-D /
        rltk_wlan boot banners emitted by the RTL8720DN ROM and SDK.
        """
        markers = (
            "AT+",
            "RTL_HalBleMacInit",
            "rltk_wlan",
            "hci_read_rom_check",
            "AmebaD",
        )
        return any(m in line for m in markers)


# --- Target actions: what this protocol can do to each target type ---
# BW16 selects scan results by INDEX (AT+DEAUTHIDX=<n>), not by MAC, and its Vampire scan prints no BSSID.
# The ingest layer keeps those APs under a synthetic source-tagged key with their scan index, and the
# resolver only offers this {index} action for APs THIS BW16 discovered (source-restricted) and drops it
# when no index is known — a scan index is valid only for its own device's list.
TARGET_ACTIONS: dict[TargetType, list[TargetAction]] = {
    TargetType.AP: [
        TargetAction(
            "Deauth (this index)",
            "AT+DEAUTHIDX={index}",
            "Deauth the AP at its BW16 scan index — lab-only, illegal to operate over the air; valid only "
            "for the BW16 that scanned it.",
            ActionCategory.ATTACK,
        ),
    ],
}


# --- Unified Action Broadcast capability map (verb -> (pre_commands, command)).
# Commands are each firmware's NATIVE realization; absent verb == device skipped. ---
from src.core.broadcast import BroadcastVerb  # noqa: E402  (bottom import avoids a cycle)

BROADCAST_CAPABILITIES = {
    BroadcastVerb.FIND_APS:    ((), "AT+SCAN"),
    BroadcastVerb.DEAUTH_ALL:  ((), "AT+DEAUTHIDX=ALL"),
    BroadcastVerb.BEACON_SPAM: ((), "AT+BEACONRANDOM=20"),
    BroadcastVerb.STOP_ALL:    ((), "AT+STOP"),
}
