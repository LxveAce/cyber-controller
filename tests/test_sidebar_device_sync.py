"""Sidebar → Devices-tab selection wiring.

Regression for the dead `device_selected` signal: selecting a device in the left sidebar list emits
``CyberControllerWindow.device_selected``, which had NO subscriber — so clicking a sidebar device drove
nothing. It is now connected to ``_focus_device_in_devices_tab``, which selects that device in the
Devices tab (making it the tab's active device). Offscreen Qt.
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
    """SoftwareTab.__init__ shells out to PowerShell for SD detection on Windows; stub it so building a
    window is instant and never blocks the harness (test isolation only, mirrors test_dual_depth_ui)."""
    import src.core.backends.sd_backend as sd
    monkeypatch.setattr(sd, "detect_sd_cards", lambda *a, **k: [])


def _quiesce(win) -> None:
    from PyQt5.QtCore import QTimer
    try:
        win._health.stop()
    except Exception:  # noqa: BLE001
        pass
    for timer in win.findChildren(QTimer):
        timer.stop()


@pytest.fixture
def make_window(qapp, isolated_settings):
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow

    created: list = []

    def _factory():
        bus = EventBus()
        win = CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))
        created.append(win)
        _quiesce(win)
        return win

    yield _factory

    for win in created:
        try:
            win.close()
        except Exception:  # noqa: BLE001
            pass
        win.deleteLater()
    qapp.processEvents()


def _select_in_sidebar(win, port: str) -> None:
    """Simulate a user clicking a device row in the sidebar: find the row for `port` and make it the
    current item (which fires currentItemChanged -> _on_sidebar_device_selected -> device_selected)."""
    from PyQt5.QtCore import Qt

    lst = win._sidebar_device_list
    for i in range(lst.count()):
        item = lst.item(i)
        if item is not None and item.data(Qt.UserRole) == port:
            lst.setCurrentItem(item)
            return
    raise AssertionError(f"no sidebar row for {port!r}; rows="
                         f"{[lst.item(i).data(Qt.UserRole) for i in range(lst.count())]}")


def test_sidebar_selection_sets_devices_tab_active_device(make_window):
    from src.models.device import Device

    win = make_window()
    win._dm.add_device(Device(port="COMA", name="Board A"))
    win._dm.add_device(Device(port="COMB", name="Board B"))
    win._refresh_sidebar_devices()

    # Nothing selected yet -> the Devices tab has no active device.
    assert win._device_tab._active_port == ""

    # Selecting COMB in the sidebar must drive the Devices tab to COMB (via the device_selected signal).
    _select_in_sidebar(win, "COMB")
    assert win._device_tab._active_port == "COMB"

    # And re-selecting a different device tracks it — proves the specific port is routed, not a one-off.
    _select_in_sidebar(win, "COMA")
    assert win._device_tab._active_port == "COMA"


def test_device_selected_signal_has_a_subscriber(make_window):
    """The signal itself must be wired. Emitting it directly (bypassing the list) still syncs the tab —
    guarding against a future refactor that keeps the signal but drops the connection."""
    from src.models.device import Device

    win = make_window()
    win._dm.add_device(Device(port="COM7", name="Solo"))
    win.device_selected.emit("COM7")
    assert win._device_tab._active_port == "COM7"


def test_periodic_sidebar_refresh_does_not_force_the_devices_tab(make_window):
    """The 3-second sidebar timer re-selects the connected device in the sidebar list. That PROGRAMMATIC
    setCurrentItem must NOT fire device_selected and force the main tabs back to Connect > Devices — the
    1.6.4 bug that trapped the user on the Devices tab (you could click another tab but got pulled back
    within ~3s). Regression: without the blockSignals guard in _refresh_sidebar_devices this fails.
    """
    from PyQt5.QtCore import Qt
    from src.models.device import Device

    win = make_window()
    win._dm.add_device(Device(port="COMB", name="Board B"))
    win._refresh_sidebar_devices()
    _select_in_sidebar(win, "COMB")  # user selects the device -> Devices tab tracks it
    assert win._device_tab._active_port == "COMB"

    # User navigates away to a different top-level tab (Settings is always present in the loadout).
    win._tabs.setCurrentWidget(win._settings_tab)
    assert win._tabs.currentWidget() is win._settings_tab

    # The periodic refresh fires (as the 3s timer would). It re-selects COMB in the sidebar internally.
    win._refresh_sidebar_devices()

    # The user must STILL be on Settings — not force-switched back to the Connect surface.
    assert win._tabs.currentWidget() is win._settings_tab, (
        "periodic sidebar refresh force-switched the main tabs back to Connect > Devices"
    )
    # The device is still highlighted in the sidebar (the fix suppresses the SIGNAL, not the selection).
    cur = win._sidebar_device_list.currentItem()
    assert cur is not None and cur.data(Qt.UserRole) == "COMB"
