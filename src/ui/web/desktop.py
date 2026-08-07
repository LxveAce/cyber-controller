"""Desktop shell for the web UI (GUI-stack pivot).

Runs the hardened Flask + SocketIO app in-process on loopback and renders it in a native
pywebview window — so the reform UI (Ace's approved mockup HTML/CSS) IS the desktop app, with
pixel-for-pixel fidelity the Qt stylesheet path can't reach, while the whole Python core carries
over unchanged.

Security posture (deliberately tighter than ``--ui web``):
  * binds 127.0.0.1 on an ephemeral port — there is NO LAN listener. The window talks to the
    server in-process; nothing off-box can reach it.
  * the existing web auth gate is UNTOUCHED. The shell generates a random one-time credential for
    THIS process (unless the operator already set CC_WEB_USER/CC_WEB_PASS) and authenticates the
    window with it, so the loopback port is not open even to another local user.

pywebview is an optional dependency (``pip install pywebview``); if it is missing we say so and
fall back to the ``--ui web`` browser path rather than crashing.
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
import threading
import time
from typing import Any
from urllib.parse import quote

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine

log = logging.getLogger(__name__)

_LANDING = "reform"  # the Phase-1 proof surface (DEVICE ▸ Dashboard, live)


def _free_loopback_port() -> int:
    """Grab an ephemeral port the OS just handed us, then release it for the server to re-bind.

    Small TOCTOU window (another process could steal it before the server binds), but on loopback
    for a desktop launch that is acceptable and self-corrects on the next launch."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_serving(port: int, timeout: float = 15.0) -> bool:
    """Block until the loopback server accepts a TCP connection (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.15)
    return False


def launch_desktop(
    device_manager: DeviceManager,
    flash_engine: FlashEngine,
    event_bus: EventBus,
    target_pool: TargetPool,
    *,
    audit: Any = None,
) -> int:
    """Run the web UI inside a native desktop window. Returns a process exit code."""
    try:
        import webview  # pywebview — optional dependency
    except ImportError:
        log.error(
            "pywebview is not installed — the desktop shell needs it.\n"
            "    pip install pywebview\n"
            "Meanwhile the same UI is available in a browser via:  cyber-controller --ui web"
        )
        return 1

    # One-time random credential for this process, so the loopback port isn't open even to another
    # local user. Respect an operator-provided credential if one is already set.
    if not os.environ.get("CC_WEB_PASS"):
        os.environ["CC_WEB_USER"] = os.environ.get("CC_WEB_USER", "cc-desktop")
        os.environ["CC_WEB_PASS"] = secrets.token_urlsafe(24)
    user = os.environ["CC_WEB_USER"]
    password = os.environ["CC_WEB_PASS"]

    port = _free_loopback_port()

    # launch_web() blocks on socketio.run(), so run it on a daemon thread; the window owns the main
    # thread (a pywebview requirement). The daemon dies with the process when the window closes.
    from src.ui.web.app import launch_web

    def _serve() -> None:
        try:
            launch_web(
                device_manager, flash_engine, event_bus, target_pool,
                host="127.0.0.1", port=port, audit=audit,
            )
        except Exception:
            log.exception("Desktop web server thread crashed")

    threading.Thread(target=_serve, name="cc-desktop-web", daemon=True).start()

    if not _wait_until_serving(port):
        log.error("Web server did not come up on 127.0.0.1:%d in time", port)
        return 1

    # Basic-auth creds in the URL authenticate the very first request; the server rotates the
    # session cookie at the auth boundary, so the credential rides exactly one navigation.
    creds = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    url = f"http://{creds}127.0.0.1:{port}/{_LANDING}"

    log.info("Opening Cyber Controller desktop window (loopback :%d)", port)
    webview.create_window(
        "Cyber Controller",
        url,
        width=1280,
        height=820,
        min_size=(900, 600),
    )
    webview.start()  # blocks until the window is closed
    return 0
