"""LxveOS protocol — serial parser for the LxveOS headless control surface.

LxveOS (``LxveAce/lxveos``) is an ESP-IDF security-panel OS. Its esp_console CLI is the headless
control surface on every board, and its ``status`` command emits ONE versioned, machine-readable
line the Cyber Controller host parses to auto-identify the unit — the seed of the M1 CC bridge:

    LXVEOS/1 status board=bare_esp32_headless chip=esp32 ui=headless fw=0.1.0-m0 panel=none \
        caps=0x000 ops=0/3/9 heap=298374

plus a human-oriented ``info`` command that prints four fixed lines:

    fw    : LxveOS 0.1.0-m0
    board : bare_esp32_headless
    chip  : esp32
    ui    : headless

Both formats are taken verbatim from the LxveOS source
(``components/lxveos_cli/src/lxveos_cli.c``: ``cmd_info``/``cmd_status``) and confirmed against a
live board. This parser is PASSIVE — LxveOS is a text CLI with no attack/TX commands, and both the
``info`` and ``status`` outputs are gated behind LxveOS's own first-run authorized-use ack.

The status line keys on FIELD NAMES, not position: LxveOS may append new keys (the firmware comment
notes older hosts must ignore unknown fields), so we parse every ``key=value`` and only type the
known ones — an appended field lands in the event data as a string rather than being dropped.
"""

from __future__ import annotations

import re

from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# One-line CC bridge: `LXVEOS/<v> status <space-separated key=value>`. Values are safe slugs / hex /
# decimal with no embedded spaces, so a global key=value scan is exact.
_RE_STATUS = re.compile(r"^LXVEOS/(\d+)\s+status\s+(.*)$")
_RE_KV = re.compile(r"(\w+)=(\S+)")

# `info` command lines: `key<pad>: value` (the firmware left-pads the key to a fixed width).
_RE_INFO_FW = re.compile(r"^fw\s*:\s*LxveOS\s+(\S+)\s*$")
_RE_INFO_BOARD = re.compile(r"^board\s*:\s*(\S+)\s*$")
_RE_INFO_CHIP = re.compile(r"^chip\s*:\s*(\S+)\s*$")
_RE_INFO_UI = re.compile(r"^ui\s*:\s*(\S+)\s*$")

# The linenoise REPL prompt.
_RE_PROMPT = re.compile(r"^lxveos>\s*$")


def _coerce_status_field(key: str, val: str):
    """Type the known ``status`` fields; leave any unknown (future) key as a raw string."""
    if key == "caps":  # hex capability bitmask, e.g. 0x007
        try:
            return int(val, 16)
        except ValueError:
            return val
    if key == "heap":  # free-heap bytes (decimal)
        try:
            return int(val)
        except ValueError:
            return val
    if key == "ops":  # ready/planned/unavailable operation tally
        parts = val.split("/")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            return {"ready": int(parts[0]), "planned": int(parts[1]), "unavailable": int(parts[2])}
        return val
    return val


class LxveOSProtocol(BaseProtocol):
    """Parser + command formatter for LxveOS's headless esp_console surface."""

    # LxveOS reports its real capabilities at RUNTIME via the status line's `caps=` bitmask; the M0
    # headless build is an identity/control surface, so no static capability tokens are claimed here
    # (declaring e.g. "wifi" would be a guess). Consumers read caps from the parsed device_info.
    capabilities: "frozenset[str]" = frozenset()
    driver_type = "text-cli"

    def __init__(self) -> None:
        # `info` prints four separate lines with no terminator line, so accumulate them across
        # parse_line calls and emit one device_info on the closing `ui :` line (the last field).
        self._info_record: dict = {}

    @property
    def protocol_name(self) -> str:
        return "lxveos"

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None

        # The self-contained CC bridge line (primary target).
        m = _RE_STATUS.match(line)
        if m:
            data = {"proto_version": int(m.group(1)), "source": "status_line"}
            for key, val in _RE_KV.findall(m.group(2)):
                data[key] = _coerce_status_field(key, val)
            return ParsedEvent(event_type="device_info", data=data, raw=line)

        # Multi-line `info` output. Fields arrive on separate lines; accumulate and emit one
        # device_info on the closing `ui :` line. A stray board/chip/ui with no in-progress record
        # is ignored (returns a benign info below) rather than emitting a half-built identity.
        m = _RE_INFO_FW.match(line)
        if m:
            self._info_record = {"fw": m.group(1), "source": "info_cmd"}
            return None
        m = _RE_INFO_BOARD.match(line)
        if m:
            if self._info_record:
                self._info_record["board"] = m.group(1)
                return None
        m = _RE_INFO_CHIP.match(line)
        if m:
            if self._info_record:
                self._info_record["chip"] = m.group(1)
                return None
        m = _RE_INFO_UI.match(line)
        if m:
            if self._info_record:
                rec, self._info_record = self._info_record, {}
                rec["ui"] = m.group(1)
                return ParsedEvent(event_type="device_info", data=rec, raw=line)

        # The REPL prompt — a readiness signal, not noise.
        if _RE_PROMPT.match(line):
            return ParsedEvent(event_type="status", data={"prompt": True}, raw=line)

        # Anything else (boot log, help text, ack-gate messages) — surfaced as benign info.
        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    # ── Commands ─────────────────────────────────────────────────────

    def get_commands(self) -> list[CommandInfo]:
        """LxveOS M0 esp_console command set (all passive/local — no RF/TX)."""
        return [
            CommandInfo("help", "System", "List all registered commands"),
            CommandInfo("agree", "System", "Acknowledge the first-run authorized-use gate"),
            CommandInfo("info", "System", "Human-readable fw/board/chip/ui summary"),
            CommandInfo("status", "System", "One machine-readable status line (CC bridge format)"),
            CommandInfo("caps", "System", "List the active capability registry"),
            CommandInfo("sysinfo", "System", "Chip/reset-reason/heap system details"),
            CommandInfo("loglevel", "System", "Set ESP-IDF log verbosity", args="<tag|*> <level>"),
            CommandInfo("nvs", "System", "Operator key/value store", args="get|set <key> [value]"),
            CommandInfo("reboot", "System", "Reboot the device"),
        ]

    # ── Formatting ───────────────────────────────────────────────────

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{cmd} {arg_str}"
        return cmd

    # ── Auto-detection ───────────────────────────────────────────────

    def identify(self, line: str) -> bool:
        """Return True if the line looks like LxveOS output."""
        return (
            line.startswith("LXVEOS/")
            or "LxveOS" in line
            or bool(_RE_PROMPT.match(line.strip()))
        )
