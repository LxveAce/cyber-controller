"""Regression — a macro save/delete error must surface a dialog, never escape the clicked-slot.

Root cause (macro_tab.py `_on_save` / `_on_delete_macro`): both called `save_macro` / `delete_macro`
with NO try/except. With the secure-container gate locked, `save_macro` RAISES; the exception escaped
the QPushButton.clicked slot and — with no sys.excepthook installed — PyQt aborts the whole app, losing
the just-recorded macro (or, absent abort, the save silently no-ops). The sibling `settings_tab._on_save`
and the macro load paths already guard with try/except + QMessageBox; save/delete were unguarded.

The fix wraps both in try/except + QMessageBox.critical. Offscreen Qt, mirroring
tests/test_macro_selection_load_failure.py."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QListWidgetItem, QMessageBox  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_tab(macros_dir):
    from src.core.device_manager import DeviceManager
    from src.core.macro_recorder import MacroRecorder
    from src.ui.qt.macro_tab import MacroTab

    return MacroTab(MacroRecorder(macros_dir=macros_dir), DeviceManager())


def _write_valid_macro(path):
    path.write_text(
        json.dumps(
            {
                "name": "Macro A",
                "description": "good",
                "steps": [{"command": "reboot", "delay_ms": 100, "expected_response": ""}],
                "device_protocol": "marauder",
            }
        ),
        encoding="utf-8",
    )


def _boom(*_a, **_k):
    raise RuntimeError("secure container gate is locked")


def test_save_error_surfaces_dialog_not_crash(qapp, tmp_path, monkeypatch):
    criticals = []
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: criticals.append(a)))

    tab = _make_tab(tmp_path)
    good = tmp_path / "m.json"
    _write_valid_macro(good)
    tab._current_macro = tab._recorder.load_macro(good)

    monkeypatch.setattr(tab._recorder, "save_macro", _boom)

    # Must NOT raise out of the slot (an unhandled slot exception aborts the app under PyQt).
    tab._on_save()

    assert len(criticals) == 1  # the failure was surfaced, not swallowed or crashed


def test_delete_error_surfaces_dialog_not_crash(qapp, tmp_path, monkeypatch):
    criticals = []
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: criticals.append(a)))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))

    tab = _make_tab(tmp_path)
    good = tmp_path / "m.json"
    _write_valid_macro(good)

    item = QListWidgetItem(str(good))
    item.setData(Qt.UserRole, str(good))
    tab._macro_list.addItem(item)
    tab._macro_list.setCurrentItem(item)

    monkeypatch.setattr(tab._recorder, "delete_macro", _boom)

    tab._on_delete_macro()  # must not raise

    assert len(criticals) == 1
