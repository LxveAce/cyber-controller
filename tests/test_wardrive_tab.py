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


def _select_provider(tab, key):
    idx = tab._upload_provider.findData(key)
    if idx >= 0:
        tab._upload_provider.setCurrentIndex(idx)


def test_upload_gate_needs_a_token(qapp, monkeypatch):
    # WS-8: with no token for the selected service, the upload is refused with a helpful message + NO worker.
    import src.config.settings as S
    monkeypatch.setattr(S, "load_settings", lambda: {"uploads": {"wigle_token": "", "wdgwars_token": ""}})
    tab = _tab(monkeypatch)
    _select_provider(tab, "wdgwars")
    tab._on_upload()
    assert tab._upload_worker is None
    assert "wdg wars" in tab._log.toPlainText().lower() and "token" in tab._log.toPlainText().lower()


def test_upload_gate_needs_a_saved_csv(qapp, monkeypatch, tmp_path):
    # Token set for the selected service, but the CSV path doesn't exist yet -> refused, no worker.
    import src.config.settings as S
    monkeypatch.setattr(S, "load_settings", lambda: {"uploads": {"wigle_token": "TOK", "wdgwars_token": ""}})
    tab = _tab(monkeypatch)
    _select_provider(tab, "wigle")
    tab._out_edit.setText(str(tmp_path / "does-not-exist.csv"))
    tab._on_upload()
    assert tab._upload_worker is None
    assert "no saved csv" in tab._log.toPlainText().lower()


def test_upload_uses_the_selected_providers_token(qapp, monkeypatch, tmp_path):
    # The gate reads the token for the SELECTED provider (wdgwars_token, not wigle_token).
    import src.config.settings as S
    monkeypatch.setattr(S, "load_settings",
                        lambda: {"uploads": {"wigle_token": "", "wdgwars_token": "KEY64"}})
    tab = _tab(monkeypatch)
    _select_provider(tab, "wdgwars")
    tab._out_edit.setText(str(tmp_path / "still-missing.csv"))   # stop before the network worker starts
    tab._on_upload()
    # WDG Wars token IS set, so it passed the token gate and fell through to the "no saved CSV" gate.
    assert tab._upload_worker is None
    assert "no saved csv" in tab._log.toPlainText().lower()


def test_shutdown_joins_the_upload_worker(qapp, monkeypatch):
    # Capstone fix: an in-flight WiGLE upload QThread must be joined on app teardown, or it's destroyed
    # mid-run ('QThread: Destroyed while thread is still running') and aborts on exit.
    tab = _tab(monkeypatch)

    class _FakeUpload:
        def __init__(self):
            self.waited = False

        def isRunning(self):
            return True

        def wait(self, ms=None):
            self.waited = True
            return True

    fake = _FakeUpload()
    tab._upload_worker = fake
    tab.shutdown()
    assert fake.waited is True, "shutdown() must wait() on a running upload worker"
