"""ESP-AT protocol — serial parser for Espressif's AT-command firmware.

ESP-AT turns an ESP32 (WROOM-32 / S2 / S3 / C3 …) into an AT-controlled Wi-Fi/BT
modem. Its serial interface is the classic Hayes-style AT command shell: you send
an ``AT``-prefixed command terminated by CRLF, the firmware may stream one or more
``+<CMD>:<payload>`` response lines, and then reports a terminal status of ``OK``
(success), ``ERROR`` (failure), or ``busy p...`` (still processing). On boot it
prints a bare ``ready`` once the AT interface is up.

Most of the AT command set is passive recon: pings, version/heap queries, AP
scans, Wi-Fi/BLE state queries. Those are SAFE (danger=""). The firmware also
exposes two active-transmit capabilities that CC keeps and labels rather than
hides: bringing up the device's own SoftAP (``AT+CWSAP`` set form, a beaconing
AP that can be used as a rogue/evil-twin) and BLE advertising (``AT+BLEADVDATA``
+ ``AT+BLEADVSTART``, which broadcast attacker-controlled BLE frames). Those
carry danger="lab-only" so the safety layer confirm-gates them. AT requires CRLF,
so ``line_ending`` is overridden to ``"\r\n"``.

Source-verified against Espressif's AT command reference (Basic / Wi-Fi / BLE /
TCP-IP command pages). BLE and SoftAP verbs depend on the chip and the compiled
build (e.g. the ESP32-S2 has no BLE); ``AT+CMD`` self-enumerates what the
attached build actually supports.

Example serial exchange:
    AT+CWMODE?
    +CWMODE:1
    OK
"""

from __future__ import annotations

from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent


class EspAtProtocol(BaseProtocol):
    """Parser and command formatter for Espressif ESP-AT firmware's AT serial shell."""

    # AT requires a CRLF terminator after each command — LF alone is ignored by the AT parser.
    line_ending = "\r\n"

    # A Wi-Fi/BT modem: Wi-Fi (STA/AP) plus BLE/BT. Mostly passive recon, with a couple of
    # labeled active-TX verbs (SoftAP set form, BLE advertising).
    capabilities = frozenset({"wifi", "ble", "bt"})

    @property
    def protocol_name(self) -> str:
        return "esp_at"

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None

        # Boot-complete banner: ESP-AT prints a bare "ready" once the AT interface is up.
        if line == "ready":
            return ParsedEvent(event_type="status", data={"ok": True, "ready": True}, raw=line)

        # Terminal SUCCESS status.
        if line == "OK":
            return ParsedEvent(event_type="status", data={"ok": True}, raw=line)

        # Terminal FAILURE status: "ERROR" or the "busy p..." still-processing reply.
        if line == "ERROR" or line.lower().startswith("busy"):
            return ParsedEvent(event_type="status", data={"ok": False, "message": line}, raw=line)

        # Structured "+<CMD>:<payload>" response lines (e.g. +CWLAP, +CIFSR, +CWMODE, +GMR fields).
        if line.startswith("+"):
            head, sep, rest = line[1:].partition(":")
            data: dict[str, object] = {"response": head.strip()}
            if sep:
                data["value"] = rest.strip()
            return ParsedEvent(event_type="info", data=data, raw=line)

        # Command echo + any other non-empty line → plain info (never a fabricated event).
        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    # ── Commands ─────────────────────────────────────────────────────

    def get_commands(self) -> list[CommandInfo]:
        """ESP-AT command set, verified against Espressif's AT command reference.

        Each ``name`` is the literal wire command the AT parser accepts (the interactive
        command palette sends it verbatim), so friendly intent lives in the description.
        Most verbs are passive queries (danger=""); the two active-transmit forms
        (SoftAP set, BLE advertising) carry danger="lab-only" so safety confirm-gates them.
        """
        return [
            # ---- System / Basic ----
            CommandInfo("AT", "System", "Ping the AT interface (expect OK)"),
            CommandInfo("AT+GMR", "System", "Show AT / SDK / compile version banner"),
            CommandInfo("AT+CMD", "System", "List every AT command the current firmware build supports"),
            CommandInfo("AT+RST", "System", "Restart / reboot the module"),
            CommandInfo("AT+SYSRAM", "System", "Query free and minimum-ever heap memory"),
            # ---- Wi-Fi ----
            CommandInfo("AT+CWLAP", "Wi-Fi", "List/scan nearby Wi-Fi access points"),
            CommandInfo("AT+CWLAPOPT", "Wi-Fi", "Configure AT+CWLAP output (columns, RSSI filter, sort)"),
            CommandInfo("AT+CWMODE?", "Wi-Fi", "Query current Wi-Fi mode (STA / AP / STA+AP)"),
            CommandInfo("AT+CWSTATE", "Wi-Fi", "Query Wi-Fi connection state and info"),
            CommandInfo("AT+CWJAP?", "Wi-Fi", "Query the associated AP (SSID/BSSID/channel/RSSI)"),
            CommandInfo("AT+CIFSR", "Wi-Fi", "Show the assigned local IP and MAC address"),
            CommandInfo("AT+CWLIF", "Wi-Fi", "List stations (IP + MAC) joined to the device's own SoftAP"),
            CommandInfo("AT+CWSAP?", "Wi-Fi", "Query the device's SoftAP config (SSID/channel/encryption)"),
            # SET form stands up a beaconing SoftAP (usable as a rogue / evil-twin AP) — active TX.
            CommandInfo("AT+CWSAP", "Wi-Fi", "Configure and start the device's SoftAP (beaconing AP)",
                        danger="lab-only"),
            # ---- BLE ----
            # BLE requires an init first, and only exists on BLE-capable chips/builds (AT+CMD confirms).
            CommandInfo("AT+BLEINIT", "BLE", "Initialize the BLE stack (required before BLE scan/advertise)"),
            CommandInfo("AT+BLESCAN", "BLE", "Passive BLE device scan"),
            # These two broadcast attacker-controlled BLE advertising frames — active TX.
            CommandInfo("AT+BLEADVDATA", "BLE", "Set the BLE advertising payload (up to 31 bytes)",
                        danger="lab-only"),
            CommandInfo("AT+BLEADVSTART", "BLE", "Start BLE advertising / broadcasting",
                        danger="lab-only"),
            # ---- Network (TCP-IP) ----
            CommandInfo("AT+PING", "Network", "Ping a remote host and report round-trip latency"),
            CommandInfo("AT+CIPDOMAIN", "Network", "Resolve a domain name to an IP via DNS"),
            CommandInfo("AT+CIPSTATE", "Network", "Show active TCP/UDP/SSL connection info"),
        ]

    # ── Formatting ───────────────────────────────────────────────────

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        """Format a command for ESP-AT serial transmission.

        The command name is already the literal wire string, so it is returned verbatim.
        If args are supplied they are appended in AT set-command syntax
        (``format_command("AT+CWMODE", {"mode": "1"}) -> "AT+CWMODE=1"``); empty values
        are ignored so the bare command is sent.
        """
        if args:
            vals = [str(v).strip() for v in args.values() if str(v).strip()]
            if vals:
                return f"{cmd}=" + ",".join(vals)
        return cmd

    # ── Auto-detection ───────────────────────────────────────────────

    def identify(self, line: str) -> bool:
        """Return True if the line looks like ESP-AT output.

        Claims the unambiguous AT+GMR version banner and the boot-complete "ready"
        line. A bare stripped "OK" is deliberately NOT claimed on its own — too
        generic — so auto-detect doesn't steal another firmware's status line.
        (The serial layer splits on ``[\\r\\n]+`` and strips before this sees a line,
        so there is no multi-line CRLF-framed "OK" form to match here.)
        """
        if not line:
            return False
        banners = ("AT version:", "SDK version:", "Bin version:", "compile time")
        if any(b in line for b in banners):
            return True
        return line.strip() == "ready"
