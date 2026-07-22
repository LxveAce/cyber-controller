"""Nodes tab (W1.1b) — manage provisioned wireless nodes from the Qt UI.

A thin view over :class:`src.core.nodes_controller.NodesController`. It is deliberately KEY-FREE: the table
shows only node_id / label / role / epoch cursors / connected / attached — never key bytes (the keys live in
the gate-keyed vault and never leave it). When the access gate is locked the table is replaced by an
unlock notice, so no node op is possible until the gate is open (the controller also fails closed).

Business logic stays in the controller; the button handlers just gather input (dialogs) and delegate to the
plain ``_do_*`` methods, which are unit-tested directly without modal dialogs. Live attach/detach over a
gateway dongle is a separate concern (needs a gateway picker) and lands in a follow-on chunk.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.nodes_controller import NodesController

log = logging.getLogger(__name__)


class NodesTab(QWidget):
    """Provisioning-management view for wireless nodes. Bind to a :class:`NodesController` (or pass a
    DeviceManager and one is built). Refreshes on a light timer to reflect connect/gate changes."""

    _COLS = ["Node", "Label", "Role", "TX epoch", "RX epoch", "Connected", "Attached"]

    def __init__(self, device_manager: Any = None, *, controller: Optional[NodesController] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        if controller is None:
            controller = NodesController(device_manager)
        self._ctrl = controller
        self._build_ui()
        self._refresh()
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._refresh)
        # The 2s poll runs only while the tab is visible (see showEvent/hideEvent) — a hidden/background
        # preview tab then costs ~0 instead of rebuilding its table every 2 seconds forever.

    def showEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        super().showEvent(ev)
        self._refresh()          # catch up immediately when shown
        self._timer.start()

    def hideEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        super().hideEvent(ev)
        self._timer.stop()

    # ── UI ───────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        _scroll = QScrollArea(self)
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QScrollArea.NoFrame)
        _content = QWidget()
        _scroll.setWidget(_content)
        _outer = QVBoxLayout(self)
        _outer.setContentsMargins(0, 0, 0, 0)
        _outer.addWidget(_scroll)
        root = QVBoxLayout(_content)

        banner = QLabel(
            "⚠  The relay/node ESP32 sketches ship as source in firmware/ — compile and flash "
            "them yourself. Provision, attach, and detach here run over a gateway board you've "
            "opened in the Devices tab (a live serial link); over-the-air node discovery from "
            "this view isn't wired up yet."
        )
        banner.setWordWrap(True)
        banner.setStyleSheet(
            "background:#3d2c00;color:#f0c000;border:1px solid #7a5c00;border-radius:6px;"
            "padding:8px 10px;font-weight:600;"
        )
        root.addWidget(banner)

        self._locked_label = QLabel("🔒  Unlock the access gate to manage nodes.")
        self._locked_label.setAlignment(Qt.AlignCenter)
        self._locked_label.setStyleSheet("color:#8b949e;padding:24px;")
        root.addWidget(self._locked_label)

        self._table = QTableWidget(0, len(self._COLS))
        self._table.setHorizontalHeaderLabels(self._COLS)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        root.addWidget(self._table, stretch=1)

        btn_row = QHBoxLayout()
        self._btn_provision = QPushButton("Provision…")
        self._btn_provision.clicked.connect(self._on_provision)
        self._btn_rotate = QPushButton("Rotate key")
        self._btn_rotate.clicked.connect(self._on_rotate)
        self._btn_deprov = QPushButton("Deprovision")
        self._btn_deprov.clicked.connect(self._on_deprovision)
        self._btn_attach = QPushButton("Attach…")
        self._btn_attach.clicked.connect(self._on_attach)
        self._btn_detach = QPushButton("Detach")
        self._btn_detach.clicked.connect(self._on_detach)
        self._btn_refresh = QPushButton("Refresh")
        self._btn_refresh.clicked.connect(self._refresh)
        self._buttons = [self._btn_provision, self._btn_rotate, self._btn_deprov,
                         self._btn_attach, self._btn_detach, self._btn_refresh]
        for b in self._buttons:
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

    # ── refresh / gate state ─────────────────────────────────────────
    def _refresh(self) -> None:
        # Runs on a QTimer tick, so NOTHING may escape onto the Qt event loop, and ANY error (incl. the
        # gate racing unlocked->locked between the check and the read) fails CLOSED — table hidden, actions
        # disabled — rather than showing stale or secret-adjacent state.
        try:
            unlocked = self._ctrl.is_unlocked()
            rows = self._ctrl.list_rows() if unlocked else []
        except Exception:
            log.debug("nodes refresh failed; falling back to the locked state", exc_info=True)
            unlocked, rows = False, []
        self._locked_label.setVisible(not unlocked)
        self._table.setVisible(unlocked)
        for b in self._buttons:
            b.setEnabled(unlocked)
        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            rx = r.get("rx_epoch")
            cells = [
                str(r["node_id"]),
                r.get("label", ""),
                r.get("role", ""),
                str(r.get("tx_epoch", "")),
                "" if rx is None else str(rx),
                "yes" if r.get("connected") else "no",
                "yes" if r.get("attached") else "no",
            ]
            for c, text in enumerate(cells):
                self._table.setItem(i, c, QTableWidgetItem(text))

    def _selected_node_id(self) -> Optional[int]:
        row = self._table.currentRow()
        if row < 0:
            return None
        item = self._table.item(row, 0)
        if item is None:
            return None
        try:
            return int(item.text())
        except ValueError:
            return None

    # ── delegated actions (unit-tested; no dialogs) ──────────────────
    def _do_provision(self, node_id: int, role: str = "host", label: str = "") -> None:
        self._ctrl.provision(node_id, role=role, label=label)
        self._refresh()

    def _do_rotate(self, node_id: int) -> None:
        self._ctrl.rotate(node_id)
        self._refresh()

    def _do_deprovision(self, node_id: int) -> None:
        self._ctrl.deprovision(node_id)
        self._refresh()

    def _do_attach(self, node_id: int, gateway_port: str) -> None:
        self._ctrl.attach_via_port(node_id, gateway_port)
        self._refresh()

    def _do_detach(self, node_id: int) -> None:
        self._ctrl.detach(node_id)
        self._refresh()

    # ── button handlers (dialogs; delegate to _do_*) ─────────────────
    def _on_provision(self) -> None:
        node_id, ok = QInputDialog.getInt(self, "Provision node", "Node ID (0–65535):", 1, 0, 65535)
        if not ok:
            return
        role, ok = QInputDialog.getItem(self, "Provision node", "Role:", ["host", "node"], 0, False)
        if not ok:
            return
        label, ok = QInputDialog.getText(self, "Provision node", "Label (optional):")
        if not ok:
            return
        try:
            self._do_provision(node_id, role, label)
        except Exception as exc:
            QMessageBox.warning(self, "Provision failed", str(exc))

    def _on_rotate(self) -> None:
        node_id = self._selected_node_id()
        if node_id is None:
            return
        if QMessageBox.question(
            self, "Rotate key",
            f"Rotate the key for node {node_id}? The old key stops working — the node must be re-flashed.",
        ) != QMessageBox.Yes:
            return
        try:
            self._do_rotate(node_id)
        except Exception as exc:
            QMessageBox.warning(self, "Rotate failed", str(exc))

    def _on_deprovision(self) -> None:
        node_id = self._selected_node_id()
        if node_id is None:
            return
        if QMessageBox.question(
            self, "Deprovision node", f"Delete node {node_id} and its key from the vault?"
        ) != QMessageBox.Yes:
            return
        try:
            self._do_deprovision(node_id)
        except Exception as exc:
            QMessageBox.warning(self, "Deprovision failed", str(exc))

    def _on_attach(self) -> None:
        node_id = self._selected_node_id()
        if node_id is None:
            return
        gateways = self._ctrl.available_gateways()
        if not gateways:
            QMessageBox.information(
                self, "Attach node",
                "No connected gateway device. Connect a serial dongle (Devices tab) to relay this node first.",
            )
            return
        ports = [g["port"] for g in gateways]
        labels = [f"{g['port']}  ({g['name']})" if g.get("name") and g["name"] != g["port"] else g["port"]
                  for g in gateways]
        choice, ok = QInputDialog.getItem(self, "Attach node", "Gateway device:", labels, 0, False)
        if not ok:
            return
        gateway_port = ports[labels.index(choice)]
        try:
            self._do_attach(node_id, gateway_port)
        except Exception as exc:
            QMessageBox.warning(self, "Attach failed", str(exc))

    def _on_detach(self) -> None:
        node_id = self._selected_node_id()
        if node_id is None:
            return
        try:
            self._do_detach(node_id)
        except Exception as exc:
            QMessageBox.warning(self, "Detach failed", str(exc))
