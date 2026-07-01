"""Bruce protocol — serial parser for Bruce multi-tool firmware.

Bruce is a multi-tool firmware for ESP32 boards (CYD, Cardputer, M5Stack, …).
Its serial interface is a Flipper-CLI-style shell: it echoes the command it is
running, prints any output, then reports a pass/fail result and re-draws the
prompt. The radio/menu features (WiFi / BLE / NFC scanning and attacks) are
driven from the on-device UI, the loader, or the JS interpreter — they are NOT
exposed as named serial commands. So this parser surfaces shell status/info
events only and never fabricates target discoveries.

Source-verified serial commands: info, free, uptime, reboot, ir rx, ir tx,
subghz rx, subghz tx, subghz tx_from_file, badusb run_from_file, loader open.

Example serial exchange:
    COMMAND: info
    Bruce v1.x | board: ...
    [CLI] Result: TRUE
    #
"""

from __future__ import annotations

import re

from src.models.action import ActionCategory, TargetAction
from src.models.target import TargetType
from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# --- Regex patterns for the Bruce serial CLI shell ---

# "COMMAND: <x>" — the shell echoes the command it is about to run.
_RE_COMMAND = re.compile(r"^COMMAND:\s*(.+)$")

# "[CLI] Result: TRUE" / "[CLI] Result: FALSE" — pass/fail of the last command.
_RE_CLI_RESULT = re.compile(r"\[CLI\]\s*Result:\s*(TRUE|FALSE)\b", re.IGNORECASE)


class BruceProtocol(BaseProtocol):
    """Parser and command formatter for Bruce firmware's serial CLI."""

    @property
    def protocol_name(self) -> str:
        return "bruce"

    capabilities = frozenset({"badusb", "ble", "ir", "nfc", "rfid", "subghz", "wifi"})

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None

        # Command echo: "COMMAND: <x>".
        m = _RE_COMMAND.match(line)
        if m:
            return ParsedEvent(
                event_type="command",
                data={"command": m.group(1).strip()},
                raw=line,
            )

        # Pass/fail result line: "[CLI] Result: TRUE" / "[CLI] Result: FALSE".
        m = _RE_CLI_RESULT.search(line)
        if m:
            result = m.group(1).upper()
            return ParsedEvent(
                event_type="status",
                data={"success": result == "TRUE", "result": result},
                raw=line,
            )

        # Shell prompt "# " (stripped to "#") — a benign heartbeat, surfaced as status.
        if line == "#":
            return ParsedEvent(event_type="status", data={"prompt": True}, raw=line)

        # Unrecognised but non-empty — plain info, never a fabricated target event.
        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    # ── Commands ─────────────────────────────────────────────────────

    def get_commands(self) -> list[CommandInfo]:
        """Bruce serial-CLI command set (source-verified).

        Only the commands the Bruce serial shell actually accepts are listed.
        The former WiFi/BLE/NFC entries were fabricated — there is no such
        serial command (those features live in the on-device menu / loader /
        JS interpreter) — so they are removed rather than shipped as broken
        buttons.
        """
        return [
            # ---- System ----
            CommandInfo("info", "System", "Show device / firmware info"),
            CommandInfo("free", "System", "Show free heap memory"),
            CommandInfo("uptime", "System", "Show device uptime"),
            CommandInfo("reboot", "System", "Reboot device"),
            # ---- IR ----
            CommandInfo("ir rx", "IR", "Receive an IR signal"),
            CommandInfo("ir tx", "IR", "Transmit an IR signal"),
            # ---- SubGHz ----
            CommandInfo("subghz rx", "SubGHz", "Receive SubGHz signals"),
            CommandInfo("subghz tx", "SubGHz", "Transmit a SubGHz signal", danger="lab-only"),
            CommandInfo("subghz tx_from_file", "SubGHz", "Replay a saved SubGHz capture",
                        danger="lab-only"),
            # ---- BadUSB ----
            CommandInfo("badusb run_from_file <script>", "BadUSB", "Run a BadUSB/Ducky script file", "script"),
            # ---- Apps ----
            CommandInfo("loader open <app>", "Apps", "Open an app / module by name", "app"),
        ]

    # ── Formatting ───────────────────────────────────────────────────

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        """Format a command for Bruce serial transmission."""
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{cmd} {arg_str}"
        return cmd

    # ── Auto-detection ───────────────────────────────────────────────

    def identify(self, line: str) -> bool:
        """Return True if line looks like Bruce serial-CLI output."""
        markers = ("[CLI] Result:", "Bruce")
        return any(m in line for m in markers)


# --- Target actions: what this protocol can do to each target type ---
# WiFi/BLE/NFC target actions were removed: Bruce exposes no serial command for
# them (deauth / beacon / ble-spam / nfc are on-device-menu / JS-only, not CLI).

TARGET_ACTIONS: dict[TargetType, list[TargetAction]] = {
    TargetType.SUBGHZ: [
        TargetAction("SubGHz Replay", "subghz tx_from_file", "Replay captured SubGHz signal", ActionCategory.ATTACK),
        TargetAction("SubGHz Scan", "subghz rx", "Scan for SubGHz transmissions", ActionCategory.SCAN),
    ],
}


# --- Unified Action Broadcast capability map (verb -> (pre_commands, command)).
# Commands are each firmware's NATIVE realization; absent verb == device skipped.
# WiFi/BLE/STOP verbs are absent: those commands don't exist on the Bruce serial CLI. ---
from src.core.broadcast import BroadcastVerb  # noqa: E402  (bottom import avoids a cycle)

BROADCAST_CAPABILITIES = {
    BroadcastVerb.SUBGHZ_SCAN: ((), "subghz rx"),
}
