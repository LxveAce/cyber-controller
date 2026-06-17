"""Tkinter lightweight UI — reduced feature set for resource-constrained environments.

Provides flash, device management, target viewing, health metrics, settings,
macro recording, and cross-device communication without the topology graph,
attack chain builder, or mission planner found in the full Qt interface.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Any

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine, FirmwareProfile
from src.core.serial_handler import SerialConnection

try:
    from src.core.cross_comm import AutoRouter
    _HAS_AUTO_ROUTER = True
except ImportError:
    _HAS_AUTO_ROUTER = False

try:
    from src.core.macro_recorder import MacroRecorder, Macro
    _HAS_MACROS = True
except ImportError:
    _HAS_MACROS = False

try:
    from src.config.settings import load_settings, save_settings, DEFAULTS as _SETTINGS_DEFAULTS
    _HAS_SETTINGS = True
except ImportError:
    _HAS_SETTINGS = False

try:
    from src.core.suicide_setup import SuicideConfig, run_cli as sm_run_cli
    _HAS_DEADMAN = True
except ImportError:
    _HAS_DEADMAN = False

if TYPE_CHECKING:
    from src.models.device import Device
    from src.models.target import Target

log = logging.getLogger(__name__)

_VERSION = "0.1.0"
_PROFILES_DIR = Path(__file__).resolve().parents[3] / "src" / "config" / "profiles"

# ── Dark theme colours ──────────────────────────────────────────────

_BG = "#0d1117"
_BG_LIGHT = "#161b22"
_BG_CARD = "#1c2128"
_FG = "#e6edf3"
_ACCENT = "#39ff14"
_BORDER = "#30363d"
_INPUT_BG = "#2d333b"
_WARNING = "#f0883e"
_ERROR = "#f85149"
_TERM_BG = "#0d1117"
_TERM_FG = "#39ff14"


def _apply_dark_theme(style: ttk.Style) -> None:
    """Configure a dark ttk theme."""
    style.theme_use("clam")

    style.configure(".", background=_BG, foreground=_FG, fieldbackground=_INPUT_BG,
                     borderwidth=1, focusthickness=0, bordercolor=_BORDER)
    style.configure("TNotebook", background=_BG, borderwidth=0, tabmargins=[0, 0, 0, 0])
    style.configure("TNotebook.Tab", background=_BG_LIGHT, foreground=_FG,
                     padding=[14, 5], borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", _BG), ("!selected", _BG_LIGHT)],
              foreground=[("selected", _ACCENT), ("!selected", _FG)],
              bordercolor=[("selected", _ACCENT)])

    style.configure("TFrame", background=_BG)
    style.configure("TLabel", background=_BG, foreground=_FG)
    style.configure("TButton", background=_BG_LIGHT, foreground=_FG, padding=[10, 5],
                     bordercolor=_BORDER, relief="flat")
    style.map("TButton",
              background=[("active", _INPUT_BG), ("pressed", _BG_CARD)],
              bordercolor=[("focus", _ACCENT)])

    style.configure("TEntry", fieldbackground=_INPUT_BG, foreground=_FG,
                     bordercolor=_BORDER, insertcolor=_ACCENT)
    style.map("TEntry", bordercolor=[("focus", _ACCENT)])

    style.configure("TCombobox", fieldbackground=_INPUT_BG, foreground=_FG,
                     background=_BG_LIGHT, selectbackground=_ACCENT,
                     selectforeground="#000000", bordercolor=_BORDER)
    style.map("TCombobox", fieldbackground=[("readonly", _INPUT_BG)],
              bordercolor=[("focus", _ACCENT)])

    style.configure("TProgressbar", background=_ACCENT, troughcolor=_BG_LIGHT,
                     borderwidth=0, thickness=20, bordercolor=_BORDER)

    style.configure("Treeview", background=_BG_LIGHT, foreground=_FG,
                     fieldbackground=_BG_LIGHT, borderwidth=0, rowheight=26)
    style.configure("Treeview.Heading", background=_BG, foreground=_ACCENT,
                     borderwidth=1, relief="flat", bordercolor=_BORDER)
    style.map("Treeview",
              background=[("selected", _INPUT_BG)],
              foreground=[("selected", _ACCENT)])

    style.configure("TLabelframe", background=_BG, foreground=_ACCENT,
                     bordercolor=_BORDER)
    style.configure("TLabelframe.Label", background=_BG, foreground=_ACCENT)

    style.configure("TPanedwindow", background=_BG)
    style.configure("Sash", sashthickness=4, background=_BORDER)

    style.configure("TScrollbar", background=_BG_LIGHT, troughcolor=_BG,
                     bordercolor=_BORDER, arrowcolor=_FG)
    style.map("TScrollbar", background=[("active", _INPUT_BG)])

    style.configure("Flash.TButton", background=_ACCENT, foreground="#000000",
                     font=("Segoe UI", 10, "bold"), borderwidth=0)
    style.map("Flash.TButton",
              background=[("active", "#2de00f"), ("pressed", "#1fc00a")])

    style.configure("Status.TLabel", background=_BG_LIGHT, foreground="#8b949e",
                     padding=[8, 3], bordercolor=_BORDER)


class TkLightApp:
    """Lightweight Tkinter interface for Cyber Controller.

    Seven tabs: Flash, Devices, Targets, Health, Settings, Macros, Cross-Comm.
    No topology graph, attack chain builder, or mission planner.
    """

    def __init__(
        self,
        device_manager: DeviceManager,
        flash_engine: FlashEngine,
        event_bus: EventBus,
        target_pool: TargetPool,
    ) -> None:
        self._dm = device_manager
        self._fe = flash_engine
        self._bus = event_bus
        self._pool = target_pool

        self._root = tk.Tk()
        self._root.title("Cyber Controller Lite")
        self._root.geometry("1000x600")
        self._root.minsize(800, 500)
        self._root.configure(bg=_BG)

        self._style = ttk.Style(self._root)
        _apply_dark_theme(self._style)

        self._profiles: dict[str, Path] = {}
        self._active_conn: SerialConnection | None = None
        self._active_port: str = ""

        # Macro recorder (optional backend)
        self._macro_recorder: MacroRecorder | None = None
        if _HAS_MACROS:
            try:
                self._macro_recorder = MacroRecorder()
            except Exception:
                log.warning("MacroRecorder init failed; macros tab will be read-only")

        self._build_menu()
        self._build_notebook()
        self._build_status_bar()

        self._load_profiles()
        self._refresh_ports()
        self._start_periodic_refresh()

        # Wire event bus for target updates
        self._bus.subscribe("target.added", self._on_target_event)
        self._bus.subscribe("target.updated", self._on_target_event)

    # ── Menu ────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        menubar = tk.Menu(self._root, bg=_BG_LIGHT, fg=_FG,
                          activebackground=_ACCENT, activeforeground="#000",
                          borderwidth=0)

        file_menu = tk.Menu(menubar, tearoff=0, bg=_BG_LIGHT, fg=_FG,
                            activebackground=_ACCENT, activeforeground="#000")
        file_menu.add_command(label="Quit", command=self._root.destroy,
                              accelerator="Ctrl+Q")
        menubar.add_cascade(label="File", menu=file_menu)

        tools_menu = tk.Menu(menubar, tearoff=0, bg=_BG_LIGHT, fg=_FG,
                             activebackground=_ACCENT, activeforeground="#000")
        tools_menu.add_command(label="Dead Man's Switch Setup",
                               command=self._launch_deadman_setup)
        menubar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=0, bg=_BG_LIGHT, fg=_FG,
                            activebackground=_ACCENT, activeforeground="#000")
        help_menu.add_command(label="Keyboard Shortcuts",
                              command=self._show_keyboard_shortcuts)
        help_menu.add_separator()
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self._root.config(menu=menubar)
        self._root.bind_all("<Control-q>", lambda _e: self._root.destroy())

    # ── Notebook ────────────────────────────────────────────────────

    def _build_notebook(self) -> None:
        self._notebook = ttk.Notebook(self._root)
        self._notebook.pack(fill=tk.BOTH, expand=True, padx=4, pady=(4, 0))

        self._build_flash_tab()
        self._build_devices_tab()
        self._build_targets_tab()
        self._build_health_tab()
        self._build_settings_tab()
        self._build_macros_tab()
        self._build_crosscomm_tab()

    # ── Flash Tab ───────────────────────────────────────────────────

    def _build_flash_tab(self) -> None:
        tab = ttk.Frame(self._notebook)
        self._notebook.add(tab, text="  Flash  ")

        # Top controls
        top = ttk.Frame(tab)
        top.pack(fill=tk.X, padx=8, pady=8)

        # Port selector
        port_frame = ttk.LabelFrame(top, text="Port")
        port_frame.pack(side=tk.LEFT, padx=(0, 8), fill=tk.X, expand=True)
        self._flash_port = ttk.Combobox(port_frame, state="readonly", width=30)
        self._flash_port.pack(padx=6, pady=6, fill=tk.X)

        # Profile selector
        prof_frame = ttk.LabelFrame(top, text="Firmware Profile")
        prof_frame.pack(side=tk.LEFT, padx=(0, 8), fill=tk.X, expand=True)
        self._flash_profile = ttk.Combobox(prof_frame, state="readonly", width=30)
        self._flash_profile.pack(padx=6, pady=6, fill=tk.X)

        # Flash button
        btn_frame = ttk.Frame(top)
        btn_frame.pack(side=tk.LEFT, padx=(0, 0))
        self._btn_flash = ttk.Button(btn_frame, text="Flash",
                                     style="Flash.TButton",
                                     command=self._on_flash)
        self._btn_flash.pack(padx=6, pady=6, ipadx=16, ipady=4)

        btn_refresh = ttk.Button(btn_frame, text="Refresh Ports",
                                 command=self._refresh_ports)
        btn_refresh.pack(padx=6, pady=(0, 6))

        # Dead Man's Switch toggle
        dms_frame = ttk.Frame(tab)
        dms_frame.pack(fill=tk.X, padx=8, pady=(0, 4))

        dms_inner = tk.Frame(dms_frame, bg=_BG, highlightbackground=_BORDER,
                             highlightcolor=_BORDER, highlightthickness=1)
        dms_inner.pack(fill=tk.X)

        self._deadman_enabled = tk.BooleanVar(value=False)
        self._deadman_enabled.trace_add("write", self._on_deadman_toggle)

        self._dms_check = tk.Checkbutton(
            dms_inner, text="Enable Dead Man's Switch",
            variable=self._deadman_enabled,
            bg=_BG, fg=_FG, selectcolor=_INPUT_BG,
            activebackground=_BG, activeforeground=_WARNING,
            font=("Segoe UI", 10, "bold"))
        self._dms_check.pack(side=tk.LEFT, padx=(8, 0), pady=(6, 2))

        self._dms_desc = ttk.Label(
            dms_inner,
            text="Integrates anti-forensic wipe into flash. Opens setup before flashing.",
            foreground="#8b949e", font=("Segoe UI", 9))
        self._dms_desc.pack(anchor=tk.W, padx=(28, 8), pady=(0, 6))

        self._dms_border_frame = dms_inner  # reference for border color toggling

        # Progress bar
        self._flash_progress = ttk.Progressbar(tab, mode="determinate",
                                                maximum=100, value=0)
        self._flash_progress.pack(fill=tk.X, padx=8, pady=(0, 4))

        # Log text area
        log_frame = ttk.LabelFrame(tab, text="Flash Log")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self._flash_log = tk.Text(log_frame, bg=_TERM_BG, fg=_TERM_FG,
                                  font=("Consolas", 10), insertbackground=_ACCENT,
                                  relief=tk.FLAT, state=tk.DISABLED, wrap=tk.WORD,
                                  highlightbackground=_BORDER, highlightcolor=_ACCENT,
                                  highlightthickness=1)
        flash_scroll = ttk.Scrollbar(log_frame, command=self._flash_log.yview)
        self._flash_log.configure(yscrollcommand=flash_scroll.set)
        flash_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._flash_log.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

    # ── Devices Tab ─────────────────────────────────────────────────

    def _build_devices_tab(self) -> None:
        tab = ttk.Frame(self._notebook)
        self._notebook.add(tab, text="  Devices  ")

        paned = ttk.PanedWindow(tab, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # Left: device listbox
        left = ttk.Frame(paned)
        paned.add(left, weight=1)

        ttk.Label(left, text="Devices", font=("Segoe UI", 11, "bold")).pack(
            anchor=tk.W, padx=6, pady=(6, 2))

        self._device_listbox = tk.Listbox(left, bg=_BG_LIGHT, fg=_FG,
                                          selectbackground=_ACCENT,
                                          selectforeground="#000",
                                          font=("Consolas", 10),
                                          relief=tk.FLAT, borderwidth=0,
                                          highlightbackground=_BORDER,
                                          highlightcolor=_ACCENT,
                                          highlightthickness=1)
        self._device_listbox.pack(fill=tk.BOTH, expand=True, padx=6, pady=2)
        self._device_listbox.bind("<<ListboxSelect>>", self._on_device_select)

        btn_row = ttk.Frame(left)
        btn_row.pack(fill=tk.X, padx=6, pady=4)
        self._btn_dev_connect = ttk.Button(btn_row, text="Connect",
                                           command=self._on_dev_connect)
        self._btn_dev_connect.pack(side=tk.LEFT, padx=(0, 4))
        self._btn_dev_disconnect = ttk.Button(btn_row, text="Disconnect",
                                              command=self._on_dev_disconnect,
                                              state=tk.DISABLED)
        self._btn_dev_disconnect.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_row, text="Scan", command=self._scan_devices).pack(side=tk.LEFT)

        # Right: serial output + command entry
        right = ttk.Frame(paned)
        paned.add(right, weight=3)

        self._dev_term_label = ttk.Label(right, text="Serial Output",
                                         font=("Segoe UI", 11, "bold"))
        self._dev_term_label.pack(anchor=tk.W, padx=6, pady=(6, 2))

        self._serial_output = tk.Text(right, bg=_TERM_BG, fg=_TERM_FG,
                                      font=("Consolas", 10), insertbackground=_ACCENT,
                                      relief=tk.FLAT, state=tk.DISABLED, wrap=tk.WORD,
                                      highlightbackground=_BORDER,
                                      highlightcolor=_ACCENT,
                                      highlightthickness=1)
        serial_scroll = ttk.Scrollbar(right, command=self._serial_output.yview)
        self._serial_output.configure(yscrollcommand=serial_scroll.set)
        serial_scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 6), pady=(0, 4))
        self._serial_output.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 4))

        cmd_frame = ttk.Frame(right)
        cmd_frame.pack(fill=tk.X, padx=6, pady=(0, 6))
        self._cmd_entry = tk.Entry(cmd_frame, font=("Consolas", 10),
                                   bg=_INPUT_BG, fg=_FG, insertbackground=_ACCENT,
                                   relief=tk.FLAT, highlightbackground=_BORDER,
                                   highlightcolor=_ACCENT, highlightthickness=1)
        self._cmd_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self._cmd_entry.bind("<Return>", lambda _e: self._on_send_cmd())
        self._btn_send = ttk.Button(cmd_frame, text="Send",
                                    command=self._on_send_cmd, state=tk.DISABLED)
        self._btn_send.pack(side=tk.LEFT)

    # ── Targets Tab ─────────────────────────────────────────────────

    def _build_targets_tab(self) -> None:
        tab = ttk.Frame(self._notebook)
        self._notebook.add(tab, text="  Targets  ")

        # Toolbar
        toolbar = ttk.Frame(tab)
        toolbar.pack(fill=tk.X, padx=8, pady=6)
        ttk.Label(toolbar, text="Discovered Targets",
                  font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Refresh",
                   command=self._refresh_targets).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text="Clear All",
                   command=self._clear_targets).pack(side=tk.RIGHT, padx=(0, 4))

        # Treeview
        columns = ("mac", "ssid", "rssi", "channel", "source", "type")
        self._target_tree = ttk.Treeview(tab, columns=columns, show="headings",
                                         selectmode="browse")
        self._target_tree.heading("mac", text="MAC")
        self._target_tree.heading("ssid", text="SSID")
        self._target_tree.heading("rssi", text="RSSI")
        self._target_tree.heading("channel", text="Channel")
        self._target_tree.heading("source", text="Source Device")
        self._target_tree.heading("type", text="Type")

        self._target_tree.column("mac", width=140, minwidth=100)
        self._target_tree.column("ssid", width=160, minwidth=80)
        self._target_tree.column("rssi", width=60, minwidth=40, anchor=tk.CENTER)
        self._target_tree.column("channel", width=60, minwidth=40, anchor=tk.CENTER)
        self._target_tree.column("source", width=120, minwidth=80)
        self._target_tree.column("type", width=70, minwidth=50, anchor=tk.CENTER)

        # Alternating row colours + RSSI colour tags
        self._target_tree.tag_configure("oddrow", background=_BG_LIGHT)
        self._target_tree.tag_configure("evenrow", background=_BG_CARD)
        self._target_tree.tag_configure("rssi_good", foreground=_ACCENT)
        self._target_tree.tag_configure("rssi_mid", foreground=_WARNING)
        self._target_tree.tag_configure("rssi_bad", foreground=_ERROR)

        tree_scroll = ttk.Scrollbar(tab, command=self._target_tree.yview)
        self._target_tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=(0, 8))
        self._target_tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

    # ── Health Tab ──────────────────────────────────────────────────

    def _build_health_tab(self) -> None:
        tab = ttk.Frame(self._notebook)
        self._notebook.add(tab, text="  Health  ")

        header = ttk.Label(tab, text="System Health",
                           font=("Segoe UI", 13, "bold"))
        header.pack(anchor=tk.W, padx=12, pady=(10, 4))

        # Card frame for metrics
        card = ttk.LabelFrame(tab, text="System Metrics")
        card.pack(fill=tk.X, padx=12, pady=(0, 8))

        # CPU row
        cpu_row = ttk.Frame(card)
        cpu_row.pack(fill=tk.X, padx=10, pady=(8, 2))
        self._health_cpu_label = tk.Label(
            cpu_row, text="CPU:  0%", font=("Consolas", 11),
            bg=_BG, fg=_ACCENT, anchor=tk.W)
        self._health_cpu_label.pack(side=tk.LEFT, padx=(0, 10))
        self._health_cpu_bar = ttk.Progressbar(
            cpu_row, mode="determinate", maximum=100, value=0)
        self._health_cpu_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # RAM row
        ram_row = ttk.Frame(card)
        ram_row.pack(fill=tk.X, padx=10, pady=2)
        self._health_ram_label = tk.Label(
            ram_row, text="RAM:  0%", font=("Consolas", 11),
            bg=_BG, fg=_ACCENT, anchor=tk.W)
        self._health_ram_label.pack(side=tk.LEFT, padx=(0, 10))
        self._health_ram_bar = ttk.Progressbar(
            ram_row, mode="determinate", maximum=100, value=0)
        self._health_ram_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Disk row
        disk_row = ttk.Frame(card)
        disk_row.pack(fill=tk.X, padx=10, pady=(2, 8))
        self._health_disk_label = tk.Label(
            disk_row, text="Disk: 0%", font=("Consolas", 11),
            bg=_BG, fg=_ACCENT, anchor=tk.W)
        self._health_disk_label.pack(side=tk.LEFT, padx=(0, 10))
        self._health_disk_bar = ttk.Progressbar(
            disk_row, mode="determinate", maximum=100, value=0)
        self._health_disk_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)

        if not _HAS_PSUTIL:
            notice = ttk.Label(tab, text="psutil not installed -- metrics unavailable",
                               foreground=_WARNING, font=("Segoe UI", 10))
            notice.pack(padx=12, pady=4)

        # Start auto-refresh
        self._refresh_health_metrics()

    def _health_color(self, pct: float) -> str:
        """Return a color based on percentage thresholds."""
        if pct < 60:
            return _ACCENT
        if pct < 80:
            return _WARNING
        return _ERROR

    def _refresh_health_metrics(self) -> None:
        """Update health metrics every 5 seconds."""
        if _HAS_PSUTIL:
            try:
                cpu = psutil.cpu_percent(interval=None)
                ram = psutil.virtual_memory().percent
                disk = psutil.disk_usage("/").percent
            except Exception:
                cpu = ram = disk = 0.0

            self._health_cpu_bar["value"] = cpu
            self._health_cpu_label.configure(
                text=f"CPU:  {cpu:.0f}%", fg=self._health_color(cpu))

            self._health_ram_bar["value"] = ram
            self._health_ram_label.configure(
                text=f"RAM:  {ram:.0f}%", fg=self._health_color(ram))

            self._health_disk_bar["value"] = disk
            self._health_disk_label.configure(
                text=f"Disk: {disk:.0f}%", fg=self._health_color(disk))

        self._root.after(5000, self._refresh_health_metrics)

    # ── Settings Tab ───────────────────────────────────────────────

    def _build_settings_tab(self) -> None:
        tab = ttk.Frame(self._notebook)
        self._notebook.add(tab, text="  Settings  ")

        header = ttk.Label(tab, text="Settings",
                           font=("Segoe UI", 13, "bold"))
        header.pack(anchor=tk.W, padx=12, pady=(10, 4))

        # Serial settings card
        serial_card = ttk.LabelFrame(tab, text="Serial")
        serial_card.pack(fill=tk.X, padx=12, pady=(0, 8))

        # Port selection
        port_row = ttk.Frame(serial_card)
        port_row.pack(fill=tk.X, padx=10, pady=(8, 2))
        ttk.Label(port_row, text="Default Port:").pack(side=tk.LEFT, padx=(0, 8))
        self._settings_port = ttk.Combobox(port_row, state="readonly", width=25)
        self._settings_port.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(port_row, text="Scan",
                   command=self._settings_refresh_ports).pack(side=tk.LEFT, padx=(4, 0))

        # Baud rate
        baud_row = ttk.Frame(serial_card)
        baud_row.pack(fill=tk.X, padx=10, pady=2)
        ttk.Label(baud_row, text="Baud Rate:").pack(side=tk.LEFT, padx=(0, 8))
        self._settings_baud = ttk.Combobox(
            baud_row, state="readonly", width=15,
            values=["9600", "19200", "38400", "57600", "115200",
                    "230400", "460800", "921600"])
        self._settings_baud.pack(side=tk.LEFT)
        self._settings_baud.set("115200")

        # Auto-connect
        auto_row = ttk.Frame(serial_card)
        auto_row.pack(fill=tk.X, padx=10, pady=(2, 8))
        self._settings_autoconnect = tk.BooleanVar(value=False)
        auto_cb = tk.Checkbutton(
            auto_row, text="Auto-connect on device detection",
            variable=self._settings_autoconnect,
            bg=_BG, fg=_FG, selectcolor=_INPUT_BG,
            activebackground=_BG, activeforeground=_ACCENT,
            font=("Segoe UI", 10))
        auto_cb.pack(side=tk.LEFT)

        # Appearance card
        appearance_card = ttk.LabelFrame(tab, text="Appearance")
        appearance_card.pack(fill=tk.X, padx=12, pady=(0, 8))

        theme_row = ttk.Frame(appearance_card)
        theme_row.pack(fill=tk.X, padx=10, pady=(8, 8))
        ttk.Label(theme_row, text="Theme:").pack(side=tk.LEFT, padx=(0, 8))
        self._settings_theme = ttk.Combobox(
            theme_row, state="readonly", width=15,
            values=["Dark"])
        self._settings_theme.pack(side=tk.LEFT)
        self._settings_theme.set("Dark")

        # Save / Load buttons
        btn_row = ttk.Frame(tab)
        btn_row.pack(fill=tk.X, padx=12, pady=(4, 8))
        ttk.Button(btn_row, text="Save Settings",
                   command=self._on_save_settings).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Load Settings",
                   command=self._on_load_settings).pack(side=tk.LEFT)

        # Pre-populate from disk
        self._on_load_settings()

    def _settings_refresh_ports(self) -> None:
        ports = self._dm.scan_ports()
        port_labels = [f"{d.port}" for d in ports]
        self._settings_port["values"] = port_labels
        if port_labels:
            self._settings_port.current(0)

    def _on_save_settings(self) -> None:
        if not _HAS_SETTINGS:
            messagebox.showwarning("Settings", "Settings module not available.")
            return
        settings = load_settings()
        baud_str = self._settings_baud.get()
        if baud_str:
            settings.setdefault("serial", {})["default_baud"] = int(baud_str)
        port_val = self._settings_port.get()
        if port_val:
            settings.setdefault("serial", {})["default_port"] = port_val
        settings.setdefault("serial", {})["auto_connect"] = self._settings_autoconnect.get()
        settings.setdefault("ui", {})["theme"] = self._settings_theme.get().lower()
        try:
            save_settings(settings)
            messagebox.showinfo("Settings", "Settings saved.")
        except Exception as exc:
            messagebox.showerror("Settings", f"Save failed: {exc}")

    def _on_load_settings(self) -> None:
        if not _HAS_SETTINGS:
            return
        try:
            settings = load_settings()
        except Exception:
            return
        serial = settings.get("serial", {})
        baud = str(serial.get("default_baud", 115200))
        if baud in [self._settings_baud.cget("values")]:
            self._settings_baud.set(baud)
        else:
            self._settings_baud.set(baud)
        port = serial.get("default_port", "")
        if port:
            current_vals = list(self._settings_port["values"])
            if port not in current_vals:
                current_vals.append(port)
                self._settings_port["values"] = current_vals
            self._settings_port.set(port)
        self._settings_autoconnect.set(serial.get("auto_connect", False))
        ui = settings.get("ui", {})
        theme = ui.get("theme", "dark")
        self._settings_theme.set(theme.capitalize())

    # ── Macros Tab ─────────────────────────────────────────────────

    def _build_macros_tab(self) -> None:
        tab = ttk.Frame(self._notebook)
        self._notebook.add(tab, text="  Macros  ")

        header = ttk.Label(tab, text="Macro Recorder",
                           font=("Segoe UI", 13, "bold"))
        header.pack(anchor=tk.W, padx=12, pady=(10, 4))

        # Control buttons
        ctrl_frame = ttk.LabelFrame(tab, text="Controls")
        ctrl_frame.pack(fill=tk.X, padx=12, pady=(0, 8))

        btn_row = ttk.Frame(ctrl_frame)
        btn_row.pack(fill=tk.X, padx=10, pady=(8, 4))

        self._btn_macro_record = ttk.Button(
            btn_row, text="Record", command=self._on_macro_record)
        self._btn_macro_record.pack(side=tk.LEFT, padx=(0, 4))

        self._btn_macro_stop = ttk.Button(
            btn_row, text="Stop", command=self._on_macro_stop, state=tk.DISABLED)
        self._btn_macro_stop.pack(side=tk.LEFT, padx=(0, 4))

        self._btn_macro_play = ttk.Button(
            btn_row, text="Play", command=self._on_macro_play)
        self._btn_macro_play.pack(side=tk.LEFT, padx=(0, 4))

        self._macro_status_label = ttk.Label(btn_row, text="Idle",
                                             foreground="#8b949e")
        self._macro_status_label.pack(side=tk.LEFT, padx=(12, 0))

        # Macro name / description entry (for saving)
        name_row = ttk.Frame(ctrl_frame)
        name_row.pack(fill=tk.X, padx=10, pady=(0, 8))
        ttk.Label(name_row, text="Name:").pack(side=tk.LEFT, padx=(0, 4))
        self._macro_name_entry = tk.Entry(
            name_row, font=("Segoe UI", 10), bg=_INPUT_BG, fg=_FG,
            insertbackground=_ACCENT, relief=tk.FLAT,
            highlightbackground=_BORDER, highlightcolor=_ACCENT,
            highlightthickness=1, width=30)
        self._macro_name_entry.pack(side=tk.LEFT, padx=(0, 8))
        self._macro_name_entry.insert(0, "Untitled")

        # Saved macros listbox
        list_card = ttk.LabelFrame(tab, text="Saved Macros")
        list_card.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 4))

        self._macro_listbox = tk.Listbox(
            list_card, bg=_BG_LIGHT, fg=_FG,
            selectbackground=_ACCENT, selectforeground="#000",
            font=("Consolas", 10), relief=tk.FLAT, borderwidth=0,
            highlightbackground=_BORDER, highlightcolor=_ACCENT,
            highlightthickness=1)
        macro_scroll = ttk.Scrollbar(list_card, command=self._macro_listbox.yview)
        self._macro_listbox.configure(yscrollcommand=macro_scroll.set)
        macro_scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 2), pady=2)
        self._macro_listbox.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        # Variable substitution
        var_card = ttk.LabelFrame(tab, text="Variables (name=value)")
        var_card.pack(fill=tk.X, padx=12, pady=(0, 8))

        var_row = ttk.Frame(var_card)
        var_row.pack(fill=tk.X, padx=10, pady=(8, 8))
        ttk.Label(var_row, text="Name:").pack(side=tk.LEFT, padx=(0, 4))
        self._macro_var_name = tk.Entry(
            var_row, font=("Segoe UI", 10), bg=_INPUT_BG, fg=_FG,
            insertbackground=_ACCENT, relief=tk.FLAT,
            highlightbackground=_BORDER, highlightcolor=_ACCENT,
            highlightthickness=1, width=15)
        self._macro_var_name.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(var_row, text="Value:").pack(side=tk.LEFT, padx=(0, 4))
        self._macro_var_value = tk.Entry(
            var_row, font=("Segoe UI", 10), bg=_INPUT_BG, fg=_FG,
            insertbackground=_ACCENT, relief=tk.FLAT,
            highlightbackground=_BORDER, highlightcolor=_ACCENT,
            highlightthickness=1, width=15)
        self._macro_var_value.pack(side=tk.LEFT, padx=(0, 8))
        self._macro_variables: dict[str, str] = {}

        ttk.Button(var_row, text="Add",
                   command=self._on_add_macro_variable).pack(side=tk.LEFT)

        if not _HAS_MACROS:
            notice = ttk.Label(tab, text="MacroRecorder module not available",
                               foreground=_WARNING, font=("Segoe UI", 10))
            notice.pack(padx=12, pady=4)

        # Populate saved macros list
        self._refresh_macro_list()

    def _refresh_macro_list(self) -> None:
        self._macro_listbox.delete(0, tk.END)
        if not self._macro_recorder:
            return
        try:
            for info in self._macro_recorder.list_saved_macros():
                label = f"{info['name']}  ({info['step_count']} steps)"
                self._macro_listbox.insert(tk.END, label)
        except Exception as exc:
            log.debug("Could not list macros: %s", exc)

    def _on_macro_record(self) -> None:
        if not self._macro_recorder:
            messagebox.showwarning("Macros", "MacroRecorder not available.")
            return
        if self._macro_recorder.is_recording:
            return
        port = self._active_port or "unknown"
        try:
            self._macro_recorder.start_recording(port)
            self._btn_macro_record.configure(state=tk.DISABLED)
            self._btn_macro_stop.configure(state=tk.NORMAL)
            self._macro_status_label.configure(text="Recording...", foreground=_ERROR)
        except Exception as exc:
            messagebox.showerror("Macros", f"Start failed: {exc}")

    def _on_macro_stop(self) -> None:
        if not self._macro_recorder or not self._macro_recorder.is_recording:
            return
        name = self._macro_name_entry.get().strip() or "Untitled"
        try:
            macro = self._macro_recorder.stop_recording(name=name)
            self._macro_recorder.save_macro(macro)
            self._btn_macro_record.configure(state=tk.NORMAL)
            self._btn_macro_stop.configure(state=tk.DISABLED)
            self._macro_status_label.configure(text="Idle", foreground="#8b949e")
            self._refresh_macro_list()
        except Exception as exc:
            messagebox.showerror("Macros", f"Stop/save failed: {exc}")

    def _on_macro_play(self) -> None:
        if not self._macro_recorder:
            messagebox.showwarning("Macros", "MacroRecorder not available.")
            return
        sel = self._macro_listbox.curselection()
        if not sel:
            messagebox.showinfo("Macros", "Select a macro to play.")
            return
        try:
            saved = self._macro_recorder.list_saved_macros()
            info = saved[sel[0]]
            macro = self._macro_recorder.load_macro(info["path"])
        except Exception as exc:
            messagebox.showerror("Macros", f"Load failed: {exc}")
            return

        if not self._active_conn:
            messagebox.showwarning("Macros", "No active serial connection. Connect a device first.")
            return

        conn = self._active_conn
        variables = dict(self._macro_variables) if self._macro_variables else None
        self._macro_status_label.configure(text="Playing...", foreground=_ACCENT)

        def _play_thread() -> None:
            try:
                self._macro_recorder.play(
                    macro,
                    send_command=lambda cmd: conn.write(cmd),
                    variables=variables,
                )
            except Exception as exc:
                self._root.after(0, lambda: messagebox.showerror("Macros", f"Playback error: {exc}"))
            self._root.after(0, lambda: self._macro_status_label.configure(
                text="Idle", foreground="#8b949e"))

        threading.Thread(target=_play_thread, daemon=True).start()

    def _on_add_macro_variable(self) -> None:
        name = self._macro_var_name.get().strip()
        value = self._macro_var_value.get().strip()
        if not name:
            return
        self._macro_variables[name] = value
        self._macro_var_name.delete(0, tk.END)
        self._macro_var_value.delete(0, tk.END)

    # ── Cross-Comm Tab ─────────────────────────────────────────────

    def _build_crosscomm_tab(self) -> None:
        tab = ttk.Frame(self._notebook)
        self._notebook.add(tab, text="  Cross-Comm  ")

        header = ttk.Label(tab, text="Cross-Device Communication",
                           font=("Segoe UI", 13, "bold"))
        header.pack(anchor=tk.W, padx=12, pady=(10, 4))

        # Use a PanedWindow for top/bottom split
        paned = ttk.PanedWindow(tab, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        # Top: Target Pool display
        pool_frame = ttk.LabelFrame(tab, text="Target Pool")
        paned.add(pool_frame, weight=2)

        columns = ("mac", "type", "rssi", "source", "last_seen")
        self._xcomm_tree = ttk.Treeview(
            pool_frame, columns=columns, show="headings",
            selectmode="browse", height=8)
        self._xcomm_tree.heading("mac", text="MAC")
        self._xcomm_tree.heading("type", text="Type")
        self._xcomm_tree.heading("rssi", text="RSSI")
        self._xcomm_tree.heading("source", text="Source")
        self._xcomm_tree.heading("last_seen", text="Last Seen")

        self._xcomm_tree.column("mac", width=140, minwidth=100)
        self._xcomm_tree.column("type", width=80, minwidth=50, anchor=tk.CENTER)
        self._xcomm_tree.column("rssi", width=60, minwidth=40, anchor=tk.CENTER)
        self._xcomm_tree.column("source", width=120, minwidth=80)
        self._xcomm_tree.column("last_seen", width=140, minwidth=80)

        self._xcomm_tree.tag_configure("oddrow", background=_BG_LIGHT)
        self._xcomm_tree.tag_configure("evenrow", background=_BG_CARD)

        xcomm_scroll = ttk.Scrollbar(pool_frame, command=self._xcomm_tree.yview)
        self._xcomm_tree.configure(yscrollcommand=xcomm_scroll.set)
        xcomm_scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 2), pady=2)
        self._xcomm_tree.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        # Middle: Auto-Router rules (read-only)
        rules_frame = ttk.LabelFrame(tab, text="Auto-Router Rules")
        paned.add(rules_frame, weight=1)

        self._xcomm_rules_listbox = tk.Listbox(
            rules_frame, bg=_BG_LIGHT, fg=_FG,
            selectbackground=_ACCENT, selectforeground="#000",
            font=("Consolas", 10), relief=tk.FLAT, borderwidth=0,
            highlightbackground=_BORDER, highlightcolor=_ACCENT,
            highlightthickness=1, height=5)
        rules_scroll = ttk.Scrollbar(rules_frame, command=self._xcomm_rules_listbox.yview)
        self._xcomm_rules_listbox.configure(yscrollcommand=rules_scroll.set)
        rules_scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 2), pady=2)
        self._xcomm_rules_listbox.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        # Bottom: Event log
        log_frame = ttk.LabelFrame(tab, text="Event Log")
        paned.add(log_frame, weight=2)

        self._xcomm_log = tk.Text(
            log_frame, bg=_TERM_BG, fg=_TERM_FG,
            font=("Consolas", 10), insertbackground=_ACCENT,
            relief=tk.FLAT, state=tk.DISABLED, wrap=tk.WORD,
            highlightbackground=_BORDER, highlightcolor=_ACCENT,
            highlightthickness=1, height=8)
        log_scroll = ttk.Scrollbar(log_frame, command=self._xcomm_log.yview)
        self._xcomm_log.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 2), pady=2)
        self._xcomm_log.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        # Subscribe to all events for the log
        self._bus.subscribe("*", self._on_xcomm_event)

        # Populate initial state
        self._refresh_xcomm_pool()
        self._refresh_xcomm_rules()

    def _refresh_xcomm_pool(self) -> None:
        """Refresh the cross-comm target pool display."""
        for item in self._xcomm_tree.get_children():
            self._xcomm_tree.delete(item)
        for idx, t in enumerate(self._pool.all()):
            row_tag = "oddrow" if idx % 2 else "evenrow"
            last_seen = ""
            if hasattr(t, "last_seen") and t.last_seen:
                try:
                    last_seen = str(t.last_seen)[:19]
                except Exception:
                    pass
            self._xcomm_tree.insert("", tk.END, values=(
                t.mac,
                t.target_type.value if hasattr(t.target_type, "value") else str(t.target_type),
                str(t.rssi),
                t.device_source,
                last_seen,
            ), tags=(row_tag,))

    def _refresh_xcomm_rules(self) -> None:
        """Refresh the auto-router rules display."""
        self._xcomm_rules_listbox.delete(0, tk.END)
        if not _HAS_AUTO_ROUTER:
            self._xcomm_rules_listbox.insert(tk.END, "(AutoRouter not available)")
            return
        # AutoRouter requires a send_command callback; check if one was set up
        # We display rules from the pool's bus-connected router if available
        # For now, show a placeholder if no router instance is accessible
        self._xcomm_rules_listbox.insert(tk.END, "(No rules configured)")

    def _on_xcomm_event(self, topic: str, payload: dict) -> None:
        """Log all EventBus events to the cross-comm event log."""
        def _append() -> None:
            ts = time.strftime("%H:%M:%S")
            summary = f"[{ts}] {topic}"
            if isinstance(payload, dict):
                # Show a brief summary of payload keys
                keys = list(payload.keys())[:4]
                if keys:
                    summary += f"  ({', '.join(keys)})"
            self._xcomm_log.configure(state=tk.NORMAL)
            self._xcomm_log.insert(tk.END, summary + "\n")
            self._xcomm_log.see(tk.END)
            self._xcomm_log.configure(state=tk.DISABLED)
            # Refresh pool display on target events
            if topic.startswith("target."):
                self._refresh_xcomm_pool()
        self._root.after(0, _append)

    # ── Status Bar ──────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        self._status_var = tk.StringVar(value="  Devices: 0  |  Targets: 0  ")
        status = ttk.Label(self._root, textvariable=self._status_var,
                           style="Status.TLabel", anchor=tk.W)
        status.pack(fill=tk.X, side=tk.BOTTOM)

    def _refresh_status(self) -> None:
        n_conn = len(self._dm.list_connected())
        n_total = len(self._dm.list_devices())
        n_targets = self._pool.count
        self._status_var.set(
            f"  Devices: {n_conn}/{n_total} connected  |  Targets: {n_targets}  "
        )

    # ── Periodic refresh ────────────────────────────────────────────

    def _start_periodic_refresh(self) -> None:
        self._refresh_status()
        self._refresh_device_list()
        self._root.after(3000, self._start_periodic_refresh)

    # ── Profile / port loading ──────────────────────────────────────

    def _load_profiles(self) -> None:
        self._profiles.clear()
        if _PROFILES_DIR.is_dir():
            for f in sorted(_PROFILES_DIR.glob("*.json")):
                try:
                    p = FirmwareProfile.from_file(f)
                    name = p.name or f.stem
                except Exception:
                    name = f.stem
                self._profiles[name] = f
        self._flash_profile["values"] = list(self._profiles.keys())
        if self._profiles:
            self._flash_profile.current(0)

    def _refresh_ports(self) -> None:
        ports = self._dm.scan_ports()
        port_labels = [f"{d.port} - {d.name}" for d in ports]
        self._flash_port["values"] = port_labels
        if port_labels:
            self._flash_port.current(0)

    # ── Flash actions ───────────────────────────────────────────────

    def _on_flash(self) -> None:
        port_text = self._flash_port.get()
        profile_name = self._flash_profile.get()
        if not port_text:
            self._flash_log_append("No port selected.")
            return
        if not profile_name:
            self._flash_log_append("No firmware profile selected.")
            return

        # If Dead Man's Switch is enabled, open setup first
        if self._deadman_enabled.get():
            self._flash_log_append("Dead Man's Switch enabled -- opening setup...")

            def _dms_done(confirmed: bool) -> None:
                if confirmed:
                    self._flash_log_append("Dead Man's Switch configured. Proceeding with flash.")
                    self._do_flash(port_text, profile_name)
                else:
                    self._flash_log_append("Dead Man's Switch setup cancelled. Flash aborted.")

            self._launch_deadman_setup(on_complete=_dms_done)
            return

        self._do_flash(port_text, profile_name)

    def _do_flash(self, port_text: str, profile_name: str) -> None:
        """Execute the actual flash operation."""
        port = port_text.split(" - ")[0].strip()
        profile_path = self._profiles.get(profile_name)
        if not profile_path:
            self._flash_log_append(f"Profile not found: {profile_name}")
            return

        profile = self._fe.load_profile(profile_path)
        self._flash_log_append(f"Flashing {profile.name} to {port}...")
        self._btn_flash.configure(state=tk.DISABLED)
        self._flash_progress["value"] = 0

        def progress_cb(pct: int, msg: str) -> None:
            self._root.after(0, self._flash_progress.configure, {"value": pct})
            self._root.after(0, self._flash_log_append, msg)

        def flash_thread() -> None:
            ok = self._fe.flash(port, profile, progress_callback=progress_cb)
            self._root.after(0, self._on_flash_done, ok)

        threading.Thread(target=flash_thread, daemon=True).start()

    def _on_flash_done(self, success: bool) -> None:
        self._btn_flash.configure(state=tk.NORMAL)
        if success:
            self._flash_progress["value"] = 100
            self._flash_log_append("Flash completed successfully.")
        else:
            self._flash_log_append("Flash failed. See log for details.")

    def _flash_log_append(self, msg: str) -> None:
        self._flash_log.configure(state=tk.NORMAL)
        self._flash_log.insert(tk.END, msg + "\n")
        self._flash_log.see(tk.END)
        self._flash_log.configure(state=tk.DISABLED)

    # ── Device actions ──────────────────────────────────────────────

    def _refresh_device_list(self) -> None:
        self._device_listbox.delete(0, tk.END)
        for dev in self._dm.list_devices():
            status = "[+]" if dev.connected else "[-]"
            self._device_listbox.insert(tk.END, f"{status} {dev.port} - {dev.name}")

    def _scan_devices(self) -> None:
        for dev in self._dm.scan_ports():
            if not self._dm.get_device(dev.port):
                self._dm.add_device(dev)
        self._refresh_device_list()

    def _on_device_select(self, _event: tk.Event) -> None:
        sel = self._device_listbox.curselection()
        if not sel:
            return
        text = self._device_listbox.get(sel[0])
        # Parse port from "[+] COM3 - description"
        parts = text.split(" - ", 1)
        port = parts[0].replace("[+]", "").replace("[-]", "").strip()
        self._active_port = port
        dev = self._dm.get_device(port)
        if dev:
            self._dev_term_label.configure(text=f"Serial Output - {dev.display_name}")
            connected = dev.connected
            self._btn_dev_connect.configure(state=tk.DISABLED if connected else tk.NORMAL)
            self._btn_dev_disconnect.configure(state=tk.NORMAL if connected else tk.DISABLED)
            self._btn_send.configure(state=tk.NORMAL if connected else tk.DISABLED)

    def _on_dev_connect(self) -> None:
        if not self._active_port:
            return
        try:
            conn = self._dm.open_connection(self._active_port)
            self._active_conn = conn
            conn.on_line(lambda line: self._root.after(0, self._append_serial, line))
            self._serial_append_sys(f"Connected to {self._active_port}")
            self._btn_dev_connect.configure(state=tk.DISABLED)
            self._btn_dev_disconnect.configure(state=tk.NORMAL)
            self._btn_send.configure(state=tk.NORMAL)
            self._refresh_device_list()
        except Exception as exc:
            self._serial_append_sys(f"Error: {exc}")

    def _on_dev_disconnect(self) -> None:
        if not self._active_port:
            return
        self._dm.close_connection(self._active_port)
        self._active_conn = None
        self._serial_append_sys(f"Disconnected from {self._active_port}")
        self._btn_dev_connect.configure(state=tk.NORMAL)
        self._btn_dev_disconnect.configure(state=tk.DISABLED)
        self._btn_send.configure(state=tk.DISABLED)
        self._refresh_device_list()

    def _on_send_cmd(self) -> None:
        cmd = self._cmd_entry.get().strip()
        if not cmd or not self._active_conn:
            return
        try:
            self._active_conn.write(cmd)
            self._append_serial(f"> {cmd}")
            self._cmd_entry.delete(0, tk.END)
        except Exception as exc:
            self._serial_append_sys(f"Send error: {exc}")

    def _append_serial(self, text: str) -> None:
        self._serial_output.configure(state=tk.NORMAL)
        self._serial_output.insert(tk.END, text + "\n")
        self._serial_output.see(tk.END)
        self._serial_output.configure(state=tk.DISABLED)

    def _serial_append_sys(self, msg: str) -> None:
        self._append_serial(f"[{msg}]")

    # ── Target actions ──────────────────────────────────────────────

    def _refresh_targets(self) -> None:
        for item in self._target_tree.get_children():
            self._target_tree.delete(item)
        for idx, t in enumerate(self._pool.all()):
            row_tag = "oddrow" if idx % 2 else "evenrow"
            # RSSI-based colour tag
            try:
                rssi_val = int(t.rssi) if t.rssi is not None else -100
            except (ValueError, TypeError):
                rssi_val = -100
            if rssi_val > -50:
                rssi_tag = "rssi_good"
            elif rssi_val > -65:
                rssi_tag = "rssi_mid"
            else:
                rssi_tag = "rssi_bad"
            self._target_tree.insert("", tk.END, values=(
                t.mac, t.ssid, t.rssi, t.channel,
                t.device_source, t.target_type.value,
            ), tags=(row_tag, rssi_tag))

    def _clear_targets(self) -> None:
        self._pool.clear()
        self._refresh_targets()

    def _on_target_event(self, _topic: str, _payload: dict) -> None:
        self._root.after(0, self._refresh_targets)

    # ── Keyboard Shortcuts Dialog ──────────────────────────────────

    def _show_keyboard_shortcuts(self) -> None:
        """Display a dialog listing common keyboard shortcuts."""
        shortcuts = (
            "Ctrl+Q          Quit application\n"
            "Return          Send command (in serial input)\n"
            "Tab / Shift+Tab Navigate between controls\n"
            "Ctrl+Tab        Next tab\n"
            "Ctrl+Shift+Tab  Previous tab\n"
            "F5              Refresh ports / targets\n"
        )
        dlg = tk.Toplevel(self._root)
        dlg.title("Keyboard Shortcuts")
        dlg.geometry("400x280")
        dlg.configure(bg=_BG)
        dlg.transient(self._root)
        dlg.grab_set()

        ttk.Label(dlg, text="Keyboard Shortcuts",
                  font=("Segoe UI", 13, "bold")).pack(padx=16, pady=(12, 8))

        text = tk.Text(dlg, bg=_BG_LIGHT, fg=_FG, font=("Consolas", 10),
                       relief=tk.FLAT, wrap=tk.WORD, height=10,
                       highlightbackground=_BORDER, highlightcolor=_ACCENT,
                       highlightthickness=1)
        text.insert("1.0", shortcuts)
        text.configure(state=tk.DISABLED)
        text.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 8))

        ttk.Button(dlg, text="Close", command=dlg.destroy).pack(pady=(0, 12))

    # ── Dead Man's Switch toggle ─────────────────────────────────

    def _on_deadman_toggle(self, *_args: Any) -> None:
        """Update the DMS checkbox area border color based on state."""
        if self._deadman_enabled.get():
            self._dms_border_frame.configure(
                highlightbackground=_WARNING, highlightcolor=_WARNING)
            self._dms_check.configure(fg=_WARNING)
        else:
            self._dms_border_frame.configure(
                highlightbackground=_BORDER, highlightcolor=_BORDER)
            self._dms_check.configure(fg=_FG)

    # ── Dead Man's Switch Setup ───────────────────────────────────

    def _launch_deadman_setup(self, on_complete: Any | None = None) -> None:
        """Launch the Dead Man's Switch setup dialog.

        Parameters
        ----------
        on_complete:
            Optional callback invoked with ``True`` when the user confirms
            setup, or ``False`` / not called when they cancel. Used by the
            flash handler to gate flashing behind DMS configuration.
        """
        if not _HAS_DEADMAN:
            messagebox.showwarning(
                "Dead Man's Switch Setup",
                "Dead Man's Switch module not available.\n\n"
                "Ensure the Dead Man's Switch submodule is initialised:\n"
                "  git submodule update --init")
            if on_complete:
                on_complete(False)
            return
        # Open a configuration dialog
        dlg = tk.Toplevel(self._root)
        dlg.title("Dead Man's Switch Setup")
        dlg.geometry("500x420")
        dlg.configure(bg=_BG)
        dlg.transient(self._root)
        dlg.grab_set()

        ttk.Label(dlg, text="Dead Man's Switch Setup",
                  font=("Segoe UI", 13, "bold")).pack(padx=16, pady=(12, 4))
        ttk.Label(dlg, text="Owner-only defensive anti-forensic layer.\n"
                  "A disarmed/unprovisioned board can NEVER wipe (fail-safe).",
                  foreground="#8b949e", wraplength=460).pack(padx=16, pady=(0, 8))

        cfg_frame = ttk.LabelFrame(dlg, text="Configuration")
        cfg_frame.pack(fill=tk.X, padx=16, pady=(0, 8))

        fields: dict[str, tk.Entry] = {}
        defaults = {
            "Chip": "esp32", "Flash Size": "4MB", "Variant": "fork",
            "Arm Pin": "27", "Max Attempts": "2",
        }
        for label_text, default_val in defaults.items():
            row = ttk.Frame(cfg_frame)
            row.pack(fill=tk.X, padx=10, pady=2)
            ttk.Label(row, text=f"{label_text}:", width=14).pack(side=tk.LEFT)
            entry = tk.Entry(row, font=("Segoe UI", 10), bg=_INPUT_BG, fg=_FG,
                             insertbackground=_ACCENT, relief=tk.FLAT,
                             highlightbackground=_BORDER, highlightcolor=_ACCENT,
                             highlightthickness=1)
            entry.insert(0, default_val)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
            fields[label_text] = entry

        note = ttk.Label(dlg, text="Full provisioning requires the CLI:\n"
                         "  cyber-controller --deadman-setup",
                         foreground=_WARNING, wraplength=460)
        note.pack(padx=16, pady=(4, 8))

        btn_row = ttk.Frame(dlg)
        btn_row.pack(pady=(0, 12))

        _completed = [False]

        def _on_ok() -> None:
            _completed[0] = True
            dlg.destroy()
            if on_complete:
                on_complete(True)

        def _on_cancel() -> None:
            dlg.destroy()
            if on_complete:
                on_complete(False)

        ttk.Button(btn_row, text="OK", command=_on_ok).pack(
            side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="Cancel", command=_on_cancel).pack(
            side=tk.LEFT)

        dlg.protocol("WM_DELETE_WINDOW", _on_cancel)

    # ── About ───────────────────────────────────────────────────────

    def _show_about(self) -> None:
        messagebox.showinfo(
            "About Cyber Controller Lite",
            f"Cyber Controller Lite v{_VERSION}\n\n"
            "Lightweight interface for flash, device management,\n"
            "and target monitoring.\n\n"
            "github.com/LxveAce/cyber-controller\n"
            "MIT License - LxveAce 2026",
        )

    # ── Run ─────────────────────────────────────────────────────────

    def run(self) -> int:
        """Start the Tkinter main loop. Returns 0 on normal exit."""
        try:
            self._root.mainloop()
        except KeyboardInterrupt:
            pass
        return 0


def launch_tk(
    device_manager: DeviceManager,
    flash_engine: FlashEngine,
    event_bus: EventBus,
    target_pool: TargetPool,
) -> int:
    """Create and run the Tkinter lightweight UI."""
    app = TkLightApp(device_manager, flash_engine, event_bus, target_pool)
    return app.run()
