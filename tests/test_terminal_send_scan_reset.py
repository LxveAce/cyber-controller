"""Gap #1 (hub 1.7.3 audit follow-up): the Devices-tab terminal Send is a SECOND door into the
device that writes the connection directly (`_on_send` -> `_active_conn.write`), bypassing the
routed CrossCommHub sink. A hand-typed `clearlist -a`/`reboot` there must ALSO flush the port's
parser scan ordinals, or a later `select -a {index}` (Deauth-AP) mis-binds to a stale index. These
tests prove the door is wired to `TargetIngestor.note_command_sent` — and an ordinary cmd leaves it.

Offscreen Qt. We build a real DeviceTab with a real shared ingestor + a real MarauderProtocol so the
assertion rides the actual `_on_send` code path, not a stubbed one.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.core.cross_comm import EventBus, TargetPool  # noqa: E402
from src.core.device_manager import DeviceManager  # noqa: E402
from src.core.target_ingest import TargetIngestor  # noqa: E402
from src.protocols.marauder import MarauderProtocol  # noqa: E402
from src.ui.qt.device_tab import DeviceTab  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeConn:
    """Enough of a SerialConnection for the ingestor to attach to and for `_on_send` to write to."""

    def __init__(self, port):
        self.port = port
        self.sent = []
        self.line_ending = "\r\n"
        self._cbs = []

    def on_line(self, cb):
        self._cbs.append(cb)

    def remove_line_callback(self, cb):
        if cb in self._cbs:
            self._cbs.remove(cb)

    def write(self, cmd):
        self.sent.append(cmd)

    def feed(self, *lines):
        for ln in lines:
            for cb in list(self._cbs):
                cb(ln)


def _ap(essid, bssid):
    return (f"ESSID: {essid}", f"BSSID: {bssid}", "RSSI: -50")


def _tab_with_active_conn(qapp, monkeypatch, port="COM_T"):
    # Never pop a modal in the offscreen test — clearlist isn't dangerous, but be explicit.
    import src.core.safety as safety
    monkeypatch.setattr(safety, "should_confirm", lambda *a, **k: False)

    bus = EventBus()
    pool = TargetPool(bus)
    ingestor = TargetIngestor(pool)
    dm = DeviceManager()
    tab = DeviceTab(dm, pool, ingestor)

    conn = _FakeConn(port)
    proto = MarauderProtocol()
    ingestor.attach(conn, proto)
    tab._active_port = port
    tab._active_conn = conn
    return tab, conn, proto


def test_terminal_clearlist_resets_parser_scan_index(qapp, monkeypatch):
    tab, conn, proto = _tab_with_active_conn(qapp, monkeypatch)

    conn.feed(*_ap("HomeNet", "aa:bb:cc:11:22:33"))
    conn.feed(*_ap("CoffeeShop", "dd:ee:ff:44:55:66"))
    assert proto._ap_indices == {"aa:bb:cc:11:22:33": 0, "dd:ee:ff:44:55:66": 1}

    # A hand-typed clear in the Devices-tab terminal must flush the ordinals (the second-door gap).
    tab._cmd_input.setText("clearlist -a")
    tab._on_send()
    assert conn.sent == ["clearlist -a"]           # the command really went to the device...
    assert proto._ap_indices == {}                 # ...AND the parser scan state was reset

    conn.feed(*_ap("NewNet", "99:88:77:66:55:44"))
    assert proto._ap_indices == {"99:88:77:66:55:44": 0}


def test_terminal_ordinary_command_does_not_reset(qapp, monkeypatch):
    tab, conn, proto = _tab_with_active_conn(qapp, monkeypatch)
    conn.feed(*_ap("HomeNet", "aa:bb:cc:11:22:33"))

    tab._cmd_input.setText("scanap")
    tab._on_send()
    assert conn.sent == ["scanap"]
    assert proto._ap_indices == {"aa:bb:cc:11:22:33": 0}  # untouched — only a clear/reboot resets
