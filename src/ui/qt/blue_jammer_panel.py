"""BlueJammer-V2 remote-control panel — the welded DeviceTab ``_bj_*`` UI, extracted (reform W1).

Lifted verbatim from ``device_tab.py`` so OPERATE ▸ Console and the Devices tab can share one copy.
The arm gate is INDEPENDENT of any serial ``arm_state`` (``BlueJammerProtocol`` never sets
``supports_arm``): arming enables only on the RF-shielded attestation + a per-press §333 confirm.
TX is inert until a validated control map captured from the user's own device is loaded, and
STOP/Idle is always available and never gated. ``safety.py`` is untouched.

The panel owns no terminal: it emits control events through an injected ``event_sink``.
"""
from __future__ import annotations

import os
import threading
from collections import deque
from typing import Callable, Optional

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.config.settings import load_settings, save_settings
from src.core.bluejammer_control import (
    BlueJammerController,
    ControlMap,
    ControlUnavailable,
    HttpTransport,
    Mode,
)
from src.ui.qt.theme import colors as C

# GC of a still-running QThread fires the C++ destructor mid-run and aborts the process. If the
# worker's in-flight web-UI call outlasts shutdown's bounded wait (a black-hole net: urlopen's 4s
# timeout misses a hung DNS resolve), we park it here so its reference survives until it finishes
# and the process exits cleanly. Mirrors main_window._KEEPALIVE_WORKERS (c324a97).
_BJ_KEEPALIVE_WORKERS: set = set()


class _BjCommandQueue(QThread):
    """Serialize BlueJammer control ops (arm / STOP) through ONE long-lived worker draining a FIFO,
    so press-order == device-order.

    Each op does a blocking HTTP call (urlopen timeout=4); running it in the clicked-slot froze the
    UI (worst on the safety STOP). One-thread-per-press unfroze it but lost ordering — a fast STOP
    after a slow Arm could land AFTER it (audit §F2). This restores ordering without re-blocking:

    * one worker drains a FIFO — ops leave in enqueue order;
    * **STOP is never dropped by an in-flight guard** — it purges any queued not-yet-started Arm (a
      later STOP supersedes a pending Arm) and enqueues itself, so the device ends Idle. STOP stays
      dispatchable at all times;
    * each op carries a monotonic id; the GUI shows only a result still the newest op, so an
      overtaken Arm can't overwrite the label with a stale 'Armed'.

    The action closure returns the status string to display (catching its own
    ControlUnavailable/PermissionError); results/'busy' reach the GUI thread via queued signals."""

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
        """Append an op. A STOP first drops any queued not-yet-started Arm (superseding them) so the
        device can't be left armed by an Arm behind the STOP; STOP itself is always kept."""
        with self._cond:
            if kind == "stop":
                self._deque = deque(op for op in self._deque if op[1] == "stop")
            self._deque.append((op_id, kind, action))
            busy = self._busy_locked()
            self._cond.notify_all()
        self.busy_changed.emit(busy)

    def request_stop(self) -> None:
        """Ask the worker to exit after the in-flight op; abandon queued not-yet-started ops."""
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


class BlueJammerPanel(QWidget):
    """The BlueJammer-V2 control/STOP card as a standalone widget. The host toggles visibility
    (shown only when a BlueJammer is the active firmware). Events are emitted through the injected
    ``event_sink``; the panel owns no terminal."""

    def __init__(self, event_sink: "Optional[Callable[[str], None]]" = None,
                 parent: "Optional[QWidget]" = None) -> None:
        super().__init__(parent)
        self._event_sink = event_sink
        # Fail-safe control state: an empty/unvalidated map -> the controller refuses to send.
        self._bj_map: ControlMap = ControlMap()
        self._bj_controller: "BlueJammerController | None" = None
        # One worker serializes every arm/STOP so press-order == device-order (audit §F2).
        # `_bj_op_seq` tags each op so only the newest result writes the label; `_bj_queue_busy`
        # disables Arm while an op is pending.
        self._bj_queue: "_BjCommandQueue | None" = None
        self._bj_op_seq: int = 0
        self._bj_queue_busy: bool = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._bj_panel = QFrame()
        self._bj_panel.setObjectName("card")
        self._bj_panel.setStyleSheet(
            f"QFrame#card{{border:1px solid {C.WARNING};background:rgba(240,136,62,0.09);}}")
        _bj_lay = QVBoxLayout(self._bj_panel)
        _bj_lay.setContentsMargins(12, 10, 12, 10)
        _bj_lbl = QLabel(
            f"<b style='color:{C.WARNING};'>&#9888; BlueJammer-V2 &mdash; remote control</b><br>"
            "Operating an RF jammer is <b>illegal</b> outside an authorized RF-shielded enclosure "
            "(47&nbsp;U.S.C. &sect;333) &mdash; use only on hardware you own, in a "
            "lawful, shielded "
            "lab. <b>Remote control is a safety feature:</b> arm and, critically, <b>STOP</b> the "
            "device without standing next to an active transmitter.<br>"
            "The control frames are closed-source, so the app <b>never sends guessed frames</b>: "
            "live arming activates once you <b>load a validated control map captured from your own "
            "device</b>, or drive it via its web UI. <b>STOP/Idle is always available; "
            "arming needs the shielded-enclosure confirmation below.</b> Web UI: "
            "<code>http://192.168.1.1</code> "
            "(Wi-Fi <code>BlueJ-V2_by_@emensta</code> / <code>NoConn1337</code>, 5&nbsp;GHz)."
        )
        _bj_lbl.setWordWrap(True)
        _bj_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        _bj_lay.addWidget(_bj_lbl)

        # STOP — the always-available safety action (ungated)
        self._bj_stop_btn = QPushButton("■  STOP  (set Idle)")
        self._bj_stop_btn.setStyleSheet(
            f"QPushButton{{background:{C.ERROR};color:#fff;font-weight:700;padding:7px;"
            f"border-radius:4px;}}QPushButton:hover{{background:{C.ERROR_BRIGHT};}}"
        )
        self._bj_stop_btn.clicked.connect(self._bj_stop)
        _bj_lay.addWidget(self._bj_stop_btn)

        # RF-shielded attestation — arming stays disabled until this is checked
        self._bj_attest = QCheckBox(
            "I confirm an authorized, RF-shielded enclosure on hardware I own (enables arming)"
        )
        self._bj_attest.setStyleSheet(f"color:{C.WARNING};")
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
                "Cyber Controller ships no jammer frames; the controller refuses to send without a "
                "validated map."
            )
            _ab.clicked.connect(lambda _checked=False, m=_m: self._bj_set_mode(m))
            self._bj_arm_btns.append(_ab)
            _bj_arm_row.addWidget(_ab)
        _bj_lay.addLayout(_bj_arm_row)

        # Status + map/web controls
        self._bj_status = QLabel(
            "No validated control map loaded — STOP/arm will guide you; the web UI / "
            "button / power work meanwhile."
        )
        self._bj_status.setWordWrap(True)
        self._bj_status.setStyleSheet(f"color:{C.TEXT_MUTED};font-size:9pt;")
        _bj_lay.addWidget(self._bj_status)

        _bj_btn_row = QHBoxLayout()
        self._bj_loadmap_btn = QPushButton("Load control map…")
        self._bj_loadmap_btn.clicked.connect(self._bj_load_map)
        _bj_btn_row.addWidget(self._bj_loadmap_btn)
        self._bj_webui_btn = QPushButton("Open control web UI (set Idle to STOP)")
        self._bj_webui_btn.clicked.connect(self._open_bj_webui)
        _bj_btn_row.addWidget(self._bj_webui_btn)
        _bj_lay.addLayout(_bj_btn_row)

        outer.addWidget(self._bj_panel)
        self._bj_load_map_from_settings()

    def _emit_event(self, text: str) -> None:
        """Emit a control event via the injected sink (host feeds its terminal/activity bus)."""
        if self._event_sink is not None:
            self._event_sink(text)

    def _open_bj_webui(self) -> None:
        """Open the BlueJammer's own control web UI in the browser."""
        import webbrowser
        webbrowser.open("http://192.168.1.1")

    # ── BlueJammer full remote control ───────────────────────────────
    def _bj_build_controller(self) -> None:
        """(Re)build the controller from the current map over the web-UI (HTTP) transport.
        Fail-safe: an empty/unvalidated map makes it refuse to send (ControlUnavailable). The
        inter-board UART path is in the framework but not auto-bound here (a separate wire)."""
        transport = HttpTransport(self._bj_http_request)
        self._bj_controller = BlueJammerController(
            transport, self._bj_map, on_event=self._bj_on_event)

    @staticmethod
    def _bj_http_request(method: str, url: str, body) -> int:
        """Generic HTTP delivery to the device's web UI (endpoints come from the user's map; nothing
        jammer-specific is shipped). Returns the HTTP status.

        Fail-safe: a transport failure (unreachable / wrong network / timeout) is translated to
        ``ControlUnavailable`` — the contract ``HttpTransport.send`` uses for a non-2xx status.
        A raw ``URLError``/``OSError`` escaping into a Qt clicked-slot (with no ``sys.excepthook``)
        aborts the app — so STOP would crash instead of showing the fail-safe guidance."""
        import urllib.error
        import urllib.request

        data = body.encode() if isinstance(body, str) else body
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:  # noqa: S310
                return int(getattr(resp, "status", 200) or 200)
        except urllib.error.HTTPError as exc:
            # A real HTTP response with a non-2xx status — hand the code back so HttpTransport.send
            # reports it rather than masking a reachable-but-erroring device.
            return int(getattr(exc, "code", 0) or 0)
        except OSError as exc:  # URLError (unreachable/DNS), socket.timeout, ConnectionError, ...
            raise ControlUnavailable(f"device web UI unreachable at {url} ({exc})") from exc

    def _bj_on_event(self, kind: str, mode: "Mode", transport: str) -> None:
        self._emit_event(f"[BlueJammer {kind}: {mode.value} via {transport}]")

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
        blocking call runs off the GUI thread); the op is tagged with a monotonic id so only its
        result may overwrite the label."""
        self._bj_ensure_queue()
        self._bj_op_seq += 1
        self._bj_status.setText(pending_text)
        assert self._bj_queue is not None  # just ensured
        self._bj_queue.enqueue(self._bj_op_seq, kind, action)

    def _bj_on_result(self, op_id: int, text: str) -> None:
        """Show a result ONLY if it is still the newest enqueued op — a superseded op (an Arm
        a later STOP purged) must not overwrite the label with a stale status."""
        if op_id == self._bj_op_seq:
            self._bj_status.setText(text)

    def _bj_on_queue_busy(self, busy: bool) -> None:
        """Disable Arm while an op is pending (STOP stays dispatchable); re-enable on drain."""
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
                return (f"In-app STOP unavailable ({exc})  →  cut power / "
                        "press the device button / set Idle in the web UI.")

        self._bj_enqueue("stop", act, "STOP…  (sending to the device web UI)")

    def _bj_set_mode(self, mode: "Mode") -> None:
        """Arm a jamming mode — gated by attestation + a per-press confirm + a validated map."""
        if not self._bj_attest.isChecked():
            self._bj_status.setText("Arming requires the RF-shielded-enclosure confirmation above.")
            return
        reply = QMessageBox.warning(
            self,
            "Confirm arm — illegal outside an authorized RF-shielded lab",
            f"Arm BlueJammer in {mode.value} mode?\n\nOperating an RF jammer is illegal outside an "
            f"authorized, RF-shielded enclosure (47 U.S.C. §333), on hardware you own. "
            f"STOP is always available.",
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
                return (f"Arm unavailable ({exc})  Load a validated control map captured from "
                        "your device, or use the web UI.")
            except PermissionError as exc:
                return str(exc)

        self._bj_enqueue("arm", act, f"Arming {mode.value}…")

    def shutdown(self, wait_ms: int = 5000) -> None:
        """Stop the serializing worker before teardown so its unparented QThread isn't destroyed
        mid-run on exit. Abandons any queued op and joins the in-flight one with a bounded wait
        (each op is a short <=4s HTTP call). Called from MainWindow.closeEvent via the host.

        Backstop: if the op outlasts *wait_ms* (a hung DNS resolve past urlopen's 4s socket
        timeout), the still-running worker is PARKED in the keep-alive set, not GC-destroyed
        mid-run (which aborts the process). Wrapped for the C++-already-gone race."""
        q = self._bj_queue
        if q is None:
            return
        q.request_stop()
        try:
            if q.isRunning() and not q.wait(wait_ms) and q.isRunning():
                _BJ_KEEPALIVE_WORKERS.add(q)  # still blocked — don't let GC destroy it
        except RuntimeError:  # C++ side already gone
            pass

    def _bj_attest_changed(self, on: bool) -> None:  # noqa: ARG002
        self._bj_refresh_arm_enabled()

    def _bj_refresh_arm_enabled(self) -> None:
        """Arm enables only when the attestation is checked AND no op is pending. STOP is never
        gated here. INDEPENDENT of any serial arm_state: BlueJammer has no supports_arm, so coupling
        to it would dead-disable every Arm button (reform critic HIGH)."""
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
        return "Control map loaded (" + (", ".join(kinds) or "empty") + ") — full control active."

    @staticmethod
    def _bj_parse_map_file(path: str) -> ControlMap:
        """Parse a USER-SUPPLIED control map JSON (frames/endpoints captured from their own device;
        none are shipped). Schema::

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
        # Fail-safe default: an unmarked map is NOT trusted. ControlMap defaults validated=False (a
        # user map may hold GUESSED frames) and the controller refuses to send it. Defaulting True
        # re-opened that hole. The author must assert it.
        return ControlMap(
            uart_frames=uart, http_calls=http, validated=bool(data.get("validated", False)))
