"""Meshtastic protocol — minimal best-effort serial parser.

NOTE: Meshtastic's real device serial API is NOT a plain-text line protocol.
On the wire it frames protobuf-encoded packets (the `meshtastic` Python
library / protobufs handle ToRadio/FromRadio messages over the serial stream).
A line-oriented text parser therefore cannot fully decode a live Meshtastic
link. This module is intentionally a BEST-EFFORT text parser only: it scrapes
human-readable log/debug lines that Meshtastic firmware also prints to the
serial console, and emits mostly 'info' events. For real telemetry, a future
backend should speak the protobuf framing via the meshtastic library rather
than relying on this parser.

Example human-readable lines this scrapes (best effort):
    Node: !a1b2c3d4 | Name: BaseCamp | SNR: 9.5 | Battery: 92%
    Position: !a1b2c3d4 | Lat: 37.7749 | Lon: -122.4194
    Message from !a1b2c3d4: hello mesh
"""

from __future__ import annotations

import re

from src.models.action import ActionCategory, TargetAction
from src.models.target import TargetType
from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# --- Regex patterns for best-effort Meshtastic text scraping ---

# Node: !id | Name: ... | SNR: ... | Battery: ...
# Name runs up to the next '|' or end-of-line so optional SNR/Battery fields
# don't truncate it.
_RE_NODE = re.compile(
    r"Node:\s*(!?[0-9A-Fa-f]+)\s*\|\s*Name:\s*([^|]+?)\s*(?:\|\s*SNR:\s*(-?[\d.]+))?"
    r"(?:\s*\|\s*Battery:\s*(\d+%))?\s*$",
    re.IGNORECASE,
)

# Position: !id | Lat: ... | Lon: ...
_RE_POSITION = re.compile(
    r"Position:\s*(!?[0-9A-Fa-f]+)\s*\|\s*Lat:\s*([\d.\-]+)\s*\|\s*Lon:\s*([\d.\-]+)",
    re.IGNORECASE,
)

# Message from !id: text
_RE_MESSAGE = re.compile(
    r"Message\s+from\s+(!?[0-9A-Fa-f]+):\s*(.+)",
    re.IGNORECASE,
)


class MeshtasticProtocol(BaseProtocol):
    """Best-effort text parser and command formatter for Meshtastic.

    See the module docstring: real Meshtastic serial traffic is protobuf, so
    this parser only handles human-readable log lines.
    """

    @property
    def protocol_name(self) -> str:
        return "meshtastic"

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None

        # Node announcement / nodedb entry
        m = _RE_NODE.search(line)
        if m:
            data: dict = {"node_id": m.group(1), "name": m.group(2).strip()}
            if m.group(3) is not None:
                data["snr"] = float(m.group(3))
            if m.group(4) is not None:
                data["battery"] = m.group(4)
            return ParsedEvent(event_type="info", data=data, raw=line)

        # Position report
        m = _RE_POSITION.search(line)
        if m:
            return ParsedEvent(
                event_type="info",
                data={
                    "node_id": m.group(1),
                    "lat": float(m.group(2)),
                    "lon": float(m.group(3)),
                },
                raw=line,
            )

        # Text message
        m = _RE_MESSAGE.search(line)
        if m:
            return ParsedEvent(
                event_type="info",
                data={"from": m.group(1), "message": m.group(2).strip()},
                raw=line,
            )

        # Everything else (incl. protobuf binary noise that survived decoding):
        # surface as a generic info event.
        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    # ── Commands ─────────────────────────────────────────────────────

    def get_commands(self) -> list[CommandInfo]:
        """Minimal Meshtastic command set.

        These mirror common `meshtastic` CLI flags but are emitted as simple
        text tokens here; a protobuf-aware backend would translate them.
        """
        return [
            CommandInfo("info", "System", "Show device / radio info"),
            CommandInfo("nodes", "Mesh", "List known nodes in the mesh"),
            CommandInfo("send <text>", "Mesh", "Send a text message to the mesh", "text"),
            CommandInfo("reboot", "System", "Reboot device"),
        ]

    # ── Formatting ───────────────────────────────────────────────────

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        """Format a command for transmission (best-effort text form)."""
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{cmd} {arg_str}"
        return cmd

    # ── Auto-detection ───────────────────────────────────────────────

    def identify(self, line: str) -> bool:
        """Return True if line looks like Meshtastic output (best effort)."""
        markers = ("Meshtastic", "meshtastic", "Node: !", "Position: !", "ToRadio", "FromRadio")
        return any(m in line for m in markers)


# --- Target actions: what this protocol can do to each target type ---

TARGET_ACTIONS: dict[TargetType, list[TargetAction]] = {
    TargetType.AP: [
        TargetAction("Mesh Relay", "relay {mac}", "Relay target info across mesh network", ActionCategory.UTILITY),
    ],
}


# --- Unified Action Broadcast capability map (verb -> (pre_commands, command)).
# Commands are each firmware's NATIVE realization; absent verb == device skipped. ---
from src.core.broadcast import BroadcastVerb  # noqa: E402  (bottom import avoids a cycle)

BROADCAST_CAPABILITIES = {
    BroadcastVerb.MESH_RELAY: ((), "nodes"),
}
