"""PyQt5 main window — tabbed interface for Cyber Controller."""

from __future__ import annotations

import html
import logging
import sys

from PyQt5.QtCore import QSettings, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QFont, QKeySequence
from PyQt5.QtWidgets import (
    QAction,
    QActionGroup,
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
from src.core.firmware_vault import FirmwareVault, configured_vault_dir
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
from src.ui.qt.icons import label_icon
from src.ui.qt.widgets.cc_icon import create_cc_icon
from src.ui.qt.widgets.command_palette import CommandPalette

log = logging.getLogger(__name__)

from src.version import __version__ as _VERSION

_GITHUB_URL = "https://github.com/LxveAce/cyber-controller"

# Backstop for closeEvent: an update-check QThread still blocked on a slow/black-hole network at exit is
# moved here so its Python wrapper isn't garbage-collected when the window is destroyed. That GC — not the
# running thread itself — is what fires the C++ QThread destructor mid-run and aborts the process
# ('QThread: Destroyed while thread is still running'). Holding a reference lets the thread finish (or the
# process exit) without the abort.
_KEEPALIVE_WORKERS: set = set()


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


class _PortScanWorker(QThread):
    """Enumerate serial ports off the GUI thread. serial.tools.list_ports.comports() does blocking
    SetupAPI/registry I/O that can take seconds on a machine with many virtual/Bluetooth COM ports, so
    running it in the Scan-Ports / F5 slot froze the event loop. Any failure yields an empty list so the
    scan can never crash the app."""

    done = pyqtSignal(object)  # list[Device]

    def __init__(self, device_manager) -> None:
        super().__init__()
        self._dm = device_manager

    def run(self) -> None:
        try:
            devices = list(self._dm.scan_ports())
        except Exception:  # noqa: BLE001 — a scan failure must never crash the UI
            devices = []
        self.done.emit(devices)


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
        # Wave-3 Batch A: last size-class the shell laid out for (debounce, mirrors flash_tab).
        self._last_nav_key: "str | None" = None   # last applied nav-chrome mode (debounce key)
        self._dm = device_manager
        self._fe = flash_engine
        self._bus = event_bus
        self._pool = target_pool
        self._vault = firmware_vault or FirmwareVault(configured_vault_dir())
        self._health = health_monitor or HealthMonitor()
        self._macro = macro_recorder or MacroRecorder()
        # The cross-comm layer is now assembled in one place — the CrossCommHub spine (src/core/
        # cross_comm_hub.py) — rather than hand-wired here. The window is a thin consumer: it holds the
        # hub and aliases each part so the rest of the UI keeps its familiar self._router/_ingestor/... refs.
        # (Router feeds off target.added and dispatches via hub.send_to_port; ingestor feeds the shared pool,
        # so a scan on device A -> target.added -> a command on device B. DeviceTab attaches the ingestor
        # per-connection. Broadcast fans one verb out to every connected device. ActionResolver is optional.)
        # Persist the capture library to the canonical captures dir so a captured handshake / PMKID
        # (and any recovered PSK) survives across app sessions — loaded on next launch, autosaved on
        # every change. Only the real app opts in; headless tests construct the hub without a path.
        from src.core.install import captures_dir
        self._hub = CrossCommHub(self._dm, self._bus, self._pool,
                                 captures_persist_path=str(captures_dir() / "captures.json"))
        self._router = self._hub.router
        self._ingestor = self._hub.ingestor
        self._broadcast = self._hub.broadcast
        self._action_resolver = self._hub.action_resolver

        # Dead Man's Switch auth flow
        self._dms_auth = DeadManAuth()
        self._dms_auth.set_auth_handler(self._dms_password_prompt)
        self._dms_auth.set_result_handler(self._dms_auth_result)

        # Start health monitor polling and wire it to the device lifecycle so the Health
        # tab's Device Health table actually populates — the monitor (un)registers each
        # device on the DeviceManager's connect/disconnect events and back-fills any
        # already-known devices. Without this the per-device table stays permanently empty.
        self._health.start()
        self._health.attach_device_manager(self._dm)

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

        self._build_shortcuts()
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
        self._scan_worker: "_PortScanWorker | None" = None  # in-flight Scan-Ports enumeration (off-GUI-thread)

    # ── Adaptive shell layout (Spade v2 nav-chrome axis) ──────────
    # Make the app-shell respond to the form factor: sidebar folds to an icon rail and the bottom
    # terminal undocks whenever the nav chrome isn't a full sidebar. The DECISION is the pure
    # `layout_profile` nav_mode (unit-tested without Qt); here we map it to the shell collapse +
    # the terminal pane's visibility. Driven by nav_mode (form-factor AND density) — so the 7" touch
    # deck collapses even though it's "regular" width. Never touches the user's Simple/Pro depth
    # choice. Debounced on nav_mode, mirroring flash_tab.
    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._relayout_shell()

    def _relayout_shell(self) -> None:
        shell = getattr(self, "_app_shell", None)
        if shell is None:   # a resize can fire during construction, before the shell is wired
            return
        from src.ui.qt.layout_profile import layout_profile
        dpi = self.logicalDpiX() or 96
        from src.ui.qt.touch_mode import touch_active
        profile = layout_profile(max(1, self.width()), max(1, self.height()),
                                 touch=touch_active(), dpi=dpi)
        if profile.nav_mode == self._last_nav_key:   # debounce: re-apply only on a nav-mode change
            return
        self._last_nav_key = profile.nav_mode
        self._apply_shell_layout(profile)

    def _apply_shell_layout(self, profile) -> None:
        # Collapse the sidebar to an icon rail when the nav chrome isn't a full sidebar: 7" touch
        # deck resolves to 'rail' at ~800x480 though it is NOT compact, so the old is_compact gate
        # left it with desktop chrome. nav_mode fixes it (see layout_profile v2).
        self._app_shell.set_nav_mode(profile.nav_mode)
        self._app_shell.set_touch_density(profile.min_target_pt)   # 44px+ hit targets on touch
        # widget(1) of the vertical splitter is the terminal — docked only when the profile
        # says so (undocked on the deck/phone that need the room; a pull-up sheet later).
        splitter = getattr(self, "_main_splitter", None)
        if splitter is not None and splitter.count() > 1:
            splitter.widget(1).setVisible(profile.terminal_docked)

    # ── Menu bar ─────────────────────────────────────────────────────

    def _build_shortcuts(self) -> None:
        # Reform: the QMainWindow menu bar was REMOVED to match the mockup's clean top bar. Nothing
        # is lost: the menu's actions fold into the command palette (Ctrl+Shift+P; see
        # _wire_command_palette), and every shortcut it carried is re-anchored to the WINDOW below,
        # so it still fires app-wide without a menu.
        self.menuBar().hide()   # no visible menu bar (explicit; nothing populates it now)

        # Shortcut-carrying actions added to the window (no menu) so their accelerators still fire.
        act_quit = QAction("Quit", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.close)

        act_font_up = QAction("Increase Font Size", self)
        act_font_up.setShortcut("Ctrl+=")
        act_font_up.triggered.connect(lambda: self._change_font_size(1))

        act_font_down = QAction("Decrease Font Size", self)
        act_font_down.setShortcut("Ctrl+-")
        act_font_down.triggered.connect(lambda: self._change_font_size(-1))

        act_palette = QAction("Command Palette", self)
        act_palette.setShortcut("Ctrl+Shift+P")
        act_palette.setStatusTip("Jump to any tab or action by name — press Ctrl+Shift+P anywhere.")
        act_palette.triggered.connect(self._on_command_palette)

        for act in (act_quit, act_font_up, act_font_down, act_palette):
            self.addAction(act)

        # Interface Mode (Simple/Pro): the top-bar segment is the visible control now, but keep the
        # checkable actions alive (set_ui_mode syncs them, hasattr-guarded; test_dual_depth_ui reads
        # them) plus the Ctrl+M toggle that works even when part of the UI is hidden.
        self._mode_group = QActionGroup(self)
        self._mode_group.setExclusive(True)
        self._act_mode_simple = QAction("Simple", self, checkable=True)
        self._act_mode_pro = QAction("Pro", self, checkable=True)
        self._act_mode_simple.triggered.connect(lambda: self.set_ui_mode("simple"))
        self._act_mode_pro.triggered.connect(lambda: self.set_ui_mode("pro"))
        for a in (self._act_mode_simple, self._act_mode_pro):
            self._mode_group.addAction(a)
        shortcut_mode = QShortcut(QKeySequence("Ctrl+M"), self)
        shortcut_mode.activated.connect(self._toggle_ui_mode)

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
        sidebar_layout.setContentsMargins(0, 8, 0, 0)
        sidebar_layout.setSpacing(0)

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
        # Elide long device rows instead of growing a horizontal scrollbar in the narrow sidebar.
        self._sidebar_device_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._sidebar_device_list.currentItemChanged.connect(self._on_sidebar_device_selected)
        # Route a sidebar device pick to the Devices tab so the selection actually drives something
        # (focus that device / make it the active device). The slot guards on _device_tab existing,
        # so wiring it here (before the tabs are built) is safe — it only runs on user interaction.
        self.device_selected.connect(self._focus_device_in_devices_tab)
        sidebar_layout.addWidget(self._sidebar_device_list)

        # Quick-action buttons — stacked so the labels don't clip when the sidebar is narrow.
        quick_actions = QVBoxLayout()
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
        self._scan_btn = QPushButton("Scan Ports")
        self._scan_btn.clicked.connect(self._on_sidebar_scan)
        sidebar_layout.addWidget(self._scan_btn)

        # Wave-10 Phase C slice B: the device-sidebar folds into the ONE app-shell sidebar (below
        # the nav destinations, below), so the top area has a single sidebar, not two.
        self._device_sidebar = sidebar

        # ── Tab widget (right side) ──────────────────────────────────
        # Detachable: any tab can pop out into its own resizable window and re-docks back in.
        self._tabs = DetachableTabWidget()
        top_layout.addWidget(self._tabs)

        # Wave-10 Phase C slice A: the app-shell frame wraps the whole top area (sidebar + tabs)
        # so the global chrome (status bar / posture toggle / omnibar) and the top-level nav sidebar
        # frame the app. Additive: top_widget + _tabs are untouched inside; the tab-bar stays
        # this slice (dual nav); the sidebar nav + binder are wired after the surfaces are built.
        from src.ui.qt.page_layout import PageLayout
        self._app_shell = PageLayout()
        self._app_shell.set_content(top_widget)
        self._main_splitter.addWidget(self._app_shell)

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
        # Land on the reform front door: DEVICE -> Dashboard (the launch screen). Fall back to
        # OPERATE -> Console only if the DEVICE surface is hidden (loadout / popped out). Spade's guards
        # (test_lands_on_device_dashboard) require the launch to stay on DEVICE ▸ Dashboard.
        if self._tabs.indexOf(self._rig_surface) >= 0:
            self._show_subtab(self._rig_surface, self._device_dashboard)
        elif self._tabs.indexOf(self._operate_surface) >= 0:
            self._show_subtab(self._operate_surface, self._operate_action)
        self._refresh_sidebar_devices()
        self._build_command_palette()
        # Wave-10 Phase C (Phase D polish): fuse the app-shell omnibar with the command palette —
        # the brief's "command input + fuzzy search". Submitting the omnibar opens the palette
        # pre-filtered to the typed text (top match selected); the operator confirms with Enter, so
        # nothing runs straight off a keystroke (some commands are consequential).
        self._app_shell.omnibar_submitted.connect(self._on_omnibar_submitted)
        # Reform chrome (Atlas): the top-bar Simple/Pro segment drives the same depth toggle as the
        # View menu, and the ⤢ pop-out icon detaches the current tab (mirrors Ctrl+Shift+D).
        self._app_shell.depth_changed.connect(lambda m: self.set_ui_mode(m))
        self._app_shell.detach_requested.connect(lambda: self._tabs.detach_current())
        # Slice E: keep the Operate Home landing summary live. Target/capture counts refresh on
        # their bus events (like the shell badges); the device count refreshes with the sidebar.
        for _evt in ("target.added", "target.updated", "target.removed", "target.cleared",
                     "capture.added", "capture.removed", "capture.cleared", "capture.cracked"):
            self._bus.subscribe(_evt, self._refresh_home_summary)
        self._refresh_home_summary()

        # Apply the persisted interface mode to every tab now that they exist (no persist write-back).
        self._apply_ui_mode(self._load_ui_mode(), persist=False)

        # Apply the persisted touch-mode override so the responsive layout's touch paths (bigger hit
        # targets, the Nodes/Crack touch stacking) are live from the first relayout, not dead until
        # the user opens Settings. auto (default) auto-detects; on/off force it. See touch_mode.py.
        try:
            from src.config.settings import load_settings
            from src.ui.qt.touch_mode import set_touch_mode
            set_touch_mode(str(load_settings().get("interface", {}).get("touch_mode", "auto")))
        except Exception:  # noqa: BLE001 — never let a settings read block window construction
            pass

        # Re-open any tabs the user had popped out into their own windows last session.
        try:
            self._tabs.restore_detached(self._qsettings.value("detached_tabs", "") or "")
        except Exception:  # noqa: BLE001 — restoring pop-outs must never block startup
            log.exception("Failed to restore detached tabs")

    # ── Tabs ─────────────────────────────────────────────────────────

    def _build_tabs(self) -> None:
        # Spade v2 verb IA (P2.5). Every leaf tab below is created ONCE, then re-parented into one of the
        # 5 verb surfaces (RIG · HUNT · OPERATE · CRACK · MAP) built at the end of this method + mounted
        # FROM src/core/nav_model.visible_nav() — the "missing consumer" the design's #1 move needed. So
        # every self._<tab> reference + the palette + tests keep working; only which verb QTabWidget each
        # leaf sits in changed from the old WS-6 noun grouping. safety.py is untouched by any of this.

        # RIG leaves — Firmware (the FlashTab: ESP32/Flipper/RTL firmware + vault) + Software OS (bootable
        # PC/USB images: Kali/Tails/Arch).
        self._flash_tab = FlashTab(self._dm, self._fe, self._vault)
        self._software_tab = SoftwareTab()

        # RIG leaves — Devices (device control + serial terminal) + Health (host + device-health gauges) +
        # Nodes (W1.1 wireless-node key management).
        self._device_tab = DeviceTab(self._dm, self._pool, self._ingestor, recorder=self._macro)
        self._device_tab._dms_auth = self._dms_auth
        # Teach the Devices tab to echo a device's serial RETURNS into the app-wide bus too — but only for
        # ports the bottom terminal does NOT itself own (it renders those via _pterm_on_line, so a co-owned
        # port would double-echo). This is what makes "every return" reach the always-visible bottom
        # terminal even when a board is connected only on the Devices tab. _pterm_conns is built earlier in
        # _build_persistent_terminal; this wiring must come after _device_tab exists (it does). See
        # device_tab._on_line_received.
        self._device_tab._pterm_owns_port = lambda port: port in self._pterm_conns
        self._health_tab = HealthTab(self._health)
        # Wireless nodes (W1.1): manage provisioned per-node keys — gate-locked + key-free.
        self._nodes_tab = NodesTab(self._dm)

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
        # ── WS-6 Proposal A (owner-approved 2026-07-21): the old 8-tab Operate is regrouped by workflow.
        # Operate = the live action loop only (Targets · Broadcast · Console · Macros). The GPS-tagged
        # survey/map trio moves to a new Survey surface, and Crack Lab + BLE Analyzer to Analyze (the renamed
        # Network surface). Every widget is created ONCE and re-parented, so every self._<tab> reference +
        # the palette + tests keep working; only which inner QTabWidget each sits in changed.
        from src.ui.qt.wardrive_multi_tab import WardriveMultiTab
        self._wardrive_multi_tab = WardriveMultiTab(device_manager=self._dm)  # F1: concurrent multi-board capture
        # FL F5: the located-ALPR-camera street map (a real tab with wake/sleep hooks, not the old Tools window).
        self._flock_heatmap = FlockHeatmapTab()
        # Crack Lab (offline WPA crack pipeline + wordlist manager): capture -> wordlist -> per-run consent ->
        # hashcat/aircrack. Dictionary-only; the consent gate is never bypassed. Passed the cross-comm hub so
        # its Captures table auto-populates from the shared capture log.
        from src.ui.qt.crack_lab_tab import CrackLabTab
        self._crack_lab_tab = CrackLabTab(self._hub)
        # Operate console (B16): a button-driven single-device console — status-poll header, SAFE/ARMED lamp,
        # two-factor arm toggle, per-firmware TX-gated command grid. Shares the Devices tab's ingestor + its
        # _dms_seen set (so a Dead-Man's-Switch port is never auto-polled here either).
        from src.ui.qt.operate_tab import OperateTab
        self._operate_console = OperateTab(
            self._dm, self._ingestor, recorder=self._macro, dms_seen=self._device_tab._dms_seen,
        )
        # BLE Analyzer (output view): the on-device Bluetooth-analyzer visual — a live RSSI graph + device
        # table, fed by ble_found events from EVERY BLE firmware via the ingestor tap (see _wire_ble_analyzer).
        # An awareness/analysis view (it transmits nothing), so it lives in Analyze.
        from src.ui.qt.ble_analyzer_tab import BleAnalyzerTab, BleScanController
        # A3: the analyzer's Start/Stop drives a BLE scan on every connected device via the SAME
        # shared broadcast engine the Console/All-Devices tabs use (each runs its own scan verb),
        # and the sends surface in the shared terminal — so scanning cross-talks across surfaces.
        _ble_scan = BleScanController(self._broadcast) if BleAnalyzerTab is not None else None
        self._ble_analyzer = (BleAnalyzerTab(scan_controller=_ble_scan)
                              if BleAnalyzerTab is not None else None)
        # Wi-Fi Analyzer (output view): the on-device Wi-Fi-analyzer visual — a channel-occupancy
        # graph + an AP table (SSID/BSSID/channel/enc/clients/handshake), fed by ap_found / rogue_ap
        # / client_found / handshake_captured / pmkid_captured events from EVERY scanning firmware via
        # the ingestor tap (see _wire_wifi_analyzer). Awareness-only — it transmits nothing and has no
        # scan control of its own, so it lives in Analyze next to the BLE Analyzer.
        from src.ui.qt.wifi_analyzer_tab import WifiAnalyzerTab
        self._wifi_analyzer = WifiAnalyzerTab() if WifiAnalyzerTab is not None else None
        if self._wifi_analyzer is not None:
            # P3 flow B: HUNT Wi-Fi analyzer hands a captured handshake to Crack Lab.
            self._wifi_analyzer.crack_capture_requested.connect(self._on_crack_capture_requested)

        # QA-1 (decision #9, reverses the 07-21 Option-B split): merge Broadcast (fan-out) + Console
        # (single-device) into ONE Operate screen — a vertical splitter with the fan-out verb bar on
        # top and the single-device console below, instead of two separate "All Devices"/"Control"
        # sub-tabs. Both are the SAME re-parented instances, so every self._broadcast_bar /
        # _operate_console reference (dual-depth fan-out, palette, tests) keeps working. Safety is
        # untouched — the console still gates offensive verbs behind its SAFE/ARMED two-factor arm.
        self._operate_action = QSplitter(Qt.Vertical)
        self._operate_action.addWidget(self._broadcast_bar)
        self._operate_action.addWidget(self._operate_console)
        self._operate_action.setStretchFactor(0, 0)
        self._operate_action.setStretchFactor(1, 1)

        # The OPERATE verb surface is assembled at the end of this method: Home launcher + the QA-1 merged
        # Control (preserved verbatim — re-splitting it would reverse owner decision #9) + Macros. Targets
        # re-homes to HUNT (nav_model), so it is no longer an OPERATE sub-view.

        # OPERATE HOME (dual-axis launcher). Spade v2 P2c: Wi-Fi/BLE/Tools/Settings are external — a tap
        # navigates to the real surface (HUNT's analyzers, CRACK's Crack Lab, Settings) instead of embedding
        # a duplicate. No more _oh_wifi/_oh_ble clones double-fed from the event taps (transmit-nothing dupes
        # + an orphan-tap crash risk); the shell routes navigate_requested. P2.5 kills the double-Operate: the
        # launcher is now the FIRST sub-view of the ONE OPERATE surface (was a peer top-level "Operate Home"),
        # so OperateHome's OWN domain grid is the Operate content nav — the radio axis of the two-level IA.
        from src.ui.qt.operate_home import build_operate_home
        self._operate_home = build_operate_home(external_domains={"tools", "settings"})
        self._operate_home.navigate_requested.connect(self._on_home_navigate)

        # Fill-from-target (Track B UX #3): a target selected in the Targets tab pushes its MAC/SSID/channel
        # into the Macro tab's variable fields, so a discovery in one surface is reusable in another.
        self._targets_tab.fill_macro_requested.connect(self._on_use_target_as_macro)
        # P3 flow C: Targets/HUNT "Operate this device" -> OPERATE console, device pre-selected.
        self._targets_tab.operate_device_requested.connect(self._on_operate_device_requested)
        # P3 flow D: Wardrive "View on map" -> the finished CSV opens on the MAP as an AP layer.
        if self._wardrive_tab is not None:
            self._wardrive_tab.view_wardrive_on_map_requested.connect(self._on_view_wardrive_on_map)

        # Mesh + Graph leaves (from the dissolved Analyze bundle): the node Graph re-homes to HUNT; Cross-Comm
        # routing re-homes to RIG (labelled "Mesh"). Created once, re-parented into their verb surface below.
        self._cross_comm_tab = CrossCommTab(self._bus, self._pool, self._router, self._dm)
        from src.ui.qt.network_tab import NetworkTab
        self._network_tab = NetworkTab(self._dm, self._pool, self._action_resolver, self._send_to_port,
                                       event_bus=self._bus)

        # Settings (persisted) — the pinned utility surface, rendered apart from the 5 job-verbs.
        self._settings_tab = SettingsTab()
        # The Settings tab's "Check now" button asks the window to run a manual (forced) update check.
        self._settings_tab.check_updates_requested.connect(lambda: self.check_for_updates(force=True))

        # ── Spade v2 verb surfaces (Axis 1). Each is ONE inner QTabWidget grouping the leaves nav_model
        # assigns to that verb; the WS-6 noun surfaces (Flash/Connect/Survey/Analyze) are dissolved into
        # these. An optional analyzer that is None is skipped (never an empty tab). Icons via label_icon.
        def _verb_surface(*panes) -> QTabWidget:
            w = QTabWidget()
            for _label, _widget in panes:
                if _widget is not None:
                    w.addTab(_widget, label_icon(_label), _label)
            return w

        # DEVICE — the reform front door: Dashboard (landing) · Firmware · Software OS · Mesh. The
        # DeviceDashboard RE-HOMES the Devices + Health leaf widgets (device list, gauges, readouts,
        # serial terminal, BlueJammer/Mesh panels) into ONE landing screen, so Devices + Health are no
        # longer their own sub-tabs; the device_tab/health_tab instances stay created (headless, pumped
        # by the Dashboard) so every signal/ref survives. Nodes re-homes to Mesh (follow-up) — kept
        # created for now. Mesh stays the whole CrossCommTab; the Dashboard takes cross_comm=None until
        # its own cross-comm summary lands (avoids double-parenting the one widget). (_rig_surface attr
        # name kept to minimize churn; nav_model relabels the rail RIG -> DEVICE.)
        from src.ui.qt.device_dashboard import DeviceDashboard
        self._device_dashboard = DeviceDashboard(self._health_tab, self._device_tab, cross_comm=None)
        self._rig_surface = _verb_surface(
            ("Dashboard", self._device_dashboard), ("Firmware", self._flash_tab),
            ("Software OS", self._software_tab), ("Mesh", self._cross_comm_tab),
        )
        # HUNT — "see what's out there, passively": Wi-Fi · BLE analyzers · Targets · node Graph.
        self._hunt_surface = _verb_surface(
            ("Wi-Fi", self._wifi_analyzer), ("BLE", self._ble_analyzer),
            ("Targets", self._targets_tab), ("Graph", self._network_tab),
        )
        # OPERATE — the ONE action surface (kills the double-Operate): Home launcher · merged Control · Macros.
        # Control is the QA-1 splitter (fan-out Broadcast + single-device Console), preserved verbatim.
        # Reform (P2): OPERATE opens on the single dense Console (OperateTab), matching the mockup —
        # the old tile Operate-Home is retired from the surface (sub-tabs: Console | Macros). Home stays
        # constructed + hidden below so its summary/action refreshes — which PRIME the console's active
        # device (_rebuild_home_actions -> console.select_device) — keep working harmlessly.
        self._operate_surface = _verb_surface(
            ("Console", self._operate_action), ("Macros", self._macro_tab),
        )
        self._operate_home.setParent(self._operate_surface)
        self._operate_home.hide()
        # CRACK — "capture -> key": the offline Crack Lab.
        self._crack_surface = _verb_surface(("Crack Lab", self._crack_lab_tab))
        # MAP — one canvas: Wardrive · Multi-Wardrive · Flock / ALPR.
        self._map_surface = _verb_surface(
            ("Wardrive", self._wardrive_tab), ("Multi-Wardrive", self._wardrive_multi_tab),
            ("Flock Map", self._flock_heatmap),
        )

        # Tap the analyzer event feeds now the analyzers exist + are mounted under HUNT. The taps key off the
        # widget (not its container), so the Analyze -> HUNT move keeps every feed live — nothing goes dark.
        if self._ble_analyzer is not None:
            self._wire_ble_analyzer()   # tap ble_found events (see the method)
        if self._wifi_analyzer is not None:
            self._wire_wifi_analyzer()  # tap ap_found/handshake events (see the method)

        # verb-key -> surface (the nav_model keys). Settings is the pinned utility node.
        self._verb_surfaces: "dict[str, object]" = {
            "rig": self._rig_surface, "hunt": self._hunt_surface, "operate": self._operate_surface,
            "crack": self._crack_surface, "map": self._map_surface, "settings": self._settings_tab,
        }

        # P3 flow-spine: cross-surface hand-off targets, (surface_key, sub_view) -> (nav_surface,
        # nav_widget, receive_widget). dispatch_intent navigates to nav_widget then calls the intent's
        # action on receive_widget. Each receive method LOADS / PRE-SELECTS only, never arms; safety
        # stays untouched. Crack Lab load_capture (B), Operate console select_device (C), Flock Map
        # load_wardrive_csv (D) -- the awareness-only map layer, which drives no device.
        self._flow_targets: "dict[tuple, tuple]" = {
            ("crack", "crack_lab"): (self._crack_surface, self._crack_lab_tab, self._crack_lab_tab),
            ("operate", "control"): (self._operate_surface, self._operate_action, self._operate_console),
            ("map", "flock"): (self._map_surface, self._flock_heatmap, self._flock_heatmap),
        }

        # ── Mount the top-level rail FROM nav_model.visible_nav() — the "missing consumer" P2.5 wires. A
        # capability-gated surface with no provider (Sense, until node firmware) is dropped by the tree, not
        # by a hardcoded list, so "wire-it-or-it-doesn't-appear" is structural. Then the pinned Settings gear.
        import src.core.nav_model as _nav
        for _node in _nav.visible_nav(self._nav_capabilities()):
            _vsurface = self._verb_surfaces.get(_node.key)
            if _vsurface is not None:
                self._tabs.addTab(_vsurface, label_icon(_node.label), _node.label)
        # Reform (P3): TERMINAL is a pinned rail surface — the persistent terminal hub (device list +
        # per-port terminals), moved out of the docked bottom pane, mounted above Settings.
        self._tabs.addTab(self._term_frame, label_icon("Terminal"), "Terminal")
        _settings_node = _nav.settings_node()
        self._tabs.addTab(self._settings_tab, label_icon(_settings_node.label), _settings_node.label)

        # (Mission Planner tab removed — was a non-functional "coming soon" placeholder; tracked as a
        # real future feature in the internal roadmap notes. Don't ship dead tabs.)

        # How-To lives under the Help menu (see _on_howto), not the tab strip — keeps the top level at the
        # 5 verb surfaces (RIG / HUNT / OPERATE / CRACK / MAP) + the pinned Settings + Help.

        # Wave-10 Phase C slice A: wire the app-shell sidebar as top-level nav now that the surfaces
        # exist. Each destination selects its surface in _tabs (dual with the tab-bar this slice).
        from src.ui.qt.page_layout_binder import PageLayoutBinder
        self._shell_surfaces: dict[str, object] = {}
        for _label, _surface in self._tab_registry():
            _key = _label.lower().replace(" ", "-")
            self._shell_surfaces[_key] = _surface
            self._app_shell.add_destination(_key, _label)
        self._app_shell.destination_selected.connect(self._on_shell_nav)
        self._app_shell_binder = PageLayoutBinder(self._app_shell, self._hub)
        # Reform chrome (Atlas): the rail is nav-only now — the DEVICE Dashboard owns the device list,
        # connect/scan and firmware controls, so the old device panel is no longer folded into the rail
        # (that made the 196px rail cluttered vs the approved mockup). Kept alive + parented but hidden
        # so its refresh timer / selection signals never touch an orphan top-level window.
        self._device_sidebar.setParent(self._app_shell)
        self._device_sidebar.hide()
        # Keep the sidebar in step with the tab strip (mode/loadout hides some surfaces).
        self._tabs.currentChanged.connect(self._sync_shell_nav)
        # Reform chrome (Atlas): mirror the active sub-tab into the top-bar breadcrumb leaf, so an
        # inner sub-tab switch shows e.g. "DEVICE ▸ Firmware" (not just the verb).
        for _surface in self._verb_surfaces.values():
            if isinstance(_surface, QTabWidget):
                _surface.currentChanged.connect(self._sync_shell_crumb_leaf)
        self._sync_shell_nav()
        # Wave-10 Phase C (slice C): the app-shell sidebar is now the SOLE nav, so hide the flat tab
        # strip — the veneer's last piece. The tabs still exist (setCurrentWidget drives them
        # from the sidebar + palette); detach stays reachable via Ctrl+Shift+D + the "Detach Current
        # Tab" palette command (the bar's double-click/context-menu paths go with the bar). Qt keeps
        # a manual hide across addTab/removeTab (tabBarAutoHide off), so loadout won't un-hide it.
        self._tabs.tabBar().hide()

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

    def _on_shell_nav(self, key: str) -> None:
        """App-shell sidebar nav: select the top-level surface for this destination in the tabs
        (dual with the tab-bar this slice). Unknown/hidden surface -> no-op, never crashes."""
        surface = self._shell_surfaces.get(key)
        if surface is not None and self._tabs.indexOf(surface) >= 0:
            self._tabs.setCurrentWidget(surface)

    def _on_omnibar_submitted(self, text: str) -> None:
        """Hand the app-shell omnibar text to the command palette as a pre-filled fuzzy query."""
        self._palette.open_palette_with(text)

    def _refresh_home_summary(self, *_args) -> None:
        """Push live hub counts into the Operate Home landing summary (slice E). Mirrors the
        binder's grounded reads (pool / captures / dm), so the metric strip shows the same truth.
        Wired to target/capture bus events + the device refresh; a no-op before OperateHome up."""
        home = getattr(self, "_operate_home", None)
        if home is None or not hasattr(home, "set_summary"):
            return
        hub = self._hub
        pool = getattr(hub, "pool", None)
        caps = getattr(hub, "captures", None)
        dm = getattr(hub, "dm", None)
        targets = int(getattr(pool, "count", 0) or 0) if pool is not None else 0
        captures = int(getattr(caps, "count", 0) or 0) if caps is not None else 0
        devs = list(dm.list_connected()) if dm is not None and hasattr(dm, "list_connected") else []
        states = [getattr(d, "arm_state", "") for d in devs]
        armed = "armed" if "armed" in states else ("pending" if "pending" in states else "")
        # Grounded session value the status bar doesn't show: the most recent capture (insertion
        # order, so all()[-1] is the last one logged). ssid may be empty -> fall back to the BSSID.
        recent = caps.all() if caps is not None and hasattr(caps, "all") else []
        last_capture = ""
        if recent:
            r = recent[-1]
            name = getattr(r, "ssid", "") or getattr(r, "bssid", "") or "capture"
            ctype = getattr(r, "capture_type", "")
            last_capture = f"{name} ({ctype})" if ctype else name
        home.set_summary(len(devs), targets, captures, armed, last_capture)
        self._refresh_home_actions()

    def _primary_operate_port(self) -> str:
        """The port whose firmware drives Home's one-tap strip: the Operate console's already-active
        device if still connected, else the first connected device. Empty string if none."""
        dm = getattr(self._hub, "dm", None)
        has = dm is not None and hasattr(dm, "list_connected")
        connected = list(dm.list_connected()) if has else []
        ports = [getattr(d, "port", "") for d in connected if getattr(d, "port", "")]
        console = getattr(self, "_operate_console", None)
        active = getattr(console, "_active_port", "") if console is not None else ""
        if active and active in ports:
            return active
        return ports[0] if ports else ""

    def _refresh_home_actions(self) -> None:
        """Keep Home's Zone B strip live on the same cadence as the summary. Refresh tile readiness
        every call (cheap, no teardown); REBUILD the strip only when the primary operate
        (port, firmware) changes — connect / disconnect / firmware-change — so a steady-state poll
        never tears down an open OpPanel (WS3 finding 2). The rebuild also primes the active
        device (finding 1). The key is stored BEFORE the rebuild, so priming is re-entrant-safe."""
        home = getattr(self, "_operate_home", None)
        console = getattr(self, "_operate_console", None)
        if home is None or console is None or not hasattr(home, "set_actions"):
            return
        port = self._primary_operate_port()
        dm = getattr(self._hub, "dm", None)
        has_dev = dm is not None and port and hasattr(dm, "get_device")
        dev = dm.get_device(port) if has_dev else None
        fw = (getattr(dev, "firmware", "") or "").strip().lower() if dev is not None else ""
        key = (port, fw)
        if key != getattr(self, "_home_actions_key", None):
            self._home_actions_key = key
            self._rebuild_home_actions(port, fw)
        home.refresh_readiness()

    def _rebuild_home_actions(self, port: str, fw: str) -> None:
        """(Re)build Home's one-tap strip for *fw* on *port*. Prime the console's active device so
        run_curated/ready_for act on THIS port even if the Control sub-view was never opened; then
        derive the curated verbs + STOP mode, and hand them to the strip. Guarded send untouched."""
        home, console = self._operate_home, self._operate_console
        wiring = (console.run_curated, console._send, console.ready_for, console.safe_state)
        if not port or not fw:
            home.set_actions([], *wiring, supports_arm=False, stop_ci=None)
            return
        console.select_device(port)   # prime _active_port (finding 1); nav-only, never sends
        from src.protocols import get_protocol
        from src.ui.qt.operate_featured import featured_actions
        proto = get_protocol(fw)
        supports_arm = bool(getattr(proto, "supports_arm", False))
        stop_ci = None if supports_arm else self._first_stop_verb(proto)
        home.set_actions(featured_actions(proto), *wiring,
                         supports_arm=supports_arm, stop_ci=stop_ci)

    @staticmethod
    def _first_stop_verb(proto):
        """The first catalog verb matching stop|disarm|reset|off|halt, for a non-arming firmware's
        STOP (Marauder/DIV/GhostESP have no arm concept). None if the catalog has no such verb, so
        the strip shows a disabled 'no stop verb' chip, never a fake button."""
        import re
        try:
            cmds = list(proto.cached_commands())
        except Exception:   # noqa: BLE001 — a catalog-less protocol simply has no stop verb
            return None
        pat = re.compile(r"stop|disarm|reset|off|halt", re.I)
        for ci in cmds:
            if pat.search(getattr(ci, "name", "") or ""):
                return ci
        return None

    def _sync_shell_nav(self, *_args) -> None:
        """Keep the app-shell sidebar in step with the tabs: show only destinations whose surface
        is in the tab strip (loadout/mode hides some), and highlight the current one. Wired
        to currentChanged + called after apply_loadout, so the sidebar never lists a hidden tool."""
        shell = getattr(self, "_app_shell", None)
        if shell is None:
            return
        cur = self._tabs.currentWidget()
        cur_key = None
        for key, surface in self._shell_surfaces.items():
            shell.set_destination_visible(key, self._tabs.indexOf(surface) >= 0)
            if surface is cur:
                cur_key = key
        if cur_key is not None:
            shell.highlight_destination(cur_key)
            self._sync_shell_crumb_leaf()

    def _sync_shell_crumb_leaf(self, *_args) -> None:
        """Set the top-bar breadcrumb leaf to the active surface's current sub-tab (e.g.
        DEVICE ▸ Dashboard), so the crumb mirrors the mockup. A plain surface (no sub-tabs) clears it."""
        shell = getattr(self, "_app_shell", None)
        if shell is None or not hasattr(shell, "set_breadcrumb_leaf"):
            return
        cur = self._tabs.currentWidget()
        leaf = ""
        if isinstance(cur, QTabWidget) and cur.count() > 0:
            leaf = cur.tabText(cur.currentIndex())
        shell.set_breadcrumb_leaf(leaf)

    # ── Loadout (which firmwares/hardware → which tabs are shown) ─────
    def _show_subtab(self, surface, widget) -> None:
        """Focus a sub-view inside a grouped surface: select the surface at top level, then the sub-tab.
        Used for by-widget navigation into a verb surface's sub-views (P2.5 verb IA)."""
        if self._tabs.indexOf(surface) >= 0:
            self._tabs.setCurrentWidget(surface)
        surface.setCurrentWidget(widget)

    def dispatch_intent(self, intent) -> bool:
        """Route a :class:`src.core.flow_intent.FlowIntent` — the P3 cross-surface hand-off dispatcher.
        Resolves the destination from ``_flow_targets`` (unknown / loadout-hidden -> log + no-op, never
        crashes), navigates there via ``_show_subtab``, then hands ``object_ref`` to the destination's
        ``action`` receive method. Any device send a receive method makes still routes through the
        EXISTING guarded path — this dispatcher never introduces a send and never arms. Returns True iff
        the intent was routed. Substrate only: the per-surface EMITTERS (later P3 slices) build + pass
        the intents; this method already backs the Crack Lab + Operate-console targets."""
        key = (getattr(intent, "surface_key", None), getattr(intent, "sub_view", None))
        tgt = self._flow_targets.get(key)
        if tgt is None:
            log.debug("dispatch_intent: no flow target for %r", key)
            return False
        surface, nav_widget, receive_widget = tgt
        if self._tabs.indexOf(surface) < 0:
            log.debug("dispatch_intent: surface for %r not mounted (loadout-hidden)", key)
            return False
        self._show_subtab(surface, nav_widget)
        action = getattr(intent, "action", "") or ""
        if action and getattr(intent, "object_ref", None) is not None and hasattr(receive_widget, action):
            try:
                getattr(receive_widget, action)(intent.object_ref)
            except Exception:  # noqa: BLE001 — a receive method must never take the app down
                log.exception("dispatch_intent: %s.%s failed", type(receive_widget).__name__, action)
        return True

    def _on_crack_capture_requested(self, bssid: str) -> None:
        """P3 flow B: the Wi-Fi analyzer asks to send an AP's handshake to Crack Lab. Resolve the
        CaptureRecord for this BSSID from the store + hand it off via a FlowIntent - LOAD-only
        (load_capture never starts a crack; the per-run consent gate stays the single arming point)
        and this never touches the guarded send path."""
        from src.core.flow_intent import FlowIntent
        store = getattr(self._hub, "captures", None)
        if store is None or not bssid:
            return
        b = bssid.strip().lower()
        all_recs = list(store.all())
        recs = [r for r in all_recs if (getattr(r, "bssid", "") or "").lower() == b]
        if not recs:
            # The analyzer ticks "HS" on every AP sharing an ESSID, but a BSSID-less capture (e.g. a
            # LxveOS `hs`) is logged under ONE bssid — so a sibling AP can show the tick yet have no
            # record under ITS bssid. Fall back to matching by SSID; if still nothing, tell the operator
            # (a non-blocking toast) rather than a SILENT no-op on a tick that promised a capture.
            ssid = self._ssid_for_bssid(bssid)
            if ssid:
                recs = [r for r in all_recs if (getattr(r, "ssid", "") or "").strip() == ssid]
            if not recs:
                self.toast("No handshake is in the capture store under this BSSID yet — a network-wide "
                           "capture can be logged under a different AP. Open Crack Lab to check.", "warning")
                return
        # Prefer a record with a crackable artifact (a pcap/hc22000 file or a complete inline line).
        rec = next((r for r in recs if r.pcap_path or r.hc22000_path or r.hc22000_line), recs[0])
        self.dispatch_intent(FlowIntent("crack", "load_capture", rec, sub_view="crack_lab"))

    def _ssid_for_bssid(self, bssid: str) -> str:
        """Best-effort SSID for a BSSID from the Wi-Fi analyzer's model (fully guarded — returns '' if
        the analyzer/model/AP is absent). Broadens the capture match past a strict BSSID so a
        network-wide handshake still resolves from a sibling AP row (P3 flow B silent-no-op fix)."""
        b = (bssid or "").strip().lower()
        model = getattr(getattr(self, "_wifi_analyzer", None), "model", None)
        if model is None or not b:
            return ""
        try:
            aps = model.access_points()
        except Exception:  # noqa: BLE001 — a lookup helper must never break the hand-off
            return ""
        for ap in aps:
            if (getattr(ap, "bssid", "") or "").strip().lower() == b:
                return (getattr(ap, "ssid", "") or "").strip()
        return ""

    def _on_operate_device_requested(self, target) -> None:
        """P3 flow C (target->OPERATE): the Targets/HUNT list opens the OPERATE console with this
        target's discovering device pre-selected. Hands off via a FlowIntent to select_device -
        NAVIGATION-only: it just drives the picker combo; the two-factor arm gate stays the single
        arming point, so this can never one-tap a send."""
        from src.core.flow_intent import FlowIntent
        port = getattr(target, "device_source", "") or ""
        if not port:
            return
        self.dispatch_intent(FlowIntent("operate", "select_device", port, sub_view="control"))

    def _on_view_wardrive_on_map(self, csv_path: str) -> bool:
        """P3 flow D (wardrive->MAP): a finished wardrive's "View on map" opens its CSV on the MAP
        as a Wi-Fi AP layer. Routes FlowIntent("map","load_wardrive_csv", path, sub_view="flock") ->
        FlockHeatmapTab.load_wardrive_csv (returns 0 on a bad file, so it never crashes).
        Awareness-only: the map plots located APs; nothing is armed or sent."""
        from src.core.flow_intent import FlowIntent
        if not csv_path:
            return False
        intent = FlowIntent("map", "load_wardrive_csv", csv_path, sub_view="flock")
        return self.dispatch_intent(intent)

    def _on_home_navigate(self, key: str) -> None:
        """An Operate-Home 'external' tile asks to open its real surface — route there, not a placeholder.
        Post-P2.5: Crack Lab is under CRACK; the Wi-Fi/BLE analyzers under HUNT; Settings is the pinned tab."""
        if key == "tools":
            self._show_subtab(self._crack_surface, self._crack_lab_tab)
        elif key == "wifi" and getattr(self, "_wifi_analyzer", None) is not None:
            self._show_subtab(self._hunt_surface, self._wifi_analyzer)
        elif key == "ble" and getattr(self, "_ble_analyzer", None) is not None:
            self._show_subtab(self._hunt_surface, self._ble_analyzer)
        elif key == "settings" and self._tabs.indexOf(self._settings_tab) >= 0:
            self._tabs.setCurrentWidget(self._settings_tab)

    def _tab_registry(self) -> "list[tuple[str, object]]":
        """Canonical (label, widget) top-level surfaces in nav_model order — the source of truth for the
        loadout show/hide + the app-shell sidebar. Verb IA (P2.5): RIG · HUNT · OPERATE · CRACK · MAP +
        the pinned Settings. Labels MUST match src/config/loadout.TAB_ORDER (apply_loadout maps by label);
        the keys the shell derives (label.lower().replace(' ','-')) match the nav_model keys."""
        import src.core.nav_model as _nav
        reg: "list[tuple[str, object]]" = []
        for _node in _nav.visible_nav(self._nav_capabilities()):
            _vsurface = self._verb_surfaces.get(_node.key)
            if _vsurface is not None:
                reg.append((_node.label, _vsurface))
        reg.append(("Terminal", self._term_frame))   # reform P3: pinned terminal hub, above Settings
        reg.append((_nav.settings_node().label, self._settings_tab))
        return reg

    def _nav_capabilities(self) -> "set[str]":
        """Capabilities a real provider backs — gates the capability-keyed nav nodes (nav_model.visible_nav).
        Today only 'sense' (counter-surveillance) is gated and no provider exists yet (node firmware, P4), so
        this is empty and Sense stays ABSENT from the rail rather than shipped as an inert tab. When a real
        sense provider lands, add 'sense' here and the reserved surface appears with zero rail rework."""
        return set()

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
                self._tabs.addTab(w, label_icon(label), label)   # keep the icon when a tab is restored
        # Restore the selection, or fall back to the Connect surface (a core surface, always present).
        if cur is not None and self._tabs.indexOf(cur) >= 0:
            self._tabs.setCurrentWidget(cur)
        elif self._tabs.indexOf(self._rig_surface) >= 0:
            self._tabs.setCurrentWidget(self._rig_surface)
        self._loadout = L.normalize(lo)
        self._sync_shell_nav()   # the visible tab set changed -> mirror it in the shell sidebar
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
        shell = getattr(self, "_app_shell", None)
        if shell is not None and hasattr(shell, "set_depth"):
            shell.set_depth(mode)   # keep the top-bar Simple/Pro segment in step (no re-emit)
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

        # Send-target selector: choose WHERE a typed line goes — auto-route, the connected device(s),
        # or the local tool shell — instead of inferring it silently from the first word (owner
        # 2026-07-21: "you should be able to choose what youre sending to"). The device checklist on
        # the left still picks WHICH serial devices; this picks the channel.
        self._pterm_target = QComboBox()
        self._pterm_target.addItem("Auto", "auto")
        self._pterm_target.addItem("Device(s)", "serial")
        self._pterm_target.addItem("Computer", "computer")
        self._pterm_target.setToolTip(
            "Where Enter sends this line:\n"
            "• Auto — a known tool (aircrack-ng/hashcat/…) runs on the computer, anything else goes "
            "to the checked device(s)\n"
            "• Device(s) — always send to the checked serial device(s)\n"
            "• Computer — always run on the computer's bundled tool shell"
        )
        self._pterm_target.setStyleSheet(
            "QComboBox { background: #161b22; color: #e6edf3; border: 1px solid #30363d; "
            "border-radius: 4px; font-size: 8pt; padding: 3px 6px; }"
            "QComboBox QAbstractItemView { background: #161b22; color: #e6edf3; "
            "selection-background-color: #30363d; }"
        )
        self._pterm_target.currentIndexChanged.connect(self._pterm_on_target_changed)
        input_row.addWidget(self._pterm_target)

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

        # Reform (P3): the persistent terminal moves OUT of the docked bottom splitter pane into its own
        # TERMINAL rail surface (mounted in _build_tabs). Kept as self._term_frame so _build_tabs can add
        # it as a verb surface; NOT added to _main_splitter (which now holds only the app shell). The
        # _apply_shell_layout terminal-dock logic already guards on splitter.count() > 1, so a one-pane
        # splitter is safe. Every _pterm_* wiring below is unchanged — the terminal still receives lines.
        self._term_frame = term_frame

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

        # Subscribe the terminal to the app-wide activity bus so flashing, command execution,
        # broadcasts, crack runs and background ops all surface here — not just serial RX. The bus is a
        # QObject, so worker-thread emits queue onto this (GUI-thread) connection safely.
        from src.core.activity_log import activity_log
        self._activity_log = activity_log()
        self._activity_log.line.connect(self._pterm_on_activity)

        # Capture-confirm correlator (punch-list #2 slice 5): surface a deauth->handshake match and
        # its honest timeouts as activity-log lines. Bus callbacks fire on the ingest thread; emit
        # queues onto this GUI thread safely (activity_log.line is a Qt signal). A 5s timer drives
        # correlator.sweep() so a window that passes with no capture reports at the right time.
        hub = getattr(self, "_hub", None)
        if hub is not None and getattr(hub, "correlator", None) is not None:
            self._bus.subscribe("capture.confirmed", self._on_capture_confirmed)
            self._bus.subscribe("capture.timeout", self._on_capture_timeout)
            self._capture_sweep_timer = QTimer(self)
            self._capture_sweep_timer.setInterval(5000)
            self._capture_sweep_timer.timeout.connect(hub.correlator.sweep)
            self._capture_sweep_timer.start()

        # Refresh device checklist
        self._pterm_refresh_ports()

    def _pterm_on_target_changed(self, _index: int) -> None:
        """Reflect the chosen send-target in the input placeholder so it's obvious where Enter goes."""
        target = self._pterm_target.currentData()
        hints = {
            "auto": "Type a command — known tools run on the computer, anything else goes to checked devices…",
            "serial": "Type a command to send to the checked device(s)…",
            "computer": "Type a bundled tool command (aircrack-ng / hashcat / …) to run on the computer…",
        }
        self._pterm_input.setPlaceholderText(hints.get(target, hints["auto"]))

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

    @staticmethod
    def _resolve_pterm_connect_ports(
        checked: "list[str]", listed: "list[str]"
    ) -> "tuple[list[str], str | None]":
        """Ports a persistent-terminal (bottom-left) Connect click should open.

        Ticked devices win. If NONE are ticked, fall back to the sole listed device so the button
        works without a manual pre-tick — the owner-reported "bottom-left Connect still doesn't
        work" was exactly this: Connect no-opped on an empty check-selection (v1.7.1 fixed the
        *Devices-tab* buttons, not these). Disconnect already fell back to "all connected"; Connect
        did not. With zero or several listed devices, connect nothing and return a message
        (auto-connecting ALL on an empty selection opens ports the user didn't intend)."""
        if checked:
            return checked, None
        if len(listed) == 1:
            return listed, None
        if not listed:
            return [], "No devices -- plug one in or use Scan Ports first"
        return [], "Multiple devices -- tick the one(s) to connect, then Connect"

    @staticmethod
    def _resolve_pterm_send_targets(
        checked: "list[str]", connected: "list[str]"
    ) -> "list[str]":
        """Ports a persistent-terminal Send should write to.

        Ticked-AND-connected ports win. If NONE are ticked, fall back to ALL connected ports — the
        SAME empty-selection fallback Connect/Disconnect already have. Without this, the owner's
        single-device flow broke: Connect (via :meth:`_resolve_pterm_connect_ports`) opens the sole
        board without ticking its checkbox, so Send saw an empty check-selection and refused every
        command with "check and connect first" even though the board was connected and streaming RX.
        Returns [] only when nothing is connected, or when the ticked ports are all disconnected — a
        genuine "connect first" case that still deserves the error."""
        connected_set = set(connected)
        if checked:
            return [p for p in checked if p in connected_set]
        return list(connected)

    def _pterm_on_connect(self) -> None:
        """Connect the persistent terminal to the checked ports (or the sole listed device when
        nothing is ticked — see :meth:`_resolve_pterm_connect_ports`)."""
        listed = [
            self._pterm_device_list.item(i).data(Qt.UserRole)
            for i in range(self._pterm_device_list.count())
        ]
        ports, msg = self._resolve_pterm_connect_ports(
            self._pterm_checked_ports(), [p for p in listed if p]
        )
        if msg is not None:
            self._pterm_output.append(f'<span style="color:#f85149;">[{msg}]</span>')
            return
        for port in ports:
            existing = self._pterm_conns.get(port)
            if existing is not None and getattr(existing, "is_connected", False):
                continue  # already connected and live
            self._pterm_conns.pop(port, None)  # stale/dead entry -> fall through and reopen
            try:
                # Honor the user-configured Default Baud Rate (Settings ▸ Serial) instead of falling back
                # to open_connection's hardcoded 115200 — otherwise a device that talks at a non-default
                # baud connects at the wrong speed and its TX/RX is garbled in the persistent terminal.
                from src.config.settings import load_settings
                baud = load_settings().get("serial", {}).get("default_baud", 115200)
                conn = self._dm.open_connection(port, baud=baud, owner="pterm")
                self._pterm_conns[port] = conn
                # Stamp a best-effort firmware so the Operate surface (Broadcast/Targets/STOP-ALL, which
                # all route by Device.firmware) works even when the board was connected here in the
                # terminal rather than the Devices tab. Fill a BLANK only — never clobber an explicit
                # Devices-tab choice. Mirror device_tab's autodetect: Flipper -> flipper, else marauder.
                dev = self._dm.get_device(port)
                if dev is not None and not getattr(dev, "firmware", ""):
                    from src.models.device import BoardType

                    fw = (
                        "flipper"
                        if getattr(dev, "board_type", None) == BoardType.FLIPPER_ZERO
                        else "marauder"
                    )
                    # Route through the central setter (not a direct dev.firmware write) so
                    # on_device_changed fires and the Broadcast panel repopulates reactively instead
                    # of waiting on its safety-net timer. forced=False: a best-effort autodetect
                    # stamp, not a manual force (re-autodetect may still refine it).
                    self._dm.set_firmware(port, fw)
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
        """Disconnect the persistent terminal from the checked ports (or all connected when nothing
        is ticked). Always gives feedback: with nothing connected the loop runs zero times, so we
        print an explicit no-op line instead of silence — the "Disconnect does nothing" half."""
        ports = self._pterm_checked_ports()
        if not ports:
            # If nothing checked, disconnect all
            ports = list(self._pterm_conns.keys())
        disconnected = 0
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
            disconnected += 1
            color = self._pterm_port_colors.get(port, "#8b949e")
            self._pterm_output.append(
                f'<span style="color:{color};">[{port}] Disconnected</span>'
            )
        if disconnected == 0:
            # Nothing was connected (or the checked ports weren't open) — never leave it silent.
            self._pterm_output.append(
                '<span style="color:#f85149;">[No connected devices to disconnect]</span>'
            )
        self._pterm_refresh_ports()
        self._refresh_sidebar_devices()

    def _pterm_on_send(self) -> None:
        """Send from the persistent terminal. A known local-tool command (aircrack-ng/hashcat/…) runs
        as a subprocess whose output streams back via the activity bus; ``stop`` kills a running tool;
        everything else goes to the checked+connected serial devices (the original behaviour)."""
        import os
        import shlex

        from src.core import tool_runner
        cmd = self._pterm_input.text().strip()
        if not cmd:
            return
        target = self._pterm_target.currentData() if hasattr(self, "_pterm_target") else "auto"
        # 'stop' kills a running local tool (checked before serial so it can't be swallowed as a device
        # cmd) — but NOT when the operator is explicitly targeting a device, where 'stop' is a firmware
        # command (e.g. stopscan) that must reach the board.
        proc = getattr(self, "_pterm_tool_proc", None)
        if target != "serial" and cmd.lower() == "stop" and proc is not None and proc.poll() is None:
            proc.terminate()
            self._pterm_output.append('<span style="color:#f0883e;">[tool] stopping…</span>')
            self._pterm_input.clear()
            return
        try:
            argv = shlex.split(cmd, posix=(os.name != "nt"))
        except ValueError:
            argv = cmd.split()
        route = tool_runner.route_terminal_send(target, argv[0] if argv else "")
        if route == "tool":
            self._pterm_run_tool(argv)
            self._pterm_input.clear()
            return
        if route == "no-tool":
            # Computer target chosen, but the first word isn't a bundled tool — refuse rather than leak
            # it to a device (the tool shell is scoped to the known crack tools, not a general OS shell).
            first = html.escape(argv[0] if argv else cmd)
            self._pterm_output.append(
                f'<span style="color:#f85149;">[{first} is not a bundled tool — the Computer target '
                f'runs only the crack tools (aircrack-ng/hashcat/…); switch to Device(s) to send it to '
                f'a board]</span>'
            )
            self._pterm_input.clear()
            return
        # Ticked-and-connected ports win; with nothing ticked, fall back to ALL connected ports so a
        # device connected via the no-tick Connect fallback can actually be sent to (mirrors the
        # Connect/Disconnect empty-selection fallback — see _resolve_pterm_send_targets).
        targets = self._resolve_pterm_send_targets(
            self._pterm_checked_ports(), list(self._pterm_conns.keys())
        )
        if not targets:
            self._pterm_output.append(
                '<span style="color:#f85149;">[No connected devices -- connect a device first]</span>'
            )
            return
        for port in targets:
            conn = self._pterm_conns[port]
            color = self._pterm_port_colors.get(port, "#58a6ff")
            try:
                # Per-device terminator: re-stamp from THIS port's persisted firmware right before writing.
                # The connection was built with LF at open time (open_connection seeds the terminator from
                # Device.firmware, still blank when the terminal opens the port), and the terminal can
                # broadcast one command to several ports of DIFFERENT firmwares — so a CR-only Flipper would
                # otherwise get an LF-terminated line its shell silently ignores. Mirrors device_tab._on_send.
                try:
                    from src.protocols import line_ending_for
                    _dev = self._dm.get_device(port)
                    _fw = (getattr(_dev, "firmware", "") or "").strip()
                    if _fw:
                        conn.line_ending = line_ending_for(_fw)
                except Exception:
                    pass
                conn.write(cmd)
                self._pterm_output.append(
                    f'<span style="color:{color};">[{port}] &gt; {cmd}</span>'
                )
            except Exception as exc:
                self._pterm_output.append(
                    f'<span style="color:#f85149;">[{port}] Send error: {exc}</span>'
                )
        self._pterm_input.clear()

    def _pterm_run_tool(self, argv: list) -> None:
        """Run a known local tool (aircrack-ng/hashcat/…) from the terminal, streaming its output via the
        activity bus (which auto-marshals the reader-thread lines onto the GUI thread). One at a time;
        ``stop`` kills it. Scoped to KNOWN tools by the caller — never an arbitrary OS command."""
        from src.core import tool_runner
        from src.core.activity_log import activity_log
        act = activity_log()
        proc = getattr(self, "_pterm_tool_proc", None)
        if proc is not None and proc.poll() is None:
            act.emit_line("tool", 'a tool is already running — type "stop" to kill it', "warn")
            return
        act.emit_line("tool", "$ " + " ".join(argv))
        self._pterm_tool_proc = tool_runner.run_tool(
            argv,
            on_line=lambda t: act.emit_line("tool", t),
            on_exit=lambda rc: act.emit_line("tool", f"[exit {rc}]", "success" if rc == 0 else "warn"))

    @staticmethod
    def _safe_serial_write(conn, data: str) -> None:
        """Write to a serial connection, swallowing a disconnect/control-char error. Used on the DMS
        auth path, which runs inside a modal event loop where a board unplugged mid-dialog would
        otherwise raise RuntimeError/ValueError uncaught out of a queued Qt slot (PyQt aborts with
        no excepthook). A failed auth write is non-fatal — the operator can retry."""
        try:
            conn.write(data)
        except Exception:  # noqa: BLE001 — a DMS auth write must never abort the serial slot
            log.exception("DMS auth serial write failed")

    def _wire_ble_analyzer(self) -> None:
        """Feed the BLE Analyzer tab from the ingestor's parsed-event stream. The observer fires on
        the serial reader thread, so it emits a Qt signal to marshal each ble_found event onto the
        GUI thread before the tab folds it in. No-op if the tab or ingestor is unavailable."""
        analyzer = getattr(self, "_ble_analyzer", None)
        ingestor = getattr(self, "_ingestor", None)
        if analyzer is None or ingestor is None:
            return
        from PyQt5.QtCore import QObject
        from PyQt5.QtCore import pyqtSignal as _sig

        class _BleEventSignal(QObject):
            ble_event = _sig(str, object)  # (port, event-data dict)

        self._ble_event_signal = _BleEventSignal()
        self._ble_event_signal.ble_event.connect(analyzer.on_ble_event)

        def _observer(ev, port):
            # Serial-thread callback: keep only BLE adverts; emit queues onto the GUI thread.
            if getattr(ev, "event_type", "") == "ble_found":
                self._ble_event_signal.ble_event.emit(port, getattr(ev, "data", {}) or {})

        self._ble_event_observer = _observer  # keep a strong ref so it isn't garbage-collected
        try:
            ingestor.add_event_observer(_observer)
        except Exception:  # noqa: BLE001 — analyzer wiring must never break app startup
            log.exception("BLE analyzer: failed to register ingestor observer")

    def _wire_wifi_analyzer(self) -> None:
        """Feed the Wi-Fi Analyzer tab from the ingestor's parsed-event stream. It fires on
        the serial reader thread, so it emits a Qt signal to marshal each Wi-Fi event onto the GUI
        thread before the tab folds it in. No-op if the tab or ingestor is unavailable."""
        analyzer = getattr(self, "_wifi_analyzer", None)
        ingestor = getattr(self, "_ingestor", None)
        if analyzer is None or ingestor is None:
            return
        from PyQt5.QtCore import QObject
        from PyQt5.QtCore import pyqtSignal as _sig

        class _WifiEventSignal(QObject):
            wifi_event = _sig(str, str, object)  # (port, event_type, event-data dict)

        self._wifi_event_signal = _WifiEventSignal()
        self._wifi_event_signal.wifi_event.connect(analyzer.on_wifi_event)

        # The Wi-Fi discovery + capture events the AP view folds in
        # (mirrors _event_to_target / _event_to_capture).
        _wifi_types = frozenset({
            "ap_found", "rogue_ap", "client_found", "handshake_captured", "pmkid_captured",
        })

        def _observer(ev, port):
            # Serial-thread callback: keep only Wi-Fi events; emit queues onto the GUI thread.
            et = getattr(ev, "event_type", "")
            if et in _wifi_types:
                self._wifi_event_signal.wifi_event.emit(port, et, getattr(ev, "data", {}) or {})

        self._wifi_event_observer = _observer  # keep a strong ref so it isn't garbage-collected
        try:
            ingestor.add_event_observer(_observer)
        except Exception:  # noqa: BLE001 — analyzer wiring must never break app startup
            log.exception("Wi-Fi analyzer: failed to register ingestor observer")

    @pyqtSlot(str, str)
    def _pterm_on_line(self, port: str, line: str) -> None:
        """Handle a serial line from a device in the persistent terminal."""
        conn = self._pterm_conns.get(port)
        if conn:
            # Dead Man's Switch auth detection — but only when the Devices tab is NOT co-owning this
            # port. When both panels hold the SAME shared SerialConnection, one received line fires BOTH
            # on_line callbacks, so running check_line here AND in DeviceTab._on_line_received would drive
            # _handle_auth twice for a single prompt: the modal password dialog spins a nested event loop,
            # so the second queued line stacks a second dialog on top and, on OK, writes the boot password
            # to the gate TWICE — which the DMS can read as a wrong/extra attempt and wipe/brick. Let the
            # Devices tab be the sole DMS owner for any port it has connected (its handler also marks
            # _dms_seen to suppress the connect probe); the terminal only handles ports it owns alone.
            if port not in getattr(self._device_tab, "_devtab_line_cbs", {}):
                # On a DMS prompt seen ONLY here (terminal is sole owner), mark the port in the SAME
                # shared _dms_seen the Devices tab uses, so any status-poll consumer (the Operate
                # console) also refuses to write into the unlock prompt — a stray write can burn a
                # DMS attempt and trip a wipe. Mirror DeviceTab's add-on-match. Guard the write too:
                # a board unplugged mid-modal would otherwise raise uncaught from this slot.
                if self._dms_auth.check_line(line, lambda pw: self._safe_serial_write(conn, pw)):
                    getattr(self._device_tab, "_dms_seen", set()).add(port)
        color = self._pterm_port_colors.get(port, "#3fb950")
        # Device serial bytes are untrusted: QTextEdit.append() renders rich text (the leading <span>
        # guarantees mightBeRichText), so escape the device line or a rogue board could forge markup
        # (e.g. a fake green [DMS] Authenticated banner) in the operator's terminal.
        self._pterm_output.append(
            f'<span style="color:{color};">[{port}]</span> {html.escape(line)}'
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
            self._device_tab._terminal.append(html.escape(line))
        # Nudge the Operate console to repaint from the (ingestor-updated) Device if it's showing this
        # port. It opens no serial subscription of its own — this is a read-only state repaint, and the
        # 2s poll covers the case where this forward doesn't fire. Guarded: never break the serial path.
        console = getattr(self, "_operate_console", None)
        if console is not None:
            try:
                console.on_line_received(port, line)
            except Exception:  # noqa: BLE001 — a view repaint must never break serial ingestion
                pass

    # Activity-bus level → source-tag colour (error/warn stand out; info/success stay calm).
    _ACTIVITY_COLORS = {"info": "#58a6ff", "success": "#3fb950", "warn": "#f0883e", "error": "#f85149"}

    @pyqtSlot(str, str, str)
    def _pterm_on_activity(self, source: str, level: str, text: str) -> None:
        """Render one line from the app-wide activity bus into the persistent terminal.

        Non-serial activity (flash/crack/broadcast/cmd/macro). The serial path (``_pterm_on_line``) is
        untouched. Untrusted tool/device text is ``html.escape``d exactly like the serial path — only the
        code-controlled colour span is trusted markup, so a crafted SSID or tool line can't forge the UI.
        """
        color = self._ACTIVITY_COLORS.get(level, "#8b949e")
        self._pterm_output.append(
            f'<span style="color:{color};">[{html.escape(source)}]</span> {html.escape(text)}'
        )

    # ── Capture-confirm correlator notices (punch-list #2 slice 5) ────
    @staticmethod
    def _capture_trigger(payload: dict) -> str:
        """Name the action that armed the window — 'deauth' for a Deauth AP, else the action itself.

        Not every chain-event action is a deauth (a Capture Handshake / Evil Portal action also arms
        a window), so the notice must not hardcode 'deauth' or it claims an attack that never fired.
        """
        action = str(payload.get("action") or "").strip()
        if "deauth" in action.lower():
            return "deauth"
        return action or "action"

    def _on_capture_confirmed(self, _topic: str, payload: dict) -> None:
        """An armed action was followed by a matching handshake in the window — surface it."""
        bssid = payload.get("bssid") or "?"
        trigger = self._capture_trigger(payload)
        verdict = "deauth confirmed" if trigger == "deauth" else "capture confirmed"
        elapsed = payload.get("elapsed_s")
        tail = f" ({elapsed:g}s after {trigger})" if isinstance(elapsed, (int, float)) else ""
        self._activity_log.emit_line(
            "capture", f"handshake captured from {bssid}{tail} — {verdict}", "success")

    def _on_capture_timeout(self, _topic: str, payload: dict) -> None:
        """An armed window passed with no matching handshake — report it (no false success)."""
        bssid = payload.get("bssid") or "?"
        trigger = self._capture_trigger(payload)
        win = payload.get("window_s")
        span = f"{win:g}s" if isinstance(win, (int, float)) else "the window"
        self._activity_log.emit_line(
            "capture", f"no handshake from {bssid} within {span} of the {trigger}", "warn")

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
        # `message` is the raw device line (deadman_auth passes line.strip()); escape it so a rogue board
        # can't inject markup into the auth-status banner it triggers.
        safe = html.escape(message)
        if success:
            self._pterm_output.append(
                f'<span style="color:#3fb950; font-weight:bold;">'
                f'[DMS] Authenticated: {safe}</span>'
            )
        else:
            self._pterm_output.append(
                f'<span style="color:#f85149; font-weight:bold;">'
                f'[DMS] Auth failed: {safe}</span>'
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
                # Block the list's selection signal for this programmatic re-selection. Otherwise
                # setCurrentItem fires currentItemChanged -> _on_sidebar_device_selected -> device_selected
                # -> _focus_device_in_devices_tab, which force-switches the main tabs to Connect > Devices.
                # This refresh runs on the 3s sidebar timer, so a selected device yanked the user back to
                # the Devices tab every 3 seconds — you could not stay on any other tab. Real user clicks on
                # the list still emit normally (they happen outside this refresh).
                self._sidebar_device_list.blockSignals(True)
                self._sidebar_device_list.setCurrentItem(item)
                self._sidebar_device_list.blockSignals(False)

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

        # Slice E: the Operate Home landing summary shares this refresh point, so its device count +
        # ARMED state stay live on the same cadence as the sidebar (poll + connect/disconnect).
        self._refresh_home_summary()
        # Fix: the app-shell status bar's device count + ARMED were only pushed once at construction
        # (its target/capture badges are live via bus events, but device status wasn't) — re-push it
        # here so the always-visible chrome doesn't go stale after a connect/disconnect.
        binder = getattr(self, "_app_shell_binder", None)
        if binder is not None:
            binder.refresh_devices()

    def _on_sidebar_device_selected(self, current: QListWidgetItem | None, _prev: QListWidgetItem | None) -> None:
        if current is None:
            return
        port = current.data(Qt.UserRole)
        if port:
            self.device_selected.emit(port)

    def _focus_device_in_devices_tab(self, port: str) -> None:
        """Sync a sidebar device selection to the Devices tab: select the matching device there (so the
        tab's active device — Send / Connect / terminal — follows the sidebar) and focus that tab. This
        is the subscriber for the device_selected signal; without it the sidebar selection drove nothing."""
        tab = getattr(self, "_device_tab", None)
        if tab is None or not port:
            return
        if tab.select_port(port):
            # Devices is re-homed into the Dashboard now, so focus the DEVICE ▸ Dashboard landing.
            self._show_subtab(self._rig_surface, self._device_dashboard)

    def _on_sidebar_scan(self) -> None:
        """Scan ports off the GUI thread, then register + refresh the sidebar when it reports back.

        comports() does blocking SetupAPI/registry I/O (seconds on a machine with many virtual COM ports);
        running it inline froze the event loop on every F5 / Scan-Ports press. The worker keeps the UI live
        and add_device()/refresh run back on the GUI thread via the queued ``done`` signal."""
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return  # a scan is already in flight — don't orphan its QThread
        self._scan_btn.setEnabled(False)
        self._scan_worker = _PortScanWorker(self._dm)
        self._scan_worker.done.connect(self._on_ports_scanned)
        self._scan_worker.finished.connect(lambda: setattr(self, "_scan_worker", None))
        self._scan_worker.start()

    def _on_ports_scanned(self, devices) -> None:
        for dev in devices:
            if not self._dm.get_device(dev.port):
                self._dm.add_device(dev)
        self._refresh_sidebar_devices()
        self._scan_btn.setEnabled(True)

    # ── Status bar ───────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        # The system-health summary + mode badge fold into the ONE shell status bar (the app-shell's top
        # bar), retiring the second, duplicate QMainWindow.statusBar() — transient action notices now go
        # through self.toast() -> the shell's toast slot, not a bottom bar. Falls back to the bottom bar
        # only if the shell somehow isn't built yet (defensive; _build_main_layout runs first).
        shell = getattr(self, "_app_shell", None)
        self._status_label = QLabel()

        # Clickable Interface-Mode badge (one-click recovery to Pro / quick switch to Simple).
        self._mode_badge = QLabel()
        self._mode_badge.setObjectName("mode_badge")
        self._mode_badge.setCursor(Qt.PointingHandCursor)
        self._mode_badge.setToolTip("Click (or Ctrl+M) to switch between Simple and Pro interface modes")
        self._mode_badge.mousePressEvent = lambda _ev: self._toggle_ui_mode()  # type: ignore[assignment]

        # Reform chrome (Atlas): the reformed top bar matches the approved mockup — brand · breadcrumb ·
        # ● SAFE lamp · Simple/Pro segment · ⤢/⚙. The old CPU/RAM/Devices/Targets summary + "Mode ▾" badge
        # are NOT folded into it: they cluttered the bar (owner: "just like the example") and duplicate the
        # Dashboard's own gauges + the segment + the shell's device slot. The labels stay alive (the
        # _refresh_status timer keeps _status_label current for any reader) but are not shown in the top bar.
        if shell is None:  # pragma: no cover - defensive fallback (no reformed shell, e.g. a bare test)
            self.statusBar().addPermanentWidget(self._status_label)
            self.statusBar().addPermanentWidget(self._mode_badge)

        self._refresh_status()
        # Paint the badge from the already-applied interface mode. _apply_ui_mode() ran during layout
        # build (before this status bar existed), so its _sync_mode_chrome() call skipped the badge
        # (guarded by hasattr(_mode_badge)) — sync it now that the badge is created, else it launches blank.
        self._sync_mode_chrome()

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

    def toast(self, message: str, level: str = "info", timeout: int = 4000) -> None:
        """Show a transient status notice in the shell's toast slot (the single status surface).

        The one entry point tabs use for fleeting "action ran / failed" messages — routes to
        ``PageLayout.toast`` so nothing has to reach for a second bottom status bar. A no-op if the
        shell isn't built (e.g. a tab hosted standalone in a test), matching the old graceful-degrade."""
        shell = getattr(self, "_app_shell", None)
        if shell is not None:
            shell.toast(message, level=level, timeout=timeout)

    # ── Command palette ─────────────────────────────────────────────

    def _build_command_palette(self) -> None:
        """Register all commands in the palette widget."""
        self._palette = CommandPalette(self)
        # Navigate by WIDGET, not a hardcoded index — immune to tab reordering (the old fixed indices
        # had drifted and pointed at the wrong tabs).
        # DEVICE sub-views (reform): Dashboard (landing) + Firmware + Software OS + Mesh. Devices +
        # Health are re-homed INTO the Dashboard, so their entries land on it; Nodes re-homes to Mesh.
        self._palette.add_command("View Dashboard", lambda: self._show_subtab(self._rig_surface, self._device_dashboard))
        self._palette.add_command("Flash Firmware", lambda: self._show_subtab(self._rig_surface, self._flash_tab))
        self._palette.add_command("Flash Software OS", lambda: self._show_subtab(self._rig_surface, self._software_tab))
        self._palette.add_command("Connect to Device", lambda: self._show_subtab(self._rig_surface, self._device_dashboard))
        self._palette.add_command("View Health", lambda: self._show_subtab(self._rig_surface, self._device_dashboard))
        self._palette.add_command("Record Macro", self._on_quick_start_macro)
        # OPERATE sub-views: Control (the QA-1 merged Broadcast + Console screen) + Macros. Both the
        # single-device and fan-out palette entries land on the one merged Control (self._operate_action).
        self._palette.add_command("Control Device", lambda: self._show_subtab(self._operate_surface, self._operate_action))
        self._palette.add_command("Broadcast Actions", lambda: self._show_subtab(self._operate_surface, self._operate_action))
        self._palette.add_command("View Macros", lambda: self._show_subtab(self._operate_surface, self._macro_tab))
        # HUNT sub-views: Targets discovery + the node Graph + the Wi-Fi/BLE analyzers (re-homed from Analyze).
        self._palette.add_command("View Targets", lambda: self._show_subtab(self._hunt_surface, self._targets_tab))
        self._palette.add_command("Network Graph", lambda: self._show_subtab(self._hunt_surface, self._network_tab))
        # MAP sub-view.
        self._palette.add_command("Wardrive", lambda: self._show_subtab(self._map_surface, self._wardrive_tab))
        # RIG sub-view: Cross-Comm routing is re-homed to RIG as "Mesh".
        self._palette.add_command("Cross-Comm Dashboard", lambda: self._show_subtab(self._rig_surface, self._cross_comm_tab))
        # CRACK sub-view: the offline Crack Lab.
        self._palette.add_command("Crack Lab", lambda: self._show_subtab(self._crack_surface, self._crack_lab_tab))
        if self._ble_analyzer is not None:
            self._palette.add_command("BLE Analyzer", lambda: self._show_subtab(self._hunt_surface, self._ble_analyzer))
        if self._wifi_analyzer is not None:
            self._palette.add_command("Wi-Fi Analyzer", lambda: self._show_subtab(self._hunt_surface, self._wifi_analyzer))
        self._palette.add_command("Open Settings", lambda: self._tabs.setCurrentWidget(self._settings_tab))
        # Slice C hides the tab-bar, so the bar's double-click/context-menu detach is gone — expose
        # detach here (+ the Ctrl+Shift+D shortcut) so popping a surface out stays discoverable.
        self._palette.add_command("Detach Current Tab", lambda: self._tabs.detach_current())
        self._palette.add_command("Dead Man's Switch Setup", self._on_suicide_setup)
        self._palette.add_command("Scan Ports", self._on_sidebar_scan)
        self._palette.add_command("Clear Terminal", self._on_clear_terminal)
        self._palette.add_command("Toggle Dead Man's Switch", self._on_toggle_suicide_mode)
        self._palette.add_command("User Guide", self._on_user_guide)
        self._palette.add_command("How-To", self._on_howto)
        self._palette.add_command("Terms of Service & Use", self._on_terms)
        self._palette.add_command("Keyboard Shortcuts", self._on_keyboard_shortcuts)
        self._palette.add_command("Check for Updates…", lambda: self.check_for_updates(force=True))
        self._palette.add_command("Quit", self.close)
        # Reform: the menu bar was removed; fold its ORPHAN actions here so nothing is lost. The
        # covered ones (guides/terms/updates/dead-man's-switch/quit) were already palette commands;
        # Flock is reachable via MAP > Flock Map.
        self._palette.add_command("Configure Loadout", self.configure_loadout)
        self._palette.add_command("Report a Bug…", self._on_report_bug)
        self._palette.add_command("About", self._on_about)
        self._palette.add_command("GitHub", self._on_github)
        self._palette.add_command("Increase Font Size", lambda: self._change_font_size(1))
        self._palette.add_command("Decrease Font Size", lambda: self._change_font_size(-1))

    def _on_command_palette(self) -> None:
        """Open the command palette dialog."""
        self._palette.open_palette()

    def _on_clear_terminal(self) -> None:
        """Clear the terminal output the user is actually looking at.

        This used to clear ONLY the Devices sub-tab's terminal, so the palette's "Clear Terminal"
        appeared to do nothing whenever the always-visible bottom panel (_pterm_output) was on screen —
        which is most of the time. Clear both; they mirror each other, so clearing one alone would also
        leave them out of sync.
        """
        pterm = getattr(self, "_pterm_output", None)
        if pterm is not None:
            pterm.clear()
        dev_term = getattr(self._device_tab, "_terminal", None)
        if dev_term is not None:
            dev_term.clear()

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

    def _on_report_bug(self) -> None:
        """Open Help ▸ Report a Bug with a little live context (connected-device count) attached."""
        from src.ui.qt.bug_report_dialog import BugReportDialog

        extra: dict = {}
        dm = getattr(self, "_dm", None) or getattr(self, "_device_manager", None)
        if dm is not None:
            try:
                extra["connected_devices"] = len(dm.list_connected())
            except Exception:  # noqa: BLE001
                pass
        BugReportDialog(self, extra=extra).exec_()

    def _on_howto(self) -> None:
        """Open the in-app How-To guide (renders docs/HOWTO.md) in a dialog. Lives under Help rather than a
        top-level tab so the strip stays at the 6 working surfaces (Flash/Connect/Operate/Survey/Analyze/
        Settings) + Help — the same "help content in a dialog" pattern as _on_user_guide."""
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

    def _on_terms(self) -> None:
        """Open Help ▸ Terms — the canonical Terms of Service & Use (src/core/legal.py) in a scroll dialog.
        Legal copy lives in one place so the dialog and the tests can't drift."""
        from src.core import legal
        dlg = QDialog(self)
        dlg.setWindowTitle(f"{legal.APP_NAME} — Terms of Service & Use")
        dlg.setMinimumSize(760, 620)
        dlg.setStyleSheet("QDialog { background-color: #0d1117; color: #e6edf3; }")
        layout = QVBoxLayout(dlg)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setStyleSheet("QTextEdit { background-color: #161b22; color: #e6edf3; "
                           "border: 1px solid #30363d; border-radius: 4px; padding: 12px; font-size: 10pt; }")
        md = legal.terms_markdown()
        try:
            view.setMarkdown(md)          # Qt 5.14+ renders the headings/lists
        except (AttributeError, TypeError):
            view.setPlainText(md)         # older Qt — still fully readable as text
        layout.addWidget(view, 1)
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
            "Layout": (
                "<h2 style='color:#a371f7;'>Layout</h2>"
                "<p>The top nav is five job surfaces, read left to right as the mission arc, plus a pinned "
                "<b>Settings</b>:</p>"
                "<ul>"
                "<li><b>RIG</b> &mdash; get a rig ready: Devices, Health, Nodes, Firmware, Software OS, Mesh.</li>"
                "<li><b>HUNT</b> &mdash; see what's out there, passively: the Wi-Fi and BLE analyzers, "
                "Targets, the node Graph.</li>"
                "<li><b>OPERATE</b> &mdash; the action surface: the Home launcher, the Control console, Macros.</li>"
                "<li><b>CRACK</b> &mdash; capture to key: the offline Crack Lab.</li>"
                "<li><b>MAP</b> &mdash; one map canvas: Wardrive, Multi-Wardrive, the Flock map.</li>"
                "</ul>"
                "<p>Each surface holds its tools as sub-views. Press <b>Ctrl+Shift+P</b> for the command "
                "palette to jump straight to any of them.</p>"
            ),
            "Flash": (
                "<h2 style='color:#a371f7;'>Flash Firmware</h2>"
                "<p>Firmware flashing lives under <b>RIG &rarr; Firmware</b> &mdash; write firmware to "
                "connected ESP32 and similar devices.</p>"
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
                "<p><b>RIG &rarr; Devices</b> provides a serial terminal for real-time device communication.</p>"
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
                "<p><b>RIG &rarr; Health</b> displays real-time metrics for your system and connected devices.</p>"
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
                "<p>Device health (when supported) shows per-device firmware, uptime, signal "
                "strength, and last-seen time.</p>"
            ),
            "Targets": (
                "<h2 style='color:#a371f7;'>Targets</h2>"
                "<p><b>HUNT &rarr; Targets</b> shows discovered Wi-Fi access points and clients from scanning "
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
                "<li><b>Flash baud rate</b> &mdash; Baud rate used when flashing firmware.</li>"
                "<li><b>Updates</b> &mdash; Automatically check GitHub for new releases.</li>"
                "<li><b>Safety &amp; disclaimers</b> &mdash; Confirm dangerous commands and "
                "suppress repeat warnings.</li>"
                "<li><b>Access Gate</b> &mdash; Passphrase-lock the app and its saved data.</li>"
                "<li><b>Secure Container</b> &mdash; Encrypt your saved macros at rest.</li>"
                "<li><b>Firmware vault path</b> &mdash; Location of the offline firmware cache.</li>"
                "</ul>"
                "<p>Settings are persisted across sessions.</p>"
            ),
        }

        for tab_name, html_body in guide_content.items():
            text_edit = QTextEdit()
            text_edit.setReadOnly(True)
            text_edit.setHtml(html_body)
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
        """Swap the verified binary in and restart. On Windows this spawns a detached helper that waits
        for THIS process to exit, then replaces the (now-unlocked) exe and relaunches it — so we must
        exit promptly and unconditionally. On Unix apply() re-execs and never returns."""
        from src.core import self_update
        try:
            self_update.apply(self_update.current_exe(), staged, self_update.platform_key())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Update failed", f"Could not apply the update:\n{exc}")
            return
        # Reached on Windows only (Unix apply() re-execs and never returns). The detached swap helper
        # is now waiting for this PID to disappear before it can replace the locked binary + relaunch.
        # A graceful QApplication.quit() can stall indefinitely on live background threads (the health
        # monitor, serial readers, the embedded web server) — the helper would then wait forever and
        # the update would never apply (the "came back on the old version" report). The verified update
        # is already staged on disk, so nothing here needs normal teardown: force an immediate exit so
        # the helper's wait-loop falls through NOW.
        import os
        os._exit(0)

    def _on_flock_heatmap(self) -> None:
        """Focus the Flock Map tab (FL F5) — located ALPR-camera detections from a scan's GeoJSON.

        The map used to open as a standalone Tools window; since the verb-IA rewire it's a sub-view of MAP,
        so this menu / palette action just navigates to it."""
        self._show_subtab(self._map_surface, self._flock_heatmap)

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
        method as a send callback (e.g. the Graph view)."""
        self._hub.send_to_port(port, command)

    # ── Cleanup ──────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Stop the timers, join every in-flight background QThread, and stop the health-monitor
        thread — the worker-teardown half of closeEvent, exposed idempotently (no UI-state
        persistence) so a built-but-never-closed window is REAPABLE. conftest.reap_qt_workers()
        calls it after every test, so a window's child QThreads (e.g. FlashTab's construction-time
        _VariantLoader) and its HealthMonitor thread can't accumulate across tests and later crash
        a teardown processEvents() on a dangling cross-thread queued signal (the intermittent
        SIGSEGV). closeEvent calls this first, then persists UI state + releases connections."""
        self._timer.stop()
        self._sidebar_timer.stop()
        # Join every in-flight background QThread. Without this, a QThread C++ dtor firing while its
        # thread still runs aborts the process ('QThread: Destroyed while thread is still running'),
        # and a leaked worker's queued cross-thread signal can crash a later processEvents() on the
        # now-freed target. FlashTab owns its own worker set (variant loader / flash / detect /
        # vault / backup-erase); a tab is a child widget with no closeEvent of its own — join it
        # through its shutdown().
        ft = getattr(self, "_flash_tab", None)
        if ft is not None:
            try:
                ft.shutdown()
            except Exception:  # noqa: BLE001 — teardown must never raise
                pass
        # SoftwareTab (OS-flash / resolve) and FlockHeatmapTab (live scan) own their own unparented worker
        # QThreads too, and don't route through the DeviceManager — join them the same way so nothing is
        # destroyed mid-run (a cut-off USB write / leaked serial ports) on exit. The Wardrive tabs DO route
        # through the DeviceManager, but dm.shutdown() only force-closes ports — it never sends the firmware
        # STOP verb, so join their shutdown() here too or an ESP32 is left scanning after the GUI is gone.
        for _tab_attr in ("_software_tab", "_flock_heatmap", "_wardrive_tab", "_wardrive_multi_tab",
                          "_crack_lab_tab", "_device_tab"):
            _tab = getattr(self, _tab_attr, None)
            _shutdown = getattr(_tab, "shutdown", None)
            if callable(_shutdown):
                try:
                    _shutdown()
                except Exception:  # noqa: BLE001 — teardown must never raise
                    pass
        # The update / self-update check threads (started on launch + on manual check) and the sidebar
        # port-scan worker are held only on self, so join them here too. The update workers do a network op
        # with a 6s socket timeout (updater.DEFAULT_TIMEOUT) plus unbounded DNS, so wait LONGER than that —
        # a 3s wait would return while the worker is still blocked, and destroying its QThread on GC then
        # aborts the process ('QThread: Destroyed while thread is still running'). comports() (the scan
        # worker) is fast, so 3s is plenty there. Any worker still blocked after the wait is parked in a
        # module keep-alive set so its wrapper isn't GC'd mid-run.
        from src.core import updater as _updater
        _update_wait = int(_updater.DEFAULT_TIMEOUT * 1000) + 1000  # > the 6s socket timeout
        for attr, wait_ms in (("_update_worker", _update_wait), ("_self_update_worker", _update_wait),
                              ("_scan_worker", 3000)):
            w = getattr(self, attr, None)
            if w is None:
                continue
            try:
                if w.isRunning() and not w.wait(wait_ms) and w.isRunning():
                    _KEEPALIVE_WORKERS.add(w)  # still blocked (black-hole net) — don't let GC destroy it
            except RuntimeError:  # C++ side already gone
                pass
        # Stop the HealthMonitor poll thread (a daemon threading.Thread). A built-but-never-closed
        # window would otherwise leave it running, accumulating one per window across a suite.
        try:
            self._health.stop()
        except Exception:  # noqa: BLE001 — teardown must never raise
            pass

    def closeEvent(self, event) -> None:
        # Join every background QThread + stop the health monitor before tearing down connections
        # (see shutdown() for why the ordering + the worker parking matter).
        self.shutdown()
        # Save splitter state
        self._qsettings.setValue("main_splitter_state", self._main_splitter.saveState())
        # Remember which tabs were popped out (+ their window geometry), then re-dock them so no
        # orphan windows linger after the main window closes.
        try:
            self._qsettings.setValue("detached_tabs", self._tabs.detached_state())
            self._tabs.close_all_popouts()
        except Exception:  # noqa: BLE001
            pass
        # Kill a terminal-launched tool subprocess (aircrack/hashcat/…) so it doesn't outlive the app.
        _tproc = getattr(self, "_pterm_tool_proc", None)
        if _tproc is not None and _tproc.poll() is None:
            try:
                _tproc.terminate()
            except Exception:  # noqa: BLE001
                pass
        # Disconnect all persistent terminal connections
        for port in list(self._pterm_conns.keys()):
            try:
                self._dm.close_connection(port)
            except Exception:
                pass
        self._pterm_conns.clear()
        self._dm.shutdown()   # workers + health monitor already stopped by shutdown()
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
        # If a prior self-update downloaded + verified a new binary but the swap couldn't replace the
        # running exe (app installed under a non-writable dir like Program Files, or an AV lock that
        # outlasted the retry window), the helper left a breadcrumb. Surface it once — otherwise the
        # app silently comes back on the OLD build with no explanation — then clear it. Frozen only.
        try:
            from src.core import self_update as _su
            if _su.is_frozen():
                _failmsg = _su.read_failed_update()
                if _failmsg:
                    QMessageBox.warning(
                        win, "Update didn't finish",
                        "Cyber Controller downloaded and verified the update, but couldn't replace the "
                        "running app:\n\n"
                        f"{_failmsg}\n\n"
                        "This usually means the app is in a folder that needs administrator rights "
                        "(such as Program Files). Move Cyber Controller to a writable location (your "
                        "user folder or Desktop) and update again, or reinstall from the release page.")
                    _su.clear_failed_update()
        except Exception:
            log.debug("failed-update breadcrumb surfacing failed", exc_info=True)

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
