"""OPERATE Console — the reform's single OPERATE surface (OperateConsole, an OperateTab re-layout).

The Operate-Home decomposition folds four of today's surfaces into ONE console: the single-device
operator console (this class's OperateTab base), the curated one-tap Zone-B verbs
(QuickActionsStrip), the Broadcast fan-out (BroadcastBar), and the BlueJammer-V2 panel
(BlueJammerPanel) — per the pinned REFORM-CONSOLE-SPEC. It SUBCLASSES OperateTab and reuses its
guarded core verbatim (_send, run_curated, ready_for, safe_state, select_device, _refresh,
_rebuild_grid, _on_command_selected, the poll); it only overrides _build_ui (re-parent the SAME
widgets into 3 fixed bands) + three thin hooks. safety.py is untouched: every send still goes
through the base's one guarded _send door.

Bands: TOP = persistent identity + arm (device/fw pickers, link + telemetry, SAFE/ARMED lamp, the
two-factor arm box — the app's single arming point); MIDDLE = <=4 segmented pills (Single-device |
Broadcast | BlueJammer*, the last honest-hidden unless the active device is a bluejammer) over the
working pane; BOTTOM = the persistent activity log. Only data inside a pane scrolls; nav never does.

This module is a PURE ADDITION — it is NOT yet wired into main_window (OPERATE still shows
Home|Control|Macros). Wiring it in + deleting operate_home/domain_grid is the next increment.
"""
from __future__ import annotations

from typing import Any, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.protocols import PROTOCOL_DISPLAY_NAMES, get_protocol
from src.ui.qt.blue_jammer_panel import BlueJammerPanel
from src.ui.qt.broadcast_tab import BroadcastBar
from src.ui.qt.operate_featured import featured_actions
from src.ui.qt.operate_tab import OperateTab
from src.ui.qt.pill_pane_stack import PillPaneStack
from src.ui.qt.quick_actions_strip import QuickActionsStrip


class _ConsoleQuickActionsStrip(QuickActionsStrip):
    """Zone-B strip for Console: an arg tile drives the SHARED right-pane OpPanel (via the console's
    _on_command_selected) instead of the strip's own inline OpPanel, so only ONE OpPanel can ever
    render (spec §Corrections LOW). A no-arg tile still one-taps run_curated."""

    def __init__(self, arg_target, parent: "Optional[QWidget]" = None) -> None:
        self._arg_target = arg_target      # = OperateConsole._on_command_selected
        super().__init__(parent)

    def _on_tile(self, ci: Any) -> None:   # overrides QuickActionsStrip._on_tile
        self._close_panel()                # no-op safety; this strip never owns its own panel
        if getattr(ci, "args", ""):
            self._arg_target(ci)   # -> shared right-pane OpPanel (base _on_command_selected)
        elif self._run_fn is not None:
            self._run_fn(ci)               # one-tap -> run_curated


class OperateConsole(OperateTab):
    """The reform OPERATE Console: OperateTab re-laid into 3 bands + pills (see module docstring).

    Overrides only ``_build_ui`` (the band re-layout) and three thin hooks (``_refresh``,
    ``_apply_operate_layout``, plus the pill/Zone-B sync helpers). All send/arm/grid/poll behaviour
    is the inherited OperateTab machinery, so the single guarded ``_send`` door and every safety
    gate are preserved unchanged."""

    def __init__(self, dm: Any, broadcast_engine: Any, event_bus: Any, ingestor: Any = None,
                 recorder: Any = None, *, dms_seen: "Optional[set]" = None,
                 parent: "Optional[QWidget]" = None) -> None:
        # Pre-super attribute injection: OperateTab.__init__ calls our overridden _build_ui /
        # _refresh / _apply_operate_layout DURING construction (before __init__ resumes), so the
        # state they read must exist first. Legal in Python: __new__ has already allocated self; we
        # set plain attributes here and never call a base method before super().__init__().
        self._broadcast_engine = broadcast_engine
        self._bus = event_bus
        self._zone_b_key: "tuple" = (None, None)   # (port, fw) gate for set_actions
        self._bj_pill_present = False              # BlueJammer-pill presence gate
        self._compact = False                      # densification flag
        super().__init__(dm, ingestor, recorder, dms_seen=dms_seen, parent=parent)

    # ── the 3-band re-layout (replaces OperateTab._build_ui; SAME widgets, new arrangement) ──
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # TOP band — persistent identity + arm (the app's single arming point).
        top = QWidget()
        top_lay = QVBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)
        self._head_row = QHBoxLayout()      # MUST stay a QHBoxLayout (base flips its direction)
        self._head_row.addWidget(QLabel("Device:"))
        self._device_combo = QComboBox()
        self._device_combo.setMinimumWidth(260)
        self._device_combo.currentIndexChanged.connect(self._on_device_changed)
        self._head_row.addWidget(self._device_combo)
        self._head_row.addSpacing(12)
        self._head_row.addWidget(QLabel("Firmware:"))
        self._fw_combo = QComboBox()
        self._fw_combo.addItem("Clear forced firmware", None)
        for key, disp in PROTOCOL_DISPLAY_NAMES.items():
            self._fw_combo.addItem(disp, key)
        self._fw_combo.setToolTip("Force this device to any firmware's command set, even if it may "
                                  "not work on the hardware (full manual control). 'Clear forced "
                                  "firmware' releases the force and keeps the current firmware (it "
                                  "does not re-probe; use the Devices tab to auto-detect).")
        self._fw_combo.currentIndexChanged.connect(self._on_fw_changed)
        self._head_row.addWidget(self._fw_combo)
        self._head_row.addStretch(1)
        top_lay.addLayout(self._head_row)

        self._link_label = QLabel("")
        self._link_label.setTextFormat(Qt.PlainText)
        self._link_label.setWordWrap(True)
        self._link_label.setStyleSheet("font-size:9pt;font-weight:bold;")
        self._link_label.setVisible(False)
        top_lay.addWidget(self._link_label)

        self._telemetry_label = QLabel("")
        self._telemetry_label.setTextFormat(Qt.PlainText)
        self._telemetry_label.setWordWrap(True)
        self._telemetry_label.setStyleSheet("color:#8b949e;font-size:9pt;")
        top_lay.addWidget(self._telemetry_label)

        self._arm_label = QLabel("")       # the always-visible SAFE/ARMED lamp
        self._arm_label.setTextFormat(Qt.PlainText)
        self._arm_label.setStyleSheet("color:#8b949e;font-size:10pt;font-weight:bold;")
        top_lay.addWidget(self._arm_label)

        arm_box = QGroupBox("Offensive-TX arm gate (two-factor)")
        self._arm_box = arm_box
        arm_row = QHBoxLayout(arm_box)
        self._btn_arm = QPushButton("Arm…")
        self._btn_arm.setToolTip("Request arming — the device replies with a one-time token.")
        self._btn_arm.clicked.connect(lambda: self._send("arm"))
        arm_row.addWidget(self._btn_arm)
        self._token_edit = QLineEdit()
        self._token_edit.setPlaceholderText("token from device")
        self._token_edit.setMaximumWidth(160)
        arm_row.addWidget(self._token_edit)
        self._btn_confirm = QPushButton("Confirm token")
        self._btn_confirm.setToolTip("Send the one-time token to complete arming (goes ARMED).")
        self._btn_confirm.clicked.connect(self._on_confirm_token)
        arm_row.addWidget(self._btn_confirm)
        self._btn_disarm = QPushButton("Disarm")
        self._btn_disarm.setToolTip("Hard-disarm: return the device to SAFE. Always available.")
        self._btn_disarm.clicked.connect(lambda: self._send("disarm"))
        arm_row.addWidget(self._btn_disarm)
        arm_row.addStretch(1)
        top_lay.addWidget(arm_box)
        outer.addWidget(top, 0)

        # MIDDLE band — segmented pills over the working pane.
        self._pills = PillPaneStack()
        # Single-device pane: master (Zone-B hero + command grid) / detail (OpPanel), a QSplitter.
        self._grid_box = QGroupBox("Commands")
        self._grid_layout = QVBoxLayout(self._grid_box)
        self._grid_layout.setSpacing(4)
        self._grid_layout.setContentsMargins(6, 6, 6, 6)
        self._op_detail_box = QGroupBox("Selected operation")
        self._op_detail_layout = QVBoxLayout(self._op_detail_box)
        self._op_hint = QLabel("Select a command above to configure and run it.")
        self._op_hint.setStyleSheet("color:#8b949e;")
        self._op_detail_layout.addWidget(self._op_hint)

        self._zone_b_strip = _ConsoleQuickActionsStrip(arg_target=self._on_command_selected)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.addWidget(self._zone_b_strip, 0)      # Zone-B hero row (fixed, above the grid)
        grid_scroll = QScrollArea()
        grid_scroll.setWidgetResizable(True)
        grid_scroll.setFrameShape(QScrollArea.NoFrame)
        # A command grid scrolls only vertically — never sideways (a horizontal scrollbar on buttons
        # reads as broken; wide description buttons keep their full command in the tooltip).
        grid_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        grid_scroll.setWidget(self._grid_box)          # the grid scrolls in-pane
        left_lay.addWidget(grid_scroll, 1)

        right = QScrollArea()
        right.setWidgetResizable(True)
        right.setFrameShape(QScrollArea.NoFrame)
        right.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right.setWidget(self._op_detail_box)

        self._single_pane = QSplitter(Qt.Horizontal)
        self._single_pane.addWidget(left)
        self._single_pane.addWidget(right)
        self._single_pane.setStretchFactor(0, 2)   # command side dominates
        self._single_pane.setStretchFactor(1, 1)   # detail is a slim column that fills on selection
        self._single_pane.setChildrenCollapsible(False)
        self._single_pane.setSizes([720, 360])
        right.setMinimumWidth(320)

        self._broadcast_bar = BroadcastBar(self._broadcast_engine, self._dm, self._bus)
        self._bj_panel = BlueJammerPanel(parent=self)   # arm gate independent of arm_state

        self._pills.set_panes([
            ("single", "Single-device", self._single_pane),
            ("broadcast", "Broadcast", self._broadcast_bar),
        ])
        outer.addWidget(self._pills, 1)

        # BOTTOM band — persistent activity log.
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(120)
        self._log.setPlaceholderText("Sent commands and results appear here.")
        outer.addWidget(self._log, 0)

        self._relayout_operate(force=True)   # seed grid cols / hit target / header — MUST be last

    # ── thin hooks over the inherited machinery ──
    def _refresh(self) -> None:
        super()._refresh()   # base repaint: lamp, arm box, grid, OpPanel, link, telemetry, poll
        self._sync_pills()   # honest-hide/show the BlueJammer pill
        self._sync_zone_b()  # set_actions on (port,fw) change; refresh_readiness every poll
        # Compact densification (critic-LOW): lamp always visible; drop telemetry/link.
        if self._compact:
            self._telemetry_label.setVisible(False)
            self._link_label.setVisible(False)
        else:
            self._telemetry_label.setVisible(True)

    def _apply_operate_layout(self, ol: Any) -> None:
        super()._apply_operate_layout(ol)   # base: head stack, log shrink, arm hit-target, cols
        zb = getattr(self, "_zone_b_strip", None)
        if zb is not None:   # Zone-B tiles honor the deck's hit-target (28pt pointer / 44pt touch)
            zb.set_min_target(int(getattr(ol, "hit_edge_pt", 44) or 44))
        self._compact = bool(getattr(ol, "collapse_chrome", False))
        self._telemetry_label.setVisible(not self._compact)
        if self._compact:
            self._link_label.setVisible(False)   # additionally hidden on a cramped deck; lamp stays

    def _sync_pills(self) -> None:
        """Honest-hide the BlueJammer pill unless the active device's RESOLVED protocol is
        bluejammer (mirrors device_tab._update_bj_panel: protocol_name, not raw firmware)."""
        dev = self._active_device()
        fw = (getattr(dev, "firmware", "") if dev is not None else "") or ""
        try:
            is_bj = get_protocol(fw).protocol_name == "bluejammer"
        except Exception:  # noqa: BLE001 — unknown fw -> not a bluejammer
            is_bj = False
        if is_bj == self._bj_pill_present:
            return
        self._bj_pill_present = is_bj
        prev = self._pills.current()
        panes = [("single", "Single-device", self._single_pane),
                 ("broadcast", "Broadcast", self._broadcast_bar)]
        if is_bj:
            panes.append(("bluejammer", "BlueJammer", self._bj_panel))
        self._pills.set_panes(panes)
        if prev and prev in self._pills.keys():
            self._pills.select(prev)

    def _sync_zone_b(self) -> None:
        """Rebuild Zone B on a (port, firmware) change ONLY (a rebuild on the poll would tear down
        an open OpPanel); refresh readiness cheaply every poll."""
        dev = self._active_device()
        connected = bool(dev is not None and getattr(dev, "connected", False))
        fw = (getattr(dev, "firmware", "") if dev is not None else "") or ""
        key = (self._active_port, fw) if connected else (None, None)
        if key != self._zone_b_key:
            self._zone_b_key = key          # store BEFORE the rebuild -> re-entrant-safe
            self._rebuild_zone_b(self._active_port, fw, connected)
        self._zone_b_strip.refresh_readiness()   # cheap, every ~2s poll — NEVER set_actions here

    def _rebuild_zone_b(self, port: str, fw: str, connected: bool) -> None:
        wiring = (self.run_curated, self._send, self.ready_for, self.safe_state)
        if not connected or not fw:
            self._zone_b_strip.set_actions([], *wiring, supports_arm=False, stop_ci=None)
            return
        self.select_device(port)   # NAV-ONLY prime (base select_device; never sends / arms)
        proto = get_protocol(fw)
        supports_arm = bool(getattr(proto, "supports_arm", False))
        stop_ci = None if supports_arm else self._first_stop_verb(proto)
        self._zone_b_strip.set_actions(featured_actions(proto), *wiring,
                                       supports_arm=supports_arm, stop_ci=stop_ci)

    @staticmethod
    def _first_stop_verb(proto: Any) -> Any:
        """First stop|disarm|reset|off|halt verb in the catalog (Zone-B STOP for non-arming fw)."""
        import re
        try:
            for ci in proto.cached_commands():
                if re.search(r"stop|disarm|reset|off|halt", getattr(ci, "name", "") or "", re.I):
                    return ci
        except Exception:  # noqa: BLE001 — no catalog -> no stop verb (disabled honest chip)
            pass
        return None
