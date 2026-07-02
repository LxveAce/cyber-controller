"""CC-7 — the connect-time probe wiring in DeviceTab.

DeviceManager.probe() + src/core/handshake already do the work (covered by test_handshake.py); this
verifies the *GUI wiring*: connecting a device runs the probe off-thread, populates Device.health /
Device.fw_banner, surfaces them in the Connect surface, and honestly marks a no-CLI (stream/controlmap)
node without writing to it. Reuses the synchronous fake connection from the handshake tests.
"""

import pytest

pytest.importorskip("PyQt5")  # GUI test — skipped on a headless box without PyQt5

from src.core.device_manager import DeviceManager
from src.models.device import Device
from test_handshake import _FakeConn, MARAUDER_HELP  # pythonpath includes "tests" (pyproject)


@pytest.fixture(scope="module")
def _qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _tab_with_live_fake(port: str, firmware: str, reply):
    """A DeviceTab whose DeviceManager already holds a live fake connection for *port*, with *port*
    selected — the post-open_connection state, so _probe_worker can be driven directly."""
    from src.ui.qt.device_tab import DeviceTab

    dm = DeviceManager()
    dev = Device(port=port, firmware=firmware, connected=True)
    dm.add_device(dev)
    conn = _FakeConn(reply)
    dm._connections[port] = conn  # inject a live conn (mirrors test_handshake's probe entry-point test)
    tab = DeviceTab(dm)
    tab._active_port = port
    return tab, dev, conn


def test_probe_worker_populates_health_and_banner(_qapp):
    tab, dev, _conn = _tab_with_live_fake("C_M", "marauder", MARAUDER_HELP)
    tab._probe_worker("C_M")  # synchronous body of the background probe
    assert dev.health == "alive"          # the firmware answered
    assert dev.fw_banner                  # an identifying line was captured


def test_probe_done_surfaces_health_in_the_label(_qapp):
    tab, _dev, _conn = _tab_with_live_fake("C_M", "marauder", MARAUDER_HELP)
    tab._probe_worker("C_M")
    tab._on_probe_done("C_M")
    assert "alive" in tab._health_label.text().lower()


def test_stream_node_marked_no_cli_without_writing(_qapp):
    # A controlmap/stream node has no text CLI — the probe must be honest ("no-cli") and send nothing.
    tab, dev, conn = _tab_with_live_fake("C_BJ", "bluejammer", ())
    tab._probe_worker("C_BJ")
    assert dev.health == "no-cli"
    assert conn.writes == []              # no probe command written to a non-CLI node


def test_disconnect_clears_stale_health(_qapp):
    tab, dev, _conn = _tab_with_live_fake("C_M", "marauder", MARAUDER_HELP)
    tab._probe_worker("C_M")
    tab._on_probe_done("C_M")
    assert tab._health_label.text()       # populated while connected
    dev.connected = False                 # link closed
    tab._update_health_label()
    assert tab._health_label.text() == ""  # stale result cleared


def test_probe_skipped_for_dms_gated_port(_qapp):
    # Safety: once a Dead-Man's-Switch auth gate has spoken on a port, the connect-time probe must NOT run
    # (an unsolicited "help" at an unlock prompt can burn an attempt / brick). _should_probe is the gate.
    tab, _dev, _conn = _tab_with_live_fake("C_DMS", "marauder", MARAUDER_HELP)
    assert tab._should_probe("C_DMS") is True     # normal connected device → probe ok
    tab._dms_seen.add("C_DMS")                     # a DMS gate line was seen on this port
    assert tab._should_probe("C_DMS") is False     # now the probe is suppressed


def test_should_probe_false_when_link_closed(_qapp):
    # The probe is deferred, so the link may have closed before it fires — don't probe a dead link.
    tab, dev, _conn = _tab_with_live_fake("C_M", "marauder", MARAUDER_HELP)
    dev.connected = False
    assert tab._should_probe("C_M") is False


def test_probe_done_only_updates_the_selected_port(_qapp):
    # A late probe result for a port the user is no longer viewing must not overwrite the current label.
    tab, _dev, _conn = _tab_with_live_fake("C_M", "marauder", MARAUDER_HELP)
    tab._probe_worker("C_M")              # C_M is alive
    tab._active_port = "C_OTHER"          # selection moved away before the result arrived
    tab._health_label.setText("")         # label now reflects the newly-selected device
    tab._on_probe_done("C_M")             # stale result for the previously-connected port
    assert tab._health_label.text() == ""  # not overwritten with C_M's health


def test_unprobed_connected_device_renders_blank_not_probing(_qapp):
    # A settled "unknown" (connected but no probe result) must be blank, not a stuck "probing…".
    tab, dev, _conn = _tab_with_live_fake("C_M", "marauder", MARAUDER_HELP)
    assert dev.health == "unknown"
    tab._update_health_label()
    assert tab._health_label.text() == ""
