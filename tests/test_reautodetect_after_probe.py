"""Post-handshake re-detect: swap the cross-comm ingest parser to the firmware the probe found (1.7.0 #3b).

On Auto-detect, a never-probed board's ingest parser is chosen at connect (the provisional Marauder default)
BEFORE the 1500 ms handshake replies. Once the probe identifies the real firmware, _reautodetect_after_probe
must swap the ingest parser to it — so a GhostESP/Bruce/… board routes to its own parser without the user
having to pre-probe or pick manually. An explicit (non-Auto) firmware pick is always honoured, never overridden.
"""
import pytest

pytest.importorskip("PyQt5")

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.models.device import Device
from src.protocols import PROTOCOL_DISPLAY_NAMES, get_protocol
from test_handshake import _FakeConn, GHOST_HELP, MARAUDER_HELP  # pythonpath includes "tests"

_MARAUDER = get_protocol("marauder").protocol_name
_GHOST = get_protocol("ghost-esp").protocol_name


@pytest.fixture(scope="module")
def _qapp():
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _tab(port, reply, *, provisional="marauder"):
    """A pool-backed DeviceTab with a live fake conn for *port*, connected, on Auto-detect, and its ingest
    parser already on the provisional connect-time default (as if connect attached it before any probe)."""
    from src.ui.qt.device_tab import DeviceTab

    dm = DeviceManager()
    dev = Device(port=port, firmware=provisional, connected=True)
    dm.add_device(dev)
    conn = _FakeConn(reply)
    dm._connections[port] = conn
    tab = DeviceTab(dm, TargetPool(EventBus()), None)
    tab._active_port = port
    tab._ingest_proto[port] = get_protocol(provisional).protocol_name
    return tab, dev, conn


def test_reautodetect_swaps_parser_to_probed_firmware(_qapp):
    tab, dev, _conn = _tab("C_G", GHOST_HELP)
    dev.fw_banner = "GhostESP v1.0.0"           # what the probe captured
    assert tab._ingest_proto["C_G"] == _MARAUDER  # started on the Marauder default

    tab._reautodetect_after_probe("C_G")

    assert tab._ingest_proto["C_G"] == _GHOST    # ingest parser swapped to GhostESP
    assert dev.firmware == _GHOST                # Device.firmware kept in sync (resolver/palette/caps follow)


def test_explicit_firmware_pick_is_not_overridden(_qapp):
    tab, dev, _conn = _tab("C_G", GHOST_HELP)
    dev.fw_banner = "GhostESP v1.0.0"
    # User explicitly chose a firmware -> auto re-detect must stay out of it.
    tab._firmware_combo.setCurrentText(PROTOCOL_DISPLAY_NAMES["bruce"])
    assert tab._firmware_combo.currentText() != "Auto-detect"  # the pick actually took
    before = tab._ingest_proto["C_G"]

    tab._reautodetect_after_probe("C_G")

    assert tab._ingest_proto["C_G"] == before   # unchanged — explicit pick honoured


def test_no_swap_when_banner_matches_current_parser(_qapp):
    tab, dev, _conn = _tab("C_M", MARAUDER_HELP)
    dev.fw_banner = "ESP32 Marauder v1.12.3"
    tab._reautodetect_after_probe("C_M")
    assert tab._ingest_proto["C_M"] == _MARAUDER  # already correct → no-op


def test_no_swap_on_unrecognised_banner(_qapp):
    tab, dev, _conn = _tab("C_M", MARAUDER_HELP)
    dev.fw_banner = "boot ok, heap 200000"      # no firmware signature
    tab._reautodetect_after_probe("C_M")
    assert tab._ingest_proto["C_M"] == _MARAUDER  # keep the provisional default


def test_end_to_end_probe_then_swap(_qapp):
    # Drive the REAL probe: the handshake captures GhostESP's banner, then _on_probe_done swaps the parser.
    tab, dev, _conn = _tab("C_E", GHOST_HELP)
    tab._probe_worker("C_E")                     # populates dev.fw_banner from the live reply
    assert dev.fw_banner                         # a banner was captured
    tab._on_probe_done("C_E")
    assert tab._ingest_proto["C_E"] == _GHOST    # auto-routed to GhostESP end to end
