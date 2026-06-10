"""Cross-comm tab — shared target pool, live event stream, and auto-routing.

Wired directly to cyber-controller's real cross-comm primitives:

* :class:`~src.core.cross_comm.TargetPool` — the shared target collection.
* :class:`~src.core.cross_comm.EventBus` — pub/sub for ``target.*`` and other
  topics.  We subscribe with a ``*`` wildcard for the live event stream and to
  ``target.added`` / ``target.updated`` to keep the pool table fresh.
* :class:`~src.core.cross_comm.AutoRouter` — rules engine.  The Add Rule dialog
  builds a real :class:`~src.core.cross_comm.RoutingRule` and registers it via
  :meth:`AutoRouter.add_rule`.

EventBus callbacks fire synchronously in the *publisher's* thread (often a
serial/hot-plug worker), so every bus callback is marshalled onto the Qt GUI
thread through a :class:`QObject` signal bridge before touching widgets.
"""

from __future__ import annotations

import logging
from typing import Any

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core.cross_comm import AutoRouter, EventBus, RoutingRule, TargetPool
from src.core.device_manager import DeviceManager
from src.models.target import TargetType

log = logging.getLogger(__name__)

# Maximum lines kept in the live event stream before old lines are trimmed.
_MAX_EVENT_LINES = 500


class _BusBridge(QObject):
    """Marshals EventBus callbacks (any thread) onto the Qt GUI thread."""

    event = pyqtSignal(str, dict)  # (topic, payload)


class AddRuleDialog(QDialog):
    """Dialog that builds a :class:`RoutingRule` from user input."""

    def __init__(self, device_ports: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Auto-Routing Rule")
        self.setMinimumWidth(420)

        form = QFormLayout(self)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Deauth strong APs")
        form.addRow("Rule name:", self.name_edit)

        # Target type — "* (any)" maps to RoutingRule.target_type = None.
        self.type_combo = QComboBox()
        self.type_combo.addItem("* (any type)", None)
        for tt in TargetType:
            self.type_combo.addItem(tt.value.upper(), tt)
        form.addRow("Match target type:", self.type_combo)

        self.ssid_edit = QLineEdit()
        self.ssid_edit.setPlaceholderText("substring match (blank = any)")
        form.addRow("SSID contains:", self.ssid_edit)

        self.min_rssi_spin = QSpinBox()
        self.min_rssi_spin.setRange(-100, 0)
        self.min_rssi_spin.setValue(-100)
        self.min_rssi_spin.setSuffix(" dBm")
        form.addRow("Minimum RSSI:", self.min_rssi_spin)

        self.port_combo = QComboBox()
        if device_ports:
            for port in device_ports:
                self.port_combo.addItem(port, port)
        else:
            self.port_combo.setEditable(True)
            self.port_combo.setEditText("")
        self.port_combo.setEditable(True)
        form.addRow("Send to device port:", self.port_combo)

        self.command_edit = QLineEdit()
        self.command_edit.setPlaceholderText("e.g. deauth {mac} on ch {channel}")
        self.command_edit.setToolTip("Placeholders: {mac}, {ssid}, {channel}")
        form.addRow("Command template:", self.command_edit)

        self.cooldown_spin = QSpinBox()
        self.cooldown_spin.setRange(0, 3600)
        self.cooldown_spin.setValue(30)
        self.cooldown_spin.setSuffix(" s")
        form.addRow("Per-target cooldown:", self.cooldown_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def get_rule(self) -> RoutingRule:
        """Build a :class:`RoutingRule` from the dialog's current state."""
        name = self.name_edit.text().strip() or "rule"
        port = (self.port_combo.currentData() or self.port_combo.currentText()).strip()
        return RoutingRule(
            name=name,
            target_type=self.type_combo.currentData(),
            ssid_pattern=self.ssid_edit.text().strip(),
            min_rssi=self.min_rssi_spin.value(),
            device_port=port,
            command_template=self.command_edit.text().strip(),
            enabled=True,
            cooldown=float(self.cooldown_spin.value()),
        )


class CrossCommTab(QWidget):
    """Cross-device coordination tab.

    Constructor:
        ``CrossCommTab(event_bus, target_pool, auto_router, device_manager)``
    """

    def __init__(
        self,
        event_bus: EventBus,
        target_pool: TargetPool,
        auto_router: AutoRouter,
        device_manager: DeviceManager,
    ) -> None:
        super().__init__()
        self._bus = event_bus
        self._pool = target_pool
        self._router = auto_router
        self._dm = device_manager

        # Bridge bus callbacks (worker threads) onto the GUI thread.
        self._bridge = _BusBridge()
        self._bridge.event.connect(self._on_bus_event, Qt.QueuedConnection)

        self._build_ui()
        self._subscribe_bus()
        self._refresh_pool()
        self._refresh_rules()

        # Periodic safety-net refresh in case any update is missed.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_pool)
        self._timer.start(5000)

    # ── Layout ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        splitter = QSplitter(Qt.Vertical)

        # ── Top: shared target pool ──────────────────────────────────
        pool_group = QGroupBox("Shared Target Pool")
        pool_layout = QVBoxLayout(pool_group)

        self._pool_table = QTableWidget(0, 6)
        self._pool_table.setHorizontalHeaderLabels(
            ["Type", "SSID", "MAC", "RSSI", "Ch", "Source"]
        )
        self._pool_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._pool_table.setAlternatingRowColors(True)
        self._pool_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._pool_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._pool_table.verticalHeader().setVisible(False)
        pool_layout.addWidget(self._pool_table)

        pool_btn_row = QHBoxLayout()
        self._pool_count_label = QLabel("0 targets")
        self._pool_count_label.setStyleSheet("color: #888;")
        pool_btn_row.addWidget(self._pool_count_label)
        pool_btn_row.addStretch()
        self._refresh_pool_btn = QPushButton("Refresh")
        self._refresh_pool_btn.clicked.connect(self._refresh_pool)
        self._clear_pool_btn = QPushButton("Clear Pool")
        self._clear_pool_btn.setStyleSheet("QPushButton { color: #ff4444; }")
        self._clear_pool_btn.clicked.connect(self._on_clear_pool)
        pool_btn_row.addWidget(self._refresh_pool_btn)
        pool_btn_row.addWidget(self._clear_pool_btn)
        pool_layout.addLayout(pool_btn_row)

        splitter.addWidget(pool_group)

        # ── Bottom: event stream + auto-rules ────────────────────────
        bottom = QWidget()
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)

        # Live event stream
        stream_group = QGroupBox("Live Event Stream")
        stream_layout = QVBoxLayout(stream_group)
        self._event_log = QTextEdit()
        self._event_log.setReadOnly(True)
        self._event_log.setFont(QFont("Consolas", 9))
        self._event_log.setStyleSheet(
            "QTextEdit { background-color: #1a1a1a; color: #39ff14; }"
        )
        self._event_log.setPlaceholderText("Bus events appear here in real time...")
        stream_layout.addWidget(self._event_log)
        clear_log_btn = QPushButton("Clear Log")
        clear_log_btn.clicked.connect(self._event_log.clear)
        stream_layout.addWidget(clear_log_btn)
        bottom_layout.addWidget(stream_group, 2)

        # Auto-routing rules
        rules_group = QGroupBox("Auto-Routing Rules")
        rules_layout = QVBoxLayout(rules_group)
        rules_layout.addWidget(QLabel("When a matching target is discovered:"))
        self._rule_list = QListWidget()
        self._rule_list.currentRowChanged.connect(
            lambda row: self._remove_rule_btn.setEnabled(row >= 0)
        )
        rules_layout.addWidget(self._rule_list)

        rule_btn_row = QHBoxLayout()
        self._add_rule_btn = QPushButton("Add Rule...")
        self._add_rule_btn.clicked.connect(self._on_add_rule)
        self._remove_rule_btn = QPushButton("Remove Rule")
        self._remove_rule_btn.setEnabled(False)
        self._remove_rule_btn.clicked.connect(self._on_remove_rule)
        rule_btn_row.addWidget(self._add_rule_btn)
        rule_btn_row.addWidget(self._remove_rule_btn)
        rules_layout.addLayout(rule_btn_row)
        bottom_layout.addWidget(rules_group, 1)

        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter)

    # ── EventBus wiring ──────────────────────────────────────────────

    def _subscribe_bus(self) -> None:
        """Subscribe to the wildcard topic; all events flow through the bridge."""
        self._bus.subscribe("*", self._bus_callback)

    def _bus_callback(self, topic: str, payload: dict[str, Any]) -> None:
        """EventBus callback — may run on a worker thread.  Re-emit on GUI thread."""
        # Emitting a queued signal is thread-safe; the slot runs on the GUI thread.
        self._bridge.event.emit(topic, dict(payload))

    def _on_bus_event(self, topic: str, payload: dict[str, Any]) -> None:
        """Handle a bus event on the Qt GUI thread."""
        self._append_event(topic, payload)
        if topic in ("target.added", "target.updated", "target.removed", "target.cleared"):
            self._refresh_pool()

    def _append_event(self, topic: str, payload: dict[str, Any]) -> None:
        summary = self._summarize_payload(topic, payload)
        self._event_log.append(f"<span style='color:#888'>[{topic}]</span> {summary}")
        # Trim history to keep the widget responsive.
        doc = self._event_log.document()
        if doc.blockCount() > _MAX_EVENT_LINES:
            cursor = self._event_log.textCursor()
            cursor.movePosition(cursor.Start)
            cursor.movePosition(
                cursor.Down, cursor.KeepAnchor, doc.blockCount() - _MAX_EVENT_LINES
            )
            cursor.removeSelectedText()
            cursor.deleteChar()
        bar = self._event_log.verticalScrollBar()
        bar.setValue(bar.maximum())

    @staticmethod
    def _summarize_payload(topic: str, payload: dict[str, Any]) -> str:
        if "mac" in payload or "ssid" in payload:
            tt = payload.get("target_type", "?")
            ssid = payload.get("ssid", "")
            mac = payload.get("mac", "")
            rssi = payload.get("rssi", "")
            label = ssid or mac or "?"
            return f"{tt} {label} ({mac}) rssi={rssi}"
        if "count" in payload:
            return f"count={payload['count']}"
        return ", ".join(f"{k}={v}" for k, v in payload.items()) or "(no payload)"

    # ── Target pool ──────────────────────────────────────────────────

    def _refresh_pool(self) -> None:
        """Rebuild the pool table from :meth:`TargetPool.all`."""
        targets = self._pool.all()
        self._pool_table.setRowCount(len(targets))
        for row, t in enumerate(targets):
            self._pool_table.setItem(row, 0, QTableWidgetItem(t.target_type.value))
            self._pool_table.setItem(row, 1, QTableWidgetItem(t.ssid or ""))
            self._pool_table.setItem(row, 2, QTableWidgetItem(t.mac or ""))
            self._pool_table.setItem(row, 3, QTableWidgetItem(str(t.rssi)))
            self._pool_table.setItem(row, 4, QTableWidgetItem(str(t.channel)))
            self._pool_table.setItem(row, 5, QTableWidgetItem(t.device_source or ""))
        self._pool_count_label.setText(f"{len(targets)} targets")

    def _on_clear_pool(self) -> None:
        reply = QMessageBox.question(
            self,
            "Clear Target Pool",
            "Remove all targets from the shared pool?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._pool.clear()
            self._refresh_pool()

    # ── Auto-routing rules ───────────────────────────────────────────

    def _refresh_rules(self) -> None:
        """Rebuild the rules list from :meth:`AutoRouter.list_rules`."""
        self._rule_list.clear()
        for rule in self._router.list_rules():
            self._rule_list.addItem(self._format_rule(rule))

    @staticmethod
    def _format_rule(rule: RoutingRule) -> QListWidgetItem:
        tt = rule.target_type.value if rule.target_type else "any"
        ssid = f" ssid~'{rule.ssid_pattern}'" if rule.ssid_pattern else ""
        text = (
            f"{rule.name}: [{tt}{ssid} rssi>={rule.min_rssi}] "
            f"-> {rule.device_port}: {rule.command_template}"
        )
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, rule.name)
        if not rule.enabled:
            item.setForeground(Qt.gray)
        return item

    def _device_ports(self) -> list[str]:
        try:
            return [d.port for d in self._dm.list_devices()]
        except Exception:  # noqa: BLE001 — DM should never block the dialog
            log.exception("Failed to enumerate device ports")
            return []

    def _on_add_rule(self) -> None:
        dialog = AddRuleDialog(self._device_ports(), self)
        if dialog.exec_() != QDialog.Accepted:
            return
        rule = dialog.get_rule()
        if not rule.command_template:
            QMessageBox.warning(self, "Invalid Rule", "A command template is required.")
            return
        if not rule.device_port:
            QMessageBox.warning(self, "Invalid Rule", "A destination device port is required.")
            return
        self._router.add_rule(rule)
        self._refresh_rules()

    def _on_remove_rule(self) -> None:
        item = self._rule_list.currentItem()
        if item is None:
            return
        name = item.data(Qt.UserRole)
        if name and self._router.remove_rule(name):
            self._refresh_rules()

    # ── Qt overrides ─────────────────────────────────────────────────

    def showEvent(self, event) -> None:  # noqa: N802 — Qt naming
        super().showEvent(event)
        self._refresh_pool()
        self._refresh_rules()
