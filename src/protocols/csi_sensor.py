"""Wi-Fi CSI sensing node — passive, receive-only verdict parser (WS1).

Cyber Controller's sensing NODE firmware (``firmware/node/node.ino``) turns received-packet channel
state info into a compact presence/motion verdict and emits ONLY that over its link — never raw CSI.
This protocol turns those verdict lines into structured ``sensing_verdict`` events by delegating to
the pure honesty-spine core :mod:`src.core.sensing` (``parse_verdict`` + ``SENSING_TIERS``), so the
parser and node agree byte-for-byte on the wire format ``csi presence=<0|1> motion=<..> conf=<..>``.

Passive / receive-only: there is NO command channel here — a sensing node is read, not driven (its
control path is the sealed NodeLink frame, not a serial CLI), the same posture as
:class:`~src.protocols.drone_mesh.DroneMeshProtocol` and the ALPR ``FlockYouProtocol``.

``sensing_verdict`` is a NEW event type ``target_ingest`` does not handle (like ``drone_found``):
a sensing verdict is NOT a scan target, so it never routes into the shared Target pool / AutoRouter.
It feeds a dedicated sensing view later (WS1 P2). Only the PROVEN tier (presence + motion) is real
on commodity 2.4 GHz Wi-Fi; the honesty tiers live in :data:`src.core.sensing.SENSING_TIERS`.

STATUS: parser grounded in the node firmware's own ``snprintf`` format (compile-validated on
esp32:esp32@2.0.11); NOT yet verified against a live CSI capture on real silicon. Registered but
deliberately NOT given a public display name (see the note in ``src/protocols/__init__.py``) so it
does not inflate the advertised parser count until it is hardware-validated — the same posture as
``lxvenode`` / ``esp32-div-serial`` / ``drone-mesh``.
"""
from __future__ import annotations

from src.core.sensing import parse_verdict
from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent


class CsiSensorProtocol(BaseProtocol):
    """Parser for a Wi-Fi CSI sensing node (receive-only, no CLI)."""

    driver_type = "controlmap"           # passive sensor: no text CLI (mirrors DroneMesh/FlockYou)
    capabilities = frozenset({"wifi"})   # senses via commodity 2.4 GHz Wi-Fi CSI

    @property
    def protocol_name(self) -> str:
        return "csi-sensor"

    def parse_line(self, line: str) -> ParsedEvent | None:
        verdict = parse_verdict(line)
        if verdict is None:
            return None
        return ParsedEvent(
            "sensing_verdict",
            {
                "presence": verdict.presence,
                "motion": verdict.motion,
                "confidence": verdict.confidence,
                "tier": verdict.tier,
                "node_id": verdict.node_id,
            },
            (line or "").strip(),
        )

    def get_commands(self) -> list[CommandInfo]:
        return []   # passive sensor — nothing to send

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        if args:
            return f"{cmd} " + " ".join(str(v) for v in args.values())
        return cmd

    def identify(self, line: str) -> bool:
        # A verdict line is unambiguous: it parses under the sensing core's tolerant parser.
        return parse_verdict(line) is not None
