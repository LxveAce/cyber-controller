"""Device tab — serial terminal UI with device list and command palette."""

from __future__ import annotations

import html
import logging
import os
import re
import threading
from collections import deque

from PyQt5.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import load_settings, save_settings
from src.core import safety
from src.core.bluejammer_control import (
    BlueJammerController,
    ControlMap,
    ControlUnavailable,
    HttpTransport,
    Mode,
)
from src.core.device_manager import DeviceManager
from src.core.serial_handler import SerialConnection
from src.protocols import (
    PROTOCOL_DISPLAY_NAMES,
    get_protocol,
    get_protocol_by_display,
    resolve_protocol_name,
)

log = logging.getLogger(__name__)

# Firmware options for the per-device protocol selector: "Auto-detect" plus every
# registered protocol's display name (de-duplicated, generic/raw last).
_AUTO_DETECT = "Auto-detect"

# Argument placeholders embedded in a CommandInfo.name, e.g. "scanap -c <ch>" / "led -r <v> -g <v> -b <v>".
# Matched by occurrence (duplicates kept) so each <...> becomes its own form field and substitution slot.
_PLACEHOLDER_RE = re.compile(r"<([^>]+)>")


def _firmware_choices() -> list[str]:
    seen: list[str] = []
    for name, disp in PROTOCOL_DISPLAY_NAMES.items():
        if disp not in seen and name not in ("generic", "raw"):
            seen.append(disp)
    seen.append("Generic / Raw")
    return [_AUTO_DETECT] + seen


# Every protocol instance, for the aggregated command palette (built once).
_ALL_PROTOCOLS = [
    get_protocol_by_display(d) for d in PROTOCOL_DISPLAY_NAMES.values()
]


class _LineSignal(QObject):
    """Helper to bridge threaded serial callbacks to Qt signals."""
    line_received = pyqtSignal(str, str)  # (source port, line) — identifies the emitting connection


class _ProbeSignal(QObject):
    """Bridge the background connect-time probe result back onto the Qt thread."""
    probe_done = pyqtSignal(str)  # port whose probe just finished


# Garbage-collecting a still-running QThread fires the C++ destructor mid-run and aborts the process
# ('QThread: Destroyed while thread is still running'). If the BlueJammer worker's in-flight web-UI
# call outlasts shutdown's bounded wait (a black-hole net — urlopen's 4s timeout misses a hung DNS
# resolve), we park the worker here so its reference survives until it finishes and the
# process exits cleanly. Mirrors main_window._KEEPALIVE_WORKERS (c324a97).
_BJ_KEEPALIVE_WORKERS: set = set()


class _BjCommandQueue(QThread):
    """Serialize BlueJammer control ops (arm / STOP) through ONE long-lived worker draining a FIFO, so
    press-order == device-order.

    Each op does a blocking HTTP call (urlopen timeout=4) to the device web UI; running it in the
    clicked-slot froze the whole UI — worst of all on the safety STOP. The *earlier* fix offloaded each
    press to its own QThread, which unfroze the UI but removed all ordering: a fast STOP after a slow
    Arm could reach the device AFTER the arm, leaving a jammer emitting while the label read stale
    (audit §F2). This restores ordering without re-blocking the UI:

    * one worker drains a FIFO — ops leave in the exact order they were enqueued;
    * **STOP is never dropped by an in-flight guard** — it purges any queued not-yet-started Arm (a
      later STOP supersedes a pending Arm) and enqueues itself, so the device deterministically ends
      Idle. STOP stays dispatchable at all times;
    * each op carries a monotonic id; the GUI only shows a result that is still the newest op, so an
      Arm that a later STOP overtook can't overwrite the label with a stale 'Armed'.

    The action closure returns the exact status string to display (it catches its own
    ControlUnavailable/PermissionError so the message matches); results/‘busy’ transitions reach the
    GUI thread via queued signals. :meth:`request_stop` + a bounded ``wait`` on close abandons any
    queued (not-yet-started) op and joins the in-flight one."""

    done = pyqtSignal(int, str)        # (op_id, result_text) — delivered on the GUI thread
    busy_changed = pyqtSignal(bool)    # True while an op is queued/running (Arm buttons disabled)

    def __init__(self) -> None:
        super().__init__()
        self._cond = threading.Condition()
        self._deque: "deque" = deque()   # (op_id, kind, action); kind in {"arm", "stop"}
        self._processing = False
        self._stopping = False

    def _busy_locked(self) -> bool:
        return self._processing or bool(self._deque)

    def enqueue(self, op_id: int, kind: str, action) -> None:
        """Append an op. A STOP first drops any queued not-yet-started Arm (it supersedes them) so the
        device can't be left armed by an Arm sitting behind the STOP; STOP itself is always kept."""
        with self._cond:
            if kind == "stop":
                self._deque = deque(op for op in self._deque if op[1] == "stop")
            self._deque.append((op_id, kind, action))
            busy = self._busy_locked()
            self._cond.notify_all()
        self.busy_changed.emit(busy)

    def request_stop(self) -> None:
        """Ask the worker to exit after the in-flight op (if any); abandons queued not-yet-started ops."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()

    def run(self) -> None:
        while True:
            with self._cond:
                while not self._deque and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
                op_id, _kind, action = self._deque.popleft()
                self._processing = True
            try:
                result = action()
            except Exception as exc:  # noqa: BLE001 — a worker exception must never abort the app
                result = f"BlueJammer action failed: {exc}"
            with self._cond:
                self._processing = False
                busy = self._busy_locked()
            self.done.emit(op_id, result)
            self.busy_changed.emit(busy)


class DeviceTab(QWidget):
    """Device management tab with list, serial terminal, and command palette."""

    def __init__(self, dm: DeviceManager, pool=None, ingestor=None, recorder=None) -> None:
        super().__init__()
        self._dm = dm
        # Cross-comm: feed this device's parsed serial output (APs/clients) into the shared TargetPool
        # so the AutoRouter can act on it across devices. Optional (backward-compatible) — when a pool
        # is supplied without an ingestor we make one. See src/core/target_ingest.py.
        self._pool = pool
        self._ingestor = ingestor
        # Macro capture: the shared MacroRecorder (optional, backward-compatible). Each command sent from
        # this tab is fed to it via record_command(), which is a no-op unless a recording is in progress.
        # This is the producer the recorder was missing — without it a macro recorded while sending from
        # the Devices tab captured zero steps (nothing ever notified the recorder that a command was sent).
        self._recorder = recorder
        if self._pool is not None and self._ingestor is None:
            from src.core.target_ingest import TargetIngestor
            # Pass the DeviceManager so a device_info line (LxveOS status/info) updates the
            # connected Device's runtime caps even on the standalone Devices-tab ingestor (no hub).
            self._ingestor = TargetIngestor(self._pool, devices=self._dm)
        self._active_conn: SerialConnection | None = None
        self._active_port: str = ""
        self._dms_auth = None  # Optional DeadManAuth instance, set by main window
        self._line_signal = _LineSignal()
        self._line_signal.line_received.connect(self._on_line_received)
        self._devtab_line_cbs: dict = {}  # port -> our on_line cb, so disconnect removes exactly it
        self._ingest_proto: dict[str, str] = {}  # port -> protocol_name the ingestor is parsing with
        # CC-7: connect-time probe runs off-thread; this bridges its result back to the Qt thread.
        self._probe_signal = _ProbeSignal()
        self._probe_signal.probe_done.connect(self._on_probe_done)
        # Ports where a Dead-Man's-Switch auth gate has been seen — the connect-time probe skips these so it
        # never writes an unsolicited command at a DMS unlock prompt (a failed attempt can wipe/brick).
        self._dms_seen: set[str] = set()

        self._build_ui()
        self._refresh_devices()

        # Auto-refresh device list every 3 seconds
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._refresh_devices)
        # The 3s device-list poll runs only while the tab is visible (showEvent starts it — fires at
        # launch for the default tab too — and hideEvent stops it). Serial I/O is independent of this.

    # ── Layout ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Horizontal)

        # ── Left: device list (in scroll area) ──────────────────────
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.NoFrame)
        left_scroll.setMinimumWidth(160)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel("Devices")
        lbl.setObjectName("card_title")
        left_layout.addWidget(lbl)

        self._device_list = QListWidget()
        self._device_list.setMinimumHeight(80)
        self._device_list.currentItemChanged.connect(self._on_device_selected)
        left_layout.addWidget(self._device_list, stretch=1)

        # Per-device firmware selector — drives the cross-comm ingest parser and
        # (via the palette) which command set is offered. Lets a HaleHound / DIV /
        # BW16 board feed the AutoRouter instead of everything defaulting to Marauder.
        fw_row = QHBoxLayout()
        self._firmware_label = QLabel("Firmware:")
        fw_row.addWidget(self._firmware_label)
        self._firmware_combo = QComboBox()
        self._firmware_combo.setToolTip(
            "Tell Cyber Controller which firmware this board runs, so it parses the "
            "device's replies and offers the matching command set (defaults to Marauder)."
        )
        self._firmware_combo.addItems(_firmware_choices())
        self._firmware_combo.currentIndexChanged.connect(
            lambda _i: (self._update_bj_panel(), self._persist_firmware())
        )
        fw_row.addWidget(self._firmware_combo, stretch=1)
        left_layout.addLayout(fw_row)

        # Capability chips — this node's role in the network. Prefers a connected device's RUNTIME
        # caps (a LxveOS status/info line updates them live) over the firmware's static map; refreshed
        # on firmware change AND per incoming line (see _on_line_received).
        self._caps_label = QLabel("")
        self._caps_label.setObjectName("caps_label")
        self._caps_label.setWordWrap(True)
        self._caps_label.setTextFormat(Qt.RichText)  # capN tokens render muted/distinct
        self._caps_label.setStyleSheet("color:#8b949e;font-size:11px;")
        left_layout.addWidget(self._caps_label)
        self._update_capabilities()

        # Live device telemetry — a firmware that reports itself over serial (LxveOS status/info ->
        # device_info) fills Device.telemetry (fw/board/chip/ui + ops + heap). A read-only line
        # under the caps chips, refreshed on selection AND per line. Blank if none reported.
        self._telemetry_label = QLabel("")
        self._telemetry_label.setObjectName("telemetry_label")
        self._telemetry_label.setWordWrap(True)
        self._telemetry_label.setStyleSheet("color:#6e7681;font-size:11px;")
        left_layout.addWidget(self._telemetry_label)
        self._update_telemetry()

        # Connect-time handshake result (CC-7): whether the firmware actually answers over the open link plus
        # an identifying banner — set by the background probe kicked off on connect. Distinct from the link
        # being open (that's the device-list color); this says the node is really talking.
        self._health_label = QLabel("")
        self._health_label.setObjectName("health_label")
        self._health_label.setTextFormat(Qt.RichText)
        self._health_label.setWordWrap(True)
        self._health_label.setStyleSheet("font-size:11px;")
        left_layout.addWidget(self._health_label)

        # Offensive-TX ARM/SAFE lamp — a firmware that reports its arm state over serial (LxveOS
        # `arm`/`disarm` -> arm_state events, or a `status` line's tx= field) drives a prominent,
        # color-coded indicator: green SAFE / amber PENDING / red ARMED / grey TX-DISABLED. Blank
        # until the firmware speaks. Refreshed on selection AND per incoming line (_on_line_received).
        self._arm_label = QLabel("")
        self._arm_label.setObjectName("arm_label")
        self._arm_label.setTextFormat(Qt.PlainText)
        self._arm_label.setStyleSheet("font-size:11px;font-weight:bold;")
        left_layout.addWidget(self._arm_label)
        self._update_arm_lamp()

        btn_row = QHBoxLayout()
        self._btn_connect = QPushButton("Connect")
        self._btn_connect.setToolTip("Open a serial link to the selected device.")
        self._btn_connect.clicked.connect(self._on_connect)
        btn_row.addWidget(self._btn_connect)

        self._btn_disconnect = QPushButton("Disconnect")
        self._btn_disconnect.setEnabled(False)
        self._btn_disconnect.setToolTip("Close the serial link to the selected device.")
        self._btn_disconnect.clicked.connect(self._on_disconnect)
        btn_row.addWidget(self._btn_disconnect)

        left_layout.addLayout(btn_row)

        btn_refresh = QPushButton("Scan Ports")
        btn_refresh.setToolTip("Scan serial ports for connected boards and add any new ones.")
        btn_refresh.clicked.connect(self._scan_and_add)
        left_layout.addWidget(btn_refresh)

        left_scroll.setWidget(left)
        splitter.addWidget(left_scroll)

        # ── Right: serial terminal ───────────────────────────────────
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # ── BlueJammer-V2 FULL remote-control panel (shown only when BlueJammer is the active firmware) ──
        # Owner: proper remote control IS the safety mechanism — arm AND, critically, instantly STOP the
        # device without standing next to an active transmitter. The controller is FAIL-SAFE: it refuses to
        # send guessed frames, so live transmission activates only once a validated control map captured from
        # the user's OWN device is loaded (closed-source frames are never shipped with the app). STOP/Idle is
        # ungated; arming is gated behind an RF-shielded-enclosure attestation + a per-press confirm.
        # Operating a jammer outside an authorized, shielded, lawful context is illegal (47 U.S.C. §333).
        self._bj_map: ControlMap = ControlMap()
        self._bj_controller: "BlueJammerController | None" = None
        # One long-lived worker serializes every arm/STOP so press-order == device-order (audit §F2).
        # Lazily created on first op; joined on close. `_bj_op_seq` tags each op so only the newest
        # op's result may write the status label; `_bj_queue_busy` disables Arm while an op is pending.
        self._bj_queue: "_BjCommandQueue | None" = None
        self._bj_op_seq: int = 0
        self._bj_queue_busy: bool = False
        self._bj_panel = QFrame()
        self._bj_panel.setObjectName("card")
        self._bj_panel.setStyleSheet("QFrame#card{border:1px solid #f0883e;background:rgba(240,136,62,0.09);}")
        _bj_lay = QVBoxLayout(self._bj_panel)
        _bj_lay.setContentsMargins(12, 10, 12, 10)
        _bj_lbl = QLabel(
            "<b style='color:#f0883e;'>&#9888; BlueJammer-V2 &mdash; full remote control</b><br>"
            "Operating an RF jammer is <b>illegal</b> outside an authorized RF-shielded enclosure "
            "(47&nbsp;U.S.C. &sect;333) &mdash; use only on hardware you own, in a lawful, shielded lab. "
            "<b>Remote control is a safety feature:</b> arm and, critically, <b>STOP</b> the device without "
            "standing next to an active transmitter.<br>"
            "The control frames are closed-source, so the app <b>never sends guessed frames</b>: live arming "
            "activates once you <b>load a validated control map captured from your own device</b>, or drive it "
            "via its web UI. <b>STOP/Idle is always available; arming needs the shielded-enclosure confirmation "
            "below.</b> Web UI: <code>http://192.168.1.1</code> (Wi-Fi <code>BlueJ-V2_by_@emensta</code> / "
            "<code>NoConn1337</code>, 5&nbsp;GHz)."
        )
        _bj_lbl.setWordWrap(True)
        _bj_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        _bj_lay.addWidget(_bj_lbl)

        # STOP — the always-available safety action (ungated)
        self._bj_stop_btn = QPushButton("■  STOP  (set Idle)")
        self._bj_stop_btn.setStyleSheet(
            "QPushButton{background:#f85149;color:#fff;font-weight:700;padding:7px;border-radius:4px;}"
            "QPushButton:hover{background:#ff6a60;}"
        )
        self._bj_stop_btn.clicked.connect(self._bj_stop)
        _bj_lay.addWidget(self._bj_stop_btn)

        # RF-shielded attestation — arming stays disabled until this is checked
        self._bj_attest = QCheckBox(
            "I confirm an authorized, RF-shielded enclosure on hardware I own (enables arming)"
        )
        self._bj_attest.setStyleSheet("color:#f0883e;")
        self._bj_attest.toggled.connect(self._bj_attest_changed)
        _bj_lay.addWidget(self._bj_attest)

        # Arm-mode buttons (gated by attestation + a per-press confirm + a validated control map)
        _bj_arm_row = QHBoxLayout()
        self._bj_arm_btns: "list[QPushButton]" = []
        for _m in (Mode.BLUETOOTH, Mode.BLE, Mode.WIFI, Mode.RC_DRONE):
            _ab = QPushButton("Arm " + _m.value)
            _ab.setEnabled(False)
            _ab.setToolTip(
                "Scaffolding — inert until you load a control map captured from your own device. "
                "Cyber Controller ships no jammer frames; the controller refuses to send without a validated map."
            )
            _ab.clicked.connect(lambda _checked=False, m=_m: self._bj_set_mode(m))
            self._bj_arm_btns.append(_ab)
            _bj_arm_row.addWidget(_ab)
        _bj_lay.addLayout(_bj_arm_row)

        # Status + map/web controls
        self._bj_status = QLabel(
            "No validated control map loaded — STOP/arm will guide you; the web UI / button / power work meanwhile."
        )
        self._bj_status.setWordWrap(True)
        self._bj_status.setStyleSheet("color:#8b949e;font-size:11px;")
        _bj_lay.addWidget(self._bj_status)

        _bj_btn_row = QHBoxLayout()
        self._bj_loadmap_btn = QPushButton("Load control map…")
        self._bj_loadmap_btn.clicked.connect(self._bj_load_map)
        _bj_btn_row.addWidget(self._bj_loadmap_btn)
        self._bj_webui_btn = QPushButton("Open control web UI (set Idle to STOP)")
        self._bj_webui_btn.clicked.connect(self._open_bj_webui)
        _bj_btn_row.addWidget(self._bj_webui_btn)
        _bj_lay.addLayout(_bj_btn_row)

        self._bj_panel.setVisible(False)
        right_layout.addWidget(self._bj_panel)
        self._bj_load_map_from_settings()

        self._term_label = QLabel("Serial Terminal")
        self._term_label.setObjectName("card_title")
        self._term_label.setWordWrap(True)
        right_layout.addWidget(self._term_label)

        self._terminal = QTextEdit()
        self._terminal.setReadOnly(True)
        self._terminal.setObjectName("terminal")
        self._terminal.setMinimumHeight(100)
        # Bound memory over a long session: O(1) auto-trim of the oldest lines once the cap is
        # hit, instead of unbounded growth on every serial line (UI-opt #6; cap is large enough
        # to be invisible in normal use, matters on a 4-8GB Pi over hours).
        self._terminal.document().setMaximumBlockCount(5000)
        right_layout.addWidget(self._terminal, stretch=1)

        # Command input row
        cmd_row = QHBoxLayout()

        self._cmd_palette = QComboBox()
        self._cmd_palette.setEditable(False)
        self._cmd_palette.setMinimumWidth(140)
        self._populate_palette()
        self._cmd_palette.currentIndexChanged.connect(self._on_palette_select)
        cmd_row.addWidget(self._cmd_palette, stretch=1)

        self._cmd_input = QLineEdit()
        self._cmd_input.setPlaceholderText("Type command or select from palette...")
        self._cmd_input.returnPressed.connect(self._on_send)
        cmd_row.addWidget(self._cmd_input, stretch=3)

        self._btn_send = QPushButton("Send")
        self._btn_send.setToolTip("Send the typed command to the selected connected device.")
        self._btn_send.clicked.connect(self._on_send)
        self._btn_send.setEnabled(False)
        cmd_row.addWidget(self._btn_send)

        right_layout.addLayout(cmd_row)
        splitter.addWidget(right)

        # Splitter proportions — setSizes sets the LAUNCH split (1:3); setStretchFactor only governs resize.
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([240, 720])
        root.addWidget(splitter)

    # ── Dual-depth (Simple / Pro) ────────────────────────────────────

    def set_ui_mode(self, mode: str) -> None:
        """Simple = device list + Connect/Disconnect + terminal + a plain command input. Hide the
        per-device firmware selector (parser stays auto/Marauder default) and the command palette
        (advanced per-firmware command picker) — manual typing still works for everyone."""
        pro = str(mode).lower() != "simple"
        for w in (getattr(self, "_firmware_label", None), getattr(self, "_firmware_combo", None),
                  getattr(self, "_cmd_palette", None)):
            if w is not None:
                w.setVisible(pro)

    # ── Device list ──────────────────────────────────────────────────

    def showEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        super().showEvent(ev)
        self._refresh_devices()   # catch up immediately when shown
        self._timer.start()

    def hideEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        super().hideEvent(ev)
        self._timer.stop()

    def _refresh_devices(self) -> None:
        """Update the list widget from the device manager."""
        selected_port = self._active_port
        self._device_list.clear()
        for dev in self._dm.list_devices():
            item = QListWidgetItem(dev.display_name)
            item.setData(Qt.UserRole, dev.port)
            if dev.connected:
                item.setForeground(QColor("#3fb950"))
            else:
                item.setForeground(QColor("#8b949e"))
            self._device_list.addItem(item)
            if dev.port == selected_port:
                self._device_list.setCurrentItem(item)
        # Auto-select the first device when nothing is active yet, so the bottom-left Connect/
        # Disconnect buttons (which act on _active_port) work after a scan. QListWidget.addItem
        # never auto-selects, and the re-select above only matches an ALREADY-chosen port — so on
        # first populate currentItem() stayed None, _active_port stayed "", and the buttons hit the
        # `if not port: return` guard and silently no-opped (looked dead). Guarded on an empty
        # _active_port so a later user pick is preserved; fires _on_device_selected (which sets
        # _active_port + the connect/disconnect enable states).
        if not self._active_port and self._device_list.count():
            first = self._device_list.item(0)
            if first is not None and bool(first.flags() & Qt.ItemIsSelectable):
                self._device_list.setCurrentItem(first)
        # Empty-state guidance (same shape as software_tab's empty-combo entry): a single
        # non-selectable hint row telling the user the next step.
        if self._device_list.count() == 0:
            hint = QListWidgetItem("No devices yet — plug one in and press Scan Ports.")
            hint.setFlags(Qt.NoItemFlags)
            hint.setForeground(QColor("#8b949e"))
            self._device_list.addItem(hint)

    def _scan_and_add(self) -> None:
        """Scan ports and register any new devices."""
        for dev in self._dm.scan_ports():
            if not self._dm.get_device(dev.port):
                self._dm.add_device(dev)
        self._refresh_devices()

    def select_port(self, port: str) -> bool:
        """Programmatically select a device by port (e.g. driven from the main-window sidebar). Refreshes
        the list first so a just-added device is present, then selects its row — which fires
        _on_device_selected and syncs the active port/connection, terminal label and button states.
        Returns True if a matching row was found and selected."""
        if not port:
            return False
        self._refresh_devices()
        for i in range(self._device_list.count()):
            item = self._device_list.item(i)
            if item is not None and item.data(Qt.UserRole) == port:
                self._device_list.setCurrentItem(item)
                return True
        return False

    def _on_device_selected(self, current: QListWidgetItem | None, _prev: QListWidgetItem | None) -> None:
        if current is None:
            return
        port = current.data(Qt.UserRole)
        self._active_port = port
        # Keep _active_conn in lock-step with the selected port so Send + the per-firmware line ending
        # act on the SELECTED device, not whichever was connected last (multi-connect safety).
        self._active_conn = self._dm.get_connection(port)
        dev = self._dm.get_device(port)
        if dev:
            self._term_label.setText(f"Serial Terminal — {dev.display_name}")
            connected = dev.connected
            self._btn_connect.setEnabled(not connected)
            self._btn_disconnect.setEnabled(connected)
            self._btn_send.setEnabled(connected)
            self._sync_firmware_combo_to(dev)  # re-point the global combo at THIS device before judging it
        self._update_health_label()
        self._update_bj_panel()  # also refreshes the caps chips off the newly-selected device
        self._update_telemetry()  # telemetry is device-specific -> refresh on selection too
        self._update_arm_lamp()   # arm state is device-specific too

    def _sync_firmware_combo_to(self, dev) -> None:
        """Re-point the (global) firmware combo at the SELECTED device, so _selected_protocol /
        _update_bj_panel / the Send-enable logic judge the device the user just clicked — not whichever
        firmware was last picked for another port. Without this, choosing 'BlueJammer' for device A then
        clicking device B showed B the BlueJammer panel + disabled Send.

        Only pin the combo to a concrete firmware when THIS device's firmware was explicitly FORCED
        (Device.firmware_forced) — otherwise fall back to Auto-detect, so an auto-detected device keeps its
        post-probe re-autodetect (which requires the combo to read Auto-detect) instead of being frozen to
        the connect-time default. Signals are blocked so this re-sync doesn't re-fire _update_bj_panel /
        _persist_firmware; the caller updates the panel right after."""
        forced = bool(getattr(dev, "firmware_forced", False))
        fw = getattr(dev, "firmware", "") or ""
        display = PROTOCOL_DISPLAY_NAMES.get(fw) if (forced and fw) else None
        target = display if (display and self._firmware_combo.findText(display) >= 0) else _AUTO_DETECT
        if self._firmware_combo.currentText() == target:
            return
        blocked = self._firmware_combo.blockSignals(True)
        try:
            self._firmware_combo.setCurrentText(target)
        finally:
            self._firmware_combo.blockSignals(blocked)

    # ── Connect / Disconnect ─────────────────────────────────────────

    def _on_connect(self) -> None:
        port = self._active_port
        if not port:
            return
        try:
            # Honor the user-configured Default Baud Rate (Settings ▸ Serial). Without this the connect
            # falls back to open_connection's hardcoded 115200, so a device whose serial monitor runs at a
            # non-default baud (e.g. 9600 or 230400) connects at the wrong speed and produces garbled TX/RX.
            baud = load_settings().get("serial", {}).get("default_baud", 115200)
            conn = self._dm.open_connection(port, baud=baud, owner="devices_tab")
            self._active_conn = conn
            # Persist the chosen firmware onto the Device so the ActionResolver + BroadcastEngine resolve
            # the SAME protocol the ingestor parses with (both key off Device.firmware, which scan_ports
            # never sets — without this the resolver returns zero actions and broadcast/STOP-ALL no-op).
            dev = self._dm.get_device(port)
            if dev is not None:
                try:
                    dev.firmware = self._selected_protocol().protocol_name
                except Exception:
                    pass
            # Carry the SOURCE port through the signal so line handling (esp. the DMS auto-auth reply)
            # targets the device that emitted the line, and keep a handle so disconnect can remove
            # exactly this callback (a co-owned conn survives close_connection, so a left-behind
            # callback would stack on the next reconnect and double-process every line).
            cb = lambda line, p=port: self._line_signal.line_received.emit(p, line)
            conn.on_line(cb)
            self._devtab_line_cbs[port] = cb
            # Cross-comm ingestion: parse this device's serial output into the shared target pool so a
            # scan here can auto-route a command to another connected device (AutoRouter). Defaults to
            # the Marauder parser; a per-device firmware selector can refine this later.
            if self._ingestor is not None:
                try:
                    proto = self._selected_protocol()
                    self._ingestor.attach(conn, proto)
                    # Remember which parser this port is on, so the post-handshake re-detect (Auto-detect)
                    # can tell whether it needs to swap the ingest parser to the firmware actually found.
                    self._ingest_proto[port] = proto.protocol_name
                except Exception as exc:
                    self._terminal.append(f"[cross-comm ingest attach failed: {exc}]")
            self._terminal.clear()
            self._terminal.append(f"[Connected to {port}]")
            self._btn_connect.setEnabled(False)
            self._btn_disconnect.setEnabled(True)
            self._btn_send.setEnabled(True)
            self._refresh_devices()
            self._update_bj_panel()
            # CC-7: activate the shipped-but-dormant S3-c handshake — run it in the background so a connected
            # device reports whether its firmware actually answers (health) + an identifying banner. Deferred
            # briefly so a Dead-Man's-Switch boot gate (which prompts on connect) is seen FIRST — _should_probe
            # then skips it, so we never fire an unsolicited "help" at a DMS unlock prompt (attempt-burn /
            # brick risk). Best-effort; stream/controlmap nodes do no write and are marked "no-cli".
            if port == self._active_port:
                self._set_probing_label()
            QTimer.singleShot(1500, lambda p=port: self._start_probe(p))
        except Exception as exc:
            self._terminal.append(f"[Error: {exc}]")

    def _on_disconnect(self) -> None:
        port = self._active_port
        if not port:
            return
        # Remove OUR callbacks before releasing the connection. A co-owned connection (e.g. the
        # persistent terminal still holds it) survives close_connection, so a left-behind on_line /
        # ingestor callback would stack a duplicate on the next reconnect and parse every line twice.
        conn = self._dm.get_connection(port)
        if conn is not None:
            cb = self._devtab_line_cbs.pop(port, None)
            if cb is not None:
                remover = getattr(conn, "remove_line_callback", None)
                if callable(remover):
                    try:
                        remover(cb)
                    except Exception:
                        pass
            if self._ingestor is not None:
                self._ingestor.detach(conn)
        self._ingest_proto.pop(port, None)
        self._dm.close_connection(port, owner="devices_tab")
        self._active_conn = None
        self._terminal.append(f"[Disconnected from {port}]")
        self._btn_connect.setEnabled(True)
        self._btn_disconnect.setEnabled(False)
        self._btn_send.setEnabled(False)
        if hasattr(self, "_health_label"):
            self._health_label.setText("")  # link closed — the last probe result is now stale
        self._dms_seen.discard(port)  # re-evaluate afresh if something else is later connected on this port
        self._refresh_devices()
        self._update_bj_panel()

    # ── Connect-time probe (CC-7) ─────────────────────────────────────
    def _should_probe(self, port: str) -> bool:
        """Guard the connect-time probe. Skip a device that has shown a Dead-Man's-Switch auth gate: the probe
        writes an unsolicited command (handshake.DEFAULT_PROBE_COMMANDS = "help") which, at a DMS unlock
        prompt, could count as a failed attempt and trip the firmware's wipe/brick. Also skip if the link
        closed before the (deferred) probe fired."""
        if port in self._dms_seen:
            return False
        dev = self._dm.get_device(port)
        return bool(dev is not None and dev.connected)

    def _start_probe(self, port: str) -> None:
        """Run the connect-time handshake on a background daemon thread so the UI never blocks on the serial
        round-trip. DeviceManager.probe (src/core/handshake) writes a probe command, reads the reply, and sets
        Device.health / Device.fw_banner in place; the result is then shown on the Qt thread via a signal.
        Best-effort: probe() never raises, and stream/controlmap nodes are marked "no-cli" with no write."""
        if not self._should_probe(port):
            if port == getattr(self, "_active_port", ""):
                self._update_health_label()  # clear the transient "probing…" (e.g. DMS-gated → no probe)
            return
        threading.Thread(target=self._probe_worker, args=(port,), daemon=True).start()

    def _probe_worker(self, port: str) -> None:
        """Background body of the connect-time probe: side-effect-only (populates Device.health/fw_banner via
        DeviceManager.probe), then marshals back to the Qt thread. Kept separate from _start_probe so tests can
        drive it synchronously without a thread."""
        try:
            self._dm.probe(port)
        except Exception:  # noqa: BLE001 - probe is best-effort; a failure just leaves health "unknown"
            pass
        finally:
            self._probe_signal.probe_done.emit(port)

    def _on_probe_done(self, port: str) -> None:
        """Qt-thread slot: the probe for *port* finished — re-route the ingest parser to the firmware the
        handshake actually identified (Auto-detect only), then refresh the health label if that port is shown."""
        self._reautodetect_after_probe(port)
        if port == getattr(self, "_active_port", ""):
            self._update_health_label()

    def _reautodetect_after_probe(self, port: str) -> None:
        """After the connect-time handshake, on Auto-detect, swap the cross-comm ingest parser to the firmware
        the probe identified — so a NEVER-PROBED board also routes to its own parser, not the provisional
        Marauder default the connect chose before any reply came back.

        Trusts the freshly-captured probe *banner*: ``dev.firmware`` may still hold the connect-time default
        (which would mask detection), whereas ``fw_banner`` is the raw identifying line. Only acts when the
        user left the selector on Auto-detect (an explicit pick is always honoured). Best-effort — a failure
        here must never break the probe-done slot or the connection.
        """
        try:
            if self._firmware_combo.currentText() != _AUTO_DETECT:
                return
            dev = self._dm.get_device(port)
            if dev is None or not dev.connected:
                return
            if getattr(dev, "firmware_forced", False):
                return  # a manual force (Broadcast/Devices) must not be clobbered by re-autodetect
            banner = (getattr(dev, "fw_banner", "") or "").strip()
            if not banner:
                return
            from src.core.device_detect import match_firmware
            name = resolve_protocol_name((match_firmware(banner)[0] or "").strip())
            if name is None:
                return
            proto = get_protocol(name)
            if proto.protocol_name == self._ingest_proto.get(port):
                return  # already parsing with the detected firmware — nothing to do
            # Keep the resolver / broadcast / palette / capabilities in sync (all key off Device.firmware,
            # which _selected_protocol() re-resolves); route through the central setter so the Broadcast
            # panel repopulates too. Not forced (this is auto-detect), then swap the live ingest parser.
            self._dm.set_firmware(port, proto.protocol_name, forced=False)
            if not self._reattach_ingest(port, proto):
                return
            if port == getattr(self, "_active_port", ""):
                self._update_bj_panel()  # -> _apply_line_ending + _update_capabilities off the new firmware
                try:
                    self._terminal.append(
                        f"[auto-detected {proto.protocol_name} — cross-comm parser switched]"
                    )
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001 - purely a UX refinement; never disturb the probe-done slot
            log.debug("re-autodetect after probe failed on %s", port, exc_info=True)

    def _reattach_ingest(self, port: str, proto) -> bool:
        """Swap the cross-comm ingest parser for *port* to *proto*. Returns True if the swap happened.

        :meth:`TargetIngestor.attach` is idempotent (it drops the port's prior on_line before adding), so a
        re-attach cleanly replaces the parser without double-parsing.
        """
        if self._ingestor is None:
            return False
        conn = self._dm.get_connection(port)
        if conn is None:
            return False
        try:
            self._ingestor.attach(conn, proto)
        except Exception:  # noqa: BLE001
            log.debug("re-attach ingest failed on %s", port, exc_info=True)
            return False
        self._ingest_proto[port] = proto.protocol_name
        return True

    def _set_probing_label(self) -> None:
        if hasattr(self, "_health_label"):
            self._health_label.setText("Health: <span style='color:#8b949e;'>&#9679; probing&hellip;</span>")

    def _update_health_label(self) -> None:
        """Show the connect-time probe result (health + identifying banner) for the selected device. Blank
        unless the device is connected — health describes a live link's firmware, not a stale/closed one."""
        if not hasattr(self, "_health_label"):
            return
        port = getattr(self, "_active_port", "")
        dev = self._dm.get_device(port) if port else None
        if dev is None or not dev.connected:
            self._health_label.setText("")
            return
        self._health_label.setText(self._format_health(dev))

    @staticmethod
    def _format_health(dev) -> str:
        """Render Device.health/fw_banner as a short colored chip. Health values come from handshake.py:
        alive (firmware answered) / no-reply (open link, silence) / no-cli (stream/controlmap node — no text
        channel, honest not a failure) / unknown (not probed yet). The banner is device output → escaped."""
        import html
        health = getattr(dev, "health", "unknown") or "unknown"
        # "unknown" = not (yet) probed → show nothing. The transient "probing…" is set explicitly by
        # _set_probing_label while a probe is in flight, so a settled unknown must not linger as "probing…".
        if health == "unknown":
            return ""
        banner = html.escape((getattr(dev, "fw_banner", "") or "").strip())
        styles = {
            "alive": ("#3fb950", "alive"),
            "no-reply": ("#d29922", "no reply"),
            "no-cli": ("#8b949e", "no CLI (stream device)"),
        }
        color, text = styles.get(health, ("#8b949e", html.escape(health)))
        chip = f"Health: <span style='color:{color};'>&#9679; {text}</span>"
        if banner:
            chip += f" &mdash; <span style='color:#8b949e;'>{banner}</span>"
        return chip

    # ── Serial I/O ───────────────────────────────────────────────────

    def _selected_protocol(self):
        """Protocol for the currently selected firmware. On 'Auto-detect', seed from the connected device's
        USB-detected board type (Flipper -> flipper, with its CR terminator; ESP32 / unknown -> marauder,
        the flagship ESP32 firmware) so a Flipper isn't silently parsed with the Marauder grammar + LF.
        Full runtime detection that distinguishes the ESP32 firmwares (marauder vs ghostesp vs bruce, which
        share a USB VID) via identify() is a cross-comm-rework item — the user can still pick explicitly."""
        choice = self._firmware_combo.currentText()
        if choice == _AUTO_DETECT:
            return self._autodetect_protocol()
        return get_protocol_by_display(choice)

    def _autodetect_protocol(self):
        from src.models.device import BoardType
        port = getattr(self, "_active_port", None)
        dev = self._dm.get_device(port) if port else None
        # Prefer a REAL prior detection over the ESP32-defaults-to-Marauder heuristic: a Devices-tab probe
        # (scan_ports) or an earlier connect's handshake may have identified the actual firmware
        # (dev.firmware, e.g. 'ghostesp') or captured an identifying banner (dev.fw_banner). Routing to that
        # firmware's OWN parser is what lets a GhostESP / Bruce / HaleHound / DIV / BW16 / Meshtastic device
        # populate the target pool + expose its own command set instead of being parsed as Marauder.
        name = self._detected_protocol_name(dev)
        if name is not None:
            return get_protocol(name)
        if getattr(dev, "board_type", None) == BoardType.FLIPPER_ZERO:
            return get_protocol("flipper")
        return get_protocol("marauder")

    @staticmethod
    def _detected_protocol_name(dev) -> "str | None":
        """A real, known firmware name from a device's detected identifier / banner, or None.

        Returns None when nothing usable was detected (so the caller keeps its Flipper/Marauder default)
        — the generic passthrough never counts as a match.
        """
        if dev is None:
            return None
        # 1) An explicit detected firmware identifier (match_firmware from scan_ports / handshake).
        name = resolve_protocol_name(getattr(dev, "firmware", "") or "")
        if name is not None:
            return name
        # 2) Otherwise mine an identifying banner captured on probe through the same signature matcher.
        banner = (getattr(dev, "fw_banner", "") or "").strip()
        if banner:
            try:
                from src.core.device_detect import match_firmware
                fw = (match_firmware(banner)[0] or "").strip()
            except Exception:  # noqa: BLE001
                fw = ""
            return resolve_protocol_name(fw)
        return None

    def _persist_firmware(self) -> None:
        """Re-persist the firmware selection onto the active Device when it's connected, so a post-connect
        firmware change keeps the resolver + broadcast (which key off Device.firmware) in sync."""
        port = getattr(self, "_active_port", None)
        if not port:
            return
        dev = self._dm.get_device(port)
        if dev is not None and dev.connected:
            try:
                fw = self._selected_protocol().protocol_name
            except Exception:
                return
            # Route through the central setter so the Broadcast panel (and anything on on_device_changed)
            # stays in sync. An explicit (non-Auto) pick is a manual FORCE that re-autodetect must honour.
            forced = self._firmware_combo.currentText() != _AUTO_DETECT
            self._dm.set_firmware(port, fw, forced=forced)

    def _open_bj_webui(self) -> None:
        """Open the BlueJammer's own control web UI (its real control surface) in the browser."""
        import webbrowser
        webbrowser.open("http://192.168.1.1")

    def _update_bj_panel(self) -> None:
        """Show the BlueJammer control/STOP panel only when a BlueJammer is the active firmware, and
        disable the inert serial-send affordances (the stock firmware has no serial command channel)."""
        try:
            is_bj = self._selected_protocol().protocol_name == "bluejammer"
        except Exception:  # noqa: BLE001
            is_bj = False
        self._bj_panel.setVisible(is_bj)
        self._cmd_input.setEnabled(not is_bj)
        self._cmd_palette.setEnabled(not is_bj)
        # No serial command channel for BlueJammer -> Send can't do anything; otherwise governed by the
        # connection state (mirror the Disconnect button).
        self._btn_send.setEnabled(False if is_bj else self._btn_disconnect.isEnabled())
        self._apply_line_ending()
        self._update_capabilities()

    def _update_capabilities(self) -> None:
        """Show the active node's capability tokens (its 'node' role) as a chip line. Prefers a
        connected device's RUNTIME-reported caps (a LxveOS status/info line updates them live) over
        the firmware's static map. Cheap per line: re-renders only when the caps set changes."""
        if not hasattr(self, "_caps_label"):
            return
        caps = self._current_capabilities()
        key = tuple(caps)
        if key == getattr(self, "_last_caps_key", None):
            return
        self._last_caps_key = key
        self._caps_label.setText(self._caps_chip_html(caps))

    def _current_capabilities(self) -> list:
        """Sorted capability tokens for the active node: the connected device's runtime capabilities
        when it reported any (device_info), else the selected firmware's static capability map."""
        port = getattr(self, "_active_port", "")
        dev = self._dm.get_device(port) if port else None
        runtime = getattr(dev, "runtime_capabilities", None) if dev is not None else None
        if runtime:
            return sorted(runtime)
        try:
            return sorted(getattr(self._selected_protocol(), "capabilities", frozenset()))
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _caps_chip_html(caps: list) -> str:
        """Render the caps line. Named slugs show upper-cased; an unknown future-bit token (``capN``,
        a firmware bit CC has no slug for yet) shows muted + labelled 'unknown cap N', so an M1
        capability the firmware lights up reads as visibly new rather than a cryptic code."""
        if not caps:
            return ""
        chips = []
        for c in caps:
            if c.startswith("cap") and c[3:].isdigit():
                chips.append(f'<span style="color:#6e7681;font-style:italic;">'
                             f'unknown cap {html.escape(c[3:])}</span>')
            else:
                chips.append(html.escape(c.upper()))
        return "Capabilities: " + "  ·  ".join(chips)

    def _update_telemetry(self) -> None:
        """Connected device's live telemetry (LxveOS ops/heap/identity from a device_info) as a
        read-only line under the caps chips. Blank if it reported none. Cheap per line: re-renders
        only when the rendered line changes."""
        if not hasattr(self, "_telemetry_label"):
            return
        port = getattr(self, "_active_port", "")
        dev = self._dm.get_device(port) if port else None
        telem = getattr(dev, "telemetry", None) if dev is not None else None
        line = self._telemetry_line(telem or {})
        if line == getattr(self, "_last_telemetry_text", None):
            return
        self._last_telemetry_text = line
        self._telemetry_label.setText(line)

    @staticmethod
    def _telemetry_line(t: dict) -> str:
        """One compact line from a device_info telemetry dict. Only the present keys render, so both
        the rich status line and the smaller info block render cleanly."""
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
                         f"{ops.get('unavailable', 0)} (ready/planned/unavailable)")
        heap = t.get("heap")
        if isinstance(heap, int):
            parts.append(f"heap {heap // 1024} KB")
        return "  ·  ".join(parts)

    def _update_arm_lamp(self) -> None:
        """Connected device's offensive-TX arm state as a prominent ARM/SAFE lamp. Prefers an explicit
        ``arm_state`` (LxveOS ``arm``/``disarm``); falls back to a ``status`` line's ``tx=`` field so the
        lamp lights up on a plain status even before any arm transition. Blank until a firmware reports
        either. Cheap per line: re-renders only when the shown state changes."""
        if not hasattr(self, "_arm_label"):
            return
        port = getattr(self, "_active_port", "")
        dev = self._dm.get_device(port) if port else None
        state = getattr(dev, "arm_state", "") if dev is not None else ""
        if not state and dev is not None:
            # No explicit arm event yet — derive a coarse lamp from the status line's tx= field.
            tx = getattr(dev, "telemetry", {}).get("tx")
            if tx is True:
                state = "armed"
            elif tx is False:
                state = "safe"
        if state == getattr(self, "_last_arm_state", None):
            return
        self._last_arm_state = state
        text, color = self._arm_lamp_render(state)
        self._arm_label.setText(text)
        self._arm_label.setStyleSheet(f"color:{color};font-size:11px;font-weight:bold;")

    @staticmethod
    def _arm_lamp_render(state: str) -> "tuple[str, str]":
        """(label text, color) for an arm state. Blank/unknown -> blank (no lamp until the fw speaks).
        A recognized-but-unlisted token still renders verbatim (muted) so a future arm state isn't lost.
        Colors read as a traffic light: green safe, amber mid-handshake, red hot, grey compiled-out."""
        table = {
            "safe":        ("● SAFE — offensive TX locked",          "#3fb950"),
            "pending":     ("● ARM PENDING — awaiting token",        "#d29922"),
            "armed":       ("● ARMED — offensive TX permitted",      "#f85149"),
            "tx_disabled": ("● TX DISABLED — offensive TX not built", "#6e7681"),
        }
        if state in table:
            return table[state]
        if state:
            return (f"● {state}", "#8b949e")
        return ("", "#8b949e")

    def _apply_line_ending(self) -> None:
        """Apply the selected firmware's command terminator to the live connection (Flipper needs CR; most
        firmwares use LF). Called whenever the firmware selection or connection changes."""
        conn = getattr(self, "_active_conn", None)
        if conn is None:
            return
        try:
            conn.line_ending = getattr(self._selected_protocol(), "line_ending", "\n")
        except Exception:  # noqa: BLE001
            conn.line_ending = "\n"

    # ── BlueJammer full remote control ───────────────────────────────
    def _bj_build_controller(self) -> None:
        """(Re)build the controller from the current control map over the web-UI (HTTP) transport — the
        control surface reachable over the device's AP. Fail-safe: with an empty/unvalidated map the
        controller refuses to send (ControlUnavailable). The inter-board UART path is supported by the
        framework for advanced wired setups but is not auto-bound here (it is a separate physical wire)."""
        transport = HttpTransport(self._bj_http_request)
        self._bj_controller = BlueJammerController(transport, self._bj_map, on_event=self._bj_on_event)

    @staticmethod
    def _bj_http_request(method: str, url: str, body) -> int:
        """Generic HTTP delivery to the device's own web UI (endpoints come from the user's control map;
        nothing jammer-specific is shipped). Returns the HTTP status.

        Fail-safe: a transport failure (device unreachable / joined the wrong network / timeout) is
        translated to ``ControlUnavailable`` — the same contract ``HttpTransport.send`` uses for a
        non-2xx status. Letting a raw ``URLError``/``OSError`` escape here would propagate out of the
        Qt clicked-slot (``_bj_stop`` / ``_bj_set_mode``); with no ``sys.excepthook`` installed PyQt
        aborts the whole app, so the safety STOP button would crash instead of showing the
        'cut power / press the device button / set Idle in the web UI' guidance."""
        import urllib.error
        import urllib.request

        data = body.encode() if isinstance(body, str) else body
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - user-supplied LAN endpoint
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:  # noqa: S310
                return int(getattr(resp, "status", 200) or 200)
        except urllib.error.HTTPError as exc:
            # A real HTTP response carrying a non-2xx status — hand the code back so HttpTransport.send
            # reports it ("web UI returned HTTP <status>") rather than masking a reachable-but-erroring device.
            return int(getattr(exc, "code", 0) or 0)
        except OSError as exc:  # URLError (unreachable/DNS), socket.timeout (TimeoutError), ConnectionError, ...
            raise ControlUnavailable(f"device web UI unreachable at {url} ({exc})") from exc

    def _bj_on_event(self, kind: str, mode: "Mode", transport: str) -> None:
        self._terminal.append(f"[BlueJammer {kind}: {mode.value} via {transport}]")

    def _bj_ensure_queue(self) -> "_BjCommandQueue":
        """Lazily create + start the single serializing worker (see :class:`_BjCommandQueue`)."""
        q = self._bj_queue
        if q is None:
            q = _BjCommandQueue()
            q.done.connect(self._bj_on_result)            # queued (cross-thread) -> GUI thread
            q.busy_changed.connect(self._bj_on_queue_busy)
            self._bj_queue = q
            q.start()
        return q

    def _bj_enqueue(self, kind: str, action, pending_text: str) -> None:
        """Enqueue an arm/STOP op onto the serializing worker. Shows *pending_text* immediately (the
        blocking HTTP call runs off the GUI thread); the op is tagged with a monotonic id so only the
        newest op's result may later overwrite the label."""
        self._bj_ensure_queue()
        self._bj_op_seq += 1
        self._bj_status.setText(pending_text)
        assert self._bj_queue is not None  # just ensured
        self._bj_queue.enqueue(self._bj_op_seq, kind, action)

    def _bj_on_result(self, op_id: int, text: str) -> None:
        """Show an op's result ONLY if it is still the newest enqueued op — a superseded op (e.g. an
        Arm that a later STOP overtook and purged) must not overwrite the label with a stale status."""
        if op_id == self._bj_op_seq:
            self._bj_status.setText(text)

    def _bj_on_queue_busy(self, busy: bool) -> None:
        """Disable Arm while any op is queued/running (STOP stays dispatchable); re-enable on drain."""
        self._bj_queue_busy = busy
        self._bj_refresh_arm_enabled()

    def _bj_stop(self) -> None:
        """STOP (set Idle) — the always-available safety action; never gated."""
        if self._bj_controller is None:
            self._bj_build_controller()
        controller = self._bj_controller
        if controller is None:  # _bj_build_controller always assigns; defensive narrowing
            return

        def act() -> str:
            try:
                controller.stop()
                return "STOP sent — Idle (emission halted)."
            except ControlUnavailable as exc:
                return (f"In-app STOP unavailable ({exc})  →  cut power / press the device button / "
                        "set Idle in the web UI.")

        self._bj_enqueue("stop", act, "STOP…  (sending to the device web UI)")

    def _bj_set_mode(self, mode: "Mode") -> None:
        """Arm a jamming mode — gated by the RF-shielded attestation + a per-press confirm + a validated map."""
        if not self._bj_attest.isChecked():
            self._bj_status.setText("Arming requires the RF-shielded-enclosure confirmation above.")
            return
        reply = QMessageBox.warning(
            self,
            "Confirm arm — illegal outside an authorized RF-shielded lab",
            f"Arm BlueJammer in {mode.value} mode?\n\nOperating an RF jammer is illegal outside an authorized, "
            f"RF-shielded enclosure (47 U.S.C. §333), on hardware you own. STOP is always available.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        if self._bj_controller is None:
            self._bj_build_controller()
        controller = self._bj_controller
        if controller is None:  # _bj_build_controller always assigns; defensive narrowing
            return

        def act() -> str:
            try:
                controller.set_mode(mode, confirm_unsafe=True)
                return f"Armed: {mode.value}."
            except ControlUnavailable as exc:
                return (f"Arm unavailable ({exc})  Load a validated control map captured from your device, "
                        "or use the web UI.")
            except PermissionError as exc:
                return str(exc)

        self._bj_enqueue("arm", act, f"Arming {mode.value}…")

    def shutdown(self, wait_ms: int = 5000) -> None:
        """Stop the BlueJammer serializing worker before the tab (and window) is torn down, so its
        unparented QThread isn't destroyed mid-run ('QThread: Destroyed while thread is still running')
        on exit. Abandons any queued (not-yet-started) op and joins the in-flight one with a bounded
        wait (each op is a short <=4s HTTP call). Invoked from MainWindow.closeEvent.

        Backstop: if the in-flight op outlasts *wait_ms* (a black-hole net — urlopen's 4s socket
        timeout doesn't cover a hung DNS resolve), the still-running worker is PARKED in a module
        keep-alive set instead of GC-destroyed mid-run (which aborts the process). Mirrors
        main_window's keep-alive handling. Wrapped for the C++-already-gone race."""
        q = self._bj_queue
        if q is None:
            return
        q.request_stop()
        try:
            if q.isRunning() and not q.wait(wait_ms) and q.isRunning():
                _BJ_KEEPALIVE_WORKERS.add(q)  # still blocked — don't let GC destroy it
        except RuntimeError:  # C++ side already gone
            pass

    def _bj_attest_changed(self, on: bool) -> None:  # noqa: ARG002 — state read in _bj_refresh_arm_enabled
        self._bj_refresh_arm_enabled()

    def _bj_refresh_arm_enabled(self) -> None:
        """Arm is enabled only when the RF-shielded attestation is checked AND no control op is pending
        (a queued/running arm or STOP). STOP is never gated here — it must stay dispatchable."""
        on = self._bj_attest.isChecked() and not self._bj_queue_busy
        for b in self._bj_arm_btns:
            b.setEnabled(on)

    def _bj_load_map(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load BlueJammer control map (JSON)", "", "JSON (*.json);;All files (*)"
        )
        if not path:
            return
        try:
            self._bj_map = self._bj_parse_map_file(path)
        except Exception as exc:  # noqa: BLE001
            self._bj_status.setText(f"Could not load control map: {exc}")
            return
        self._bj_build_controller()
        self._bj_status.setText(self._bj_map_summary())
        try:
            s = load_settings()
            s.setdefault("bluejammer", {})["control_map_path"] = path
            save_settings(s)
        except Exception:  # noqa: BLE001
            pass

    def _bj_load_map_from_settings(self) -> None:
        try:
            path = (load_settings().get("bluejammer") or {}).get("control_map_path")
        except Exception:  # noqa: BLE001
            path = None
        if not path or not os.path.exists(path):
            return
        try:
            self._bj_map = self._bj_parse_map_file(path)
            self._bj_build_controller()
            self._bj_status.setText(self._bj_map_summary())
        except Exception:  # noqa: BLE001
            pass

    def _bj_map_summary(self) -> str:
        kinds = []
        if self._bj_map.uart_frames:
            kinds.append(f"{len(self._bj_map.uart_frames)} UART")
        if self._bj_map.http_calls:
            kinds.append(f"{len(self._bj_map.http_calls)} web-UI")
        if not self._bj_map.validated:
            return "Control map loaded but not marked validated — it will not send."
        return "Control map loaded (" + (", ".join(kinds) or "empty") + ") — full remote control active."

    @staticmethod
    def _bj_parse_map_file(path: str) -> ControlMap:
        """Parse a USER-SUPPLIED control map JSON (frames/endpoints captured from their own device; none are
        shipped with the app). Schema::

            {"validated": true,
             "uart_frames": {"Idle": "<hex>", "WiFi": "<hex>"},
             "http_calls":  {"Idle": ["POST", "/mode", "idle"]}}
        """
        import json

        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        def _mode(key: str) -> "Mode":
            try:
                return Mode(key)
            except ValueError:
                return Mode[key]

        uart = {
            _mode(k): (bytes.fromhex(v) if isinstance(v, str) else bytes(v))
            for k, v in (data.get("uart_frames") or {}).items()
        }
        http = {
            _mode(k): (v[0], v[1], v[2] if len(v) > 2 else None)
            for k, v in (data.get("http_calls") or {}).items()
        }
        # Fail-safe default: an unmarked map is NOT trusted. bluejammer_control.ControlMap defaults
        # validated=False for exactly this reason (a user map may hold GUESSED frames), and the
        # controller refuses to transmit an unvalidated map. Defaulting to True here re-opened that hole
        # (a map omitting "validated" would send frames / silently no-op STOP). The author must assert it.
        return ControlMap(uart_frames=uart, http_calls=http, validated=bool(data.get("validated", False)))

    def _command_info(self, cmd: str):
        """Return the CommandInfo for *cmd* from the selected protocol, if any."""
        for ci in self._selected_protocol().cached_commands():  # memoized (UI-opt #2)
            if ci.name == cmd:
                return ci
        return None

    @staticmethod
    def _placeholder_tokens(cmd: str) -> "list[str]":
        """The <...> placeholder names in *cmd*, in order, duplicates kept (e.g. ['v','v','v'])."""
        return _PLACEHOLDER_RE.findall(cmd)

    @staticmethod
    def _sanitize_arg(value: str) -> str:
        """Clean a user-entered argument: strip, drop control chars + DEL, strip angle brackets (so a value
        can't smuggle a new <token>), cap at 64 chars (mirrors action_resolver._safe_sub)."""
        value = value.strip()
        value = "".join(ch for ch in value if ch >= " " and ch != "\x7f")
        return value.replace("<", "").replace(">", "")[:64]

    @staticmethod
    def _substitute_tokens(cmd: str, values: "list[str]") -> str:
        """Occurrence-ordered substitution: replace each <...> with the next value (handles repeated <v>)."""
        it = iter(values)
        return _PLACEHOLDER_RE.sub(lambda _m: next(it), cmd)

    def _resolve_placeholders(self, cmd: str, ci) -> "str | None":
        """If *cmd* has <...> placeholders, prompt a small form (one field per occurrence) and return the
        filled command. Returns *cmd* unchanged when there are no placeholders, or None if the user cancels
        or leaves a field blank. Labels come from ci.args when its count matches; else the token name."""
        tokens = self._placeholder_tokens(cmd)
        if not tokens:
            return cmd
        labels = [a.strip() for a in (ci.args.split(",") if ci and getattr(ci, "args", "") else [])]
        use_labels = labels if len(labels) == len(tokens) else None
        from PyQt5.QtWidgets import (
            QDialog,
            QDialogButtonBox,
            QFormLayout,
            QLabel,
            QLineEdit,
            QVBoxLayout,
        )
        dlg = QDialog(self)
        dlg.setWindowTitle("Command parameters")
        outer = QVBoxLayout(dlg)
        head = QLabel(((ci.description + "\n\n") if ci and getattr(ci, "description", "") else "") + cmd)
        head.setWordWrap(True)
        outer.addWidget(head)
        form = QFormLayout()
        edits: "list[QLineEdit]" = []
        for i, tok in enumerate(tokens):
            edit = QLineEdit()
            edit.setPlaceholderText("<" + tok + ">")
            form.addRow((use_labels[i] if use_labels else tok) + ":", edit)
            edits.append(edit)
        outer.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        outer.addWidget(buttons)
        if edits:
            edits[0].setFocus()
        if dlg.exec_() != QDialog.Accepted:
            self._terminal.append(f"[cancelled: {cmd}]")
            return None
        values = [self._sanitize_arg(e.text()) for e in edits]
        if any(v == "" for v in values):
            self._terminal.append("[cancelled: a parameter was left blank]")
            return None
        return self._substitute_tokens(cmd, values)

    def _on_send(self) -> None:
        cmd = self._cmd_input.text().strip()
        if not cmd or not self._active_conn:
            return
        # Capture the CommandInfo BEFORE substitution (its match is on the templated name with <...> tokens),
        # then prompt for any placeholder args so we never send a literal "<ch>"/"<idx>" over the wire.
        ci = self._command_info(cmd)
        resolved = self._resolve_placeholders(cmd, ci)
        if resolved is None:
            return
        cmd = resolved
        # Safety gate: LABEL + warn on dangerous commands; never block. "Yes" always
        # proceeds, and Settings -> "Suppress all safety warnings" turns this off.
        # Reloaded each send so a settings change takes effect immediately.
        settings = load_settings()
        danger = safety.classify(cmd, ci)
        if safety.should_confirm(danger, settings):
            from PyQt5.QtWidgets import QMessageBox
            reply = QMessageBox.warning(
                self,
                "Confirm dangerous command",
                safety.lab_only_warning_text(cmd, danger),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self._terminal.append(f"[cancelled: {cmd}]")
                return
        try:
            # Per-device terminator: re-stamp from the SELECTED device's own persisted firmware right
            # before writing. The single shared firmware combo can still hold a DIFFERENT device's
            # firmware after a device switch, so without this, selecting a CR-only Flipper while the
            # combo is left on an LF firmware sends LF-terminated commands the Flipper CLI silently
            # ignores. (AutoRouter/execute_action already re-stamp per write; _on_send didn't.)
            try:
                from src.protocols import line_ending_for
                _dev = self._dm.get_device(self._active_port)
                _fw = (getattr(_dev, "firmware", "") or "").strip()
                if _fw:
                    self._active_conn.line_ending = line_ending_for(_fw)
            except Exception:
                pass
            self._active_conn.write(cmd)
            # Terminal Send is a SECOND door into the device — it writes the connection
            # directly, not via the routed CrossCommHub sink. A hand-typed `clearlist -a`/
            # `reboot` here must ALSO flush the port's parser scan ordinals, or a later
            # `select -a {index}` (Deauth-AP) mis-binds to a stale index. Same reset the sink
            # uses, so both write paths stay in lockstep. Guarded — never break a send; a
            # no-op when this tab has no shared ingestor / the command isn't a clear/reboot.
            if self._ingestor is not None:
                try:
                    self._ingestor.note_command_sent(self._active_port, cmd)
                except Exception:  # noqa: BLE001 — scan-state bookkeeping must never fail a send
                    pass
            # Feed the just-sent command into an in-progress macro recording (no-op when not recording).
            if self._recorder is not None:
                self._recorder.record_command(cmd)
            self._terminal.append(f"> {cmd}")
            # Mirror the sent command into the app-wide activity bus so the persistent terminal reflects
            # command execution too (this tab's own terminal is separate). Guarded — never break a send.
            try:
                from src.core.activity_log import activity_log
                activity_log().emit_line("cmd", f"[{self._active_port}] > {cmd}")
            except Exception:  # noqa: BLE001
                pass
            self._cmd_input.clear()
        except Exception as exc:
            self._terminal.append(f"[Send error: {exc}]")

    def _on_line_received(self, port: str, line: str) -> None:
        # Dead Man's Switch auto-auth must reply to the device that EMITTED the prompt — not whatever
        # row happens to be selected (self._active_conn). With two devices connected, a DMS prompt from
        # A while B is selected would otherwise write A's boot password to B (credential leak) AND never
        # answer A, exhausting its attempt counter -> the exact flash-wipe/brick the DMS is built around.
        if self._dms_auth is not None:
            conn = self._dm.get_connection(port)
            if conn is not None:
                if self._dms_auth.check_line(line, lambda pw, c=conn: c.write(pw)):
                    # A DMS auth gate spoke on this port — mark it so the connect-time probe never writes an
                    # unsolicited command here (see _should_probe).
                    self._dms_seen.add(port)
        # Untrusted device bytes: QTextEdit.append() renders rich text when the line begins with markup
        # (mightBeRichText), so escape it -- otherwise a board emitting <b>/<img>/<span> spoofs the
        # terminal (command-echo/output injection on a security tool).
        self._terminal.append(html.escape(line))
        # A device_info line (LxveOS status/info) OR an arm_state line (arm/disarm) may have just
        # updated this port's Device runtime caps / telemetry / arm state via the ingestor (which ran
        # first on the serial thread, before this Qt slot). Refresh the caps chips + telemetry line +
        # arm lamp to match. All self-guard on unchanged content, so calling them per line is cheap.
        if port == getattr(self, "_active_port", ""):
            self._update_capabilities()
            self._update_telemetry()
            self._update_arm_lamp()

    # ── Command palette ──────────────────────────────────────────────

    def _populate_palette(self) -> None:
        self._cmd_palette.addItem("-- Command Palette --")
        for proto in _ALL_PROTOCOLS:
            for ci in proto.cached_commands():  # memoized (UI-opt #2)
                label = f"[{proto.protocol_name}] {ci.category}: {ci.name}"
                self._cmd_palette.addItem(label, ci.name)

    def _on_palette_select(self, idx: int) -> None:
        if idx <= 0:
            return
        cmd = self._cmd_palette.itemData(idx)
        if cmd:
            self._cmd_input.setText(cmd)
        self._cmd_palette.setCurrentIndex(0)
