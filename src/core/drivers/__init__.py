"""Driver seam — dispatch a node's outbound command by its transport kind (comms rework, S3-a).

`Device.driver_type` (S1) is a *label*; this package turns it into *dispatch*. A `Driver` is a stateless
strategy that knows HOW to deliver an intent to a node given its live connection, so the send path stops
assuming every firmware speaks a line-based text CLI:

- **TextCliDriver** (`text-cli`, the default): today's behavior — stamp the firmware's line terminator, then
  `SerialConnection.write` (which rejects embedded control chars). Every line-shell firmware uses this.
- **StreamDriver** (`stream`, e.g. Meshtastic): a binary/framed link, NOT a text CLI. Writing plain text to it
  is discarded by the firmware and can desync the protobuf framing, so a routed text command is an honest
  no-op here (logged). The real framed-byte path (`deliver_raw`) lands in S3-b.
- **ControlMapDriver** (`controlmap`, e.g. BlueJammer): no serial command channel at all — control is the
  device's web UI / a hardware-validated ControlMap (see `bluejammer_control.py`). A routed serial text
  command can't drive it, so it too is an honest no-op.

`CrossCommHub.send_to_port` dispatches through `driver_for(dev)`. Selection reuses the `Device.driver_type`
accessor so there's a single source of truth for "what kind of node is this."
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class Driver(ABC):
    """Stateless strategy for delivering a command to a node over its connection."""

    driver_type: str = ""

    @abstractmethod
    def deliver_text(self, conn, dev, command: str) -> bool:
        """Deliver a text *command* to the node on *conn* (a live SerialConnection). Returns True if the
        command was actually sent, False if this driver has no text command channel (a logged no-op)."""
        ...


class TextCliDriver(Driver):
    """Line-based ASCII command shell — the default. Exactly the original send path."""

    driver_type = "text-cli"

    def deliver_text(self, conn, dev, command: str) -> bool:
        # Stamp the target device's firmware terminator (Flipper CR vs LF) before writing, so a routed
        # command isn't dropped just because that device isn't the focused UI tab.
        if dev is not None:
            from src.protocols import line_ending_for
            conn.line_ending = line_ending_for(getattr(dev, "firmware", "") or getattr(dev, "name", ""))
        conn.write(command)  # SerialConnection.write rejects embedded control chars
        return True


class StreamDriver(Driver):
    """Binary/framed stream (e.g. Meshtastic protobuf StreamAPI) — no text command channel yet."""

    driver_type = "stream"

    def deliver_text(self, conn, dev, command: str) -> bool:
        # Plain text is discarded by the firmware and could desync the framing — don't write it.
        log.warning("stream driver: %r has no text command channel (protobuf/stream); dropping %r",
                    getattr(dev, "firmware", ""), command)
        return False

    def deliver_raw(self, conn, frames: bytes) -> bool:
        """The framed-byte transport path. Not implemented until S3-b (Meshtastic protobuf framer)."""
        raise NotImplementedError("stream raw-byte transport lands in S3-b (Meshtastic protobuf framer)")


class ControlMapDriver(Driver):
    """No serial command channel (e.g. BlueJammer) — control is a web UI / validated ControlMap elsewhere."""

    driver_type = "controlmap"

    def deliver_text(self, conn, dev, command: str) -> bool:
        # There is no serial CLI to write to — see bluejammer_control.py (web-UI / hardware-validated map).
        log.warning("controlmap driver: %r has no serial command channel (web-UI/controlmap); dropping %r",
                    getattr(dev, "firmware", ""), command)
        return False


# One stateless instance per kind (strategies hold no per-node state).
_DRIVERS: "dict[str, Driver]" = {d.driver_type: d for d in (TextCliDriver(), StreamDriver(), ControlMapDriver())}


def driver_for(dev) -> Driver:
    """The Driver for a device, selected by its `driver_type` (reusing the Device accessor). Unknown kinds
    and a missing device fall back to the text-CLI driver (the historical default send path)."""
    dt = getattr(dev, "driver_type", "text-cli") if dev is not None else "text-cli"
    return _DRIVERS.get(dt, _DRIVERS["text-cli"])


__all__ = ["Driver", "TextCliDriver", "StreamDriver", "ControlMapDriver", "driver_for"]
