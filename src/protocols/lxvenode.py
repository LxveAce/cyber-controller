"""LxveNode protocol — serial parser for the LxveNode relay/repeater link.

LxveNode (``LxveAce/lxvenode``) is a symmetric ESP32-S3 + Wio-SX1262 (LoRa 915) relay node. One firmware,
role chosen at boot (``base`` at the computer / ``relay`` at the target / ``middle`` repeater). It turns two
boards into a "wireless USB cable" to a target device with Wi-Fi-near / ESP-NOW-mid / LoRa-far auto-failover.

This is the CC-side OUTER parser. It is deliberately a direct analog of ``src/protocols/lxveos.py`` and
reuses every hard-won idea in that file (bounded digit runs, empty-value tolerance, key-on-NAMES, hex
free-text, forward-compat unknown types/keys, bounded caps decode). Two grammars ride one COM port:

1. The node's OWN link/session/telemetry protocol — versioned ``LXVENODE/<v>`` lines the base injects so CC
   can see link health, role, failover, and backpressure:

       LXVENODE/1 node  role=base fw=0.1.0 board=lxvenode_compact_s3 batt=87 caps=0x0b peers=1 target=marauder
       LXVENODE/1 link  tier=lora rssi=-104 snr=-7 dr=sf9bw125 latency_ms=620 up=1 peer=nodeA mode=compact
       LXVENODE/1 tier  from=wifi to=lora reason=rssi
       LXVENODE/1 rx    seq=418 src=target payload=<hex of one target console line>
       LXVENODE/1 txack seq=77 state=delivered

2. The TARGET's own stream — carried transparently by the node. On Wi-Fi/ESP-NOW (near) the target's console
   lines pass through VERBATIM with no prefix; on LoRa (far) each ``LXVEOS/1`` event is binary-compacted
   over the air and the base reconstitutes it, arriving here inside an ``rx`` frame's hex ``payload``. Either
   way this parser hands the unwrapped target text to the TARGET's own parser (Marauder / LxveOS / GhostESP /
   …) via the DEMUX (``_target_proto``) so the target experience in CC is byte-for-byte unchanged. The node
   is a *link*, not a new parser for the target.

Design SSOT: ``LxveNode-in-Cyber-Controller.md`` (§3 parser, §4 profile) + wire spec ``LxveNode-Relay-Protocol.md``.

------------------------------------------------------------------------------------------------------------
HONESTY / verify-never-fake (nothing here is bench-verified — no node exists yet):
  * TODO(HW): the wire spec doc (``LxveNode-Relay-Protocol.md`` §7) names the base→CC ASCII lines
    ``LXNODE/1`` (role/link/stats/tele/resync/busy/abort/pair/done), while the CC design SSOT (§3a) uses
    ``LXVENODE/1`` (node/link/tier/rx/txack). This parser accepts BOTH prefixes and a UNION of both type
    sets so it can't miss a real line, but the firmware and the docs MUST be reconciled to ONE prefix +
    type vocabulary before HW validation. Treat the accepted set here as a superset, not a contract.
  * TODO(HW): the caps bit order (``_NODE_CAP_SLUGS``) is the DESIGN order (wifi/ble/espnow/lora/nrf24/gps);
    confirm it against the firmware's real ``lxvenode_cap_t`` enum on first bring-up, exactly as lxveos.py's
    caps map was verified against source + a live board.
  * TODO(HW): field typing (rssi/snr/latency/sf/queue depths/batt) mirrors the doc examples; the real field
    names/units land when the firmware emits its first line. Unknown keys/types are surfaced, never dropped.
------------------------------------------------------------------------------------------------------------
"""

from __future__ import annotations

import re

from src.protocols.base import BaseProtocol, CommandInfo, ParsedEvent

# General node line: `LXVENODE/<v> <type> <space-separated key=value>`. Also accept the wire-spec's `LXNODE/`
# base-ASCII prefix (see the honesty note) so a real line is never missed while the docs are reconciled.
# Digit runs are bounded (\d{1,20}, not \d+): a hostile 4301-digit run would otherwise hit Python's
# ~4300-digit str->int limit and raise ValueError out of the parse. 20 digits fits any 64-bit value; an
# over-long run fails to match here (dropped clean) while a real version parses.
_RE_EVENT = re.compile(r"^LX(?:VE)?NODE/(\d{1,20})\s+(\w+)\s*(.*)$")
# key=value pairs; value may be EMPTY (e.g. a hidden peer emits `peer=`), so `\S*` not `\S+`.
_RE_KV = re.compile(r"(\w+)=(\S*)")

# LxveNode capability bitmask -> slug, in the DESIGN bit order (see TODO(HW) above): WIFI=bit0, BLE=1,
# ESPNOW=2, LORA=3, NRF24=4, GPS=5. A set bit beyond this range surfaces as `capN` (forward-compat).
_NODE_CAP_SLUGS = ("wifi", "ble", "espnow", "lora", "nrf24", "gps")

# Node line types that describe the LINK / session / node health — all folded into ONE `link_state` CC
# event (the Link strip + Device.link consume it). Each keeps a `link_event` sub-tag so the UI can tell a
# steady `link` update from a `tier` failover / `busy` backpressure / `abort` notice.
_LINK_TYPES = frozenset({"link", "tier", "stats", "tele", "role", "resync", "busy", "abort", "pair"})

# Integer-typed fields across the node line vocabulary. rssi/snr are signed (leading '-'), handled by
# _to_int. MAC-shaped / enum / string fields (tier/mode/reason/role/kind/state/dr/peer/from/to/of) stay
# strings. Any field not listed stays a raw string (forward-compat).
_INT_FIELDS = frozenset({
    "rssi", "snr", "latency_ms", "rtt_ms", "sf", "batt", "peers", "seq", "sid", "n",
    "conoutq", "evtq", "cmdq", "dropped_evt", "dropped_conout", "airtime_pct",
    "conout_ack", "cmd_ack", "tele_ack",
})
# 1/0 boolean flags. A non-0/1 value stays raw (forward-compat).
_BOOL_FIELDS = frozenset({"up", "vbus"})
# Hex-encoded free-text / opaque digest fields — decoded best-effort with the raw kept as `<field>_hex`.
# `payload` (the wrapped target line) is handled separately by the demux, NOT here.
_HEX_FIELDS = frozenset({"tgt_status"})


def _decode_caps(mask: int) -> list[str]:
    """Expand the ``caps`` bitmask into node capability slugs (firmware bit order). A set bit beyond the
    known range is surfaced as ``capN`` rather than dropped (same forward-compat posture as lxveos.py)."""
    out = []
    bit = 0
    while (1 << bit) <= mask:
        if mask & (1 << bit):
            out.append(_NODE_CAP_SLUGS[bit] if bit < len(_NODE_CAP_SLUGS) else f"cap{bit}")
        bit += 1
    return out


def _hex_field(val: str) -> tuple[str, str]:
    """Decode a hex-encoded field to (text, raw_hex). Free-text (peer names, target digests) is hex on the
    wire so a space can't break the space-split line; decode best-effort to UTF-8 (undecodable bytes ->
    U+FFFD) and keep the raw hex alongside. A value that isn't valid hex is returned unchanged for both
    (forward-compat with a future non-hex field)."""
    try:
        raw = bytes.fromhex(val)
    except ValueError:
        return val, val
    return raw.decode("utf-8", "replace"), val


def _to_int(val: str):
    """Coerce a decimal (optionally signed) field to int, else leave it raw. len<=20 bounds int() against a
    hostile 4301-digit run (isdigit() alone imposes no length limit). A non-numeric value passes through
    unchanged so a future non-int field is never corrupted."""
    body = val[1:] if val[:1] in "+-" else val
    if body.isdigit() and len(body) <= 20:
        return int(val)
    return val


def _to_bool(val: str):
    """Coerce a 1/0 flag to bool; anything else stays raw (forward-compat)."""
    if val in ("0", "1"):
        return val == "1"
    return val


def _coerce_field(key: str, val: str):
    """Type one node-line field by NAME. caps is decoded by the caller (it also emits caps_tokens)."""
    if key == "caps":
        try:
            mask = int(val, 16)
        except ValueError:
            return val
        # A real caps bitmask is a few bits. A hostile device can send a 64 KiB caps= hex string, and
        # _decode_caps is O(bits^2) big-int work on the serial reader thread (DoS). Cap at 64 bits (ample
        # for forward-compat capN); an over-large value stays a raw string.
        if mask < 0 or mask.bit_length() > 64:
            return val
        return mask
    if key in _INT_FIELDS:
        return _to_int(val)
    if key in _BOOL_FIELDS:
        return _to_bool(val)
    return val


class LxveNodeProtocol(BaseProtocol):
    """Parser + command formatter for the LxveNode relay link.

    Outer layer: parses ``LXVENODE/1`` node/link/tier/rx/txack lines. Inner layer (the DEMUX): unwraps an
    ``rx`` frame — and any bare target-console passthrough line — and delegates to the TARGET's own parser
    (``_target_proto``), so a relayed Marauder/LxveOS/GhostESP surfaces its own events unchanged.
    """

    # LxveNode reports its real capabilities at RUNTIME via the `node` line's `caps=` bitmask (like lxveos);
    # no static tokens are claimed here (declaring "lora" would be a guess before the firmware says so).
    # Consumers read caps from the parsed device_info's caps_tokens.
    capabilities: "frozenset[str]" = frozenset()
    driver_type = "text-cli"          # a line shell over USB-CDC (or TCP via a future NetConnection)
    line_ending = "\n"

    def __init__(self) -> None:
        # The DEMUX target parser. Lazily defaulted to the generic passthrough so a node with an unknown /
        # unset relayed firmware still surfaces the target's console as `info` (never dropped). Set from the
        # `node` frame's `target=` hint or explicitly by CC via set_target_protocol().
        self._target_proto: BaseProtocol | None = None
        self._target_name: str = ""

    @property
    def protocol_name(self) -> str:
        return "lxvenode"

    # ── DEMUX (the one genuinely new mechanism) ──────────────────────

    def _target(self) -> BaseProtocol:
        """The inner target parser, defaulting to the generic passthrough on first use. Lazy import keeps
        this module free of a circular dependency on the ``src.protocols`` package __init__."""
        if self._target_proto is None:
            from src.protocols import get_protocol  # lazy: avoids protocols/__init__ <-> this circular
            self._target_proto = get_protocol("generic")
            self._target_name = "generic"
        return self._target_proto

    def set_target_protocol(self, proto) -> None:
        """Point the demux at the relayed target's firmware. Accepts an internal name (``"marauder"``) or a
        ready parser instance (anything with ``parse_line``). Called by CC when the operator picks the
        relayed firmware, or automatically from a ``node`` frame's ``target=`` hint. Idempotent: setting the
        same name again is a no-op (keeps the existing parser instance + its per-connection scan ordinals)."""
        if isinstance(proto, str):
            name = proto.strip().lower()
            if not name or name == self._target_name:
                return
            from src.protocols import get_protocol  # lazy (see _target)
            self._target_proto = get_protocol(name)
            self._target_name = name
        elif hasattr(proto, "parse_line"):
            self._target_proto = proto
            self._target_name = getattr(proto, "protocol_name", "custom")

    @property
    def target_protocol_name(self) -> str:
        """The internal name of the currently-bound relayed target firmware (``""`` until first used)."""
        return self._target_name

    # ── Parsing ──────────────────────────────────────────────────────

    def parse_line(self, line: str) -> ParsedEvent | None:
        line = line.strip()
        if not line:
            return None

        # Outer layer: a `LXVENODE/<v> <type> ...` node line.
        m = _RE_EVENT.match(line)
        if m:
            return self._parse_event(int(m.group(1)), m.group(2), m.group(3), line)

        # Inner layer (bare passthrough): on Wi-Fi/ESP-NOW-near the target's console/`LXVEOS/1` lines arrive
        # UNWRAPPED (no node prefix). Delegate straight to the target's own parser — the node is transparent.
        return self._target().parse_line(line)

    def _parse_event(self, version: int, etype: str, rest: str, raw: str) -> ParsedEvent | None:
        """Dispatch one `LXVENODE/<version> <etype> <rest>` node line."""
        kv = dict(_RE_KV.findall(rest))

        # `node` — the node's own identity + telemetry + caps -> device_info (Device.apply_device_info fills
        # runtime_capabilities from caps_tokens + telemetry; already exists, no CC change).
        if etype == "node":
            data: dict = {"proto_version": version, "source": "node_frame"}
            for key, val in kv.items():
                if key in _HEX_FIELDS:
                    text, raw_hex = _hex_field(val)
                    data[key] = text
                    data[key + "_hex"] = raw_hex
                else:
                    data[key] = _coerce_field(key, val)
            if isinstance(data.get("caps"), int):
                data["caps_tokens"] = _decode_caps(data["caps"])
            # A `target=<fw>` hint auto-binds the demux, so a relayed Marauder routes to marauder.py without
            # the operator picking it. Guarded so a bad/unknown name just falls back to generic.
            tgt = kv.get("target")
            if tgt:
                try:
                    self.set_target_protocol(tgt)
                except Exception:  # noqa: BLE001 — a demux bind must never break identity ingestion
                    pass
            return ParsedEvent(event_type="device_info", data=data, raw=raw)

        # `rx` — DEMUX: one wrapped target console line. Decode the hex payload and delegate to the target's
        # own parser; its ParsedEvent (ap_found / handshake_captured / arm_state / …) flows to
        # TargetIngestor._route with ZERO ingestor changes. A missing/blank payload is a benign keepalive.
        if etype == "rx":
            payload = kv.get("payload", "")
            if not payload:
                return ParsedEvent(event_type="info", data={"node_event": "rx", "fields": kv,
                                                             "proto_version": version}, raw=raw)
            text, _hex = _hex_field(payload)
            return self._target().parse_line(text)

        # link / tier / stats / tele / role / resync / busy / abort / pair -> one `link_state` event. A new
        # Device.apply_link_state stores Device.link = {tier, rssi, snr, latency_ms, dr, mode, role, peer,
        # up, ...} in the guarded shape of apply_arm_state; TargetIngestor._route gains one branch (see
        # INTEGRATION.md). `link_event` names the sub-type so the Link strip renders the right thing.
        if etype in _LINK_TYPES:
            data = {"proto_version": version, "link_event": etype}
            for key, val in kv.items():
                if key in _HEX_FIELDS:
                    text, raw_hex = _hex_field(val)
                    data[key] = text
                    data[key + "_hex"] = raw_hex
                else:
                    data[key] = _coerce_field(key, val)
            return ParsedEvent(event_type="link_state", data=data, raw=raw)

        # `txack` (command relayed) and `done` (end-of-batch marker) — surfaced as benign info; the Link
        # strip may show a small pending/delivered indicator off txack.
        if etype in ("txack", "done"):
            data = {"node_event": etype, "proto_version": version}
            for key, val in kv.items():
                data[key] = _coerce_field(key, val)
            return ParsedEvent(event_type="info", data=data, raw=raw)

        # Forward-compat: an unknown node type — keep it (name + fields) rather than dropping, so a newer
        # node firmware never breaks an older CC (mirrors lxveos.py's unknown-type handling).
        return ParsedEvent(
            event_type="info",
            data={"node_event": etype, "fields": kv, "proto_version": version},
            raw=raw,
        )

    # ── Commands ─────────────────────────────────────────────────────

    def get_commands(self) -> list[CommandInfo]:
        """The NODE command surface (driving the node itself, not the relayed target). None are ``danger``
        at the node level — the node transmits nothing offensive; ``target flash`` is bulk and gated by CC's
        existing Flash owner-gate, not the danger class. The RELAYED TARGET's command grid comes from its
        OWN protocol (via the demux), unchanged.

        NOTE (drop-in): these deliberately do NOT pass a ``stream=`` kwarg — that CommandInfo field is a
        FLEET add (see INTEGRATION.md) and would break against today's CommandInfo. No Node verb is a
        high-bandwidth stream, so none needs it; the ``stream`` gate applies to the TARGET protocols' grids.
        """
        C = CommandInfo
        return [
            C("nodeinfo", "Node", "Query node identity + telemetry (role/fw/board/batt/caps/peers)"),
            C("link", "Node", "Show the current link tier + quality (rssi/snr/dr/latency/mode)", args="status"),
            C("tier", "Node", "Pin the link tier for a range test, or return to auto-failover",
              args="auto|wifi|espnow|lora"),
            C("peers", "Node", "List paired peers / repeater hops"),
            C("pair", "Node", "Manage node pairing (out-of-band key provisioning)", args="open|status|clear"),
            C("stats", "Node", "Dump link queue depths + honest drop counters (conout/evt/cmd, airtime)"),
            # Target-link control over the relay (scoped to J3). reset/boot are tiny CTRL commands that work
            # on any tier incl. LoRa; flash is Wi-Fi-only BULK and is refused on LoRa by the transport.
            C("target", "Node", "Target-link control over the relay: reset/enter-bootloader/status",
              args="reset|boot|status"),
            C("target flash", "Node",
              "Proxy an esptool reflash of the TARGET over Wi-Fi (near only; refused on LoRa). "
              "Gated by CC's Flash owner-gate — confirm required.", args="<profile>"),
        ]

    # ── Formatting ───────────────────────────────────────────────────

    def format_command(self, cmd: str, args: dict[str, str] | None = None) -> str:
        if args:
            arg_str = " ".join(str(v) for v in args.values())
            return f"{cmd} {arg_str}"
        return cmd

    # ── Auto-detection ───────────────────────────────────────────────

    def identify(self, line: str) -> bool:
        """Return True if the line looks like LxveNode output. Matches the node's own banner/lines; the
        connect-time probe + post-probe re-autodetect route the port to this parser (the LxveOS path)."""
        stripped = line.strip()
        return (
            stripped.startswith("LXVENODE/")
            or stripped.startswith("LXNODE/")   # wire-spec base-ASCII prefix (see honesty note)
            or "LxveNode" in line
        )
