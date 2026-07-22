"""BLE analyzer view — the live signal-strength graph + device table (Stream-A output view).

Renders the on-device Bluetooth-analyzer visual from the firmware-agnostic BleAnalyzerModel: a
scrolling RSSI-over-time graph for the strongest advertisers plus a live device table (name, addr,
vendor, signal bars, tracker flag, age). Fed by the TargetIngestor event-observer tap, so every BLE
firmware's ble_found events populate it (Marauder / Ghost / Flipper / HaleHound / DIV / LxveOS).
Awareness-only: it visualizes what's advertising nearby and drives no device.

The pure core (BleAnalyzerModel + the pixel-mapping helpers here) is Qt-free and unit-testable; the
widget is import-guarded and offscreen-renderable via render_native() for a windowless visual test.
"""
from __future__ import annotations

import threading
import time
from typing import List, Optional

from src.core.ble_analyzer import BleAnalyzerModel, BleDevice
from src.core.broadcast import BroadcastVerb


class BleScanController:
    """Start/stop a BLE scan on every connected BLE-capable device via the shared broadcast engine:
    each device runs its OWN native BLE-scan verb; CC transmits nothing. Cross-talk: the sends flow
    through the app-wide activity_log, so the terminal + other surfaces reflect the same action."""

    def __init__(self, engine) -> None:
        self._engine = engine

    def target_count(self) -> int:
        """How many connected devices can run a BLE scan (drives Start's enabled state)."""
        try:
            return len(self._engine.plan(BroadcastVerb.BLE_SCAN).concrete)
        except Exception:  # noqa: BLE001
            return 0

    def start(self) -> int:
        return self._dispatch(BroadcastVerb.BLE_SCAN)

    def stop(self) -> int:
        return self._dispatch(BroadcastVerb.STOP_ALL)

    def _dispatch(self, verb: "BroadcastVerb") -> int:
        try:
            plan = self._engine.plan(verb)
        except Exception:  # noqa: BLE001
            return 0
        if not plan.concrete:
            return 0
        from src.core.activity_log import activity_log
        act = activity_log()
        for c in plan.concrete:   # surface each native send in the shared terminal (cross-talk)
            act.emit_line("ble-analyzer", f"[{c.port}] {c.firmware}: {c.command}")
        threading.Thread(target=self._safe_dispatch, args=(plan,), daemon=True).start()
        return len(plan.concrete)

    def _safe_dispatch(self, plan) -> None:
        try:
            self._engine.dispatch(plan, confirmed=True)   # BLE_SCAN/STOP_ALL are safe; guarded path
        except Exception:  # noqa: BLE001 — a send error must never crash a background thread
            pass


# ── pure pixel mapping (Qt-free, unit-testable) ──
_RSSI_TOP = -30       # graph top edge dBm (very strong)
_RSSI_BOTTOM = -100   # graph bottom edge dBm (noise floor)
_WINDOW_S = 60.0      # graph time span: the last minute, newest at the right edge
_GRAPH_MAX_LINES = 6  # only the strongest N devices get a plotted line (keeps the graph readable)


def rssi_to_y(rssi: float, top_px: float, height_px: float,
              rssi_top: float = _RSSI_TOP, rssi_bottom: float = _RSSI_BOTTOM) -> float:
    """Map an RSSI to a y pixel: rssi_top→top_px (strong), rssi_bottom→bottom edge. Clamped."""
    span = rssi_top - rssi_bottom
    if span <= 0:
        return top_px
    frac = (rssi_top - rssi) / span
    frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
    return top_px + frac * height_px


def time_to_x(t: float, now: float, window_s: float, left_px: float, width_px: float) -> float:
    """Map a timestamp to an x pixel: now at the right edge, now-window_s at the left. Clamped."""
    if window_s <= 0:
        return left_px + width_px
    frac = 1.0 - (now - t) / window_s
    frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
    return left_px + frac * width_px


# Distinct per-device line colors (cycled). Dark-theme friendly, high-contrast against the graph bg.
_PALETTE = ("#58a6ff", "#3fb950", "#d29922", "#f85149", "#bc8cff", "#39c5cf", "#ff7b72", "#e3b341")


def device_color(index: int) -> str:
    return _PALETTE[index % len(_PALETTE)]


def graph_devices(model: BleAnalyzerModel, now: float, window_s: float = _WINDOW_S,
                  limit: int = _GRAPH_MAX_LINES) -> "List[BleDevice]":
    """The devices that get a plotted line: the strongest fresh ones with a sample in the window.
    Pure so the view and its test agree on what's drawn."""
    out = []
    for dev in model.devices(sort="rssi"):
        if dev.rssi is None:
            continue
        if any(t >= now - window_s for t, _ in dev.samples):
            out.append(dev)
        if len(out) >= limit:
            break
    return out


def _rssi_color(rssi: "Optional[float]") -> str:
    """Color-grade an RSSI: green strong → orange medium → red weak; muted if none (Biscuit)."""
    if rssi is None:
        return "#8b949e"
    if rssi >= -60:
        return "#3fb950"
    if rssi >= -80:
        return "#f0883e"
    return "#f85149"


# Per-operation Help sheet (Biscuit pattern, A2) — honest "what it does" for the analyzer.
_BLE_HELP = {
    "title": "BLE Analyzer",
    "summary": "A passive, firmware-agnostic view of the Bluetooth Low Energy advertisements a "
               "connected device reports — a live RSSI graph + a dedup table (transmits nothing).",
    "what_it_does": [
        ("📡", "Advertisement Capture",
         "Folds in every BLE advertisement the firmware reports (Marauder / GhostESP / Flipper / "
         "HaleHound / DIV / LxveOS)."),
        ("📈", "Live RSSI Graph", "Plots signal strength over time as a device nears or leaves."),
        ("🧹", "De-duplicated Table", "One row per device (by address) with vendor, hits, and age."),
        ("🎯", "Tracker Detection", "Flags AirTags / Find My + other trackers the firmware finds."),
    ],
    "statistics": [
        ("👁", "Present", "Devices seen in the last few seconds (a live scan is feeding the view)."),
        ("Σ", "Seen", "Total distinct devices observed this session."),
        ("🎯", "Trackers", "How many look like trackers (AirTags / Find My, etc.)."),
        ("🏷", "Named", "How many advertise a device name."),
        ("📶", "Strongest", "The closest device's RSSI (higher / greener = nearer)."),
    ],
    "tips": [
        "Start a BLE scan on the connected device — this view only shows what the scan finds.",
        "Sort by Strongest to find the closest device; by Most seen to find a persistent one.",
        "Pause freezes the view without losing data — recording continues in the background.",
    ],
}


# ── Qt widget (the pure core above stays Qt-free) ──
try:
    from PyQt5.QtCore import QPointF, QRectF, Qt, QTimer
    from PyQt5.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPolygonF
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

    from src.ui.qt.biscuit import HelpSheet, StartStopButton, StatGrid

    from src.ui.qt.widgets.signal_bars import SignalBarsDelegate

    _BG = QColor("#0d1117")
    _GRID = QColor("#21262d")
    _AXIS_TEXT = QColor("#6e7681")
    _TRACKER = QColor("#f85149")

    class _RssiGraph(QWidget):
        """A scrolling RSSI-over-time graph: one polyline per strong advertiser, newest at the
        right edge; reads the model live each paint (offscreen-renderable via render_native())."""

        def __init__(self, model: BleAnalyzerModel, parent: "Optional[QWidget]" = None) -> None:
            super().__init__(parent)
            self._model = model
            self._now_fn = time.monotonic          # injectable clock (tests override it)
            self._receiving = False                 # True only while a scan is actually feeding events (set by the tab)
            self.setMinimumHeight(180)

        def set_clock(self, fn) -> None:
            self._now_fn = fn

        def paintEvent(self, _ev) -> None:  # noqa: N802 (Qt override)
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            self._paint(p, self.width(), self.height())
            p.end()

        def _paint(self, p: "QPainter", w: int, h: int) -> None:
            p.fillRect(0, 0, w, h, _BG)
            left, top = 44.0, 8.0
            gw, gh = max(1.0, w - left - 10.0), max(1.0, h - top - 20.0)
            now = self._now_fn()

            # dBm grid + axis labels every 20 dB.
            p.setPen(QPen(_GRID, 1))
            for dbm in range(_RSSI_TOP, _RSSI_BOTTOM - 1, -20):
                y = rssi_to_y(dbm, top, gh)
                p.setPen(QPen(_GRID, 1))
                p.drawLine(int(left), int(y), int(left + gw), int(y))
                p.setPen(QPen(_AXIS_TEXT, 1))
                p.drawText(QRectF(0, y - 8, left - 4, 16),
                           Qt.AlignRight | Qt.AlignVCenter, f"{dbm}")

            devs = graph_devices(self._model, now)
            if not devs:
                # Only say "listening" when a scan is actually feeding events; otherwise the view sat here
                # painting "Listening…" from the moment it opened and looked like a live scan that finds
                # nothing (owner: "starts weird as if its already searching"). The tab sets _receiving.
                msg = ("Listening for BLE advertisements…" if getattr(self, "_receiving", False)
                       else "Idle — no BLE scan running")
                p.setPen(QPen(_AXIS_TEXT, 1))
                p.drawText(QRectF(left, top, gw, gh), Qt.AlignCenter, msg)
                return

            for i, dev in enumerate(devs):
                color = QColor(device_color(i))
                pts = [QPointF(time_to_x(t, now, _WINDOW_S, left, gw), rssi_to_y(r, top, gh))
                       for t, r in dev.samples if t >= now - _WINDOW_S]
                if not pts:
                    continue
                p.setPen(QPen(color, 2))
                if len(pts) == 1:
                    p.drawEllipse(pts[0], 2.5, 2.5)
                else:
                    p.drawPolyline(QPolygonF(pts))
                # A dot + name tag at the newest (right-most) sample.
                p.setBrush(QBrush(color))
                p.drawEllipse(pts[-1], 3.0, 3.0)
                p.drawText(QPointF(pts[-1].x() + 5, pts[-1].y() - 3), dev.display_name()[:18])

    class BleAnalyzerTab(QWidget):
        """Live BLE analyzer: scrolling RSSI graph + device table, fed by ble_found events from any
        firmware. Holds its own BleAnalyzerModel; on_ble_event(port, data) folds one sighting in."""

        _COLS = ("Signal", "Name", "Address", "Vendor", "Trk", "Hits", "Age")
        _ACTIVE_WINDOW_S = 10.0   # a ble_found event within this many seconds = a scan is actively feeding us

        def __init__(self, scan_controller: "Optional[BleScanController]" = None,
                     parent: "Optional[QWidget]" = None) -> None:
            super().__init__(parent)
            self._model = BleAnalyzerModel()
            self._now_fn = time.monotonic
            self._sort = "rssi"
            self._paused = False
            self._last_event_ts: "Optional[float]" = None   # when we last folded in a ble_found event
            self._scan = scan_controller     # None -> Start disabled (no engine wired; a bare tab)
            self._scanning = False           # whether WE started a scan here (drives the pill)

            root = QVBoxLayout(self)
            self._header = QLabel(
                "Not scanning. Start a BLE scan on a connected device to see BLE advertisements here.")
            self._header.setStyleSheet("color:#8b949e;")
            self._header.setWordWrap(True)
            root.addWidget(self._header)

            # Live stat grid (Biscuit statistics pattern, A2) — mirrors the header summary as tiles.
            self._stats = StatGrid(["Present", "Seen", "Trackers", "Named", "Strongest"], columns=5)
            root.addWidget(self._stats)

            # Start/Stop (A3): the primary pill runs the CONNECTED firmware's own BLE-scan verb on
            # every BLE-capable device via the shared broadcast engine (CC transmits nothing).
            self._scan_btn = StartStopButton()
            self._scan_btn.setToolTip("Start a BLE scan on every connected BLE-capable device.")
            self._scan_btn.start_requested.connect(self._on_start_scan)
            self._scan_btn.stop_requested.connect(self._on_stop_scan)
            self._scan_btn.set_ready(self._scan is not None)   # disabled until an engine is wired
            root.addWidget(self._scan_btn)

            self._graph = _RssiGraph(self._model)
            root.addWidget(self._graph, 1)

            ctl = QHBoxLayout()
            self._sort_combo = QComboBox()
            self._sort_combo.addItems(["Strongest", "Recent", "Name", "Most seen"])
            self._sort_combo.setToolTip("Order the device table.")
            self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
            self._btn_pause = QPushButton("Pause")
            self._btn_pause.setToolTip("Freeze the view (recording continues in the background).")
            self._btn_pause.setCheckable(True)
            self._btn_pause.toggled.connect(self._on_pause)
            self._btn_clear = QPushButton("Clear")
            self._btn_clear.setToolTip("Forget all tracked BLE devices and start fresh.")
            self._btn_clear.clicked.connect(self._on_clear)
            ctl.addWidget(QLabel("Sort:"))
            ctl.addWidget(self._sort_combo)
            ctl.addWidget(self._btn_pause)
            ctl.addWidget(self._btn_clear)
            ctl.addStretch(1)
            self._btn_help = QPushButton("?")
            self._btn_help.setFixedWidth(28)
            self._btn_help.setToolTip("What the BLE Analyzer does")
            self._btn_help.clicked.connect(lambda: HelpSheet(_BLE_HELP, self).exec_())
            ctl.addWidget(self._btn_help)
            root.addLayout(ctl)

            self._table = QTableWidget(0, len(self._COLS))
            self._table.setHorizontalHeaderLabels(list(self._COLS))
            self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self._table.verticalHeader().setVisible(False)
            self._table.setItemDelegateForColumn(0, SignalBarsDelegate(self._table))
            hdr = self._table.horizontalHeader()
            hdr.setSectionResizeMode(1, QHeaderView.Stretch)
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

        def on_ble_event(self, port: str, data: dict) -> None:
            """Fold one ble_found / LxveOS ble event into the model. GUI-thread only (marshal
            serial-thread events via a signal first). Recording continues while paused; Pause only
            freezes the repaint (same posture as the flock tab), so no data is lost while paused."""
            try:
                now = self._now_fn()
                self._model.observe(data, now)
                self._last_event_ts = now   # marks the view as actively receiving (drives the honest empty state)
            except Exception:  # noqa: BLE001 — a bad event must never break the view
                pass

        @property
        def model(self) -> BleAnalyzerModel:
            return self._model

        # ── controls ──
        def _on_sort_changed(self, idx: int) -> None:
            self._sort = ("rssi", "recent", "name", "hits")[idx]
            self._refresh()

        def _on_pause(self, checked: bool) -> None:
            self._paused = checked
            self._btn_pause.setText("Resume" if checked else "Pause")

        def _on_clear(self) -> None:
            self._model.clear()
            self._refresh()

        # ── scan control (A3) ──
        def _on_start_scan(self) -> None:
            """Start a BLE scan on every connected BLE-capable device (guarded broadcast path)."""
            if self._scan is None:
                return
            n = self._scan.start()
            self._scanning = n > 0
            self._scan_btn.set_running(self._scanning)
            if not self._scanning:
                self._scan_btn.set_ready(False)   # nothing to scan on — reflect it honestly

        def _on_stop_scan(self) -> None:
            if self._scan is not None:
                self._scan.stop()
            self._scanning = False
            self._scan_btn.set_running(False)

        # ── render ──
        def _is_receiving(self, now: float) -> bool:
            """True when a ble_found event arrived recently — i.e. a scan is actually feeding this view.
            Drives the honest empty state so an idle analyzer never poses as a live scan."""
            return self._last_event_ts is not None and (now - self._last_event_ts) <= self._ACTIVE_WINDOW_S

        def _refresh(self) -> None:
            if self._paused:
                return
            now = self._now_fn()
            receiving = self._is_receiving(now)
            self._graph._receiving = receiving
            s = self._model.summary(now)
            targets = self._scan.target_count() if self._scan is not None else None
            if s["total"] == 0 and not receiving:
                # Nothing seen and no scan feeding us — say so plainly, and name the missing prerequisite so
                # a disabled Start pill explains itself instead of just sitting greyed out (A5 #1).
                if targets == 0:
                    self._header.setText(
                        "No BLE-capable device connected — connect one on the Devices tab, then press Start.")
                else:
                    self._header.setText(
                        "Not scanning. Press Start to scan on the connected device(s), or start a scan "
                        "elsewhere to see BLE advertisements here.")
            else:
                strongest = "—" if s["strongest"] is None else f"{s['strongest']} dBm"
                self._header.setText(
                    f"{s['fresh']} present · {s['total']} seen · {s['trackers']} tracker(s) · "
                    f"{s['named']} named · strongest {strongest}")
            self._update_stats(s, receiving)
            # Keep the Start pill honest: enabled only when a BLE-capable device is connected.
            if self._scan is not None and not self._scanning:
                self._scan_btn.set_ready(bool(targets))   # targets is an int here (scan controller present)
            self._fill_table(now)
            self._graph.update()

        def _update_stats(self, s: dict, receiving: bool) -> None:
            """Mirror the summary into the Biscuit stat tiles (color-graded, honest when idle)."""
            _green, _orange, _muted = "#3fb950", "#f0883e", "#8b949e"
            strongest = s["strongest"]
            self._stats.set_stats({
                "Present": (s["fresh"], _green if (receiving and s["fresh"]) else _muted),
                "Seen": s["total"],
                "Trackers": (s["trackers"], _orange if s["trackers"] else _muted),
                "Named": s["named"],
                "Strongest": ("—" if strongest is None else f"{strongest}", _rssi_color(strongest)),
            })

        def _fill_table(self, now: float) -> None:
            devs = self._model.devices(sort=self._sort)
            self._table.setRowCount(len(devs))
            for row, d in enumerate(devs):
                fresh = d.freshness(now)
                age = d.age(now)
                cells = [
                    "" if d.rssi is None else str(d.rssi),   # SignalBarsDelegate reads the RSSI int
                    d.display_name(),
                    d.addr,
                    d.vendor or "",
                    "⚑" if d.tracker else "",
                    str(d.hits),
                    "now" if age < 1 else f"{int(age)}s",
                ]
                for col, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    if col == 4 and d.tracker:
                        item.setForeground(_TRACKER)
                    # Fade a stale row so a device that has left the area visibly decays.
                    if fresh < 1.0 and col != 0:
                        item.setForeground(QColor(139, 148, 158, int(90 + 165 * fresh)))
                    self._table.setItem(row, col, item)

        def render_native(self, width: int = 800, height: int = 240) -> "QImage":
            """Render the graph to a QImage — offscreen, no window needed (visual smoke test)."""
            img = QImage(width, height, QImage.Format_ARGB32)
            img.fill(_BG)
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing)
            self._graph._paint(p, width, height)
            p.end()
            return img

except ImportError:  # PyQt5 unavailable — the pure helpers above stay importable/testable.
    BleAnalyzerTab = None  # type: ignore[assignment,misc]
    _RssiGraph = None  # type: ignore[assignment,misc]
