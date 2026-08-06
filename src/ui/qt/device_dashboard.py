"""DEVICE ▸ Dashboard — the reform's landing screen (the app opens here).

Per the owner-approved reform mockup + REFORM-DENSITY-SPEC "Dashboard", launch opens on a real working
device screen — a dense GitHub-Primer card grid — instead of the rejected Operate-Home bounce-pad. This
is a RE-COMPOSITION: it RE-HOMES the individual live display widgets out of the existing HealthTab,
DeviceTab and CrossCommTab (their gauges, device list, per-device readout labels, serial terminal,
tables) into seven Primer cards laid out like the mockup, and keeps the host controller instances alive
so their poll + signal handlers keep driving the re-homed widgets. Nothing is rebuilt or thinned — every
field, table, control, state and safety item from the density spec survives (verified: the hosts never
re-parent/re-add their children, so re-homing the leaves is geometry-safe).

Two consequences of keeping the hosts HEADLESS (their roots never shown, so their own poll timers never
start): (1) the Dashboard PUMPS the host refreshes itself — ``health_tab._refresh()`` on a 5s timer and
``device_tab._refresh_devices()`` on a 3s timer, started in its own showEvent — else the re-homed
gauges/tables/list would freeze at their constructor values; (2) the Dashboard owns its own Relay Link
render (link_strip.py only ships a render-model, not a widget) and its own responsive behavior.

safety.py is byte-untouched. The ARM/SAFE lamp and the always-on, ungated BlueJammer STOP are re-homed
by reference and stay live + prominent (the lamp updates per serial line via signal, not the poll).
CrossCommTab is kept WHOLE and SHOWN (its pool refresh gates on isVisible()), so its own timers keep
firing natively. Additive: NOT yet wired into main_window — the shell mount + landing re-point is the
shell increment.
"""
from __future__ import annotations

from typing import Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.ui.qt.link_strip import link_strip_render

_ACCENT = "#a371f7"
_META = "#6e7681"
# De-bulk to the mockup's tight scale: card chrome + tighter fonts/padding on the re-homed controls
# (buttons/combos/lists/tables carry Qt's generous defaults; the readout labels keep their own inline
# font-size, which is fine). The ARM/SAFE lamp keeps its own prominent style (not overridden here).
_CARD_QSS = (
    "QFrame#dashcard{background:#161b22;border:1px solid #30363d;border-radius:8px;}"
    "QPushButton{font-size:8pt;padding:3px 9px;}"
    "QComboBox{font-size:8pt;padding:2px 6px;}"
    "QLineEdit{font-size:8pt;padding:2px 6px;}"
    "QListWidget{font-size:8pt;outline:0;}"
    "QListWidget::item{padding:2px 4px;}"
    "QTableWidget{font-size:8pt;}"
    "QTableWidget::item{padding:1px 4px;}"
    "QHeaderView::section{font-size:8pt;padding:2px 5px;}"
    "QTextEdit{font-size:8pt;}"
)


def _add(layout, widget) -> None:
    """Re-home *widget* into *layout* (skip if it doesn't exist)."""
    if widget is not None:
        layout.addWidget(widget)


class DeviceDashboard(QWidget):
    """The DEVICE ▸ Dashboard landing — a 3-region Primer card grid re-homing the live device widgets."""

    def __init__(self, health_tab: QWidget, device_tab: QWidget,
                 cross_comm: "Optional[QWidget]" = None, parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._health_tab = health_tab
        self._device_tab = device_tab
        self._cross_comm = cross_comm
        self._link_label: "Optional[QLabel]" = None
        self._link_card: "Optional[QFrame]" = None
        self.setStyleSheet(_CARD_QSS)
        self._build_ui()
        # Pump the headless hosts' refreshes (their own show-gated timers never start once re-homed).
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(5000)
        self._health_timer.timeout.connect(self._pump_health)
        self._dev_timer = QTimer(self)
        self._dev_timer.setInterval(3000)
        self._dev_timer.timeout.connect(self._pump_devices)
        self._pump_health()
        self._pump_devices()

    # ── card helpers ─────────────────────────────────────────────────────────
    @staticmethod
    def _card(title: str, meta: str = "") -> "tuple[QFrame, QVBoxLayout, QLabel]":
        frame = QFrame()
        frame.setObjectName("dashcard")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(10, 7, 10, 9)
        lay.setSpacing(6)
        hdr = QHBoxLayout()
        t = QLabel(title.upper())
        t.setStyleSheet(f"color:{_ACCENT};font-size:10pt;font-weight:700;letter-spacing:0.5px;")
        hdr.addWidget(t)
        hdr.addStretch(1)
        m = QLabel(meta)
        m.setStyleSheet(f"color:{_META};font-size:9pt;")
        hdr.addWidget(m)
        lay.addLayout(hdr)
        return frame, lay, m

    @staticmethod
    def _column() -> "tuple[QWidget, QVBoxLayout]":
        col = QWidget()
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        v.setAlignment(Qt.AlignTop)
        return col, v

    # ── layout ───────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        grid_host = QWidget()
        gh_v = QVBoxLayout(grid_host)
        gh_v.setContentsMargins(14, 14, 14, 14)
        gh_v.setSpacing(12)
        cols_row = QWidget()
        grid = QHBoxLayout(cols_row)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)

        left, left_v = self._column()
        center, center_v = self._column()
        right, right_v = self._column()

        dt, ht = self._device_tab, self._health_tab

        # LEFT — Devices + Device Health
        dev_card, dev_v, _ = self._card("Devices", "Scan · Connect")
        _add(dev_v, getattr(dt, "_device_list", None))
        btn_row = QHBoxLayout()
        _add(btn_row, getattr(dt, "_btn_connect", None))
        _add(btn_row, getattr(dt, "_btn_disconnect", None))
        scan = QPushButton("Scan Ports")
        scan.setToolTip("Scan serial ports and register newly connected boards.")
        if hasattr(dt, "_scan_and_add"):
            scan.clicked.connect(dt._scan_and_add)
        btn_row.addWidget(scan)
        btn_row.addStretch(1)
        dev_v.addLayout(btn_row)
        fw_row = QHBoxLayout()
        _add(fw_row, getattr(dt, "_firmware_label", None))
        _add(fw_row, getattr(dt, "_firmware_combo", None))
        dev_v.addLayout(fw_row)
        left_v.addWidget(dev_card)
        left_v.addStretch(1)   # Devices top-aligned; Device Health moves to a full-width row below

        # CENTER — System Health + Selected Device + Relay Link
        sys_card, sys_v, _ = self._card("System Health", "host · live 5s")
        gauges = QHBoxLayout()
        gauges.setSpacing(14)
        for g, d in (("_cpu_gauge", "_cpu_detail"), ("_ram_gauge", "_ram_detail"),
                     ("_disk_gauge", "_disk_detail"), ("_batt_gauge", "_batt_detail")):
            gauge, detail = getattr(ht, g, None), getattr(ht, d, None)
            if gauge is None:
                continue
            gauge.setFixedSize(84, 92)     # de-bulk: the host gauge is min-100px; the mockup is ~84
            col = QWidget()
            cv = QVBoxLayout(col)
            cv.setContentsMargins(0, 0, 0, 0)
            cv.setSpacing(2)
            cv.addWidget(gauge, 0, Qt.AlignHCenter)
            if detail is not None:
                cv.addWidget(detail, 0, Qt.AlignHCenter)
            gauges.addWidget(col)
        gauges.addStretch(1)
        sys_v.addLayout(gauges)
        gps_row = QHBoxLayout()
        gps_cap = QLabel("GPS:")
        gps_cap.setStyleSheet(f"color:{_META};")
        gps_row.addWidget(gps_cap)
        _add(gps_row, getattr(ht, "_gps_status", None))
        gps_row.addStretch(1)
        sys_v.addLayout(gps_row)
        center_v.addWidget(sys_card)

        sel_card, sel_v, _ = self._card("Selected Device")
        top_row = QHBoxLayout()
        _add(top_row, getattr(dt, "_arm_label", None))   # ARM/SAFE lamp — prominent, always visible
        top_row.addStretch(1)
        _add(top_row, getattr(dt, "_health_label", None))
        sel_v.addLayout(top_row)
        for attr in ("_caps_label", "_telemetry_label", "_alert_label", "_snapshot_label"):
            _add(sel_v, getattr(dt, attr, None))
        center_v.addWidget(sel_card)

        link_card, link_v, _ = self._card("Relay Link", "LxveNode")
        self._link_label = QLabel("")
        self._link_label.setTextFormat(Qt.PlainText)
        self._link_label.setWordWrap(True)
        self._link_label.setStyleSheet("font-family:'Cascadia Mono',Consolas,monospace;font-size:9pt;")
        link_v.addWidget(self._link_label)
        self._link_card = link_card
        center_v.addWidget(link_card)

        # RIGHT — Cross-Comm + Serial Terminal
        if self._cross_comm is not None:
            # Mount CrossCommTab directly — it ships its own card chrome, so wrapping it in another
            # dashcard made the card-in-card the owner flagged. Kept WHOLE + SHOWN (pool gates on visible).
            right_v.addWidget(self._cross_comm, 1)

        term_card, term_v, _ = self._card("Serial Terminal")
        _add(term_v, getattr(dt, "_term_label", None))
        _add(term_v, getattr(dt, "_terminal", None))
        cmd_row = QHBoxLayout()
        _add(cmd_row, getattr(dt, "_cmd_palette", None))
        _add(cmd_row, getattr(dt, "_cmd_input", None))
        _add(cmd_row, getattr(dt, "_btn_send", None))
        term_v.addLayout(cmd_row)
        _add(term_v, getattr(dt, "_bj_panel", None))     # ungated STOP + arm gate ride inside, whole
        _add(term_v, getattr(dt, "_mesh_panel", None))   # host toggles visibility by reference
        right_v.addWidget(term_card, 0)                   # sizes to content, doesn't hog the column
        right_v.addStretch(1)

        grid.addWidget(left, 0)
        grid.addWidget(center, 5)     # command/health column dominates (mockup 1.15fr)
        grid.addWidget(right, 4)      # slimmer right column (mockup 1fr)
        left.setMinimumWidth(232)
        left.setMaximumWidth(300)
        gh_v.addWidget(cols_row)
        # Device Health goes full-width below the columns — a 5-col table is unreadable crammed into the
        # 232px left column (owner: "device health is unreadable"). Usability over mockup-exact placement.
        dev_health = getattr(ht, "_dev_card", None)
        if dev_health is not None:
            gh_v.addWidget(dev_health)
        gh_v.addStretch(1)
        scroll.setWidget(grid_host)
        outer.addWidget(scroll, 1)

    # ── live pumps (hosts are headless — their own show-gated timers never fire) ──
    def _pump_health(self) -> None:
        fn = getattr(self._health_tab, "_refresh", None)
        if callable(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001 — a refresh hiccup must not crash the landing
                pass

    def _pump_devices(self) -> None:
        fn = getattr(self._device_tab, "_refresh_devices", None)
        if callable(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
        self._render_link()

    def _render_link(self) -> None:
        """Render the Relay Link strip for the active device (link_strip.py ships only a render-model)."""
        if self._link_label is None:
            return
        dt = self._device_tab
        dev = None
        try:
            port = getattr(dt, "_active_port", "") or ""
            dm = getattr(dt, "_dm", None)
            if port and dm is not None:
                dev = dm.get_device(port)
        except Exception:  # noqa: BLE001
            dev = None
        link = (getattr(dev, "link", None) or {}) if dev is not None else {}
        view = link_strip_render(link)
        visible = bool(getattr(view, "visible", False))
        self._link_label.setText(getattr(view, "text", "") or "")
        color = getattr(view, "color", "") or "#8b949e"
        self._link_label.setStyleSheet(
            f"color:{color};font-family:'Cascadia Mono',Consolas,monospace;font-size:9pt;")
        if self._link_card is not None:
            self._link_card.setVisible(visible)   # hide the whole card for a plain non-relayed USB

    # ── lifecycle ────────────────────────────────────────────────────────────
    def showEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        super().showEvent(ev)
        self._pump_health()
        self._pump_devices()
        self._health_timer.start()
        self._dev_timer.start()

    def hideEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        super().hideEvent(ev)
        self._health_timer.stop()
        self._dev_timer.stop()

    def set_ui_mode(self, mode: str) -> None:
        """Forward the Simple/Pro depth toggle to the composed hosts — each hides its own Pro widgets
        by reference (Disk/Battery gauges, Device Health table, command palette, rules/history/stream)."""
        for w in (self._health_tab, self._device_tab, self._cross_comm):
            fn = getattr(w, "set_ui_mode", None)
            if callable(fn):
                fn(mode)
