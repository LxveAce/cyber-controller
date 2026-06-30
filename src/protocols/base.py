"""Base protocol — abstract interface for firmware-specific serial parsers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedEvent:
    """Structured output from parsing a serial line.

    Attributes:
        event_type: Category string (e.g. 'ap_found', 'handshake', 'info').
        data: Parsed payload dict.
        raw: Original line text.
    """

    event_type: str
    data: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


@dataclass
class CommandInfo:
    """Metadata for a protocol command.

    Attributes:
        name: Command string to send.
        category: Grouping category.
        description: What the command does.
        args: Optional argument description.
        danger: Risk class for the safety/disclaimer system. "" = safe;
            "lab-only" = RF transmit / jamming / deauth / brute force that
            must only be run in an authorized, controlled environment;
            "illegal-tx" = transmission that is illegal in most jurisdictions
            (e.g. broadband jamming). The UI gates non-empty values behind a
            confirmation unless the user has suppressed warnings.
    """

    name: str
    category: str = ""
    description: str = ""
    args: str = ""
    danger: str = ""


class BaseProtocol(ABC):
    """Abstract base class for firmware communication protocols.

    Subclasses implement the three core methods to support a specific
    firmware's serial interface (command formatting, output parsing,
    and command enumeration).
    """

    # Line terminator the firmware's CLI expects after each command. Most firmwares read a line on LF
    # ("\n"); the Flipper Zero shell only submits a line on CR ("\r"). Subclasses override as needed.
    line_ending: str = "\n"

    # What this firmware/board can do — a set of canonical capability tokens used to surface each connected
    # device as a node in the network (Devices-tab capability chips) and to inform Broadcast/AutoRouter which
    # devices a verb applies to. Canonical tokens (keep consistent across protocols):
    #   wifi · ble · bt · subghz · nfc · rfid · ir · gps · lora · mesh · nrf24 · rc · badusb · jammer
    # Subclasses override; an empty set means "no declared capabilities" (e.g. generic/raw).
    capabilities: "frozenset[str]" = frozenset()

    @property
    @abstractmethod
    def protocol_name(self) -> str:
        """Human-readable protocol identifier."""
        ...

    @abstractmethod
    def parse_line(self, line: str) -> ParsedEvent | None:
        """Parse a single line of serial output.

        Args:
            line: Raw text line received from the device.

        Returns:
            A ParsedEvent if the line is meaningful, or None for noise.
        """
        ...

    @abstractmethod
    def get_commands(self) -> list[CommandInfo]:
        """Return the full list of supported commands."""
        ...

    # Class-level memo of the (static) command list. Subclasses build a fresh list of
    # CommandInfo dataclasses on every get_commands() call; that's hot on each Send (the UI
    # looks up CommandInfo per keystroke-completed command) and on the startup palette build
    # (236 items across all protocols). The list is constant per protocol class, so cache it
    # once. (UI-opt #2.)
    _commands_cache: dict[type, list[CommandInfo]] = {}

    def cached_commands(self) -> list[CommandInfo]:
        """Memoized get_commands() keyed by concrete protocol class.

        Returns a shared list — callers must treat it as READ-ONLY (the UI's lookups and
        palette build only iterate). Equivalent in content to get_commands().
        """
        cls = type(self)
        cached = BaseProtocol._commands_cache.get(cls)
        if cached is None:
            cached = self.get_commands()
            BaseProtocol._commands_cache[cls] = cached
        return cached

    @abstractmethod
    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        """Format a command string ready to send over serial.

        Args:
            cmd: Base command name.
            args: Optional key-value arguments.

        Returns:
            Formatted command string (without trailing newline).
        """
        ...

    def identify(self, line: str) -> bool:
        """Return True if the line looks like output from this protocol.

        Used during auto-detection to guess which firmware is running.
        The default implementation returns False.
        """
        return False
