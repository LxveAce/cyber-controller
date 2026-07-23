"""Wi-Fi analyzer view (src/ui/qt/wifi_analyzer_tab.py).

Two layers: the pure pixel-mapping helper (no Qt) and the offscreen widget. The render check is
verify-never-fake — it proves the channel graph's ink RESPONDS to data
(occupied channels add bars over
the empty baseline), so the view can't be a static/fake visual.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from src.core.wifi_analyzer import WifiAnalyzerModel  # noqa: E402
from src.ui.qt.wifi_analyzer_tab import channel_bars  # noqa: E402


# ── pure channel-view mapping (no Qt) ──
def test_channel_bars_baseline_and_counts():
    m = WifiAnalyzerModel()
    m.observe("ap_found", {"bssid": "00:00:00:00:00:01", "channel": 6, "rssi": -50}, now=100.0)
    m.observe("ap_found", {"bssid": "00:00:00:00:00:02", "channel": 6, "rssi": -70}, now=100.0)
    m.observe("ap_found", {"bssid": "00:00:00:00:00:03", "channel": 11, "rssi": -60}, now=100.0)
    bars = channel_bars(m, now=100.0)
    by_ch = {ch: (count, strongest) for ch, count, strongest in bars}
    assert [ch for ch, _c, _s in bars][:14] == list(range(1, 15))  # 2.4 GHz baseline axis, in order
    assert by_ch[6] == (2, -50)      # two APs on ch6, strongest -50
    assert by_ch[11] == (1, -60)
    assert by_ch[1] == (0, None)     # an empty channel is present with a zero bar


def test_channel_bars_appends_out_of_band_channel():
    m = WifiAnalyzerModel()
    m.observe("ap_found", {"bssid": "00:00:00:00:00:01", "channel": 36, "rssi": -55}, now=1.0)
    channels = [ch for ch, _c, _s in channel_bars(m, now=1.0)]
    assert channels[:14] == list(range(1, 15)) and 36 in channels   # appended after the baseline


# ── offscreen widget ──
@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt5.QtWidgets")
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _make_tab(clock_val):
    from src.ui.qt.wifi_analyzer_tab import WifiAnalyzerTab
    tab = WifiAnalyzerTab()
    tab.set_clock(lambda: clock_val[0])
    return tab


def test_widget_ingests_events_and_fills_table(qapp):
    clock = [1000.0]
    tab = _make_tab(clock)
    tab.on_wifi_event("COM4", "ap_found",
                      {"bssid": "aa:bb:cc:dd:ee:01", "ssid": "Home", "channel": 6, "rssi": -55,
                       "encryption": "WPA2"})
    tab.on_wifi_event("COM4", "ap_found",
                      {"bssid": "aa:bb:cc:dd:ee:02", "ssid": "", "channel": 1, "rssi": -70,
                       "auth": "open"})   # LxveOS-shaped open network
    tab.on_wifi_event("COM4", "handshake_captured", {"bssid": "aa:bb:cc:dd:ee:01"})
    assert tab.model.ap_count == 2
    tab._refresh()
    assert tab._table.rowCount() == 2
    assert "2 present" in tab._header.text() and "1 open" in tab._header.text()
    assert "1 handshake" in tab._header.text()


def test_stat_grid_mirrors_the_summary(qapp):
    clock = [2000.0]
    tab = _make_tab(clock)
    tab.on_wifi_event("COM4", "ap_found",
                      {"bssid": "aa:bb:cc:dd:ee:01", "ssid": "Home", "rssi": -55, "auth": "open"})
    tab.on_wifi_event("COM4", "client_found",
                      {"mac": "11:22:33:44:55:66", "bssid": "aa:bb:cc:dd:ee:01"})
    tab.on_wifi_event("COM4", "handshake_captured", {"bssid": "aa:bb:cc:dd:ee:01"})
    tab._refresh()
    tiles = tab._stats._tiles
    assert tiles["Present"]._value.text() == "1"
    assert tiles["Seen"]._value.text() == "1"
    assert tiles["Open"]._value.text() == "1"
    assert tiles["Clients"]._value.text() == "1"
    assert tiles["Handshakes"]._value.text() == "1"
    assert tiles["Strongest"]._value.text() == "-55"


def test_idle_until_a_scan_feeds_it(qapp):
    # Honest empty state: with nothing feeding it,
    # the view reads as idle rather than posing as a live
    # scan. An ap_found event flips it to the live summary;
    # the graph's in-canvas text tracks the same
    # "receiving" state.
    clock = [500.0]
    tab = _make_tab(clock)
    tab._refresh()
    assert "not scanning" in tab._header.text().lower()   # honest idle header on open
    assert tab._graph._receiving is False  # graph draws "Idle — no Wi-Fi scan running"
    tab.on_wifi_event("COM4", "ap_found", {"bssid": "aa:bb:cc:dd:ee:01", "channel": 6, "rssi": -55})
    tab._refresh()
    assert tab._graph._receiving is True                  # an arriving event = actively receiving
    assert "1 present" in tab._header.text()              # header flips to the live summary


def test_receiving_lapses_when_events_stop(qapp):
    clock = [500.0]
    tab = _make_tab(clock)
    tab.on_wifi_event("COM4", "ap_found", {"bssid": "aa:bb:cc:dd:ee:01", "channel": 6, "rssi": -55})
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

    base = non_bg_pixels(tab.render_native(480, 200))   # empty: baseline + "idle" text only
    # Feed APs across several channels so the channel graph draws real bars.
    for ch in (1, 6, 11):
        for k in range(3):
            tab.on_wifi_event("COM4", "ap_found",
                          {"bssid": f"aa:bb:cc:00:{ch:02d}:{k:02d}", "channel": ch, "rssi": -45})
    withbars = non_bg_pixels(tab.render_native(480, 200))
    assert withbars > base    # the channel bars added ink over the empty baseline — a real draw


def test_pause_freezes_repaint_but_keeps_recording(qapp):
    clock = [1000.0]
    tab = _make_tab(clock)
    tab._on_pause(True)
    tab.on_wifi_event("COM4", "ap_found", {"bssid": "aa:bb:cc:dd:ee:01", "rssi": -55})
    tab._refresh()                                                       # no-op while paused
    assert tab._table.rowCount() == 0            # view frozen
    assert tab.model.ap_count == 1               # but the model kept recording
    tab._on_pause(False)
    tab._refresh()
    assert tab._table.rowCount() == 1            # resume catches the table up


def test_clear_empties_the_model(qapp):
    clock = [1000.0]
    tab = _make_tab(clock)
    tab.on_wifi_event("COM4", "ap_found", {"bssid": "aa:bb:cc:dd:ee:01", "rssi": -55})
    tab._on_clear()
    assert tab.model.ap_count == 0 and tab._table.rowCount() == 0


def test_help_documents_every_stat_tile(qapp):
    from src.ui.qt.wifi_analyzer_tab import _WIFI_HELP
    help_stats = {name for _icon, name, _desc in _WIFI_HELP["statistics"]}
    grid_labels = {"Present", "Seen", "Open", "Clients", "Handshakes", "Strongest"}
    assert grid_labels <= help_stats
    assert _WIFI_HELP["title"] == "Wi-Fi Analyzer" and _WIFI_HELP["what_it_does"]
