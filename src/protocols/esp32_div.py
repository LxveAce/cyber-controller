"""ESP32-DIV protocol — INBOUND-only parser + on-device tool catalog.

ESP32-DIV (cifertech/ESP32-DIV) is a TOUCH/BUTTON-driven multi-tool. Unlike
Marauder, Ghost ESP, Bruce, or HaleHound, it exposes **no serial command
interface**: this was verified on hardware — every serial command sent to the
device returns silence. The firmware only ever *prints* to the serial console
(its boot banner, on-screen button events, and ESP-IDF driver logs); it never
*reads* commands from it.

Consequences for this protocol class:

* ``parse_line`` is INBOUND-ONLY. It interprets the lines DIV emits (banner,
  button presses, ESP-IDF errors, misc info) into ``ParsedEvent`` objects.
* ``get_commands`` is a documented **catalog** of the device's on-device tools
  (the things you reach via the touchscreen / buttons), each annotated with a
  danger flag. These are *reference / UI* entries, NOT sendable serial
  commands. The catalog lets the rest of cyber-controller render DIV's
  capabilities (and gate the dangerous RF/NFC tools behind a confirmation)
  even though there is nothing to transmit.
* ``format_command`` exists only to satisfy the ``BaseProtocol`` contract; it
  formats a name for *display* and explicitly does not produce something the
  device will act on (DIV ignores serial input entirely).

Captured v1.1.0 boot output this parser handles::

    ==================================
    ESP32-DIV
    Developed by: CiferTech
    Version:      1.1.0
    Contact:      cifertech@gmail.com
    GitHub:       github.com/cifertech
    ==================================
    Button 0: Pressed
    ...
    E (622) ADC: adc1_lock_release(419): adc lock release failed

On a bare board (no shield wired up) every ``Button N`` reads ``Pressed``;
the parser reports the literal reported state and does not try to "correct"
it. ESP-IDF logs of the form ``E (<ticks>) TAG: message`` are surfaced as
``error`` events so the UI can show driver problems without the user having to
read raw console spew.
"""

from __future__ import annotations

import re

from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# --- Regex patterns for ESP32-DIV serial output (INBOUND only) ----------

# Version line from the boot banner:  "Version:      1.1.0"
# Captures the version token (digits/dots, optionally a -suffix like 1.1.0-rc1).
_RE_VERSION = re.compile(
    r"^Version:\s*([0-9][0-9A-Za-z.\-_]*)\s*$",
    re.IGNORECASE,
)

# Button event:  "Button 0: Pressed"  /  "Button 7: Released"
_RE_BUTTON = re.compile(
    r"^Button\s+(\d+):\s*(Pressed|Released)\s*$",
    re.IGNORECASE,
)

# ESP-IDF log line:  "E (622) ADC: adc1_lock_release(419): ..."
# Level is one of E/W/I/D/V; the tick count is in parens; then "TAG: message".
# Only E (error) and W (warning) levels are treated as errors; lower levels
# (I/D/V) fall through to generic info so we don't cry wolf on routine logs.
_RE_ESP_IDF = re.compile(
    r"^([EWIDV])\s*\((\d+)\)\s*([^:]+):\s*(.*)$",
)

# Banner identity / contact lines we surface as device_info rather than info,
# so the boot banner reads cleanly. (The Version line is handled separately and
# is the authoritative firmware/version event.)
_RE_DEVELOPED_BY = re.compile(r"^Developed by:\s*(.+?)\s*$", re.IGNORECASE)
_RE_CONTACT = re.compile(r"^Contact:\s*(.+?)\s*$", re.IGNORECASE)
_RE_GITHUB = re.compile(r"^GitHub:\s*(.+?)\s*$", re.IGNORECASE)

# A line that is purely the banner rule "====..." (any run of '=').
_RE_RULE = re.compile(r"^=+\s*$")

# The bare product-name banner line.
_RE_PRODUCT = re.compile(r"^ESP32-DIV\s*$", re.IGNORECASE)

# Generic error/fail wording on a free-form line (belt-and-suspenders for any
# error text DIV prints that is not in the strict ESP-IDF format).
_RE_GENERIC_ERROR = re.compile(r"\b(?:error|fail(?:ed|ure)?|panic|abort)\b", re.IGNORECASE)


class Esp32DivProtocol(BaseProtocol):
    """Inbound-only parser and on-device tool catalog for ESP32-DIV.

    DIV has no serial command interface (verified on hardware), so:

    * ``parse_line`` interprets what the device prints; it never expects a
      reply to anything we send.
    * ``get_commands`` returns a reference catalog of the device's
      touchscreen/button tools, with danger flags — not sendable commands.

    See the module docstring for the full rationale.
    """

    @property
    def protocol_name(self) -> str:
        return "esp32-div"

    # ── Parsing (INBOUND only) ───────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        """Parse a single ESP32-DIV serial output line.

        Returns:
            * ``device_info`` {firmware, version} for the banner Version line;
            * ``device_info`` {field, value} for other banner identity lines;
            * ``button`` {index, state} for "Button N: Pressed/Released";
            * ``error`` {message, ...} for ESP-IDF "E (...)"/"W (...)" logs and
              any other error-worded line;
            * ``info`` {message} for anything else non-empty;
            * ``None`` for an empty/whitespace line.
        """
        line = line.strip()
        if not line:
            return None

        # --- Boot banner: authoritative firmware/version ---
        m = _RE_VERSION.match(line)
        if m:
            return ParsedEvent(
                event_type="device_info",
                data={"firmware": "esp32-div", "version": m.group(1).strip()},
                raw=line,
            )

        # --- Button event ---
        m = _RE_BUTTON.match(line)
        if m:
            # Normalise state capitalisation (Pressed/Released) regardless of
            # how the firmware cased it, but keep the raw line intact.
            state = m.group(2).strip().capitalize()
            return ParsedEvent(
                event_type="button",
                data={"index": int(m.group(1)), "state": state},
                raw=line,
            )

        # --- ESP-IDF driver log: "E (622) ADC: message" ---
        m = _RE_ESP_IDF.match(line)
        if m:
            level, ticks, tag, message = m.groups()
            level = level.upper()
            if level in ("E", "W"):
                return ParsedEvent(
                    event_type="error",
                    data={
                        "message": message.strip(),
                        "tag": tag.strip(),
                        "level": level,
                        "ticks": int(ticks),
                    },
                    raw=line,
                )
            # I/D/V informational logs: surface as info, keeping the fields.
            return ParsedEvent(
                event_type="info",
                data={
                    "message": message.strip(),
                    "tag": tag.strip(),
                    "level": level,
                    "ticks": int(ticks),
                },
                raw=line,
            )

        # --- Remaining banner identity lines -> device_info ---
        m = _RE_DEVELOPED_BY.match(line)
        if m:
            return ParsedEvent(
                event_type="device_info",
                data={"field": "developer", "value": m.group(1).strip()},
                raw=line,
            )
        m = _RE_CONTACT.match(line)
        if m:
            return ParsedEvent(
                event_type="device_info",
                data={"field": "contact", "value": m.group(1).strip()},
                raw=line,
            )
        m = _RE_GITHUB.match(line)
        if m:
            return ParsedEvent(
                event_type="device_info",
                data={"field": "github", "value": m.group(1).strip()},
                raw=line,
            )
        if _RE_PRODUCT.match(line):
            return ParsedEvent(
                event_type="device_info",
                data={"field": "product", "value": "ESP32-DIV"},
                raw=line,
            )

        # The banner rule line ("====...") is pure decoration: drop it as noise
        # so it does not clutter the event stream.
        if _RE_RULE.match(line):
            return None

        # --- Free-form error wording not in ESP-IDF format ---
        if _RE_GENERIC_ERROR.search(line):
            return ParsedEvent(
                event_type="error",
                data={"message": line},
                raw=line,
            )

        # --- Anything else non-empty ---
        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    # ── Commands (CATALOG — not sendable; reference/UI only) ──────────

    def get_commands(self) -> list[CommandInfo]:
        """Return DIV's on-device tool catalog (reference, not sendable).

        ESP32-DIV has no serial command interface — these entries document the
        tools reachable via the device's touchscreen/buttons, grouped by
        domain and annotated with danger flags so the UI can warn before a user
        is pointed at a transmit/jam/clone tool. Nothing here is sent to the
        device; see ``format_command`` and the module docstring.

        Danger flags:
            "" safe; "lab-only" RF TX / spoof / clone / brute that must only
            run in an authorized controlled environment; "illegal-tx"
            transmission that is illegal in most jurisdictions (jamming /
            reader-disruption).
        """
        return [
            # ---- WiFi ----
            CommandInfo("packet_monitor", "WiFi", "802.11 packet monitor / channel sniffer"),
            CommandInfo("wifi_scanner", "WiFi", "Scan for nearby WiFi access points"),
            CommandInfo(
                "beacon_spam", "WiFi",
                "Flood the air with fake AP beacon frames",
                danger="lab-only",
            ),
            CommandInfo(
                "deauth", "WiFi",
                "Send 802.11 deauthentication frames to disconnect clients",
                danger="lab-only",
            ),
            CommandInfo("deauth_detector", "WiFi", "Detect deauth attacks in the area"),
            CommandInfo(
                "captive_portal", "WiFi",
                "Host a rogue captive-portal / evil-twin login page",
                danger="lab-only",
            ),
            CommandInfo(
                "probe_flood", "WiFi",
                "Flood probe-request frames",
                danger="lab-only",
            ),
            # ---- BLE ----
            CommandInfo("ble_scanner", "BLE", "Scan for nearby BLE devices"),
            CommandInfo("ble_sniffer", "BLE", "Sniff BLE advertising traffic"),
            CommandInfo(
                "ble_spoofer", "BLE",
                "Advertise as / impersonate another BLE device",
                danger="lab-only",
            ),
            CommandInfo(
                "sour_apple", "BLE",
                "Apple BLE proximity-pairing spam (denial-of-service popups)",
                danger="lab-only",
            ),
            CommandInfo(
                "ble_jammer", "BLE",
                "Jam the 2.4GHz BLE advertising channels",
                danger="illegal-tx",
            ),
            CommandInfo(
                "ble_rubber_ducky", "BLE",
                "BLE HID keystroke-injection (Rubber Ducky)",
                danger="lab-only",
            ),
            # ---- RF24 (2.4GHz / NRF24) ----
            CommandInfo("scanner_2g4", "2.4GHz", "Scan the 2.4GHz band for active channels"),
            CommandInfo(
                "protokill", "2.4GHz",
                "Broadband 2.4GHz protocol jammer (WiFi/BLE/NRF)",
                danger="illegal-tx",
            ),
            # ---- SubGHz (CC1101) ----
            CommandInfo(
                "subghz_replay", "SubGHz",
                "Capture and replay a Sub-GHz RF signal",
                danger="lab-only",
            ),
            CommandInfo(
                "subghz_jammer", "SubGHz",
                "Jam a Sub-GHz frequency",
                danger="illegal-tx",
            ),
            CommandInfo("subghz_profiles", "SubGHz", "Manage saved Sub-GHz frequency/modulation profiles"),
            # ---- IR ----
            CommandInfo("ir_replay", "IR", "Capture and replay an infrared signal"),
            CommandInfo("ir_saved", "IR", "Replay a saved infrared signal"),
            CommandInfo("ir_universal", "IR", "Universal IR remote (brute TV-B-Gone style)"),
            # ---- NFC (PN532) ----
            CommandInfo("card_reader", "NFC", "Read an NFC card / tag UID and data"),
            CommandInfo(
                "card_clone", "NFC",
                "Clone a read NFC card onto a writable tag",
                danger="lab-only",
            ),
            CommandInfo("nfc_dump", "NFC", "Dump full NFC card memory to storage"),
            CommandInfo("decode_access", "NFC", "Decode an access-control card's format/facility data"),
            CommandInfo(
                "nfc_erase", "NFC",
                "Erase / wipe a writable NFC tag",
                danger="lab-only",
            ),
            CommandInfo(
                "jam_reader", "NFC",
                "Jam an NFC reader's field",
                danger="illegal-tx",
            ),
            CommandInfo(
                "tag_disrupt", "NFC",
                "Disrupt NFC tag-to-reader communication",
                danger="illegal-tx",
            ),
            CommandInfo(
                "disrupt_emulate", "NFC",
                "Emulate a tag to disrupt / confuse a reader",
                danger="lab-only",
            ),
            # ---- GPS ----
            CommandInfo("wardriver", "GPS", "Wardrive: log WiFi/BLE with GPS coordinates"),
            CommandInfo("satellite_scanner", "GPS", "Show visible GNSS satellites / fix status"),
            # ---- System ----
            CommandInfo("serial_monitor", "System", "On-device serial console viewer"),
            CommandInfo("sd_file_manager", "System", "Browse / manage files on the SD card"),
            CommandInfo("update_firmware", "System", "Update DIV firmware from SD card"),
            CommandInfo("touch_calibrate", "System", "Calibrate the touchscreen"),
            CommandInfo("settings", "System", "Open the device settings menu"),
        ]

    # ── Formatting (display only — DIV ignores serial input) ──────────

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        """Format a catalog entry for DISPLAY only.

        ESP32-DIV does not accept serial commands (verified on hardware), so
        the returned string is never something the device will act on — it is
        purely a stable, readable rendering of a catalog entry (its name plus
        any args, space-joined). Provided to satisfy the BaseProtocol contract
        and to give the UI a consistent label for reference/catalog use.
        """
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{cmd} {arg_str}".strip()
        return cmd

    # ── Auto-detection ───────────────────────────────────────────────

    def identify(self, line: str) -> bool:
        """Return True if the line looks like ESP32-DIV output.

        Matches the product banner ("ESP32-DIV") and the CiferTech author /
        GitHub markers (case-insensitive on the vendor name) from the boot
        banner.
        """
        return (
            "ESP32-DIV" in line
            or "CiferTech" in line
            or "cifertech" in line
        )
