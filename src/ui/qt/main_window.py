"""PyQt5 main window — tabbed interface for Cyber Controller."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPalette
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
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

        # Start health monitor polling
        self._health.start()

        self.setWindowTitle(f"Cyber Controller v{_VERSION}")
        self.setMinimumSize(1100, 700)

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

        act_suicide = QAction("&Suicide Marauder Setup…", self)
        act_suicide.setStatusTip("Provision the Suicide-Marauder boot password & duress config (host-side).")
        act_suicide.triggered.connect(self._on_suicide_setup)
        tools_menu.addAction(act_suicide)

        # Help
        help_menu = mb.addMenu("&Help")

        act_about = QAction("&About", self)
        act_about.triggered.connect(self._on_about)
        help_menu.addAction(act_about)

        act_github = QAction("&GitHub", self)
        act_github.triggered.connect(self._on_github)
        help_menu.addAction(act_github)

    # ── Main layout with sidebar + tabs ──────────────────────────────

    def _build_main_layout(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Sidebar ──────────────────────────────────────────────────
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(200)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)

        # Title
        title_label = QLabel("CYBER CONTROLLER")
        title_label.setObjectName("sidebar_title")
        title_label.setAlignment(Qt.AlignCenter)
        sidebar_layout.addWidget(title_label)

        # Separator
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: #30363d;")
        sidebar_layout.addWidget(sep)

        # Device count
        self._device_count_label = QLabel("0 devices")
        self._device_count_label.setObjectName("device_count")
        sidebar_layout.addWidget(self._device_count_label)

        # Device list
        self._sidebar_device_list = QListWidget()
        self._sidebar_device_list.currentItemChanged.connect(self._on_sidebar_device_selected)
        sidebar_layout.addWidget(self._sidebar_device_list)

        # Scan ports button
        scan_btn = QPushButton("Scan Ports")
        scan_btn.clicked.connect(self._on_sidebar_scan)
        sidebar_layout.addWidget(scan_btn)

        main_layout.addWidget(sidebar)

        # ── Tab widget (right side) ──────────────────────────────────
        self._tabs = QTabWidget()
        main_layout.addWidget(self._tabs)

        self._build_tabs()
        self._refresh_sidebar_devices()

    # ── Tabs ─────────────────────────────────────────────────────────

    def _build_tabs(self) -> None:
        # Flash tab (functional, with vault integration)
        self._flash_tab = FlashTab(self._dm, self._fe, self._vault)
        self._tabs.addTab(self._flash_tab, "Flash")

        # Device tab (functional)
        self._device_tab = DeviceTab(self._dm, self._pool, self._ingestor)
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
        """Open the Suicide-Marauder host-side password & duress setup dialog."""
        try:
            from src.ui.qt.suicide_dialog import SuicideSetupDialog
        except Exception as exc:  # noqa: BLE001 — missing submodule / import error
            QMessageBox.critical(
                self,
                "Suicide Setup",
                f"Could not open the setup dialog: {exc}\n\n"
                "Ensure the suicide-marauder submodule is initialised:\n"
                "  git submodule update --init suicide-marauder",
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
    apply_theme(app)

    win = CyberControllerWindow(
        device_manager, flash_engine, event_bus, target_pool,
        firmware_vault, health_monitor, macro_recorder,
    )
    win.show()
    return app.exec_()
