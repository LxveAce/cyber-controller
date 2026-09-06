"""Interactive startup and exit boundaries with inert UI/services and no operator state."""
from __future__ import annotations

import builtins
import importlib.util
import sys
from types import SimpleNamespace

import pytest

from src import app
from src.core import install
from src.security import access_gate


@pytest.fixture
def startup(monkeypatch):
    events = []
    manager = SimpleNamespace(shutdown=lambda: events.append("shutdown"))

    def bootstrap():
        events.append("bootstrap")
        return (manager, *([None] * 7))

    monkeypatch.setattr(app, "_setup_logging", lambda *a: None)
    monkeypatch.setattr(install, "reconcile", lambda: None)
    monkeypatch.setattr(app, "_acquire_instance_lock", lambda: True)
    monkeypatch.setattr(app, "_bootstrap", bootstrap)
    monkeypatch.setattr(access_gate, "enforce", lambda ui: events.append(("gate", ui)) or True)
    monkeypatch.setitem(sys.modules, "pyi_splash", SimpleNamespace(
        close=lambda: events.append("splash")))
    for mode in ("desktop", "web"):
        monkeypatch.setitem(app._LAUNCHERS, mode,
                            lambda *a, mode=mode, **kw: events.append(("launch", mode)) or 0)
    return events, manager


@pytest.mark.parametrize("mode", ["desktop", "web", "qt"])
@pytest.mark.parametrize("allowed", [False, True])
def test_explicit_ui_closes_splash_before_access_gate(startup, monkeypatch, mode, allowed):
    events, _manager = startup
    monkeypatch.setattr(access_gate, "enforce", lambda ui: events.append(("gate", ui)) or allowed)
    assert app.main(["--ui", mode]) == (0 if allowed else 1)
    assert events[0] == "splash"
    assert events[1] == ("gate", "web" if mode == "web" else "desktop")
    assert events.count("bootstrap") == events.count("shutdown") == int(allowed)


def test_default_ui_closes_splash_before_chooser(startup, monkeypatch):
    events, _manager = startup
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "src.ui.launcher", SimpleNamespace(
        select_ui=lambda: events.append("chooser") or "desktop"))
    assert app.main([]) == 0
    assert events[:3] == ["splash", "chooser", ("gate", "desktop")]


@pytest.mark.parametrize("fault", ["absent", "broken"])
def test_optional_splash_failure_preserves_denied_gate(startup, monkeypatch, fault):
    events, _manager = startup
    if fault == "absent":
        # None in sys.modules deterministically models ImportError, even in a frozen test process.
        monkeypatch.setitem(sys.modules, "pyi_splash", None)
    else:
        def broken():
            raise OSError("synthetic unavailable splash IPC")
        monkeypatch.setitem(sys.modules, "pyi_splash", SimpleNamespace(close=broken))
    monkeypatch.setattr(access_gate, "enforce", lambda ui: events.append(("gate", ui)) or False)
    assert app.main(["--ui", "desktop"]) == 1
    assert events == [("gate", "desktop")]


@pytest.mark.parametrize("mode,module,wrapper", [
    ("browser", "src.ui.web.app", app._launch_web),
    ("Qt desktop", "src.ui.web.desktop_qt", app._launch_desktop_qt),
])
@pytest.mark.parametrize("stage", ["module-import", "runtime-import"])
def test_import_failure_reports_actual_module_and_keeps_fallback_contract(
    monkeypatch, caplog, mode, module, wrapper, stage,
):
    error = ImportError("synthetic transitive dependency could not load", name="dependency_fixture")

    def fail(*a, **kw):
        raise error

    monkeypatch.setattr(app, "_open_browser_when_ready", lambda *a: None)
    if stage == "module-import":
        real_import = builtins.__import__

        def importer(name, *args, **kwargs):
            if name == module:
                raise error
            return real_import(name, *args, **kwargs)
        monkeypatch.setattr(builtins, "__import__", importer)
    else:
        monkeypatch.setitem(sys.modules, module, SimpleNamespace(
            launch_web=fail, launch_desktop_qt=fail))
    assert wrapper(None, None, None, None) == 1
    assert "dependency_fixture" in caplog.text
    assert mode.lower() in caplog.text.lower()
    assert "Flask is not installed" not in caplog.text
    assert "Neither pywebview nor PyQtWebEngine is installed" not in caplog.text
    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)


@pytest.mark.parametrize("module,wrapper", [
    ("src.ui.web.app", app._launch_web),
    ("src.ui.web.desktop_qt", app._launch_desktop_qt),
])
def test_non_import_runtime_errors_are_not_relabelled(monkeypatch, module, wrapper):
    error = RuntimeError("inert application failure")

    def fail(*a, **kw):
        raise error

    monkeypatch.setattr(app, "_open_browser_when_ready", lambda *a: None)
    monkeypatch.setitem(sys.modules, module, SimpleNamespace(launch_web=fail, launch_desktop_qt=fail))
    with pytest.raises(RuntimeError) as caught:
        wrapper(None, None, None, None)
    assert caught.value is error


def test_forced_pywebview_failure_does_not_claim_package_is_missing(monkeypatch, caplog):
    monkeypatch.setenv("CC_DESKTOP_SHELL", "pywebview")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setitem(sys.modules, "src.ui.web.desktop", SimpleNamespace(
        launch_desktop=lambda *a, **kw: 1))
    monkeypatch.setattr(app, "_launch_desktop_qt", lambda *a, **kw: pytest.fail("forced shell fell back"))
    assert app._launch_desktop(None, None, None, None) == 1
    assert "pip install" not in caplog.text
    assert "could not start" in caplog.text


@pytest.mark.parametrize("ui_code,expected", [(0, 130), (1, 1), (17, 17)])
def test_shutdown_interrupt_is_nonzero_without_retry_or_masking_ui_failure(
    startup, monkeypatch, caplog, ui_code, expected,
):
    events, manager = startup

    def interrupted():
        events.append("shutdown")
        raise KeyboardInterrupt

    manager.shutdown = interrupted
    monkeypatch.setitem(app._LAUNCHERS, "desktop", lambda *a, **kw: ui_code)
    try:
        result = app.main(["--ui", "desktop"])
    except KeyboardInterrupt:
        pytest.fail("final shutdown interruption escaped main")
    assert result == expected
    assert events.count("shutdown") == 1
    assert "cleanup may be incomplete" in caplog.text


def test_ui_interrupt_still_attempts_cleanup(startup, monkeypatch):
    events, _manager = startup

    def interrupted(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setitem(app._LAUNCHERS, "desktop", interrupted)
    assert app.main(["--ui", "desktop"]) == 0
    assert events.count("shutdown") == 1


def test_ui_systemexit_survives_shutdown_interrupt(startup, monkeypatch):
    events, manager = startup

    def quit_ui(*a, **kw):
        raise SystemExit(23)

    def interrupted():
        events.append("shutdown")
        raise KeyboardInterrupt

    manager.shutdown = interrupted
    monkeypatch.setitem(app._LAUNCHERS, "desktop", quit_ui)
    try:
        with pytest.raises(SystemExit) as caught:
            app.main(["--ui", "desktop"])
    except KeyboardInterrupt:
        pytest.fail("shutdown interruption replaced the original SystemExit")
    assert caught.value.code == 23
    assert events.count("shutdown") == 1


def test_unrelated_shutdown_failure_still_propagates(startup):
    events, manager = startup
    error = OSError("synthetic cleanup failure")

    def fail():
        events.append("shutdown")
        raise error

    manager.shutdown = fail
    with pytest.raises(OSError) as caught:
        app.main(["--ui", "desktop"])
    assert caught.value is error
    assert events.count("shutdown") == 1
