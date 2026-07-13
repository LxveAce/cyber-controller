"""FlashEngine.active_ports() — an honest per-port view of in-flight ops.

The scalar ``status`` is a single shared field, but different ports flash in parallel (multi-board), so
a finished op sets it to DONE even while another port is still writing — the web /api/health could then
report "done" mid-flash. ``active_ports()`` reads the per-port ``_busy_ports`` reservation set so a
poller can tell the truth. Pure logic, no hardware."""

from __future__ import annotations

from src.core.flash_engine import FlashEngine, FlashStatus


def test_active_ports_reflects_reserved_ports():
    eng = FlashEngine()
    assert eng.active_ports() == []
    with eng._port_guard("COM3"):
        assert eng.active_ports() == ["COM3"]
        with eng._port_guard("COM7"):
            assert eng.active_ports() == ["COM3", "COM7"]   # sorted snapshot
        assert eng.active_ports() == ["COM3"]               # COM7 released
    assert eng.active_ports() == []                         # both released


def test_active_ports_is_independent_of_the_shared_status():
    # The single shared status can read DONE while another port is still busy; active_ports stays truthful
    # so a web /api/health poller won't re-enable controls mid-flash on another port.
    eng = FlashEngine()
    with eng._port_guard("COM3"):
        eng._status = FlashStatus.DONE          # a DIFFERENT port's op just finished
        assert eng.active_ports() == ["COM3"]   # COM3 is still busy -> honest


def test_falsy_port_is_not_reserved():
    # SD/UF2/DFU paths pass a blank port; those aren't serial reservations and must not appear as active.
    eng = FlashEngine()
    with eng._port_guard(""):
        assert eng.active_ports() == []
