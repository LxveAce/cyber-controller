"""Renderer routing in src.app._launch_desktop, exercised with call recorders only.

_launch_desktop is the packaged app's window selector: CC_DESKTOP_SHELL forces a shell, otherwise
pywebview is tried first, the QtWebEngine shell second, and the browser last. Every backend is a
recorder here; no server, window, thread or socket is created.
"""
from __future__ import annotations

import importlib.util
import sys
import types

import pytest

from src import app

ARGS = tuple(object() for _ in range(4))
VAULT, HEALTH, MACRO, AUDIT = object(), object(), object(), object()


class CleanupError(RuntimeError):
    pass


@pytest.fixture
def routing(monkeypatch):
    script = {"pywebview_installed": True, "pywebview_rc": 0, "pywebview_raises": None,
              "qt_rc": 0, "web_rc": 0}
    calls = []
    captured = {}
    monkeypatch.delenv("CC_DESKTOP_SHELL", raising=False)

    lifecycle = types.ModuleType("src.ui.web.server_lifecycle")
    lifecycle.DesktopCleanupError = CleanupError
    monkeypatch.setitem(sys.modules, "src.ui.web.server_lifecycle", lifecycle)

    def find_spec(name, *rest):
        assert name == "webview", f"unexpected probe for {name}"
        calls.append("probe")
        return object() if script["pywebview_installed"] else None

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)

    def launch_desktop(*args, **kwargs):
        calls.append("pywebview")
        captured["pywebview"] = (args, kwargs)
        if script["pywebview_raises"] is not None:
            raise script["pywebview_raises"]
        return script["pywebview_rc"]

    desktop = types.ModuleType("src.ui.web.desktop")
    desktop.launch_desktop = launch_desktop
    monkeypatch.setitem(sys.modules, "src.ui.web.desktop", desktop)

    def launch_qt(*args, **kwargs):
        calls.append("qt")
        captured["qt"] = (args, kwargs)
        return script["qt_rc"]

    def launch_web(*args, **kwargs):
        calls.append("web")
        captured["web"] = (args, kwargs)
        return script["web_rc"]

    monkeypatch.setattr(app, "_launch_desktop_qt", launch_qt)
    monkeypatch.setattr(app, "_launch_web", launch_web)

    def run():
        return app._launch_desktop(*ARGS, VAULT, HEALTH, MACRO, audit=AUDIT)

    return types.SimpleNamespace(script=script, calls=calls, captured=captured, run=run,
                                 setenv=lambda v: monkeypatch.setenv("CC_DESKTOP_SHELL", v))


def test_qtweb_forces_qt_without_consulting_pywebview(routing):
    routing.setenv("qtweb")
    routing.script["qt_rc"] = 7
    assert routing.run() == 7
    assert routing.calls == ["qt"]


def test_shell_value_is_trimmed_and_case_folded(routing):
    routing.setenv("  QtWeb ")
    assert routing.run() == 0
    assert routing.calls == ["qt"]


def test_qt_receives_every_argument(routing):
    routing.setenv("qtweb")
    routing.run()
    args, kwargs = routing.captured["qt"]
    assert args == (*ARGS, VAULT, HEALTH, MACRO, AUDIT)
    assert kwargs == {}


def test_pywebview_success_returns_without_qt(routing):
    assert routing.run() == 0
    assert routing.calls == ["probe", "pywebview"]
    args, kwargs = routing.captured["pywebview"]
    assert args == ARGS
    assert kwargs == {"audit": AUDIT}


def test_pywebview_nonzero_falls_through_to_qt(routing):
    routing.script["pywebview_rc"] = 1
    assert routing.run() == 0
    assert routing.calls == ["probe", "pywebview", "qt"]


def test_pywebview_exception_falls_through_to_qt(routing):
    routing.script["pywebview_raises"] = RuntimeError("WebView2Loader.dll not found (double)")
    assert routing.run() == 0
    assert routing.calls == ["probe", "pywebview", "qt"]


def test_cleanup_error_reraises_without_qt(routing):
    routing.script["pywebview_raises"] = CleanupError("listener still owned (double)")
    with pytest.raises(CleanupError):
        routing.run()
    assert routing.calls == ["probe", "pywebview"]


def test_forced_pywebview_that_is_absent_returns_1_without_qt(routing):
    routing.setenv("pywebview")
    routing.script["pywebview_installed"] = False
    assert routing.run() == 1
    assert routing.calls == ["probe"]


def test_forced_pywebview_success_returns_0(routing):
    routing.setenv("pywebview")
    assert routing.run() == 0
    assert routing.calls == ["probe", "pywebview"]


def test_pywebview_absent_goes_straight_to_qt(routing):
    routing.script["pywebview_installed"] = False
    assert routing.run() == 0
    assert routing.calls == ["probe", "qt"]


def test_qt_failure_falls_back_to_browser_on_loopback_defaults(routing):
    routing.script["pywebview_installed"] = False
    routing.script["qt_rc"] = 1
    assert routing.run() == 0
    assert routing.calls == ["probe", "qt", "web"]
    args, kwargs = routing.captured["web"]
    assert args == (*ARGS, VAULT, HEALTH, MACRO)
    assert kwargs == {"audit": AUDIT}, "the browser fallback keeps the loopback host/port defaults"


def test_every_shell_failing_returns_the_browser_code(routing):
    routing.script.update(pywebview_rc=1, qt_rc=1, web_rc=3)
    assert routing.run() == 3
    assert routing.calls == ["probe", "pywebview", "qt", "web"]


def test_unknown_shell_value_is_ignored(routing):
    routing.setenv("something-else")
    assert routing.run() == 0
    assert routing.calls == ["probe", "pywebview"]
