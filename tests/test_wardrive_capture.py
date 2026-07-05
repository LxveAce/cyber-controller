"""Tests for _WardriveCapture — GPS-tagged capture routed through the DeviceManager (F1 slice 2).

The point of this slice is the bugfix: capture must borrow its ports from the shared DeviceManager (with an
owner tag) instead of opening its own serial.Serial(), which collides with any board already open elsewhere
(Windows COM ports are exclusive). A fake DeviceManager stands in for the real one so the routing, the
line-callback flow, and the teardown are all verified without hardware.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt import wardrive_tab  # noqa: E402

FIX = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
AP = "BSSID:AA:BB:CC:DD:EE:01 RSSI:-40 Ch:1 ESSID:Net"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeConn:
    def __init__(self) -> None:
        self._cbs: list = []
        self.written: list[str] = []

    def on_line(self, cb) -> None:
        self._cbs.append(cb)

    def remove_line_callback(self, cb) -> None:
        if cb in self._cbs:
            self._cbs.remove(cb)

    def write(self, data: str) -> None:
        self.written.append(data)

    def feed(self, line: str) -> None:          # simulate the DM reader pushing a line
        for cb in list(self._cbs):
            cb(line)


class _FakeDM:
    def __init__(self) -> None:
        self.conns: dict[str, _FakeConn] = {}
        self.opened: list[tuple[str, str | None]] = []
        self.closed: list[tuple[str, str | None]] = []

    def open_connection(self, port: str, baud: int = 115200, owner=None) -> _FakeConn:
        self.opened.append((port, owner))
        return self.conns.setdefault(port, _FakeConn())

    def close_connection(self, port: str, owner=None) -> None:
        self.closed.append((port, owner))


def _capture(dm, tmp_path, gps="COM_GPS"):
    out = str(tmp_path / "drive.csv")
    cap = wardrive_tab._WardriveCapture(dm, gps, 9600, "COM_DEV", 115200, out)
    return cap, out


def test_capture_opens_both_ports_via_dm_with_owner_tag(qapp, tmp_path):
    dm = _FakeDM()
    cap, _ = _capture(dm, tmp_path)
    cap.start()
    assert ("COM_DEV", "wardrive") in dm.opened
    assert ("COM_GPS", "wardrive") in dm.opened          # never a raw serial.Serial() -> no COM clash
    assert "scanap\n" in dm.conns["COM_DEV"].written      # scan kicked off on the device port
    cap.stop()


def test_capture_logs_ap_after_gps_fix(qapp, tmp_path):
    dm = _FakeDM()
    cap, out = _capture(dm, tmp_path)
    cap.start()
    dm.conns["COM_GPS"].feed(FIX)                         # shared GPS fix arrives on its reader thread
    dm.conns["COM_DEV"].feed(AP)                          # AP arrives on the device reader thread
    assert cap._sess.ap_count == 1
    cap.stop()
    with open(out, encoding="utf-8") as fh:
        text = fh.read()
    assert "AA:BB:CC:DD:EE:01" in text                    # written to the WiGLE CSV


def test_capture_no_fix_logs_nothing(qapp, tmp_path):
    dm = _FakeDM()
    cap, _ = _capture(dm, tmp_path)
    cap.start()
    dm.conns["COM_DEV"].feed(AP)                          # AP with no GPS fix yet
    assert cap._sess.ap_count == 0
    cap.stop()


def test_capture_stop_releases_owner_and_stops_scan(qapp, tmp_path):
    dm = _FakeDM()
    cap, _ = _capture(dm, tmp_path)
    stopped = []
    cap.stopped.connect(lambda: stopped.append(True))
    cap.start()
    cap.stop()
    assert "stopscan\n" in dm.conns["COM_DEV"].written
    assert ("COM_DEV", "wardrive") in dm.closed
    assert ("COM_GPS", "wardrive") in dm.closed           # our owner ref released on both ports
    assert stopped == [True]
    dm.conns["COM_DEV"].feed(AP)                           # a late line after stop must be ignored, not crash
    assert cap._sess.ap_count == 0


def test_capture_without_gps_opens_only_device(qapp, tmp_path):
    dm = _FakeDM()
    cap, _ = _capture(dm, tmp_path, gps="")
    cap.start()
    assert ("COM_DEV", "wardrive") in dm.opened
    assert "COM_GPS" not in dm.conns                       # no GPS port -> only the device is opened
    cap.stop()
