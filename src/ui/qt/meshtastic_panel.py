"""Meshtastic control panel — the live UI over a MeshtasticBackend (comms rework, Wave 8).

A stream device (Meshtastic protobuf StreamAPI) has no text command channel, so instead of the terminal's
command grid it gets this panel: a live node table, a channel selector, a received-message log, and a send-text
box driving the node's own ``send_text``. State arrives on the shared EventBus under ``mesh.*`` topics (published
by :class:`~src.core.cross_comm_hub.CrossCommHub` off the decode thread); the backend for the active port is
reached through the live connection (``conn.mesh_backend``).

**Thread safety:** bus callbacks fire on the serial reader thread. Touching a QWidget off the GUI thread is
undefined (segfault risk — ledger C-8). So every bus event is marshaled to the GUI thread through a pyqtSignal
(the queued connection Qt inserts across threads) before any widget is touched.
"""

from __future__ import annotations

import html
import logging

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.ui.qt.theme import colors as C

log = logging.getLogger(__name__)

_MESH_TOPICS = ("mesh.my_info", "mesh.node", "mesh.channel", "mesh.text", "mesh.config_complete")


class MeshtasticPanel(QWidget):
    """Live Meshtastic node/channel/text surface for one connected stream device."""

    # Bridges an off-GUI-thread bus event onto the GUI thread (Qt makes this a queued connection when the
    # emitter thread differs from the receiver thread).
    _evt = pyqtSignal(str, dict)

    def __init__(self, dm, bus=None, parent=None) -> None:
        super().__init__(parent)
        self._dm = dm
        self._bus = bus
        self._port = ""
        self._build_ui()
        self._evt.connect(self._on_evt_gui)
        if bus is not None:
            for topic in _MESH_TOPICS:
                bus.subscribe(topic, self._on_bus_event)
        self._refresh()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        title = QLabel(
            f"<b style='color:{C.ACCENT};'>&#9673; Meshtastic &mdash; LoRa mesh</b><br>"
            f"<span style='color:{C.TEXT_MUTED};font-size:9pt;'>Licensed ISM-band comms (e.g. US 915&nbsp;MHz). "
            "CC drives the node's own StreamAPI &mdash; it reads nodes/channels and sends text; it authors no "
            "RF/interference frames.</span>"
        )
        title.setWordWrap(True)
        title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(title)

        self._status = QLabel("")
        self._status.setStyleSheet(f"color:{C.TEXT_MUTED};font-size:9pt;")
        root.addWidget(self._status)

        # Node table
        self._nodes = QTableWidget(0, 5)
        self._nodes.setHorizontalHeaderLabels(["Node", "Name", "HW", "SNR", "Batt"])
        self._nodes.verticalHeader().setVisible(False)
        self._nodes.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._nodes.setSelectionMode(QAbstractItemView.NoSelection)
        self._nodes.setMinimumHeight(120)
        hdr = self._nodes.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        for col in (2, 3, 4):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        root.addWidget(self._nodes)

        # Channel selector
        ch_row = QHBoxLayout()
        ch_row.addWidget(QLabel("Channel:"))
        self._channel = QComboBox()
        self._channel.setMinimumWidth(160)
        ch_row.addWidget(self._channel)
        ch_row.addStretch(1)
        root.addLayout(ch_row)

        # Message log
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet(
            f"QTextEdit{{background:{C.BG_DEEP};color:{C.TEXT_PRIMARY};"
            f"font-family:{C.FONT_MONO};font-size:9pt;border:1px solid {C.BORDER};}}"
        )
        self._log.setMinimumHeight(120)
        root.addWidget(self._log)

        # Send row
        send_row = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a message to the mesh…")
        self._input.returnPressed.connect(self._send)
        send_row.addWidget(self._input, 1)
        self._send_btn = QPushButton("Send")
        self._send_btn.setStyleSheet(
            f"QPushButton{{background:{C.ACCENT};color:#fff;font-weight:600;padding:6px 14px;"
            f"border-radius:4px;}}QPushButton:hover{{background:{C.ACCENT_BRIGHT};}}"
            f"QPushButton:disabled{{background:{C.BG_INPUT};color:{C.TEXT_DISABLED};}}"
        )
        self._send_btn.clicked.connect(self._send)
        send_row.addWidget(self._send_btn)
        root.addLayout(send_row)

    # ── external control ───────────────────────────────────────────────────────

    def set_port(self, port: str) -> None:
        """Point the panel at the device on *port* (called when the active device changes). Rebuilds the view
        from that device's backend, so state decoded before the panel was shown still appears."""
        if port != self._port:
            self._port = port or ""
            self._log.clear()
        self._refresh()

    # ── backend access ─────────────────────────────────────────────────────────

    def _backend(self):
        if not self._port:
            return None
        conn = self._dm.get_connection(self._port)
        return getattr(conn, "mesh_backend", None) if conn is not None else None

    # ── event handling (GUI thread) ────────────────────────────────────────────

    def _on_bus_event(self, topic: str, payload: dict) -> None:
        # Off the GUI thread — marshal onto it before touching any widget.
        self._evt.emit(topic, dict(payload or {}))

    def _on_evt_gui(self, topic: str, payload: dict) -> None:
        if payload.get("port") not in (self._port, None):
            return  # an event for a different device's mesh
        if topic == "mesh.text":
            frm = payload.get("from_id", "?")
            self._append_log(frm, payload.get("text", ""), incoming=True)
        elif topic in ("mesh.node", "mesh.my_info", "mesh.config_complete"):
            self._refresh_nodes()
            self._refresh_status()
        elif topic == "mesh.channel":
            self._refresh_channels()

    # ── rendering ───────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        self._refresh_nodes()
        self._refresh_channels()
        self._refresh_status()

    def _refresh_status(self) -> None:
        backend = self._backend()
        if backend is None:
            self._status.setText("Connect a Meshtastic device to see its mesh.")
            self._send_btn.setEnabled(False)
            self._input.setEnabled(False)
            return
        self._send_btn.setEnabled(True)
        self._input.setEnabled(True)
        me = ("me " + backend.node_list()[0].node_id) if backend.nodes else "connecting…"
        state = "config synced" if backend.config_complete else "waiting for config…"
        self._status.setText(
            f"{len(backend.nodes)} node(s) · {len(backend.active_channels())} channel(s) · {state} · {me}"
        )

    def _refresh_nodes(self) -> None:
        backend = self._backend()
        nodes = backend.node_list() if backend is not None else []
        self._nodes.setRowCount(len(nodes))
        for row, n in enumerate(nodes):
            name = (n.long_name or "").strip() + (" (this node)" if n.is_local else "")
            snr = "" if n.snr is None else f"{n.snr:.1f}"
            batt = "" if n.battery is None else f"{n.battery}%"
            for col, val in enumerate((n.node_id, name, n.hw_model_name, snr, batt)):
                item = QTableWidgetItem(str(val))
                if col in (3, 4):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self._nodes.setItem(row, col, item)

    def _refresh_channels(self) -> None:
        backend = self._backend()
        chans = backend.active_channels() if backend is not None else []
        current = self._channel.currentData()
        self._channel.blockSignals(True)
        self._channel.clear()
        for c in chans:
            label = f"[{c.index}] {c.name or '(default)'}" + (" ·PRIMARY" if c.role == 1 else "")
            self._channel.addItem(label, c.index)
        if current is not None:  # preserve the user's selection across a refresh
            idx = self._channel.findData(current)
            if idx >= 0:
                self._channel.setCurrentIndex(idx)
        self._channel.blockSignals(False)

    def _append_log(self, sender: str, text: str, incoming: bool) -> None:
        color = C.INFO if incoming else C.TERMINAL
        arrow = "&#8592;" if incoming else "&#8594;"  # ← incoming / → sent
        safe = html.escape(text)
        who = html.escape(sender)
        self._log.append(
            f"<span style='color:{C.TEXT_DIM};'>{arrow}</span> "
            f"<span style='color:{color};font-weight:600;'>{who}</span>: {safe}"
        )

    # ── send ─────────────────────────────────────────────────────────────────────

    def _send(self) -> None:
        backend = self._backend()
        text = self._input.text().strip()
        if backend is None or not text:
            return
        channel = self._channel.currentData()
        channel = int(channel) if channel is not None else 0
        try:
            backend.send_text(text, channel=channel)
        except Exception:  # noqa: BLE001 — a wire error must not crash the UI
            log.exception("meshtastic panel: send failed")
            self._append_log("send failed", text, incoming=False)
            return
        self._append_log("me", text, incoming=False)
        self._input.clear()
