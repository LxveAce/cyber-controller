"""Device tab — serial terminal UI with device list and command palette."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core.device_manager import DeviceManager
from src.core.serial_handler import ConnectionState, SerialConnection
from src.models.device import Device
from src.protocols.marauder import MarauderProtocol
from src.protocols.ghost_esp import GhostESPProtocol
from src.protocols.base import CommandInfo

log = logging.getLogger(__name__)

# Aggregate all known commands for the command palette
_ALL_PROTOCOLS = [MarauderProtocol(), GhostESPProtocol()]


class _LineSignal(QObject):
    """Helper to bridge threaded serial callbacks to Qt signals."""
    line_received = pyqtSignal(str)


class DeviceTab(QWidget):
    """Device management tab with list, serial terminal, and command palette."""

    def __init__(self, dm: DeviceManager, pool=None, ingestor=None) -> None:
        super().__init__()
        self._dm = dm
        # Cross-comm: feed this device's parsed serial output (APs/clients) into the shared TargetPool
        # so the AutoRouter can act on it across devices. Optional (backward-compatible) — when a pool
        # is supplied without an ingestor we make one. See src/core/target_ingest.py.
        self._pool = pool
        self._ingestor = ingestor
        if self._pool is not None and self._ingestor is None:
            from src.core.target_ingest import TargetIngestor
            self._ingestor = TargetIngestor(self._pool)
        self._active_conn: SerialConnection | None = None
        self._active_port: str = ""
        self._dms_auth = None  # Optional DeadManAuth instance, set by main window
        self._line_signal = _LineSignal()
        self._line_signal.line_received.connect(self._on_line_received)

        self._build_ui()
        self._refresh_devices()

        # Auto-refresh device list every 3 seconds
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_devices)
        self._timer.start(3000)

    # ── Layout ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Horizontal)

        # ── Left: device list (in scroll area) ──────────────────────
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.NoFrame)
        left_scroll.setMinimumWidth(160)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel("Devices")
        lbl.setObjectName("card_title")
        left_layout.addWidget(lbl)

        self._device_list = QListWidget()
        self._device_list.setMinimumHeight(80)
        self._device_list.currentItemChanged.connect(self._on_device_selected)
        left_layout.addWidget(self._device_list, stretch=1)

        btn_row = QHBoxLayout()
        self._btn_connect = QPushButton("Connect")
        self._btn_connect.clicked.connect(self._on_connect)
        btn_row.addWidget(self._btn_connect)

        self._btn_disconnect = QPushButton("Disconnect")
        self._btn_disconnect.setEnabled(False)
        self._btn_disconnect.clicked.connect(self._on_disconnect)
        btn_row.addWidget(self._btn_disconnect)

        left_layout.addLayout(btn_row)

        btn_refresh = QPushButton("Scan Ports")
        btn_refresh.clicked.connect(self._scan_and_add)
        left_layout.addWidget(btn_refresh)

        left_scroll.setWidget(left)
        splitter.addWidget(left_scroll)

        # ── Right: serial terminal ───────────────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self._term_label = QLabel("Serial Terminal")
        self._term_label.setObjectName("card_title")
        self._term_label.setWordWrap(True)
        right_layout.addWidget(self._term_label)

        self._terminal = QTextEdit()
        self._terminal.setReadOnly(True)
        self._terminal.setObjectName("terminal")
        self._terminal.setMinimumHeight(100)
        right_layout.addWidget(self._terminal, stretch=1)

        # Command input row
        cmd_row = QHBoxLayout()

        self._cmd_palette = QComboBox()
        self._cmd_palette.setEditable(False)
        self._cmd_palette.setMinimumWidth(140)
        self._populate_palette()
        self._cmd_palette.currentIndexChanged.connect(self._on_palette_select)
        cmd_row.addWidget(self._cmd_palette, stretch=1)

        self._cmd_input = QLineEdit()
        self._cmd_input.setPlaceholderText("Type command or select from palette...")
        self._cmd_input.returnPressed.connect(self._on_send)
        cmd_row.addWidget(self._cmd_input, stretch=3)

        self._btn_send = QPushButton("Send")
        self._btn_send.clicked.connect(self._on_send)
        self._btn_send.setEnabled(False)
        cmd_row.addWidget(self._btn_send)

        right_layout.addLayout(cmd_row)
        splitter.addWidget(right)

        # Splitter proportions
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter)

    # ── Device list ──────────────────────────────────────────────────

    def _refresh_devices(self) -> None:
        """Update the list widget from the device manager."""
        selected_port = self._active_port
        self._device_list.clear()
        for dev in self._dm.list_devices():
            item = QListWidgetItem(dev.display_name)
            item.setData(Qt.UserRole, dev.port)
            if dev.connected:
                item.setForeground(QColor("#39ff14"))
            else:
                item.setForeground(QColor("#8b949e"))
            self._device_list.addItem(item)
            if dev.port == selected_port:
                self._device_list.setCurrentItem(item)

    def _scan_and_add(self) -> None:
        """Scan ports and register any new devices."""
        for dev in self._dm.scan_ports():
            if not self._dm.get_device(dev.port):
                self._dm.add_device(dev)
        self._refresh_devices()

    def _on_device_selected(self, current: QListWidgetItem | None, _prev: QListWidgetItem | None) -> None:
        if current is None:
            return
        port = current.data(Qt.UserRole)
        self._active_port = port
        dev = self._dm.get_device(port)
        if dev:
            self._term_label.setText(f"Serial Terminal — {dev.display_name}")
            connected = dev.connected
            self._btn_connect.setEnabled(not connected)
            self._btn_disconnect.setEnabled(connected)
            self._btn_send.setEnabled(connected)

    # ── Connect / Disconnect ─────────────────────────────────────────

    def _on_connect(self) -> None:
        port = self._active_port
        if not port:
            return
        try:
            conn = self._dm.open_connection(port)
            self._active_conn = conn
            conn.on_line(lambda line: self._line_signal.line_received.emit(line))
            # Cross-comm ingestion: parse this device's serial output into the shared target pool so a
            # scan here can auto-route a command to another connected device (AutoRouter). Defaults to
            # the Marauder parser; a per-device firmware selector can refine this later.
            if self._ingestor is not None:
                try:
                    self._ingestor.attach(conn, MarauderProtocol())
                except Exception as exc:
                    self._terminal.append(f"[cross-comm ingest attach failed: {exc}]")
            self._terminal.clear()
            self._terminal.append(f"[Connected to {port}]")
            self._btn_connect.setEnabled(False)
            self._btn_disconnect.setEnabled(True)
            self._btn_send.setEnabled(True)
            self._refresh_devices()
        except Exception as exc:
            self._terminal.append(f"[Error: {exc}]")

    def _on_disconnect(self) -> None:
        port = self._active_port
        if not port:
            return
        self._dm.close_connection(port)
        self._active_conn = None
        self._terminal.append(f"[Disconnected from {port}]")
        self._btn_connect.setEnabled(True)
        self._btn_disconnect.setEnabled(False)
        self._btn_send.setEnabled(False)
        self._refresh_devices()

    # ── Serial I/O ───────────────────────────────────────────────────

    def _on_send(self) -> None:
        cmd = self._cmd_input.text().strip()
        if not cmd or not self._active_conn:
            return
        try:
            self._active_conn.write(cmd)
            self._terminal.append(f"> {cmd}")
            self._cmd_input.clear()
        except Exception as exc:
            self._terminal.append(f"[Send error: {exc}]")

    def _on_line_received(self, line: str) -> None:
        # Run through Dead Man's Switch auth detection if available
        if self._dms_auth and self._active_conn:
            self._dms_auth.check_line(
                line, lambda pw: self._active_conn.write(pw)
            )
        self._terminal.append(line)

    # ── Command palette ──────────────────────────────────────────────

    def _populate_palette(self) -> None:
        self._cmd_palette.addItem("-- Command Palette --")
        for proto in _ALL_PROTOCOLS:
            for ci in proto.get_commands():
                label = f"[{proto.protocol_name}] {ci.category}: {ci.name}"
                self._cmd_palette.addItem(label, ci.name)

    def _on_palette_select(self, idx: int) -> None:
        if idx <= 0:
            return
        cmd = self._cmd_palette.itemData(idx)
        if cmd:
            self._cmd_input.setText(cmd)
        self._cmd_palette.setCurrentIndex(0)
