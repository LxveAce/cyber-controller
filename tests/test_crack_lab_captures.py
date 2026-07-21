"""Crack Lab Captures panel (punch-list #2, slice 3): the auto-populating, exportable capture list.

Offscreen Qt. Verifies the CrackLabTab, given a hub with a shared CaptureStore, (a) paints a row for
a capture present at construction, (b) paints a row for a capture that arrives live over the bus,
(c) loads a double-clicked capture's file into the cracker, (d) writes a solved crack back onto the
record (capture.cracked), (e) exports the log to CSV, and (f) degrades safely with no hub.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from src.core.capture_store import CaptureStore  # noqa: E402
from src.core.cross_comm import EventBus  # noqa: E402
from src.models.capture import CaptureRecord  # noqa: E402
from src.ui.qt.crack_lab_tab import CrackLabTab  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _hub_with_store():
    """A minimal stand-in for CrossCommHub: only the shared capture store the tab reads."""
    return SimpleNamespace(captures=CaptureStore(EventBus()))


def _rec(bssid="AA:BB:CC:DD:EE:FF", **kw):
    return CaptureRecord(bssid=bssid, capture_type="eapol", ssid="HomeNet", channel=6,
                         device_source="COM7", pcap_path="/sd/hs_01.pcapng", **kw)


def test_capture_present_at_construction_paints_a_row(qapp):
    hub = _hub_with_store()
    hub.captures.add(_rec())
    tab = CrackLabTab(hub)
    assert tab._captures_table.rowCount() == 1
    # The SSID column shows the joined network name.
    from src.core.capture_export import CAPTURE_CSV_COLUMNS
    col = CAPTURE_CSV_COLUMNS.index("ssid")
    assert tab._captures_table.item(0, col).text() == "HomeNet"


def test_live_capture_over_bus_appears(qapp):
    hub = _hub_with_store()
    tab = CrackLabTab(hub)
    assert tab._captures_table.rowCount() == 0
    hub.captures.add(_rec(bssid="11:22:33:44:55:66"))
    qapp.processEvents()                       # flush the queued bridge signal to the GUI thread
    assert tab._captures_table.rowCount() == 1


def test_double_click_loads_file_and_sets_active_key(qapp):
    hub = _hub_with_store()
    rec = _rec()
    hub.captures.add(rec)
    tab = CrackLabTab(hub)
    tab._on_capture_activated(0, 0)
    assert tab._capture_edit.text() == "/sd/hs_01.pcapng"
    assert tab._active_capture_key == rec.key
    assert tab._bssid_edit.text() == "AA:BB:CC:DD:EE:FF"


def test_crack_result_writes_back_onto_capture(qapp):
    hub = _hub_with_store()
    rec = _rec()
    hub.captures.add(rec)
    tab = CrackLabTab(hub)
    tab._active_capture_key = rec.key
    result = SimpleNamespace(cracked=True, password="hunter2", ssid="HomeNet",
                             bssid="AA:BB:CC:DD:EE:FF", detail="found in rockyou")
    tab._on_done(result)
    stored = hub.captures.get(rec.key)
    assert stored.crack_status == "cracked" and stored.password == "hunter2"


def test_export_writes_csv(qapp, monkeypatch, tmp_path):
    hub = _hub_with_store()
    hub.captures.add(_rec())
    tab = CrackLabTab(hub)
    out = tmp_path / "caps.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), "CSV"))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: 0)
    tab._on_export_captures()
    assert out.exists()
    body = out.read_text(encoding="utf-8")
    assert "capture_type" in body.splitlines()[0] and "AA:BB:CC:DD:EE:FF" in body


def test_export_default_dir_is_the_captures_dir(qapp, monkeypatch, tmp_path):
    # WS-7: the export save-dialog defaults into the canonical captures dir, so a just-captured file and
    # its exported log share one predictable place.
    import src.core.install as install
    monkeypatch.setattr(install, "_CONFIG_DIR", tmp_path / ".cyber-controller")
    monkeypatch.delenv("CC_CAPTURES_DIR", raising=False)
    hub = _hub_with_store()
    hub.captures.add(_rec())
    tab = CrackLabTab(hub)
    captured = {}

    def _fake_save(*a, **k):
        captured["default"] = a[2]          # (self, caption, default_path, filter)
        return ("", "")                     # user cancels -> no write

    monkeypatch.setattr(QFileDialog, "getSaveFileName", _fake_save)
    tab._on_export_captures()
    assert captured["default"].startswith(str(tmp_path / ".cyber-controller" / "captures"))


def test_export_writes_json(qapp, monkeypatch, tmp_path):
    # changelog/README promise CSV *or* JSON; a .json path must route to the JSON writer, not CSV.
    # Regression: the UI wired only "Export CSV…", so JSON export was entirely unreachable.
    hub = _hub_with_store()
    hub.captures.add(_rec())
    tab = CrackLabTab(hub)
    out = tmp_path / "caps.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), "JSON (*.json)"))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: 0)
    tab._on_export_captures()
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) == 1
    assert "AA:BB:CC:DD:EE:FF" in out.read_text(encoding="utf-8")


def test_export_appends_extension_from_chosen_filter(qapp, monkeypatch, tmp_path):
    # "All files" / no typed extension: the chosen filter decides the format AND the extension is
    # appended, so CSV and JSON exports never collide as one ambiguous extensionless file.
    hub = _hub_with_store()
    hub.captures.add(_rec())
    tab = CrackLabTab(hub)
    stem = tmp_path / "caps_noext"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(stem), "JSON"))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: 0)
    tab._on_export_captures()
    written = tmp_path / "caps_noext.json"
    assert written.exists(), "no-extension + JSON filter must append .json"
    assert isinstance(json.loads(written.read_text(encoding="utf-8")), list)


def test_hub_none_degrades_without_crash(qapp, monkeypatch):
    tab = CrackLabTab()                         # no hub -> manual-only
    assert tab._captures is None
    assert tab._captures_table.rowCount() == 0
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: 0)
    tab._on_export_captures()                   # must not raise
    tab._on_capture_activated(0, 0)             # must not raise


def test_browse_clears_writeback_binding_so_wrong_record_is_not_marked(qapp, monkeypatch):
    # Red-team fix: double-click A binds the write-back to A; Browsing to an unrelated file B must
    # DROP that binding, so cracking B does not write B's password onto record A (a false confirm).
    hub = _hub_with_store()
    rec = _rec()
    hub.captures.add(rec)
    tab = CrackLabTab(hub)
    tab._on_capture_activated(0, 0)
    assert tab._active_capture_key == rec.key
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        lambda *a, **k: ("/some/other.pcapng", "Captures"))
    tab._pick_capture()
    assert tab._active_capture_key == ""            # browsing dropped the binding
    tab._on_done(SimpleNamespace(cracked=True, password="wrong", ssid="", bssid="", detail=""))
    assert hub.captures.get(rec.key).crack_status == "uncracked"   # A untouched


def test_writeback_binding_cleared_after_a_successful_writeback(qapp):
    # One write-back per load: after a solved crack writes onto A, the binding clears so a later
    # unrelated run can't re-mark A.
    hub = _hub_with_store()
    rec = _rec()
    hub.captures.add(rec)
    tab = CrackLabTab(hub)
    tab._active_capture_key = rec.key
    tab._on_done(SimpleNamespace(cracked=True, password="hunter2", ssid="", bssid="", detail=""))
    assert hub.captures.get(rec.key).crack_status == "cracked"
    assert tab._active_capture_key == ""


def test_capture_confirm_label_is_not_hardcoded_deauth():
    # Red-team fix: the activity-log notice must name the actual arming action — only a Deauth AP
    # reads as "deauth"; a Capture Handshake / Evil Portal action must not claim a deauth fired.
    from src.ui.qt.main_window import CyberControllerWindow as W
    assert W._capture_trigger({"action": "Deauth AP"}) == "deauth"
    assert W._capture_trigger({"action": "Capture Handshake"}) == "Capture Handshake"
    assert W._capture_trigger({"action": "Evil Portal"}) == "Evil Portal"
    assert W._capture_trigger({}) == "action"
