"""PyQt5 main window — tabbed interface for Cyber Controller."""

from __future__ import annotations

import logging
import sys

from PyQt5.QtCore import QSettings, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QFont, QKeySequence
from PyQt5.QtWidgets import (
    QAction,
    QActionGroup,
    QApplication,
    QCheckBox,
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
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.core.cross_comm import EventBus, TargetPool
from src.core.cross_comm_hub import CrossCommHub
from src.core.deadman_auth import DeadManAuth
from src.core.device_manager import DeviceManager
from src.core.firmware_vault import FirmwareVault
from src.core.flash_engine import FlashEngine
from src.core.health_monitor import HealthMonitor
from src.core.macro_recorder import MacroRecorder
from src.ui.qt.cross_comm_tab import CrossCommTab
from src.ui.qt.detachable_tabs import DetachableTabWidget
from src.ui.qt.device_tab import DeviceTab
from src.ui.qt.flash_tab import FlashTab
from src.ui.qt.flock_heatmap_tab import FlockHeatmapTab
from src.ui.qt.nodes_tab import NodesTab
from src.ui.qt.health_tab import HealthTab
from src.ui.qt.macro_tab import MacroTab
from src.ui.qt.screen import (
    adaptive_launch_size,
    adaptive_minimum_size,
    enable_high_dpi,
    recommended_ui_mode,
)
from src.ui.qt.settings_tab import SettingsTab
from src.ui.qt.software_tab import SoftwareTab
from src.ui.qt.targets_tab import TargetsTab
from src.ui.qt.theme import apply_theme
from src.ui.qt.wardrive_tab import WardriveTab
from src.ui.qt.widgets.cc_icon import create_cc_icon
from src.ui.qt.widgets.cc_logo import CCLogo
from src.ui.qt.widgets.command_palette import CommandPalette

log = logging.getLogger(__name__)

from src.version import __version__ as _VERSION

_GITHUB_URL = "https://github.com/LxveAce/cyber-controller"


class _UpdateCheckWorker(QThread):
    """Run the in-app update check off the UI thread and emit the result object.

    Never blocks or slows launch — it hits the network on its own thread with a hard timeout, and any
    failure is folded into an OFFLINE result so the check can never crash the app.
    """

    done = pyqtSignal(object)  # updater.CheckResult

    def __init__(self, installed: str, updates_state: dict) -> None:
        super().__init__()
        self._installed = installed
        self._updates = updates_state

    def run(self) -> None:
        from src.core import updater
        try:
            result = updater.check(self._installed, self._updates)
        except Exception:  # noqa: BLE001 — the check must never crash the app
            result = updater.CheckResult(status=updater.OFFLINE)
        self.done.emit(result)


class _SelfUpdateWorker(QThread):
    """Download + verify + stage the new release binary off the UI thread. Emits the staged path on
    success or an error string; the swap/relaunch (:func:`self_update.apply`) is left to the UI
    thread so the re-exec happens on the main thread, not a worker."""

    progress = pyqtSignal(int, int)  # bytes_done, bytes_total (total 0 == unknown)
    ok = pyqtSignal(str)             # staged path
    fail = pyqtSignal(str)           # error message

    def __init__(self, result) -> None:
        super().__init__()
        self._result = result

    def run(self) -> None:
        from src.core import self_update
        try:
            staged = self_update.self_update(
                self._result, progress=lambda d, t: self.progress.emit(d, t), restart=False)
            self.ok.emit(staged)
        except Exception as exc:  # noqa: BLE001 — any failure is surfaced, never crashes the app
            self.fail.emit(str(exc))


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
        # The cross-comm layer is now assembled in one place — the CrossCommHub spine (src/core/
        # cross_comm_hub.py) — rather than hand-wired here. The window is a thin consumer: it holds the
        # hub and aliases each part so the rest of the UI keeps its familiar self._router/_ingestor/... refs.
        # (Router feeds off target.added and dispatches via hub.send_to_port; ingestor feeds the shared pool,
        # so a scan on device A -> target.added -> a command on device B. DeviceTab attaches the ingestor
        # per-connection. Broadcast fans one verb out to every connected device. ActionResolver is optional.)
        self._hub = CrossCommHub(self._dm, self._bus, self._pool)
        self._router = self._hub.router
        self._ingestor = self._hub.ingestor
        self._broadcast = self._hub.broadcast
        self._action_resolver = self._hub.action_resolver

        # Dead Man's Switch auth flow
        self._dms_auth = DeadManAuth()
        self._dms_auth.set_auth_handler(self._dms_password_prompt)
        self._dms_auth.set_result_handler(self._dms_auth_result)

        # Start health monitor polling
        self._health.start()

        self.setWindowTitle(f"Cyber Controller v{_VERSION}")
        # Cyberdeck-aware sizing: the desktop ideal is a 900x600 floor / 1280x800 launch, but a small
        # deck panel (800x480, 1024x600) can't hold a 900-wide window — clamp both to the actual screen.
        _screen = QApplication.primaryScreen()
        if _screen is not None:
            _ag = _screen.availableGeometry()
            self.setMinimumSize(*adaptive_minimum_size(_ag.width(), _ag.height()))
            self.resize(*adaptive_launch_size(_ag.width(), _ag.height()))
        else:  # no screen (offscreen/headless) — keep the desktop defaults
            self.setMinimumSize(900, 600)
            self.resize(1280, 800)
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

        view_menu.addSeparator()

        # Interface Mode (dual-depth progressive disclosure) — Simple / Pro radio.
        mode_menu = view_menu.addMenu("&Interface Mode")
        self._mode_group = QActionGroup(self)
        self._mode_group.setExclusive(True)
        self._act_mode_simple = QAction("&Simple (guided, fewer options)", self, checkable=True)
        self._act_mode_pro = QAction("&Pro (full access)", self, checkable=True)
        self._act_mode_simple.triggered.connect(lambda: self.set_ui_mode("simple"))
        self._act_mode_pro.triggered.connect(lambda: self.set_ui_mode("pro"))
        for a in (self._act_mode_simple, self._act_mode_pro):
            self._mode_group.addAction(a)
            mode_menu.addAction(a)

        # Ctrl+M toggles Simple<->Pro, always available even if part of the UI is hidden.
        shortcut_mode = QShortcut(QKeySequence("Ctrl+M"), self)
        shortcut_mode.activated.connect(self._toggle_ui_mode)

        view_menu.addSeparator()
        act_loadout = QAction("&Loadout…", self)
        act_loadout.setStatusTip("Choose which firmwares/hardware you use — hide unused features (or Full Stack).")
        act_loadout.triggered.connect(self.configure_loadout)
        view_menu.addAction(act_loadout)

        # Tools
        tools_menu = mb.addMenu("&Tools")

        act_suicide = QAction("&Dead Man's Switch Setup…", self)
        act_suicide.setStatusTip("Provision the Dead Man's Switch boot password & duress config (host-side).")
        act_suicide.triggered.connect(self._on_suicide_setup)
        tools_menu.addAction(act_suicide)

        dv_menu = tools_menu.addMenu("Device &View (skin)")
        dv_menu.setStatusTip("Open an on-screen reconstruction of a firmware's on-board menu (preview).")
        from src.ui.qt.device_view import SKINS
        for _key, (_title, _factory) in SKINS.items():
            _act = QAction(f"{_title}…", self)
            _act.triggered.connect(lambda _checked=False, k=_key: self._on_device_view(k))
            dv_menu.addAction(_act)

        cr_menu = tools_menu.addMenu("Cardputer &Remote")
        cr_menu.setStatusTip("A Cardputer-shaped Device View + a raw CLI console — two lanes, one guarded send.")
        for _key, (_title, _factory) in SKINS.items():
            _act = QAction(f"{_title}…", self)
            _act.triggered.connect(lambda _checked=False, k=_key: self._on_cardputer_remote(k))
            cr_menu.addAction(_act)

        act_flock_map = QAction("Flock &Heatmap…", self)
        act_flock_map.setStatusTip("Map located ALPR-camera detections from a saved Flock scan (cameras.geojson).")
        act_flock_map.triggered.connect(self._on_flock_heatmap)
        tools_menu.addAction(act_flock_map)

        # Help
        help_menu = mb.addMenu("&Help")

        act_guide = QAction("&User Guide", self)
        act_guide.triggered.connect(self._on_user_guide)
        help_menu.addAction(act_guide)

        act_howto = QAction("&How-To Guide", self)
        act_howto.triggered.connect(self._on_howto)
        help_menu.addAction(act_howto)

        act_shortcuts = QAction("&Keyboard Shortcuts", self)
        act_shortcuts.triggered.connect(self._on_keyboard_shortcuts)
        help_menu.addAction(act_shortcuts)

        act_palette = QAction("Command &Palette", self)
        act_palette.setShortcut("Ctrl+Shift+P")
        act_palette.setStatusTip("Jump to any tab or action by name — press Ctrl+Shift+P anywhere.")
        act_palette.triggered.connect(self._on_command_palette)
        help_menu.addAction(act_palette)

        help_menu.addSeparator()

        act_updates = QAction("Check for &Updates…", self)
        act_updates.triggered.connect(lambda: self.check_for_updates(force=True))
        help_menu.addAction(act_updates)

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

        # Pop the current tab out into its own window (re-dock by closing it).
        shortcut_detach = QShortcut(QKeySequence("Ctrl+Shift+D"), self)
        shortcut_detach.activated.connect(lambda: self._tabs.detach_current())

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
        # Detachable: any tab can pop out into its own resizable window and re-dock seamlessly.
        self._tabs = DetachableTabWidget()
        top_layout.addWidget(self._tabs)

        self._main_splitter.addWidget(top_widget)

        # ── Bottom half: persistent terminal ─────────────────────────
        self._build_persistent_terminal()

        # Splitter proportions: ~65% top, ~35% bottom
        self._main_splitter.setStretchFactor(0, 65)
        self._main_splitter.setStretchFactor(1, 35)

        # Restore saved splitter position if available; otherwise set EXPLICIT launch sizes.
        # setStretchFactor only governs how a RESIZE redistributes space — it does NOT set the initial
        # split, so without setSizes the window launches at the children's sizeHints (misproportioned).
        saved_splitter = self._qsettings.value("main_splitter_state")
        if saved_splitter:
            self._main_splitter.restoreState(saved_splitter)
        else:
            _h = max(self.height(), 600)
            self._main_splitter.setSizes([int(_h * 0.65), int(_h * 0.35)])  # ~65% top / 35% terminal

        self._build_tabs()
        # Apply the saved loadout (hide unused tabs) before choosing the default tab.
        self.apply_loadout(self._load_loadout(), persist=False)
        # Default to the Connect surface / Devices sub-tab (the landing view) — most time is on device control.
        self._show_subtab(self._connect_surface, self._device_tab)
        self._refresh_sidebar_devices()
        self._build_command_palette()

        # Apply the persisted interface mode to every tab now that they exist (no persist write-back).
        self._apply_ui_mode(self._load_ui_mode(), persist=False)

        # Re-open any tabs the user had popped out into their own windows last session.
        try:
            self._tabs.restore_detached(self._qsettings.value("detached_tabs", "") or "")
        except Exception:  # noqa: BLE001 — restoring pop-outs must never block startup
            log.exception("Failed to restore detached tabs")

    # ── Tabs ─────────────────────────────────────────────────────────

    def _build_tabs(self) -> None:
        # Flash surface (S4 GUI regroup) — the flashing hub: Firmware (the FlashTab: ESP32/Flipper/RTL firmware +
        # vault) leads, with Software OS (bootable PC/USB images: Kali/Tails/Arch) alongside it. Both are
        # RE-PARENTED into one inner QTabWidget, never recreated, so every self._flash_tab / self._software_tab
        # reference keeps working. Navigate via _show_subtab(self._flash_surface, <widget>).
        self._flash_tab = FlashTab(self._dm, self._fe, self._vault)
        self._software_tab = SoftwareTab()
        self._flash_surface = QTabWidget()
        self._flash_surface.addTab(self._flash_tab, "Firmware")
        self._flash_surface.addTab(self._software_tab, "Software OS")
        self._tabs.addTab(self._flash_surface, "Flash")

        # Connect surface (S4 GUI regroup) — the landing surface: Devices (device control + serial terminal)
        # leads, with Health (host + device-health gauges) alongside it. Both are RE-PARENTED into one inner
        # QTabWidget here, never recreated, so every self._device_tab / self._health_tab reference (dual-depth
        # fan-out, the serial-mirror path, palette, tests) keeps working. Navigate via _show_subtab().
        self._device_tab = DeviceTab(self._dm, self._pool, self._ingestor)
        self._device_tab._dms_auth = self._dms_auth
        self._health_tab = HealthTab(self._health)
        self._connect_surface = QTabWidget()
        self._connect_surface.addTab(self._device_tab, "Devices")
        self._connect_surface.addTab(self._health_tab, "Health")
        # Wireless nodes (W1.1): manage provisioned per-node keys — gate-locked + key-free.
        self._nodes_tab = NodesTab(self._dm)
        self._connect_surface.addTab(self._nodes_tab, "Nodes")
        self._tabs.addTab(self._connect_surface, "Connect")

        # Operate surface (S4 GUI regroup) — the action surface: discover Targets, fan a verb to every radio
        # (Broadcast), record/replay Macros, and GPS-log (Wardrive). All four are RE-PARENTED into one inner
        # QTabWidget here, never recreated, so every self._targets_tab / _broadcast_bar / _macro_tab /
        # _wardrive_tab reference (dual-depth mode fan-out, macro nav, palette, tests) keeps working. Targets
        # leads. Navigate into a sub-view via _show_subtab(self._operate_surface, <widget>).
        self._macro_tab = MacroTab(self._macro, self._dm)
        self._targets_tab = TargetsTab(
            self._pool,
            self._bus,
            device_manager=self._dm,
            action_resolver=self._action_resolver,
        )
        self._wardrive_tab = WardriveTab(device_manager=self._dm)  # GPS-tagged Wi-Fi capture -> WiGLE CSV (lawful, owner-authorized); routes through the DM so it can't double-open a board
        from src.ui.qt.broadcast_tab import BroadcastBar
        self._broadcast_bar = BroadcastBar(self._broadcast, self._dm, self._bus)
        self._operate_surface = QTabWidget()
        self._operate_surface.addTab(self._targets_tab, "Targets")
        self._operate_surface.addTab(self._broadcast_bar, "Broadcast")
        self._operate_surface.addTab(self._macro_tab, "Macros")
        self._operate_surface.addTab(self._wardrive_tab, "Wardrive")
        from src.ui.qt.wardrive_multi_tab import WardriveMultiTab
        self._wardrive_multi_tab = WardriveMultiTab(device_manager=self._dm)  # F1: concurrent multi-board capture
        self._operate_surface.addTab(self._wardrive_multi_tab, "Multi-Wardrive")
        # FL F5: the located-ALPR-camera map is a real sub-tab now (it was a standalone Tools window with no
        # tab lifecycle). It sits next to Wardrive since both are GPS-tagged field-survey views.
        self._flock_heatmap = FlockHeatmapTab()
        self._operate_surface.addTab(self._flock_heatmap, "Flock Map")
        self._tabs.addTab(self._operate_surface, "Operate")

        # Fill-from-target (Track B UX #3): a target selected in the Targets tab pushes its
        # MAC/SSID/channel into the Macro tab's variable fields, so a discovery in one surface is
        # reusable in another without retyping. Same tab-signal → window-connects pattern as
        # SettingsTab.check_updates_requested — no global, no new transport.
        self._targets_tab.fill_macro_requested.connect(self._on_use_target_as_macro)

        # Network anchor surface (S4 GUI regroup) — the node graph is the centerpiece of the cross-comm
        # model, so it leads; Cross-Comm routing (event stream + auto-routing rules) rides alongside it as a
        # sub-view. Both widgets are the SAME objects the rest of main_window + the tests reference: they are
        # RE-PARENTED into an inner QTabWidget here, never recreated, so every self._cross_comm_tab /
        # self._network_tab use keeps working. Navigate into a sub-view via _show_subtab().
        self._cross_comm_tab = CrossCommTab(self._bus, self._pool, self._router, self._dm)
        from src.ui.qt.network_tab import NetworkTab
        self._network_tab = NetworkTab(self._dm, self._pool, self._action_resolver, self._send_to_port)
        self._network_surface = QTabWidget()
        self._network_surface.addTab(self._network_tab, "Graph")
        self._network_surface.addTab(self._cross_comm_tab, "Cross-Comm")
        self._tabs.addTab(self._network_surface, "Network")

        # (Mission Planner tab removed — was a non-functional "coming soon" placeholder; tracked as a
        # real future feature in command-center/projects/cc-reformed-roadmap.md. Don't ship dead tabs.)

        # Settings (persisted)
        self._settings_tab = SettingsTab()
        # The Settings tab's "Check now" button asks the window to run a manual (forced) update check.
        self._settings_tab.check_updates_requested.connect(lambda: self.check_for_updates(force=True))
        self._tabs.addTab(self._settings_tab, "Settings")

        # How-To lives under the Help menu (see _on_howto), not the tab strip — keeps the top level at the
        # 5 working surfaces (Flash / Connect / Operate / Network / Settings) + Help.

    # ── Interface mode (dual-depth Simple / Pro) ────────────────────

    def _load_ui_mode(self) -> str:
        # Honor an explicit user choice; otherwise auto-pick Simple on a small/deck screen, Pro on desktop.
        explicit: str | None = None
        try:
            from src.config.settings import load_settings
            cfg = load_settings().get("interface", {})
            if "mode" in cfg:
                explicit = str(cfg.get("mode")).lower()
        except Exception:  # noqa: BLE001
            explicit = None
        avail_h = 1000  # assume roomy if we can't read the screen (keeps the old Pro default off-screen)
        try:
            scr = QApplication.primaryScreen()
            if scr is not None:
                avail_h = scr.availableGeometry().height()
        except Exception:  # noqa: BLE001
            pass
        return recommended_ui_mode(avail_h, explicit)

    # ── Loadout (which firmwares/hardware → which tabs are shown) ─────
    def _show_subtab(self, surface, widget) -> None:
        """Focus a sub-view inside a grouped surface: select the surface at top level, then the sub-tab.
        Used for by-widget navigation into the Network surface (Graph / Cross-Comm) after the S4 regroup."""
        if self._tabs.indexOf(surface) >= 0:
            self._tabs.setCurrentWidget(surface)
        surface.setCurrentWidget(widget)

    def _tab_registry(self) -> "list[tuple[str, object]]":
        """Canonical (label, widget) tabs in order — the source of truth for loadout show/hide."""
        return [
            ("Flash", self._flash_surface), ("Connect", self._connect_surface),
            # S4 regroup: Flash (Firmware + Software OS), Connect (Devices + Health), Operate (Targets + Broadcast
            # + Macros + Wardrive) and Network (Graph + Cross-Comm) are each ONE loadout-toggleable surface unit.
            ("Operate", self._operate_surface),
            ("Network", self._network_surface),
            ("Settings", self._settings_tab),
        ]

    def _load_loadout(self) -> dict:
        from src.config import loadout as L
        try:
            from src.config.settings import load_settings
            return L.normalize(load_settings().get("interface", {}).get("loadout"))
        except Exception:  # noqa: BLE001
            return L.default_loadout()

    def apply_loadout(self, lo: dict, *, persist: bool = True) -> None:
        """Show only the tabs the loadout calls for (Full Stack / unconfigured → all). Re-runnable."""
        from src.config import loadout as L
        visible = L.visible_tabs(lo)
        reg = dict(self._tab_registry())
        popouts = getattr(self._tabs, "_popouts", {})
        cur = self._tabs.currentWidget()
        # Remove every registered tab from the bar (widgets are retained as attributes; detached stay out).
        for _label, w in self._tab_registry():
            i = self._tabs.indexOf(w)
            if i >= 0:
                self._tabs.removeTab(i)
        # Add the visible ones back in canonical order (skip any currently popped out into a window).
        for label in visible:
            w = reg.get(label)
            if w is not None and w not in popouts and self._tabs.indexOf(w) < 0:
                self._tabs.addTab(w, label)
        # Restore the selection, or fall back to the Connect surface (a core surface, always present).
        if cur is not None and self._tabs.indexOf(cur) >= 0:
            self._tabs.setCurrentWidget(cur)
        elif self._tabs.indexOf(self._connect_surface) >= 0:
            self._tabs.setCurrentWidget(self._connect_surface)
        self._loadout = L.normalize(lo)
        if persist:
            try:
                from src.config.settings import load_settings, save_settings
                s = load_settings()
                s.setdefault("interface", {})["loadout"] = self._loadout
                save_settings(s)
            except Exception:  # noqa: BLE001
                log.exception("Failed to persist loadout")

    def configure_loadout(self) -> None:
        """Open the loadout picker (View ▸ Loadout / first run) and apply + persist the choice."""
        from src.ui.qt.loadout_dialog import LoadoutDialog
        result = LoadoutDialog.choose(self, getattr(self, "_loadout", None) or self._load_loadout())
        if result is not None:
            self.apply_loadout(result, persist=True)

    @property
    def ui_mode(self) -> str:
        return getattr(self, "_ui_mode", "pro")

    def _toggle_ui_mode(self) -> None:
        self.set_ui_mode("pro" if self.ui_mode == "simple" else "simple")

    def set_ui_mode(self, mode: str, *, persist: bool = True) -> None:
        """Public entry point: switch interface mode, fan out to tabs, update chrome, persist."""
        self._apply_ui_mode("simple" if str(mode).lower() == "simple" else "pro", persist=persist)

    def _apply_ui_mode(self, mode: str, *, persist: bool = True) -> None:
        self._ui_mode = mode
        # Fan out to every tab that opts into dual-depth (others are simply unaffected — safe partial
        # rollout). Each tab hides/shows its advanced widget groups; Pro restores the full UI.
        for tab in (
            getattr(self, "_flash_tab", None), getattr(self, "_device_tab", None),
            getattr(self, "_software_tab", None), getattr(self, "_health_tab", None),
            getattr(self, "_macro_tab", None), getattr(self, "_cross_comm_tab", None),
            getattr(self, "_settings_tab", None), getattr(self, "_wardrive_tab", None),
            getattr(self, "_targets_tab", None), getattr(self, "_broadcast_bar", None),
            getattr(self, "_network_tab", None),
        ):
            fn = getattr(tab, "set_ui_mode", None)
            if callable(fn):
                try:
                    fn(mode)
                except Exception:  # noqa: BLE001 — one tab must never break the toggle
                    log.exception("set_ui_mode failed for %s", type(tab).__name__)
        self._sync_mode_chrome()
        if persist:
            try:
                from src.config.settings import load_settings, save_settings
                s = load_settings()
                s.setdefault("interface", {})["mode"] = mode
                save_settings(s)
            except Exception:  # noqa: BLE001
                log.exception("Failed to persist interface mode")

    def _sync_mode_chrome(self) -> None:
        """Keep the View-menu radio + status badge in sync with the current mode."""
        mode = self.ui_mode
        if hasattr(self, "_act_mode_simple"):
            self._act_mode_simple.setChecked(mode == "simple")
            self._act_mode_pro.setChecked(mode == "pro")
        if hasattr(self, "_mode_badge"):
            label = "Simple" if mode == "simple" else "Pro"
            color = "#f0883e" if mode == "simple" else "#a371f7"
            self._mode_badge.setText(f'  Mode: <span style="color:{color};font-weight:bold;">{label} ▾</span>  ')

    # ── Persistent terminal (bottom dock) ──────────────────────────

    # ── Device colors for multi-device terminal ───────────────────
    _DEVICE_COLORS = ["#3fb950", "#58a6ff", "#f0883e", "#f85149", "#d2a8ff"]

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
            "color: #a371f7; font-size: 9pt; font-weight: bold; "
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
            "color: #a371f7; font-size: 10pt; font-weight: bold; "
            "font-family: 'JetBrains Mono', monospace; background: transparent;"
        )
        terminal_panel.addWidget(term_header)

        # Terminal output
        self._pterm_output = QTextEdit()
        self._pterm_output.setReadOnly(True)
        self._pterm_output.setObjectName("terminal")
        # Bound memory: O(1) auto-trim of oldest lines past the cap (UI-opt #6).
        self._pterm_output.document().setMaximumBlockCount(5000)
        self._pterm_output.setStyleSheet(
            "QTextEdit#terminal { background-color: #0d1117; color: #7ee787; "
            "font-family: 'JetBrains Mono', 'Consolas', monospace; font-size: 9pt; "
            "border: 1px solid #30363d; border-radius: 4px; padding: 6px; }"
        )
        terminal_panel.addWidget(self._pterm_output, stretch=1)

        # Command input row
        input_row = QHBoxLayout()
        input_row.setSpacing(4)

        prompt_label = QLabel(">")
        prompt_label.setStyleSheet(
            "color: #7ee787; font-family: 'JetBrains Mono', monospace; "
            "font-size: 10pt; font-weight: bold; background: transparent;"
        )
        input_row.addWidget(prompt_label)

        self._pterm_input = QLineEdit()
        self._pterm_input.setPlaceholderText("Type command and press Enter (sent to all checked devices)...")
        self._pterm_input.setStyleSheet(
            "QLineEdit { background-color: #161b22; color: #e6edf3; "
            "font-family: 'JetBrains Mono', 'Consolas', monospace; font-size: 9pt; "
            "border: 1px solid #30363d; border-radius: 4px; padding: 6px; }"
            "QLineEdit:focus { border-color: #a371f7; }"
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
        # Maps port -> our on_line callback, so disconnect removes EXACTLY it. A co-owned connection
        # survives close_connection, so a left-behind callback would stack a duplicate on the next
        # reconnect and mirror every line twice — the same leak fixed in the Devices tab.
        self._pterm_line_cbs: dict = {}

        # Bridge serial callbacks to the Qt thread (carries port + line)
        from PyQt5.QtCore import QObject
        from PyQt5.QtCore import pyqtSignal as _sig

        class _PTermLineSignal(QObject):
            line_received = _sig(str, str)  # (port, line)

        self._pterm_line_signal = _PTermLineSignal()
        self._pterm_line_signal.line_received.connect(self._pterm_on_line)

        # Refresh device checklist
        self._pterm_refresh_ports()

    def _pterm_refresh_ports(self) -> None:
        """Refresh the persistent terminal device checklist from the device manager."""
        # Self-heal: drop any stored connection that has died (hot-unplug) or whose device is gone, so the
        # list never renders a dead port as connected (the "@"/color key off _pterm_conns) and a replugged
        # port can be reconnected instead of being silently skipped. Runs on the GUI thread (3s timer).
        for p in list(self._pterm_conns):
            c = self._pterm_conns.get(p)
            if c is None or not getattr(c, "is_connected", False) or self._dm.get_device(p) is None:
                self._pterm_conns.pop(p, None)
                self._pterm_port_colors.pop(p, None)
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
                color = self._pterm_port_colors.get(dev.port, "#3fb950")
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
            existing = self._pterm_conns.get(port)
            if existing is not None and getattr(existing, "is_connected", False):
                continue  # already connected and live
            self._pterm_conns.pop(port, None)  # stale/dead entry -> fall through and reopen
            try:
                conn = self._dm.open_connection(port, owner="pterm")
                self._pterm_conns[port] = conn
                color = self._pterm_assign_color(port)
                # Capture port in closure
                _port = port
                cb = lambda line, p=_port: self._pterm_line_signal.line_received.emit(p, line)
                conn.on_line(cb)
                self._pterm_line_cbs[port] = cb
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
            # Remove our on_line callback before releasing (capture the conn first — after
            # close_connection, get_connection may return None). A co-owned conn stays alive, so this
            # stops a duplicate callback stacking on the next reconnect.
            conn = self._dm.get_connection(port)
            cb = self._pterm_line_cbs.pop(port, None)
            if conn is not None and cb is not None:
                remover = getattr(conn, "remove_line_callback", None)
                if callable(remover):
                    try:
                        remover(cb)
                    except Exception:
                        pass
            try:
                self._dm.close_connection(port, owner="pterm")
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
        color = self._pterm_port_colors.get(port, "#3fb950")
        self._pterm_output.append(
            f'<span style="color:{color};">[{port}]</span> {line}'
        )
        # Also mirror to the device tab terminal if it has the port SELECTED but does NOT itself hold the
        # same shared connection — otherwise the device tab's own on_line callback already appended this
        # line and we'd duplicate it (both panels co-own one SerialConnection on a shared port).
        if (
            hasattr(self._device_tab, '_active_port')
            and self._device_tab._active_port == port
            and hasattr(self._device_tab, '_terminal')
            and getattr(self._device_tab, '_active_conn', None) is not conn
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
                f'<span style="color:#3fb950; font-weight:bold;">'
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
                item.setForeground(QColor("#3fb950"))
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
            dot_color = "#3fb950"
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

        # Clickable Interface-Mode badge (one-click recovery to Pro / quick switch to Simple).
        self._mode_badge = QLabel()
        self._mode_badge.setObjectName("mode_badge")
        self._mode_badge.setCursor(Qt.PointingHandCursor)
        self._mode_badge.setToolTip("Click (or Ctrl+M) to switch between Simple and Pro interface modes")
        self._mode_badge.mousePressEvent = lambda _ev: self._toggle_ui_mode()  # type: ignore[assignment]
        self.statusBar().addPermanentWidget(self._mode_badge)

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
        # Navigate by WIDGET, not a hardcoded index — immune to tab reordering (the old fixed indices
        # had drifted and pointed at the wrong tabs).
        self._palette.add_command("Flash Firmware", lambda: self._show_subtab(self._flash_surface, self._flash_tab))
        self._palette.add_command("Flash Software OS", lambda: self._show_subtab(self._flash_surface, self._software_tab))
        self._palette.add_command("Connect to Device", lambda: self._show_subtab(self._connect_surface, self._device_tab))
        self._palette.add_command("View Health", lambda: self._show_subtab(self._connect_surface, self._health_tab))
        self._palette.add_command("Record Macro", self._on_quick_start_macro)
        # Operate surface sub-views: focus the surface, then the sub-tab (re-parented under _operate_surface).
        self._palette.add_command("View Targets", lambda: self._show_subtab(self._operate_surface, self._targets_tab))
        self._palette.add_command("Broadcast Actions", lambda: self._show_subtab(self._operate_surface, self._broadcast_bar))
        self._palette.add_command("View Macros", lambda: self._show_subtab(self._operate_surface, self._macro_tab))
        self._palette.add_command("Wardrive", lambda: self._show_subtab(self._operate_surface, self._wardrive_tab))
        # Network surface sub-views: focus the surface, then the sub-tab (re-parented under _network_surface).
        self._palette.add_command("Network Graph", lambda: self._show_subtab(self._network_surface, self._network_tab))
        self._palette.add_command("Cross-Comm Dashboard", lambda: self._show_subtab(self._network_surface, self._cross_comm_tab))
        self._palette.add_command("Open Settings", lambda: self._tabs.setCurrentWidget(self._settings_tab))
        self._palette.add_command("Dead Man's Switch Setup", self._on_suicide_setup)
        self._palette.add_command("Scan Ports", self._on_sidebar_scan)
        self._palette.add_command("Clear Terminal", self._on_clear_terminal)
        self._palette.add_command("Toggle Dead Man's Switch", self._on_toggle_suicide_mode)
        self._palette.add_command("User Guide", self._on_user_guide)
        self._palette.add_command("How-To", self._on_howto)
        self._palette.add_command("Keyboard Shortcuts", self._on_keyboard_shortcuts)
        self._palette.add_command("Check for Updates…", lambda: self.check_for_updates(force=True))
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
        self._show_subtab(self._operate_surface, self._macro_tab)  # Macros is a sub-view of the Operate surface
        if hasattr(self._macro_tab, '_on_record'):
            self._macro_tab._on_record()

    def _on_use_target_as_macro(self, target) -> None:
        """Fill the Macro tab's variable fields from a Targets-tab selection, then surface Macros.

        Presentation/wiring only — reuses the shared TargetPool's Target and the existing subtab
        navigation; nothing is sent to any device."""
        ch = getattr(target, "channel", 0)
        self._macro_tab.fill_target_variables(
            mac=getattr(target, "mac", "") or "",
            ssid=getattr(target, "ssid", "") or "",
            channel=str(ch) if ch else "",
        )
        self._show_subtab(self._operate_surface, self._macro_tab)

    # ── Help dialogs ─────────────────────────────────────────────────

    def _on_howto(self) -> None:
        """Open the in-app How-To guide (renders docs/HOWTO.md) in a dialog. Lives under Help rather than a
        top-level tab so the strip stays at the 5 working surfaces (Flash/Connect/Operate/Network/Settings)
        + Help — the same "help content in a dialog" pattern as _on_user_guide."""
        from src.ui.qt.howto_tab import HowToTab
        dlg = QDialog(self)
        dlg.setWindowTitle("Cyber Controller — How-To")
        dlg.setMinimumSize(800, 600)
        dlg.setStyleSheet("QDialog { background-color: #0d1117; color: #e6edf3; }")
        layout = QVBoxLayout(dlg)
        layout.addWidget(HowToTab())
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignRight)
        dlg.exec_()

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
            "QTabBar::tab:selected { color: #a371f7; border-bottom: 2px solid #a371f7; }"
            "QTextEdit { background-color: #161b22; color: #e6edf3; border: 1px solid #30363d; "
            "border-radius: 4px; padding: 12px; font-size: 10pt; }"
        )

        layout = QVBoxLayout(dlg)
        tabs = QTabWidget()

        guide_content = {
            "Flash": (
                "<h2 style='color:#a371f7;'>Flash Firmware</h2>"
                "<p>The Flash tab lets you write firmware to connected ESP32 and similar devices.</p>"
                "<h3 style='color:#a371f7;'>Getting Started</h3>"
                "<ul>"
                "<li><b>Select Port</b> &mdash; Pick the serial port your device is connected to. "
                "Click <b>Refresh</b> to re-scan if it does not appear.</li>"
                "<li><b>Choose Firmware Profile</b> &mdash; Select a built-in profile (Marauder, GhostESP, "
                "Bruce, etc.) or click <b>Browse</b> to load a custom JSON profile.</li>"
                "<li><b>Board / Variant</b> &mdash; If your board has a display or a non-standard chip, "
                "pick the matching variant. 'Auto' uses the firmware default.</li>"
                "<li><b>Flash</b> &mdash; Click to begin. Progress is shown in the bar below.</li>"
                "</ul>"
                "<h3 style='color:#a371f7;'>Advanced Features</h3>"
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
                "<h2 style='color:#a371f7;'>Device Control</h2>"
                "<p>The Devices tab provides a serial terminal for real-time device communication.</p>"
                "<h3 style='color:#a371f7;'>Connecting</h3>"
                "<ul>"
                "<li>Select a device from the list on the left.</li>"
                "<li>Click <b>Connect</b> to open a serial connection.</li>"
                "<li>The terminal on the right shows all serial output from the device.</li>"
                "</ul>"
                "<h3 style='color:#a371f7;'>Sending Commands</h3>"
                "<ul>"
                "<li><b>Command Palette</b> &mdash; The dropdown lists all known commands for supported "
                "protocols (Marauder, GhostESP). Select one to auto-fill the input.</li>"
                "<li><b>Manual Input</b> &mdash; Type any command in the text field and press Enter or "
                "click Send.</li>"
                "<li><b>Disconnect</b> when done to free the serial port.</li>"
                "</ul>"
            ),
            "Health Monitor": (
                "<h2 style='color:#a371f7;'>Health Monitor</h2>"
                "<p>The Health tab displays real-time metrics for your system and connected devices.</p>"
                "<h3 style='color:#a371f7;'>System Health</h3>"
                "<ul>"
                "<li><b>CPU %</b> &mdash; Current processor utilization.</li>"
                "<li><b>RAM %</b> &mdash; Memory usage percentage.</li>"
                "<li><b>Disk %</b> &mdash; Storage utilization.</li>"
                "</ul>"
                "<h3 style='color:#a371f7;'>Thresholds</h3>"
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
                "<h2 style='color:#a371f7;'>Targets</h2>"
                "<p>The Targets tab shows discovered Wi-Fi access points and clients from scanning "
                "devices.</p>"
                "<h3 style='color:#a371f7;'>Understanding Targets</h3>"
                "<ul>"
                "<li><b>RSSI</b> &mdash; Received Signal Strength Indicator. Higher (less negative) "
                "values mean stronger signal. Typical: -30 dBm (excellent) to -90 dBm (weak).</li>"
                "<li><b>BSSID</b> &mdash; The MAC address of the access point.</li>"
                "<li><b>SSID</b> &mdash; The network name (may be hidden).</li>"
                "<li><b>Channel</b> &mdash; The Wi-Fi channel the AP operates on.</li>"
                "</ul>"
                "<h3 style='color:#a371f7;'>Filtering</h3>"
                "<ul>"
                "<li>Use the search box (Ctrl+F) to filter targets by SSID, BSSID, or channel.</li>"
                "<li>Click column headers to sort.</li>"
                "<li>Targets are shared across all connected devices via the TargetPool.</li>"
                "</ul>"
            ),
            "Cross-Comm": (
                "<h2 style='color:#a371f7;'>Cross-Comm</h2>"
                "<p>Cross-device communication lets multiple connected devices work together "
                "automatically.</p>"
                "<h3 style='color:#a371f7;'>Architecture</h3>"
                "<ul>"
                "<li><b>EventBus</b> &mdash; A publish/subscribe message bus. Devices, tabs, and "
                "the auto-router all communicate through events.</li>"
                "<li><b>TargetPool</b> &mdash; A shared, de-duplicated collection of all discovered "
                "targets. Multiple devices feed into the same pool.</li>"
                "<li><b>AutoRouter</b> &mdash; Rule-based routing engine. When a target appears on "
                "device A, AutoRouter can automatically send a command to device B.</li>"
                "</ul>"
                "<h3 style='color:#a371f7;'>Ingest Loop</h3>"
                "<p>The TargetIngestor continuously parses serial output from each connected device, "
                "extracting APs and clients. These are added to the TargetPool, triggering "
                "<code>target.added</code> events on the EventBus, which the AutoRouter picks up "
                "and applies routing rules to.</p>"
            ),
            "Macros": (
                "<h2 style='color:#a371f7;'>Macros</h2>"
                "<p>Record, edit, and replay serial command sequences for automation.</p>"
                "<h3 style='color:#a371f7;'>Recording</h3>"
                "<ul>"
                "<li>Select a port and click <b>Record</b>.</li>"
                "<li>Send commands manually &mdash; each one is captured as a macro step.</li>"
                "<li>Click <b>Stop</b> when done.</li>"
                "<li>Click <b>Save</b> to persist the macro as a JSON file.</li>"
                "</ul>"
                "<h3 style='color:#a371f7;'>Variables</h3>"
                "<ul>"
                "<li><b>TARGET_MAC</b> &mdash; Substituted into commands containing "
                "<code>${TARGET_MAC}</code>.</li>"
                "<li><b>TARGET_SSID</b> &mdash; Substituted for <code>${TARGET_SSID}</code>.</li>"
                "<li><b>CHANNEL</b> &mdash; Substituted for <code>${CHANNEL}</code>.</li>"
                "</ul>"
                "<h3 style='color:#a371f7;'>Playback</h3>"
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
                "<h2 style='color:#a371f7;'>Settings</h2>"
                "<p>The Settings tab controls application-level preferences.</p>"
                "<h3 style='color:#a371f7;'>Available Settings</h3>"
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
            "border: none; border-bottom: 2px solid #a371f7; padding: 6px 8px; "
            "font-weight: 600; }"
        )

        layout = QVBoxLayout(dlg)

        title = QLabel("Keyboard Shortcuts")
        title.setStyleSheet(
            "font-size: 14pt; font-weight: bold; color: #a371f7; padding: 8px; "
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
            key_item.setForeground(QColor("#3fb950"))
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

    # ── In-app updates ───────────────────────────────────────────────
    def check_for_updates(self, force: bool = False) -> None:
        """Kick a NON-BLOCKING update check on a background thread.

        ``force=True`` is a manual "Check for Updates" — it bypasses ``updates.enabled`` and the
        suppression flags so it always reports. The automatic (force=False) check is skipped when the
        feature is disabled, but is NEVER gated by suppression (only the resulting prompt is).
        """
        from src.config.settings import load_settings
        from src.core import updater
        updates = load_settings().get("updates", {})
        if not updater.should_auto_check(updates, force=force):
            return
        worker = getattr(self, "_update_worker", None)
        if worker is not None and worker.isRunning():
            return  # a check is already in flight
        worker = _UpdateCheckWorker(_VERSION, dict(updates))
        worker.done.connect(lambda result, f=force: self._on_update_check_done(result, f))
        worker.finished.connect(lambda: setattr(self, "_update_worker", None))
        self._update_worker = worker  # keep a reference so the thread isn't GC'd
        worker.start()

    def _on_update_check_done(self, result, force: bool) -> None:
        """Apply the update decision flow on the UI thread once the background check returns."""
        from src.config.settings import load_settings, save_settings
        from src.core import updater
        from src.core import self_update
        from src.ui.qt.update_dialog import (
            ACTION_SELF_UPDATE,
            ACTION_UPDATE,
            OfflineErrorDialog,
            UpdateAvailableDialog,
        )
        try:
            settings = load_settings()
            upd = settings.get("updates", {})
            # The silent check ALWAYS ran — record that it happened regardless of any suppression.
            upd["last_check_iso"] = updater.now_iso()
            if result.latest_tag:
                upd["last_seen_latest"] = result.latest_tag

            if result.status == updater.OFFLINE:
                # OFFLINE handling is gated ONLY by offline_error_suppressed (never the version logic).
                if force or not upd.get("offline_error_suppressed", False):
                    dlg = OfflineErrorDialog(self)
                    dlg.exec_()
                    if dlg.dont_show_again():
                        upd["offline_error_suppressed"] = True
            elif result.status == updater.UP_TO_DATE:
                if force:  # a manual check confirms; the automatic one stays silent
                    QMessageBox.information(
                        self, "Check for Updates",
                        f"You're up to date — v{_VERSION} is the latest release.",
                    )
            elif result.status == updater.NEWER:
                # Only the PROMPT is gated. A manual check always prompts.
                if force or updater.should_prompt(upd, result.behind):
                    dlg = UpdateAvailableDialog(
                        result.latest_tag, _VERSION, updater.apply_update_url(result),
                        behind=result.behind, parent=self,
                        can_self_update=self_update.is_frozen(),
                    )
                    dlg.exec_()
                    action = dlg.action()
                    if action == ACTION_SELF_UPDATE:
                        self._begin_self_update(result)
                    elif action != ACTION_UPDATE and dlg.dont_show_again():
                        upd["suppressed"] = True
                        upd["suppressed_at_behind"] = result.behind
                        upd["dismissed_version"] = result.latest_tag

            settings["updates"] = upd
            save_settings(settings)
        except Exception:  # noqa: BLE001 — updater UI must never crash the app
            log.debug("update-check post-processing failed", exc_info=True)

    def _begin_self_update(self, result) -> None:
        """Run the in-place self-update: a modal progress dialog over a background download+verify,
        then swap the binary and restart. Offered only on frozen builds. A failure falls back to the
        release page so the user is never stranded."""
        from PyQt5.QtWidgets import QProgressDialog

        prog = QProgressDialog("Downloading update…", None, 0, 0, self)  # None → no cancel button
        prog.setWindowTitle("Updating")
        prog.setWindowModality(Qt.WindowModal)
        prog.setAutoClose(False)
        prog.setAutoReset(False)
        prog.setMinimumDuration(0)

        worker = _SelfUpdateWorker(result)
        self._self_update_worker = worker  # keep a reference so the thread isn't GC'd

        def on_progress(done: int, total: int) -> None:
            if total > 0:
                prog.setMaximum(total)
                prog.setValue(done)

        def on_ok(staged: str) -> None:
            prog.setLabelText("Verified. Restarting…")
            self._finish_self_update(staged)

        def on_fail(msg: str) -> None:
            from PyQt5.QtCore import QUrl
            from PyQt5.QtGui import QDesktopServices
            from src.core import updater
            prog.close()
            QMessageBox.warning(
                self, "Update failed",
                f"Couldn't install the update automatically:\n{msg}\n\n"
                "Opening the release page so you can download it manually.",
            )
            QDesktopServices.openUrl(QUrl(updater.apply_update_url(result)))

        worker.progress.connect(on_progress)
        worker.ok.connect(on_ok)
        worker.fail.connect(on_fail)
        worker.finished.connect(lambda: setattr(self, "_self_update_worker", None))
        worker.start()

    def _finish_self_update(self, staged: str) -> None:
        """Swap the verified binary in and restart. On Windows this spawns a detached helper and we
        quit so the (now unlocked) exe can be replaced + relaunched; on Unix apply() re-execs and
        never returns."""
        from PyQt5.QtWidgets import QApplication

        from src.core import self_update
        try:
            self_update.apply(self_update.current_exe(), staged, self_update.platform_key())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Update failed", f"Could not apply the update:\n{exc}")
            return
        QApplication.instance().quit()  # reached on Windows only (Unix apply() re-execs, no return)

    def _on_device_view(self, firmware: str = "marauder") -> None:
        """Open a Device View — an on-screen reconstruction of a firmware's on-board UI.

        P2/P3 scope: a faithful, navigable TFT *skin* (model-driven; runs with no hardware) for Marauder
        and GhostESP. Live serial drive + the gate/Dead-Man panel are later phases (see the Device-View
        plan). Honest framing: this is a reconstruction, not a pixel mirror (only Flipper can be a true
        mirror).
        """
        try:
            from src.ui.qt.device_view import SKINS, DeviceScreenModel, DeviceView
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Device View", f"Could not open the Device View: {exc}")
            return
        title, factory = SKINS.get(firmware, SKINS["marauder"])
        skin_id = firmware if firmware in SKINS else "marauder"
        model = DeviceScreenModel(title, factory(), skin=skin_id)   # DV3: selects the per-firmware SkinSpec
        # Keep a reference so the top-level window isn't garbage-collected. Wire it to actually drive the
        # connected device when its firmware matches the skin (else it stays a preview).
        self._device_view = DeviceView(model, send=lambda c, fw=firmware: self._device_view_send(fw, c))
        self._device_view.setWindowTitle(f"Device View — {title} (reconstructed skin · preview)")
        self._device_view.show()
        self._device_view.raise_()
        self._device_view.activateWindow()

    def _on_cardputer_remote(self, firmware: str = "marauder") -> None:
        """Open a Cardputer Remote (CP2) — the same skin shaped to the Cardputer's 240x135 PLUS a raw CLI
        console, both driving the connected device through the identical guarded send as the Device View."""
        try:
            from src.ui.qt.cardputer_remote import CardputerRemote
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Cardputer Remote", f"Could not open the Cardputer Remote: {exc}")
            return
        from src.ui.qt.device_view import SKINS
        title = SKINS.get(firmware, SKINS["marauder"])[0]
        # Same send lambda the Device View uses -> same firmware-match + safety + write validation.
        self._cardputer_remote = CardputerRemote(
            firmware, send=lambda c, fw=firmware: self._device_view_send(fw, c))
        self._cardputer_remote.setWindowTitle(f"Cardputer Remote — {title} (reconstructed skin · preview)")
        self._cardputer_remote.resize(320, 520)
        self._cardputer_remote.show()
        self._cardputer_remote.raise_()
        self._cardputer_remote.activateWindow()

    def _on_flock_heatmap(self) -> None:
        """Focus the Flock Map tab (FL F5) — located ALPR-camera detections from a scan's GeoJSON.

        The map used to open as a standalone Tools window; it's a sub-tab of the Operate surface now, so this
        menu / palette action just navigates to it."""
        self._show_subtab(self._operate_surface, self._flock_heatmap)

    # Device-View skin id -> serial protocol_name (for matching a connected device to the skin).
    _SKIN_PROTOCOL = {"marauder": "marauder", "ghostesp": "ghostesp", "esp32div": "esp32_div",
                      "bruce": "bruce"}

    def _device_view_send(self, firmware: str, cmd: str) -> bool:
        """Send a Device-View command to the active device IFF its selected firmware matches the skin.

        Routes through the same safety classifier as the Devices tab (destructive commands prompt for
        confirmation). Returns True only if the command was actually written — so the skin shows "sent"
        vs "preview" honestly, and one firmware's commands are never sent to a different device.
        """
        dt = getattr(self, "_device_tab", None)
        conn = getattr(dt, "_active_conn", None) if dt is not None else None
        if conn is None:
            return False
        try:
            proto = dt._selected_protocol()
        except Exception:  # noqa: BLE001
            return False
        if getattr(proto, "protocol_name", None) != self._SKIN_PROTOCOL.get(firmware, firmware):
            return False  # don't send a Marauder command to a GhostESP, etc.
        from src.config.settings import load_settings
        from src.core import safety
        info = next((ci for ci in proto.cached_commands() if ci.name == cmd), None)
        danger = safety.classify(cmd, info)
        if safety.should_confirm(danger, load_settings()):
            reply = QMessageBox.warning(
                self, "Confirm dangerous command", safety.lab_only_warning_text(cmd, danger),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return False
        try:
            conn.write(cmd)
            return True
        except Exception:  # noqa: BLE001
            log.exception("Device View send failed")
            return False

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
        """Write a routed command to a connected device. Thin delegate to the cross-comm spine — the logic
        lives once on the hub (src/core/cross_comm_hub.py); kept here for the callers that pass this bound
        method as a send callback (e.g. the Network tab)."""
        self._hub.send_to_port(port, command)

    # ── Cleanup ──────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._timer.stop()
        self._sidebar_timer.stop()
        # Save splitter state
        self._qsettings.setValue("main_splitter_state", self._main_splitter.saveState())
        # Remember which tabs were popped out (+ their window geometry), then re-dock them so no
        # orphan windows linger after the main window closes.
        try:
            self._qsettings.setValue("detached_tabs", self._tabs.detached_state())
            self._tabs.close_all_popouts()
        except Exception:  # noqa: BLE001
            pass
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
    enable_high_dpi()  # must precede QApplication construction; no-op if one already exists
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Cyber Controller")
    app.setOrganizationName("LxveAce")
    app.setWindowIcon(create_cc_icon())
    apply_theme(app)

    # Smart-install downgrade guard: if the existing ~/.cyber-controller was written by a NEWER version
    # than this build, offer to keep it or back it up and start fresh (the older build may not read a
    # newer config/vault format). Nothing is deleted — "start fresh" moves the old config aside.
    try:
        from src.core import install
        if install.classify() == "downgrade":
            box = QMessageBox()
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Existing configuration is from a newer version")
            box.setText(
                f"Your Cyber Controller settings were written by a newer version "
                f"(v{install.installed_version()}) than this one (v{_VERSION}).\n\n"
                "Continuing may behave unexpectedly. Keep them, or back them up and start fresh "
                "(your old config is moved aside, not deleted)."
            )
            keep = box.addButton("Keep && Continue", QMessageBox.AcceptRole)
            box.addButton("Back up && Start Fresh", QMessageBox.DestructiveRole)
            box.setDefaultButton(keep)
            box.exec_()
            if box.clickedButton() is not keep:
                bk = install.backup_config_dir()
                install.record_version()  # the now-fresh dir belongs to this version
                QMessageBox.information(
                    None, "Fresh start",
                    f"Previous configuration backed up to:\n{bk}\n\nStarting fresh with defaults.",
                )
    except Exception:
        log.debug("downgrade prompt skipped", exc_info=True)

    # Animated startup — PyQt5 (the heaviest UI) ONLY. Hand off from the PyInstaller extraction splash
    # to a richer animated loading screen, build the dashboard, then cross-fade to it. The lightweight
    # UIs (Tk/TUI/web) intentionally have no such animation.
    import time as _time

    from src.core.resources import resource_path
    from src.ui.qt.loading_splash import LoadingSplash, fade_in_window, reduced_motion

    _logo = str(resource_path("assets", "cc-logo.png"))
    splash = LoadingSplash(_logo)
    splash.start()
    try:
        import pyi_splash  # type: ignore[import-not-found]
        pyi_splash.close()  # the static extraction splash hands off to the animated one
    except Exception:
        pass

    splash.set_status("Loading firmware profiles…")
    _t0 = _time.monotonic()
    win = CyberControllerWindow(
        device_manager, flash_engine, event_bus, target_pool,
        firmware_vault, health_monitor, macro_recorder,
    )
    splash.set_status("Starting dashboard…")

    def _first_run_dialogs() -> None:
        # One-time legal / authorized-use disclaimer (always seen at least once; LABELS, never blocks).
        from PyQt5.QtWidgets import QMessageBox

        from src.config.settings import load_settings, save_settings
        from src.core import safety
        _settings = load_settings()
        if safety.needs_first_run_disclaimer(_settings):
            box = QMessageBox(win)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Authorized Use Only")
            box.setText(safety.legal_disclaimer_text())
            box.setStandardButtons(QMessageBox.Ok)
            box.button(QMessageBox.Ok).setText("I Understand")
            box.exec_()
            _settings["_disclaimer_ack"] = True
            save_settings(_settings)
        # One-time interface-mode choice (Simple vs Pro). New users are nudged to Simple; Pro stays the
        # stored default so declining changes nothing.
        _settings = load_settings()
        if not _settings.get("_interface_mode_ack", False):
            box = QMessageBox(win)
            box.setIcon(QMessageBox.Question)
            box.setWindowTitle("Choose your interface")
            box.setText(
                "<b>Simple</b> — a guided, streamlined view with fewer options (great to start).<br>"
                "<b>Pro</b> — the full interface with every control.<br><br>"
                "You can switch anytime: <b>View ▸ Interface Mode</b>, the status-bar badge, or <b>Ctrl+M</b>."
            )
            simple_btn = box.addButton("Use Simple", QMessageBox.AcceptRole)
            box.addButton("Use Pro", QMessageBox.RejectRole)
            box.setDefaultButton(simple_btn)
            box.exec_()
            if box.clickedButton() is simple_btn:
                win.set_ui_mode("simple")
            _settings = load_settings()
            _settings["_interface_mode_ack"] = True
            save_settings(_settings)
        # One-time loadout choice — tailor the GUI to the firmwares/hardware in use (or Full Stack).
        if not win._load_loadout().get("configured", False):
            from src.config import loadout as _L
            from src.ui.qt.loadout_dialog import LoadoutDialog
            _result = LoadoutDialog.choose(win, win._load_loadout())
            # On cancel, default to Full Stack so nothing is hidden (and we don't re-ask every launch).
            win.apply_loadout(_result if _result is not None else _L.full_stack_loadout(), persist=True)
        # After the one-time modals, kick a NON-BLOCKING background update check (off-thread, hard
        # timeout — never blocks or slows launch). Automatic path honours updates.enabled; the prompt
        # (if any) is applied on the UI thread when the worker returns.
        try:
            win.check_for_updates(force=False)
        except Exception:
            log.debug("startup update check kick failed", exc_info=True)

    def _reveal() -> None:
        win.show()
        fade_in_window(win)            # OutQuart fade-in of the dashboard
        splash.finish(_first_run_dialogs)  # fade the splash out, then run first-run dialogs

    # Let the loading animation breathe for a pleasant minimum (illustrative motion is fine for a
    # once-per-launch event); skip the delay entirely under reduced motion.
    min_ms = 0 if reduced_motion() else 1000
    elapsed_ms = int((_time.monotonic() - _t0) * 1000)
    QTimer.singleShot(max(0, min_ms - elapsed_ms), _reveal)

    return app.exec_()
