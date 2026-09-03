"""When the native desktop window can't open (e.g. no WebView2 runtime), the app must fall back to
the browser UI — never die silently with no window. This guards the "installs but won't launch" fix."""
from __future__ import annotations

import sys
import types

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.ui.web import desktop


def test_desktop_falls_back_to_browser_when_webview_backend_fails(monkeypatch):
    # A fake pywebview whose start() raises the way a missing WebView2 runtime does.
    fake_webview = types.ModuleType("webview")
    fake_webview.create_window = lambda *a, **k: None
    def _boom():
        raise RuntimeError("WebView2Loader.dll not found")
    fake_webview.start = _boom
    monkeypatch.setitem(sys.modules, "webview", fake_webview)

    # Don't stand up a real server or thread; pretend it's serving so we reach the window step.
    monkeypatch.setattr(desktop.threading, "Thread", lambda *a, **k: types.SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(desktop, "_wait_until_serving", lambda *a, **k: True)

    opened: list[str] = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

    # Break the keep-alive loop on the first tick so the test returns.
    def _stop(_):
        raise KeyboardInterrupt
    monkeypatch.setattr(desktop.time, "sleep", _stop)

    rc = desktop.launch_desktop(DeviceManager(), FlashEngine(), EventBus(), TargetPool())

    assert rc == 0                                   # graceful, not a crash
    assert opened, "the browser should have been opened as the fallback"
    assert opened[0].startswith("http://127.0.0.1:")  # the loopback UI, with the auth token
    assert "desktop-auth?token=" in opened[0]


def test_desktop_shell_module_still_importable():
    # The pywebview shell must import cleanly (webview stays a lazy, optional import).
    from src.ui.web import desktop as d

    assert callable(d.launch_desktop)
