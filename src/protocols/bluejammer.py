"""BlueJammer-V2 protocol — informational telemetry parser (NO command channel).

BlueJammer-V2 (EmenstaNougat) is a two-board 2.4 GHz RF-research rig. It exposes **no interactive
serial CLI**: the ESP32 "jamming engine" accepts no serial commands — its only control is the
physical button and the BW16-hosted web UI (http://192.168.1.1 on the device's own AP) — and its
serial output is read-only telemetry that the BW16 forwards into that web UI.

So this protocol is **informational only**: it parses boot / mode / nRF status lines into ``info`` /
``status`` events and identifies the firmware on its banner. :meth:`get_commands` returns an EMPTY
list because Cyber Controller has no way — and, per the illegal-to-operate framing (FCC 47 U.S.C.
§333), no intent — to key the transmitter over serial. CC flashes the image and reads telemetry for
lab study; it never operates the jammer. The device's control surface (the web UI) is documented in
``config/profiles/bluejammer_bw16.json`` for a launcher, not driven over this serial parser.
"""

from __future__ import annotations

import re

from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# Bracketed status tags the firmware/telemetry may emit, e.g. "[SYS] booting",
# "[MODE] BLE", "[NRF] hop ch 37". Tolerant — the exact format is unconfirmed (closed-source,
# no hardware yet), so anything bracketed maps to info/status and everything else to info.
_RE_TAG = re.compile(r"^\[(?P<tag>[A-Za-z0-9_]+)\]\s*(?P<msg>.*)$")


class BlueJammerProtocol(BaseProtocol):
    """Telemetry-only parser for BlueJammer-V2 (no sendable serial commands)."""

    # No serial command channel — control is the physical button + the device's own web UI (see the module
    # docstring). "controlmap" marks the node as controlled elsewhere, not an empty text CLI.
    driver_type = "controlmap"

    @property
    def protocol_name(self) -> str:
        return "bluejammer"

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None
        m = _RE_TAG.match(line)
        if m:
            tag = m.group("tag").upper()
            msg = m.group("msg").strip()
            data: dict[str, object] = {"tag": tag}
            if msg:
                data["message"] = msg
            # An ERROR/FAIL tag is a failed-status event; everything else is info telemetry.
            etype = "status" if tag in ("ERROR", "FAIL", "FAULT") else "info"
            if etype == "status":
                data["ok"] = False
            return ParsedEvent(event_type=etype, data=data, raw=line)
        return ParsedEvent(event_type="info", data={"message": line}, raw=line)

    def get_commands(self) -> list[CommandInfo]:
        # Intentionally empty: BlueJammer-V2 has NO serial command channel. Control is the
        # device's physical button + its self-hosted web UI; CC never keys the transmitter.
        return []

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        # No command channel exists; return the text verbatim if anything ever calls this.
        return cmd

    def identify(self, line: str) -> bool:
        """Recognise BlueJammer-V2 boot/telemetry banners."""
        markers = (
            "BlueJammer",
            "BlueJ-V2",
            "@emensta",
            "NoConn1337",
            "nRF24",
            "NRF24",
        )
        return any(m in line for m in markers)
