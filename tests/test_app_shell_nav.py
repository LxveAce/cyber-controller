"""Wave-10 Phase C slice A: the app-shell wraps the top area with a top-level nav sidebar.

Parity guardrail: wrapping the top area (device-sidebar + tabs) in the shell must lose nothing —
every top-level surface stays reachable via BOTH the tab-bar (unchanged) AND the new shell sidebar
(dual nav this slice). Asserts the shell is the splitter's top widget, the tabs live inside it, the
sidebar has a destination per surface, and selecting one drives the tab widget.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtCore import QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt.page_layout import PageLayout  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_window():
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine
    from src.ui.qt.main_window import CyberControllerWindow
    bus = EventBus()
    return CyberControllerWindow(DeviceManager(), FlashEngine(), bus, TargetPool(bus))


def _quiesce(win):
    try:
        win._health.stop()
    except Exception:  # noqa: BLE001
        pass
    for t in win.findChildren(QTimer):
        t.stop()


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


_SURFACES = {"device", "hunt", "operate", "crack", "map", "settings"}


def test_app_shell_is_the_splitter_top_widget(win):
    assert isinstance(win._app_shell, PageLayout)
    assert win._main_splitter.widget(0) is win._app_shell   # shell is the top slot
    assert win._main_splitter.count() == 2                  # shell + terminal (unchanged shape)


def test_tabs_live_inside_the_shell_content(win):
    # the whole top area (sidebar + tabs) is the shell's content; _tabs is unchanged + reachable
    assert win._app_shell._content is not None
    assert win._tabs.count() == 6                            # the 5 verb surfaces + pinned Settings


def test_shell_sidebar_has_a_destination_per_surface(win):
    dests = set(win._app_shell._destinations)
    assert _SURFACES <= dests, f"missing shell nav destinations: {_SURFACES - dests}"


def test_shell_nav_selects_the_surface_in_the_tabs(win):
    # selecting a shell destination drives _tabs.setCurrentWidget to that verb surface
    win._app_shell.select_destination("device")
    assert win._tabs.currentWidget() is win._rig_surface
    win._app_shell.select_destination("hunt")
    assert win._tabs.currentWidget() is win._hunt_surface
    win._app_shell.select_destination("crack")
    assert win._tabs.currentWidget() is win._crack_surface


def test_tab_bar_is_hidden_sidebar_is_sole_nav(win):
    # Slice C: the flat tab strip is hidden — the app-shell sidebar is now the sole visible nav.
    assert win._tabs.tabBar().isHidden()
    # every surface is still reachable via the sidebar (setCurrentWidget drives the hidden tabs).
    win._app_shell.select_destination("map")
    assert win._tabs.currentWidget() is win._map_surface
    win._app_shell.select_destination("device")
    assert win._tabs.currentWidget() is win._rig_surface


def test_tab_bar_stays_hidden_after_a_loadout_change(win):
    # apply_loadout removes+re-adds tabs; a manual tabBar().hide() must survive that (Qt keeps it
    # since tabBarAutoHide is off) — else a mode/loadout switch would flash the strip back.
    from src.config.loadout import default_loadout
    win.apply_loadout(default_loadout(), persist=False)
    assert win._tabs.tabBar().isHidden()


def test_detach_stays_reachable_with_the_bar_hidden(win):
    # The bar's double-click/context-menu detach is gone with the bar — detach must stay reachable
    # via the command palette + the Ctrl+Shift+D shortcut + the API.
    labels = [c.label for c in win._palette._commands]
    assert "Detach Current Tab" in labels          # discoverable in the palette
    assert callable(win._tabs.detach_current)       # the API the palette + shortcut call


def test_shell_sidebar_mirrors_the_visible_tab_set(win):
    # After a loadout change, each shell destination is visible IFF its surface is in the tabs,
    # so the sidebar never lists a mode-hidden tool. Robust to whatever the loadout hides.
    from src.config.loadout import default_loadout
    win.apply_loadout(default_loadout(), persist=False)
    for key, surface in win._shell_surfaces.items():
        present = win._tabs.indexOf(surface) >= 0
        dest = win._app_shell._destinations[key]
        assert (not dest.isHidden()) == present, f"{key} sidebar visibility != tab presence"


def test_shell_sidebar_highlights_the_current_tab(win):
    # Switching via the tab-bar updates the sidebar highlight (currentChanged -> _sync_shell_nav).
    win._tabs.setCurrentWidget(win._rig_surface)
    assert win._app_shell._destinations["device"].isChecked()
    win._tabs.setCurrentWidget(win._settings_tab)
    assert win._app_shell._destinations["settings"].isChecked()
    assert not win._app_shell._destinations["device"].isChecked()   # only one active


def test_device_sidebar_folded_into_the_app_shell(win):
    # Slice B: the device-sidebar is now a child of the ONE app-shell (not a second column beside
    # second column), so the top area is a single sidebar. The device list itself is unchanged.
    from PyQt5.QtWidgets import QFrame
    assert isinstance(win._device_sidebar, QFrame)
    # it lives inside the app-shell (its ancestor chain includes the shell), not the bare top_widget
    anc, in_shell = win._device_sidebar.parent(), False
    while anc is not None:
        if anc is win._app_shell:
            in_shell = True
            break
        anc = anc.parent()
    assert in_shell, "device-sidebar is not inside the app-shell"
    # the device list still exists + is reachable (behavior unchanged)
    assert win._sidebar_device_list is not None


def test_binder_refresh_devices_repushes_device_status(win):
    # The binder's device-truth push (link count + ARMED) must be re-runnable: refresh_devices()
    # re-invokes it, so the shell chrome can be brought up to date after the device set changes.
    pushed = []
    win._app_shell_binder._push_device_status = lambda: pushed.append(1)
    win._app_shell_binder.refresh_devices()
    assert pushed, "refresh_devices() did not re-push device status"


def test_device_refresh_repushes_the_app_shell_status(win):
    # Fix: the app-shell device status was only pushed once at construction (bus badges are live,
    # but device status wasn't). _refresh_sidebar_devices() — the device-change refresh point — must
    # re-push it, so the always-visible count + ARMED don't go stale after a connect/disconnect.
    calls = []
    win._app_shell_binder.refresh_devices = lambda: calls.append(1)
    win._refresh_sidebar_devices()
    assert calls, "_refresh_sidebar_devices did not re-push the app-shell device status"


def test_armed_state_reaches_the_app_shell_end_to_end(win):
    # End-to-end (Atlas's ask: connect/disconnect/arm updates it): a connected + ARMED device, then
    # the device refresh -> the always-visible ARMED indicator reflects it (was stuck at the
    # construction-time state before the fix). Disconnecting it clears the indicator.
    from src.models.device import Device
    dev = Device(port="COM15", firmware="marauder", connected=True)
    dev.arm_state = "armed"
    win._dm.add_device(dev)
    win._refresh_sidebar_devices()
    assert win._app_shell._status["armed"].text() == "ARMED"   # arm reaches the shell now
    dev.connected = False
    win._refresh_sidebar_devices()
    assert win._app_shell._status["armed"].text() == ""        # cleared on disconnect


def test_rail_is_driven_by_nav_model(win):
    # P2.5 regression guard (the "missing consumer"): the top-level rail is built FROM
    # nav_model.visible_nav() — NOT a hardcoded noun list. The visible labels must equal the nav_model verb
    # surfaces + the pinned Settings, in that order, and the capability-gated Sense surface (no provider yet)
    # must be ABSENT from both the rail and the sidebar. Wire-it-or-it-doesn't-appear is structural.
    import src.core.nav_model as nav
    titles = [win._tabs.tabText(i) for i in range(win._tabs.count())]
    expected = [n.label for n in nav.visible_nav(win._nav_capabilities())] + [nav.settings_node().label]
    assert titles == expected
    assert "SENSE" not in titles
    assert "sense" not in win._app_shell._destinations   # reserved surface stays out until a provider lands
