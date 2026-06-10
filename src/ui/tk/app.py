"""Tkinter lightweight UI — reduced feature set for resource-constrained environments.

Provides flash, device management, and target viewing without the topology graph,
attack chain builder, or mission planner found in the full Qt interface.
"""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine, FirmwareProfile
from src.core.serial_handler import SerialConnection

if TYPE_CHECKING:
    from src.models.device import Device
    from src.models.target import Target

log = logging.getLogger(__name__)

_VERSION = "0.1.0"
_PROFILES_DIR = Path(__file__).resolve().parents[3] / "src" / "config" / "profiles"

# ── Dark theme colours ──────────────────────────────────────────────

_BG = "#1e1e1e"
_BG_LIGHT = "#2d2d2d"
_FG = "#dcdcdc"
_ACCENT = "#39ff14"
_TERM_BG = "#111111"
_TERM_FG = "#39ff14"


def _apply_dark_theme(style: ttk.Style) -> None:
    """Configure a dark ttk theme."""
    style.theme_use("clam")

    style.configure(".", background=_BG, foreground=_FG, fieldbackground=_BG_LIGHT,
                     borderwidth=1, focusthickness=0)
    style.configure("TNotebook", background=_BG, borderwidth=0)
    style.configure("TNotebook.Tab", background=_BG_LIGHT, foreground=_FG,
                     padding=[12, 4])
    style.map("TNotebook.Tab",
              background=[("selected", _BG), ("!selected", _BG_LIGHT)],
              foreground=[("selected", _ACCENT), ("!selected", _FG)])

    style.configure("TFrame", background=_BG)
    style.configure("TLabel", background=_BG, foreground=_FG)
    style.configure("TButton", background=_BG_LIGHT, foreground=_FG, padding=[8, 4])
    style.map("TButton",
              background=[("active", "#3a3a3a"), ("pressed", "#444444")])

    style.configure("TCombobox", fieldbackground=_BG_LIGHT, foreground=_FG,
                     background=_BG_LIGHT, selectbackground=_ACCENT,
                     selectforeground="#000000")
    style.map("TCombobox", fieldbackground=[("readonly", _BG_LIGHT)])

    style.configure("TProgressbar", background=_ACCENT, troughcolor=_BG_LIGHT,
                     borderwidth=0, thickness=20)

    style.configure("Treeview", background=_BG_LIGHT, foreground=_FG,
                     fieldbackground=_BG_LIGHT, borderwidth=0, rowheight=24)
    style.configure("Treeview.Heading", background=_BG, foreground=_ACCENT,
                     borderwidth=1)
    style.map("Treeview",
              background=[("selected", "#333333")],
              foreground=[("selected", _ACCENT)])

    style.configure("TLabelframe", background=_BG, foreground=_ACCENT)
    style.configure("TLabelframe.Label", background=_BG, foreground=_ACCENT)

    style.configure("Flash.TButton", background="#39ff14", foreground="#000000",
                     font=("Segoe UI", 10, "bold"))
    style.map("Flash.TButton",
              background=[("active", "#2de00f"), ("pressed", "#1fc00a")])

    style.configure("Status.TLabel", background="#141414", foreground="#888888",
                     padding=[6, 2])


class TkLightApp:
    """Lightweight Tkinter interface for Cyber Controller.

    Three tabs: Flash, Devices, Targets.  No topology graph, attack chain
    builder, or mission planner.
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

        help_menu = tk.Menu(menubar, tearoff=0, bg=_BG_LIGHT, fg=_FG,
                            activebackground=_ACCENT, activeforeground="#000")
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

        # Progress bar
        self._flash_progress = ttk.Progressbar(tab, mode="determinate",
                                                maximum=100, value=0)
        self._flash_progress.pack(fill=tk.X, padx=8, pady=(0, 4))

        # Log text area
        log_frame = ttk.LabelFrame(tab, text="Flash Log")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self._flash_log = tk.Text(log_frame, bg=_TERM_BG, fg=_TERM_FG,
                                  font=("Consolas", 9), insertbackground=_TERM_FG,
                                  relief=tk.FLAT, state=tk.DISABLED, wrap=tk.WORD)
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
                                          font=("Consolas", 9),
                                          relief=tk.FLAT, borderwidth=0)
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
                                      font=("Consolas", 9), insertbackground=_TERM_FG,
                                      relief=tk.FLAT, state=tk.DISABLED, wrap=tk.WORD)
        serial_scroll = ttk.Scrollbar(right, command=self._serial_output.yview)
        self._serial_output.configure(yscrollcommand=serial_scroll.set)
        serial_scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 6), pady=(0, 4))
        self._serial_output.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 4))

        cmd_frame = ttk.Frame(right)
        cmd_frame.pack(fill=tk.X, padx=6, pady=(0, 6))
        self._cmd_entry = ttk.Entry(cmd_frame, font=("Consolas", 10))
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

        tree_scroll = ttk.Scrollbar(tab, command=self._target_tree.yview)
        self._target_tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=(0, 8))
        self._target_tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

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
        for t in self._pool.all():
            self._target_tree.insert("", tk.END, values=(
                t.mac, t.ssid, t.rssi, t.channel,
                t.device_source, t.target_type.value,
            ))

    def _clear_targets(self) -> None:
        self._pool.clear()
        self._refresh_targets()

    def _on_target_event(self, _topic: str, _payload: dict) -> None:
        self._root.after(0, self._refresh_targets)

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
