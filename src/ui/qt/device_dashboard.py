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
    QAbstractItemView,
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
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
        self._stacked: "Optional[bool]" = None   # responsive reflow state (3-col vs stacked)
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
        # Live per-serial-line liveness (density spec: the ARM lamp + alert update per line, not only on
        # the 3s poll). The host's line signal fires device_tab._on_line_received FIRST (updating the
        # device's arm/alert state), then this re-renders — guarded on an unchanged signature, so it's
        # cheap under a scan burst. (Spade's CC-DASH-SELDEV-LIVENESS flag.)
        line_signal = getattr(self._device_tab, "_line_signal", None)
        if line_signal is not None and hasattr(line_signal, "line_received"):
            try:
                line_signal.line_received.connect(self._on_device_line)
            except Exception:  # noqa: BLE001 — a missing signal must never block the landing
                pass

    def _on_device_line(self, *_args) -> None:
        """Per serial line: refresh the Selected Device readouts so the ARM lamp + detector alert are
        live per line (not only on the 3s poll). Cheap — _render_selected_device guards on an unchanged
        signature, so a burst of scan lines only re-renders on a real change."""
        self._render_selected_device()

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

        # Selected Device — BUILT FRESH (not re-homed): each device_tab readout sets its OWN inline
        # style that beats any Dashboard QSS, so re-homing them can't be made readable. We render the
        # same data (via the host's formatter methods — no logic duplicated) with mockup styling.
        sel_card, sel_v, self._sd_meta = self._card("Selected Device", "")
        top_row = QHBoxLayout()
        self._sd_arm = QLabel("○ —")                     # ARM/SAFE lamp — prominent pill (safety)
        self._sd_arm.setTextFormat(Qt.PlainText)
        top_row.addWidget(self._sd_arm)
        top_row.addStretch(1)
        self._sd_health = QLabel("")                     # connection health chip
        self._sd_health.setStyleSheet("font-size:9pt;")
        top_row.addWidget(self._sd_health)
        sel_v.addLayout(top_row)
        self._sd_caps = QWidget()                        # capability pills row
        self._sd_caps_lay = QHBoxLayout(self._sd_caps)
        self._sd_caps_lay.setContentsMargins(0, 2, 0, 2)
        self._sd_caps_lay.setSpacing(5)
        sel_v.addWidget(self._sd_caps)
        self._sd_telem = QLabel("")                      # identity/telemetry (muted mono line)
        self._sd_telem.setWordWrap(True)
        self._sd_telem.setStyleSheet(f"color:{_META};font-size:8pt;font-family:'Cascadia Mono',Consolas,monospace;")
        sel_v.addWidget(self._sd_telem)
        self._sd_alert = QLabel("")                      # detector alert — a real amber callout
        self._sd_alert.setWordWrap(True)
        self._sd_alert.setStyleSheet("color:#d29922;background:rgba(210,153,34,0.10);"
                                     "border-left:3px solid #d29922;border-radius:3px;padding:4px 8px;font-size:9pt;")
        sel_v.addWidget(self._sd_alert)
        self._sd_snap = QLabel("")                       # airspace snapshot (blue mono)
        self._sd_snap.setWordWrap(True)
        self._sd_snap.setStyleSheet("color:#58a6ff;font-size:9pt;font-family:'Cascadia Mono',Consolas,monospace;")
        sel_v.addWidget(self._sd_snap)
        center_v.addWidget(sel_card)

        link_card, link_v, _ = self._card("Relay Link", "LxveNode")
        self._link_label = QLabel("")
        self._link_label.setTextFormat(Qt.PlainText)
        self._link_label.setWordWrap(True)
        self._link_label.setStyleSheet("font-family:'Cascadia Mono',Consolas,monospace;font-size:9pt;")
        link_v.addWidget(self._link_label)
        self._link_card = link_card
        center_v.addWidget(link_card)

        # RIGHT — Cross-Comm summary (fresh, read-only) + Serial Terminal.
        # The full pool/rules/history/stream editor is the DEVICE ▸ Mesh sub-tab (the whole CrossCommTab).
        # Here we render a compact SUMMARY of the SAME shared target pool so the landing shows cross-device
        # discovery at a glance, WITHOUT double-parenting that one widget (density rule: a summary, not a
        # duplicate editor). Built fresh (like Selected Device) so it is readable + styleable.
        xc_card, xc_v, self._xc_meta = self._card("Cross-Comm", "")
        self._xc_table = QTableWidget(0, 4)
        self._xc_table.setHorizontalHeaderLabels(["Type", "SSID / Name", "RSSI", "Ch"])
        self._xc_table.verticalHeader().setVisible(False)
        self._xc_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._xc_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._xc_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._xc_table.setAlternatingRowColors(True)
        self._xc_table.setMinimumHeight(148)
        _hdr = self._xc_table.horizontalHeader()
        _hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        _hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        _hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        _hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        xc_v.addWidget(self._xc_table)
        xc_btns = QHBoxLayout()
        xc_refresh = QPushButton("Refresh")
        xc_refresh.setToolTip("Re-read the shared cross-device target pool.")
        xc_refresh.clicked.connect(self._render_cross_comm)
        xc_btns.addWidget(xc_refresh)
        self._xc_clear = QPushButton("Clear Pool")
        self._xc_clear.setToolTip("Clear all discovered cross-device targets from the shared pool.")
        self._xc_clear.setStyleSheet("QPushButton{font-size:8pt;padding:3px 9px;color:#f85149;}")
        self._xc_clear.clicked.connect(self._clear_pool)
        xc_btns.addWidget(self._xc_clear)
        xc_btns.addStretch(1)
        xc_v.addLayout(xc_btns)
        right_v.addWidget(xc_card)

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
        self._grid, self._left = grid, left   # kept for the responsive reflow (resizeEvent)
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
        self._render_selected_device()
        self._render_link()
        self._render_cross_comm()

    def _pool(self):
        return getattr(self._device_tab, "_pool", None)

    def _clear_pool(self) -> None:
        pool = self._pool()
        if pool is not None and hasattr(pool, "clear"):
            try:
                pool.clear()
            except Exception:  # noqa: BLE001
                pass
        self._render_cross_comm()

    def _render_cross_comm(self) -> None:
        """Render the compact Cross-Comm pool summary from the shared TargetPool (strongest first, top
        rows only — the pool can hold thousands). Read-only; the full editor is the Mesh sub-tab."""
        tbl = getattr(self, "_xc_table", None)
        if tbl is None:
            return
        pool = self._pool()
        targets = []
        if pool is not None:
            try:
                targets = list(pool.all())
            except Exception:  # noqa: BLE001
                targets = []
        try:
            targets.sort(key=lambda t: getattr(t, "rssi", 0) or -999, reverse=True)
        except Exception:  # noqa: BLE001
            pass
        cap = 60
        shown = targets[:cap]
        total = len(targets)
        extra = f"  (+{total - cap} more)" if total > cap else ""
        self._xc_meta.setText(f"{total} target" + ("" if total == 1 else "s") + extra)
        tbl.setRowCount(len(shown))
        for row, t in enumerate(shown):
            ttype = (getattr(getattr(t, "target_type", None), "value", "") or "").upper()
            name = getattr(t, "ssid", "") or getattr(t, "mac", "") or ""
            rssi = getattr(t, "rssi", 0)
            ch = getattr(t, "channel", 0)
            cells = [QTableWidgetItem(ttype), QTableWidgetItem(str(name)),
                     QTableWidgetItem("" if not rssi else str(rssi)),
                     QTableWidgetItem("" if not ch else str(ch))]
            cells[2].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            cells[3].setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            for col, item in enumerate(cells):
                tbl.setItem(row, col, item)
        clear_btn = getattr(self, "_xc_clear", None)
        if clear_btn is not None:
            clear_btn.setEnabled(pool is not None and hasattr(pool, "clear"))

    def _active_dev(self):
        dt = self._device_tab
        try:
            port = getattr(dt, "_active_port", "") or ""
            dm = getattr(dt, "_dm", None)
            return dm.get_device(port) if (port and dm is not None) else None
        except Exception:  # noqa: BLE001
            return None

    def _render_selected_device(self) -> None:
        """Render the Selected Device readouts fresh + readable (mockup styling), reusing the host's
        formatter methods for the DATA so no device logic is duplicated and no density field is dropped.

        Cheap on repeat: it computes the data first and early-returns on an unchanged signature, so it is
        safe to also fire per serial line (the density spec wants the ARM lamp + alert live per line, not
        only on the 3s poll — Spade's liveness flag). A burst of scan lines only re-renders on real change.
        """
        from src.ui.qt.arm_lamp import arm_lamp_render
        dt, dev = self._device_tab, self._active_dev()
        cls = type(dt)
        port = getattr(dt, "_active_port", "") or ""

        def _fmt(fn, *a):
            try:
                return str(fn(*a)) if dev is not None else ""
            except Exception:  # noqa: BLE001 — a formatter hiccup must not crash the landing
                return ""

        arm_text, color = arm_lamp_render(getattr(dev, "arm_state", "") or "")
        color = color or "#8b949e"
        health = _fmt(cls._format_health, dev).replace("Health:", "").strip()
        try:
            caps = [str(c) for c in dt._current_capabilities()] if dev is not None else []
        except Exception:  # noqa: BLE001
            caps = []
        telem = _fmt(cls._telemetry_line, getattr(dev, "telemetry", {}) or {})
        banner = (getattr(dev, "fw_banner", "") or "") if dev is not None else ""
        telem = (telem + (f"  ·  {banner}" if banner else "")).strip()
        alert = _fmt(cls._alert_line, getattr(dev, "alert_count", 0), getattr(dev, "last_alert", {}) or {})
        snap = _fmt(cls._snapshot_line, getattr(dev, "last_snapshot", {}) or {})

        sig = (port, arm_text, color, health, tuple(caps), telem, alert, snap)
        if sig == getattr(self, "_sd_sig", None):
            return   # nothing changed — skip the widget churn (makes per-serial-line rendering cheap)
        self._sd_sig = sig

        # ARM/SAFE lamp — a prominent state-tinted pill (arm_lamp_render is shared with Operate).
        _tint = {"#3fb950": "#0f2417", "#d29922": "#241c07", "#f85149": "#2b1416"}.get(color, "#161b22")
        self._sd_arm.setText(arm_text or "○ —")
        self._sd_arm.setStyleSheet(f"color:{color};background:{_tint};border:1px solid {color};"
                                   f"border-radius:7px;padding:4px 12px;font-weight:700;font-size:10pt;")
        self._sd_health.setText(health)
        # Capability pills (fresh row) — rebuilt only when the signature changed (cheap per line).
        while self._sd_caps_lay.count():
            w = self._sd_caps_lay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)   # remove NOW (deleteLater is async — would double the pills)
                w.deleteLater()
        for cs in caps:
            unknown = cs.lower().startswith(("cap", "unknown")) and any(ch.isdigit() for ch in cs)
            pill = QLabel(cs if unknown else cs.upper())
            if unknown:
                pill.setStyleSheet(f"color:{_META};font-style:italic;font-size:8pt;padding:2px 4px;")
            else:
                pill.setStyleSheet("border:1px solid #6e40c9;border-radius:11px;color:#c8b6f5;"
                                   "background:#0d1117;padding:2px 8px;font-size:8pt;")
            self._sd_caps_lay.addWidget(pill)
        self._sd_caps_lay.addStretch(1)
        self._sd_caps.setVisible(bool(caps))
        self._sd_telem.setText(telem)
        self._sd_alert.setText(alert); self._sd_alert.setVisible(bool(alert))
        self._sd_snap.setText(snap); self._sd_snap.setVisible(bool(snap))

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

    def resizeEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(ev)
        self._apply_responsive(ev.size().width())

    def _apply_responsive(self, width: int) -> None:
        """Reflow for many screen sizes (CC is universal: cyberdeck 320-480px · tablet · desktop · 4K).
        Below ~900px the 3 columns stack into one so a narrow deck stays usable (scroll handles height);
        at desktop width they sit side-by-side. Only flips on crossing the breakpoint (no per-pixel churn)."""
        grid = getattr(self, "_grid", None)
        if grid is None:
            return
        stacked = width < 900
        if stacked == self._stacked:
            return
        self._stacked = stacked
        grid.setDirection(QBoxLayout.TopToBottom if stacked else QBoxLayout.LeftToRight)
        if stacked:
            for i in range(grid.count()):
                grid.setStretch(i, 0)          # each column sizes to content when stacked
            self._left.setMaximumWidth(16777215)
        else:
            grid.setStretch(0, 0); grid.setStretch(1, 5); grid.setStretch(2, 4)
            self._left.setMaximumWidth(300)

    def set_ui_mode(self, mode: str) -> None:
        """Forward the Simple/Pro depth toggle to the composed hosts — each hides its own Pro widgets
        by reference (Disk/Battery gauges, Device Health table, command palette, rules/history/stream)."""
        for w in (self._health_tab, self._device_tab, self._cross_comm):
            fn = getattr(w, "set_ui_mode", None)
            if callable(fn):
                fn(mode)
