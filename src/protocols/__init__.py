"""Protocol registry — maps firmware names to their serial parsers.

This package exposes one BaseProtocol subclass per supported firmware plus a
small registry layer so the rest of cyber-controller can look protocols up by
internal name or by human-friendly display name.

Public API:
    PROTOCOLS              -- dict[name -> protocol class]
    PROTOCOL_DISPLAY_NAMES -- dict[name -> display string]
    get_protocol(name)             -> BaseProtocol instance
    get_protocol_by_display(disp)  -> BaseProtocol instance
    get_protocol_module(name)      -> module object (for TARGET_ACTIONS access)
    list_protocols()               -> list[str] of internal names

A 'generic' / 'raw' passthrough is always available as a fallback for unknown
or unspecified firmware: it never tries to interpret the line, emitting every
non-empty line as an 'info' event.
"""

from __future__ import annotations

import importlib
import types

from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent
from src.protocols.bluejammer import BlueJammerProtocol
from src.protocols.bruce import BruceProtocol
from src.protocols.bw16 import BW16Protocol
from src.protocols.esp32_div import Esp32DivProtocol
from src.protocols.flipper import FlipperProtocol
from src.protocols.flock_you import FlockYouProtocol
from src.protocols.ghost_esp import GhostESPProtocol
from src.protocols.halehound import HaleHoundProtocol
from src.protocols.marauder import MarauderProtocol
from src.protocols.meshtastic import MeshtasticProtocol
from src.protocols.nrf_bluenullifier import NrfBlueNullifier2Protocol


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
    "esp32-div": Esp32DivProtocol,
    "bw16": BW16Protocol,
    "bluejammer": BlueJammerProtocol,
    "nrf-bluenullifier2": NrfBlueNullifier2Protocol,
    "flock-you": FlockYouProtocol,
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
    "esp32-div": "ESP32-DIV",
    "bw16": "BW16 (RTL8720DN)",
    "bluejammer": "BlueJammer-V2 (lab-only)",
    "nrf-bluenullifier2": "nRF BlueNullifier 2 (lab-only)",
    "flock-you": "Flock-You (ALPR detector)",
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
        # Also tolerate a MISSING separator: device_detect emits 'ghostesp' while the registry key is
        # 'ghost-esp' (and 'esp32div' vs 'esp32-div'). Match by comparing separator-stripped forms so a
        # detected device resolves to its real protocol instead of silently falling back to Generic.
        squashed = key.replace("_", "").replace("-", "")
        cls = next((c for n, c in PROTOCOLS.items() if n.replace("-", "") == squashed), None)
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


def resolve_protocol_name(name: str) -> str | None:
    """Resolve a loose/detected firmware string to a REAL registered protocol name, or ``None``.

    Unlike :func:`get_protocol` (which always returns *something*, falling back to the generic passthrough),
    this returns ``None`` when *name* doesn't identify a known firmware — so a caller can tell "a real
    firmware was detected" from "unknown, use my default heuristic". Matching is case-insensitive and
    tolerant of separator drift (``ghost_esp`` / ``ghostesp`` / ``Ghost ESP`` all resolve to ``ghost-esp``).
    The generic/raw passthrough entries never count as a real match.
    """
    if not name:
        return None
    key = name.strip().lower()
    for candidate in (key, key.replace("_", "-")):
        cls = PROTOCOLS.get(candidate)
        if cls is not None and cls is not GenericProtocol:
            return candidate
    # Tolerate a missing/space separator: 'ghostesp' / 'Ghost ESP' vs the registry key 'ghost-esp'.
    squashed = key.replace("_", "").replace("-", "").replace(" ", "")
    for reg_name, cls in PROTOCOLS.items():
        if cls is not GenericProtocol and reg_name.replace("-", "") == squashed:
            return reg_name
    return None


def list_protocols() -> list[str]:
    """Return the list of registered internal protocol names."""
    return list(PROTOCOLS.keys())


def capabilities_for(name: str) -> "frozenset[str]":
    """Capability tokens a firmware/board supports (wifi/ble/subghz/nfc/ir/gps/lora/...). Used to surface each
    connected device as a node in the network (capability view, Broadcast/AutoRouter applicability). Returns an
    empty set for unknown names or firmwares that declare none."""
    try:
        return frozenset(getattr(get_protocol(name), "capabilities", frozenset()))
    except Exception:  # noqa: BLE001
        return frozenset()


def driver_type_for(name: str) -> str:
    """The transport/driver kind CC uses to talk to a firmware: "text-cli" (a line-based command shell —
    the default), "stream" (a binary/framed link with no text command channel, e.g. Meshtastic protobuf), or
    "controlmap" (no serial command channel at all, e.g. BlueJammer's web-UI control). Lets a node say
    honestly whether it even has a sendable command channel. Falls back to "text-cli" for unknown firmwares."""
    try:
        return getattr(get_protocol(name), "driver_type", "text-cli") or "text-cli"
    except Exception:  # noqa: BLE001
        return "text-cli"


def line_ending_for(name: str) -> str:
    """The per-firmware serial command terminator (LF by default, CR for Flipper). Programmatic send paths
    (AutoRouter, Broadcast, execute_action) must stamp this on the connection before writing, because the
    interactive device tab only sets it on the *active* tab's connection — so without this a command routed
    to a non-active Flipper would be LF-terminated and silently ignored by its CR-only shell. Falls back to
    LF for unknown firmwares."""
    try:
        return getattr(get_protocol(name), "line_ending", "\n") or "\n"
    except Exception:  # noqa: BLE001
        return "\n"


# --- Protocol module lookup (for TARGET_ACTIONS access) ---

# Maps internal name -> dotted module path so get_protocol_module() can return
# the module object itself (not just a parser instance).
_NAME_TO_MODULE: dict[str, str] = {
    "marauder": "src.protocols.marauder",
    "ghost-esp": "src.protocols.ghost_esp",
    "bruce": "src.protocols.bruce",
    "flipper": "src.protocols.flipper",
    "halehound": "src.protocols.halehound",
    "meshtastic": "src.protocols.meshtastic",
    "esp32-div": "src.protocols.esp32_div",
    "bw16": "src.protocols.bw16",
    "bluejammer": "src.protocols.bluejammer",
    "nrf-bluenullifier2": "src.protocols.nrf_bluenullifier",
    "flock-you": "src.protocols.flock_you",
    "generic": "src.protocols",  # GenericProtocol lives in __init__
    "raw": "src.protocols",
}


def get_protocol_module(name: str) -> types.ModuleType | None:
    """Return the protocol *module* for the given internal name.

    Unlike :func:`get_protocol` (which returns a parser *instance*), this
    returns the module object so callers can access module-level attributes
    such as ``TARGET_ACTIONS``.

    Returns ``None`` for unknown names (no fallback to generic).
    """
    if not name:
        return None
    key = name.strip().lower()
    mod_path = _NAME_TO_MODULE.get(key)
    if mod_path is None:
        mod_path = _NAME_TO_MODULE.get(key.replace("_", "-"))
    if mod_path is None:
        return None
    return importlib.import_module(mod_path)


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
    "Esp32DivProtocol",
    "BW16Protocol",
    "BlueJammerProtocol",
    "NrfBlueNullifier2Protocol",
    "FlockYouProtocol",
    "GenericProtocol",
    "PROTOCOLS",
    "PROTOCOL_DISPLAY_NAMES",
    "get_protocol",
    "get_protocol_by_display",
    "resolve_protocol_name",
    "get_protocol_module",
    "list_protocols",
    "capabilities_for",
    "driver_type_for",
    "line_ending_for",
]
