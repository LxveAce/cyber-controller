"""PyQt5 main window — tabbed interface for Cyber Controller."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from PyQt5.QtCore import Qt, QByteArray, QSettings, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QFont, QKeySequence, QPalette
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QShortcut,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.core.cross_comm import AutoRouter, EventBus, TargetPool
from src.core.target_ingest import TargetIngestor
from src.core.firmware_vault import FirmwareVault
from src.core.health_monitor import HealthMonitor
from src.core.macro_recorder import MacroRecorder
from src.ui.qt.flash_tab import FlashTab
from src.ui.qt.device_tab import DeviceTab
from src.ui.qt.health_tab import HealthTab
from src.ui.qt.macro_tab import MacroTab
from src.ui.qt.targets_tab import TargetsTab
from src.ui.qt.cross_comm_tab import CrossCommTab
from src.ui.qt.settings_tab import SettingsTab
from src.ui.qt.theme import apply_theme
from src.ui.qt.widgets.cc_logo import CCLogo
from src.ui.qt.widgets.cc_icon import create_cc_icon
from src.ui.qt.widgets.command_palette import CommandPalette
from src.core.deadman_auth import DeadManAuth

log = logging.getLogger(__name__)

_VERSION = "0.3.0"
_GITHUB_URL = "https://github.com/LxveAce/cyber-controller"


def _placeholder_tab(label_text: str) -> QWidget:
    """Create a simple placeholder tab with a centred label."""
    w = QWidget()
    layout = QVBoxLayout(w)
    lbl = QLabel(label_text)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setFont(QFont("Segoe UI", 14))
    lbl.setObjectName("muted")
    layout.addWidget(lbl)
    return w


class CyberControllerWindow(QMainWindow):
    """Main application window with tabbed interface."""

    # Signal emitted when a device is selected in the sidebar
    device_selected = pyqtSignal(str)  # port string

    def __init__(
        self,
        device_manager: DeviceManager,
        flash_engine: FlashEngine,
        event_bus: EventBus,
        target_pool: TargetPool,
        firmware_vault: FirmwareVault | None = None,
        health_monitor: HealthMonitor | None = None,
        macro_recorder: MacroRecorder | None = None,
    ) -> None:
        super().__init__()
        self._dm = device_manager
        self._fe = flash_engine
        self._bus = event_bus
        self._pool = target_pool
        self._vault = firmware_vault or FirmwareVault()
        self._health = health_monitor or HealthMonitor()
        self._macro = macro_recorder or MacroRecorder()
        # Auto-router drives cross-device routing rules; send_command writes to a port.
        self._router = AutoRouter(self._bus, self._send_to_port)
        # Target ingestor feeds each connected device's parsed serial output (APs/clients) into the
        # shared pool, completing the cross-comm loop: a scan on device A -> target.added -> AutoRouter
        # -> a command on device B. DeviceTab attaches it per-connection.
        self._ingestor = TargetIngestor(self._pool)

        # Dead Man's Switch auth flow
        self._dms_auth = DeadManAuth()
        self._dms_auth.set_auth_handler(self._dms_password_prompt)
        self._dms_auth.set_result_handler(self._dms_auth_result)

        # Start health monitor polling
        self._health.start()

        self.setWindowTitle(f"Cyber Controller v{_VERSION}")
        self.setMinimumSize(900, 600)
        self.setWindowIcon(create_cc_icon())

        # QSettings for persisting splitter state
        self._qsettings = QSettings("LxveAce", "CyberController")

        self._build_menu_bar()
        self._build_main_layout()
        self._build_status_bar()

        # Periodic status-bar refresh
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start(2000)

        # Sidebar device list refresh
        self._sidebar_timer = QTimer(self)
        self._sidebar_timer.timeout.connect(self._refresh_sidebar_devices)
        self._sidebar_timer.start(3000)

    # ── Menu bar ─────────────────────────────────────────────────────

    def _build_menu_bar(self) -> None:
        mb = self.menuBar()

        # File
        file_menu = mb.addMenu("&File")

        act_new = QAction("&New Session", self)
        act_new.setShortcut("Ctrl+N")
        act_new.triggered.connect(self._on_new_session)
        file_menu.addAction(act_new)

        act_open = QAction("&Open Session...", self)
        act_open.setShortcut("Ctrl+O")
        act_open.triggered.connect(self._on_open_session)
        file_menu.addAction(act_open)

        act_save = QAction("&Save Session", self)
        act_save.setShortcut("Ctrl+S")
        act_save.triggered.connect(self._on_save_session)
        file_menu.addAction(act_save)

        file_menu.addSeparator()

        act_quit = QAction("&Quit", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        # View
        view_menu = mb.addMenu("&View")

        act_font_up = QAction("Font Size &+", self)
        act_font_up.setShortcut("Ctrl+=")
        act_font_up.triggered.connect(lambda: self._change_font_size(1))
        view_menu.addAction(act_font_up)

        act_font_down = QAction("Font Size &-", self)
        act_font_down.setShortcut("Ctrl+-")
        act_font_down.triggered.connect(lambda: self._change_font_size(-1))
        view_menu.addAction(act_font_down)

        # Tools
        tools_menu = mb.addMenu("&Tools")

        act_suicide = QAction("&Dead Man's Switch Setup…", self)
        act_suicide.setStatusTip("Provision the Dead Man's Switch boot password & duress config (host-side).")
        act_suicide.triggered.connect(self._on_suicide_setup)
        tools_menu.addAction(act_suicide)

        # Help
        help_menu = mb.addMenu("&Help")

        act_guide = QAction("&User Guide", self)
        act_guide.triggered.connect(self._on_user_guide)
        help_menu.addAction(act_guide)

        act_shortcuts = QAction("&Keyboard Shortcuts", self)
        act_shortcuts.triggered.connect(self._on_keyboard_shortcuts)
        help_menu.addAction(act_shortcuts)

        act_palette = QAction("Command &Palette", self)
        act_palette.setShortcut("Ctrl+Shift+P")
        act_palette.triggered.connect(self._on_command_palette)
        help_menu.addAction(act_palette)

        help_menu.addSeparator()

        act_about = QAction("&About", self)
        act_about.triggered.connect(self._on_about)
        help_menu.addAction(act_about)

        act_github = QAction("&GitHub", self)
        act_github.triggered.connect(self._on_github)
        help_menu.addAction(act_github)

        # ── Global shortcuts ────────────────────────────────────────
        shortcut_f5 = QShortcut(QKeySequence("F5"), self)
        shortcut_f5.activated.connect(self._on_sidebar_scan)

        shortcut_suicide = QShortcut(QKeySequence("Ctrl+Shift+S"), self)
        shortcut_suicide.activated.connect(self._on_suicide_setup)

    # ── Main layout with sidebar + tabs ──────────────────────────────

    def _build_main_layout(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Vertical splitter: top (sidebar+tabs) / bottom (terminal) ──
        self._main_splitter = QSplitter(Qt.Vertical)
        main_layout.addWidget(self._main_splitter)

        # ── Top half: sidebar + tabs ─────────────────────────────────
        top_widget = QWidget()
        top_layout = QHBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)

        # ── Sidebar ──────────────────────────────────────────────────
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(160)
        sidebar.setMaximumWidth(280)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)

        # CC Logo (replaces plain text title)
        logo = CCLogo()
        logo_container = QHBoxLayout()
        logo_container.setContentsMargins(10, 8, 10, 4)
        logo_container.addStretch()
        logo_container.addWidget(logo)
        logo_container.addStretch()
        sidebar_layout.addLayout(logo_container)

        # Separator
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: #30363d;")
        sidebar_layout.addWidget(sep)

        # Connection status indicator
        self._conn_status_label = QLabel("No device connected")
        self._conn_status_label.setStyleSheet(
            "color: #8b949e; font-size: 8pt; padding: 4px 8px; background: transparent;"
        )
        self._conn_status_label.setWordWrap(True)
        sidebar_layout.addWidget(self._conn_status_label)

        # Device count
        self._device_count_label = QLabel("0 devices")
        self._device_count_label.setObjectName("device_count")
        sidebar_layout.addWidget(self._device_count_label)

        # Device list
        self._sidebar_device_list = QListWidget()
        self._sidebar_device_list.currentItemChanged.connect(self._on_sidebar_device_selected)
        sidebar_layout.addWidget(self._sidebar_device_list)

        # Quick-action buttons
        quick_actions = QHBoxLayout()
        quick_actions.setContentsMargins(4, 4, 4, 4)
        quick_actions.setSpacing(4)

        btn_send_cmd = QPushButton("Send Command")
        btn_send_cmd.setStyleSheet("font-size: 8pt; padding: 4px 6px;")
        btn_send_cmd.setToolTip("Open a quick input dialog to send a command to the active device")
        btn_send_cmd.clicked.connect(self._on_quick_send_command)
        quick_actions.addWidget(btn_send_cmd)

        btn_start_macro = QPushButton("Start Macro")
        btn_start_macro.setStyleSheet("font-size: 8pt; padding: 4px 6px;")
        btn_start_macro.setToolTip("Switch to the Macros tab and start recording")
        btn_start_macro.clicked.connect(self._on_quick_start_macro)
        quick_actions.addWidget(btn_start_macro)

        sidebar_layout.addLayout(quick_actions)

        # Scan ports button
        scan_btn = QPushButton("Scan Ports")
        scan_btn.clicked.connect(self._on_sidebar_scan)
        sidebar_layout.addWidget(scan_btn)

        top_layout.addWidget(sidebar)

        # ── Tab widget (right side) ──────────────────────────────────
        self._tabs = QTabWidget()
        top_layout.addWidget(self._tabs)

        self._main_splitter.addWidget(top_widget)

        # ── Bottom half: persistent terminal ─────────────────────────
        self._build_persistent_terminal()

        # Splitter proportions: ~65% top, ~35% bottom
        self._main_splitter.setStretchFactor(0, 65)
        self._main_splitter.setStretchFactor(1, 35)

        # Restore saved splitter position if available
        saved_splitter = self._qsettings.value("main_splitter_state")
        if saved_splitter:
            self._main_splitter.restoreState(saved_splitter)

        self._build_tabs()
        # Default to Devices tab (index 1) — after initial setup/flash, users spend
        # most time on device control.
        self._tabs.setCurrentIndex(1)
        self._refresh_sidebar_devices()
        self._build_command_palette()

    # ── Tabs ─────────────────────────────────────────────────────────

    def _build_tabs(self) -> None:
        # Flash tab (functional, with vault integration)
        self._flash_tab = FlashTab(self._dm, self._fe, self._vault)
        self._tabs.addTab(self._flash_tab, "Flash")

        # Device tab (functional)
        self._device_tab = DeviceTab(self._dm, self._pool, self._ingestor)
        self._device_tab._dms_auth = self._dms_auth
        self._tabs.addTab(self._device_tab, "Devices")

        # Health tab (new)
        self._health_tab = HealthTab(self._health)
        self._tabs.addTab(self._health_tab, "Health")

        # Macro tab (new)
        self._macro_tab = MacroTab(self._macro, self._dm)
        self._tabs.addTab(self._macro_tab, "Macros")

        # Target pool (shared discovered targets)
        self._targets_tab = TargetsTab(self._pool, self._bus)
        self._tabs.addTab(self._targets_tab, "Targets")

        # Cross-comm routing (event stream + auto-routing rules)
        self._cross_comm_tab = CrossCommTab(self._bus, self._pool, self._router, self._dm)
        self._tabs.addTab(self._cross_comm_tab, "Cross-Comm")

        # Mission planner (model exists; UI pending)
        self._tabs.addTab(_placeholder_tab("Mission Planner -- coming soon"), "Missions")

        # Settings (persisted)
        self._settings_tab = SettingsTab()
        self._tabs.addTab(self._settings_tab, "Settings")

    # ── Persistent terminal (bottom dock) ──────────────────────────

    # ── Device colors for multi-device terminal ───────────────────
    _DEVICE_COLORS = ["#39ff14", "#58a6ff", "#f0883e", "#f85149", "#d2a8ff"]

    def _build_persistent_terminal(self) -> None:
        """Build the always-visible multi-device terminal panel at the bottom."""
        term_frame = QFrame()
        term_frame.setObjectName("persistent_terminal_frame")
        term_frame.setStyleSheet(
            """
            QFrame#persistent_terminal_frame {
                background-color: #0d1117;
                border-top: 1px solid #30363d;
            }
            """
        )
        term_layout = QHBoxLayout(term_frame)
        term_layout.setContentsMargins(8, 4, 8, 4)
        term_layout.setSpacing(6)

        # ── Left side: device checklist ──────────────────────────────
        device_panel = QVBoxLayout()
        device_panel.setSpacing(4)

        self._pterm_label = QLabel("Devices")
        self._pterm_label.setStyleSheet(
            "color: #39ff14; font-size: 9pt; font-weight: bold; "
            "font-family: 'JetBrains Mono', monospace; background: transparent;"
        )
        device_panel.addWidget(self._pterm_label)

        # Select All checkbox
        self._pterm_select_all = QCheckBox("Select All")
        self._pterm_select_all.setStyleSheet(
            "QCheckBox { color: #8b949e; font-size: 8pt; background: transparent; }"
        )
        self._pterm_select_all.stateChanged.connect(self._pterm_on_select_all)
        device_panel.addWidget(self._pterm_select_all)

        # Device checklist (replaces the old port combo)
        self._pterm_device_list = QListWidget()
        self._pterm_device_list.setMinimumWidth(160)
        self._pterm_device_list.setMaximumWidth(220)
        self._pterm_device_list.setStyleSheet(
            "QListWidget { background: #161b22; color: #e6edf3; border: 1px solid #30363d; "
            "border-radius: 4px; font-size: 8pt; }"
            "QListWidget::item { padding: 2px 4px; }"
        )
        device_panel.addWidget(self._pterm_device_list, stretch=1)

        # Connect / Disconnect buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self._pterm_btn_connect = QPushButton("Connect")
        self._pterm_btn_connect.setStyleSheet(
            "font-size: 8pt; padding: 3px 10px; background: #238636; color: #fff; "
            "border: none; border-radius: 4px;"
        )
        self._pterm_btn_connect.clicked.connect(self._pterm_on_connect)
        btn_row.addWidget(self._pterm_btn_connect)

        self._pterm_btn_disconnect = QPushButton("Disconnect")
        self._pterm_btn_disconnect.setStyleSheet(
            "font-size: 8pt; padding: 3px 10px; background: #da3633; color: #fff; "
            "border: none; border-radius: 4px;"
        )
        self._pterm_btn_disconnect.clicked.connect(self._pterm_on_disconnect)
        btn_row.addWidget(self._pterm_btn_disconnect)
        device_panel.addLayout(btn_row)

        term_layout.addLayout(device_panel)

        # ── Right side: terminal output + input ──────────────────────
        terminal_panel = QVBoxLayout()
        terminal_panel.setSpacing(4)

        term_header = QLabel("Terminal")
        term_header.setStyleSheet(
            "color: #39ff14; font-size: 10pt; font-weight: bold; "
            "font-family: 'JetBrains Mono', monospace; background: transparent;"
        )
        terminal_panel.addWidget(term_header)

        # Terminal output
        self._pterm_output = QTextEdit()
        self._pterm_output.setReadOnly(True)
        self._pterm_output.setObjectName("terminal")
        self._pterm_output.setStyleSheet(
            "QTextEdit#terminal { background-color: #0d1117; color: #39ff14; "
            "font-family: 'JetBrains Mono', 'Consolas', monospace; font-size: 9pt; "
            "border: 1px solid #30363d; border-radius: 4px; padding: 6px; }"
        )
        terminal_panel.addWidget(self._pterm_output, stretch=1)

        # Command input row
        input_row = QHBoxLayout()
        input_row.setSpacing(4)

        prompt_label = QLabel(">")
        prompt_label.setStyleSheet(
            "color: #39ff14; font-family: 'JetBrains Mono', monospace; "
            "font-size: 10pt; font-weight: bold; background: transparent;"
        )
        input_row.addWidget(prompt_label)

        self._pterm_input = QLineEdit()
        self._pterm_input.setPlaceholderText("Type command and press Enter (sent to all checked devices)...")
        self._pterm_input.setStyleSheet(
            "QLineEdit { background-color: #161b22; color: #e6edf3; "
            "font-family: 'JetBrains Mono', 'Consolas', monospace; font-size: 9pt; "
            "border: 1px solid #30363d; border-radius: 4px; padding: 6px; }"
            "QLineEdit:focus { border-color: #39ff14; }"
        )
        self._pterm_input.returnPressed.connect(self._pterm_on_send)
        input_row.addWidget(self._pterm_input)

        terminal_panel.addLayout(input_row)

        term_layout.addLayout(terminal_panel, stretch=1)

        self._main_splitter.addWidget(term_frame)

        # Internal state for multi-device persistent terminal connections
        # Maps port -> SerialConnection
        self._pterm_conns: dict[str, object] = {}
        # Maps port -> color (assigned on connect)
        self._pterm_port_colors: dict[str, str] = {}

        # Bridge serial callbacks to the Qt thread (carries port + line)
        from PyQt5.QtCore import QObject, pyqtSignal as _sig

        class _PTermLineSignal(QObject):
            line_received = _sig(str, str)  # (port, line)

        self._pterm_line_signal = _PTermLineSignal()
        self._pterm_line_signal.line_received.connect(self._pterm_on_line)

        # Refresh device checklist
        self._pterm_refresh_ports()

    def _pterm_refresh_ports(self) -> None:
        """Refresh the persistent terminal device checklist from the device manager."""
        # Remember which ports were checked
        checked_ports: set[str] = set()
        for i in range(self._pterm_device_list.count()):
            item = self._pterm_device_list.item(i)
            if item.checkState() == Qt.Checked:
                checked_ports.add(item.data(Qt.UserRole))

        self._pterm_device_list.clear()
        for dev in self._dm.list_devices():
            # Show connection status dot
            prefix = "@ " if dev.port in self._pterm_conns else ""
            item = QListWidgetItem(f"{prefix}{dev.port} -- {dev.display_name}")
            item.setData(Qt.UserRole, dev.port)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            # Restore check state or default to unchecked
            if dev.port in checked_ports:
                item.setCheckState(Qt.Checked)
            else:
                item.setCheckState(Qt.Unchecked)
            # Color connected devices
            if dev.port in self._pterm_conns:
                color = self._pterm_port_colors.get(dev.port, "#39ff14")
                item.setForeground(QColor(color))
            else:
                item.setForeground(QColor("#8b949e"))
            self._pterm_device_list.addItem(item)

    def _pterm_on_select_all(self, state: int) -> None:
        """Toggle all device checkboxes on/off."""
        check = Qt.Checked if state == Qt.Checked else Qt.Unchecked
        for i in range(self._pterm_device_list.count()):
            self._pterm_device_list.item(i).setCheckState(check)

    def _pterm_checked_ports(self) -> list[str]:
        """Return a list of ports that are currently checked in the device list."""
        ports = []
        for i in range(self._pterm_device_list.count()):
            item = self._pterm_device_list.item(i)
            if item.checkState() == Qt.Checked:
                port = item.data(Qt.UserRole)
                if port:
                    ports.append(port)
        return ports

    def _pterm_assign_color(self, port: str) -> str:
        """Assign a color to a port from the cycling palette."""
        if port in self._pterm_port_colors:
            return self._pterm_port_colors[port]
        used = set(self._pterm_port_colors.values())
        for color in self._DEVICE_COLORS:
            if color not in used:
                self._pterm_port_colors[port] = color
                return color
        # All colors used, cycle based on count
        idx = len(self._pterm_port_colors) % len(self._DEVICE_COLORS)
        color = self._DEVICE_COLORS[idx]
        self._pterm_port_colors[port] = color
        return color

    def _pterm_on_connect(self) -> None:
        """Connect the persistent terminal to all checked ports."""
        ports = self._pterm_checked_ports()
        if not ports:
            self._pterm_output.append(
                '<span style="color:#f85149;">[No devices checked -- check one or more devices]</span>'
            )
            return
        for port in ports:
            if port in self._pterm_conns:
                continue  # already connected
            try:
                conn = self._dm.open_connection(port)
                self._pterm_conns[port] = conn
                color = self._pterm_assign_color(port)
                # Capture port in closure
                _port = port
                conn.on_line(lambda line, p=_port: self._pterm_line_signal.line_received.emit(p, line))
                self._pterm_output.append(
                    f'<span style="color:{color};">[{port}] Connected</span>'
                )
            except Exception as exc:
                self._pterm_output.append(
                    f'<span style="color:#f85149;">[{port}] Connection error: {exc}</span>'
                )
        self._pterm_refresh_ports()
        self._refresh_sidebar_devices()

    def _pterm_on_disconnect(self) -> None:
        """Disconnect the persistent terminal from all checked ports."""
        ports = self._pterm_checked_ports()
        if not ports:
            # If nothing checked, disconnect all
            ports = list(self._pterm_conns.keys())
        for port in ports:
            if port not in self._pterm_conns:
                continue
            try:
                self._dm.close_connection(port)
            except Exception:
                pass
            del self._pterm_conns[port]
            color = self._pterm_port_colors.get(port, "#8b949e")
            self._pterm_output.append(
                f'<span style="color:{color};">[{port}] Disconnected</span>'
            )
        self._pterm_refresh_ports()
        self._refresh_sidebar_devices()

    def _pterm_on_send(self) -> None:
        """Send a command from the persistent terminal to all checked+connected devices."""
        cmd = self._pterm_input.text().strip()
        if not cmd:
            return
        checked = self._pterm_checked_ports()
        # Filter to only connected ports
        targets = [p for p in checked if p in self._pterm_conns]
        if not targets:
            self._pterm_output.append(
                '<span style="color:#f85149;">[No connected devices checked -- check and connect first]</span>'
            )
            return
        for port in targets:
            conn = self._pterm_conns[port]
            color = self._pterm_port_colors.get(port, "#58a6ff")
            try:
                conn.write(cmd)
                self._pterm_output.append(
                    f'<span style="color:{color};">[{port}] &gt; {cmd}</span>'
                )
            except Exception as exc:
                self._pterm_output.append(
                    f'<span style="color:#f85149;">[{port}] Send error: {exc}</span>'
                )
        self._pterm_input.clear()

    @pyqtSlot(str, str)
    def _pterm_on_line(self, port: str, line: str) -> None:
        """Handle a serial line from a device in the persistent terminal."""
        # Run through Dead Man's Switch auth detection
        conn = self._pterm_conns.get(port)
        if conn:
            handled = self._dms_auth.check_line(
                line, lambda pw: conn.write(pw)
            )
            if handled:
                pass
        color = self._pterm_port_colors.get(port, "#39ff14")
        self._pterm_output.append(
            f'<span style="color:{color};">[{port}]</span> {line}'
        )
        # Also mirror to the device tab terminal if it's connected to the same port
        if (
            hasattr(self._device_tab, '_active_port')
            and self._device_tab._active_port == port
            and hasattr(self._device_tab, '_terminal')
        ):
            self._device_tab._terminal.append(line)

    # ── Dead Man's Switch auth UI ────────────────────────────────────

    def _dms_password_prompt(self) -> str | None:
        """Show a password dialog for DMS authentication. Returns password or None."""
        dlg = QInputDialog(self)
        dlg.setWindowTitle("Dead Man's Switch — Authentication Required")
        dlg.setLabelText(
            "The connected device requires a Dead Man's Switch password.\n"
            "Enter the boot password to unlock:"
        )
        dlg.setTextEchoMode(QLineEdit.Password)
        dlg.setStyleSheet(
            "QInputDialog { background-color: #0d1117; color: #e6edf3; }"
            "QLabel { color: #f0883e; font-size: 10pt; background: transparent; }"
            "QLineEdit { background-color: #161b22; color: #e6edf3; "
            "border: 1px solid #f0883e; border-radius: 4px; padding: 6px; "
            "font-family: 'JetBrains Mono', monospace; font-size: 10pt; }"
            "QPushButton { background: #238636; color: #fff; border: none; "
            "border-radius: 4px; padding: 6px 16px; font-size: 9pt; }"
            "QPushButton:hover { background: #2ea043; }"
        )
        ok = dlg.exec_()
        if ok:
            return dlg.textValue()
        return None

    def _dms_auth_result(self, success: bool, message: str) -> None:
        """Handle DMS auth result — show in persistent terminal with coloring."""
        if success:
            self._pterm_output.append(
                f'<span style="color:#39ff14; font-weight:bold;">'
                f'[DMS] Authenticated: {message}</span>'
            )
        else:
            self._pterm_output.append(
                f'<span style="color:#f85149; font-weight:bold;">'
                f'[DMS] Auth failed: {message}</span>'
            )

    # ── Sidebar helpers ──────────────────────────────────────────────

    def _refresh_sidebar_devices(self) -> None:
        """Refresh the sidebar device list from DeviceManager."""
        current_port = None
        current_item = self._sidebar_device_list.currentItem()
        if current_item:
            current_port = current_item.data(Qt.UserRole)

        self._sidebar_device_list.clear()
        devices = self._dm.list_devices()
        connected_count = 0

        for dev in devices:
            # Unicode status dot: green for connected, gray for disconnected
            if dev.connected:
                prefix = "● "  # green dot (colored via foreground)
                connected_count += 1
            else:
                prefix = "○ "  # open circle for disconnected

            item = QListWidgetItem(f"{prefix}{dev.display_name}")
            item.setData(Qt.UserRole, dev.port)
            if dev.connected:
                item.setForeground(QColor("#39ff14"))
            else:
                item.setForeground(QColor("#8b949e"))
            self._sidebar_device_list.addItem(item)

            if dev.port == current_port:
                self._sidebar_device_list.setCurrentItem(item)

        total = len(devices)
        self._device_count_label.setText(
            f"{connected_count}/{total} device{'s' if total != 1 else ''}"
        )

        # Update connection status indicator
        connected_names = [d.display_name for d in devices if d.connected]
        if connected_names:
            status_text = "Connected to " + ", ".join(connected_names[:2])
            if len(connected_names) > 2:
                status_text += f" +{len(connected_names) - 2} more"
            dot_color = "#39ff14"
        else:
            status_text = "No device connected"
            dot_color = "#f85149"
        self._conn_status_label.setText(f'<span style="color:{dot_color};">&#9679;</span> {status_text}')
        self._conn_status_label.setStyleSheet(
            "font-size: 8pt; padding: 4px 8px; background: transparent; color: #8b949e;"
        )

        # Also refresh persistent terminal device checklist
        if hasattr(self, '_pterm_device_list'):
            self._pterm_refresh_ports()

    def _on_sidebar_device_selected(self, current: QListWidgetItem | None, _prev: QListWidgetItem | None) -> None:
        if current is None:
            return
        port = current.data(Qt.UserRole)
        if port:
            self.device_selected.emit(port)

    def _on_sidebar_scan(self) -> None:
        """Scan ports and refresh the sidebar."""
        for dev in self._dm.scan_ports():
            if not self._dm.get_device(dev.port):
                self._dm.add_device(dev)
        self._refresh_sidebar_devices()

    # ── Status bar ───────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        self._status_label = QLabel()
        self.statusBar().addPermanentWidget(self._status_label)
        self._refresh_status()

    def _refresh_status(self) -> None:
        n = len(self._dm.list_connected())
        total = len(self._dm.list_devices())
        targets = self._pool.count

        # System health summary
        health = self._health.latest_system_health
        cpu = health.get("cpu_percent", 0)
        mem = health.get("memory_percent", 0)

        self._status_label.setText(
            f"  CPU: {cpu:.0f}%  |  RAM: {mem:.0f}%  "
            f"|  Devices: {n}/{total}  |  Targets: {targets}  "
        )

    # ── Command palette ─────────────────────────────────────────────

    def _build_command_palette(self) -> None:
        """Register all commands in the palette widget."""
        self._palette = CommandPalette(self)
        self._palette.add_command("Flash Firmware", lambda: self._tabs.setCurrentIndex(0))
        self._palette.add_command("Connect to Device", lambda: self._tabs.setCurrentIndex(1))
        self._palette.add_command("View Health", lambda: self._tabs.setCurrentIndex(2))
        self._palette.add_command("Record Macro", self._on_quick_start_macro)
        self._palette.add_command("View Targets", lambda: self._tabs.setCurrentIndex(4))
        self._palette.add_command("Cross-Comm Dashboard", lambda: self._tabs.setCurrentIndex(5))
        self._palette.add_command("Open Settings", lambda: self._tabs.setCurrentIndex(7))
        self._palette.add_command("Dead Man's Switch Setup", self._on_suicide_setup)
        self._palette.add_command("Scan Ports", self._on_sidebar_scan)
        self._palette.add_command("Clear Terminal", self._on_clear_terminal)
        self._palette.add_command("Toggle Dead Man's Switch", self._on_toggle_suicide_mode)
        self._palette.add_command("User Guide", self._on_user_guide)
        self._palette.add_command("Keyboard Shortcuts", self._on_keyboard_shortcuts)
        self._palette.add_command("Quit", self.close)

    def _on_command_palette(self) -> None:
        """Open the command palette dialog."""
        self._palette.open_palette()

    def _on_clear_terminal(self) -> None:
        """Clear the device tab terminal output."""
        if hasattr(self._device_tab, '_terminal'):
            self._device_tab._terminal.clear()

    def _on_toggle_suicide_mode(self) -> None:
        """Toggle the Dead Man's Switch checkbox in the flash tab."""
        self._flash_tab.suicide_enabled = not self._flash_tab.suicide_enabled

    # ── Quick-action sidebar buttons ─────────────────────────────────

    def _on_quick_send_command(self) -> None:
        """Open a quick input dialog to send a command to the active device."""
        cmd, ok = QInputDialog.getText(
            self, "Send Command", "Enter command to send:",
        )
        if ok and cmd.strip():
            # Try to write to the active connection in the device tab
            if hasattr(self._device_tab, '_active_conn') and self._device_tab._active_conn:
                try:
                    self._device_tab._active_conn.write(cmd.strip())
                    if hasattr(self._device_tab, '_terminal'):
                        self._device_tab._terminal.append(f"> {cmd.strip()}")
                except Exception as exc:
                    QMessageBox.warning(self, "Send Error", f"Failed to send command:\n{exc}")
            else:
                QMessageBox.information(
                    self, "No Connection",
                    "No active device connection. Connect to a device in the Devices tab first.",
                )

    def _on_quick_start_macro(self) -> None:
        """Switch to the Macros tab and start recording."""
        self._tabs.setCurrentIndex(3)  # Macros tab
        if hasattr(self._macro_tab, '_on_record'):
            self._macro_tab._on_record()

    # ── Help dialogs ─────────────────────────────────────────────────

    def _on_user_guide(self) -> None:
        """Open the User Guide dialog with feature documentation tabs."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Cyber Controller User Guide")
        dlg.setMinimumSize(800, 600)
        dlg.setStyleSheet(
            "QDialog { background-color: #0d1117; color: #e6edf3; }"
            "QTabWidget { background-color: #0d1117; }"
            "QTabWidget::pane { background-color: #0d1117; border: 1px solid #30363d; }"
            "QTabBar::tab { background: transparent; color: #8b949e; padding: 8px 14px; "
            "border-bottom: 2px solid transparent; }"
            "QTabBar::tab:selected { color: #39ff14; border-bottom: 2px solid #39ff14; }"
            "QTextEdit { background-color: #161b22; color: #e6edf3; border: 1px solid #30363d; "
            "border-radius: 4px; padding: 12px; font-size: 10pt; }"
        )

        layout = QVBoxLayout(dlg)
        tabs = QTabWidget()

        guide_content = {
            "Flash": (
                "<h2 style='color:#39ff14;'>Flash Firmware</h2>"
                "<p>The Flash tab lets you write firmware to connected ESP32 and similar devices.</p>"
                "<h3 style='color:#39ff14;'>Getting Started</h3>"
                "<ul>"
                "<li><b>Select Port</b> &mdash; Pick the serial port your device is connected to. "
                "Click <b>Refresh</b> to re-scan if it does not appear.</li>"
                "<li><b>Choose Firmware Profile</b> &mdash; Select a built-in profile (Marauder, GhostESP, "
                "Bruce, etc.) or click <b>Browse</b> to load a custom JSON profile.</li>"
                "<li><b>Board / Variant</b> &mdash; If your board has a display or a non-standard chip, "
                "pick the matching variant. 'Auto' uses the firmware default.</li>"
                "<li><b>Flash</b> &mdash; Click to begin. Progress is shown in the bar below.</li>"
                "</ul>"
                "<h3 style='color:#39ff14;'>Advanced Features</h3>"
                "<ul>"
                "<li><b>Backup</b> &mdash; Saves the current flash contents to a .bin file before "
                "overwriting.</li>"
                "<li><b>Erase Flash</b> &mdash; Wipes the entire flash memory (useful before a clean "
                "install).</li>"
                "<li><b>Batch Queue</b> &mdash; Queue multiple port+profile combos and flash them "
                "sequentially.</li>"
                "<li><b>Firmware Vault</b> &mdash; Download firmware binaries for offline use. "
                "Clear the cache when you need disk space.</li>"
                "</ul>"
            ),
            "Device Control": (
                "<h2 style='color:#39ff14;'>Device Control</h2>"
                "<p>The Devices tab provides a serial terminal for real-time device communication.</p>"
                "<h3 style='color:#39ff14;'>Connecting</h3>"
                "<ul>"
                "<li>Select a device from the list on the left.</li>"
                "<li>Click <b>Connect</b> to open a serial connection.</li>"
                "<li>The terminal on the right shows all serial output from the device.</li>"
                "</ul>"
                "<h3 style='color:#39ff14;'>Sending Commands</h3>"
                "<ul>"
                "<li><b>Command Palette</b> &mdash; The dropdown lists all known commands for supported "
                "protocols (Marauder, GhostESP). Select one to auto-fill the input.</li>"
                "<li><b>Manual Input</b> &mdash; Type any command in the text field and press Enter or "
                "click Send.</li>"
                "<li><b>Disconnect</b> when done to free the serial port.</li>"
                "</ul>"
            ),
            "Health Monitor": (
                "<h2 style='color:#39ff14;'>Health Monitor</h2>"
                "<p>The Health tab displays real-time metrics for your system and connected devices.</p>"
                "<h3 style='color:#39ff14;'>System Health</h3>"
                "<ul>"
                "<li><b>CPU %</b> &mdash; Current processor utilization.</li>"
                "<li><b>RAM %</b> &mdash; Memory usage percentage.</li>"
                "<li><b>Disk %</b> &mdash; Storage utilization.</li>"
                "</ul>"
                "<h3 style='color:#39ff14;'>Thresholds</h3>"
                "<ul>"
                "<li><b>Green</b> (0-59%) &mdash; Normal operation.</li>"
                "<li><b>Yellow</b> (60-79%) &mdash; Elevated, monitor closely.</li>"
                "<li><b>Orange</b> (80-89%) &mdash; Warning, consider closing other apps.</li>"
                "<li><b>Red</b> (90-100%) &mdash; Critical, may affect flash reliability.</li>"
                "</ul>"
                "<p>Device health (when supported) shows per-device temperature, signal strength, "
                "and uptime.</p>"
            ),
            "Targets": (
                "<h2 style='color:#39ff14;'>Targets</h2>"
                "<p>The Targets tab shows discovered Wi-Fi access points and clients from scanning "
                "devices.</p>"
                "<h3 style='color:#39ff14;'>Understanding Targets</h3>"
                "<ul>"
                "<li><b>RSSI</b> &mdash; Received Signal Strength Indicator. Higher (less negative) "
                "values mean stronger signal. Typical: -30 dBm (excellent) to -90 dBm (weak).</li>"
                "<li><b>BSSID</b> &mdash; The MAC address of the access point.</li>"
                "<li><b>SSID</b> &mdash; The network name (may be hidden).</li>"
                "<li><b>Channel</b> &mdash; The Wi-Fi channel the AP operates on.</li>"
                "</ul>"
                "<h3 style='color:#39ff14;'>Filtering</h3>"
                "<ul>"
                "<li>Use the search box (Ctrl+F) to filter targets by SSID, BSSID, or channel.</li>"
                "<li>Click column headers to sort.</li>"
                "<li>Targets are shared across all connected devices via the TargetPool.</li>"
                "</ul>"
            ),
            "Cross-Comm": (
                "<h2 style='color:#39ff14;'>Cross-Comm</h2>"
                "<p>Cross-device communication lets multiple connected devices work together "
                "automatically.</p>"
                "<h3 style='color:#39ff14;'>Architecture</h3>"
                "<ul>"
                "<li><b>EventBus</b> &mdash; A publish/subscribe message bus. Devices, tabs, and "
                "the auto-router all communicate through events.</li>"
                "<li><b>TargetPool</b> &mdash; A shared, de-duplicated collection of all discovered "
                "targets. Multiple devices feed into the same pool.</li>"
                "<li><b>AutoRouter</b> &mdash; Rule-based routing engine. When a target appears on "
                "device A, AutoRouter can automatically send a command to device B.</li>"
                "</ul>"
                "<h3 style='color:#39ff14;'>Ingest Loop</h3>"
                "<p>The TargetIngestor continuously parses serial output from each connected device, "
                "extracting APs and clients. These are added to the TargetPool, triggering "
                "<code>target.added</code> events on the EventBus, which the AutoRouter picks up "
                "and applies routing rules to.</p>"
            ),
            "Macros": (
                "<h2 style='color:#39ff14;'>Macros</h2>"
                "<p>Record, edit, and replay serial command sequences for automation.</p>"
                "<h3 style='color:#39ff14;'>Recording</h3>"
                "<ul>"
                "<li>Select a port and click <b>Record</b>.</li>"
                "<li>Send commands manually &mdash; each one is captured as a macro step.</li>"
                "<li>Click <b>Stop</b> when done.</li>"
                "<li>Click <b>Save</b> to persist the macro as a JSON file.</li>"
                "</ul>"
                "<h3 style='color:#39ff14;'>Variables</h3>"
                "<ul>"
                "<li><b>TARGET_MAC</b> &mdash; Substituted into commands containing "
                "<code>${TARGET_MAC}</code>.</li>"
                "<li><b>TARGET_SSID</b> &mdash; Substituted for <code>${TARGET_SSID}</code>.</li>"
                "<li><b>CHANNEL</b> &mdash; Substituted for <code>${CHANNEL}</code>.</li>"
                "</ul>"
                "<h3 style='color:#39ff14;'>Playback</h3>"
                "<ul>"
                "<li>Load a macro, set variables, pick a port, and click <b>Play</b>.</li>"
                "<li>Speed multiplier adjusts delay between steps (0.25x to 10x).</li>"
                "</ul>"
            ),
            "Dead Man's Switch": (
                "<h2 style='color:#f0883e;'>Dead Man's Switch</h2>"
                "<p><b>Owner-only defensive anti-forensic mechanism</b> for hardware you own.</p>"
                "<h3 style='color:#f0883e;'>What It Does</h3>"
                "<p>When enabled, the board implements a Dead Man's Switch (DMS). If the correct "
                "boot password is not entered within the configured number of attempts, the board "
                "wipes all flash memory and (optionally) bricks the boot chain, leaving no "
                "recoverable data.</p>"
                "<h3 style='color:#f0883e;'>Dead-Man Gate</h3>"
                "<ul>"
                "<li>An arming GPIO pin determines whether the DMS is active.</li>"
                "<li>When armed, the boot password must be entered via serial within the configured "
                "attempt limit.</li>"
                "<li>If attempts are exhausted, all memory regions are wiped and overwritten.</li>"
                "</ul>"
                "<h3 style='color:#f0883e;'>Password Setup</h3>"
                "<ul>"
                "<li>The boot password is hashed <b>host-side</b> using PBKDF2-HMAC-SHA256.</li>"
                "<li>Only the hash, salt, and parameters are sent to the device.</li>"
                "<li>The plaintext is never stored, logged, or transmitted.</li>"
                "</ul>"
                "<h3 style='color:#f0883e;'>Duress Mode</h3>"
                "<ul>"
                "<li>A separate duress password can trigger immediate wipe when entered.</li>"
                "<li>Useful if compelled to unlock &mdash; entering the duress code destroys data "
                "while appearing to comply.</li>"
                "</ul>"
                "<h3 style='color:#f0883e;'>T2 Brick Mode</h3>"
                "<p>If enabled, the wipe also corrupts the bootloader, making the board permanently "
                "non-reflashable. Use with extreme caution.</p>"
            ),
            "Settings": (
                "<h2 style='color:#39ff14;'>Settings</h2>"
                "<p>The Settings tab controls application-level preferences.</p>"
                "<h3 style='color:#39ff14;'>Available Settings</h3>"
                "<ul>"
                "<li><b>Serial baud rate</b> &mdash; Default baud rate for new connections "
                "(115200 typical for ESP32).</li>"
                "<li><b>Auto-reconnect</b> &mdash; Whether to automatically reconnect when a "
                "device is detected after disconnection.</li>"
                "<li><b>Theme</b> &mdash; Visual theme selection (currently cyber-dark).</li>"
                "<li><b>Macro directory</b> &mdash; Where macro JSON files are saved.</li>"
                "<li><b>Firmware vault path</b> &mdash; Location of the offline firmware cache.</li>"
                "<li><b>Health polling interval</b> &mdash; How often system metrics are sampled.</li>"
                "<li><b>Cross-comm auto-routing</b> &mdash; Enable/disable automatic command routing "
                "between devices.</li>"
                "</ul>"
                "<p>Settings are persisted across sessions.</p>"
            ),
        }

        for tab_name, html in guide_content.items():
            text_edit = QTextEdit()
            text_edit.setReadOnly(True)
            text_edit.setHtml(html)
            tabs.addTab(text_edit, tab_name)

        layout.addWidget(tabs)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)

        dlg.exec_()

    def _on_keyboard_shortcuts(self) -> None:
        """Show a dialog with all keyboard shortcuts."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Keyboard Shortcuts")
        dlg.setMinimumSize(500, 420)
        dlg.setStyleSheet(
            "QDialog { background-color: #0d1117; color: #e6edf3; }"
            "QTableWidget { background-color: #161b22; color: #e6edf3; "
            "border: 1px solid #30363d; border-radius: 4px; gridline-color: #30363d; "
            "alternate-background-color: #1c2128; }"
            "QTableWidget::item { padding: 6px 12px; }"
            "QHeaderView::section { background-color: #0d1117; color: #8b949e; "
            "border: none; border-bottom: 2px solid #39ff14; padding: 6px 8px; "
            "font-weight: 600; }"
        )

        layout = QVBoxLayout(dlg)

        title = QLabel("Keyboard Shortcuts")
        title.setStyleSheet(
            "font-size: 14pt; font-weight: bold; color: #39ff14; padding: 8px; "
            "background: transparent;"
        )
        layout.addWidget(title)

        shortcuts = [
            ("Ctrl+Q", "Quit"),
            ("Ctrl+N", "New Session"),
            ("Ctrl+O", "Open Session"),
            ("Ctrl+S", "Save Session"),
            ("Ctrl+= / Ctrl+-", "Font Size Up / Down"),
            ("Ctrl+F", "Search (in targets)"),
            ("F5", "Refresh Devices / Scan Ports"),
            ("Ctrl+Shift+S", "Dead Man's Switch Setup"),
            ("Ctrl+Shift+P", "Command Palette"),
        ]

        table = QTableWidget(len(shortcuts), 2)
        table.setHorizontalHeaderLabels(["Shortcut", "Action"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        table.horizontalHeader().resizeSection(0, 180)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)

        for row, (key, action) in enumerate(shortcuts):
            key_item = QTableWidgetItem(key)
            key_item.setFont(QFont("JetBrains Mono", 10))
            key_item.setForeground(QColor("#39ff14"))
            table.setItem(row, 0, key_item)
            table.setItem(row, 1, QTableWidgetItem(action))

        layout.addWidget(table)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)

        dlg.exec_()

    # ── Slots ────────────────────────────────────────────────────────

    def _on_new_session(self) -> None:
        log.info("New session requested")

    def _on_open_session(self) -> None:
        log.info("Open session requested")

    def _on_save_session(self) -> None:
        log.info("Save session requested")

    def _change_font_size(self, delta: int) -> None:
        font = QApplication.font()
        new_size = max(7, font.pointSize() + delta)
        font.setPointSize(new_size)
        QApplication.setFont(font)

    def _on_about(self) -> None:
        QMessageBox.about(
            self,
            "About Cyber Controller",
            f"<h2>Cyber Controller v{_VERSION}</h2>"
            "<p>Flagship cyberdeck-oriented all-in-one security hardware controller.</p>"
            f'<p><a href="{_GITHUB_URL}">GitHub</a></p>'
            "<p>MIT License &mdash; LxveAce 2026</p>",
        )

    def _on_github(self) -> None:
        import webbrowser
        webbrowser.open(_GITHUB_URL)

    def _on_suicide_setup(self) -> None:
        """Open the Dead Man's Switch host-side password & duress setup dialog."""
        try:
            from src.ui.qt.suicide_dialog import SuicideSetupDialog
        except Exception as exc:  # noqa: BLE001 — missing submodule / import error
            QMessageBox.critical(
                self,
                "Dead Man's Switch Setup",
                f"Could not open the setup dialog: {exc}\n\n"
                "Ensure the deadmans-switch submodule is initialised:\n"
                "  git submodule update --init deadmans-switch",
            )
            return
        SuicideSetupDialog(self).exec_()

    # ── Cross-comm send ──────────────────────────────────────────────

    def _send_to_port(self, port: str, command: str) -> None:
        """AutoRouter callback — write a routed command to a connected device."""
        conn = self._dm.get_connection(port)
        if conn and conn.is_connected:
            try:
                conn.write(command)  # rejects embedded control chars
            except Exception:
                log.exception("AutoRouter send to %s failed", port)
        else:
            log.warning("AutoRouter: no active connection on %s for routed command", port)

    # ── Cleanup ──────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._timer.stop()
        self._sidebar_timer.stop()
        # Save splitter state
        self._qsettings.setValue("main_splitter_state", self._main_splitter.saveState())
        # Disconnect all persistent terminal connections
        for port in list(self._pterm_conns.keys()):
            try:
                self._dm.close_connection(port)
            except Exception:
                pass
        self._pterm_conns.clear()
        self._health.stop()
        self._dm.shutdown()
        log.info("Window closed — resources released")
        event.accept()


def launch_qt(
    device_manager: DeviceManager,
    flash_engine: FlashEngine,
    event_bus: EventBus,
    target_pool: TargetPool,
    firmware_vault: FirmwareVault | None = None,
    health_monitor: HealthMonitor | None = None,
    macro_recorder: MacroRecorder | None = None,
) -> int:
    """Create the QApplication, show the main window, and run the event loop.

    Returns:
        QApplication exit code.
    """
    app = QApplication(sys.argv)
    app.setApplicationName("Cyber Controller")
    app.setOrganizationName("LxveAce")
    app.setWindowIcon(create_cc_icon())
    apply_theme(app)

    win = CyberControllerWindow(
        device_manager, flash_engine, event_bus, target_pool,
        firmware_vault, health_monitor, macro_recorder,
    )
    win.show()
    return app.exec_()
