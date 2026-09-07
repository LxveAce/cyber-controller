"""Default UI routing for the frozen Linux bundle on a Wayland-only session.

The packaged window requires X11 or XWayland (the xcb platform). When WAYLAND_DISPLAY is set and
DISPLAY is not, a frozen Linux build must not try a window by default: Qt would abort the process,
uncatchably, before any fallback. A QT_QPA_PLATFORM value does not change that: it can be inherited
from a launcher and is not a --ui request, and neither xcb without an X display nor a platform the
bundle does not ship can open the window. Source installs, other platforms, X11, XWayland, headless
and every explicit --ui choice keep today's behaviour.

Everything is a double: sys.frozen, sys.platform, the environment, the chooser module, the access
gate, the bootstrap and the launchers. An import guard proves neither the chooser nor PyQt is
imported on a routed start. No window, display, socket, plugin file or Qt is touched.
"""
from __future__ import annotations

import builtins
import logging
import os
import sys
from types import SimpleNamespace

import pytest

from src import app as entry
from src.core import install
from src.security import access_gate

LOGGER = "cyber-controller"
REQUIRES = "the packaged window requires X11 or XWayland (xcb)"

X11 = {"DISPLAY": ":0"}
XWAYLAND = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}
WAYLAND_ONLY = {"WAYLAND_DISPLAY": "wayland-0"}
HEADLESS = {}
OVERRIDES = ["xcb", "wayland", "offscreen"]


# The pure decision.

@pytest.mark.parametrize("platform, frozen, env, expected", [
    ("linux", True, WAYLAND_ONLY, "web"),
    ("linux", True, {**WAYLAND_ONLY, "QT_QPA_PLATFORM": "   "}, "web"),
    ("linux", True, {**WAYLAND_ONLY, "QT_QPA_PLATFORM": "xcb"}, "web"),
    ("linux", True, {**WAYLAND_ONLY, "QT_QPA_PLATFORM": "wayland"}, "web"),
    ("linux", True, {**WAYLAND_ONLY, "QT_QPA_PLATFORM": "offscreen"}, "web"),
    ("linux", True, HEADLESS, "web"),
    ("linux", True, X11, None),
    ("linux", True, XWAYLAND, None),
    ("linux", True, {**XWAYLAND, "QT_QPA_PLATFORM": "xcb"}, None),
    ("linux", True, {**X11, "QT_QPA_PLATFORM": "wayland"}, None),
    ("linux", False, WAYLAND_ONLY, None),
    ("linux", False, {**WAYLAND_ONLY, "QT_QPA_PLATFORM": "xcb"}, None),
    ("linux", False, HEADLESS, "web"),
    ("linux", False, X11, None),
    ("win32", True, WAYLAND_ONLY, None),
    ("darwin", True, WAYLAND_ONLY, None),
    ("win32", True, HEADLESS, None),
])
def test_default_ui_matrix(platform, frozen, env, expected):
    ui, _message = entry._default_ui(platform=platform, frozen=frozen, environ=env)
    assert ui == expected


def test_wayland_only_frozen_reason_is_truthful():
    ui, message = entry._default_ui(platform="linux", frozen=True, environ=WAYLAND_ONLY)
    assert ui == "web"
    assert "Wayland session without X11 (DISPLAY unset)" in message
    assert REQUIRES in message
    assert "no Wayland platform support is claimed" in message
    assert "verified" not in message, "no certification wording"
    assert "--ui desktop" in message and "DISPLAY" in message, "tells the user both ways out"
    assert "QT_QPA_PLATFORM" not in message, "nothing to name when no override is set"


@pytest.mark.parametrize("value", OVERRIDES)
def test_qt_platform_override_does_not_reopen_the_window_route(value):
    env = {**WAYLAND_ONLY, "QT_QPA_PLATFORM": value}
    ui, message = entry._default_ui(platform="linux", frozen=True, environ=env)
    assert ui == "web", "an environment override is not a --ui desktop request"
    assert f"(QT_QPA_PLATFORM={value} is set and left unchanged)" in message
    assert REQUIRES in message and "defaulting to --ui web" in message
    assert env["QT_QPA_PLATFORM"] == value, "the environment value is left intact"


def test_headless_message_is_unchanged():
    assert entry._default_ui(platform="linux", frozen=True, environ=HEADLESS) == (
        "web", "No graphical display; defaulting to --ui web")


@pytest.mark.parametrize("platform, frozen, env", [
    ("linux", True, X11), ("linux", True, XWAYLAND),
    ("linux", True, {**XWAYLAND, "QT_QPA_PLATFORM": "xcb"}),
    ("linux", False, WAYLAND_ONLY),
    ("linux", False, {**WAYLAND_ONLY, "QT_QPA_PLATFORM": "wayland"}),
    ("linux", False, X11), ("win32", True, X11), ("darwin", False, HEADLESS),
])
def test_no_message_when_nothing_is_decided(platform, frozen, env):
    assert entry._default_ui(platform=platform, frozen=frozen, environ=env) == (None, None)


@pytest.mark.parametrize("platform, frozen, env, warns", [
    ("linux", True, WAYLAND_ONLY, True),
    ("linux", True, {**WAYLAND_ONLY, "QT_QPA_PLATFORM": "xcb"}, True),
    ("linux", True, XWAYLAND, False),
    ("linux", True, X11, False),
    ("linux", False, WAYLAND_ONLY, False),
    ("win32", True, WAYLAND_ONLY, False),
])
def test_window_caveat_only_for_frozen_linux_wayland_only(platform, frozen, env, warns):
    caveat = entry._window_caveat(platform=platform, frozen=frozen, environ=env)
    if warns:
        assert "Desktop window requested on a Wayland session without X11" in caveat
        assert REQUIRES in caveat and "may fail to open" in caveat and "--ui web" in caveat
        assert "verified" not in caveat
        if "QT_QPA_PLATFORM" in env:
            assert "(QT_QPA_PLATFORM=xcb is set and left unchanged)" in caveat
        else:
            assert "QT_QPA_PLATFORM" not in caveat
    else:
        assert caveat is None


def test_defaults_read_the_process_metadata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert entry._default_ui()[0] == "web"
    assert entry._window_caveat() is not None
    monkeypatch.setenv("QT_QPA_PLATFORM", "xcb")
    assert entry._default_ui()[0] == "web", "an inherited xcb override still needs an X display"
    monkeypatch.setenv("DISPLAY", ":0")
    assert entry._default_ui() == (None, None)
    assert entry._window_caveat() is None
    monkeypatch.delenv("DISPLAY")
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert entry._default_ui() == (None, None), "source install keeps the chooser"


# main() wiring with doubles and an import guard.

def start(monkeypatch, *, platform, frozen, env, chooser=None, guard_imports=True):
    events = []
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if guard_imports:
            assert name != "src.ui.launcher" and not name.startswith("PyQt"), name
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(sys, "platform", platform)
    if frozen:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    else:
        monkeypatch.delattr(sys, "frozen", raising=False)
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    if chooser is not None:
        monkeypatch.setitem(sys.modules, "src.ui.launcher", SimpleNamespace(
            select_ui=lambda: events.append("chooser") or chooser))
    monkeypatch.setattr(entry, "_setup_logging", lambda *a: None)
    monkeypatch.setattr(install, "reconcile", lambda: None)
    monkeypatch.setattr(entry, "_acquire_instance_lock", lambda: True)
    monkeypatch.setattr(access_gate, "enforce", lambda ui: events.append(("gate", ui)) or True)
    monkeypatch.setattr(entry, "_bootstrap", lambda: events.append("bootstrap") or (
        SimpleNamespace(shutdown=lambda: events.append("shutdown")), *([None] * 7)))
    monkeypatch.setitem(entry._LAUNCHERS, "web", lambda *a, **kw: events.append("web") or 0)
    monkeypatch.setitem(entry._LAUNCHERS, "desktop",
                        lambda *a, **kw: events.append("desktop") or 0)
    return events


def test_frozen_wayland_only_routes_to_web_before_any_qt_import(monkeypatch, caplog):
    events = start(monkeypatch, platform="linux", frozen=True, env=WAYLAND_ONLY)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert entry.main([]) == 0
    assert events == [("gate", "web"), "bootstrap", "web", "shutdown"]
    assert "Wayland session without X11 (DISPLAY unset)" in caplog.text
    assert "defaulting to --ui web" in caplog.text
    assert "may fail to open" not in caplog.text, "no window caveat on the web route"


@pytest.mark.parametrize("value", OVERRIDES)
def test_frozen_wayland_only_with_qt_override_still_routes_to_web(monkeypatch, caplog, value):
    events = start(monkeypatch, platform="linux", frozen=True,
                   env={**WAYLAND_ONLY, "QT_QPA_PLATFORM": value})
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert entry.main([]) == 0
    assert events == [("gate", "web"), "bootstrap", "web", "shutdown"]
    assert f"(QT_QPA_PLATFORM={value} is set and left unchanged)" in caplog.text
    assert "defaulting to --ui web" in caplog.text
    assert os.environ.get("QT_QPA_PLATFORM") == value, "the environment is not modified"


def test_frozen_xwayland_still_gets_the_chooser(monkeypatch, caplog):
    events = start(monkeypatch, platform="linux", frozen=True, env=XWAYLAND, chooser="desktop",
                   guard_imports=False)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert entry.main([]) == 0
    assert events == ["chooser", ("gate", "desktop"), "bootstrap", "desktop", "shutdown"]
    assert "Wayland session" not in caplog.text


def test_source_install_wayland_only_keeps_the_chooser(monkeypatch, caplog):
    events = start(monkeypatch, platform="linux", frozen=False,
                   env={**WAYLAND_ONLY, "QT_QPA_PLATFORM": "xcb"}, chooser="web",
                   guard_imports=False)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert entry.main([]) == 0
    assert events == ["chooser", ("gate", "web"), "bootstrap", "web", "shutdown"]
    assert "Wayland session" not in caplog.text


@pytest.mark.parametrize("requested", ["desktop", "qtweb"])
@pytest.mark.parametrize("env", [WAYLAND_ONLY, {**WAYLAND_ONLY, "QT_QPA_PLATFORM": "xcb"}])
def test_explicit_window_request_is_honoured_with_a_warning(monkeypatch, caplog, requested, env):
    events = start(monkeypatch, platform="linux", frozen=True, env=env)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert entry.main(["--ui", requested]) == 0
    assert events == [("gate", "desktop"), "bootstrap", "desktop", "shutdown"]
    assert "Desktop window requested on a Wayland session without X11" in caplog.text
    assert REQUIRES in caplog.text and "may fail to open" in caplog.text
    if "QT_QPA_PLATFORM" in env:
        assert "(QT_QPA_PLATFORM=xcb is set and left unchanged)" in caplog.text


def test_explicit_web_request_gets_no_wayland_message(monkeypatch, caplog):
    events = start(monkeypatch, platform="linux", frozen=True, env=WAYLAND_ONLY)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert entry.main(["--ui", "web"]) == 0
    assert events == [("gate", "web"), "bootstrap", "web", "shutdown"]
    assert "Wayland session" not in caplog.text


def test_headless_frozen_still_routes_to_web(monkeypatch, caplog):
    events = start(monkeypatch, platform="linux", frozen=True, env=HEADLESS)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert entry.main([]) == 0
    assert events == [("gate", "web"), "bootstrap", "web", "shutdown"]
    assert "No graphical display; defaulting to --ui web" in caplog.text


def test_other_platform_is_untouched(monkeypatch, caplog):
    events = start(monkeypatch, platform="win32", frozen=True, env=WAYLAND_ONLY, chooser="web",
                   guard_imports=False)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert entry.main([]) == 0
    assert events[0] == "chooser"
    assert "Wayland session" not in caplog.text
