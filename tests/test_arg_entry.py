"""Argument-entry helpers (coverage P4): commands with <...> placeholders prompt for values instead of
sending the literal token. Tests the pure helpers + the no-placeholder passthrough. Offscreen."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_placeholder_tokens_keeps_duplicates(qapp):
    from src.ui.qt.device_tab import DeviceTab
    assert DeviceTab._placeholder_tokens("led -r <v> -g <v> -b <v>") == ["v", "v", "v"]
    assert DeviceTab._placeholder_tokens("scanap -c <ch>") == ["ch"]
    assert DeviceTab._placeholder_tokens("led set <r> <g> <b>") == ["r", "g", "b"]
    assert DeviceTab._placeholder_tokens("scanap") == []


def test_substitute_tokens_occurrence_ordered(qapp):
    from src.ui.qt.device_tab import DeviceTab
    # repeated <v> must consume three separate values, not collapse to one
    assert DeviceTab._substitute_tokens("led -r <v> -g <v> -b <v>", ["10", "20", "30"]) == "led -r 10 -g 20 -b 30"
    assert DeviceTab._substitute_tokens("scanap -c <ch>", ["6"]) == "scanap -c 6"
    assert DeviceTab._substitute_tokens("select -a <idx>", ["3"]) == "select -a 3"


def test_sanitize_arg(qapp):
    from src.ui.qt.device_tab import DeviceTab
    assert DeviceTab._sanitize_arg("  6\n") == "6"               # trim + drop newline
    assert DeviceTab._sanitize_arg("a<b>c") == "abc"             # strip angle brackets (no token smuggling)
    assert DeviceTab._sanitize_arg("x" * 100) == "x" * 64        # 64-char cap
    assert DeviceTab._sanitize_arg("a\x01b") == "ab"             # drop control chars


def test_resolve_no_tokens_is_passthrough(qapp):
    from src.core.device_manager import DeviceManager
    from src.ui.qt.device_tab import DeviceTab
    tab = DeviceTab(DeviceManager())
    # No placeholders -> returns unchanged, no dialog.
    assert tab._resolve_placeholders("scanap", None) == "scanap"
