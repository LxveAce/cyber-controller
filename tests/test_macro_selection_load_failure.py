"""Regression — a failed list-selection load must never leave the previously loaded macro
silently armed for Play.

Root cause (macro_tab.py `_on_macro_selected`): when `_recorder.load_macro(path)` raised, the
except only logged. `_current_macro` kept pointing at the *previously* loaded macro and Play stayed
enabled, so pressing Play would execute the old macro even though the user selected a different one.
The sibling file-dialog path `_on_load_file` already surfaced the error via QMessageBox; the list path
silently diverged.

The fix surfaces the failure (QMessageBox, mirroring _on_load_file) AND clears stale state
(`_current_macro = None` + `_clear_display()` so Play is disabled). Offscreen Qt, mirroring
tests/test_fill_from_target.py."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QApplication,
    QListWidgetItem,
    QMessageBox,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_tab(macros_dir):
    from src.core.device_manager import DeviceManager
    from src.core.macro_recorder import MacroRecorder
    from src.ui.qt.macro_tab import MacroTab

    return MacroTab(MacroRecorder(macros_dir=macros_dir), DeviceManager())


def _item_for(path):
    item = QListWidgetItem(str(path))
    item.setData(Qt.UserRole, str(path))
    return item


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


def test_failed_selection_disarms_previous_macro(qapp, tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a))
    )

    tab = _make_tab(tmp_path)

    # 1) Macro A loads OK via the list path: Play enabled, editor shows A.
    good = tmp_path / "macro_a.json"
    _write_valid_macro(good)
    tab._on_macro_selected(_item_for(good), None)

    assert tab._current_macro is not None
    assert tab._current_macro.name == "Macro A"
    assert tab._btn_play.isEnabled()
    assert tab._macro_name_label.text() == "Macro A"

    # 2) Select a corrupt macro — load_macro raises (invalid JSON).
    bad = tmp_path / "macro_bad.json"
    bad.write_text("{ this is not valid json", encoding="utf-8")
    tab._on_macro_selected(_item_for(bad), None)

    # The failure is surfaced to the user...
    assert len(warnings) == 1
    # ...and stale state is cleared so the old macro can't be silently played.
    assert tab._current_macro is None
    assert not tab._btn_play.isEnabled()
    assert tab._macro_name_label.text() == "No macro loaded"
