"""PyQt + QtWebEngine desktop shell for the web UI (owner's choice, 2026-08-07).

This is "the PyQt app, skinned as the HTML": a native PyQt ``QMainWindow`` whose entire body is a
``QWebEngineView`` rendering the reform mockup's own HTML/CSS/JS. It reuses the whole web stack
unchanged — ``reform.html``/``reform.css``/``reform.js`` + the hardened Flask + SocketIO bridge + the
Python core. Only the window changes: a Qt/Chromium web view instead of pywebview's OS webview.

Native-shell features (so it behaves like a real desktop app, not a bare browser frame):
  * leanness — trims Chromium's background services + caps render processes (owner: responsive, not
    heavy on hardware). No fidelity or security loss.
  * adaptive sizing — the minimum/launch size clamps to the actual screen, so a small cyberdeck panel
    (800x480 / 1024x600) is usable instead of stuck behind a 900-wide floor.
  * window identity — organisation/app name + the CC icon (also the base for QSettings later).
  * external-link + download handling — off-box links open in the system browser (never white-screen
    the app), and downloads route to a native Save dialog.

Security posture is identical to ``desktop.py``: binds 127.0.0.1 on an ephemeral port (no LAN
listener), the web auth gate is untouched, and a single-use bootstrap token establishes the session
on a clean URL (credentials never ride in the address, so relative ``fetch()`` / WebSocket work).

Why this over ``desktop.py`` (pywebview): the owner wants a genuine PyQt application. The trade-off is
weight — QtWebEngine bundles Chromium — most noticeable on the Raspberry-Pi (ARM) target, where
PyQtWebEngine wheels are unreliable; the Pi path stays pywebview / ``--ui web`` until that's validated.

PyQtWebEngine is an optional dependency; if it is missing we say so and point at the other UIs.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from typing import Any
from urllib.parse import quote

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.ui.web.desktop import _free_loopback_port, _wait_until_serving

log = logging.getLogger(__name__)

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "")

# Chromium flags that trim background/telemetry services and cap the render-process count without
# touching rendering fidelity or the security model. Set BEFORE QtWebEngine initializes; an operator
# override (an existing QTWEBENGINE_CHROMIUM_FLAGS) always wins.
_LEAN_CHROMIUM_FLAGS = (
    "--disable-features=Translate,MediaRouter,DialMediaRouteProvider,OptimizationHints,AcceptCHFrame "
    "--disable-background-networking --disable-component-update --disable-domain-reliability "
    "--disable-sync --disable-breakpad --renderer-process-limit=1"
)


def launch_desktop_qt(
    device_manager: DeviceManager,
    flash_engine: FlashEngine,
    event_bus: EventBus,
    target_pool: TargetPool,
    *,
    audit: Any = None,
) -> int:
    """Run the web UI inside a native PyQt/QtWebEngine window. Returns a process exit code."""
    # Leanness (P1-10): trim Chromium before QtWebEngine loads. setdefault so an operator override wins.
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", _LEAN_CHROMIUM_FLAGS)
    try:
        from PyQt5.QtCore import QFile, QIODevice, QObject, QSettings, Qt, QUrl, pyqtSlot
        from PyQt5.QtGui import QDesktopServices, QKeySequence
        from PyQt5.QtWebChannel import QWebChannel
        from PyQt5.QtWebEngineWidgets import (
            QWebEnginePage,
            QWebEngineScript,
            QWebEngineSettings,
            QWebEngineView,
        )
        from PyQt5.QtWidgets import (
            QAction,
            QApplication,
            QFileDialog,
            QMainWindow,
            QMenu,
            QSystemTrayIcon,
        )
    except ImportError:
        log.error(
            "PyQtWebEngine is not installed — the Qt desktop shell needs it.\n"
            "    pip install PyQtWebEngine\n"
            "Meanwhile the same UI is available via:  cyber-controller --ui webview  (pywebview)\n"
            "                                     or:  cyber-controller --ui web      (browser)"
        )
        return 1

    # Strong web credential set so the loopback port is not open even to another local user; the
    # window authenticates via a single-use bootstrap token, NOT credentials-in-URL (Chromium refuses
    # relative fetch() from a user:pass@host document, which would break every live update + action).
    if not os.environ.get("CC_WEB_PASS"):
        os.environ["CC_WEB_USER"] = os.environ.get("CC_WEB_USER", "cc-desktop")
        os.environ["CC_WEB_PASS"] = secrets.token_urlsafe(24)

    token = secrets.token_urlsafe(32)
    port = _free_loopback_port()

    # launch_web() blocks on socketio.run(), so run it on a daemon thread; the Qt window owns the main
    # thread. The daemon dies with the process when the window closes.
    from src.ui.web.app import launch_web

    def _serve() -> None:
        try:
            launch_web(
                device_manager, flash_engine, event_bus, target_pool,
                host="127.0.0.1", port=port, audit=audit, desktop_token=token,
            )
        except Exception:
            log.exception("Desktop web server thread crashed")

    threading.Thread(target=_serve, name="cc-desktop-qt-web", daemon=True).start()

    if not _wait_until_serving(port):
        log.error("Web server did not come up on 127.0.0.1:%d in time", port)
        return 1

    # /desktop-auth consumes the one-time token, sets the session cookie, and 302s to /reform (clean
    # URL). No credentials ever ride in the address, so fetch()/WebSocket work in the window.
    url = f"http://127.0.0.1:{port}/desktop-auth?token={quote(token, safe='')}"

    # External-link + new-window interception (P0-3): keep the app itself always on the loopback origin;
    # any off-box link (GitHub, Sponsors, docs) opens in the system browser instead of navigating — or
    # white-screening — the app. `createWindow` (target=_blank) routes out the same way.
    class _ShellPage(QWebEnginePage):
        def acceptNavigationRequest(self, qurl, nav_type, is_main_frame):  # noqa: N802 (Qt override)
            if is_main_frame and qurl.host() not in _LOCAL_HOSTS:
                QDesktopServices.openUrl(qurl)
                return False
            return super().acceptNavigationRequest(qurl, nav_type, is_main_frame)

        def createWindow(self, _wtype):  # noqa: N802 (Qt override)
            # A link asked for a new window; open its first navigation externally, then discard.
            tmp = QWebEnginePage(self)
            tmp.urlChanged.connect(lambda u, p=tmp: (QDesktopServices.openUrl(u), p.deleteLater()))
            return tmp

    # Set the high-DPI + AA_ShareOpenGLContexts attributes BEFORE the QApplication exists (Qt reads them
    # at construction). Needed for QtWebEngine on Linux/Mesa + ARM GPUs; no-ops if the launcher already
    # created the app. When this shell is reached directly via --ui desktop (no launcher), this is the
    # only place they get set.
    from src.ui.qt.screen import enable_high_dpi
    enable_high_dpi()
    app = QApplication.instance() or QApplication([])
    # Window identity (P0-4) — org/app name (also the base any future QSettings key hangs off) + CC icon.
    app.setOrganizationName("LxveAce")
    app.setApplicationName("CyberController")

    # Window geometry persistence (P1-13): remember size + position across launches via QSettings
    # (keyed off the org/app name set above). Saved on close, restored on next open.
    _qsettings = QSettings()

    class _CCMainWindow(QMainWindow):
        def closeEvent(self, ev):  # noqa: N802 (Qt override)
            _qsettings.setValue("window/geometry", self.saveGeometry())
            super().closeEvent(ev)

    window = _CCMainWindow()
    window.setWindowTitle("Cyber Controller")
    try:
        from src.ui.qt.widgets.cc_icon import create_cc_icon
        window.setWindowIcon(create_cc_icon())
    except Exception:  # noqa: BLE001 — an icon is cosmetic; never block launch on it
        pass

    view = QWebEngineView(window)
    view.setPage(_ShellPage(view))

    # System tray (#19): a guarded tray icon so the app lives in the tray + raises native notices.
    # Guarded on isSystemTrayAvailable() — headless/no-tray environments simply get no tray (and
    # notify() below silently no-ops), never an error.
    _tray = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        _tray = QSystemTrayIcon(window)
        try:
            _tray.setIcon(window.windowIcon())
        except Exception:  # noqa: BLE001 — the tray works without a custom icon
            pass
        _tray.setToolTip("Cyber Controller")
        _tray_menu = QMenu()
        _tray_menu.addAction("Show", window.showNormal)
        _tray_menu.addAction("Quit", app.quit)
        _tray.setContextMenu(_tray_menu)
        _tray.activated.connect(
            lambda reason: window.showNormal() if reason == QSystemTrayIcon.Trigger else None
        )
        _tray.show()

    # QWebChannel native file bridge (#14, the linchpin): expose real OS file dialogs to the reform
    # UI so flash-by-path / wordlist / OS-image / capture selection use a QFileDialog (true absolute
    # path) instead of the browser <input type=file> (fakepath + multi-GB upload). Loopback-desktop
    # only; on the web build window.ccbridge is undefined and reform.js falls back to manual input.
    from src.ui.web.native_bridge import open_picker_spec

    class _NativeBridge(QObject):
        """Registered on the web channel as ``ccbridge_native``; its slots open native dialogs."""

        @pyqtSlot(str, result=str)
        def pick(self, kind: str) -> str:
            """Open an OS open-file dialog for *kind* and return the chosen absolute path ('' if
            cancelled). Modal on the Qt main thread (the channel delivers slot calls here)."""
            title, file_filter = open_picker_spec(kind)
            path, _ = QFileDialog.getOpenFileName(window, title, "", file_filter)
            return path or ""

        @pyqtSlot(str, str, result=str)
        def pickSave(self, title: str, suggested: str) -> str:  # noqa: N802 (JS-facing camelCase)
            """Open an OS save-file dialog and return the chosen path ('' if cancelled)."""
            path, _ = QFileDialog.getSaveFileName(window, title or "Save file", suggested or "")
            return path or ""

        @pyqtSlot(str, str)
        def notify(self, title: str, body: str) -> None:
            """Raise a native tray notification (#19) for a finished op — flash done, key recovered,
            macro finished. Called from reform.js on the relevant socket events. No-ops without a
            tray. reform.js never sends the recovered PASSWORD here, only the network name."""
            if _tray is not None:
                _tray.showMessage(title or "Cyber Controller", body or "",
                                  QSystemTrayIcon.Information, 5000)

    _bridge = _NativeBridge(window)
    _channel = QWebChannel(view.page())
    _channel.registerObject("ccbridge_native", _bridge)
    view.page().setWebChannel(_channel)

    # Inject qwebchannel.js (shipped as a Qt resource) + a tiny bootstrap that publishes
    # window.ccbridge, so the page can call the native pickers. DocumentReady = window exists; the
    # bridge is ready well before any user click. Fires 'ccbridge-ready' so the UI reveals Browse.
    def _read_qrc(qrc_path: str) -> str:
        f = QFile(qrc_path)
        if f.open(QIODevice.ReadOnly):
            try:
                return bytes(f.readAll()).decode("utf-8")
            finally:
                f.close()
        return ""

    _qwc_js = _read_qrc(":/qtwebchannel/qwebchannel.js")
    if _qwc_js:
        _boot = _qwc_js + (
            "\nnew QWebChannel(qt.webChannelTransport, function(channel){"
            "window.ccbridge = channel.objects.ccbridge_native;"
            "window.dispatchEvent(new Event('ccbridge-ready'));});\n"
        )
        _script = QWebEngineScript()
        _script.setName("cc-webchannel-bootstrap")
        _script.setSourceCode(_boot)
        _script.setInjectionPoint(QWebEngineScript.DocumentReady)
        _script.setWorldId(QWebEngineScript.MainWorld)
        _script.setRunsOnSubFrames(False)
        view.page().profile().scripts().insert(_script)
    else:
        log.warning("qwebchannel.js resource missing — native file bridge off (web fallback used)")

    # Downloads (P0-3): route any download the page triggers to a native Save dialog.
    def _on_download(item) -> None:
        suggested = item.path() or getattr(item, "downloadFileName", lambda: "")() or ""
        path, _ = QFileDialog.getSaveFileName(window, "Save file", suggested)
        if path:
            item.setPath(path)
            item.accept()
        else:
            item.cancel()

    view.page().profile().downloadRequested.connect(_on_download)

    # Settings hardening (leanness + attack-surface): disable engine features the reform UI never uses.
    _settings = view.settings()
    for _name, _on in (
        ("PluginsEnabled", False),
        ("WebGLEnabled", False),
        ("ScreenCaptureEnabled", False),
        ("FullScreenSupportEnabled", False),
        ("HyperlinkAuditingEnabled", False),
        ("DnsPrefetchEnabled", False),
        ("PdfViewerEnabled", False),
    ):
        _attr = getattr(QWebEngineSettings, _name, None)
        if _attr is not None:
            _settings.setAttribute(_attr, _on)

    view.load(QUrl(url))
    window.setCentralWidget(view)

    # Adaptive sizing (P0-5): clamp the minimum + launch size to the real screen so a small cyberdeck
    # panel is usable (the old hardcoded 900-wide minimum made an 800x480 / 1024x600 deck unusable).
    from src.ui.qt.screen import adaptive_launch_size, adaptive_minimum_size
    _geo = app.primaryScreen().availableGeometry()
    _min_w, _min_h = adaptive_minimum_size(_geo.width(), _geo.height())
    _lw, _lh = adaptive_launch_size(_geo.width(), _geo.height())
    window.setMinimumSize(_min_w, _min_h)
    _saved_geo = _qsettings.value("window/geometry")
    if _saved_geo is None or not window.restoreGeometry(_saved_geo):
        window.resize(_lw, _lh)   # first run, or a stale/invalid saved geometry -> adaptive default

    # Native keyboard shortcuts (P1-11) — app-level QActions, NO visible menu bar (keep the exact
    # chromeless HTML look). Zoom / reload / fullscreen / quit: the desktop reflexes the mockup lacks.
    def _shortcut(seq, fn):
        act = QAction(window)
        act.setShortcut(QKeySequence(seq))
        act.setShortcutContext(Qt.ApplicationShortcut)
        act.triggered.connect(fn)
        window.addAction(act)

    def _zoom(delta):
        view.setZoomFactor(max(0.5, min(3.0, round(view.zoomFactor() + delta, 2))))

    _shortcut("Ctrl+Q", app.quit)
    _shortcut("Ctrl++", lambda: _zoom(0.1))
    _shortcut("Ctrl+=", lambda: _zoom(0.1))
    _shortcut("Ctrl+-", lambda: _zoom(-0.1))
    _shortcut("Ctrl+0", lambda: view.setZoomFactor(1.0))
    _shortcut("F5", view.reload)
    _shortcut("F11", lambda: window.showNormal() if window.isFullScreen() else window.showFullScreen())

    window.show()
    log.info("Opening Cyber Controller PyQt/QtWebEngine window (loopback :%d)", port)
    return app.exec_()
