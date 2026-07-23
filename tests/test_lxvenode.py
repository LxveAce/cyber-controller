"""Tests for LxveNodeProtocol (the LXVENODE/1 relay parser + demux).

Ready-to-merge: drop into ``cyber-controller/tests/`` (alongside the existing protocol tests) and run with
``pytest`` from the cyber-controller repo root — the imports below assume the merged locations
``src/protocols/lxvenode.py`` and ``src/protocols/base.py``. The demux-delegation tests use a local fake
target parser so they don't depend on any real target protocol module.

Covers: the LXVENODE/1 line parser (node -> device_info, caps decode, link/tier -> link_state), the DEMUX
(rx unwrap + bare passthrough both delegate to the inner target parser), link_state field typing, identify,
and the forward-compat / hardening posture ported from lxveos.py (unknown types/keys, empty values, bounded
digit runs, DoS-bounded caps).
"""

from __future__ import annotations

import pytest

from src.protocols.base import ParsedEvent
from src.protocols.lxvenode import LxveNodeProtocol


# ── A fake target parser to prove the demux delegates without a real target module ──────────────

class _FakeTarget:
    """Minimal duck-typed target parser. Records what it was handed and echoes a distinctive event."""

    protocol_name = "fake"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def parse_line(self, line: str) -> ParsedEvent:
        self.seen.append(line)
        return ParsedEvent(event_type="fake_event", data={"text": line}, raw=line)


@pytest.fixture()
def proto() -> LxveNodeProtocol:
    return LxveNodeProtocol()


# ── node -> device_info + caps decode ────────────────────────────────────────────────────────────

def test_node_frame_becomes_device_info(proto):
    ev = proto.parse_line(
        "LXVENODE/1 node role=base fw=0.1.0 board=lxvenode_compact_s3 batt=87 caps=0x0b peers=1"
    )
    assert ev is not None
    assert ev.event_type == "device_info"
    assert ev.data["role"] == "base"
    assert ev.data["fw"] == "0.1.0"
    assert ev.data["batt"] == 87            # int-coerced
    assert ev.data["peers"] == 1
    assert ev.data["proto_version"] == 1
    assert ev.data["source"] == "node_frame"


def test_caps_bitmask_decodes_to_tokens(proto):
    # 0x0b = 0b1011 -> bits 0,1,3 -> wifi, ble, lora (design bit order wifi/ble/espnow/lora/nrf24/gps)
    ev = proto.parse_line("LXVENODE/1 node role=relay caps=0x0b")
    assert ev.data["caps"] == 0x0b
    assert ev.data["caps_tokens"] == ["wifi", "ble", "lora"]


def test_caps_beyond_known_range_is_forward_compat_capN(proto):
    # bit 7 set (0x80) is beyond the 6 known slugs -> surfaced as cap7, not dropped
    ev = proto.parse_line("LXVENODE/1 node caps=0x80")
    assert ev.data["caps_tokens"] == ["cap7"]


def test_caps_absurdly_large_stays_raw_string(proto):
    # DoS guard: a >64-bit caps value is left a raw string (no O(bits^2) big-int work on the reader thread)
    big = "0x" + "f" * 64  # 256 bits
    ev = proto.parse_line(f"LXVENODE/1 node caps={big}")
    assert ev.data["caps"] == big
    assert "caps_tokens" not in ev.data


# ── link / tier -> link_state ────────────────────────────────────────────────────────────────────

def test_link_frame_becomes_link_state(proto):
    ev = proto.parse_line(
        "LXVENODE/1 link tier=lora rssi=-104 snr=-7 dr=sf9bw125 latency_ms=620 up=1 peer=nodeA mode=compact"
    )
    assert ev.event_type == "link_state"
    assert ev.data["link_event"] == "link"
    assert ev.data["tier"] == "lora"
    assert ev.data["rssi"] == -104          # signed int
    assert ev.data["snr"] == -7
    assert ev.data["latency_ms"] == 620
    assert ev.data["dr"] == "sf9bw125"      # left a string
    assert ev.data["up"] is True            # 1/0 -> bool
    assert ev.data["peer"] == "nodeA"
    assert ev.data["mode"] == "compact"


def test_tier_failover_becomes_link_state(proto):
    ev = proto.parse_line("LXVENODE/1 tier from=wifi to=lora reason=rssi")
    assert ev.event_type == "link_state"
    assert ev.data["link_event"] == "tier"
    assert ev.data["from"] == "wifi"
    assert ev.data["to"] == "lora"
    assert ev.data["reason"] == "rssi"


def test_stats_and_busy_are_link_state(proto):
    ev = proto.parse_line("LXVENODE/1 stats conoutq=6144 evtq=3 cmdq=0 dropped_evt=2 airtime_pct=41")
    assert ev.event_type == "link_state"
    assert ev.data["conoutq"] == 6144
    assert ev.data["dropped_evt"] == 2
    assert ev.data["airtime_pct"] == 41
    busy = proto.parse_line("LXVENODE/1 busy cmdq=full")
    assert busy.event_type == "link_state"
    assert busy.data["link_event"] == "busy"
    assert busy.data["cmdq"] == "full"      # non-numeric -> stays raw


def test_tgt_status_hex_digest_decodes(proto):
    ev = proto.parse_line("LXVENODE/1 tele batt=64 vbus=1 tgt=up tgt_status=41726d3d73616665")
    assert ev.event_type == "link_state"
    assert ev.data["batt"] == 64
    assert ev.data["vbus"] is True
    assert ev.data["tgt"] == "up"
    assert ev.data["tgt_status"] == "Arm=safe"           # hex-decoded text
    assert ev.data["tgt_status_hex"] == "41726d3d73616665"  # raw kept


# ── DEMUX: rx unwrap delegates to the inner target parser ────────────────────────────────────────

def test_rx_frame_delegates_to_target_parser(proto):
    fake = _FakeTarget()
    proto.set_target_protocol(fake)
    payload_hex = "hello world".encode().hex()   # 68656c6c6f20776f726c64
    ev = proto.parse_line(f"LXVENODE/1 rx seq=418 src=target payload={payload_hex}")
    # The returned event is the TARGET's own, produced from the UNWRAPPED text
    assert ev.event_type == "fake_event"
    assert ev.data["text"] == "hello world"
    assert fake.seen == ["hello world"]


def test_bare_passthrough_line_delegates_to_target_parser(proto):
    # On Wi-Fi/ESP-NOW-near, target console lines arrive with NO node prefix -> delegate straight through
    fake = _FakeTarget()
    proto.set_target_protocol(fake)
    line = "LXVEOS/1 ap bssid=aa:bb:cc:dd:ee:ff ssid=4d794e6574 ch=6 rssi=-42 auth=wpa2"
    ev = proto.parse_line(line)
    assert ev.event_type == "fake_event"
    assert fake.seen == [line]


def test_rx_without_payload_is_benign_info(proto):
    fake = _FakeTarget()
    proto.set_target_protocol(fake)
    ev = proto.parse_line("LXVENODE/1 rx seq=419 src=target payload=")
    assert ev.event_type == "info"
    assert ev.data["node_event"] == "rx"
    assert fake.seen == []          # nothing delegated for an empty payload


def test_node_target_hint_auto_binds_demux(proto):
    # A `node` frame's target= hint auto-points the demux at the relayed firmware (real registry lookup)
    proto.parse_line("LXVENODE/1 node role=base target=marauder")
    assert proto.target_protocol_name == "marauder"


def test_set_target_protocol_by_name_is_idempotent(proto):
    proto.set_target_protocol("marauder")
    inst1 = proto._target_proto
    proto.set_target_protocol("marauder")   # same name -> keep the same instance (preserves scan ordinals)
    assert proto._target_proto is inst1


# ── txack / done -> info ─────────────────────────────────────────────────────────────────────────

def test_txack_is_info(proto):
    ev = proto.parse_line("LXVENODE/1 txack seq=77 state=delivered")
    assert ev.event_type == "info"
    assert ev.data["node_event"] == "txack"
    assert ev.data["seq"] == 77
    assert ev.data["state"] == "delivered"


# ── Forward-compat + hardening (ported from lxveos.py) ───────────────────────────────────────────

def test_unknown_node_type_is_surfaced_not_dropped(proto):
    ev = proto.parse_line("LXVENODE/1 wibble foo=bar answer=42")
    assert ev.event_type == "info"
    assert ev.data["node_event"] == "wibble"
    assert ev.data["fields"] == {"foo": "bar", "answer": "42"}


def test_unknown_key_on_known_type_is_kept(proto):
    ev = proto.parse_line("LXVENODE/1 link tier=wifi rssi=-40 futurekey=xyz")
    assert ev.data["tier"] == "wifi"
    assert ev.data["futurekey"] == "xyz"    # unknown key preserved as raw string


def test_empty_value_is_tolerated(proto):
    ev = proto.parse_line("LXVENODE/1 link tier=lost peer=")
    assert ev.event_type == "link_state"
    assert ev.data["tier"] == "lost"
    assert ev.data["peer"] == ""


def test_forward_compat_higher_version_still_parses(proto):
    ev = proto.parse_line("LXVENODE/2 node role=base fw=9.9.9")
    assert ev.event_type == "device_info"
    assert ev.data["proto_version"] == 2


def test_lxnode_prefix_alias_is_accepted(proto):
    # The wire-spec base-ASCII prefix (LXNODE/) is accepted too, until docs+firmware reconcile to one prefix
    ev = proto.parse_line("LXNODE/1 link tier=espnow rssi=-60")
    assert ev.event_type == "link_state"
    assert ev.data["tier"] == "espnow"


def test_hostile_long_digit_run_does_not_raise(proto):
    # A 4301-digit version would blow Python's str->int limit; the bounded regex simply doesn't match the
    # node grammar, so the line falls through to the (generic) target parser instead of raising ValueError.
    line = "LXVENODE/" + ("9" * 4301) + " node role=base"
    ev = proto.parse_line(line)          # must not raise
    assert ev is None or isinstance(ev, ParsedEvent)


def test_blank_line_is_none(proto):
    assert proto.parse_line("   ") is None


# ── identify + basics ────────────────────────────────────────────────────────────────────────────

def test_identify_claims_node_lines(proto):
    assert proto.identify("LXVENODE/1 node role=base fw=0.1.0") is True
    assert proto.identify("LXNODE/1 link tier=wifi") is True
    assert proto.identify("hello from LxveNode compact") is True
    assert proto.identify("LXVEOS/1 status board=x") is False
    assert proto.identify("random serial noise") is False


def test_metadata(proto):
    assert proto.protocol_name == "lxvenode"
    assert proto.driver_type == "text-cli"
    assert proto.line_ending == "\n"


def test_node_commands_present_and_danger_free(proto):
    cmds = {c.name: c for c in proto.get_commands()}
    assert "nodeinfo" in cmds
    assert "tier" in cmds
    assert "target" in cmds
    assert "target flash" in cmds
    # No Node verb is danger-classed (the node transmits nothing offensive)
    assert all(c.danger == "" for c in cmds.values())
    # None carries a stream= kwarg (that CommandInfo field is a fleet add; Node verbs never need it)


def test_format_command(proto):
    assert proto.format_command("tier", {"mode": "lora"}) == "tier lora"
    assert proto.format_command("nodeinfo") == "nodeinfo"
