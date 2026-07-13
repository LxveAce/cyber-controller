"""Regression: parser scan ordinals reset in the COMMAND SINK on a device list-clear, NOT on the UI
pool clear (hub 1.7.3 audit §A correction, owner-reported Bug A follow-up).

`MarauderProtocol.reset_scan_index()` (and the new GhostESP/Esp32Div equivalents) had zero production
callers, so `_ap_indices` went stale and a later `select -a {index}` (Deauth-AP) bound to the WRONG
AP after a device `clearlist -a`. The fix wires the reset into `CrossCommHub.send_to_port` — and
crucially NOT onto the UI `target.cleared` event, which sends no device command (resetting there
would desync the ordinals from the still-populated device list).
"""
from __future__ import annotations

from src.core.cross_comm_hub import CrossCommHub
from src.core.device_manager import DeviceManager
from src.protocols.esp32_div import Esp32DivProtocol
from src.protocols.ghost_esp import GhostESPProtocol
from src.protocols.marauder import MarauderProtocol


class _FakeConn:
    is_connected = True

    def __init__(self, port):
        self.port = port
        self._cbs = []

    def on_line(self, cb):
        self._cbs.append(cb)

    def remove_line_callback(self, cb):
        if cb in self._cbs:
            self._cbs.remove(cb)

    def feed(self, *lines):
        for ln in lines:
            for cb in list(self._cbs):
                cb(ln)


def _ap(essid, bssid):
    return (f"ESSID: {essid}", f"BSSID: {bssid}", "RSSI: -50")


def _hub_with_marauder(monkeypatch, port="COM_X"):
    """A hub with a Marauder parser attached to *port*, and send_to_port's connection/driver stubbed
    so a routed command reaches the scan-reset without real serial."""
    hub = CrossCommHub(DeviceManager())
    conn = _FakeConn(port)
    proto = MarauderProtocol()
    hub.ingestor.attach(conn, proto)

    # send_to_port needs a live connection + a delivering driver; stub both (assert the reset, not I/O).
    monkeypatch.setattr(hub.dm, "get_connection", lambda p: conn if p == port else None)
    monkeypatch.setattr(hub.dm, "get_device", lambda p: object())
    monkeypatch.setattr(
        "src.core.cross_comm_hub.driver_for",
        lambda dev: type("_D", (), {"deliver_text": staticmethod(lambda *a, **k: None)})(),
    )
    return hub, conn, proto, port


def test_clearlist_a_via_send_to_port_restarts_index_at_zero(monkeypatch):
    hub, conn, proto, port = _hub_with_marauder(monkeypatch)

    conn.feed(*_ap("HomeNet", "aa:bb:cc:11:22:33"))
    conn.feed(*_ap("CoffeeShop", "dd:ee:ff:44:55:66"))
    assert proto._ap_indices == {"aa:bb:cc:11:22:33": 0, "dd:ee:ff:44:55:66": 1}

    # The device's AP list is cleared through the routed command sink.
    hub.send_to_port(port, "clearlist -a")
    assert proto._ap_indices == {}

    # The next scan's first AP must be ordinal 0 again — a Deauth-AP {index} binds to the right row.
    conn.feed(*_ap("NewNet", "99:88:77:66:55:44"))
    assert proto._ap_indices == {"99:88:77:66:55:44": 0}


def test_ui_pool_clear_does_NOT_reset_parser(monkeypatch):
    """The audit's critical correction: a UI Clear All (`TargetPool.clear` → target.cleared) sends no
    device command, so the parser must KEEP its ordinals — else they desync from the device list."""
    hub, conn, proto, _port = _hub_with_marauder(monkeypatch)

    conn.feed(*_ap("HomeNet", "aa:bb:cc:11:22:33"))
    conn.feed(*_ap("CoffeeShop", "dd:ee:ff:44:55:66"))

    hub.pool.clear()  # publishes target.cleared

    # A re-observation must keep its original ordinal (device list unchanged), NOT restart at 0.
    conn.feed(*_ap("ThirdNet", "12:34:56:78:9a:bc"))
    assert proto._ap_indices["12:34:56:78:9a:bc"] == 2


def test_reboot_resets_marauder_index(monkeypatch):
    hub, conn, proto, port = _hub_with_marauder(monkeypatch)
    conn.feed(*_ap("HomeNet", "aa:bb:cc:11:22:33"))
    hub.send_to_port(port, "reboot")
    assert proto._ap_indices == {}


def test_ghostesp_and_esp32div_expose_resets():
    g = GhostESPProtocol()
    g._ap_indices["x"] = 5
    g._ap_index = 6
    g.reset_scan_index()
    assert g._ap_indices == {} and g._ap_index == 0

    d = Esp32DivProtocol()
    d._ap_indices["x"] = 1
    d._sta_indices["y"] = 2
    d._ap_index = 3
    d._sta_index = 4
    d.reset_scan_index()
    assert d._ap_indices == {} and d._ap_index == 0
    assert d._sta_indices == {"y": 2}  # station list untouched by an AP clear
    d.reset_station_index()
    assert d._sta_indices == {} and d._sta_index == 0
