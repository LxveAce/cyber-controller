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


_SURFACES = {"flash", "connect", "operate", "operate-home", "survey", "analyze", "settings"}


def test_app_shell_is_the_splitter_top_widget(win):
    assert isinstance(win._app_shell, PageLayout)
    assert win._main_splitter.widget(0) is win._app_shell   # shell is the top slot
    assert win._main_splitter.count() == 2                  # shell + terminal (unchanged shape)


def test_tabs_live_inside_the_shell_content(win):
    # the whole top area (sidebar + tabs) is the shell's content; _tabs is unchanged + reachable
    assert win._app_shell._content is not None
    assert win._tabs.count() == 7                            # all 7 top-level tabs still present


def test_shell_sidebar_has_a_destination_per_surface(win):
    dests = set(win._app_shell._destinations)
    assert _SURFACES <= dests, f"missing shell nav destinations: {_SURFACES - dests}"


def test_shell_nav_selects_the_surface_in_the_tabs(win):
    # dual nav: selecting a shell destination drives _tabs.setCurrentWidget to that surface
    win._app_shell.select_destination("connect")
    assert win._tabs.currentWidget() is win._connect_surface
    win._app_shell.select_destination("analyze")
    assert win._tabs.currentWidget() is win._network_surface
    win._app_shell.select_destination("operate-home")
    assert win._tabs.currentWidget() is win._operate_home


def test_tab_bar_is_hidden_sidebar_is_sole_nav(win):
    # Slice C: the flat tab strip is hidden — the app-shell sidebar is now the sole visible nav.
    assert win._tabs.tabBar().isHidden()
    # every surface is still reachable via the sidebar (setCurrentWidget drives the hidden tabs).
    win._app_shell.select_destination("survey")
    assert win._tabs.currentWidget() is win._survey_surface
    win._app_shell.select_destination("flash")
    assert win._tabs.currentWidget() is win._flash_surface


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
    win._tabs.setCurrentWidget(win._connect_surface)
    assert win._app_shell._destinations["connect"].isChecked()
    win._tabs.setCurrentWidget(win._settings_tab)
    assert win._app_shell._destinations["settings"].isChecked()
    assert not win._app_shell._destinations["connect"].isChecked()   # only one active


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
