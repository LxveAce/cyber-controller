"""Textual TUI — terminal-based interface for Cyber Controller.

Essential operations: flash, connect to device, send commands, view targets.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Log,
    ProgressBar,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine, FirmwareProfile
from src.core.serial_handler import SerialConnection

if TYPE_CHECKING:
    from src.models.device import Device
    from src.models.target import Target

log = logging.getLogger(__name__)

_PROFILES_DIR = Path(__file__).resolve().parents[3] / "src" / "config" / "profiles"


def _health_class(pct: float) -> str:
    """Return a CSS class name based on a system metric percentage."""
    if pct < 60:
        return "health-ok"
    if pct < 85:
        return "health-warn"
    return "health-crit"


class HealthFooter(Static):
    """Displays CPU and RAM usage in the footer area."""

    cpu_pct: reactive[float] = reactive(0.0)
    ram_pct: reactive[float] = reactive(0.0)

    def render(self) -> str:
        if not _HAS_PSUTIL:
            return "  [psutil not installed]"
        return f"  CPU: {self.cpu_pct:.0f}%  |  RAM: {self.ram_pct:.0f}%"

    def watch_cpu_pct(self) -> None:
        self.set_class(_health_class(self.cpu_pct) == "health-ok", "health-ok")
        self.set_class(_health_class(self.cpu_pct) == "health-warn", "health-warn")
        self.set_class(_health_class(self.cpu_pct) == "health-crit", "health-crit")


class CyberControllerTUI(App):
    """Terminal UI for Cyber Controller built with Textual."""

    TITLE = "Cyber Controller TUI"
    CSS_PATH = "styles.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("f", "focus_flash", "Flash Tab"),
        Binding("t", "focus_terminal", "Terminal Tab"),
        Binding("g", "focus_targets", "Targets Tab"),
        Binding("r", "refresh", "Refresh"),
    ]

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

        self._profiles: dict[str, Path] = {}
        self._active_conn: SerialConnection | None = None
        self._active_port: str = ""

        self._load_profiles()

    # ── Profile loading ─────────────────────────────────────────────

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

    def _get_port_options(self) -> list[tuple[str, str]]:
        ports = self._dm.scan_ports()
        if not ports:
            return [("No devices found", "")]
        return [(f"{d.port} - {d.name}", d.port) for d in ports]

    def _get_profile_options(self) -> list[tuple[str, str]]:
        if not self._profiles:
            return [("No profiles found", "")]
        return [(name, name) for name in self._profiles]

    # ── Layout ──────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent("Flash", "Terminal", "Targets", id="tabs"):
            with TabPane("Flash", id="flash-tab"):
                yield from self._compose_flash_tab()
            with TabPane("Terminal", id="terminal-tab"):
                yield from self._compose_terminal_tab()
            with TabPane("Targets", id="targets-tab"):
                yield from self._compose_targets_tab()
        yield HealthFooter(id="health-footer")
        yield Footer()

    def _compose_flash_tab(self) -> ComposeResult:
        with Vertical(id="flash-layout"):
            with Horizontal(id="flash-selectors"):
                with Vertical(id="flash-port-group", classes="selector-group"):
                    yield Label("Port", classes="selector-label")
                    yield Select(
                        self._get_port_options(),
                        id="flash-port-select",
                        prompt="Select port...",
                    )
                with Vertical(id="flash-profile-group", classes="selector-group"):
                    yield Label("Firmware Profile", classes="selector-label")
                    yield Select(
                        self._get_profile_options(),
                        id="flash-profile-select",
                        prompt="Select profile...",
                    )
                with Vertical(id="flash-btn-group", classes="selector-group"):
                    yield Button("Flash", id="btn-flash", variant="success")
                    yield Button("Refresh Ports", id="btn-refresh-ports")
            yield Checkbox("Enable Dead Man's Switch", id="deadman-toggle")
            yield Label("Anti-forensic wipe — prompts for setup before flashing", classes="selector-label", id="deadman-desc")
            yield ProgressBar(id="flash-progress", total=100, show_eta=False)
            yield Label("Flash Log", classes="section-label")
            yield Log(id="flash-log", auto_scroll=True)

    def _compose_terminal_tab(self) -> ComposeResult:
        with Vertical(id="terminal-layout"):
            with Horizontal(id="terminal-top"):
                yield Select(
                    self._get_port_options(),
                    id="terminal-port-select",
                    prompt="Select device...",
                )
                yield Button("Connect", id="btn-connect", variant="primary")
                yield Button("Disconnect", id="btn-disconnect", variant="error",
                             disabled=True)
            yield Log(id="serial-log", auto_scroll=True)
            with Horizontal(id="terminal-input-row"):
                yield Input(placeholder="Type command...", id="cmd-input")
                yield Button("Send", id="btn-send", disabled=True)

    def _compose_targets_tab(self) -> ComposeResult:
        with Vertical(id="targets-layout"):
            with Horizontal(id="targets-toolbar"):
                yield Label("Discovered Targets", classes="section-label")
                yield Button("Refresh", id="btn-refresh-targets")
                yield Button("Clear All", id="btn-clear-targets", variant="error")
            yield DataTable(id="target-table")

    # ── Lifecycle ───────────────────────────────────────────────────

    def on_mount(self) -> None:
        # Set up target table columns
        table = self.query_one("#target-table", DataTable)
        table.add_columns("MAC", "SSID", "RSSI", "Channel", "Source", "Type")
        table.zebra_stripes = True
        self._refresh_targets()

        # Wire event bus
        self._bus.subscribe("target.added", self._on_target_event)
        self._bus.subscribe("target.updated", self._on_target_event)

        # Start health metric polling
        self.set_interval(5.0, self._update_health)

    # ── Health metrics ──────────────────────────────────────────────

    def _update_health(self) -> None:
        """Poll CPU and RAM usage, update the health footer widget."""
        if not _HAS_PSUTIL:
            return
        try:
            footer = self.query_one("#health-footer", HealthFooter)
            footer.cpu_pct = psutil.cpu_percent(interval=None)
            footer.ram_pct = psutil.virtual_memory().percent
        except Exception:
            pass

    # ── Actions ─────────────────────────────────────────────────────

    def action_focus_flash(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = "flash-tab"

    def action_focus_terminal(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = "terminal-tab"

    def action_focus_targets(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = "targets-tab"

    def action_refresh(self) -> None:
        self._refresh_targets()
        self._refresh_port_selects()

    def _refresh_port_selects(self) -> None:
        opts = self._get_port_options()
        try:
            self.query_one("#flash-port-select", Select).set_options(opts)
            self.query_one("#terminal-port-select", Select).set_options(opts)
        except Exception:
            pass

    # ── Button handlers ─────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn-flash":
            self._do_flash()
        elif btn_id == "btn-refresh-ports":
            self._refresh_port_selects()
        elif btn_id == "btn-connect":
            self._do_connect()
        elif btn_id == "btn-disconnect":
            self._do_disconnect()
        elif btn_id == "btn-send":
            self._do_send()
        elif btn_id == "btn-refresh-targets":
            self._refresh_targets()
        elif btn_id == "btn-clear-targets":
            self._pool.clear()
            self._refresh_targets()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "cmd-input":
            self._do_send()

    # ── Flash ───────────────────────────────────────────────────────

    def _do_flash(self) -> None:
        port_select = self.query_one("#flash-port-select", Select)
        profile_select = self.query_one("#flash-profile-select", Select)
        flash_log = self.query_one("#flash-log", Log)
        progress = self.query_one("#flash-progress", ProgressBar)
        btn = self.query_one("#btn-flash", Button)

        port = port_select.value
        profile_name = profile_select.value

        if not port or port == Select.BLANK:
            flash_log.write_line("No port selected.")
            return
        if not profile_name or profile_name == Select.BLANK:
            flash_log.write_line("No firmware profile selected.")
            return

        profile_path = self._profiles.get(str(profile_name))
        if not profile_path:
            flash_log.write_line(f"Profile not found: {profile_name}")
            return

        profile = self._fe.load_profile(profile_path)

        deadman_cb = self.query_one("#deadman-toggle", Checkbox)
        if deadman_cb.value:
            flash_log.write_line("[Dead Man's Switch] Setup required before flash.")
            flash_log.write_line("Run with --deadman-setup flag or use the full GUI for interactive setup.")
            flash_log.write_line("Aborting flash — complete DMS provisioning first.")
            return

        flash_log.write_line(f"Flashing {profile.name} to {port}...")
        btn.disabled = True
        progress.update(progress=0)

        def progress_cb(pct: int, msg: str) -> None:
            self.call_from_thread(progress.update, progress=pct)
            self.call_from_thread(flash_log.write_line, msg)

        def flash_thread() -> None:
            ok = self._fe.flash(str(port), profile, progress_callback=progress_cb)
            self.call_from_thread(self._on_flash_done, ok)

        threading.Thread(target=flash_thread, daemon=True).start()

    def _on_flash_done(self, success: bool) -> None:
        btn = self.query_one("#btn-flash", Button)
        flash_log = self.query_one("#flash-log", Log)
        progress = self.query_one("#flash-progress", ProgressBar)
        btn.disabled = False
        if success:
            progress.update(progress=100)
            flash_log.write_line("Flash completed successfully.")
        else:
            flash_log.write_line("Flash failed. See log for details.")

    # ── Terminal ────────────────────────────────────────────────────

    def _do_connect(self) -> None:
        port_select = self.query_one("#terminal-port-select", Select)
        serial_log = self.query_one("#serial-log", Log)
        port = port_select.value

        if not port or port == Select.BLANK:
            serial_log.write_line("[No device selected]")
            return

        try:
            conn = self._dm.open_connection(str(port))
            self._active_conn = conn
            self._active_port = str(port)
            conn.on_line(
                lambda line: self.call_from_thread(serial_log.write_line, line)
            )
            serial_log.write_line(f"[Connected to {port}]")
            self.query_one("#btn-connect", Button).disabled = True
            self.query_one("#btn-disconnect", Button).disabled = False
            self.query_one("#btn-send", Button).disabled = False
        except Exception as exc:
            serial_log.write_line(f"[Error: {exc}]")

    def _do_disconnect(self) -> None:
        serial_log = self.query_one("#serial-log", Log)
        if self._active_port:
            self._dm.close_connection(self._active_port)
            serial_log.write_line(f"[Disconnected from {self._active_port}]")
        self._active_conn = None
        self._active_port = ""
        self.query_one("#btn-connect", Button).disabled = False
        self.query_one("#btn-disconnect", Button).disabled = True
        self.query_one("#btn-send", Button).disabled = True

    def _do_send(self) -> None:
        cmd_input = self.query_one("#cmd-input", Input)
        serial_log = self.query_one("#serial-log", Log)
        cmd = cmd_input.value.strip()
        if not cmd or not self._active_conn:
            return
        try:
            self._active_conn.write(cmd)
            serial_log.write_line(f"> {cmd}")
            cmd_input.value = ""
        except Exception as exc:
            serial_log.write_line(f"[Send error: {exc}]")

    # ── Targets ─────────────────────────────────────────────────────

    def _refresh_targets(self) -> None:
        table = self.query_one("#target-table", DataTable)
        table.clear()
        for t in self._pool.all():
            table.add_row(
                t.mac, t.ssid, str(t.rssi), str(t.channel),
                t.device_source, t.target_type.value,
            )

    def _on_target_event(self, _topic: str, _payload: dict) -> None:
        try:
            self.call_from_thread(self._refresh_targets)
        except Exception:
            pass


def launch_tui(
    device_manager: DeviceManager,
    flash_engine: FlashEngine,
    event_bus: EventBus,
    target_pool: TargetPool,
) -> int:
    """Create and run the Textual TUI."""
    app = CyberControllerTUI(device_manager, flash_engine, event_bus, target_pool)
    app.run()
    return 0
