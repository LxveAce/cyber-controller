"""Protocol registry — maps firmware names to their serial parsers.

This package exposes one BaseProtocol subclass per supported firmware plus a
small registry layer so the rest of cyber-controller can look protocols up by
internal name or by human-friendly display name.

Public API:
    PROTOCOLS              -- dict[name -> protocol class]
    PROTOCOL_DISPLAY_NAMES -- dict[name -> display string]
    get_protocol(name)             -> BaseProtocol instance
    get_protocol_by_display(disp)  -> BaseProtocol instance
    list_protocols()               -> list[str] of internal names

A 'generic' / 'raw' passthrough is always available as a fallback for unknown
or unspecified firmware: it never tries to interpret the line, emitting every
non-empty line as an 'info' event.
"""

from __future__ import annotations

from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent
from src.protocols.marauder import MarauderProtocol
from src.protocols.ghost_esp import GhostESPProtocol
from src.protocols.bruce import BruceProtocol
from src.protocols.flipper import FlipperProtocol
from src.protocols.halehound import HaleHoundProtocol
from src.protocols.meshtastic import MeshtasticProtocol


class GenericProtocol(BaseProtocol):
    """Passthrough fallback protocol.

    Performs no firmware-specific parsing: every non-empty line is surfaced
    as an 'info' event with its raw text. Used when the firmware is unknown
    or when the caller explicitly wants raw serial output.
    """

    @property
    def protocol_name(self) -> str:
        return "generic"

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None
        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    def get_commands(self) -> list[CommandInfo]:
        return []

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{cmd} {arg_str}"
        return cmd

    def identify(self, line: str) -> bool:
        # The generic protocol never claims a line during auto-detection;
        # it is only used as an explicit fallback.
        return False


# --- Registry: internal name -> protocol class ---

PROTOCOLS: dict[str, type[BaseProtocol]] = {
    "marauder": MarauderProtocol,
    "ghost-esp": GhostESPProtocol,
    "bruce": BruceProtocol,
    "flipper": FlipperProtocol,
    "halehound": HaleHoundProtocol,
    "meshtastic": MeshtasticProtocol,
    # Fallbacks (both names map to the same passthrough class).
    "generic": GenericProtocol,
    "raw": GenericProtocol,
}

# --- Human-friendly display names ---

PROTOCOL_DISPLAY_NAMES: dict[str, str] = {
    "marauder": "ESP32 Marauder",
    "ghost-esp": "Ghost ESP",
    "bruce": "Bruce",
    "flipper": "Flipper Zero",
    "halehound": "HaleHound",
    "meshtastic": "Meshtastic",
    "generic": "Generic / Raw",
    "raw": "Generic / Raw",
}

# Reverse lookup: display string -> internal name (case-insensitive).
_DISPLAY_TO_NAME: dict[str, str] = {
    disp.lower(): name for name, disp in PROTOCOL_DISPLAY_NAMES.items()
}


def get_protocol(name: str) -> BaseProtocol:
    """Return a protocol instance for the given internal name.

    Unknown names fall back to the generic passthrough protocol. Lookup is
    case-insensitive and tolerant of underscores vs. hyphens (e.g. both
    'ghost_esp' and 'ghost-esp' resolve to GhostESP).
    """
    if not name:
        return GenericProtocol()
    key = name.strip().lower()
    cls = PROTOCOLS.get(key)
    if cls is None:
        # Normalise underscores to hyphens for convenience (ghost_esp).
        cls = PROTOCOLS.get(key.replace("_", "-"))
    if cls is None:
        return GenericProtocol()
    return cls()


def get_protocol_by_display(display: str) -> BaseProtocol:
    """Return a protocol instance for the given display name.

    Unknown display names fall back to the generic passthrough protocol.
    Lookup is case-insensitive.
    """
    if not display:
        return GenericProtocol()
    name = _DISPLAY_TO_NAME.get(display.strip().lower())
    if name is None:
        return GenericProtocol()
    return get_protocol(name)


def list_protocols() -> list[str]:
    """Return the list of registered internal protocol names."""
    return list(PROTOCOLS.keys())


__all__ = [
    "BaseProtocol",
    "CommandInfo",
    "ParsedEvent",
    "MarauderProtocol",
    "GhostESPProtocol",
    "BruceProtocol",
    "FlipperProtocol",
    "HaleHoundProtocol",
    "MeshtasticProtocol",
    "GenericProtocol",
    "PROTOCOLS",
    "PROTOCOL_DISPLAY_NAMES",
    "get_protocol",
    "get_protocol_by_display",
    "list_protocols",
]
