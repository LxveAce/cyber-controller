"""Device tab — serial terminal UI with device list and command palette."""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QColor, QFont
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

from src.core.device_manager import DeviceManager
from src.core.serial_handler import ConnectionState, SerialConnection
from src.models.device import Device
from src.protocols import (
    PROTOCOL_DISPLAY_NAMES,
    get_protocol,
    get_protocol_by_display,
)
from src.protocols.base import CommandInfo
from src.core import safety
from src.config.settings import load_settings, save_settings
from src.core.bluejammer_control import (
    BlueJammerController,
    ControlMap,
    ControlUnavailable,
    HttpTransport,
    Mode,
    UartTransport,
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


class DeviceTab(QWidget):
    """Device management tab with list, serial terminal, and command palette."""

    def __init__(self, dm: DeviceManager, pool=None, ingestor=None) -> None:
        super().__init__()
        self._dm = dm
        # Cross-comm: feed this device's parsed serial output (APs/clients) into the shared TargetPool
        # so the AutoRouter can act on it across devices. Optional (backward-compatible) — when a pool
        # is supplied without an ingestor we make one. See src/core/target_ingest.py.
        self._pool = pool
        self._ingestor = ingestor
        if self._pool is not None and self._ingestor is None:
            from src.core.target_ingest import TargetIngestor
            self._ingestor = TargetIngestor(self._pool)
        self._active_conn: SerialConnection | None = None
        self._active_port: str = ""
        self._dms_auth = None  # Optional DeadManAuth instance, set by main window
        self._line_signal = _LineSignal()
        self._line_signal.line_received.connect(self._on_line_received)
        self._devtab_line_cbs: dict = {}  # port -> our on_line cb, so disconnect removes exactly it

        self._build_ui()
        self._refresh_devices()

        # Auto-refresh device list every 3 seconds
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_devices)
        self._timer.start(3000)

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
        self._firmware_combo.addItems(_firmware_choices())
        self._firmware_combo.currentIndexChanged.connect(
            lambda _i: (self._update_bj_panel(), self._persist_firmware())
        )
        fw_row.addWidget(self._firmware_combo, stretch=1)
        left_layout.addLayout(fw_row)

        # Capability chips — surface what the selected firmware/board can do (this device's "node" role in
        # the network). Driven off BaseProtocol.capabilities; updated when the firmware selection changes.
        self._caps_label = QLabel("")
        self._caps_label.setObjectName("caps_label")
        self._caps_label.setWordWrap(True)
        self._caps_label.setStyleSheet("color:#8b949e;font-size:11px;")
        left_layout.addWidget(self._caps_label)
        self._update_capabilities()

        btn_row = QHBoxLayout()
        self._btn_connect = QPushButton("Connect")
        self._btn_connect.clicked.connect(self._on_connect)
        btn_row.addWidget(self._btn_connect)

        self._btn_disconnect = QPushButton("Disconnect")
        self._btn_disconnect.setEnabled(False)
        self._btn_disconnect.clicked.connect(self._on_disconnect)
        btn_row.addWidget(self._btn_disconnect)

        left_layout.addLayout(btn_row)

        btn_refresh = QPushButton("Scan Ports")
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

    def _scan_and_add(self) -> None:
        """Scan ports and register any new devices."""
        for dev in self._dm.scan_ports():
            if not self._dm.get_device(dev.port):
                self._dm.add_device(dev)
        self._refresh_devices()

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
        self._update_bj_panel()

    # ── Connect / Disconnect ─────────────────────────────────────────

    def _on_connect(self) -> None:
        port = self._active_port
        if not port:
            return
        try:
            conn = self._dm.open_connection(port, owner="devices_tab")
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
                    self._ingestor.attach(conn, self._selected_protocol())
                except Exception as exc:
                    self._terminal.append(f"[cross-comm ingest attach failed: {exc}]")
            self._terminal.clear()
            self._terminal.append(f"[Connected to {port}]")
            self._btn_connect.setEnabled(False)
            self._btn_disconnect.setEnabled(True)
            self._btn_send.setEnabled(True)
            self._refresh_devices()
            self._update_bj_panel()
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
        self._dm.close_connection(port, owner="devices_tab")
        self._active_conn = None
        self._terminal.append(f"[Disconnected from {port}]")
        self._btn_connect.setEnabled(True)
        self._btn_disconnect.setEnabled(False)
        self._btn_send.setEnabled(False)
        self._refresh_devices()
        self._update_bj_panel()

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
        if getattr(dev, "board_type", None) == BoardType.FLIPPER_ZERO:
            return get_protocol("flipper")
        return get_protocol("marauder")

    def _persist_firmware(self) -> None:
        """Re-persist the firmware selection onto the active Device when it's connected, so a post-connect
        firmware change keeps the resolver + broadcast (which key off Device.firmware) in sync."""
        port = getattr(self, "_active_port", None)
        if not port:
            return
        dev = self._dm.get_device(port)
        if dev is not None and dev.connected:
            try:
                dev.firmware = self._selected_protocol().protocol_name
            except Exception:
                pass

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
        """Show the selected firmware's capability tokens (its 'node' role) as a chip line in the Devices
        tab. Empty for firmwares that declare none (e.g. generic/raw)."""
        if not hasattr(self, "_caps_label"):
            return
        try:
            caps = sorted(getattr(self._selected_protocol(), "capabilities", frozenset()))
        except Exception:  # noqa: BLE001
            caps = []
        self._caps_label.setText(
            ("Capabilities: " + "  ·  ".join(c.upper() for c in caps)) if caps else ""
        )

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
        nothing jammer-specific is shipped). Returns the HTTP status."""
        import urllib.request

        data = body.encode() if isinstance(body, str) else body
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - user-supplied LAN endpoint
        with urllib.request.urlopen(req, timeout=4) as resp:  # noqa: S310
            return int(getattr(resp, "status", 200) or 200)

    def _bj_on_event(self, kind: str, mode: "Mode", transport: str) -> None:
        self._terminal.append(f"[BlueJammer {kind}: {mode.value} via {transport}]")

    def _bj_stop(self) -> None:
        """STOP (set Idle) — the always-available safety action; never gated."""
        if self._bj_controller is None:
            self._bj_build_controller()
        try:
            self._bj_controller.stop()
            self._bj_status.setText("STOP sent — Idle (emission halted).")
        except ControlUnavailable as exc:
            self._bj_status.setText(
                f"In-app STOP unavailable ({exc})  →  cut power / press the device button / set Idle in the web UI."
            )

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
        try:
            self._bj_controller.set_mode(mode, confirm_unsafe=True)
            self._bj_status.setText(f"Armed: {mode.value}.")
        except ControlUnavailable as exc:
            self._bj_status.setText(
                f"Arm unavailable ({exc})  Load a validated control map captured from your device, or use the web UI."
            )
        except PermissionError as exc:
            self._bj_status.setText(str(exc))

    def _bj_attest_changed(self, on: bool) -> None:
        for b in self._bj_arm_btns:
            b.setEnabled(bool(on))

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
            QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout,
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
            self._terminal.append(f"> {cmd}")
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
                self._dms_auth.check_line(line, lambda pw, c=conn: c.write(pw))
        self._terminal.append(line)

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
