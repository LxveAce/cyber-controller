"""ESP32-DIV (LxveLabs serial fork) protocol.

Stock cifertech/ESP32-DIV is touch/button-only and speaks nothing over serial, so CC models it as a
``controlmap`` device (see ``esp32_div.py`` / the two-profile plan). This protocol is for the LxveLabs
**serial fork**, which adds a line-based serial CLI: it answers with an ``LXVEDIV/1`` identity banner and
emits structured ``TAG key=val`` result lines. Wire contract:
``command-center/projects/cc-app/LXVEDIV-SERIAL-PROTOCOL-2026-07-21.md``.

The parser is pure and unit-tested against canned LXVEDIV lines (no hardware). Data keys match the stock DIV
parser (``ap_found``/``client_found``/``ble_found`` etc.) so the target pool / analyzers populate identically;
the fork supplies its own stable ``idx`` per target (used by ``select ap/sta/ble <n>``).
"""
from __future__ import annotations

import re

from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# The identity prefix the fork prints on boot and on `id`/`status`/`version`. This is what distinguishes the
# serial fork from a stock DIV (which prints nothing on serial), so `identify()` keys on it.
_RE_IDENT = re.compile(r"^LXVEDIV/\d", re.IGNORECASE)


def _kv(rest: str) -> dict[str, str]:
    """Parse the ``key=val`` pairs of a structured result line into a dict. Values are unquoted single tokens
    (SSIDs with spaces are the last field in their tag, so `_kv` keeps it simple: split on whitespace)."""
    out: dict[str, str] = {}
    for tok in rest.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def _int(d: dict[str, str], key: str) -> int:
    try:
        return int(d.get(key, ""))
    except (TypeError, ValueError):
        return 0


class Esp32DivSerialProtocol(BaseProtocol):
    """Parser + command catalog for the LxveLabs ESP32-DIV serial fork (LXVEDIV/1)."""

    capabilities: "frozenset[str]" = frozenset({"wifi", "ble", "nrf24"})
    driver_type = "text-cli"

    @property
    def protocol_name(self) -> str:
        return "esp32-div-serial"

    def identify(self, line: str) -> bool:
        return bool(_RE_IDENT.search(line.strip()))

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None

        # The identity/status banner (also the poll-safe `status` reply).
        if _RE_IDENT.search(line):
            return ParsedEvent(event_type="status", data={"message": line}, raw=line)

        tag, _, rest = line.partition(" ")
        tag = tag.upper()

        if tag == "AP":
            d = _kv(rest)
            return ParsedEvent(
                event_type="ap_found",
                data={
                    "ssid": d.get("ssid", ""),
                    "bssid": d.get("bssid", ""),
                    "channel": _int(d, "ch"),
                    "rssi": _int(d, "rssi"),
                    "encryption": d.get("enc", ""),
                    "index": _int(d, "idx"),
                },
                raw=line,
            )
        if tag == "STA":
            d = _kv(rest)
            return ParsedEvent(
                event_type="client_found",
                data={
                    "mac": d.get("mac", ""),
                    "bssid": d.get("ap", ""),  # the AP this station is associated with
                    "rssi": _int(d, "rssi"),
                    "index": _int(d, "idx"),
                },
                raw=line,
            )
        if tag == "BLE":
            d = _kv(rest)
            return ParsedEvent(
                event_type="ble_found",
                data={
                    "mac": d.get("mac", ""),
                    "name": d.get("name", ""),
                    "rssi": _int(d, "rssi"),
                    "index": _int(d, "idx"),
                },
                raw=line,
            )
        if tag == "PMKID":
            d = _kv(rest)
            return ParsedEvent(
                event_type="pmkid_captured",
                data={"bssid": d.get("bssid", ""), "pmkid": d.get("pmkid", "")},
                raw=line,
            )
        if tag == "EAPOL":
            d = _kv(rest)
            return ParsedEvent(
                event_type="handshake_captured",
                data={"bssid": d.get("bssid", "")},
                raw=line,
            )
        if tag == "ATTACK":
            d = _kv(rest)
            # A started deauth registers as deauth activity; other attack tags are informational.
            if d.get("verb", "").startswith("deauth") and d.get("state") == "start":
                return ParsedEvent(event_type="deauth_sent", data={"target": d.get("target", "")}, raw=line)
            return ParsedEvent(event_type="info", data={"message": line}, raw=line)
        if tag == "PKT":
            d = _kv(rest)
            return ParsedEvent(event_type="packet", data={"info": rest.strip()}, raw=line)

        # BLESPAM / JAM / anything else -> raw passthrough (surfaces in the serial monitor).
        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    def get_commands(self) -> list[CommandInfo]:
        return [
            # ── WiFi scan / sniff (safe) ──────────────────────────────
            CommandInfo("scanwifi", "WiFi", "Scan for access points"),
            CommandInfo("scansta", "WiFi", "Scan for stations / clients"),
            CommandInfo("scanall", "WiFi", "Scan APs + stations"),
            CommandInfo("stopscan", "WiFi", "Stop the current scan"),
            CommandInfo("list ap", "WiFi", "List discovered access points"),
            CommandInfo("list sta", "WiFi", "List discovered stations"),
            CommandInfo("setch <ch>", "WiFi", "Set WiFi channel (1-14)", "ch"),
            CommandInfo("getch", "WiFi", "Get the current WiFi channel"),
            CommandInfo("hop start", "WiFi", "Start channel hopping"),
            CommandInfo("hop stop", "WiFi", "Stop channel hopping"),
            CommandInfo("sniff start", "Capture", "Start the packet sniffer"),
            CommandInfo("sniff stop", "Capture", "Stop the packet sniffer"),
            CommandInfo("pmkid start", "Capture", "Capture PMKID hashes"),
            CommandInfo("pmkid stop", "Capture", "Stop PMKID capture"),
            CommandInfo("handshake start", "Capture", "Capture WPA handshakes"),
            CommandInfo("handshake stop", "Capture", "Stop handshake capture"),
            CommandInfo("capture start", "Capture", "Start raw packet capture"),
            CommandInfo("capture stop", "Capture", "Stop raw packet capture"),
            CommandInfo("capture save", "Capture", "Save the capture to SD"),
            # ── WiFi attacks (lab-only) ───────────────────────────────
            CommandInfo("deauth", "Attack", "Deauth the selected target", danger="lab-only"),
            CommandInfo("deauth all", "Attack", "Deauth all discovered APs", danger="lab-only"),
            CommandInfo("beacon", "Attack", "Beacon spam (random SSIDs)", danger="lab-only"),
            CommandInfo("beacon list", "Attack", "Beacon spam from an SSID list", danger="lab-only"),
            CommandInfo("beacon target", "Attack", "Clone the target AP's beacons", danger="lab-only"),
            CommandInfo("probe", "Attack", "Probe-request flood", danger="lab-only"),
            CommandInfo("rickroll", "Attack", "Rickroll beacon spam", danger="lab-only"),
            CommandInfo("stopattack", "Attack", "Stop the current attack"),
            # ── BLE (scan safe; spam lab-only) ────────────────────────
            CommandInfo("scanble", "BLE", "Scan for BLE devices"),
            CommandInfo("blestop", "BLE", "Stop the BLE scan"),
            CommandInfo("list ble", "BLE", "List discovered BLE devices"),
            CommandInfo("blespam", "BLE", "BLE notification spam (all)", danger="lab-only"),
            CommandInfo("blespam apple", "BLE", "BLE spam (Apple popups)", danger="lab-only"),
            CommandInfo("blespam samsung", "BLE", "BLE spam (Samsung)", danger="lab-only"),
            CommandInfo("blespam google", "BLE", "BLE spam (Google Fast Pair)", danger="lab-only"),
            CommandInfo("blespam windows", "BLE", "BLE spam (Windows Swift Pair)", danger="lab-only"),
            CommandInfo("blespam random", "BLE", "BLE spam (random)", danger="lab-only"),
            # ── 2.4GHz / nRF24 ────────────────────────────────────────
            CommandInfo("nrf scan", "2.4GHz", "NRF24 device scan"),
            CommandInfo("nrf sniff", "2.4GHz", "NRF24 packet sniffing"),
            CommandInfo("nrf stop", "2.4GHz", "Stop NRF24 operations"),
            CommandInfo("nrf jam", "2.4GHz", "NRF24 channel jamming (fork: inert stub — jam-fire is owner/Mythos-completed)", danger="illegal-tx"),
            # ── Target select (safe) ──────────────────────────────────
            CommandInfo("select ap <n>", "Target", "Select an AP by index", "n"),
            CommandInfo("select sta <n>", "Target", "Select a station by index", "n"),
            CommandInfo("select ble <n>", "Target", "Select a BLE device by index", "n"),
            CommandInfo("clear", "Target", "Clear all discovered targets"),
            # ── Storage (safe) ────────────────────────────────────────
            CommandInfo("save", "Storage", "Save results to SD"),
            CommandInfo("save pcap", "Storage", "Save the capture as PCAP"),
            CommandInfo("save hashes", "Storage", "Save captured hashes"),
            CommandInfo("sd info", "Storage", "SD card status"),
            CommandInfo("sd ls", "Storage", "List SD card files"),
            # ── System (safe) ─────────────────────────────────────────
            CommandInfo("id", "System", "Print the LXVEDIV identity banner"),
            CommandInfo("version", "System", "Firmware version"),
            CommandInfo("status", "System", "Current status (poll-safe)"),
            CommandInfo("stop", "System", "Stop all operations"),
            CommandInfo("reboot", "System", "Reboot the device"),
            CommandInfo("led <r> <g> <b>", "System", "Set the LED colour (0-255)", "r,g,b"),
            CommandInfo("led off", "System", "Turn the LED off"),
            CommandInfo("settings", "System", "Show settings"),
            CommandInfo("help", "System", "Show help"),
        ]

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{cmd} {arg_str}"
        return cmd
