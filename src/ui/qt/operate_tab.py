"""Operate console (B16) — a focused operator surface for a single connected device.

This is the button-driven counterpart to the Devices tab's free-text terminal: a 2-second
status-poll header, a prominent SAFE/ARMED lamp, the LxveOS two-factor arm-token toggle, and a
catalog-driven command grid whose offensive-TX buttons stay disabled until the device is ARMED.

It is a POLL-DRIVEN, READ-ONLY VIEW of shared ``Device`` state: the TargetIngestor mutates the
Device on the serial reader thread; this tab only reads it (on the Qt thread, via the timer or a
forwarded line slot) and repaints. It never opens its own serial subscription — that would
double-parse and, at a Dead-Man's-Switch prompt, risk a wrong-attempt wipe (see ``dms_seen``).

Writes go through the same guarded path the Devices tab uses (``SerialConnection.write`` rejects
embedded control chars; the safety layer LABELS/warns on dangerous verbs but never blocks). The arm
gate itself transmits nothing — it just toggles whether the firmware will honour a later TX op.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import load_settings
from src.core import safety
from src.protocols import PROTOCOL_DISPLAY_NAMES, get_protocol
from src.ui.qt.arm_lamp import arm_lamp_render

log = logging.getLogger(__name__)


class OperateTab(QWidget):
    """Single-device operator console: status header + ARM/SAFE lamp + two-factor arm toggle + a
    per-firmware, TX-gated command grid. Reads shared Device state on a 2s poll; writes via the same
    guarded ``SerialConnection.write`` path as the Devices tab. Opens no serial subscription itself.
    """

    def __init__(self, dm: Any, ingestor: Any = None, recorder: Any = None, *,
                 dms_seen: Optional[set] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._dm = dm
        self._ingestor = ingestor
        self._recorder = recorder
        # Shared with the Devices tab so a port that has shown a Dead-Man's-Switch unlock prompt is
        # NEVER auto-polled here either (a stray write there can count as a failed attempt and trip
        # a wipe). main_window passes device_tab._dms_seen; tests pass their own set.
        self._dms_seen = dms_seen if dms_seen is not None else set()
        self._active_port: str = ""
        self._grid_fw: str = ""        # firmware the grid was built for (skip rebuilds)
        self._tx_buttons: list = []    # offensive-TX buttons (danger != "") — on only when ARMED
        self._safe_buttons: list = []  # non-TX buttons — enabled whenever the device is connected
        self._last_arm_state: Optional[str] = None
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._poll_tick)
        self._reload_devices()
        self._refresh()

    # ── lifecycle: the poll runs only while the tab is visible ────────────
    def showEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        super().showEvent(ev)
        self._reload_devices()
        self._refresh()
        self._timer.start()

    def hideEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        super().hideEvent(ev)
        self._timer.stop()

    # ── UI ────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        _scroll = QScrollArea(self)
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QScrollArea.NoFrame)
        _content = QWidget()
        _scroll.setWidget(_content)
        _outer = QVBoxLayout(self)
        _outer.setContentsMargins(0, 0, 0, 0)
        _outer.addWidget(_scroll)
        root = QVBoxLayout(_content)

        # Header: device picker + telemetry line
        head = QHBoxLayout()
        head.addWidget(QLabel("Device:"))
        self._device_combo = QComboBox()
        self._device_combo.setMinimumWidth(260)
        self._device_combo.currentIndexChanged.connect(self._on_device_changed)
        head.addWidget(self._device_combo)
        head.addStretch(1)
        root.addLayout(head)

        self._telemetry_label = QLabel("")
        self._telemetry_label.setTextFormat(Qt.PlainText)
        self._telemetry_label.setWordWrap(True)
        self._telemetry_label.setStyleSheet("color:#8b949e;font-size:11px;")
        root.addWidget(self._telemetry_label)

        # SAFE/ARMED lamp
        self._arm_label = QLabel("")
        self._arm_label.setTextFormat(Qt.PlainText)
        self._arm_label.setStyleSheet("color:#8b949e;font-size:13px;font-weight:bold;")
        root.addWidget(self._arm_label)

        # Two-factor arm toggle. Only shown for firmwares that actually arm (LxveOS); hidden for firmwares
        # with no arm concept, where it would just be three dead buttons (see _refresh).
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
        root.addWidget(arm_box)

        # Command grid (rebuilt per active-device firmware)
        self._grid_box = QGroupBox("Commands")
        self._grid_layout = QVBoxLayout(self._grid_box)
        root.addWidget(self._grid_box)

        # Small activity log (sent commands / errors — this tab is button-driven, not a terminal)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(120)
        self._log.setPlaceholderText("Sent commands and results appear here.")
        root.addWidget(self._log)
        root.addStretch(1)

    # ── device selection ──────────────────────────────────────────────────
    def _reload_devices(self) -> None:
        """Repopulate the device picker (connected first), preserving the current selection if it is
        still present. Cheap; safe to call every show/poll."""
        try:
            devices = self._dm.list_devices()
        except Exception:
            devices = []
        devices.sort(key=lambda d: (not getattr(d, "connected", False), getattr(d, "port", "")))
        want = self._active_port
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        for d in devices:
            port = getattr(d, "port", "")
            mark = "" if getattr(d, "connected", False) else "  (disconnected)"
            fw = getattr(d, "firmware", "") or "?"
            self._device_combo.addItem(f"{port} — {fw}{mark}", port)
        idx = self._device_combo.findData(want) if want else -1
        if idx < 0 and self._device_combo.count():
            idx = 0
        if idx >= 0:
            self._device_combo.setCurrentIndex(idx)
            self._active_port = self._device_combo.itemData(idx) or ""
        else:
            self._active_port = ""
        self._device_combo.blockSignals(False)

    def _on_device_changed(self, _idx: int) -> None:
        self._active_port = self._device_combo.currentData() or ""
        self._last_arm_state = None  # force a lamp repaint for the newly-selected device
        self._refresh()

    def _active_device(self):
        port = self._active_port
        if not port:
            return None
        try:
            return self._dm.get_device(port)
        except Exception:
            return None

    def _active_supports_arm(self) -> bool:
        """Whether the active device's firmware implements the two-factor ARM handshake. Only those
        firmwares gate offensive-TX behind the armed lockout; the rest are confirm-gated instead."""
        dev = self._active_device()
        fw = (getattr(dev, "firmware", "") if dev is not None else "") or ""
        if not fw:
            return False
        try:
            return bool(getattr(get_protocol(fw), "supports_arm", False))
        except Exception:
            return False

    # ── command grid ──────────────────────────────────────────────────────
    def _rebuild_grid(self, firmware: str) -> None:
        """Rebuild the command grid for *firmware*'s catalog, grouped by category. Offensive-TX verbs go
        into ``_tx_buttons``, the rest ``_safe_buttons`` — split by :func:`safety.classify` (the SAME
        check the send path enforces) so a dangerous verb with no explicit ``danger=`` field (Marauder /
        ESP32-DIV catalogs set none) is still correctly flagged, and the button state matches enforcement."""
        self._grid_fw = firmware
        self._tx_buttons = []
        self._safe_buttons = []
        # Personalize the group title with the firmware so the operator sees whose buttons these are.
        disp = PROTOCOL_DISPLAY_NAMES.get(firmware, firmware) if firmware else ""
        self._grid_box.setTitle(f"Commands — {disp}" if disp else "Commands")
        # Clear the existing grid contents.
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        proto = get_protocol(firmware)
        commands = list(proto.cached_commands())  # copy — the cached list is shared/read-only
        if not commands:
            self._grid_layout.addWidget(QLabel("No command catalog for this device's firmware."))
            return
        # Group by category, preserving first-seen order.
        groups: "dict[str, list]" = {}
        for ci in commands:
            groups.setdefault(getattr(ci, "category", "") or "Other", []).append(ci)
        for category, cmds in groups.items():
            box = QGroupBox(category)
            grid = QGridLayout(box)
            for i, ci in enumerate(cmds):
                btn = QPushButton(ci.name)
                # The authoritative danger level (same call the send path uses), not the raw catalog field.
                danger = safety.classify(ci.name, ci)
                tip = ci.description or ci.name
                if getattr(ci, "args", ""):
                    tip += f"\nargs: {ci.args}"
                if danger:
                    tip += f"\n[{danger}]"
                    # Visual danger cue so a deauth/jam verb reads differently from a scan at a glance.
                    color = "#f85149" if danger == "illegal-tx" else "#d29922"
                    btn.setStyleSheet(f"QPushButton {{ border: 1px solid {color}; color: {color}; }}")
                btn.setToolTip(tip)
                btn.clicked.connect(lambda _checked=False, c=ci: self._on_command_button(c))
                grid.addWidget(btn, i // 3, i % 3)
                if danger:
                    self._tx_buttons.append(btn)
                else:
                    self._safe_buttons.append(btn)
            self._grid_layout.addWidget(box)

    def _on_command_button(self, ci) -> None:
        """A grid button was clicked. If the command takes arguments, prompt for the full argument
        string (seeded with the verb); otherwise send the bare verb."""
        cmd = ci.name
        if getattr(ci, "args", ""):
            text, ok = QInputDialog.getText(
                self, f"{ci.name} — arguments", f"{ci.description}\n\nargs: {ci.args}",
                QLineEdit.Normal, ci.name + " ",
            )
            if not ok:
                return
            cmd = text.strip()
            if not cmd:
                return
        self._send(cmd, ci)

    # ── arm toggle ────────────────────────────────────────────────────────
    def _on_confirm_token(self) -> None:
        token = self._token_edit.text().strip()
        if not token:
            self._append_log("[enter the token the device printed after Arm…]")
            return
        self._send(f"arm {token}")

    # ── send path (mirrors DeviceTab._on_send's guarded core) ─────────────
    def _send(self, cmd: str, ci: Any = None) -> None:
        """Write *cmd* to the active device's connection through the guarded path: safety LABEL/warn
        on a dangerous verb (never blocks), then ``SerialConnection.write`` (rejects control chars /
        raises if disconnected). Mirrors the Devices tab's ingestor scan-ordinal + macro-recorder
        parity so a second write door can't desync either. Best-effort: never raises to caller."""
        cmd = (cmd or "").strip()
        if not cmd:
            return
        conn = None
        try:
            conn = self._dm.get_connection(self._active_port)
        except Exception:
            conn = None
        if conn is None:
            self._append_log("[no connection — connect the device on the Devices tab first]")
            return
        # Safety gate: LABEL + warn on dangerous commands; never block. Reloaded each send so a
        # settings change takes effect immediately (same posture as DeviceTab._on_send).
        settings = load_settings()
        danger = safety.classify(cmd, ci)
        # Defense-in-depth TX lockout: on a firmware that implements arming (LxveOS), an offensive-TX verb
        # is REFUSED unless the device is explicitly armed at send time — not just because a button was left
        # enabled during the <=2s between an armed -> safe change and the next poll repaint. On firmware with
        # NO arm concept (Marauder/DIV/GhostESP/Bruce), there is nothing to arm, so it is confirm-gated below
        # instead of dead-ended — every button is usable for authorized lab work (owner directive 2026-07-21).
        if danger and safety.tx_hard_block(
            danger, self._active_supports_arm(), getattr(self._active_device(), "arm_state", "")
        ):
            state = getattr(self._active_device(), "arm_state", "")
            self._append_log(f"[blocked: '{cmd}' needs the device ARMED "
                             f"(currently {state or 'unknown'}) — Arm first]")
            return
        if safety.should_confirm(danger, settings):
            reply = QMessageBox.warning(
                self, "Confirm dangerous command", safety.lab_only_warning_text(cmd, danger),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self._append_log(f"[cancelled: {cmd}]")
                return
        try:
            conn.write(cmd)
        except Exception as exc:
            self._append_log(f"[send error: {exc}]")
            return
        # Scan-ordinal parity + macro capture (both guarded — bookkeeping must never fail a send).
        if self._ingestor is not None:
            try:
                self._ingestor.note_command_sent(self._active_port, cmd)
            except Exception:
                pass
        if self._recorder is not None:
            try:
                self._recorder.record_command(cmd)
            except Exception:
                pass
        self._append_log(f"> {cmd}")
        # Mirror the sent command into the app-wide activity bus so the always-visible bottom terminal
        # echoes Operate-console sends too (this console keeps its own focused log). Guarded — never
        # break a send. Matches DeviceTab._on_send's "cmd" tap so every send door is reflected.
        try:
            from src.core.activity_log import activity_log
            activity_log().emit_line("operate", f"[{self._active_port}] > {cmd}")
        except Exception:  # noqa: BLE001
            pass

    def _append_log(self, text: str) -> None:
        self._log.append(text)

    # ── poll + refresh ────────────────────────────────────────────────────
    def _poll_tick(self) -> None:
        """Timer body: optionally auto-send a poll-safe ``status`` to refresh the header, then
        repaint from the (ingestor-updated) Device. The auto-send is GATED like the connect-time
        probe — skip a DMS-gated or disconnected port — and only fires for a firmware whose catalog
        defines ``status``, so a firmware that wouldn't understand it never gets a stray write."""
        dev = self._active_device()
        connected = dev is not None and getattr(dev, "connected", False)
        if connected and self._active_port not in self._dms_seen:
            if self._firmware_has_status(getattr(dev, "firmware", "")):
                try:
                    conn = self._dm.get_connection(self._active_port)
                    if conn is not None:
                        conn.write("status")
                except Exception:
                    pass  # a poll write is best-effort; never surface an error from the timer
        self._refresh()

    @staticmethod
    def _firmware_has_status(firmware: str) -> bool:
        """True when *firmware*'s command catalog defines a ``status`` verb (so an auto-poll
        ``status`` is a real command, not a stray write). LxveOS documents it as poll-safe."""
        if not firmware:
            return False
        try:
            return any(c.name == "status" for c in get_protocol(firmware).cached_commands())
        except Exception:
            return False

    def _refresh(self) -> None:
        """Repaint header + lamp + button-enable from the shared Device. Read-only, no writes."""
        dev = self._active_device()
        connected = bool(dev is not None and getattr(dev, "connected", False))
        # Grid: rebuild only when the active firmware changed (buttons are otherwise stable).
        firmware = (getattr(dev, "firmware", "") if dev is not None else "") or ""
        if firmware != self._grid_fw:
            self._rebuild_grid(firmware)
        # Telemetry header.
        telemetry = getattr(dev, "telemetry", {}) if dev is not None else {}
        self._telemetry_label.setText(self._telemetry_line(telemetry))
        # Lamp (display only). A coarse tx= fallback can light the lamp before any arm event, but it
        # NEVER gates the TX buttons below — see tx_armed. `(telemetry or {})` guards a None value.
        state = getattr(dev, "arm_state", "") if dev is not None else ""
        if not state and dev is not None:
            tx = (getattr(dev, "telemetry", {}) or {}).get("tx")
            if tx is True:
                state = "armed"
            elif tx is False:
                state = "safe"
        if state != self._last_arm_state:
            self._last_arm_state = state
            text, color = arm_lamp_render(state)
            self._arm_label.setText(text or "○ (no arm state reported)")
            css = f"color:{color or '#8b949e'};font-size:13px;font-weight:bold;"
            self._arm_label.setStyleSheet(css)
        # Arm box only applies to firmware that arms; hide the three dead buttons otherwise.
        arm_fw = self._active_supports_arm()
        self._arm_box.setVisible(arm_fw)
        # TX-button enable. On arming firmware: offensive-TX enables only when ARMED (arm_state directly,
        # not the tx=-derived display `state`, so a merely TX-capable SAFE board can't enable one). On
        # firmware with no arm concept: enabled whenever connected, and each send is confirm-gated in
        # _send (owner directive 2026-07-21: authorized lab use, total functionality). _send re-checks both.
        tx_armed = connected and getattr(dev, "arm_state", "") == "armed"
        tx_enabled = tx_armed if arm_fw else connected
        for b in self._tx_buttons:
            b.setEnabled(tx_enabled)
        for b in self._safe_buttons:
            b.setEnabled(connected)
        self._btn_arm.setEnabled(connected and state != "armed")
        self._btn_confirm.setEnabled(connected and state == "pending")
        self._btn_disarm.setEnabled(connected)

    # Public slot so main_window can push a live serial line for an immediate repaint (no second
    # subscription — the Device is already mutated by the ingestor; this just repaints if shown).
    def on_line_received(self, port: str, line: str) -> None:  # noqa: ARG002
        if port and port == self._active_port:
            self._refresh()

    @staticmethod
    def _telemetry_line(t: dict) -> str:
        """One compact identity/telemetry line from a device_info dict (present keys only). Mirrors
        the Devices tab's formatter so both surfaces read identically."""
        if not t:
            return ""
        parts = []
        ident = "/".join(str(t[k]) for k in ("board", "chip") if t.get(k))
        if ident:
            parts.append(ident)
        if t.get("fw"):
            parts.append(f"fw {t['fw']}")
        if t.get("ui"):
            parts.append(f"ui {t['ui']}")
        ops = t.get("ops")
        if isinstance(ops, dict):
            parts.append(f"ops {ops.get('ready', 0)}/{ops.get('planned', 0)}/"
                         f"{ops.get('unavailable', 0)}")
        heap = t.get("heap")
        if isinstance(heap, int):
            parts.append(f"heap {heap // 1024} KB")
        return "  ·  ".join(parts)
