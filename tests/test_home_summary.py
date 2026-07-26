"""Wave-10 Phase C slice E: the Operate Home landing gains a state-line + metric strip.

Enrich-the-landing (not a new surface): a header ABOVE the domain grid shows a one-line session
state-line + a compact metric strip (devices / targets / captures / armed), sourced from the SAME
hub the app-shell binder reads — it invents nothing. The header shows only on the grid landing and
hides inside a domain screen. These tests assert the widget (visibility toggle + metric text) and
the integration (main_window pushes real hub counts, live on target/capture bus events).

Harness mirrors tests/test_command_palette_nav.py (offscreen Qt, real core objects, quiesced).
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _no_blocking_sd_probe(monkeypatch):
    import src.core.backends.sd_backend as sd
    monkeypatch.setattr(sd, "detect_sd_cards", lambda *a, **k: [])


def _make_window():
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow
    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


def _quiesce(win) -> None:
    try:
        win._health.stop()
    except Exception:  # noqa: BLE001
        pass
    for timer in win.findChildren(QTimer):
        timer.stop()


@pytest.fixture
def win(qapp):
    w = _make_window()
    _quiesce(w)
    yield w
    try:
        w.close()
    except Exception:  # noqa: BLE001
        pass
    w.deleteLater()
    qapp.processEvents()


def _target():
    from src.models.target import Target, TargetType
    return Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, device_source="COM3")


def test_summary_shows_on_grid_and_hides_inside_a_domain(win):
    home = win._operate_home
    home.show_home()
    assert not home._summary.isHidden()          # the landing header belongs to the grid
    home.show_domain("wifi")
    assert home._summary.isHidden()              # gone inside a domain screen (full height)
    home.show_home()
    assert not home._summary.isHidden()          # back on the grid -> back on screen


def test_set_summary_updates_the_metric_strip(win):
    m = win._operate_home._summary._metrics
    win._operate_home.set_summary(2, 3, 1, "armed")
    assert m["devices"].text() == "2 devices"
    assert m["targets"].text() == "3 targets"
    assert m["captures"].text() == "1 capture"   # singular for 1
    assert m["armed"].text() == "ARMED"
    win._operate_home.set_summary(1, 0, 0, "")
    assert m["devices"].text() == "1 device"     # singular for 1
    assert m["targets"].text() == "0 targets"
    assert m["armed"].text() == ""               # not armed -> no chip


def test_refresh_reads_live_hub_counts(win):
    # main_window's refresh mirrors the binder's grounded reads: adding a target to the pool the hub
    # wraps is reflected after a refresh (proves it reads the SAME live pool, invents nothing).
    win._pool.add(_target())
    win._refresh_home_summary()
    assert win._operate_home._summary._metrics["targets"].text() == "1 target"


def test_target_bus_event_refreshes_the_summary(win):
    # The summary is wired to the target/capture bus events (like the shell badges): publishing one
    # refreshes it with no manual call, so a discovery updates the landing live.
    win._pool.add(_target())
    win._bus.publish("target.added", {})
    assert win._operate_home._summary._metrics["targets"].text() == "1 target"


def test_last_capture_shows_the_most_recent_from_the_store(win):
    # Grounded session value the status bar doesn't show: the strip surfaces the most recent capture
    # from the real capture store (ssid + type), not a fabricated value.
    from src.models.capture import CaptureRecord
    win._hub.captures.add(
        CaptureRecord(bssid="AA:BB:CC:DD:EE:FF", ssid="HomeNet", capture_type="eapol"))
    win._refresh_home_summary()
    text = win._operate_home._summary._last.text()
    assert "HomeNet" in text and "eapol" in text


def test_last_capture_is_optional_and_empty_by_default(win):
    # No last capture -> the label is empty (the strip degrades cleanly, invents nothing).
    m = win._operate_home._summary._last
    win._operate_home.set_summary(1, 0, 0, "")
    assert m.text() == ""
    win._operate_home.set_summary(1, 0, 1, "", "HomeNet (eapol)")
    assert "HomeNet (eapol)" in m.text()
