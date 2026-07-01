"""Meshtastic protocol — raw serial-log viewer only (NO text command channel).

SOURCE-VERIFIED: Meshtastic's device serial link is PROTOBUF-framed — the
StreamAPI sends length-delimited ToRadio/FromRadio protobuf packets handled by
the `meshtastic` Python library. It is NOT a plain-text line CLI. Plain-text
commands written to the port (info, nodes, send <text>, reboot, ...) are simply
DISCARDED by the firmware; they do nothing. Real control therefore requires
speaking the protobuf StreamAPI (or shelling out to the `meshtastic` CLI), which
Cyber-Controller does not yet implement.

What CC honestly supports for Meshtastic today:
  * Flashing (handled by the flash/profile layer — works).
  * Viewing the RAW serial log — the firmware also prints human-readable
    boot/debug text to the console, which this parser surfaces verbatim as
    generic 'info' events.

What this module deliberately does NOT pretend to do:
  * Send commands — there is no text command channel, so get_commands() is
    intentionally EMPTY (no fake buttons that silently do nothing).
  * Decode structured telemetry (nodes / positions / messages) — that data
    arrives as protobuf frames this text parser cannot decode, so it makes NO
    structured Node/Position/Message claims.

A future backend should speak the protobuf framing via the meshtastic library
rather than relying on this passive text scrape.
"""

from __future__ import annotations

from src.models.action import TargetAction
from src.models.target import TargetType
from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent


class MeshtasticProtocol(BaseProtocol):
    """Raw serial-log viewer for Meshtastic — no text command channel.

    See the module docstring: the real Meshtastic serial link is protobuf
    (StreamAPI), so CC cannot send working text commands and cannot decode
    structured telemetry over this text path. ``parse_line`` only scrapes the
    human-readable boot/debug text the firmware also prints; ``get_commands``
    is empty (no fake control buttons).
    """

    @property
    def protocol_name(self) -> str:
        return "meshtastic"

    capabilities = frozenset({"lora", "mesh"})

    # Protobuf StreamAPI, not a text shell — plain-text writes are discarded, so there is no serial command
    # channel (see the module docstring). Marks the node honestly as "stream" rather than an empty text-CLI.
    driver_type = "stream"

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        """Passive, honest scrape of the human-readable serial log.

        Meshtastic's structured data is protobuf-framed and is NOT decoded
        here, so this never emits structured Node/Position/Message events. Any
        non-empty human-readable line is surfaced verbatim as a generic 'info'
        event; blank lines are noise (None).
        """
        line = line.strip()
        if not line:
            return None
        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    # ── Commands ─────────────────────────────────────────────────────

    def get_commands(self) -> list[CommandInfo]:
        """No sendable commands.

        Meshtastic's serial link is protobuf (StreamAPI); plain-text commands
        are discarded by the firmware. Rather than ship buttons that silently
        do nothing, this returns an empty list. Real control would require a
        protobuf-aware backend or the external `meshtastic` CLI.
        """
        return []

    # ── Formatting ───────────────────────────────────────────────────

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        """Format a command string (BaseProtocol interface requirement).

        There are no real commands to send (see get_commands); any text written
        to the port is discarded by the protobuf firmware. Retained only so the
        abstract interface is satisfied.
        """
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{cmd} {arg_str}"
        return cmd

    # ── Auto-detection ───────────────────────────────────────────────

    def identify(self, line: str) -> bool:
        """Return True if the line looks like Meshtastic output (best effort)."""
        markers = ("Meshtastic", "meshtastic", "ToRadio", "FromRadio")
        return any(m in line for m in markers)


# --- Target actions: what this protocol can do to each target type ---

TARGET_ACTIONS: dict[TargetType, list[TargetAction]] = {
    # Intentionally empty. Meshtastic's serial link is protobuf-framed
    # (ToRadio/FromRadio), so a plain-text target action like the prior phantom
    # "relay {mac}" never executes — removed rather than shipped broken. A
    # protobuf-aware backend (or the `meshtastic` CLI bridge) is the real fix.
}


# --- Unified Action Broadcast capability map (verb -> (pre_commands, command)).
# Intentionally EMPTY. Every Meshtastic broadcast verb would have to be realized
# as a plain-text serial command (e.g. the old MESH_RELAY -> "nodes"), but the
# firmware discards text on the serial link — it speaks protobuf/StreamAPI. A
# broadcast "Mesh Status" button wired to "nodes" would write bytes the firmware
# ignores: a phantom. So Meshtastic advertises NO broadcast capability until a
# protobuf-aware backend exists.
BROADCAST_CAPABILITIES: dict = {}
