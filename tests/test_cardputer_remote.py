"""CP2 Cardputer Remote — a Cardputer-shaped DeviceView + a raw CLI console, two lanes over ONE guarded send.

Verifies: the view is shaped to the Cardputer (240x135, CP1); BOTH lanes send through the identical wrapper
(no second/unguarded path); the raw lane reports honestly (sent vs preview) and never crashes; control chars
are passed verbatim to the SAME guard (not silently sanitized here); and the needs_arg guard (DV4) still
blocks a bare arg-requiring command through the composite. Runs offscreen.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt.cardputer_remote import CardputerRemote  # noqa: E402
from src.ui.qt.device_view import DeviceScreenModel, MenuNode  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_view_is_cardputer_shaped(qapp):
    r = CardputerRemote("marauder", send=lambda c: True)
    assert (r.view._native.width(), r.view._native.height()) == (240, 135)   # CP1 cardputer native size


def test_both_lanes_share_one_guarded_send(qapp):
    r = CardputerRemote("marauder", send=lambda c: True)
    assert r.view._send == r._dispatch          # the skin lane dispatches through the SAME wrapper as raw
    assert getattr(r.view._send, "__func__", None) is CardputerRemote._dispatch   # literally _dispatch, bound to r
    assert getattr(r.view._send, "__self__", None) is r   # the view has NO independent send of its own


def test_raw_lane_sends_through_guarded_send(qapp):
    sent = []
    r = CardputerRemote("marauder", send=lambda c: sent.append(c) or True)
    r._input.setText("scanap")
    r._submit_raw()
    assert sent == ["scanap"]
    assert r._input.text() == ""                 # input cleared after send
    assert "» sent: scanap" in r._console.toPlainText()


def test_raw_empty_line_sends_nothing(qapp):
    sent = []
    r = CardputerRemote("marauder", send=lambda c: sent.append(c) or True)
    r._input.setText("   ")
    r._submit_raw()
    assert sent == []
    assert r._console.toPlainText() == ""


def test_not_delivered_is_preview(qapp):
    r = CardputerRemote("marauder", send=lambda c: False)   # no device / firmware mismatch -> not written
    r._input.setText("scanap")
    r._submit_raw()
    assert r._console.toPlainText().startswith("preview: scanap")


def test_send_none_is_preview(qapp):
    r = CardputerRemote("marauder", send=None)
    assert r._dispatch("reboot") is False
    assert r._console.toPlainText().startswith("preview: reboot")


def test_send_raising_is_caught_as_preview(qapp):
    def boom(c):
        raise RuntimeError("serial gone")
    r = CardputerRemote("marauder", send=boom)
    r._input.setText("scanap")
    r._submit_raw()                              # must not propagate the exception
    assert r._console.toPlainText().startswith("preview: scanap")


def test_control_chars_passed_verbatim_to_the_guard(qapp):
    sent = []
    r = CardputerRemote("marauder", send=lambda c: sent.append(c) or False)
    r._input.setText("scan\x00ap")
    r._submit_raw()
    assert sent == ["scan\x00ap"]                # NOT sanitized here — the SAME conn.write guard rejects it
    assert r._console.toPlainText().startswith("preview: scan")   # send returned False -> honest preview


def test_skin_nav_lane_dispatches_real_command_to_shared_transcript(qapp):
    sent = []
    r = CardputerRemote("marauder", send=lambda c: sent.append(c) or True)
    m = r.view.model
    m.enter()                    # into the WiFi submenu
    m.sel = 0
    m.enter(r.view._send)        # activate the leaf -> real command through the shared dispatch
    assert len(sent) == 1
    assert ("» sent: " + sent[0]) in r._console.toPlainText()   # unified transcript (skin + raw)


def test_needs_arg_leaf_does_not_fire_through_composite(qapp):
    sent = []
    r = CardputerRemote("bruce", send=lambda c: sent.append(c) or True)
    # a leaf whose real command REQUIRES an argument must never fire a bare line, even via the composite.
    model = DeviceScreenModel(
        "Bruce", [MenuNode("Run Ducky…", command="badusb run_from_file <script>", needs_arg=True)], skin="bruce")
    model.sel = 0
    model.enter(r._dispatch)     # drive the needs_arg leaf through the SAME dispatch the widget uses
    assert sent == []                              # needs_arg guard held -> nothing sent
    assert model.status.startswith("needs arg:")
    assert r._console.toPlainText() == ""          # and nothing logged


def test_unknown_firmware_falls_back_to_marauder(qapp):
    r = CardputerRemote("nonexistent", send=lambda c: True)
    assert (r.view._native.width(), r.view._native.height()) == (240, 135)   # still cardputer-shaped, no crash


def test_dispatch_true_on_delivery(qapp):
    r = CardputerRemote("marauder", send=lambda c: True)
    assert r._dispatch("reboot") is True                # honest True when the send reports delivered
    assert r._console.toPlainText().startswith("» sent: reboot")


def test_footer_status_and_console_agree(qapp):
    """The DeviceView's own footer status and the console transcript are driven by the SAME send bool."""
    r = CardputerRemote("marauder", send=lambda c: True)
    m = r.view.model
    m.enter(); m.sel = 0
    m.enter(r.view._send)                               # activate a leaf
    cmd = m.status.split("» sent: ", 1)[1] if "» sent: " in m.status else None
    assert cmd is not None                              # footer says sent
    assert ("» sent: " + cmd) in r._console.toPlainText()   # console shows the same


def test_no_direct_device_conduit_in_source(qapp):
    """Structural lock: the widget reaches the device ONLY via the injected send. No CODE identifier may name
    a serial/connection/write API (docstrings/strings are ignored — we parse the AST, not grep text)."""
    import ast
    from pathlib import Path

    import src.ui.qt.cardputer_remote as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.ImportFrom):
            used.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            used.update(a.name for a in node.names)
    # These identifiers would each mean a direct, un-guarded path to the hardware.
    forbidden = {"write", "SerialConnection", "_active_conn", "device_manager", "get_connection", "_dm"}
    leaked = forbidden & used
    assert not leaked, f"widget must not touch the device directly (only the injected send): {leaked}"


def test_long_input_transcript_is_bounded(qapp):
    r = CardputerRemote("marauder", send=lambda c: True)
    for i in range(600):                                # more than the 500-block cap
        r._input.setText(f"cmd{i}")
        r._submit_raw()
    assert r._console.blockCount() <= 500               # bounded — a long session can't grow unbounded
