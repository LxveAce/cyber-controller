"""BLE analyzer view (src/ui/qt/ble_analyzer_tab.py).

Two layers: the pure pixel-mapping helpers (no Qt) and the offscreen widget. The render check is
verify-never-fake — it proves the graph's ink RESPONDS to data (a device line adds pixels over the
empty grid), so the view can't be a static/fake visual.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from src.core.ble_analyzer import BleAnalyzerModel  # noqa: E402
from src.ui.qt.ble_analyzer_tab import (  # noqa: E402
    device_color,
    graph_devices,
    rssi_to_y,
    time_to_x,
)


# ── pure pixel mapping (no Qt) ──
def test_rssi_to_y_maps_and_clamps():
    # strong (top edge) -> top_px; noise floor (bottom edge) -> top_px + height
    assert rssi_to_y(-30, 10.0, 100.0) == 10.0
    assert rssi_to_y(-100, 10.0, 100.0) == 110.0
    assert rssi_to_y(-65, 0.0, 70.0) == pytest.approx(35.0, abs=0.5)  # midpoint-ish
    # out-of-range clamps to the edges, never off-canvas
    assert rssi_to_y(0, 10.0, 100.0) == 10.0        # stronger than top -> clamped to top
    assert rssi_to_y(-140, 10.0, 100.0) == 110.0    # below floor -> clamped to bottom


def test_time_to_x_newest_at_right():
    # now sits at the right edge; window start at the left; older clamps left
    assert time_to_x(1000.0, 1000.0, 60.0, 40.0, 600.0) == 640.0     # now -> right
    assert time_to_x(940.0, 1000.0, 60.0, 40.0, 600.0) == 40.0       # window start -> left
    assert time_to_x(500.0, 1000.0, 60.0, 40.0, 600.0) == 40.0       # older -> clamped left
    assert time_to_x(970.0, 1000.0, 60.0, 40.0, 600.0) == pytest.approx(340.0, abs=0.5)


def test_device_color_cycles():
    assert device_color(0) == device_color(8)       # palette of 8 wraps
    assert device_color(0) != device_color(1)


def test_graph_devices_picks_strong_fresh_in_window():
    m = BleAnalyzerModel()
    m.observe({"mac": "00:00:00:00:00:01", "rssi": -40}, now=100.0)  # strong, in window
    m.observe({"mac": "00:00:00:00:00:02", "rssi": -90}, now=100.0)  # weak, in window
    m.observe({"mac": "00:00:00:00:00:03", "rssi": -50}, now=1.0)    # strong but OLD sample
    m.observe({"mac": "00:00:00:00:00:04"}, now=100.0)              # no rssi -> no line
    lines = graph_devices(m, now=100.0, window_s=60.0, limit=6)
    addrs = [d.addr[-1] for d in lines]
    assert "1" in addrs and "2" in addrs        # both fresh with rssi get a line
    assert "3" not in addrs                      # sample outside the 60s window
    assert "4" not in addrs                      # no rssi, no line
    assert addrs[0] == "1"                        # strongest first


def test_graph_devices_respects_limit():
    m = BleAnalyzerModel()
    for i in range(10):
        m.observe({"mac": f"00:00:00:00:00:{i:02d}", "rssi": -40 - i}, now=100.0)
    assert len(graph_devices(m, now=100.0, limit=6)) == 6


# ── offscreen widget ──
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt5.QtWidgets")
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _make_tab(clock_val):
    from src.ui.qt.ble_analyzer_tab import BleAnalyzerTab
    tab = BleAnalyzerTab()
    tab.set_clock(lambda: clock_val[0])
    return tab


def test_widget_ingests_events_and_fills_table(qapp):
    clock = [1000.0]
    tab = _make_tab(clock)
    tab.on_ble_event("COM4", {"mac": "aa:bb:cc:dd:ee:01", "name": "Watch", "rssi": -55})
    tab.on_ble_event("COM23", {"addr": "aa:bb:cc:dd:ee:02", "rssi": -70, "tracker": 1})  # LxveOS
    assert tab.model.device_count == 2
    tab._refresh()
    assert tab._table.rowCount() == 2
    assert "2 present" in tab._header.text() and "1 tracker" in tab._header.text()


def test_biscuit_stat_grid_mirrors_the_summary(qapp):
    # A2: the analyzer's Biscuit stat tiles reflect the summary (Present/Seen/Trackers/Strongest).
    clock = [2000.0]
    tab = _make_tab(clock)
    tab.on_ble_event("COM4", {"mac": "aa:bb:cc:dd:ee:01", "name": "Watch", "rssi": -55})
    tab.on_ble_event("COM23", {"addr": "aa:bb:cc:dd:ee:02", "rssi": -70, "tracker": 1})
    tab._refresh()
    tiles = tab._stats._tiles
    assert tiles["Present"]._value.text() == "2"
    assert tiles["Seen"]._value.text() == "2"
    assert tiles["Trackers"]._value.text() == "1"
    assert tiles["Strongest"]._value.text() == "-55"     # the strongest RSSI seen
    # and the per-operation Help sheet spec is present (honest "what it does")
    from src.ui.qt.ble_analyzer_tab import _BLE_HELP
    assert _BLE_HELP["title"] == "BLE Analyzer" and _BLE_HELP["what_it_does"]


def test_idle_until_a_scan_feeds_it(qapp):
    # Regression (owner: the BLE analyzer "starts weird as if its already searching"): with nothing feeding
    # it, the view must read as idle rather than pose as a live scan. A ble_found event flips it to the live
    # summary; the graph's in-canvas text tracks the same "receiving" state.
    clock = [500.0]
    tab = _make_tab(clock)
    tab._refresh()
    assert "not scanning" in tab._header.text().lower()   # honest idle header on open, not a live-looking count
    assert tab._graph._receiving is False                 # graph draws "Idle — no BLE scan running"
    tab.on_ble_event("COM4", {"mac": "aa:bb:cc:dd:ee:01", "rssi": -55})
    tab._refresh()
    assert tab._graph._receiving is True                  # an arriving event = actively receiving
    assert "1 present" in tab._header.text()              # header flips to the live summary


def test_receiving_lapses_when_events_stop(qapp):
    # Past the active window with no new events, the view stops reporting itself as receiving (no false "live").
    clock = [500.0]
    tab = _make_tab(clock)
    tab.on_ble_event("COM4", {"mac": "aa:bb:cc:dd:ee:01", "rssi": -55})
    assert tab._is_receiving(clock[0]) is True
    assert tab._is_receiving(clock[0] + tab._ACTIVE_WINDOW_S + 1) is False


def test_render_native_ink_responds_to_data(qapp):
    from PyQt5.QtGui import QColor
    clock = [1000.0]
    tab = _make_tab(clock)

    def non_bg_pixels(img):
        bg = QColor("#0d1117").rgb()
        return sum(1 for x in range(0, img.width(), 5) for y in range(0, img.height(), 5)
                   if img.pixel(x, y) != bg)

    base = non_bg_pixels(tab.render_native(480, 240))    # empty: grid + "listening" text only
    # Feed a device a spread of samples across the window so its line has real extent.
    for k in range(8):
        clock[0] = 1000.0 + k * 5
        tab.on_ble_event("COM4", {"mac": "aa:bb:cc:dd:ee:aa", "name": "Beacon", "rssi": -45 + k})
    withline = non_bg_pixels(tab.render_native(480, 240))
    assert withline > base    # the device line added ink over the empty grid — a real draw


def test_pause_freezes_repaint_but_keeps_recording(qapp):
    clock = [1000.0]
    tab = _make_tab(clock)
    tab._on_pause(True)
    tab.on_ble_event("COM4", {"mac": "aa:bb:cc:dd:ee:01", "rssi": -55})  # recorded while paused
    tab._refresh()                                                       # no-op while paused
    assert tab._table.rowCount() == 0            # view frozen
    assert tab.model.device_count == 1           # but the model kept recording
    tab._on_pause(False)
    tab._refresh()
    assert tab._table.rowCount() == 1            # resume catches the table up


# ── A3: real Start/Stop via the shared broadcast engine + cross-talk ──
class _Cmd:
    def __init__(self, port, fw, cmd):
        self.port, self.firmware, self.command, self.pre_commands = port, fw, cmd, ()


class _Plan:
    def __init__(self, concrete):
        self.concrete, self.worst_danger, self.skipped = concrete, "", []


class _FakeEngine:
    """Minimal broadcast engine: BLE_SCAN -> the native scan verb; STOP_ALL -> its stop."""

    def __init__(self, concrete=1):
        self._concrete = concrete
        self.dispatched = []

    def plan(self, verb):
        from src.core.broadcast import BroadcastVerb
        cmd = "sniffbt" if verb == BroadcastVerb.BLE_SCAN else "stopscan"
        return _Plan([_Cmd("COM3", "marauder", cmd) for _ in range(self._concrete)])

    def dispatch(self, plan, confirmed=False):
        self.dispatched.append([c.command for c in plan.concrete])
        return []


def test_scan_controller_dispatches_scan_and_stop(qapp):
    import time
    from src.ui.qt.ble_analyzer_tab import BleScanController
    eng = _FakeEngine(concrete=2)
    ctrl = BleScanController(eng)
    assert ctrl.target_count() == 2                    # two BLE-capable devices connected
    assert ctrl.start() == 2                            # dispatched to both
    time.sleep(0.1)
    assert eng.dispatched == [["sniffbt", "sniffbt"]]  # each ran its native BLE-scan verb
    ctrl.stop()
    time.sleep(0.1)
    assert eng.dispatched[-1] == ["stopscan", "stopscan"]


def test_scan_controller_no_targets_sends_nothing(qapp):
    from src.ui.qt.ble_analyzer_tab import BleScanController
    ctrl = BleScanController(_FakeEngine(concrete=0))
    assert ctrl.target_count() == 0
    assert ctrl.start() == 0                            # nothing connected -> no phantom send


def test_analyzer_start_stop_pill_wiring(qapp):
    from src.ui.qt.ble_analyzer_tab import BleScanController
    tab = _make_tab([1000.0])
    tab._scan = BleScanController(_FakeEngine(concrete=1))
    tab._refresh()
    assert tab._scan_btn.isEnabled() and tab._scan_btn.text() == "Start"   # a target is present
    tab._on_start_scan()
    assert tab._scanning and tab._scan_btn.text() == "Stop"
    tab._on_stop_scan()
    assert not tab._scanning and tab._scan_btn.text() == "Start"


def test_analyzer_without_controller_disables_start(qapp):
    tab = _make_tab([1000.0])                           # no scan_controller injected
    assert tab._scan is None
    assert not tab._scan_btn.isEnabled()                # honest: can't start a scan with no engine
