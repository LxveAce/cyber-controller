"""Broadcast tab — a universal fan-out row PLUS a section per connected device.

Top row: one big button per intent that fires on EVERY connected radio at once (each translated into
that firmware's native command). Below it, a section per connected device where you can FORCE the
device to any firmware (exposing that firmware's command set — even if it may not work on the hardware,
for full manual control), see its capabilities, and fire that verb on just that device. Everything
populates reactively off DeviceManager events; dangerous actions confirm via the shared safety gate;
STOP ALL is always available.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import load_settings
from src.core import safety
from src.core.broadcast import BROADCAST_ACTIONS, BroadcastEngine, BroadcastVerb
from src.models.action import ActionCategory
from src.protocols import PROTOCOL_DISPLAY_NAMES

log = logging.getLogger(__name__)


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)


class _Bridge(QObject):
    """Marshals worker-thread / device-callback events back onto the GUI thread."""
    done = pyqtSignal(str)      # a dispatch summary string
    rebuild = pyqtSignal()      # device connected/disconnected/firmware-changed -> repopulate sections


class _DeviceSection(QFrame):
    """One connected device: header + a force-any-firmware combo + capability chips + its verb buttons."""

    def __init__(self, port: str, engine: BroadcastEngine, device_manager,
                 on_verb: Callable[[str, BroadcastVerb], None]) -> None:
        super().__init__()
        self._port = port
        self._engine = engine
        self._dm = device_manager
        self._on_verb = on_verb
        self._syncing = False
        self._last_fw: str | None = None
        self.setObjectName("card")
        v = QVBoxLayout(self)

        head = QHBoxLayout()
        self._title = QLabel()
        self._title.setObjectName("card_title")
        head.addWidget(self._title, 1)
        head.addWidget(QLabel("Firmware:"))
        self._fw_combo = QComboBox()
        self._fw_combo.addItem("Auto-detect", None)   # data None = release any force
        for key, disp in PROTOCOL_DISPLAY_NAMES.items():
            self._fw_combo.addItem(disp, key)
        self._fw_combo.setToolTip("Force this device to any firmware's command set — even if it may not "
                                  "work on the hardware (full manual control).")
        self._fw_combo.currentIndexChanged.connect(self._on_fw_changed)
        head.addWidget(self._fw_combo)
        v.addLayout(head)

        self._caps = QLabel()
        self._caps.setObjectName("muted")
        self._caps.setWordWrap(True)
        v.addWidget(self._caps)

        self._btn_grid = QGridLayout()
        v.addLayout(self._btn_grid)
        self.refresh()

    def _dev(self):
        return self._dm.get_device(self._port)

    def _on_fw_changed(self, _idx: int) -> None:
        if self._syncing:
            return
        dev = self._dev()
        if dev is None:
            return
        data = self._fw_combo.currentData()
        if data is None:   # Auto-detect: release the force, keep the current firmware
            self._dm.set_firmware(self._port, dev.firmware, forced=False)
        else:              # force to the chosen firmware + its command set
            self._dm.set_firmware(self._port, str(data), forced=True)
        # set_firmware fires on_device_changed -> the BroadcastBar refreshes this section.

    def refresh(self) -> None:
        dev = self._dev()
        if dev is None:
            return
        name = getattr(dev, "name", "") or self._port
        health = getattr(dev, "health", "") or ""
        forced = bool(getattr(dev, "firmware_forced", False))
        health_tag = f"  ·  {health}" if health and health != "unknown" else ""
        self._title.setText(f"{self._port} — {name}{health_tag}")

        # Sync the combo to the current state WITHOUT re-firing the change handler.
        self._syncing = True
        idx = self._fw_combo.findData(dev.firmware) if forced else 0
        self._fw_combo.setCurrentIndex(max(0, idx))
        self._syncing = False

        try:
            caps = sorted(dev.capabilities)
        except Exception:  # noqa: BLE001
            caps = []
        cap_txt = ("Capabilities: " + ", ".join(caps)) if caps else "No known capabilities for this firmware"
        self._caps.setText(cap_txt + ("  (firmware forced)" if forced else ""))

        # Only rebuild the verb buttons when the firmware actually changed (avoids flicker on refresh).
        if dev.firmware == self._last_fw:
            return
        self._last_fw = dev.firmware
        _clear_layout(self._btn_grid)
        verbs = self._engine.supported_verbs(dev.firmware)
        cols = 3
        for i, verb in enumerate(verbs):
            action = BROADCAST_ACTIONS[verb]
            btn = QPushButton(f"{action.icon} {action.label}")
            btn.setObjectName("broadcast_btn")
            if action.category == ActionCategory.ATTACK:
                btn.setProperty("danger", "true")
            btn.clicked.connect(lambda _=False, v=verb: self._on_verb(self._port, v))
            self._btn_grid.addWidget(btn, i // cols, i % cols)
        if not verbs:
            hint = QLabel("No broadcast actions for this firmware — force a different firmware above to "
                          "expose its commands.")
            hint.setObjectName("muted")
            hint.setWordWrap(True)
            self._btn_grid.addWidget(hint, 0, 0, 1, cols)


class BroadcastBar(QWidget):
    """A universal fan-out row over every connected device, plus a section per device."""

    def __init__(self, engine: BroadcastEngine, device_manager, event_bus,
                 settings_loader: Callable = load_settings) -> None:
        super().__init__()
        self._engine = engine
        self._dm = device_manager
        self._bus = event_bus
        self._load_settings = settings_loader
        self._buttons: dict[BroadcastVerb, QPushButton] = {}
        self._advanced_buttons: list[QPushButton] = []
        self._sections: dict[str, _DeviceSection] = {}
        self._bridge = _Bridge()
        self._bridge.done.connect(self._set_status)
        self._bridge.rebuild.connect(self._rebuild_sections)
        self._build_ui()
        # Repopulate reactively when a device connects / disconnects / has its firmware forced. The
        # callbacks fire on background threads, so marshal to the GUI thread via the bridge signal.
        for reg in ("on_device_connected", "on_device_disconnected", "on_device_changed"):
            fn = getattr(self._dm, reg, None)
            if callable(fn):
                fn(lambda *_a: self._bridge.rebuild.emit())
        self._refresh_enabled()
        self._rebuild_sections()
        # A slow safety-net timer (events do the real work); the section rebuild is a cheap diff.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_timer)
        self._timer.start(4000)

    # ── layout ───────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        title = QLabel("Unified Action Broadcast")
        title.setObjectName("card_title")
        root.addWidget(title)
        sub = QLabel(
            "The top buttons run an action on EVERY connected device at once (each in its own native "
            "command). Below, each connected device has its own section — force it to any firmware and "
            "fire that firmware's commands on just that device. STOP ALL is always available.")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # Everything scrolls so the universal grid + all per-device sections stay reachable on a small
        # window (the whole tab was previously un-scrollable and clipped on a deck screen).
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        inner = QWidget()
        col = QVBoxLayout(inner)

        col.addWidget(self._section_label("Universal — fan out to all connected devices"))
        grid = QGridLayout()
        cols = 4
        i = 0
        for verb, action in BROADCAST_ACTIONS.items():
            if verb == BroadcastVerb.STOP_ALL:
                continue
            btn = QPushButton(f"{action.icon}\n{action.label}")
            btn.setObjectName("broadcast_btn")
            btn.setMinimumHeight(64)
            if action.category == ActionCategory.ATTACK:
                btn.setProperty("danger", "true")
                self._advanced_buttons.append(btn)
            btn.clicked.connect(lambda _=False, v=verb: self._on_verb_clicked(v))
            self._buttons[verb] = btn
            grid.addWidget(btn, i // cols, i % cols)
            i += 1
        col.addLayout(grid)

        self._stop_btn = QPushButton("\U0001F6D1  STOP ALL")
        self._stop_btn.setObjectName("broadcast_btn")
        self._stop_btn.setProperty("danger", "true")
        self._stop_btn.setMinimumHeight(48)
        self._stop_btn.clicked.connect(lambda: self._on_verb_clicked(BroadcastVerb.STOP_ALL))
        col.addWidget(self._stop_btn)

        col.addWidget(self._section_label("Per device"))
        self._empty_hint = QLabel(
            "No connected devices — connect a board on the Devices tab, then each one appears here.")
        self._empty_hint.setObjectName("muted")
        self._empty_hint.setWordWrap(True)
        col.addWidget(self._empty_hint)
        self._sections_layout = QVBoxLayout()
        col.addLayout(self._sections_layout)

        col.addStretch()
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        self._status = QLabel("")
        self._status.setObjectName("broadcast_status")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

    @staticmethod
    def _section_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("muted")
        return lbl

    # ── reactive per-device sections ─────────────────────────────────
    def _rebuild_sections(self) -> None:
        """Diff the connected devices against the current sections — add/remove changed ports, refresh
        the rest. Cheap + idempotent, so the safety-net timer can call it without churning widgets."""
        try:
            connected = {getattr(d, "port", ""): d for d in self._dm.list_connected()}
        except Exception:  # noqa: BLE001
            return
        for port in list(self._sections):
            if port not in connected:
                self._sections.pop(port).setParent(None)
        for port in connected:
            if port and port not in self._sections:
                sec = _DeviceSection(port, self._engine, self._dm, self._on_device_verb_clicked)
                self._sections[port] = sec
                self._sections_layout.addWidget(sec)
        for sec in self._sections.values():
            try:
                sec.refresh()
            except Exception:  # noqa: BLE001 — one section must never break the panel
                log.debug("device section refresh failed", exc_info=True)
        self._empty_hint.setVisible(not connected)

    def _on_timer(self) -> None:
        self._refresh_enabled()
        self._rebuild_sections()

    # ── interface mode (dual-depth Simple / Pro) ─────────────────────
    def set_ui_mode(self, mode: str) -> None:
        """Simple hides the offensive attack verbs on the universal row; STOP ALL + scans stay. The
        per-device sections are unaffected (they're the power surface). Presentation only."""
        pro = str(mode).lower() != "simple"
        for btn in self._advanced_buttons:
            btn.setVisible(pro)

    # ── live enable + preview (universal row) ────────────────────────
    def _refresh_enabled(self) -> None:
        try:
            avail = self._engine.available_verbs()
        except Exception:  # noqa: BLE001
            log.debug("broadcast available_verbs failed", exc_info=True)
            return
        for verb, btn in self._buttons.items():
            n = avail.get(verb, 0)
            action = BROADCAST_ACTIONS[verb]
            btn.setEnabled(n > 0)
            btn.setText(f"{action.icon}\n{action.label}" + (f"  ·  {n}" if n else ""))
            btn.setToolTip("No connected device can do this." if n == 0 else f"{n} device(s) will run this.")
        try:
            self._stop_btn.setEnabled(avail.get(BroadcastVerb.STOP_ALL, 0) > 0)
        except Exception:  # noqa: BLE001
            pass

    # ── actions ──────────────────────────────────────────────────────
    def _on_verb_clicked(self, verb: BroadcastVerb) -> None:
        """Universal row: fan *verb* out to every capable device."""
        self._launch(self._engine.plan(verb))

    def _on_device_verb_clicked(self, port: str, verb: BroadcastVerb) -> None:
        """A per-device section button: run *verb* on just that one device."""
        self._launch(self._engine.plan_for_port(port, verb))

    def _launch(self, plan) -> None:
        if not plan.concrete:
            self._set_status(f"No connected device supports '{plan.action.label}'.")
            return
        danger = plan.worst_danger
        if safety.should_confirm(danger, self._load_settings()):
            reply = QMessageBox.warning(
                self, "Confirm broadcast", self._warning_text(plan, danger),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                self._set_status(f"Broadcast '{plan.action.label}' cancelled.")
                return
        self._dispatch_async(plan)

    def _dispatch_async(self, plan) -> None:
        # Surface the per-device commands + the outcome in the app-wide activity bus so the persistent
        # terminal reflects broadcasts. The bus is a QObject, so the worker-thread emit queues safely.
        from src.core.activity_log import activity_log
        act = activity_log()
        for c in plan.concrete:
            pre = (" ; ".join(c.pre_commands) + " ; ") if c.pre_commands else ""
            act.emit_line("broadcast", f"[{c.port}] {c.firmware}: {pre}{c.command}")
        if plan.skipped:
            act.emit_line("broadcast",
                          "skipped: " + ", ".join(f"{p} ({f})" for p, f, _ in plan.skipped), "warn")

        def run() -> None:
            try:
                results = self._engine.dispatch(plan, confirmed=True)
                sent = sum(1 for r in results if r.status == "sent")
                failed = len(results) - sent
                msg = (f"Broadcast '{plan.action.label}' → {sent} sent, "
                       f"{failed} failed, {len(plan.skipped)} skipped.")
                act.emit_line("broadcast", msg, "success" if failed == 0 else "warn")
            except Exception as exc:  # never let a dispatch error kill the UI
                msg = f"Broadcast error: {exc}"
                act.emit_line("broadcast", msg, "error")
            self._bridge.done.emit(msg)

        threading.Thread(target=run, daemon=True).start()
        self._set_status(f"Broadcasting '{plan.action.label}' to {len(plan.concrete)} device(s)…")

    @staticmethod
    def _warning_text(plan, danger: str) -> str:
        lines = [safety.lab_only_warning_text(plan.action.label, danger), "", "Will run:"]
        for c in plan.concrete:
            pre = (" ; ".join(c.pre_commands) + " ; ") if c.pre_commands else ""
            lines.append(f"  • {c.port} [{c.firmware}]: {pre}{c.command}")
        if plan.skipped:
            lines.append("")
            lines.append("Skipped: " + ", ".join(f"{p} ({f})" for p, f, _ in plan.skipped))
        return "\n".join(lines)

    def _set_status(self, text: str) -> None:
        self._status.setText(text)
