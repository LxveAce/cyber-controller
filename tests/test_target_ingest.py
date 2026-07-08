"""Full cross-comm loop test: a device's serial scan line -> protocol parser -> TargetIngestor ->
TargetPool -> 'target.added' -> AutoRouter -> a command routed to ANOTHER device. This closes the
"one device gets an AP, another executes on it" path with the REAL parser, pool, ingestor, and router.
"""
from __future__ import annotations

from src.core.cross_comm import AutoRouter, EventBus, RoutingRule, TargetPool
from src.core.target_ingest import TargetIngestor
from src.models.target import TargetType
from src.protocols import get_protocol

# A representative Marauder AP scan line (matches src/protocols/marauder.py _RE_AP — note "Ch:" case).
_AP_LINE = "AP: MyLab BSSID: DE:AD:BE:EF:00:11 Ch: 6 RSSI: -42"


class _FakeConn:
    """Minimal stand-in for SerialConnection: records on_line callbacks and lets the test feed lines."""

    def __init__(self, port: str) -> None:
        self.port = port
        self._cbs = []

    def on_line(self, cb):
        self._cbs.append(cb)

    def feed(self, line: str) -> None:
        for cb in list(self._cbs):
            cb(line)


def _wire():
    bus = EventBus()
    pool = TargetPool(bus)
    routed: list[tuple[str, str]] = []
    router = AutoRouter(bus, lambda port, cmd: routed.append((port, cmd)))
    router.add_rule(RoutingRule(
        name="ap-to-deviceB", target_type=TargetType.AP, ssid_pattern="lab", min_rssi=-90,
        command_template="attack {mac} ch {channel}", device_port="COM_B", cooldown=0.0, enabled=True,
    ))
    ingest = TargetIngestor(pool)
    conn = _FakeConn("COM_A")
    ingest.attach(conn, get_protocol("marauder"))
    return conn, pool, routed


def test_opened_connection_auto_feeds_pool_via_hub():
    # Regression for "the Targets tab doesn't populate with AP's": the pool was fed ONLY when the Devices
    # tab called ingestor.attach on Connect — every other opener (Wardrive, Broadcast, an injected link)
    # left Targets empty while scans ran. CrossCommHub now auto-attaches the ingestor to every connection
    # the DeviceManager opens, so a scan line on ANY opened device lands in the pool with no Devices-tab
    # Connect. attach_connection is the injected-link path and it fires the same connection-opened hook.
    from src.core.cross_comm_hub import CrossCommHub
    from src.core.device_manager import DeviceManager
    from src.models.device import Device

    dm = DeviceManager()
    hub = CrossCommHub(dm)  # subscribes to on_connection_opened
    dev = Device(port="COM_W", name="Wardrive board", firmware="marauder")
    conn = _FakeConn("COM_W")
    dm.attach_connection(dev, conn)  # opens the link -> fires the hook -> hub attaches the ingestor

    assert hub.pool.count == 0
    conn.feed(_AP_LINE)  # a scan line arrives with NO Devices-tab Connect ever happening
    macs = [t.mac.lower() for t in hub.pool.all()]
    assert "de:ad:be:ef:00:11" in macs, "an opened device's scan must feed the shared pool automatically"


def test_parser_line_actually_matches():
    # Guard: confirm the test fixture line parses to an ap_found (so a regex drift fails loudly here).
    ev = get_protocol("marauder").parse_line(_AP_LINE)
    assert ev is not None and ev.event_type == "ap_found", ev
    assert ev.data["bssid"] == "DE:AD:BE:EF:00:11" and ev.data["channel"] == 6


def test_full_loop_serial_to_cross_device_command():
    conn, pool, routed = _wire()
    conn.feed(_AP_LINE)  # device A (COM_A) reports an AP over serial
    assert len(routed) == 1, f"expected exactly one routed command, got {routed}"
    port, cmd = routed[0]
    assert port == "COM_B"
    assert "de:ad:be:ef:00:11" in cmd.lower() and "ch 6" in cmd.lower(), cmd


def test_ap_landed_in_pool():
    conn, pool, _ = _wire()
    conn.feed(_AP_LINE)
    macs = [t.mac.lower() for t in pool.all()]
    assert "de:ad:be:ef:00:11" in macs


def test_non_matching_line_is_ignored():
    conn, _, routed = _wire()
    conn.feed("Scan complete")        # not an AP/client line
    conn.feed("> ready")
    assert routed == []


# ── Multi-firmware: HaleHound's richer events reach the pool (BLE/SubGHz/NFC/rogue) ──

def _ingest(protocol_name: str, lines: list[str]) -> TargetPool:
    bus = EventBus()
    pool = TargetPool(bus)
    conn = _FakeConn("COM_HH")
    TargetIngestor(pool).attach(conn, get_protocol(protocol_name))
    for ln in lines:
        conn.feed(ln)
    return pool


def test_halehound_ble_into_pool():
    pool = _ingest("halehound",
                   ["[BLE] Name: Watch | ADDR: AA:BB:CC:DD:EE:FF | RSSI: -60 | Type: Random"])
    bles = [t for t in pool.all() if t.target_type == TargetType.BLE]
    assert len(bles) == 1
    assert bles[0].mac == "AA:BB:CC:DD:EE:FF" and bles[0].ssid == "Watch"


def test_halehound_subghz_into_pool():
    pool = _ingest("halehound",
                   ["[SUBGHZ] Freq: 433.92MHz | Mod: ASK | Data: AA BB CC | RSSI: -30"])
    sg = [t for t in pool.all() if t.target_type == TargetType.SUBGHZ]
    assert len(sg) == 1 and "433.92" in sg[0].mac


def test_halehound_nfc_into_pool():
    pool = _ingest("halehound",
                   ["[NFC] UID: 04:AB:CD:EF:12:34:56 | ATQA: 0044 | SAK: 00 | Type: NTAG215"])
    nfc = [t for t in pool.all() if t.target_type == TargetType.NFC]
    assert len(nfc) == 1 and nfc[0].mac == "04:AB:CD:EF:12:34:56"


def test_halehound_rogue_ap_flagged_in_pool():
    pool = _ingest("halehound",
                   ["[GUARDIAN] ROGUE AP: EvilTwin | BSSID: 11:22:33:44:55:66 | CH: 6 | RSSI: -30"])
    aps = [t for t in pool.all() if t.target_type == TargetType.AP]
    assert len(aps) == 1
    assert aps[0].mac == "11:22:33:44:55:66" and aps[0].extra.get("rogue") is True


# ── Flipper RFID + NFC label (bug-hunt fixes #13/#15, #25) ────────────────────────────────────────

def test_flipper_rfid_becomes_rfid_target():
    # 125 kHz RFID was emitted as nfc_found without a uid and silently dropped; now it's a routable
    # RFID target keyed by the tag serial.
    pool = _ingest("flipper", ["RFID: Type: EM4100 | Data: 01 02 03 04 05"])
    rfids = [t for t in pool.all() if t.target_type == TargetType.RFID]
    assert len(rfids) == 1
    assert rfids[0].mac == "01 02 03 04 05" and rfids[0].ssid == "EM4100"


def test_flipper_nfc_label_uses_nfc_type():
    # The ingestor read 'type' but parsers emit 'nfc_type', so the label degraded to the SAK byte ("08").
    pool = _ingest("flipper", ["NFC: Type: Mifare Classic 1K | UID: 04:AB:CD:EF | ATQA: 0004 | SAK: 08"])
    nfcs = [t for t in pool.all() if t.target_type == TargetType.NFC]
    assert len(nfcs) == 1
    assert nfcs[0].ssid == "Mifare Classic 1K"
