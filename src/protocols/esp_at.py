"""ESP-AT protocol — serial parser for Espressif's AT-command firmware.

ESP-AT turns an ESP32 (WROOM-32 / S2 / S3 / C3 …) into an AT-controlled Wi-Fi/BT
modem. Its serial interface is the classic Hayes-style AT command shell: you send
an ``AT``-prefixed command terminated by CRLF, the firmware may stream one or more
``+<CMD>:<payload>`` response lines, and then reports a terminal status of ``OK``
(success), ``ERROR`` (failure), or ``busy p...`` (still processing). On boot it
prints a bare ``ready`` once the AT interface is up.

This is a modem firmware — there is NO offensive/RF-attack transmit surface — so
every command this parser offers is a SAFE, read-only query (danger=""). AT
requires CRLF, so ``line_ending`` is overridden to ``"\r\n"``.

Source-verified serial commands (AT command set): ``AT`` (ping), ``AT+GMR``
(version), ``AT+CWLAP`` (list APs), ``AT+CWMODE?`` (query Wi-Fi mode),
``AT+CIFSR`` (local IP/MAC).

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

    # A Wi-Fi/BT modem: it does Wi-Fi (STA/AP) and BLE/BT, but no offensive RF.
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
        """ESP-AT read-only AT helper set (all SAFE — no transmit/attack surface).

        Each ``name`` is the literal wire command the AT parser accepts (the interactive
        command palette sends it verbatim), so friendly intent lives in the description.
        """
        return [
            # ---- System ----
            CommandInfo("AT", "System", "Ping the AT interface (expect OK)"),
            CommandInfo("AT+GMR", "System", "Show AT / SDK / compile version banner"),
            # ---- Wi-Fi ----
            CommandInfo("AT+CWLAP", "Wi-Fi", "List/scan nearby Wi-Fi access points"),
            CommandInfo("AT+CWMODE?", "Wi-Fi", "Query current Wi-Fi mode (STA / AP / STA+AP)"),
            CommandInfo("AT+CIFSR", "Wi-Fi", "Show the assigned local IP and MAC address"),
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
