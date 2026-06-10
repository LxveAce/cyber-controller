"""PyQt5 main window — tabbed interface for Cyber Controller."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QPalette
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.core.cross_comm import EventBus, TargetPool
from src.ui.qt.flash_tab import FlashTab
from src.ui.qt.device_tab import DeviceTab

log = logging.getLogger(__name__)

_VERSION = "0.1.0"
_GITHUB_URL = "https://github.com/LxveAce/cyber-controller"


def _apply_dark_palette(app: QApplication) -> None:
    """Apply a dark colour palette to the entire application."""
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(30, 30, 30))
    palette.setColor(QPalette.WindowText, QColor(220, 220, 220))
    palette.setColor(QPalette.Base, QColor(20, 20, 20))
    palette.setColor(QPalette.AlternateBase, QColor(40, 40, 40))
    palette.setColor(QPalette.ToolTipBase, QColor(30, 30, 30))
    palette.setColor(QPalette.ToolTipText, QColor(220, 220, 220))
    palette.setColor(QPalette.Text, QColor(220, 220, 220))
    palette.setColor(QPalette.Button, QColor(45, 45, 45))
    palette.setColor(QPalette.ButtonText, QColor(220, 220, 220))
    palette.setColor(QPalette.BrightText, QColor(255, 50, 50))
    palette.setColor(QPalette.Link, QColor(57, 255, 20))
    palette.setColor(QPalette.Highlight, QColor(57, 255, 20))
    palette.setColor(QPalette.HighlightedText, QColor(0, 0, 0))
    app.setPalette(palette)
    app.setStyleSheet(
        "QToolTip { color: #dcdcdc; background-color: #2b2b2b; border: 1px solid #555; }"
    )


def _placeholder_tab(label_text: str) -> QWidget:
    """Create a simple placeholder tab with a centred label."""
    w = QWidget()
    layout = QVBoxLayout(w)
    lbl = QLabel(label_text)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setFont(QFont("Segoe UI", 14))
    lbl.setStyleSheet("color: #555;")
    layout.addWidget(lbl)
    return w


class CyberControllerWindow(QMainWindow):
    """Main application window with tabbed interface."""

    def __init__(
        self,
        device_manager: DeviceManager,
        flash_engine: FlashEngine,
        event_bus: EventBus,
        target_pool: TargetPool,
    ) -> None:
        super().__init__()
        self._dm = device_manager
        self._fe = flash_engine
        self._bus = event_bus
        self._pool = target_pool

        self.setWindowTitle(f"Cyber Controller v{_VERSION}")
        self.setMinimumSize(1100, 700)

        self._build_menu_bar()
        self._build_tabs()
        self._build_status_bar()

        # Periodic status-bar refresh
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start(2000)

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

        act_theme = QAction("Toggle &Theme", self)
        act_theme.triggered.connect(self._on_toggle_theme)
        view_menu.addAction(act_theme)

        act_font_up = QAction("Font Size &+", self)
        act_font_up.setShortcut("Ctrl+=")
        act_font_up.triggered.connect(lambda: self._change_font_size(1))
        view_menu.addAction(act_font_up)

        act_font_down = QAction("Font Size &-", self)
        act_font_down.setShortcut("Ctrl+-")
        act_font_down.triggered.connect(lambda: self._change_font_size(-1))
        view_menu.addAction(act_font_down)

        # Help
        help_menu = mb.addMenu("&Help")

        act_about = QAction("&About", self)
        act_about.triggered.connect(self._on_about)
        help_menu.addAction(act_about)

        act_github = QAction("&GitHub", self)
        act_github.triggered.connect(self._on_github)
        help_menu.addAction(act_github)

    # ── Tabs ─────────────────────────────────────────────────────────

    def _build_tabs(self) -> None:
        self._tabs = QTabWidget()
        self.setCentralWidget(self._tabs)

        # Flash tab (functional)
        self._flash_tab = FlashTab(self._dm, self._fe)
        self._tabs.addTab(self._flash_tab, "Flash")

        # Device tab (functional)
        self._device_tab = DeviceTab(self._dm)
        self._tabs.addTab(self._device_tab, "Devices")

        # Placeholder tabs
        self._tabs.addTab(_placeholder_tab("Target Pool — coming soon"), "Targets")
        self._tabs.addTab(_placeholder_tab("Cross-Comm Routing — coming soon"), "Cross-Comm")
        self._tabs.addTab(_placeholder_tab("Mission Planner — coming soon"), "Missions")
        self._tabs.addTab(_placeholder_tab("Settings — coming soon"), "Settings")

    # ── Status bar ───────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        self._status_label = QLabel()
        self.statusBar().addPermanentWidget(self._status_label)
        self._refresh_status()

    def _refresh_status(self) -> None:
        n = len(self._dm.list_connected())
        total = len(self._dm.list_devices())
        targets = self._pool.count
        self._status_label.setText(
            f"  Devices: {n}/{total} connected  |  Targets: {targets}  "
        )

    # ── Slots ────────────────────────────────────────────────────────

    def _on_new_session(self) -> None:
        log.info("New session requested")

    def _on_open_session(self) -> None:
        log.info("Open session requested")

    def _on_save_session(self) -> None:
        log.info("Save session requested")

    def _on_toggle_theme(self) -> None:
        # Simple toggle: if the window bg is dark, switch to light
        app = QApplication.instance()
        if app is None:
            return
        current = app.palette().color(QPalette.Window).lightness()
        if current < 128:
            app.setPalette(app.style().standardPalette())
            app.setStyleSheet("")
        else:
            _apply_dark_palette(app)

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

    # ── Cleanup ──────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._timer.stop()
        self._dm.shutdown()
        log.info("Window closed — resources released")
        event.accept()


def launch_qt(
    device_manager: DeviceManager,
    flash_engine: FlashEngine,
    event_bus: EventBus,
    target_pool: TargetPool,
) -> int:
    """Create the QApplication, show the main window, and run the event loop.

    Returns:
        QApplication exit code.
    """
    app = QApplication(sys.argv)
    app.setApplicationName("Cyber Controller")
    app.setOrganizationName("LxveAce")
    app.setFont(QFont("Segoe UI", 10))
    _apply_dark_palette(app)

    win = CyberControllerWindow(device_manager, flash_engine, event_bus, target_pool)
    win.show()
    return app.exec_()
