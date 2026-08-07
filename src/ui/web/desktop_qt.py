"""PyQt + QtWebEngine desktop shell for the web UI (owner's choice, 2026-08-07).

This is "the PyQt app, skinned as the HTML": a native PyQt ``QMainWindow`` whose entire body is a
``QWebEngineView`` rendering the reform mockup's own HTML/CSS/JS. It reuses the whole web stack
unchanged — ``reform.html``/``reform.css``/``reform.js`` + the hardened Flask + SocketIO bridge + the
Python core. Only the window changes: a Qt/Chromium web view instead of pywebview's OS webview.

Why this over ``desktop.py`` (pywebview): the owner wants a genuine PyQt application. The trade-off is
weight — QtWebEngine bundles Chromium — most noticeable on the Raspberry-Pi (ARM) target, where
PyQtWebEngine wheels are unreliable; the Pi path stays pywebview / ``--ui web`` until that's validated.

Security posture is identical to ``desktop.py``: binds 127.0.0.1 on an ephemeral port (no LAN
listener), the web auth gate is untouched, and a single-use bootstrap token establishes the session
on a clean URL (credentials never ride in the address, so relative ``fetch()`` / WebSocket work).

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


def launch_desktop_qt(
    device_manager: DeviceManager,
    flash_engine: FlashEngine,
    event_bus: EventBus,
    target_pool: TargetPool,
    *,
    audit: Any = None,
) -> int:
    """Run the web UI inside a native PyQt/QtWebEngine window. Returns a process exit code."""
    try:
        from PyQt5.QtCore import QUrl
        from PyQt5.QtWebEngineWidgets import QWebEngineView
        from PyQt5.QtWidgets import QApplication, QMainWindow
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

    app = QApplication.instance() or QApplication([])
    window = QMainWindow()
    window.setWindowTitle("Cyber Controller")
    view = QWebEngineView(window)
    view.load(QUrl(url))
    window.setCentralWidget(view)
    window.resize(1280, 820)
    window.setMinimumSize(900, 600)
    window.show()

    log.info("Opening Cyber Controller PyQt/QtWebEngine window (loopback :%d)", port)
    return app.exec_()
