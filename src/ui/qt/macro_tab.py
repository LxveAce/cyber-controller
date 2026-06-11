"""Macro tab — record, edit, and replay serial command sequences."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PyQt5.QtCore import Qt, pyqtSignal, QObject
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core.macro_recorder import Macro, MacroRecorder, MacroStep
from src.core.device_manager import DeviceManager

log = logging.getLogger(__name__)


def _make_card(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    """Create a card-styled QFrame with optional title label."""
    card = QFrame()
    card.setObjectName("card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(8)
    if title:
        lbl = QLabel(title)
        lbl.setObjectName("card_title")
        layout.addWidget(lbl)
    return card, layout


class _PlaybackSignal(QObject):
    """Bridge threaded playback callbacks to Qt signals."""
    progress = pyqtSignal(int, int, str)   # step_index, total, message
    complete = pyqtSignal(bool, str)        # success, message


class MacroTab(QWidget):
    """Macro recording and playback tab.

    Left panel: list of saved macros with load/delete buttons.
    Right panel: macro editor/viewer with Record/Stop/Play controls.
    Variable substitution fields at the top.
    """

    def __init__(self, recorder: MacroRecorder, dm: DeviceManager) -> None:
        super().__init__()
        self._recorder = recorder
        self._dm = dm
        self._current_macro: Macro | None = None
        self._playback_signal = _PlaybackSignal()
        self._playback_signal.progress.connect(self._on_playback_progress)
        self._playback_signal.complete.connect(self._on_playback_complete)

        self._build_ui()
        self._refresh_macro_list()

    # ── Layout ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)

        # ── Left panel: saved macros ─────────────────────────────────
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel("Saved Macros")
        lbl.setObjectName("card_title")
        left_layout.addWidget(lbl)

        self._macro_list = QListWidget()
        self._macro_list.currentItemChanged.connect(self._on_macro_selected)
        left_layout.addWidget(self._macro_list)

        btn_row = QHBoxLayout()
        btn_load = QPushButton("Load File...")
        btn_load.clicked.connect(self._on_load_file)
        btn_row.addWidget(btn_load)

        btn_delete = QPushButton("Delete")
        btn_delete.clicked.connect(self._on_delete_macro)
        btn_row.addWidget(btn_delete)
        left_layout.addLayout(btn_row)

        btn_refresh = QPushButton("Refresh List")
        btn_refresh.clicked.connect(self._refresh_macro_list)
        left_layout.addWidget(btn_refresh)

        splitter.addWidget(left)

        # ── Right panel: editor/player ───────────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Variable substitution fields card
        var_card, var_layout_inner = _make_card("Variable Substitution")
        var_row = QHBoxLayout()

        var_row.addWidget(QLabel("TARGET_MAC:"))
        self._var_mac = QLineEdit()
        self._var_mac.setPlaceholderText("AA:BB:CC:DD:EE:FF")
        var_row.addWidget(self._var_mac)

        var_row.addWidget(QLabel("TARGET_SSID:"))
        self._var_ssid = QLineEdit()
        self._var_ssid.setPlaceholderText("MyNetwork")
        var_row.addWidget(self._var_ssid)

        var_row.addWidget(QLabel("CHANNEL:"))
        self._var_channel = QLineEdit()
        self._var_channel.setPlaceholderText("6")
        self._var_channel.setMaximumWidth(50)
        var_row.addWidget(self._var_channel)

        var_layout_inner.addLayout(var_row)
        right_layout.addWidget(var_card)

        # Macro info
        info_row = QHBoxLayout()
        self._macro_name_label = QLabel("No macro loaded")
        self._macro_name_label.setObjectName("card_title")
        info_row.addWidget(self._macro_name_label)
        info_row.addStretch()
        self._macro_info_label = QLabel("")
        self._macro_info_label.setObjectName("muted")
        info_row.addWidget(self._macro_info_label)
        right_layout.addLayout(info_row)

        # Steps table
        self._steps_table = QTableWidget()
        self._steps_table.setColumnCount(3)
        self._steps_table.setHorizontalHeaderLabels(["Command", "Delay (ms)", "Expected Response"])
        self._steps_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._steps_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self._steps_table.horizontalHeader().resizeSection(1, 100)
        self._steps_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._steps_table.setAlternatingRowColors(True)
        self._steps_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._steps_table.setSelectionBehavior(QTableWidget.SelectRows)
        self._steps_table.verticalHeader().setVisible(False)
        right_layout.addWidget(self._steps_table)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFormat("Ready")
        right_layout.addWidget(self._progress)

        # Control buttons
        ctrl_row = QHBoxLayout()

        # Port selector for recording/playback
        ctrl_row.addWidget(QLabel("Port:"))
        self._port_combo = QComboBox()
        self._port_combo.setMinimumWidth(120)
        ctrl_row.addWidget(self._port_combo)

        btn_refresh_ports = QPushButton("Refresh")
        btn_refresh_ports.clicked.connect(self._refresh_ports)
        ctrl_row.addWidget(btn_refresh_ports)

        ctrl_row.addStretch()

        # Speed selector
        ctrl_row.addWidget(QLabel("Speed:"))
        self._speed_combo = QComboBox()
        self._speed_combo.addItems(["0.25x", "0.5x", "1x", "2x", "4x", "10x"])
        self._speed_combo.setCurrentText("1x")
        ctrl_row.addWidget(self._speed_combo)

        self._btn_record = QPushButton("Record")
        self._btn_record.setObjectName("erase_btn")  # Red styling
        self._btn_record.clicked.connect(self._on_record)
        ctrl_row.addWidget(self._btn_record)

        self._btn_stop = QPushButton("Stop")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop)
        ctrl_row.addWidget(self._btn_stop)

        self._btn_play = QPushButton("Play")
        self._btn_play.setObjectName("flash_btn")  # Green styling
        self._btn_play.setEnabled(False)
        self._btn_play.clicked.connect(self._on_play)
        ctrl_row.addWidget(self._btn_play)

        self._btn_save = QPushButton("Save")
        self._btn_save.setEnabled(False)
        self._btn_save.clicked.connect(self._on_save)
        ctrl_row.addWidget(self._btn_save)

        right_layout.addLayout(ctrl_row)

        splitter.addWidget(right)

        # Splitter proportions
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter)

        # Initial port refresh
        self._refresh_ports()

    # ── Macro list management ────────────────────────────────────────

    def _refresh_macro_list(self) -> None:
        """Reload the saved macros list."""
        self._macro_list.clear()
        for info in self._recorder.list_saved_macros():
            item = QListWidgetItem(
                f"{info['name']}  ({info['step_count']} steps)"
            )
            item.setData(Qt.UserRole, info["path"])
            self._macro_list.addItem(item)

    def _on_macro_selected(self, current: QListWidgetItem | None, _prev: QListWidgetItem | None) -> None:
        if current is None:
            return
        path = current.data(Qt.UserRole)
        if path:
            try:
                self._current_macro = self._recorder.load_macro(path)
                self._display_macro(self._current_macro)
            except Exception as exc:
                log.error("Failed to load macro: %s", exc)

    def _on_load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Macro", "", "JSON Files (*.json)"
        )
        if path:
            try:
                self._current_macro = self._recorder.load_macro(path)
                self._display_macro(self._current_macro)
            except Exception as exc:
                QMessageBox.warning(self, "Load Error", f"Failed to load macro:\n{exc}")

    def _on_delete_macro(self) -> None:
        current = self._macro_list.currentItem()
        if not current:
            return
        path = current.data(Qt.UserRole)
        if path:
            reply = QMessageBox.question(
                self, "Delete Macro",
                f"Delete {current.text()}?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self._recorder.delete_macro(path)
                self._refresh_macro_list()
                self._current_macro = None
                self._clear_display()

    def _display_macro(self, macro: Macro) -> None:
        """Show macro details in the editor panel."""
        self._macro_name_label.setText(macro.name)
        desc = macro.description or "No description"
        proto = macro.device_protocol or "any"
        self._macro_info_label.setText(
            f"{macro.step_count} steps | {macro.total_duration_ms}ms | Protocol: {proto} | {desc}"
        )

        self._steps_table.setRowCount(len(macro.steps))
        for row, step in enumerate(macro.steps):
            self._steps_table.setItem(row, 0, QTableWidgetItem(step.command))
            self._steps_table.setItem(row, 1, QTableWidgetItem(str(step.delay_ms)))
            self._steps_table.setItem(row, 2, QTableWidgetItem(step.expected_response))

        self._btn_play.setEnabled(True)
        self._btn_save.setEnabled(True)
        self._progress.setValue(0)
        self._progress.setFormat("Ready")

    def _clear_display(self) -> None:
        self._macro_name_label.setText("No macro loaded")
        self._macro_info_label.setText("")
        self._steps_table.setRowCount(0)
        self._btn_play.setEnabled(False)
        self._btn_save.setEnabled(False)
        self._progress.setValue(0)
        self._progress.setFormat("Ready")

    # ── Port management ──────────────────────────────────────────────

    def _refresh_ports(self) -> None:
        self._port_combo.clear()
        for dev in self._dm.scan_ports():
            self._port_combo.addItem(f"{dev.port} -- {dev.name}", dev.port)

    # ── Record / Stop / Play ─────────────────────────────────────────

    def _on_record(self) -> None:
        port = self._port_combo.currentData()
        if not port:
            QMessageBox.warning(self, "No Port", "Select a device port first.")
            return

        if self._recorder.is_recording:
            return

        self._recorder.start_recording(port)
        self._btn_record.setText("Recording...")
        self._btn_record.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_play.setEnabled(False)
        self._progress.setFormat("Recording...")
        self._progress.setValue(0)

    def _on_stop(self) -> None:
        if self._recorder.is_recording:
            macro = self._recorder.stop_recording(
                name="Recording",
                description="Recorded macro",
            )
            self._current_macro = macro
            self._display_macro(macro)
            self._btn_record.setText("Record")
            self._btn_record.setEnabled(True)
            self._btn_stop.setEnabled(False)
            self._progress.setFormat("Recording stopped")
        elif self._recorder.is_playing:
            self._recorder.stop_playback()
            self._btn_stop.setEnabled(False)

    def _on_play(self) -> None:
        if not self._current_macro:
            return

        port = self._port_combo.currentData()
        if not port:
            QMessageBox.warning(self, "No Port", "Select a device port first.")
            return

        # Get serial connection
        conn = self._dm.get_connection(port)
        if not conn or not conn.is_connected:
            QMessageBox.warning(
                self, "Not Connected",
                f"Not connected to {port}. Connect first in the Devices tab.",
            )
            return

        # Parse speed
        speed_text = self._speed_combo.currentText().replace("x", "")
        try:
            speed = float(speed_text)
        except ValueError:
            speed = 1.0

        # Gather variables
        variables = {}
        mac = self._var_mac.text().strip()
        if mac:
            variables["TARGET_MAC"] = mac
        ssid = self._var_ssid.text().strip()
        if ssid:
            variables["TARGET_SSID"] = ssid
        channel = self._var_channel.text().strip()
        if channel:
            variables["CHANNEL"] = channel

        # Start playback
        self._btn_play.setEnabled(False)
        self._btn_record.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._progress.setValue(0)

        self._recorder.play(
            macro=self._current_macro,
            send_command=conn.write,
            speed_multiplier=speed,
            variables=variables,
            progress_callback=self._playback_signal.progress.emit,
            complete_callback=self._playback_signal.complete.emit,
            async_=True,
        )

    def _on_save(self) -> None:
        if not self._current_macro:
            return
        path = self._recorder.save_macro(self._current_macro)
        self._refresh_macro_list()
        self._progress.setFormat(f"Saved: {path.name}")

    # ── Playback callbacks (via Qt signals) ──────────────────────────

    def _on_playback_progress(self, step: int, total: int, msg: str) -> None:
        if total > 0:
            pct = int((step / total) * 100)
            self._progress.setValue(pct)
        self._progress.setFormat(f"Step {step + 1}/{total}: {msg}")

        # Highlight current step in table
        if 0 <= step < self._steps_table.rowCount():
            self._steps_table.selectRow(step)

    def _on_playback_complete(self, success: bool, msg: str) -> None:
        self._btn_play.setEnabled(True)
        self._btn_record.setEnabled(True)
        self._btn_stop.setEnabled(False)
        if success:
            self._progress.setValue(100)
            self._progress.setFormat("Playback complete")
        else:
            self._progress.setFormat(f"Playback stopped: {msg}")
