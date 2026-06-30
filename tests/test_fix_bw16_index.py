"""BW16 right-click deauth-by-index (control-coverage): the Vampire scan prints index + SSID but NO BSSID,
so those APs used to be dropped from the pool. They now enter under a synthetic source-tagged key carrying
the scan index, and BW16 gets an {index}-based Deauth action that the resolver source-restricts to the BW16
that scanned it. Uses the real bw16 parser + ingestor + resolver."""

from __future__ import annotations

import types


class _FakeConn:
    def __init__(self, port: str) -> None:
        self.port = port
        self._cbs = []

    def on_line(self, cb):
        self._cbs.append(cb)

    def feed(self, line: str) -> None:
        for cb in list(self._cbs):
            cb(line)


def _bw16_ingest(port: str = "COM7"):
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.target_ingest import TargetIngestor
    from src.protocols import get_protocol
    bus = EventBus()
    pool = TargetPool(bus)
    ingest = TargetIngestor(pool)
    conn = _FakeConn(port)
    ingest.attach(conn, get_protocol("bw16"))
    return conn, pool


def test_bw16_indexonly_ap_enters_pool_with_synthetic_key():
    conn, pool = _bw16_ingest("COM7")
    conn.feed("3: MyLab (CH 36, RSSI -24)")          # Vampire scan: index + SSID, no BSSID
    aps = [t for t in pool.all() if t.ssid == "MyLab"]
    assert len(aps) == 1, "BW16 index-only AP should not be dropped"
    t = aps[0]
    assert t.extra.get("index") == 3                 # scan index carried for the deauth action
    assert t.mac == "idx:COM7:3"                      # synthetic, source-tagged key (includes the port)
    assert t.device_source == "COM7"


def _resolver(connected_port: str = "COM7"):
    from src.core import action_resolver as AR
    dev = types.SimpleNamespace(port=connected_port, firmware="bw16", name="bw16")
    dm = types.SimpleNamespace(list_connected=lambda: [dev])
    return AR.ActionResolver(dm)


def test_bw16_index_deauth_action_resolves_for_discovering_device():
    from src.models.target import Target, TargetType
    r = _resolver("COM7")
    t = Target(mac="idx:COM7:3", target_type=TargetType.AP, ssid="MyLab", device_source="COM7")
    t.extra["index"] = 3
    actions = r.resolve(t).get("COM7", [])
    deauth = [a for a in actions if "Deauth" in a.name]
    assert deauth and deauth[0].command_template == "AT+DEAUTHIDX=3"   # {index} substituted


def test_bw16_index_action_source_restricted():
    from src.models.target import Target, TargetType
    r = _resolver("COM7")
    # AP discovered by a DIFFERENT BW16 (COM9) -> COM7 must not offer the index action (wrong list).
    t = Target(mac="idx:COM9:3", target_type=TargetType.AP, ssid="MyLab", device_source="COM9")
    t.extra["index"] = 3
    assert r.resolve(t).get("COM7", []) == []
