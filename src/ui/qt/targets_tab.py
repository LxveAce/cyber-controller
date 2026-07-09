"""Targets tab — a focused view of the shared target pool.

Shows every :class:`~src.models.target.Target` currently in the
:class:`~src.core.cross_comm.TargetPool`.  Auto-refreshes on a QTimer and on
``target.*`` :class:`~src.core.cross_comm.EventBus` events.  Because bus
callbacks may fire on worker threads, they are marshalled onto the Qt GUI
thread through a signal bridge before any widget is touched.

Right-clicking a target row opens a context menu of firmware-specific actions
grouped by connected device (requires ``ActionResolver`` from the parallel
action system).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QCursor
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.target_export import export_targets_csv
from src.models.target import Target, TargetType
from src.ui.qt.widgets.signal_bars import SignalBarsDelegate

# "Live view" window: a target seen within this many seconds counts as "currently in range". The Live-view
# toggle filters the table down to just these; the shared pool always keeps the full session regardless.
_LIVE_VIEW_WINDOW_S = 45.0

# Graceful import for action system (created by a parallel agent).
try:
    from src.core.action_resolver import ActionResolver
    from src.core.action_resolver import execute_action as _execute_action_fn
    _HAS_ACTION_RESOLVER = True
except Exception:  # noqa: BLE001
    ActionResolver = None  # type: ignore[assignment,misc]
    _execute_action_fn = None  # type: ignore[assignment]
    _HAS_ACTION_RESOLVER = False

try:
    from src.models.action import ActionCategory
    _HAS_ACTION_MODEL = True
except Exception:  # noqa: BLE001
    ActionCategory = None  # type: ignore[assignment,misc]
    _HAS_ACTION_MODEL = False

log = logging.getLogger(__name__)

# RSSI thresholds for the signal-strength color cue.
_RSSI_STRONG = -60
_RSSI_WEAK = -80

# Unicode category symbols for the context menu.
_CATEGORY_ICONS: dict[str, str] = {
    "attack": "⚡",    # ⚡
    "scan": "\U0001f50d",  # 🔍
    "capture": "\U0001f4e6",  # 📦
    "monitor": "\U0001f4ca",  # 📊
    "utility": "\U0001f527",  # 🔧
}

# QSS for the dark-themed context menu.
_MENU_QSS = """
QMenu {
    background-color: #161b22;
    color: #e6edf3;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 4px 0;
    font-size: 9pt;
}
QMenu::item {
    padding: 6px 20px 6px 12px;
    background: transparent;
}
QMenu::item:selected {
    background-color: #1c2128;
    color: #a371f7;
}
QMenu::item:disabled {
    color: #484f58;
}
QMenu::separator {
    height: 1px;
    background: #30363d;
    margin: 4px 8px;
}
"""


class _BusBridge(QObject):
    """Marshals EventBus callbacks (any thread) onto the Qt GUI thread."""

    changed = pyqtSignal()


class TargetsTab(QWidget):
    """Read-only table of discovered targets with right-click action menu.

    Constructor:
        ``TargetsTab(target_pool, event_bus, device_manager=None, action_resolver=None)``
    """

    # Emitted (with the selected :class:`Target`) when the user asks to reuse a target elsewhere —
    # main_window connects this to fill the Macro tab's variable fields. Same tab-signal → window-
    # connects pattern as SettingsTab.check_updates_requested; no global / new transport.
    fill_macro_requested = pyqtSignal(object)

    _COLUMNS = ["Type", "SSID", "MAC", "RSSI", "Ch", "Source", "Enc", "Last Seen"]
    # Abbreviated / technical columns hidden in Simple mode (kept in Pro). Indices into _COLUMNS.
    _ADVANCED_COLUMNS = (4, 5, 6)  # Ch, Source, Enc

    def __init__(
        self,
        target_pool: TargetPool,
        event_bus: EventBus,
        device_manager: DeviceManager | None = None,
        action_resolver: "ActionResolver | None" = None,
    ) -> None:
        super().__init__()
        self._pool = target_pool
        self._bus = event_bus
        self._dm = device_manager
        self._resolver = action_resolver

        self._bridge = _BusBridge()
        self._bridge.changed.connect(self._refresh, Qt.QueuedConnection)

        self._build_ui()
        self._subscribe_bus()
        self._refresh()

        # Periodic safety-net refresh (also covers age-driven RSSI changes).
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(3000)

    # ── Layout ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        container = QWidget()
        root = QVBoxLayout(container)

        # Search / filter bar
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Filter by SSID, MAC, or type...")
        self._search_input.textChanged.connect(self._apply_filter)
        root.addWidget(self._search_input)

        # Toolbar row
        toolbar = QHBoxLayout()
        self._count_label = QLabel("0 targets")
        self._count_label.setObjectName("muted")
        self._count_label.setWordWrap(True)
        toolbar.addWidget(self._count_label)
        toolbar.addStretch()
        self._use_macro_btn = QPushButton("Use as Macro Target")
        self._use_macro_btn.setToolTip(
            "Fill the Macros tab's variable fields (MAC / SSID / channel) from the selected target."
        )
        self._use_macro_btn.clicked.connect(self._on_use_as_macro_target)
        toolbar.addWidget(self._use_macro_btn)
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.setToolTip("Rebuild the table from the shared target pool now.")
        self._refresh_btn.clicked.connect(self._refresh)
        self._export_btn = QPushButton("Export CSV…")
        self._export_btn.setToolTip(
            "Export EVERY target seen this session (APs, clients, BLE) to a CSV file.\n"
            "Exports the whole shared pool — ignores the search box and the Live-view filter."
        )
        self._export_btn.clicked.connect(self._on_export_csv)
        self._clear_btn = QPushButton("Clear All")
        self._clear_btn.setToolTip("Remove every target from the shared pool (affects all tabs).")
        self._clear_btn.clicked.connect(self._on_clear)
        toolbar.addWidget(self._refresh_btn)
        toolbar.addWidget(self._export_btn)
        toolbar.addWidget(self._clear_btn)
        self._live_view = QCheckBox("Live view")
        self._live_view.setToolTip(
            "Live view: show only targets seen in the last "
            f"{int(_LIVE_VIEW_WINDOW_S)}s — i.e. what's currently in range.\n"
            "Off (default): show everything discovered this session. The shared pool always keeps every "
            "target; this only filters what THIS table displays."
        )
        self._live_view.toggled.connect(lambda _checked: self._apply_filter())
        toolbar.addWidget(self._live_view)
        root.addLayout(toolbar)

        # Table
        self._table = QTableWidget(0, len(self._COLUMNS))
        self._table.setHorizontalHeaderLabels(self._COLUMNS)
        # Header tooltips — spell out the abbreviated columns.
        for _col, _tip in enumerate((
            "Kind of target (access point, client, or BLE device).",
            "Wi-Fi network name (SSID), when known.",
            "Hardware (MAC) address of the target.",
            "Signal strength (RSSI) in dBm — closer to 0 is stronger.",
            "Wi-Fi channel the target was seen on.",
            "Device (port) that discovered this target.",
            "Encryption / security of the network (e.g. WPA2, open).",
            "When this target was last seen.",
        )):
            _hdr = self._table.horizontalHeaderItem(_col)
            if _hdr is not None:
                _hdr.setToolTip(_tip)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setMinimumHeight(100)

        # Use SignalBarsDelegate for the RSSI column (index 3)
        self._signal_delegate = SignalBarsDelegate(self._table)
        self._table.setItemDelegateForColumn(3, self._signal_delegate)

        # Right-click context menu for target actions
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)

        root.addWidget(self._table, stretch=1)

        # Empty-state guidance — shown while the pool has no targets.
        self._empty_hint = QLabel(
            "No targets yet — run a scan from Operate ▸ Broadcast, or send a scan command "
            "on the Devices tab. Discovered targets land here in the shared pool."
        )
        self._empty_hint.setObjectName("muted")
        self._empty_hint.setWordWrap(True)
        root.addWidget(self._empty_hint)

        scroll.setWidget(container)
        outer.addWidget(scroll)

    # ── EventBus wiring ──────────────────────────────────────────────

    def _subscribe_bus(self) -> None:
        for topic in ("target.added", "target.updated", "target.removed", "target.cleared"):
            self._bus.subscribe(topic, self._bus_callback)

    def _bus_callback(self, _topic: str, _payload: dict[str, Any]) -> None:
        """EventBus callback (any thread) — request a GUI-thread refresh."""
        self._bridge.changed.emit()

    # ── Refresh ──────────────────────────────────────────────────────

    def _refresh(self) -> None:
        """Rebuild the table from :meth:`TargetPool.all`."""
        targets = self._pool.all()

        # Disable sorting while repopulating to avoid row-index churn.
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(targets))
        for row, t in enumerate(targets):
            self._table.setItem(row, 0, QTableWidgetItem(t.target_type.value))
            self._table.setItem(row, 1, QTableWidgetItem(t.ssid or ""))
            self._table.setItem(row, 2, QTableWidgetItem(t.mac or ""))

            # Store RSSI / Channel as ints in the DisplayRole so the Ch and RSSI headers sort
            # numerically (1, 2, 10) instead of lexicographically ("1", "10", "2").
            rssi_item = QTableWidgetItem()
            rssi_item.setData(Qt.DisplayRole, int(t.rssi))
            self._table.setItem(row, 3, rssi_item)

            chan_item = QTableWidgetItem()
            chan_item.setData(Qt.DisplayRole, int(t.channel))
            self._table.setItem(row, 4, chan_item)
            self._table.setItem(row, 5, QTableWidgetItem(t.device_source or ""))
            self._table.setItem(row, 6, QTableWidgetItem(t.encryption or ""))
            seen_item = QTableWidgetItem(self._fmt_time(t.last_seen))
            # Stash epoch seconds on the Last Seen cell so Live view can age rows out by freshness.
            seen_item.setData(Qt.UserRole, self._epoch(t.last_seen))
            self._table.setItem(row, 7, seen_item)
        self._table.setSortingEnabled(True)

        self._count_label.setText(f"{len(targets)} target{'s' if len(targets) != 1 else ''}")
        self._empty_hint.setVisible(len(targets) == 0)

        # Re-apply search + Live-view filter (the 3s safety-net timer also ages Live-view rows out here).
        self._apply_filter()

    def _apply_filter(self, _text: Any = None) -> None:
        """Show/hide rows: search text (Type/SSID/MAC) AND, when Live view is on, freshness.

        Live view keeps only targets seen within the last :data:`_LIVE_VIEW_WINDOW_S` seconds ("what's
        currently in range"); the shared pool still holds the whole session. The argument is ignored
        (``textChanged`` passes it); search text and toggle state are read from the widgets.
        """
        filter_text = self._search_input.text().strip().lower()
        live = self._live_view.isChecked()
        now = time.time()
        for row in range(self._table.rowCount()):
            match = True
            if filter_text:
                # Match against Type (col 0), SSID (col 1), MAC (col 2).
                match = any(
                    (self._table.item(row, col) is not None)
                    and filter_text in (self._table.item(row, col).text() or "").lower()
                    for col in (0, 1, 2)
                )
            if match and live:
                seen_item = self._table.item(row, 7)
                ts = seen_item.data(Qt.UserRole) if seen_item is not None else None
                if ts is not None and (now - float(ts)) > _LIVE_VIEW_WINDOW_S:
                    match = False
            self._table.setRowHidden(row, not match)

    @staticmethod
    def _epoch(last_seen: Any) -> "float | None":
        """Best-effort epoch seconds for a target's last_seen (datetime / number / str), else None."""
        try:
            return float(last_seen.timestamp())  # tz-aware datetime
        except (AttributeError, TypeError, ValueError, OSError):
            pass
        try:
            return float(last_seen)  # already numeric
        except (TypeError, ValueError):
            return None

    def _on_clear(self) -> None:
        reply = QMessageBox.question(
            self,
            "Clear Targets",
            "Remove all targets from the shared pool?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._pool.clear()
            self._refresh()

    # ── Scan-to-export ───────────────────────────────────────────────

    def export_csv_to(self, path: str) -> int:
        """Write EVERY target in the shared pool (the whole session) to *path* as CSV; return the row count.

        Exports the full pool regardless of the search box or the Live-view filter — "scan to export all".
        The write itself is unit-tested via :func:`src.core.target_export.export_targets_csv`; the dialog
        wrapper below is not.
        """
        return export_targets_csv(self._pool.all(), path)

    def _on_export_csv(self) -> None:
        targets = self._pool.all()
        if not targets:
            QMessageBox.information(
                self, "Export CSV", "No targets to export yet — connect a device and run a scan first."
            )
            return
        from pathlib import Path

        default = str(Path.home() / "cyber-controller-targets.csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export targets to CSV", default, "CSV (*.csv);;All files (*)"
        )
        if not path:
            return
        try:
            n = self.export_csv_to(path)
        except OSError as exc:
            QMessageBox.warning(self, "Export CSV", f"Could not write CSV:\n{exc}")
            return
        QMessageBox.information(
            self, "Export CSV", f"Exported {n} target{'s' if n != 1 else ''} to:\n{path}"
        )

    # ── Interface mode (dual-depth Simple / Pro) ─────────────────────

    def set_ui_mode(self, mode: str) -> None:
        """Simple = a lean target list: hide the abbreviated technical columns (Ch / Source / Enc)
        and the bulk-destructive "Clear All" button. Pro restores the full table. The right-click
        action menu (the tab's purpose) and per-row "Use as Macro Target" stay in both modes."""
        pro = str(mode).lower() != "simple"
        for col in self._ADVANCED_COLUMNS:
            self._table.setColumnHidden(col, not pro)
        if getattr(self, "_clear_btn", None) is not None:
            self._clear_btn.setVisible(pro)

    # ── Context menu ────────────────────────────────────────────────

    def _target_from_row(self, row: int) -> Target | None:
        """Reconstruct a Target from the table row data."""
        try:
            type_item = self._table.item(row, 0)
            ssid_item = self._table.item(row, 1)
            mac_item = self._table.item(row, 2)
            rssi_item = self._table.item(row, 3)
            ch_item = self._table.item(row, 4)
            source_item = self._table.item(row, 5)
            enc_item = self._table.item(row, 6)

            if not mac_item:
                return None

            target_type_str = type_item.text() if type_item else "ap"
            try:
                target_type = TargetType(target_type_str.lower())
            except ValueError:
                target_type = TargetType.AP

            rssi = 0
            if rssi_item:
                try:
                    rssi = int(rssi_item.text())
                except (ValueError, TypeError):
                    pass

            channel = 0
            if ch_item:
                try:
                    channel = int(ch_item.text())
                except (ValueError, TypeError):
                    pass

            return Target(
                mac=mac_item.text() or "",
                target_type=target_type,
                ssid=ssid_item.text() if ssid_item else "",
                rssi=rssi,
                channel=channel,
                device_source=source_item.text() if source_item else "",
                encryption=enc_item.text() if enc_item else "",
            )
        except Exception:
            log.exception("Failed to reconstruct target from row %d", row)
            return None

    def _pooled_target_from_row(self, row: int) -> Target | None:
        """Row -> the AUTHORITATIVE pooled Target, falling back to the row-reconstructed one.

        The pooled object carries extra['index'] (and the live device_source), which the
        row-reconstructed Target omits. Without this, index-gated actions (e.g. the BW16
        'Deauth (this index)' -> AT+DEAUTHIDX=n) were silently dropped in the context menu even
        though the Network tab offered them (it resolves against the real pool objects)."""
        target = self._target_from_row(row)
        if target is None:
            return None
        if self._pool is not None:
            pooled = self._pool.get(target.key)
            if pooled is not None:
                return pooled
        return target

    def _on_use_as_macro_target(self) -> None:
        """Toolbar affordance: push the selected row's target into the Macro tab's variable fields.

        Handles the no-selection case gracefully with a status-bar hint (no emission, no dialog)."""
        row = self._table.currentRow()
        if row < 0:
            self._notify("Select a target row first, then 'Use as Macro Target'.")
            return
        target = self._pooled_target_from_row(row)
        if target is None:
            self._notify("Could not read the selected target.")
            return
        self.fill_macro_requested.emit(target)

    def _notify(self, msg: str) -> None:
        """Show a transient hint on the main window's status bar, when there is one."""
        window = self.window()
        if window is not None and hasattr(window, "statusBar"):
            window.statusBar().showMessage(msg, 4000)

    def _on_context_menu(self, pos) -> None:
        """Build and show the right-click context menu for a target row."""
        item = self._table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        target = self._pooled_target_from_row(row)
        if target is None:
            return

        menu = QMenu(self)
        menu.setStyleSheet(_MENU_QSS)

        # Header label (non-clickable)
        label = target.ssid or target.mac or "Unknown"
        mac_display = target.mac or "?"
        header_action = menu.addAction(f'Actions for "{label}" ({mac_display})')
        header_action.setEnabled(False)
        menu.addSeparator()

        actions_added = False

        # Try to resolve actions via the ActionResolver
        if self._resolver is not None and _HAS_ACTION_RESOLVER:
            try:
                resolved = self._resolver.resolve(target)
                if resolved:
                    for port, action_list in resolved.items():
                        # Build a submenu per device/port
                        device = self._dm.get_device(port) if self._dm else None
                        fw_label = ""
                        if device and device.firmware:
                            fw_label = device.firmware
                        elif device and device.protocol and device.protocol.value != "unknown":
                            fw_label = device.protocol.value.capitalize()
                        else:
                            fw_label = "Device"
                        submenu = menu.addMenu(f"{port} ({fw_label})")
                        submenu.setStyleSheet(_MENU_QSS)
                        for action in action_list:
                            # Determine icon from category
                            icon = ""
                            if _HAS_ACTION_MODEL and hasattr(action, "category"):
                                cat_name = action.category.value if hasattr(action.category, "value") else str(action.category)
                                icon = _CATEGORY_ICONS.get(cat_name.lower(), "")
                            elif hasattr(action, "category"):
                                icon = _CATEGORY_ICONS.get(str(action.category).lower(), "")

                            action_name = getattr(action, "name", str(action))
                            display = f"{icon} {action_name}" if icon else action_name

                            act = submenu.addAction(display)
                            # Capture action and port in lambda closure
                            _action = action
                            _port = port
                            act.triggered.connect(
                                lambda checked=False, a=_action, p=_port: self._execute_action(a, p, target)
                            )
                        actions_added = True
            except Exception:
                log.exception("ActionResolver.resolve() failed")

        if not actions_added:
            no_act = menu.addAction("No actions available — connect a device first")
            no_act.setEnabled(False)

        # Always-available cross-tab + clipboard actions
        menu.addSeparator()
        use_macro = menu.addAction("Use as macro target")
        use_macro.setToolTip("Fill the Macros tab's MAC / SSID / channel fields from this target.")
        use_macro.triggered.connect(lambda: self.fill_macro_requested.emit(target))
        copy_mac = menu.addAction("Copy MAC")
        copy_mac.triggered.connect(lambda: self._copy_to_clipboard(target.mac))
        copy_ssid = menu.addAction("Copy SSID")
        copy_ssid.triggered.connect(lambda: self._copy_to_clipboard(target.ssid))

        menu.exec_(QCursor.pos())

    def _execute_action(self, action: Any, port: str, target: Target) -> None:
        """Execute a resolved action against a target on a specific device port."""
        action_name = getattr(action, "name", str(action))
        try:
            # A right-click target action here (Deauth AP / Karma Clone / Probe Flood / Rickroll Beacon)
            # is a real attack send, so it must clear the SAME "Confirm before dangerous commands" gate
            # every sibling send surface uses (device_tab._on_send, network_tab._run_target_action) —
            # otherwise the Targets tab silently bypasses the marketed safety toggle. Classify the rendered
            # command AND its pre-commands (worst wins) and floor an ATTACK-category action at lab-only, so
            # a keyword-free attack template is still gated. Inside the try -> a gate error fails closed.
            from src.config.settings import load_settings
            from src.core import safety
            from src.models.action import ActionCategory
            _cmds = [getattr(action, "command_template", "") or ""]
            _cmds += list(getattr(action, "pre_commands", None) or [])
            _danger = safety.worst_of(*(safety.classify(c) for c in _cmds))
            if getattr(action, "category", None) == ActionCategory.ATTACK:
                _danger = safety.worst_of(_danger, safety.LAB_ONLY)
            if safety.should_confirm(_danger, load_settings()):
                reply = QMessageBox.warning(
                    self, "Confirm dangerous command",
                    safety.lab_only_warning_text(getattr(action, "command_template", ""), _danger),
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return
            # Prefer the proper execute_action function from the action_resolver module.
            if _HAS_ACTION_RESOLVER and _execute_action_fn is not None and self._dm is not None:
                success = _execute_action_fn(
                    action, port, self._dm, event_bus=None,
                )
                status = "success" if success else "failed"
                detail = getattr(action, "command_template", "")
            elif self._dm is not None:
                # Fallback: send the action's command directly
                command = getattr(action, "command", None) or getattr(action, "command_template", "")
                if callable(command):
                    command = command(target)
                elif "{" in command:
                    command = command.replace("{mac}", target.mac or "")
                    command = command.replace("{ssid}", target.ssid or "")
                    command = command.replace("{channel}", str(target.channel))
                conn = self._dm.get_connection(port)
                if conn and conn.is_connected:
                    conn.write(command)
                    status = "sent"
                    detail = command
                else:
                    status = "failed"
                    detail = f"No active connection on {port}"
            else:
                status = "failed"
                detail = "No device manager available"

            # Publish result on the event bus (we handle it ourselves since we
            # passed event_bus=None to execute_action to avoid double-publishing).
            self._bus.publish("action.executed", {
                "action": action_name,
                "port": port,
                "target_mac": target.mac,
                "target_ssid": target.ssid,
                "status": status,
                "detail": detail,
            })

            # Update status bar via parent window
            window = self.window()
            if window and hasattr(window, "statusBar"):
                window.statusBar().showMessage(
                    f"Action '{action_name}' {status} on {port}: {detail}", 5000
                )

        except Exception as exc:
            log.exception("Failed to execute action on %s", port)
            self._bus.publish("action.executed", {
                "action": action_name,
                "port": port,
                "target_mac": target.mac,
                "target_ssid": target.ssid,
                "status": "failed",
                "detail": str(exc),
            })
            window = self.window()
            if window and hasattr(window, "statusBar"):
                window.statusBar().showMessage(f"Action failed: {exc}", 5000)

    @staticmethod
    def _copy_to_clipboard(text: str) -> None:
        """Copy text to the system clipboard."""
        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText(text or "")

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _rssi_color(rssi: int) -> QColor:
        if rssi >= _RSSI_STRONG:
            return QColor("#3fb950")  # strong — green
        if rssi >= _RSSI_WEAK:
            return QColor("#ffd700")  # medium — yellow
        return QColor("#ff8c00")      # weak — orange

    @staticmethod
    def _fmt_time(last_seen: Any) -> str:
        try:
            return last_seen.strftime("%H:%M:%S")
        except (AttributeError, ValueError):
            return str(last_seen)

    # ── Qt overrides ─────────────────────────────────────────────────

    def showEvent(self, event) -> None:  # noqa: N802 — Qt naming
        super().showEvent(event)
        self._refresh()
