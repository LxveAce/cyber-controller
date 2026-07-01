"""Connect-time handshake/health probe (comms rework, S3-c). The pure classify/learn helpers are tested
against canned help dumps (no hardware); probe_device is tested with a synchronous fake connection. Live
confirmation against real firmware is the S5 bench gate.
"""
from __future__ import annotations

from src.core.device_manager import DeviceManager
from src.core.handshake import (
    classify_reply,
    detect_firmware,
    learn_vocabulary,
    probe_commands_for,
    probe_device,
)
from src.models.device import Device, Protocol

# A representative Marauder `help` reply (banner line + a few real commands).
MARAUDER_HELP = [
    "ESP32 Marauder v1.9.1",
    "  scanall",
    "  stopscan",
    "  sniffpmkid",
    "  list -a",
    "  attack -t deauth",
]


class _FakeConn:
    """Synchronous fake: firing on write means probe_device sees the reply immediately (no real waiting)."""

    def __init__(self, reply_lines=(), connected: bool = True) -> None:
        self.is_connected = connected
        self.writes: list[str] = []
        self._cbs: list = []
        self._reply = list(reply_lines)

    def on_line(self, cb) -> None:
        self._cbs.append(cb)

    def remove_line_callback(self, cb) -> None:
        try:
            self._cbs.remove(cb)
        except ValueError:
            pass

    def write(self, cmd: str) -> None:
        self.writes.append(cmd)
        for ln in self._reply:
            for cb in list(self._cbs):
                cb(ln)


# ── pure: classify_reply ───────────────────────────────────────────────

def test_classify_reply_alive_uses_firmware_banner():
    health, banner = classify_reply(MARAUDER_HELP, Device(port="C", firmware="marauder"))
    assert health == "alive"
    assert "Marauder" in banner  # the identify()-matched line, not a random one


def test_classify_reply_alive_fallback_banner_when_no_identify():
    health, banner = classify_reply(["> ready", "ok"], Device(port="C", firmware="generic"))
    assert health == "alive"
    assert banner == "> ready"


def test_classify_reply_no_reply_on_silence():
    assert classify_reply(["", "   ", "\n"], Device(port="C", firmware="marauder")) == ("no-reply", "")


# ── pure: learn_vocabulary (drift detection) ───────────────────────────

def test_learn_vocabulary_detects_live_commands():
    vocab = learn_vocabulary(MARAUDER_HELP, Device(port="C", firmware="marauder"))
    assert "scanall" in vocab and "sniffpmkid" in vocab
    # 'scanap' is the OLD token — not advertised here and not even a known Marauder command, so never learned.
    assert "scanap" not in vocab


def test_learn_vocabulary_empty_for_unknown_firmware():
    assert learn_vocabulary(MARAUDER_HELP, Device(port="C", firmware="")) == frozenset()


# ── probe command selection respects driver_type ───────────────────────

def test_probe_commands_only_for_text_cli():
    assert probe_commands_for(Device(port="C", firmware="marauder")) == ["help"]
    assert probe_commands_for(Device(port="C", firmware="meshtastic")) == []   # stream
    assert probe_commands_for(Device(port="C", firmware="bluejammer")) == []   # controlmap


# ── probe_device orchestration ─────────────────────────────────────────

def test_probe_device_alive_sets_health_and_banner():
    dev = Device(port="C_M", firmware="marauder")
    conn = _FakeConn(MARAUDER_HELP)
    res = probe_device(conn, dev)
    assert res.health == "alive"
    assert dev.health == "alive"
    assert "Marauder" in dev.fw_banner
    assert "scanall" in res.live_commands
    assert conn.writes == ["help"]


def test_probe_device_no_reply_when_silent():
    dev = Device(port="C_S", firmware="marauder")
    conn = _FakeConn(reply_lines=())  # opens fine but answers nothing
    res = probe_device(conn, dev, timeout=0.05)
    assert res.health == "no-reply"
    assert dev.health == "no-reply"


def test_probe_device_no_cli_for_stream_without_writing():
    dev = Device(port="C_MESH", firmware="meshtastic")
    conn = _FakeConn(MARAUDER_HELP)  # even if it would answer, we must not probe a protobuf stream
    res = probe_device(conn, dev)
    assert res.health == "no-cli"
    assert dev.health == "no-cli"
    assert conn.writes == []  # nothing written to a non-text-CLI node


# ── firmware autodetect (1.4) ──────────────────────────────────────────

GHOST_HELP = ["GhostESP v1.0.0", "  scanwifi", "  stopscan", "  help"]


def test_detect_firmware_from_banner():
    assert detect_firmware(MARAUDER_HELP) == "marauder"
    assert detect_firmware(GHOST_HELP) == "ghost-esp"


def test_detect_firmware_none_on_unrecognized():
    assert detect_firmware(["> ready", "unknown board", "ok"]) is None
    assert detect_firmware([]) is None


def test_probe_sets_firmware_on_unknown_device():
    # An unknown board (no firmware; USB VID only) that prints a Marauder banner should be identified.
    dev = Device(port="C_U", firmware="")
    probe_device(_FakeConn(MARAUDER_HELP), dev)
    assert dev.firmware == "marauder"
    assert dev.protocol == Protocol.MARAUDER
    assert dev.health == "alive"


def test_probe_does_not_override_known_firmware():
    # firmware already set -> detection must not clobber it even if the reply looks like another firmware.
    dev = Device(port="C_K", firmware="bruce")
    probe_device(_FakeConn(MARAUDER_HELP), dev)
    assert dev.firmware == "bruce"


# ── DeviceManager.probe entry point ────────────────────────────────────

def test_device_manager_probe_sets_health():
    dm = DeviceManager()
    dm.add_device(Device(port="C_M", firmware="marauder"))
    dm._connections["C_M"] = _FakeConn(MARAUDER_HELP)
    res = dm.probe("C_M")
    assert res is not None and res.health == "alive"
    assert dm.get_device("C_M").health == "alive"


def test_device_manager_probe_none_without_live_connection():
    dm = DeviceManager()
    dm.add_device(Device(port="C_X", firmware="marauder"))
    assert dm.probe("C_X") is None  # no connection injected
