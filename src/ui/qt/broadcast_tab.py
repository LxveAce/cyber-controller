"""Broadcast tab — the universal fan-out surface (one intent → every connected radio at once).

One big button per intent fires on EVERY connected device at once, each translated into that
firmware's native command. The button live-enables to how many devices can do it, dangerous
actions confirm via the shared safety gate, and STOP ALL is always available. Single-device deep
control (force a firmware, run its own commands, arm gate) lives on the Control tab — this surface
is ONLY the fan-out, so All Devices and Control each have one clear job (QA-1 Option B).
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from PyQt5.QtCore import QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QGridLayout,
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

log = logging.getLogger(__name__)


class _Bridge(QObject):
    """Marshals worker-thread / device-callback events back onto the GUI thread."""
    done = pyqtSignal(str)      # a dispatch summary string
    rebuild = pyqtSignal()      # device connect/disconnect/fw-change -> re-enable the fan-out


class BroadcastBar(QWidget):
    """The universal fan-out surface: one intent → every connected device at once (the Control tab
    owns single-device deep control)."""

    def __init__(self, engine: BroadcastEngine, device_manager, event_bus,
                 settings_loader: Callable = load_settings) -> None:
        super().__init__()
        self._engine = engine
        self._dm = device_manager
        self._bus = event_bus
        self._load_settings = settings_loader
        self._buttons: dict[BroadcastVerb, QPushButton] = {}
        self._advanced_buttons: list[QPushButton] = []
        self._bridge = _Bridge()
        self._bridge.done.connect(self._set_status)
        self._bridge.rebuild.connect(self._refresh_enabled)
        self._build_ui()
        # Re-enable the fan-out reactively when a device connects / disconnects / is forced (the
        # button counts change). Callbacks fire on background threads, so marshal via the bridge.
        for reg in ("on_device_connected", "on_device_disconnected", "on_device_changed"):
            fn = getattr(self._dm, reg, None)
            if callable(fn):
                fn(lambda *_a: self._bridge.rebuild.emit())
        self._refresh_enabled()
        # A slow safety-net timer (events do the real work); re-enable is a cheap recompute.
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
            "Each button runs an action on EVERY connected device at once, translated into that "
            "firmware's own native command. The count shows how many devices can do it; STOP ALL "
            "is always available. For single-device deep control (force a firmware, run its own "
            "commands), use the Control tab.")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # Everything scrolls so the universal grid stays reachable on a small window (the tab was
        # previously un-scrollable and clipped on a deck screen).
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
    def _on_timer(self) -> None:
        self._refresh_enabled()

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
        # A5 #14: with nothing connected every button is greyed out — say why in the status line instead of
        # a wall of dead buttons; clear the hint once a device appears.
        if not any(avail.values()):
            self._set_status("No devices connected — connect one on the Devices tab to broadcast.")
        elif self._status.text().startswith("No devices connected"):
            self._set_status("")

    # ── actions ──────────────────────────────────────────────────────
    def _on_verb_clicked(self, verb: BroadcastVerb) -> None:
        """Universal row: fan *verb* out to every capable device."""
        self._launch(self._engine.plan(verb))

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
                # dispatch() drops devices whose write didn't finish before the deadline, so they'd
                # otherwise vanish from the count — treat any missing device as a timeout failure so a
                # STOP ALL that didn't reach every board is never reported as fully successful.
                timed_out = max(0, len(plan.concrete) - len(results))
                failed = (len(results) - sent) + timed_out
                tail = f" ({timed_out} timed out)" if timed_out else ""
                msg = (f"Broadcast '{plan.action.label}' → {sent} sent, "
                       f"{failed} failed, {len(plan.skipped)} skipped.{tail}")
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
