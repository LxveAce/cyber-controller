"""nRF BlueNullifier 2 protocol — no command channel, lab-study telemetry only.

nrfBlueNullifier-2-nrf24L01 (wirebits, GPL-3.0) is a NodeMCU-32S (ESP32-WROOM-32) driving **two**
nRF24L01+ radios that both run ``startConstCarrier(RF24_PA_MAX)`` while hopping ``random(80)``
channels — a continuous 2.4 GHz noise transmitter (a jammer). It is **illegal to operate** (FCC
47 U.S.C. 333) and is integrated here ONLY as a lab flash-and-study target, exactly like
BlueJammer-V2.

Unlike BlueJammer-V2 (which at least has a BW16 web-UI control surface), this firmware has **no
control interface at all**: the sketch contains no ``Serial.begin`` and no command parser — it jams
the instant it powers on, and its only "control" is the physical power switch. So this protocol is a
pure no-op:

* :meth:`get_commands` returns an EMPTY list — there is nothing on the wire to send, and per the
  illegal-to-operate framing there is no intent to key a transmitter over serial anyway.
* ``driver_type = "controlmap"`` marks the node as "controlled elsewhere (the power switch)", NOT an
  empty text CLI — an honest statement that it has no sendable command channel.
* :meth:`parse_line` surfaces any incidental line (e.g. ROM boot chatter) as a read-only ``info``
  event, and :meth:`identify` never claims a line (the sketch prints no identifiable banner).

Cyber Controller flashes the pinned, SHA-256-verified image for study; it never operates the device.
The flash profile lives in ``src/config/profiles/nrf_bluenullifier2.json``.
"""

from __future__ import annotations

from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent


class NrfBlueNullifier2Protocol(BaseProtocol):
    """No-op parser for nRF BlueNullifier 2 (no sendable serial commands; flash-and-study only)."""

    # No serial command channel at all — control is the physical power switch (see module doc).
    driver_type = "controlmap"

    @property
    def protocol_name(self) -> str:
        return "nrf-bluenullifier2"

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None
        # The app prints nothing (no Serial.begin); anything seen is incidental ROM/boot chatter.
        # Surface it read-only as info — never a target, never a command.
        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    def get_commands(self) -> list[CommandInfo]:
        return []

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        # There is no command channel; keep the base contract but never produce an operate command.
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{cmd} {arg_str}"
        return cmd

    def identify(self, line: str) -> bool:
        # Prints no identifiable banner — never claims a line during auto-detection.
        return False
