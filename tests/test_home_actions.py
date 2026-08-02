"""WS3 step 6/7 — Operate-Home's one-tap strip rebuilds on the RIGHT cadence, primes the console.

MainWindow drives Zone B on two cadences: readiness refreshes every poll (cheap), but the strip
only REBUILDS when the primary operate (port, firmware) changes — connect / disconnect /
firmware-change — so a steady-state poll never tears down an open OpPanel (critic finding 2). The
rebuild also primes the console's active device so Home's taps act on the connected port even if
Control was never opened (finding 1). These assert that cadence + priming + STOP mode, offscreen.

Harness mirrors tests/test_home_summary.py (offscreen Qt, real core objects, quiesced).
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


def _wire_fake_device(win, monkeypatch, dev):
    """Point Home's action refresh at *dev* on its port, and spy the console priming (so the test
    does not need the real device combo populated). Returns the list priming ports land in."""
    primed: list[str] = []
    monkeypatch.setattr(win, "_primary_operate_port", lambda: dev.port)
    monkeypatch.setattr(win._hub.dm, "get_device", lambda p: dev if p == dev.port else None)
    monkeypatch.setattr(win._operate_console, "select_device", lambda p: primed.append(p))
    win._home_actions_key = None   # force the first call to be a "change"
    return primed


def test_strip_rebuilds_only_on_firmware_change_not_on_the_poll(win, monkeypatch):
    from src.models.device import Device
    home = win._operate_home
    calls: list[int] = []
    orig = home.set_actions
    monkeypatch.setattr(
        home, "set_actions",
        lambda *a, **k: (calls.append(len(a[0]) if a else 0), orig(*a, **k))[1])
    dev = Device(port="COM7", firmware="marauder", connected=True)
    primed = _wire_fake_device(win, monkeypatch, dev)

    win._refresh_home_actions()
    assert len(calls) == 1               # connect -> rebuilt once
    assert home._strip._tiles            # marauder tiles present
    assert primed == ["COM7"]            # console primed on connect (finding 1)

    win._refresh_home_actions()          # a steady-state poll, nothing changed
    assert len(calls) == 1               # tuple-guard held -> NO rebuild (no OpPanel teardown)

    dev.firmware = "lxveos"              # a firmware change
    win._refresh_home_actions()
    assert len(calls) == 2               # rebuilt on firmware change
    assert primed == ["COM7", "COM7"]    # re-primed for the new firmware


def test_disconnect_yields_the_honest_empty_strip(win, monkeypatch):
    from src.models.device import Device
    home = win._operate_home
    dev = Device(port="COM7", firmware="marauder", connected=True)
    _wire_fake_device(win, monkeypatch, dev)
    win._refresh_home_actions()
    assert home._strip._tiles            # connected -> tiles

    # now no device: primary port empty, no rebuild-args -> honest empty (no invented tiles)
    monkeypatch.setattr(win, "_primary_operate_port", lambda: "")
    win._refresh_home_actions()
    assert home._strip._tiles == []      # cleared to the honest-empty strip
    assert home._strip._stop_btn is not None   # STOP still present (disabled chip)


def test_stop_mode_is_arm_for_lxveos_and_a_verb_for_marauder(win, monkeypatch):
    from src.models.device import Device
    home = win._operate_home

    dev = Device(port="COM7", firmware="lxveos", connected=True)
    _wire_fake_device(win, monkeypatch, dev)
    win._refresh_home_actions()
    assert home._strip._supports_arm is True     # arming fw -> STOP disarms via safe_state
    assert home._strip._stop_ci is None

    dev2 = Device(port="COM8", firmware="marauder", connected=True)
    _wire_fake_device(win, monkeypatch, dev2)
    win._refresh_home_actions()
    assert home._strip._supports_arm is False    # non-arming fw -> STOP maps to a stop verb
    assert home._strip._stop_ci is not None      # marauder has 'stopscan'
