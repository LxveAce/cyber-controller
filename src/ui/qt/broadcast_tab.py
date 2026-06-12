"""Broadcast tab — one big button per intent; each fires the action on EVERY connected radio
at once, translated into that firmware's native command. Dangerous actions confirm once for the
whole fan-out (reusing the safety gate); STOP ALL is always available and never gated."""
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
    QVBoxLayout,
    QWidget,
)

from src.config.settings import load_settings
from src.core import safety
from src.core.broadcast import BROADCAST_ACTIONS, BroadcastEngine, BroadcastVerb
from src.models.action import ActionCategory

log = logging.getLogger(__name__)


class _Bridge(QObject):
    """Marshals a worker-thread dispatch result back onto the GUI thread."""
    done = pyqtSignal(str)


class BroadcastBar(QWidget):
    """The big-button action row over every connected device."""

    def __init__(self, engine: BroadcastEngine, device_manager, event_bus,
                 settings_loader: Callable = load_settings) -> None:
        super().__init__()
        self._engine = engine
        self._dm = device_manager
        self._bus = event_bus
        self._load_settings = settings_loader
        self._buttons: dict[BroadcastVerb, QPushButton] = {}
        self._bridge = _Bridge()
        self._bridge.done.connect(self._set_status)
        self._build_ui()
        self._refresh_enabled()
        # Re-plan periodically so buttons enable/disable as devices connect/disconnect.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_enabled)
        self._timer.start(3000)

    # ── layout ───────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        title = QLabel("Unified Action Broadcast")
        title.setObjectName("card_title")
        root.addWidget(title)
        sub = QLabel(
            "One click runs the action on EVERY connected device at once, each translated into its "
            "own native command (results merge into the shared target pool). Dangerous actions "
            "confirm once for the whole fan-out; STOP ALL is always available.")
        sub.setWordWrap(True)
        root.addWidget(sub)

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
            btn.clicked.connect(lambda _=False, v=verb: self._on_verb_clicked(v))
            self._buttons[verb] = btn
            grid.addWidget(btn, i // cols, i % cols)
            i += 1
        root.addLayout(grid)

        self._stop_btn = QPushButton("\U0001F6D1  STOP ALL")
        self._stop_btn.setObjectName("broadcast_btn")
        self._stop_btn.setProperty("danger", "true")
        self._stop_btn.setMinimumHeight(48)
        self._stop_btn.clicked.connect(lambda: self._on_verb_clicked(BroadcastVerb.STOP_ALL))
        root.addWidget(self._stop_btn)

        self._status = QLabel("")
        self._status.setObjectName("broadcast_status")
        self._status.setWordWrap(True)
        root.addWidget(self._status)
        root.addStretch()

    # ── live enable + preview ────────────────────────────────────────
    def _refresh_enabled(self) -> None:
        try:
            avail = self._engine.available_verbs()
        except Exception:
            log.debug("broadcast available_verbs failed", exc_info=True)
            return
        for verb, btn in self._buttons.items():
            n = avail.get(verb, 0)
            action = BROADCAST_ACTIONS[verb]
            btn.setEnabled(n > 0)
            btn.setText(f"{action.icon}\n{action.label}" + (f"  ·  {n}" if n else ""))
            if n == 0:
                btn.setToolTip("No connected device can do this.")
            else:
                try:
                    preview = ", ".join(f"{c.port} {c.firmware}:{c.command}"
                                        for c in self._engine.plan(verb).concrete)
                except Exception:
                    preview = ""
                btn.setToolTip(f"{n} device(s): {preview}")
        try:
            self._stop_btn.setEnabled(bool(self._dm.list_connected()))
        except Exception:
            pass

    # ── actions ──────────────────────────────────────────────────────
    def _on_verb_clicked(self, verb: BroadcastVerb) -> None:
        plan = self._engine.plan(verb)
        if not plan.concrete:
            self._set_status(f"No connected device supports '{BROADCAST_ACTIONS[verb].label}'.")
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
        def run() -> None:
            try:
                results = self._engine.dispatch(plan, confirmed=True)
                sent = sum(1 for r in results if r.status == "sent")
                msg = (f"Broadcast '{plan.action.label}' → {sent} sent, "
                       f"{len(results) - sent} failed, {len(plan.skipped)} skipped.")
            except Exception as exc:  # never let a dispatch error kill the UI
                msg = f"Broadcast error: {exc}"
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
