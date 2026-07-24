"""Auto-detect a running Meshtastic node on connect (Wave 2).

Two detectors: the banner signature (`meshtastic/firmware`, grounded in the real Heltec V3 boot line) and the
definitive StreamAPI probe (a running node has no text tell — it only answers protobuf). Both leave
device.firmware UNSET and set a meshtastic-matching banner, so the caller's re-autodetect set_firmware fires
on_device_changed (which attaches the stream backend + shows the panel). No real-radio data committed.
"""

from __future__ import annotations

from src.core.device_detect import match_firmware
from src.core.handshake import _probe_meshtastic_stream, probe_device
from src.models.device import Device
from src.protocols import meshtastic_proto as mp
from src.protocols.stream_framer import StreamFramer


def _from_radio_my_info(num=0x043AE298):
    return StreamFramer.frame(mp.field_bytes(3, mp.field_varint(1, num)))


class _StreamConn:
    """Fake conn supporting BOTH the text probe (on_line/write) and the stream probe (on_bytes/write_bytes).
    On write_bytes it delivers ``reply_frame`` to the byte callbacks (simulating the node's FromRadio reply)."""

    def __init__(self, reply_frame=None, text_reply=()):
        self.is_connected = True
        self.raw = False
        self.writes: list[str] = []
        self._line_cbs: list = []
        self._byte_cbs: list = []
        self._reply_frame = reply_frame
        self._text_reply = list(text_reply)

    def on_line(self, cb):
        self._line_cbs.append(cb)

    def remove_line_callback(self, cb):
        if cb in self._line_cbs:
            self._line_cbs.remove(cb)

    def write(self, cmd):
        self.writes.append(cmd)
        for ln in self._text_reply:
            for cb in list(self._line_cbs):
                cb(ln)

    def on_bytes(self, cb):
        self._byte_cbs.append(cb)

    def remove_byte_callback(self, cb):
        if cb in self._byte_cbs:
            self._byte_cbs.remove(cb)

    def write_bytes(self, data):
        if self._reply_frame is not None:
            for cb in list(self._byte_cbs):
                cb(self._reply_frame)


# ── banner signature (grounded in the real Heltec V3 boot line) ────────────────


def test_match_firmware_real_heltec_boot_line():
    name, _ = match_firmware("S:B:43,2.7.15.567b8ea,heltec-v3,meshtastic/firmware")
    assert name == "meshtastic"


def test_match_firmware_versioned_banner():
    name, ver = match_firmware("Meshtastic v2.7.15")
    assert name == "meshtastic"
    assert ver == "2.7.15"


# ── the definitive StreamAPI probe ─────────────────────────────────────────────


def test_stream_probe_positive_and_restores_mode():
    conn = _StreamConn(reply_frame=_from_radio_my_info())
    assert _probe_meshtastic_stream(conn, timeout=0.3) is True
    assert conn.raw is False       # prior mode restored
    assert conn._byte_cbs == []    # probe callback removed


def test_stream_probe_negative_on_garbage():
    conn = _StreamConn(reply_frame=b"not a valid frame at all")
    assert _probe_meshtastic_stream(conn, timeout=0.3) is False
    assert conn.raw is False


def test_stream_probe_negative_on_silence():
    conn = _StreamConn(reply_frame=None)
    assert _probe_meshtastic_stream(conn, timeout=0.3) is False


class _EchoConn:
    """A device that ECHOES serial input at a shell prompt (real behavior seen on classic ESP32s at COM34-36
    during HW test) — it reflects our exact want_config frame back, which naively decodes as a my_info."""

    def __init__(self):
        self.raw = False
        self._byte_cbs: list = []

    def on_bytes(self, cb):
        self._byte_cbs.append(cb)

    def remove_byte_callback(self, cb):
        if cb in self._byte_cbs:
            self._byte_cbs.remove(cb)

    def write_bytes(self, data):
        # echo verbatim, wrapped in shell junk like the real "#...\r\n> " prompt observed on hardware
        for cb in list(self._byte_cbs):
            cb(b"\x23" + bytes(data) + b"\r\n> ")


def test_stream_probe_rejects_echoed_want_config():
    # HW-grounded regression: an echoing shell reflects our field-3 want_config, which decodes as a my_info
    # with my_node_num=None. The detector must NOT treat that as Meshtastic (false positive found on COM34-36).
    assert _probe_meshtastic_stream(_EchoConn(), timeout=0.3) is False


# ── probe_device integration ───────────────────────────────────────────────────


def test_probe_device_identifies_meshtastic_via_stream():
    # Unknown device; the text probe gets no reply, but the StreamAPI answers → banner=meshtastic,
    # health=no-cli, firmware LEFT UNSET (so the caller's set_firmware fires on_device_changed).
    dev = Device(port="COM-TEST", firmware="")
    conn = _StreamConn(reply_frame=_from_radio_my_info())
    res = probe_device(conn, dev, timeout=0.15, settle=0.05)
    assert res.health == "no-cli"
    assert match_firmware(dev.fw_banner or "")[0] == "meshtastic"
    assert not dev.firmware  # NOT set directly


def test_probe_device_text_cli_device_is_not_meshtastic():
    # A device that answers 'help' with a Marauder banner is identified as marauder; the stream probe never
    # runs (already known), so no want_config is written to it.
    dev = Device(port="COM-TEST", firmware="")
    conn = _StreamConn(reply_frame=None, text_reply=["ESP32 Marauder v1.9.1", "  scanall"])
    res = probe_device(conn, dev, timeout=0.15, settle=0.05)
    assert dev.firmware == "marauder"
    assert res.health == "alive"
