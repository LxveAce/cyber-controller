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
    """

    name: str
    category: str = ""
    description: str = ""
    args: str = ""


class BaseProtocol(ABC):
    """Abstract base class for firmware communication protocols.

    Subclasses implement the three core methods to support a specific
    firmware's serial interface (command formatting, output parsing,
    and command enumeration).
    """

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
