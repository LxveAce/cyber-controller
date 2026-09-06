"""When the native desktop window can't open (e.g. no WebView2 runtime), the app must fall back to
the browser UI — never die silently with no window. This guards the "installs but won't launch" fix."""
from __future__ import annotations

import sys
import types

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.ui.web import app as webapp
from src.ui.web import desktop


def _server_stub(*_args, **_kwargs):
    return types.SimpleNamespace(port=12345, start=lambda: None,
                                 wait_ready=lambda: True, close=lambda: None)


def test_desktop_falls_back_to_browser_when_webview_backend_fails(monkeypatch):
    # A fake pywebview whose start() raises the way a missing WebView2 runtime does.
    fake_webview = types.ModuleType("webview")
    fake_webview.create_window = lambda *a, **k: None
    def _boom():
        raise RuntimeError("WebView2Loader.dll not found")
    fake_webview.start = _boom
    monkeypatch.setitem(sys.modules, "webview", fake_webview)

    # Don't stand up a real server or thread; pretend it's serving so we reach the window step.
    monkeypatch.setattr(webapp, "create_desktop_server", _server_stub)

    opened: list[str] = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    # Break the keep-alive loop on the first tick so the test returns.
    def _stop(_):
        raise KeyboardInterrupt
    monkeypatch.setattr(desktop, "time", types.SimpleNamespace(sleep=_stop))

    rc = desktop.launch_desktop(DeviceManager(), FlashEngine(), EventBus(), TargetPool())

    assert rc == 0                                   # graceful, not a crash
    assert opened, "the browser should have been opened as the fallback"
    assert opened[0].startswith("http://127.0.0.1:")  # the loopback UI, with the auth token
    assert "desktop-auth?token=" in opened[0]


def test_desktop_shell_module_still_importable():
    # The pywebview shell must import cleanly (webview stays a lazy, optional import).
    from src.ui.web import desktop as d

    assert callable(d.launch_desktop)


def test_desktop_does_not_wait_when_browser_fallback_cannot_open(monkeypatch, caplog):
    fake_webview = types.ModuleType("webview")
    fake_webview.create_window = lambda *a, **k: None

    def fail_native():
        raise RuntimeError("native backend unavailable")

    fake_webview.start = fail_native
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr(webapp, "create_desktop_server", _server_stub)
    monkeypatch.setenv("CC_WEB_PASS", "synthetic-test-credential")

    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda _url: False)

    def must_not_wait(_seconds):
        raise AssertionError("No window or browser opened; keep-alive would leave the user stranded")

    monkeypatch.setattr(desktop, "time", types.SimpleNamespace(sleep=must_not_wait))
    assert desktop.launch_desktop(object(), object(), object(), object()) == 1
    assert "browser fallback could not open" in caplog.text
