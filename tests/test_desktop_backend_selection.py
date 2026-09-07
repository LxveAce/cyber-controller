"""Frozen Linux builds name pywebview's bundled Qt backend explicitly; every other case leaves the
choice to pywebview.

Process metadata (sys.frozen, sys.platform, the environment) and webview are doubles. No backend is
imported, no window opens, no server starts, nothing is written to the environment.
"""
from __future__ import annotations

import logging
import os
import sys

import pytest

from src.ui.web import desktop

LOGGER = "src.ui.web.desktop"
PORT = 45678


@pytest.mark.parametrize("platform, frozen, env, expected", [
    ("linux", True, {}, "qt"),
    ("linux", True, {"PYWEBVIEW_GUI": ""}, "qt"),
    ("linux", True, {"PYWEBVIEW_GUI": "   "}, "qt"),
    ("linux", True, {"PYWEBVIEW_GUI": "gtk"}, None),
    ("linux", True, {"PYWEBVIEW_GUI": "qt"}, None),
    ("linux", False, {}, None),
    ("win32", True, {}, None),
    ("darwin", True, {}, None),
    ("win32", False, {}, None),
])
def test_preferred_backend_matrix(platform, frozen, env, expected):
    assert desktop._preferred_backend(platform=platform, frozen=frozen, environ=env) == expected


def test_defaults_read_the_process_metadata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("PYWEBVIEW_GUI", raising=False)
    assert desktop._preferred_backend() == "qt"
    monkeypatch.setenv("PYWEBVIEW_GUI", "gtk")
    assert desktop._preferred_backend() is None, "an explicit choice is pywebview's to read"
    monkeypatch.delenv("PYWEBVIEW_GUI", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert desktop._preferred_backend() is None, "source install"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert desktop._preferred_backend() is None, "other platform"


# The window step, with webview and the bootstrap doubled.

class FakeWebview:
    def __init__(self, *, fail=False):
        self.windows = []
        self.starts = []
        self.fail = fail

    def create_window(self, title, url, **kwargs):
        self.windows.append((title, url, kwargs))

    def start(self, *args, **kwargs):
        self.starts.append((args, kwargs))
        if self.fail:
            raise RuntimeError("backend failed (double)")


class FakeBootstrap:
    def __init__(self):
        self.tokens = iter(["second", "third"])

    def rotate(self):
        return next(self.tokens)


def run_window(monkeypatch, *, platform, frozen, env=None, fail=False):
    monkeypatch.setattr(sys, "platform", platform)
    if frozen:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    else:
        monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv("PYWEBVIEW_GUI", raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    webview = FakeWebview(fail=fail)
    code = desktop._run_desktop_window(webview, PORT, FakeBootstrap(), "first")
    return webview, code


def test_frozen_linux_starts_with_the_bundled_backend(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        webview, code = run_window(monkeypatch, platform="linux", frozen=True)
    assert code == 0
    assert webview.starts == [((), {"gui": "qt"})]
    assert webview.windows[0][1] == f"http://127.0.0.1:{PORT}/desktop-auth?token=first"
    assert "using the bundled qt webview backend" in caplog.text
    assert "PYWEBVIEW_GUI" in caplog.text, "the override is named for the user"


@pytest.mark.parametrize("platform, frozen, env", [
    ("linux", True, {"PYWEBVIEW_GUI": "gtk"}),
    ("linux", False, {}),
    ("win32", True, {}),
    ("darwin", True, {}),
])
def test_other_cases_leave_the_choice_to_pywebview(monkeypatch, platform, frozen, env):
    webview, code = run_window(monkeypatch, platform=platform, frozen=frozen, env=env)
    assert code == 0
    assert webview.starts == [((), {})], "plain start(): pywebview applies its own selection"


def test_nothing_is_written_to_the_environment(monkeypatch):
    monkeypatch.delenv("PYWEBVIEW_GUI", raising=False)
    before = dict(os.environ)
    run_window(monkeypatch, platform="linux", frozen=True)
    after = dict(os.environ)
    assert "PYWEBVIEW_GUI" not in after
    assert set(after) - set(before) == set()


def test_backend_failure_on_frozen_linux_still_falls_back_to_the_browser(monkeypatch):
    opened = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    def interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(desktop.time, "sleep", interrupt)
    webview, code = run_window(monkeypatch, platform="linux", frozen=True, fail=True)
    assert code == 0
    assert webview.starts == [((), {"gui": "qt"})]
    assert opened == [f"http://127.0.0.1:{PORT}/desktop-auth?token=second"], "rotated token"
