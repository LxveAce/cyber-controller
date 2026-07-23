"""Tests for MultiWardriveController — concurrent multi-board wardrive (F1 slice 4a).

Pure controller (no Qt): N boards routed through a fake DeviceManager, one shared GPS, one merged
MultiWardriveSession. Verifies the routing/owner tags, per-firmware scan verbs, shared-GPS gating,
cross-board dedup, clean teardown, and that one bad board doesn't sink the rest of the deck.
"""
import io

import pytest

from src.core.wardrive_multi import MultiWardriveController

FIX = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"
AP1 = "BSSID:AA:BB:CC:DD:EE:01 RSSI:-40 Ch:1 ESSID:A"
AP2 = "BSSID:AA:BB:CC:DD:EE:02 RSSI:-50 Ch:6 ESSID:B"


class _FakeConn:
    def __init__(self, fail_write: bool = False) -> None:
        self._cbs: list = []
        self.written: list[str] = []
        self.line_ending = "\n"
        self._fail_write = fail_write

    def on_line(self, cb) -> None:
        self._cbs.append(cb)

    def remove_line_callback(self, cb) -> None:
        if cb in self._cbs:
            self._cbs.remove(cb)

    def write(self, data: str) -> None:
        if self._fail_write:
            raise OSError("write failed (device yanked mid-start)")
        self.written.append(data)

    def feed(self, line: str) -> None:
        for cb in list(self._cbs):
            cb(line)


class _FakeDM:
    def __init__(self, fail_ports=(), write_fail_ports=()) -> None:
        self.conns: dict[str, _FakeConn] = {}
        self.opened: list[tuple[str, str | None]] = []
        self.closed: list[tuple[str, str | None]] = []
        self._fail = set(fail_ports)
        self._write_fail = set(write_fail_ports)

    def open_connection(self, port: str, baud: int = 115200, owner=None) -> _FakeConn:
        if port in self._fail:
            raise OSError(f"Access is denied ({port})")
        self.opened.append((port, owner))
        return self.conns.setdefault(port, _FakeConn(fail_write=port in self._write_fail))

    def close_connection(self, port: str, owner=None) -> None:
        self.closed.append((port, owner))


def _ctrl(dm, gps="COM_GPS"):
    return MultiWardriveController(dm, io.StringIO(), gps_port=gps)


def test_no_cli_board_is_skipped_not_blind_written():
    # A Biscuit (BLE-app-driven, driver_type 'controlmap', no serial CLI) added to a wardrive must NOT be
    # blind-written scan verbs — that no-ops silently and looks like a dead board (the bug). It's skipped,
    # recorded, and never opened; the real text-cli board still runs.
    dm = _FakeDM()
    c = _ctrl(dm)
    c.add_board("COM3", firmware="marauder")
    c.add_board("COM6", firmware="biscuit")              # WROOM BLE gateway — no serial CLI
    c.start()
    assert ("COM3", "wardrive-multi") in dm.opened       # the real CLI board is driven
    assert "scanall" in dm.conns["COM3"].written
    assert "COM6" not in dm.conns                         # the Biscuit is never opened / written
    assert any(p == "COM6" and "no serial command CLI" in m for p, m in c.errors)


def test_two_boards_share_one_gps_and_merge():
    dm = _FakeDM()
    c = _ctrl(dm)
    c.add_board("COM3", firmware="marauder")
    c.add_board("COM4", firmware="")                     # unknown -> scanap default
    c.start()
    for tag in (("COM3", "wardrive-multi"), ("COM4", "wardrive-multi"), ("COM_GPS", "wardrive-multi")):
        assert tag in dm.opened                          # all via the dm, owner-tagged (no raw serial)
    assert "scanall" in dm.conns["COM3"].written         # marauder's native verb
    assert "scanap" in dm.conns["COM4"].written          # default verb
    dm.conns["COM_GPS"].feed(FIX)                         # ONE shared fix feeds every board
    dm.conns["COM3"].feed(AP1)
    dm.conns["COM4"].feed(AP2)
    assert c.ap_count == 2
    snap = c.snapshot()
    assert snap["total_aps"] == 2 and snap["running"] is True
    per = {b["port"]: b["aps"] for b in snap["boards"]}
    assert per == {"COM3": 1, "COM4": 1}
    c.stop()


def test_same_ap_from_two_boards_is_deduped():
    dm = _FakeDM()
    c = _ctrl(dm)
    c.add_board("COM3")
    c.add_board("COM4")
    c.start()
    dm.conns["COM_GPS"].feed(FIX)
    dm.conns["COM3"].feed(AP1)
    dm.conns["COM4"].feed(AP1)                            # same bssid on another board
    assert c.ap_count == 1                                # merged into one unique AP
    c.stop()


def test_no_fix_logs_nothing():
    dm = _FakeDM()
    c = _ctrl(dm)
    c.add_board("COM3")
    c.start()
    dm.conns["COM3"].feed(AP1)                            # no GPS fix yet
    assert c.ap_count == 0
    c.stop()


def test_stop_tears_down_all_and_ignores_late_lines():
    dm = _FakeDM()
    c = _ctrl(dm)
    c.add_board("COM3", firmware="marauder")
    c.add_board("COM4")
    c.start()
    dm.conns["COM_GPS"].feed(FIX)
    c.stop()
    assert "stopscan" in dm.conns["COM3"].written and "stopscan" in dm.conns["COM4"].written
    for tag in (("COM3", "wardrive-multi"), ("COM4", "wardrive-multi"), ("COM_GPS", "wardrive-multi")):
        assert tag in dm.closed                           # every owner ref released
    dm.conns["COM3"].feed(AP1)                            # a late line after stop must be ignored
    assert c.ap_count == 0


def test_one_bad_board_does_not_sink_the_deck():
    dm = _FakeDM(fail_ports=["COM_BAD"])
    c = _ctrl(dm)
    c.add_board("COM_BAD")
    c.add_board("COM_OK")
    c.start()
    assert any(p == "COM_BAD" for p, _ in c.errors)       # the bad board's failure is recorded, not raised
    dm.conns["COM_GPS"].feed(FIX)
    dm.conns["COM_OK"].feed(AP1)                          # the good board keeps capturing
    assert c.ap_count == 1
    snap = c.snapshot()
    started = {b["port"]: b["started"] for b in snap["boards"]}
    assert started == {"COM_BAD": False, "COM_OK": True}
    c.stop()


def test_board_failing_mid_start_is_torn_down_not_leaked():
    # open_connection succeeds and the reader callback is registered, but a scan-start write() raises
    # (e.g. the ESP32 is yanked mid-start). The board must not be left open/scanning under our owner tag.
    dm = _FakeDM(write_fail_ports=["COM_WF"])
    c = _ctrl(dm)
    c.add_board("COM_WF")
    c.add_board("COM_OK")
    c.start()
    assert any(p == "COM_WF" for p, _ in c.errors)            # the write failure is recorded, not raised
    conn = dm.conns["COM_WF"]
    assert ("COM_WF", "wardrive-multi") in dm.closed          # connection reclaimed (not leaked)
    assert conn._cbs == []                                    # reader callback removed (device not left scanning)
    dm.conns["COM_GPS"].feed(FIX)                             # the good board keeps capturing
    dm.conns["COM_OK"].feed(AP1)
    assert c.ap_count == 1
    snap = c.snapshot()
    started = {b["port"]: b["started"] for b in snap["boards"]}
    assert started == {"COM_WF": False, "COM_OK": True}       # the failed board reports not-started
    c.stop()
    assert dm.closed.count(("COM_WF", "wardrive-multi")) == 1  # stop() doesn't double-close the reclaimed port


def test_add_board_after_start_raises():
    dm = _FakeDM()
    c = _ctrl(dm)
    c.add_board("COM3")
    c.start()
    with pytest.raises(RuntimeError):
        c.add_board("COM4")
    c.stop()
