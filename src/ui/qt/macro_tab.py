"""Macro tab — record, edit, and replay serial command sequences."""

from __future__ import annotations

import logging

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtGui import QColor
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
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.device_manager import DeviceManager
from src.core.macro_recorder import Macro, MacroRecorder, is_offensive_macro

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
        root.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Horizontal)

        # ── Left panel: saved macros (in scroll area) ────────────────
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.NoFrame)
        left_scroll.setMinimumWidth(160)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel("Saved Macros")
        lbl.setObjectName("card_title")
        left_layout.addWidget(lbl)

        self._macro_list = QListWidget()
        self._macro_list.setMinimumHeight(80)
        self._macro_list.currentItemChanged.connect(self._on_macro_selected)
        left_layout.addWidget(self._macro_list, stretch=1)

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

        left_scroll.setWidget(left)
        splitter.addWidget(left_scroll)

        # ── Right panel: editor/player (in scroll area) ─────────────
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.NoFrame)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Variable substitution fields card
        self._var_card, var_layout_inner = _make_card("Variable Substitution")
        var_card = self._var_card
        var_row = QHBoxLayout()

        mac_label = QLabel("TARGET_MAC:")
        mac_label.setWordWrap(True)
        var_row.addWidget(mac_label)
        self._var_mac = QLineEdit()
        self._var_mac.setPlaceholderText("AA:BB:CC:DD:EE:FF")
        self._var_mac.setMinimumWidth(100)
        var_row.addWidget(self._var_mac)

        ssid_label = QLabel("TARGET_SSID:")
        ssid_label.setWordWrap(True)
        var_row.addWidget(ssid_label)
        self._var_ssid = QLineEdit()
        self._var_ssid.setPlaceholderText("MyNetwork")
        self._var_ssid.setMinimumWidth(80)
        var_row.addWidget(self._var_ssid)

        ch_label = QLabel("CHANNEL:")
        ch_label.setWordWrap(True)
        var_row.addWidget(ch_label)
        self._var_channel = QLineEdit()
        self._var_channel.setPlaceholderText("6")
        self._var_channel.setMaximumWidth(50)
        self._var_channel.setMinimumWidth(40)
        var_row.addWidget(self._var_channel)

        var_layout_inner.addLayout(var_row)
        right_layout.addWidget(var_card)

        # Macro info
        info_row = QHBoxLayout()
        self._macro_name_label = QLabel("No macro loaded")
        self._macro_name_label.setObjectName("card_title")
        self._macro_name_label.setWordWrap(True)
        info_row.addWidget(self._macro_name_label)
        info_row.addStretch()
        self._macro_info_label = QLabel("")
        self._macro_info_label.setObjectName("muted")
        self._macro_info_label.setWordWrap(True)
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
        self._steps_table.setMinimumHeight(80)
        right_layout.addWidget(self._steps_table, stretch=1)

        # Progress bar
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFormat("Ready")
        self._progress.setMinimumHeight(20)
        right_layout.addWidget(self._progress)

        # Control buttons
        ctrl_row = QHBoxLayout()

        # Port selector for recording/playback
        ctrl_row.addWidget(QLabel("Port:"))
        self._port_combo = QComboBox()
        self._port_combo.setMinimumWidth(100)
        ctrl_row.addWidget(self._port_combo)

        btn_refresh_ports = QPushButton("Refresh")
        btn_refresh_ports.clicked.connect(self._refresh_ports)
        ctrl_row.addWidget(btn_refresh_ports)

        ctrl_row.addStretch()

        # Speed selector
        self._speed_label = QLabel("Speed:")
        ctrl_row.addWidget(self._speed_label)
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

        right_scroll.setWidget(right)
        splitter.addWidget(right_scroll)

        # Splitter proportions — setSizes sets the LAUNCH split (1:3); setStretchFactor only governs resize.
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([240, 720])
        root.addWidget(splitter)

        # Initial port refresh
        self._refresh_ports()

    # ── Macro list management ────────────────────────────────────────

    # ── Dual-depth (Simple / Pro) ────────────────────────────────────

    def set_ui_mode(self, mode: str) -> None:
        """Simple = play saved macros (list, read-only steps, Play/Stop, Load). Hide authoring controls:
        Record, Save, the speed multiplier (locked to 1x), and variable-substitution fields."""
        pro = str(mode).lower() != "simple"
        for w in (
            getattr(self, "_var_card", None), getattr(self, "_speed_label", None),
            getattr(self, "_speed_combo", None), getattr(self, "_btn_record", None),
            getattr(self, "_btn_save", None),
        ):
            if w is not None:
                w.setVisible(pro)
        if not pro and getattr(self, "_speed_combo", None) is not None:
            self._speed_combo.setCurrentText("1x")  # locked playback speed in Simple

    # ── Cross-tab fill (Targets → Macros) ────────────────────────────

    def fill_target_variables(
        self, mac: str = "", ssid: str = "", channel: str = "",
    ) -> None:
        """Populate the variable-substitution fields from a discovered target.

        Called when the user picks "Use as macro target" in the Targets tab (wired in main_window),
        so a target found in one surface is usable in another without retyping the MAC/SSID/channel.
        Empty strings are written through so a partial target clears stale values."""
        self._var_mac.setText(mac or "")
        self._var_ssid.setText(ssid or "")
        self._var_channel.setText(channel or "")

    def _refresh_macro_list(self) -> None:
        """Reload the saved macros list."""
        self._macro_list.clear()
        for info in self._recorder.list_saved_macros():
            lock = "🔒 " if info.get("secured") else ""
            item = QListWidgetItem(
                f"{lock}{info['name']}  ({info['step_count']} steps)"
            )
            if info.get("secured"):
                item.setToolTip("Stored encrypted in the secure container")
            item.setData(Qt.UserRole, info["path"])
            self._macro_list.addItem(item)
        # Empty-state guidance — a single non-selectable hint row when nothing is saved yet.
        if self._macro_list.count() == 0:
            hint = QListWidgetItem(
                "No saved macros yet — press Record, run some commands, then Save."
            )
            hint.setFlags(Qt.NoItemFlags)
            hint.setForeground(QColor("#8b949e"))
            self._macro_list.addItem(hint)

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
                # Never leave the previously loaded macro silently armed for Play:
                # surface the failure (mirroring _on_load_file) and clear stale state.
                self._current_macro = None
                self._clear_display()
                QMessageBox.warning(self, "Load Error", f"Failed to load macro:\n{exc}")

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
                try:
                    self._recorder.delete_macro(path)
                except Exception as exc:  # noqa: BLE001 — surface a delete failure (OS error, secure-store
                    # error) as a dialog instead of letting it escape the slot and abort the app.
                    QMessageBox.critical(self, "Delete Error", f"Failed to delete macro:\n{exc}")
                    return
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

        # Tag the macro with the port's known firmware so replayed commands carry the right protocol
        # (parser + terminator) instead of the generic "any". Unknown firmware falls back to "".
        dev = self._dm.get_device(port)
        protocol = (getattr(dev, "firmware", "") or "") if dev is not None else ""
        self._recorder.start_recording(port, protocol=protocol)
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

        # Arm gate: offensive/transmitting macros (attack templates) must be explicitly confirmed
        # before playback — the engine has no per-macro arm gate, so this is the "user must arm"
        # that keeps a template from firing on a stray Play click. Safe recon macros play ungated.
        if is_offensive_macro(self._current_macro) and not self._confirm_offensive():
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
            # An offensive macro was already confirmed above; the engine now also enforces the arm
            # gate, so tell it this play is armed. Recon macros are unaffected (not offensive).
            armed=True,
            async_=True,
        )

    def _confirm_offensive(self) -> bool:
        """Ask the operator to arm a transmitting/disruptive macro; True only on explicit Yes.

        Defaults to No so a stray click never fires an attack template."""
        reply = QMessageBox.warning(
            self, "Arm this macro?",
            "This macro transmits and can disrupt wireless networks or devices. Only run it on "
            "equipment you own or are explicitly authorized to test.\n\nProceed?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    def _on_save(self) -> None:
        if not self._current_macro:
            return
        try:
            path = self._recorder.save_macro(self._current_macro)
        except Exception as exc:  # noqa: BLE001 — save_macro raises on a locked secure-container gate or any
            # disk/OS error. It must surface as a dialog, not escape this clicked-slot: with no sys.excepthook
            # installed PyQt aborts the whole app, and the just-recorded macro would be lost with no warning.
            # Mirrors settings_tab._on_save + the load path above.
            QMessageBox.critical(self, "Save Error", f"Failed to save macro:\n{exc}")
            return
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
        self._emit_activity(f"step {step + 1}/{total}: {msg}")

    def _on_playback_complete(self, success: bool, msg: str) -> None:
        self._btn_play.setEnabled(True)
        self._btn_record.setEnabled(True)
        self._btn_stop.setEnabled(False)
        if success:
            self._progress.setValue(100)
            self._progress.setFormat("Playback complete")
            self._emit_activity("playback complete", "success")
        else:
            self._progress.setFormat(f"Playback stopped: {msg}")
            self._emit_activity(f"playback stopped: {msg}", "warn")

    @staticmethod
    def _emit_activity(text: str, level: str = "info") -> None:
        """Mirror a macro-playback line to the app-wide activity bus (persistent terminal). Guarded so
        a logging hiccup never interrupts playback."""
        try:
            from src.core.activity_log import activity_log
            activity_log().emit_line("macro", text, level)
        except Exception:  # noqa: BLE001
            pass
