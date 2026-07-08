"""The Secure Container UI must not claim encryption it doesn't provide.

Regression for audit finding [1] (HIGH — false security in a security tool): the Secure Container checkbox +
description advertised at-rest encryption for "logs, sessions, captures", but secure_store.save has exactly one
caller (macro_recorder.py, category "macros") — logs are in-memory/session-only, sessions don't persist, and
wardrive CSVs are plaintext by design. The copy must name what's actually protected (macros). Offscreen Qt.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    import src.config.settings as S
    monkeypatch.setattr(S, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(S, "SETTINGS_PATH", tmp_path / "settings.json")
    return S


@pytest.fixture(autouse=True)
def _no_blocking_sd_probe(monkeypatch):
    import src.core.backends.sd_backend as sd
    monkeypatch.setattr(sd, "detect_sd_cards", lambda *a, **k: [])


@pytest.fixture
def window(qapp, isolated_settings):
    from PyQt5.QtCore import QTimer

    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow

    bus = EventBus()
    win = CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))
    try:
        win._health.stop()
    except Exception:  # noqa: BLE001
        pass
    for t in win.findChildren(QTimer):
        t.stop()
    yield win
    try:
        win.close()
    except Exception:  # noqa: BLE001
        pass
    win.deleteLater()
    qapp.processEvents()


def test_secure_container_checkbox_names_macros_not_logs(window):
    text = window._settings_tab._secure_container_check.text().lower()
    # Names what's actually protected...
    assert "macro" in text
    # ...and no longer advertises encryption of logs/sessions/captures (which it never provided).
    for false_claim in ("log", "session", "capture"):
        assert false_claim not in text, f"checkbox still claims to protect {false_claim!r}: {text!r}"
