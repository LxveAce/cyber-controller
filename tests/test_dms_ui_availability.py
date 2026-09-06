"""Every supported DMS UI fails closed before showing or reading password controls."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5.QtWidgets")

from src.core import suicide_setup  # noqa: E402
from src.ui.qt import main_window, suicide_dialog  # noqa: E402
from src.ui.tk import app as tk_app  # noqa: E402


def test_main_window_checks_runtime_before_opening_dialog(monkeypatch):
    notices = []
    monkeypatch.setattr(
        suicide_setup,
        "dms_runtime_status",
        lambda: (False, "synthetic unavailable runtime"),
    )
    monkeypatch.setattr(
        main_window.QMessageBox,
        "warning",
        lambda *_args: notices.append(_args),
    )

    class _MustNotOpen:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("password dialog must not open")

    monkeypatch.setattr(suicide_dialog, "SuicideSetupDialog", _MustNotOpen)

    main_window.CyberControllerWindow._on_suicide_setup(object())

    assert len(notices) == 1
    assert "synthetic unavailable runtime" in notices[0][-1]


def test_dialog_rechecks_runtime_before_reading_password(monkeypatch):
    notices = []
    monkeypatch.setattr(
        suicide_dialog,
        "dms_runtime_status",
        lambda: (False, "runtime disappeared"),
    )
    monkeypatch.setattr(
        suicide_dialog.QMessageBox,
        "warning",
        lambda *_args: notices.append(_args),
    )

    class _PasswordField:
        def text(self):
            raise AssertionError("password field must not be read")

    fake_dialog = type("FakeDialog", (), {"pw1": _PasswordField(), "pw2": _PasswordField()})()

    suicide_dialog.SuicideSetupDialog._on_provision(fake_dialog)

    assert len(notices) == 1
    assert "runtime disappeared" in notices[0][-1]


def test_tk_checks_runtime_before_creating_dialog(monkeypatch):
    callbacks = []
    notices = []
    monkeypatch.setattr(tk_app, "_HAS_DEADMAN", True)
    monkeypatch.setattr(
        tk_app,
        "dms_runtime_status",
        lambda: (False, "synthetic missing partition data"),
    )
    monkeypatch.setattr(
        tk_app.messagebox,
        "showwarning",
        lambda *_args: notices.append(_args),
    )
    monkeypatch.setattr(
        tk_app.tk,
        "Toplevel",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("configuration dialog must not open")
        ),
    )

    tk_app.TkLightApp._launch_deadman_setup(object(), callbacks.append)

    assert callbacks == [False]
    assert len(notices) == 1
    assert "synthetic missing partition data" in notices[0][-1]
