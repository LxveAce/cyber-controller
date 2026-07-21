"""Offscreen smoke test for the Wardrive Qt tab. Serial-port enumeration is mocked."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_wardrive_tab(qapp, monkeypatch):
    from src.ui.qt import wardrive_tab
    monkeypatch.setattr(wardrive_tab, "_list_serial_ports",
                        lambda: [("COM5", "USB Serial"), ("COM6", "GPS")])
    tab = wardrive_tab.WardriveTab()
    assert tab._dev_combo.count() == 2
    assert tab._gps_combo.count() == 3  # "(none)" + 2 ports
    assert tab._gps_combo.itemData(0) is None
    assert tab._out_edit.text().endswith(".csv")


def _tab(monkeypatch):
    from src.ui.qt import wardrive_tab
    monkeypatch.setattr(wardrive_tab, "_list_serial_ports", lambda: [])
    return wardrive_tab.WardriveTab()


def test_wigle_upload_gate_needs_a_token(qapp, monkeypatch):
    # WS-8: with no WiGLE token set, the upload is refused with a helpful message and NO worker spawns.
    import src.config.settings as S
    monkeypatch.setattr(S, "load_settings", lambda: {"uploads": {"wigle_token": ""}})
    tab = _tab(monkeypatch)
    tab._on_upload_wigle()
    assert tab._upload_worker is None
    assert "token" in tab._log.toPlainText().lower()


def test_wigle_upload_gate_needs_a_saved_csv(qapp, monkeypatch, tmp_path):
    # Token set, but the CSV path doesn't exist yet -> refused, no worker.
    import src.config.settings as S
    monkeypatch.setattr(S, "load_settings", lambda: {"uploads": {"wigle_token": "TOK"}})
    tab = _tab(monkeypatch)
    tab._out_edit.setText(str(tmp_path / "does-not-exist.csv"))
    tab._on_upload_wigle()
    assert tab._upload_worker is None
    assert "no saved csv" in tab._log.toPlainText().lower()
