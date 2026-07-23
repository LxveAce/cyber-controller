"""Wi-Fi analyzer view — the live access-point table + channel view (Stream-A output view).

Renders the on-device Wi-Fi-analyzer visual from the firmware-agnostic WifiAnalyzerModel:
a per-channel
occupancy graph (the classic 2.4 GHz channel view) plus a live AP table (signal bars, SSID, BSSID,
channel, encryption, client count, and a handshake-captured tick). Fed by the TargetIngestor
event-observer tap, so every scanning firmware's ap_found / rogue_ap / client_found /
handshake_captured / pmkid_captured events populate it (Marauder / Ghost / HaleHound / DIV / BW16 /
LxveOS). Awareness-only: it visualizes what's out there and drives no device —
it transmits nothing and
has no scan control of its own; it just shows what a scan running elsewhere finds.

The pure core (WifiAnalyzerModel + the pixel-mapping helpers here) is Qt-free and unit-testable; the
widget is import-guarded and offscreen-renderable via render_native() for a windowless visual test.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

from src.core.wifi_analyzer import WifiAnalyzerModel

# ── pure pixel mapping (Qt-free, unit-testable) ──
# The channel view always shows 2.4 GHz channels 1-14 as a stable, recognizable axis;
# any higher (5 GHz)
# channel a firmware actually reports is appended so it isn't lost.
_BASE_CHANNELS: Tuple[int, ...] = tuple(range(1, 15))


def channel_bars(model: WifiAnalyzerModel, now: float, ttl: float = 30.0,
                 fresh_only: bool = True) -> "List[Tuple[int, int, Optional[int]]]":
    """The channel-view bars: an ordered list of (channel, ap_count, strongest_rssi).
    Pure so the view
    and its test agree on what's drawn. Channels 1-14 form the baseline;
    any extra channel that carries
    an AP is appended in ascending order."""
    occ = model.channel_occupancy(now, ttl, fresh_only)
    channels = list(_BASE_CHANNELS)
    for ch in sorted(occ):
        if ch not in channels:
            channels.append(ch)
    out: "List[Tuple[int, int, Optional[int]]]" = []
    for ch in channels:
        count = occ.get(ch, 0)
        strongest = model.strongest_on_channel(ch, now, ttl, fresh_only) if count else None
        out.append((ch, count, strongest))
    return out


def _rssi_color(rssi: "Optional[float]") -> str:
    """Color-grade an RSSI: green strong → orange medium → red weak; muted if none."""
    if rssi is None:
        return "#8b949e"
    if rssi >= -60:
        return "#3fb950"
    if rssi >= -80:
        return "#f0883e"
    return "#f85149"


# Per-operation Help sheet (Biscuit pattern) — honest "what it does" for the analyzer.
_WIFI_HELP = {
    "title": "Wi-Fi Analyzer",
    "summary": "A passive, firmware-agnostic view of the Wi-Fi access points a connected device "
               "reports — a channel-occupancy graph + a dedup AP table with captured-handshake "
               "flags (transmits nothing, drives nothing).",
    "what_it_does": [
        ("📡", "AP Capture",
         "Folds in every access point the firmware reports (Marauder / GhostESP / "
         "HaleHound / DIV / BW16 / LxveOS)."),
        ("📊", "Channel View", "How many APs sit on each 2.4 GHz channel, by signal."),
        ("🧹", "De-duplicated Table", "One row per AP (by BSSID): SSID, channel, enc, "
         "associated-client count."),
        ("🔑", "Handshake Flag", "Ticks an AP once its WPA handshake or PMKID is captured."),
    ],
    "statistics": [
        ("👁", "Present", "APs seen in the last few seconds (a live scan is feeding the view)."),
        ("Σ", "Seen", "Total distinct APs observed this session."),
        ("🔓", "Open", "How many report an open (unencrypted) network."),
        ("💻", "Clients", "Distinct client stations seen this session."),
        ("🔑", "Handshakes", "APs with a captured WPA handshake or PMKID."),
        ("📶", "Strongest", "The closest AP's RSSI (higher / greener = nearer)."),
    ],
    "tips": [
        "Start a Wi-Fi scan on a connected device — this view only shows what the scan finds.",
        "Sort by Channel to spot a congested channel; by Most clients to find a busy AP.",
        "Pause freezes the view without losing data — recording continues in the background.",
    ],
}


# ── Qt widget (the pure core above stays Qt-free) ──
try:
    from PyQt5.QtCore import QRectF, Qt, QTimer
    from PyQt5.QtGui import QBrush, QColor, QImage, QPainter, QPen
    from PyQt5.QtWidgets import (
        QAbstractItemView,
        QComboBox,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QPushButton,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )

    from src.ui.qt.biscuit import HelpSheet, StatGrid
    from src.ui.qt.widgets.signal_bars import SignalBarsDelegate

    _BG = QColor("#0d1117")
    _GRID = QColor("#21262d")
    _AXIS_TEXT = QColor("#6e7681")
    _BAR_EMPTY = QColor("#21262d")
    _ROGUE = QColor("#f85149")
    _OPEN = QColor("#f0883e")
    _CAPTURE = QColor("#3fb950")

    class _ChannelGraph(QWidget):
        """A per-channel AP-occupancy bar graph (the 2.4 GHz channel view): one bar per channel, its
        height the AP count and its color the strongest signal on that channel. Reads the model live
        each paint (offscreen-renderable via render_native())."""

        def __init__(self, model: WifiAnalyzerModel, parent: "Optional[QWidget]" = None) -> None:
            super().__init__(parent)
            self._model = model
            self._now_fn = time.monotonic          # injectable clock (tests override it)
            self._receiving = False  # True only while a scan is feeding events
            self.setMinimumHeight(150)

        def set_clock(self, fn) -> None:
            self._now_fn = fn

        def paintEvent(self, _ev) -> None:  # noqa: N802 (Qt override)
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            self._paint(p, self.width(), self.height())
            p.end()

        def _paint(self, p: "QPainter", w: int, h: int) -> None:
            p.fillRect(0, 0, w, h, _BG)
            left, top = 28.0, 8.0
            gw, gh = max(1.0, w - left - 10.0), max(1.0, h - top - 22.0)
            now = self._now_fn()
            base = top + gh  # y of the channel baseline

            bars = channel_bars(self._model, now)
            max_count = max((c for _ch, c, _r in bars), default=0)
            if max_count <= 0:
                # No APs on any channel. Say so honestly:
                # only claim "listening" when a scan is actually
                # feeding events, otherwise the view would pose as a live scan the moment it opened.
                p.setPen(QPen(_GRID, 1))
                p.drawLine(int(left), int(base), int(left + gw), int(base))
                msg = ("Listening for access points…" if getattr(self, "_receiving", False)
                       else "Idle — no Wi-Fi scan running")
                p.setPen(QPen(_AXIS_TEXT, 1))
                p.drawText(QRectF(left, top, gw, gh), Qt.AlignCenter, msg)
                return

            slot = gw / len(bars)
            bar_w = max(2.0, slot * 0.6)
            p.setPen(QPen(_GRID, 1))
            p.drawLine(int(left), int(base), int(left + gw), int(base))
            for i, (ch, count, strongest) in enumerate(bars):
                cx = left + slot * (i + 0.5)
                if count > 0:
                    bh = (count / max_count) * (gh - 4)
                    color = QColor(_rssi_color(strongest))
                    p.setBrush(QBrush(color))
                    p.setPen(QPen(color, 1))
                    p.drawRect(QRectF(cx - bar_w / 2, base - bh, bar_w, bh))
                    p.setPen(QPen(_AXIS_TEXT, 1))
                    p.drawText(QRectF(cx - slot / 2, base - bh - 14, slot, 12),
                               Qt.AlignCenter, str(count))
                else:
                    p.setPen(QPen(_BAR_EMPTY, 1))
                    p.drawLine(int(cx), int(base), int(cx), int(base - 2))
                # Channel number under the baseline.
                p.setPen(QPen(_AXIS_TEXT, 1))
                p.drawText(QRectF(cx - slot / 2, base + 3, slot, 14), Qt.AlignCenter, str(ch))

    class WifiAnalyzerTab(QWidget):
        """Live Wi-Fi analyzer: channel-occupancy graph + AP table, fed by ap_found / client_found /
        handshake events from any firmware. Holds its own WifiAnalyzerModel; on_wifi_event(port,
        event_type, data) folds one event in. Passive —
        it has no scan control and transmits nothing."""

        _COLS = ("Signal", "SSID", "BSSID", "Ch", "Enc", "Clients", "HS")
        _ACTIVE_WINDOW_S = 10.0   # a Wi-Fi event within this many seconds = actively feeding

        def __init__(self, parent: "Optional[QWidget]" = None) -> None:
            super().__init__(parent)
            self._model = WifiAnalyzerModel()
            self._now_fn = time.monotonic
            self._sort = "rssi"
            self._paused = False
            self._last_event_ts: "Optional[float]" = None  # when we last folded in a Wi-Fi event

            root = QVBoxLayout(self)
            self._header = QLabel(
                "Not scanning. Start a Wi-Fi scan on a connected device to see access points.")
            self._header.setStyleSheet("color:#8b949e;")
            self._header.setWordWrap(True)
            root.addWidget(self._header)

            # Live stat grid (Biscuit statistics pattern) — mirrors the header summary as tiles.
            self._stats = StatGrid(
                ["Present", "Seen", "Open", "Clients", "Handshakes", "Strongest"], columns=6)
            root.addWidget(self._stats)

            self._graph = _ChannelGraph(self._model)
            root.addWidget(self._graph, 1)

            ctl = QHBoxLayout()
            self._sort_combo = QComboBox()
            self._sort_combo.addItems(["Strongest", "Recent", "Channel", "Most clients", "Name"])
            self._sort_combo.setToolTip("Order the access-point table.")
            self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
            self._btn_pause = QPushButton("Pause")
            self._btn_pause.setToolTip("Freeze the view (recording continues in the background).")
            self._btn_pause.setCheckable(True)
            self._btn_pause.toggled.connect(self._on_pause)
            self._btn_clear = QPushButton("Clear")
            self._btn_clear.setToolTip("Forget all tracked access points and start fresh.")
            self._btn_clear.clicked.connect(self._on_clear)
            ctl.addWidget(QLabel("Sort:"))
            ctl.addWidget(self._sort_combo)
            ctl.addWidget(self._btn_pause)
            ctl.addWidget(self._btn_clear)
            ctl.addStretch(1)
            self._btn_help = QPushButton("?")
            self._btn_help.setFixedWidth(28)
            self._btn_help.setToolTip("What the Wi-Fi Analyzer does")
            self._btn_help.clicked.connect(lambda: HelpSheet(_WIFI_HELP, self).exec_())
            ctl.addWidget(self._btn_help)
            root.addLayout(ctl)

            self._table = QTableWidget(0, len(self._COLS))
            self._table.setHorizontalHeaderLabels(list(self._COLS))
            self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self._table.verticalHeader().setVisible(False)
            self._table.setItemDelegateForColumn(0, SignalBarsDelegate(self._table))
            hdr = self._table.horizontalHeader()
            hdr.setSectionResizeMode(1, QHeaderView.Stretch)  # let SSID take the slack
            root.addWidget(self._table, 2)

            self._timer = QTimer(self)
            self._timer.setInterval(1000)
            self._timer.timeout.connect(self._refresh)
            self._timer.start()

        # ── data in ──
        def set_clock(self, fn) -> None:
            """Override the clock (tests inject a deterministic one)."""
            self._now_fn = fn
            self._graph.set_clock(fn)

        def on_wifi_event(self, port: str, event_type: str, data: dict) -> None:
            """Fold one Wi-Fi event into the model. GUI-thread only
            (marshal serial-thread events via a
            signal first). Recording continues while paused;
            Pause only freezes the repaint, so no data
            is lost while paused."""
            try:
                now = self._now_fn()
                self._model.observe(event_type, data, now)
                self._last_event_ts = now  # marks the view as actively receiving
            except Exception:  # noqa: BLE001 — a bad event must never break the view
                pass

        @property
        def model(self) -> WifiAnalyzerModel:
            return self._model

        # ── controls ──
        def _on_sort_changed(self, idx: int) -> None:
            self._sort = ("rssi", "recent", "channel", "clients", "ssid")[idx]
            self._refresh()

        def _on_pause(self, checked: bool) -> None:
            self._paused = checked
            self._btn_pause.setText("Resume" if checked else "Pause")

        def _on_clear(self) -> None:
            self._model.clear()
            self._refresh()

        # ── render ──
        def _is_receiving(self, now: float) -> bool:
            """True when a Wi-Fi event arrived recently — i.e. a scan is actually feeding this view.
            Drives the honest empty state so an idle analyzer never poses as a live scan."""
            return (self._last_event_ts is not None
                    and (now - self._last_event_ts) <= self._ACTIVE_WINDOW_S)

        def _refresh(self) -> None:
            if self._paused:
                return
            now = self._now_fn()
            receiving = self._is_receiving(now)
            self._graph._receiving = receiving
            s = self._model.summary(now)
            if s["total"] == 0 and not receiving:
                # Nothing seen and no scan feeding us — say so plainly
                # and name the prerequisite, since
                # this view has no Start of its own (a scan is started on a device elsewhere).
                self._header.setText(
                    "Not scanning. Start a Wi-Fi scan on a connected device to see access points.")
            else:
                strongest = "—" if s["strongest"] is None else f"{s['strongest']} dBm"
                self._header.setText(
                    f"{s['fresh']} present · {s['total']} seen · {s['open']} open · "
                    f"{s['handshakes']} handshakes · {s['clients']} clients · top {strongest}")
            self._update_stats(s, receiving)
            self._fill_table(now)
            self._graph.update()

        def _update_stats(self, s: dict, receiving: bool) -> None:
            """Mirror the summary into the Biscuit stat tiles (color-graded, honest when idle)."""
            _green, _orange, _muted = "#3fb950", "#f0883e", "#8b949e"
            strongest = s["strongest"]
            self._stats.set_stats({
                "Present": (s["fresh"], _green if (receiving and s["fresh"]) else _muted),
                "Seen": s["total"],
                "Open": (s["open"], _orange if s["open"] else _muted),
                "Clients": s["clients"],
                "Handshakes": (s["handshakes"], _green if s["handshakes"] else _muted),
                "Strongest": ("—" if strongest is None else f"{strongest}", _rssi_color(strongest)),
            })

        def _fill_table(self, now: float) -> None:
            aps = self._model.access_points(sort=self._sort)
            self._table.setRowCount(len(aps))
            for row, ap in enumerate(aps):
                fresh = ap.freshness(now)
                cells = [
                    "" if ap.rssi is None else str(ap.rssi),   # SignalBarsDelegate reads the RSSI
                    ap.display_ssid(),
                    ap.bssid,
                    "" if ap.channel is None else str(ap.channel),
                    ap.enc_label(),
                    str(ap.client_count()),
                    "✓" if ap.has_capture() else "",
                ]
                for col, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    if col == 1 and ap.rogue:
                        item.setForeground(_ROGUE)
                        item.setToolTip("Flagged as a rogue / evil-twin AP by the firmware.")
                    elif col == 4 and ap.is_open():
                        item.setForeground(_OPEN)  # open networks are the notable insecure ones
                    elif col == 6 and ap.has_capture():
                        item.setForeground(_CAPTURE)
                        item.setToolTip("PMKID captured" if ap.pmkid and not ap.handshake
                                        else "WPA handshake captured")
                    # Fade a stale row so an AP that has left range visibly decays.
                    if fresh < 1.0 and col != 0:
                        item.setForeground(QColor(139, 148, 158, int(90 + 165 * fresh)))
                    self._table.setItem(row, col, item)

        def render_native(self, width: int = 800, height: int = 200) -> "QImage":
            """Render the channel graph to a QImage — offscreen, no window needed
            (visual smoke test)."""
            img = QImage(width, height, QImage.Format_ARGB32)
            img.fill(_BG)
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing)
            self._graph._paint(p, width, height)
            p.end()
            return img

except ImportError:  # PyQt5 unavailable — the pure helpers above stay importable/testable.
    WifiAnalyzerTab = None  # type: ignore[assignment,misc]
    _ChannelGraph = None  # type: ignore[assignment,misc]
