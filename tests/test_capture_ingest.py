"""Auto-register ingest branch (punch-list #2, slice 2): the firmware capture events that
``_event_to_target`` drops (``handshake_captured`` / ``pmkid_captured`` / ``pcap_saved``) now
land in the shared ``CaptureStore`` via ``_event_to_capture``, with SSID/channel/RSSI joined
from the pool by BSSID — while non-capture lines still feed the target pool. No Qt / no hardware."""

from __future__ import annotations

from src.core.capture_store import CaptureStore
from src.core.cross_comm import EventBus, TargetPool
from src.core.target_ingest import TargetIngestor
from src.models.target import Target, TargetType
from src.protocols.base import ParsedEvent


class _Proto:
    """A stand-in protocol: parse_line looks the line up in a canned {line: ParsedEvent} map."""

    def __init__(self, events: dict[str, ParsedEvent]) -> None:
        self._events = events

    def parse_line(self, line: str) -> ParsedEvent | None:
        return self._events.get(line)


class _Conn:
    """A stand-in SerialConnection: records on_line callbacks and lets a test feed lines through."""

    def __init__(self, port: str) -> None:
        self.port = port
        self._cbs: list = []

    def on_line(self, cb) -> None:
        self._cbs.append(cb)

    def feed(self, line: str) -> None:
        for cb in self._cbs:
            cb(line)


def test_handshake_event_registers_capture_with_pool_join():
    pool = TargetPool(EventBus())
    # The AP was seen in a scan first, so the capture can join its SSID / channel / RSSI.
    pool.add(Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP,
                    ssid="HomeNet", channel=6, rssi=-42))
    store = CaptureStore(pool.bus)
    ing = TargetIngestor(pool, captures=store)
    ev = ParsedEvent(event_type="handshake_captured", data={"bssid": "AA:BB:CC:DD:EE:FF"}, raw="HS")
    conn = _Conn("COM7")
    ing.attach(conn, _Proto({"L": ev}))
    conn.feed("L")

    caps = store.all()
    assert len(caps) == 1
    c = caps[0]
    assert c.capture_type == "eapol" and c.bssid == "AA:BB:CC:DD:EE:FF"
    assert c.ssid == "HomeNet" and c.channel == 6 and c.rssi == -42   # joined from the pool
    assert c.device_source == "COM7" and c.raw == "HS"


def test_pmkid_event_registers_with_inline_hash():
    pool = TargetPool(EventBus())
    store = CaptureStore()
    ing = TargetIngestor(pool, captures=store)
    ev = ParsedEvent(event_type="pmkid_captured",
                     data={"bssid": "11:22:33:44:55:66", "pmkid": "cafebabedeadbeef"}, raw="PMKID")
    conn = _Conn("COM8")
    ing.attach(conn, _Proto({"L": ev}))
    conn.feed("L")

    c = store.get("pmkid:11:22:33:44:55:66")
    assert c is not None and c.capture_type == "pmkid" and c.pmkid == "cafebabedeadbeef"


def test_pcap_saved_attaches_to_recent_capture():
    pool = TargetPool(EventBus())
    store = CaptureStore()
    ing = TargetIngestor(pool, captures=store)
    hs = ParsedEvent(event_type="handshake_captured", data={"bssid": "AA:BB:CC:DD:EE:FF"}, raw="HS")
    pcap = ParsedEvent(event_type="pcap_saved", data={"path": "/sd/hs_01.pcapng"}, raw="PCAP")
    conn = _Conn("COM7")
    ing.attach(conn, _Proto({"HS": hs, "PCAP": pcap}))
    conn.feed("HS")
    conn.feed("PCAP")

    assert store.count == 1                                    # the pcap attaches, not a second row
    c = store.get("eapol:aa:bb:cc:dd:ee:ff")
    assert c is not None and c.pcap_path == "/sd/hs_01.pcapng"


def test_non_capture_line_still_feeds_pool_and_logs_no_capture():
    pool = TargetPool(EventBus())
    store = CaptureStore()
    ing = TargetIngestor(pool, captures=store)
    ap = ParsedEvent(event_type="ap_found",
                     data={"bssid": "AA:BB:CC:DD:EE:FF", "ssid": "Net", "channel": 6}, raw="AP")
    conn = _Conn("COM7")
    ing.attach(conn, _Proto({"AP": ap}))
    conn.feed("AP")

    assert store.count == 0                                    # not a capture
    assert pool.get("ap:AA:BB:CC:DD:EE:FF") is not None   # target still ingested (no regression)


def test_ghostesp_credential_capture_is_not_logged_as_handshake():
    # GhostESP's 'capture' event is an evil-portal credential grab (username/password), NOT a WPA
    # handshake — it must not pollute the handshake capture log.
    pool = TargetPool(EventBus())
    store = CaptureStore()
    ing = TargetIngestor(pool, captures=store)
    cred = ParsedEvent(event_type="capture", data={"type": "pw", "value": "hunter2"}, raw="CRED")
    conn = _Conn("COM7")
    ing.attach(conn, _Proto({"C": cred}))
    conn.feed("C")
    assert store.count == 0


def test_ingestor_without_captures_is_backward_compatible():
    # The Devices-tab constructs TargetIngestor(pool) with no store — a capture line must not crash.
    pool = TargetPool(EventBus())
    ing = TargetIngestor(pool)                                 # captures defaults to None
    hs = ParsedEvent(event_type="handshake_captured", data={"bssid": "AA:BB:CC:DD:EE:FF"}, raw="HS")
    conn = _Conn("COM7")
    ing.attach(conn, _Proto({"HS": hs}))
    conn.feed("HS")                                            # no store -> no-op, no exception


def test_hub_exposes_and_wires_capture_store():
    from src.core.cross_comm_hub import CrossCommHub
    from src.core.device_manager import DeviceManager

    hub = CrossCommHub(DeviceManager())
    assert isinstance(hub.captures, CaptureStore)
    assert hub.ingestor._captures is hub.captures   # the hub ingestor feeds the capture log


def test_pcap_attach_does_not_bump_times_seen():
    # Red-team fix: a handshake that also writes a pcap is ONE capture, not two — the file-attach
    # must not inflate times_seen (it now routes through CaptureStore.attach_file, not a full add).
    pool = TargetPool(EventBus())
    store = CaptureStore()
    ing = TargetIngestor(pool, captures=store)
    hs = ParsedEvent(event_type="handshake_captured", data={"bssid": "AA:BB:CC:DD:EE:FF"}, raw="HS")
    pcap = ParsedEvent(event_type="pcap_saved", data={"path": "/sd/hs.pcapng"}, raw="PCAP")
    conn = _Conn("COM7")
    ing.attach(conn, _Proto({"HS": hs, "PCAP": pcap}))
    conn.feed("HS")
    conn.feed("PCAP")
    c = store.get("eapol:aa:bb:cc:dd:ee:ff")
    assert c is not None and c.pcap_path == "/sd/hs.pcapng" and c.times_seen == 1


def test_second_unrelated_pcap_does_not_clobber_handshake_file():
    # Red-team fix: a LATER raw pcap on the same port (no intervening handshake) must NOT overwrite
    # the earlier handshake's pcap_path — the attach is one-shot; the raw pcap lands on its own row.
    pool = TargetPool(EventBus())
    store = CaptureStore()
    ing = TargetIngestor(pool, captures=store)
    hs = ParsedEvent(event_type="handshake_captured", data={"bssid": "AA:BB:CC:DD:EE:FF"}, raw="HS")
    p1 = ParsedEvent(event_type="pcap_saved", data={"path": "/sd/hs.pcapng"}, raw="P1")
    p2 = ParsedEvent(event_type="pcap_saved", data={"path": "/sd/raw_0.pcap"}, raw="P2")
    conn = _Conn("COM7")
    ing.attach(conn, _Proto({"HS": hs, "P1": p1, "P2": p2}))
    conn.feed("HS")
    conn.feed("P1")                    # attaches to the handshake, then pops recent-key (one-shot)
    conn.feed("P2")                    # a raw pcap, no handshake -> must not touch the HS record
    hs_rec = store.get("eapol:aa:bb:cc:dd:ee:ff")
    assert hs_rec is not None and hs_rec.pcap_path == "/sd/hs.pcapng"   # unchanged
    bare = store.get("eapol:")
    assert bare is not None and bare.pcap_path == "/sd/raw_0.pcap"       # logged separately


def test_detach_clears_recent_capture_state():
    # Red-team fix: detach must drop the port's pending pcap-attach target so a pcap written by the
    # NEXT device on that port can't be attached to the previous device's stale handshake record.
    pool = TargetPool(EventBus())
    store = CaptureStore()
    ing = TargetIngestor(pool, captures=store)
    hs = ParsedEvent(event_type="handshake_captured", data={"bssid": "AA:BB:CC:DD:EE:FF"}, raw="HS")
    conn = _Conn("COM7")
    ing.attach(conn, _Proto({"HS": hs}))
    conn.feed("HS")
    assert ing._recent_capture.get("COM7")          # armed
    ing.detach(conn)
    assert "COM7" not in ing._recent_capture
