"""Targets tab — a focused view of the shared target pool.

Shows every :class:`~src.models.target.Target` currently in the
:class:`~src.core.cross_comm.TargetPool`.  Auto-refreshes on a QTimer and on
``target.*`` :class:`~src.core.cross_comm.EventBus` events.  Because bus
callbacks may fire on worker threads, they are marshalled onto the Qt GUI
thread through a signal bridge before any widget is touched.
"""

from __future__ import annotations

import logging
from typing import Any

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.cross_comm import EventBus, TargetPool
from src.ui.qt.widgets.signal_bars import SignalBarsDelegate

log = logging.getLogger(__name__)

# RSSI thresholds for the signal-strength color cue.
_RSSI_STRONG = -60
_RSSI_WEAK = -80


class _BusBridge(QObject):
    """Marshals EventBus callbacks (any thread) onto the Qt GUI thread."""

    changed = pyqtSignal()


class TargetsTab(QWidget):
    """Read-only table of discovered targets.

    Constructor:
        ``TargetsTab(target_pool, event_bus)``
    """

    _COLUMNS = ["Type", "SSID", "MAC", "RSSI", "Ch", "Source", "Enc", "Last Seen"]

    def __init__(self, target_pool: TargetPool, event_bus: EventBus) -> None:
        super().__init__()
        self._pool = target_pool
        self._bus = event_bus

        self._bridge = _BusBridge()
        self._bridge.changed.connect(self._refresh, Qt.QueuedConnection)

        self._build_ui()
        self._subscribe_bus()
        self._refresh()

        # Periodic safety-net refresh (also covers age-driven RSSI changes).
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(3000)

    # ── Layout ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        container = QWidget()
        root = QVBoxLayout(container)

        # Search / filter bar
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Filter by SSID, MAC, or type...")
        self._search_input.textChanged.connect(self._apply_filter)
        root.addWidget(self._search_input)

        # Toolbar row
        toolbar = QHBoxLayout()
        self._count_label = QLabel("0 targets")
        self._count_label.setObjectName("muted")
        self._count_label.setWordWrap(True)
        toolbar.addWidget(self._count_label)
        toolbar.addStretch()
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self._refresh)
        self._clear_btn = QPushButton("Clear All")
        self._clear_btn.clicked.connect(self._on_clear)
        toolbar.addWidget(self._refresh_btn)
        toolbar.addWidget(self._clear_btn)
        root.addLayout(toolbar)

        # Table
        self._table = QTableWidget(0, len(self._COLUMNS))
        self._table.setHorizontalHeaderLabels(self._COLUMNS)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setMinimumHeight(100)

        # Use SignalBarsDelegate for the RSSI column (index 3)
        self._signal_delegate = SignalBarsDelegate(self._table)
        self._table.setItemDelegateForColumn(3, self._signal_delegate)

        root.addWidget(self._table, stretch=1)

        scroll.setWidget(container)
        outer.addWidget(scroll)

    # ── EventBus wiring ──────────────────────────────────────────────

    def _subscribe_bus(self) -> None:
        for topic in ("target.added", "target.updated", "target.removed", "target.cleared"):
            self._bus.subscribe(topic, self._bus_callback)

    def _bus_callback(self, _topic: str, _payload: dict[str, Any]) -> None:
        """EventBus callback (any thread) — request a GUI-thread refresh."""
        self._bridge.changed.emit()

    # ── Refresh ──────────────────────────────────────────────────────

    def _refresh(self) -> None:
        """Rebuild the table from :meth:`TargetPool.all`."""
        targets = self._pool.all()

        # Disable sorting while repopulating to avoid row-index churn.
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(targets))
        for row, t in enumerate(targets):
            self._table.setItem(row, 0, QTableWidgetItem(t.target_type.value))
            self._table.setItem(row, 1, QTableWidgetItem(t.ssid or ""))
            self._table.setItem(row, 2, QTableWidgetItem(t.mac or ""))

            rssi_item = QTableWidgetItem(str(t.rssi))
            self._table.setItem(row, 3, rssi_item)

            self._table.setItem(row, 4, QTableWidgetItem(str(t.channel)))
            self._table.setItem(row, 5, QTableWidgetItem(t.device_source or ""))
            self._table.setItem(row, 6, QTableWidgetItem(t.encryption or ""))
            self._table.setItem(row, 7, QTableWidgetItem(self._fmt_time(t.last_seen)))
        self._table.setSortingEnabled(True)

        self._count_label.setText(f"{len(targets)} target{'s' if len(targets) != 1 else ''}")

        # Re-apply any active filter
        self._apply_filter(self._search_input.text())

    def _apply_filter(self, text: str) -> None:
        """Show/hide table rows based on search text matching SSID, MAC, or Type."""
        filter_text = text.strip().lower()
        for row in range(self._table.rowCount()):
            if not filter_text:
                self._table.setRowHidden(row, False)
                continue
            # Check Type (col 0), SSID (col 1), MAC (col 2)
            match = False
            for col in (0, 1, 2):
                item = self._table.item(row, col)
                if item and filter_text in (item.text() or "").lower():
                    match = True
                    break
            self._table.setRowHidden(row, not match)

    def _on_clear(self) -> None:
        reply = QMessageBox.question(
            self,
            "Clear Targets",
            "Remove all targets from the shared pool?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._pool.clear()
            self._refresh()

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _rssi_color(rssi: int) -> QColor:
        if rssi >= _RSSI_STRONG:
            return QColor("#39ff14")  # strong — green
        if rssi >= _RSSI_WEAK:
            return QColor("#ffd700")  # medium — yellow
        return QColor("#ff8c00")      # weak — orange

    @staticmethod
    def _fmt_time(last_seen: Any) -> str:
        try:
            return last_seen.strftime("%H:%M:%S")
        except (AttributeError, ValueError):
            return str(last_seen)

    # ── Qt overrides ─────────────────────────────────────────────────

    def showEvent(self, event) -> None:  # noqa: N802 — Qt naming
        super().showEvent(event)
        self._refresh()
