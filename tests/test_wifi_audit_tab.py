"""Wi-Fi Audit tab (src/ui/qt/wifi_audit_tab.py) — smoke/integration test.

Drives the new UI surface headless (offscreen): it constructs + populates from the real engines,
and proves the per-run CONSENT gate is honoured (a declined affirmation starts no crack worker,
a missing capture is rejected). No real cracking tool is invoked.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402

import src.ui.qt.wifi_audit_tab as wat  # noqa: E402
from src.ui.qt.wifi_audit_tab import WifiAuditTab  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_tab_constructs_and_populates(qapp):
    tab = WifiAuditTab()
    # the wired controls exist
    assert tab._capture_edit is not None
    assert tab._wordlist_combo is not None
    assert tab._backend_combo is not None
    assert tab._run_btn is not None and tab._log is not None
    # populated from the real engines (detect_tools + scan_installed ran without raising)
    assert tab._tools_label.text()          # some tool-presence summary rendered
    assert tab._wordlist_combo.count() >= 1  # at least the "(no wordlists…)" placeholder
    assert tab._backend_combo.count() >= 1


def test_run_rejects_missing_capture(qapp, monkeypatch):
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a))
    monkeypatch.setattr(wat.cp, "available_backends", lambda _t: ["hashcat"])
    tab = WifiAuditTab()
    tab._backend_combo.clear()
    tab._backend_combo.addItem("hashcat")
    tab._capture_edit.setText("")            # no capture chosen
    tab._on_run()
    assert warned, "missing capture should warn"
    assert tab._worker is None, "no crack worker should start"


def test_consent_declined_starts_no_worker(qapp, monkeypatch, tmp_path):
    # valid-looking capture (.hc22000 with one WPA* hashline) + non-empty wordlist
    cap = tmp_path / "cap.hc22000"
    cap.write_text("WPA*01*deadbeef*aabbccddeeff*112233445566*7373696400*00*00\n", encoding="utf-8")
    wl = tmp_path / "words.txt"
    wl.write_text("password\nletmein\n", encoding="utf-8")

    monkeypatch.setattr(wat.cp, "available_backends", lambda _t: ["hashcat"])
    # decline the consent affirmation
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)

    tab = WifiAuditTab()
    tab._backend_combo.clear()
    tab._backend_combo.addItem("hashcat")
    tab._capture_edit.setText(str(cap))
    tab._wordlist_combo.clear()
    tab._wordlist_combo.addItem("words.txt", str(wl))
    tab._on_run()
    assert tab._worker is None, "declining consent must start no crack worker"
    assert tab._run_btn.isEnabled(), "run button stays enabled after a declined run"
