"""OPERATE HOME launcher (src/ui/qt/operate_home.py) — the domain tile grid as a pure launcher.

Spade D6c: OperateHome is no longer a browser. Every tile NAVIGATES to its real surface
(``navigate_requested``) or is a greyed P4 roadmap tile (gps/subghz); there is no in-place
``DomainDetailView`` browser. These assert that launcher behavior, offscreen.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from src.ui.qt.layout_profile import layout_profile, operate_home_layout  # noqa: E402
from src.ui.qt.operate_home import OperateHome, build_operate_home  # noqa: E402
from src.ui.qt.theme import colors as C  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_starts_on_the_domain_grid(qapp):
    h = OperateHome()
    assert h.current_domain() is None            # a launcher opens nothing in-place
    assert h._grid is not None                   # the grid is the content


def test_selecting_wifi_navigates_to_the_real_surface(qapp):
    h = OperateHome()
    seen: list[str] = []
    h.navigate_requested.connect(seen.append)
    h._grid.domain_selected.emit("wifi")         # a tile activation announces its key
    assert seen == ["wifi"]                       # navigates to the real analyzer
    assert h.current_domain() is None             # stays a launcher; the host does the navigation
    assert h.domain_view("wifi") is None          # never an embedded browser


def test_selecting_ble_navigates_to_the_real_surface(qapp):
    h = OperateHome()
    seen: list[str] = []
    h.navigate_requested.connect(seen.append)
    h._grid.domain_selected.emit("ble")
    assert seen == ["ble"]
    assert h.domain_view("ble") is None


def test_tools_and_settings_navigate(qapp):
    # Tools + Settings live in real tabs, so their tiles navigate — never a "coming soon"
    # placeholder that lies about a shipped feature.
    h = OperateHome()
    seen: list[str] = []
    h.navigate_requested.connect(seen.append)
    h._grid.domain_selected.emit("tools")
    h._grid.domain_selected.emit("settings")
    assert seen == ["tools", "settings"]
    assert h.domain_view("tools") is None and h.domain_view("settings") is None


def test_gps_and_subghz_are_greyed_roadmap_tiles(qapp):
    # Spade D6c (Atlas call): GPS + Sub-GHz land in MAP at P4, so they are greyed, non-activating
    # roadmap tiles — no browser, no navigate-to-nothing. Their cards are disabled (Qt blocks
    # the click) so they never emit navigate_requested, and they build no in-place screen.
    h = OperateHome()
    assert set(h._grid.roadmap_keys()) == {"gps", "subghz"}
    seen: list[str] = []
    h.navigate_requested.connect(seen.append)
    for key in ("gps", "subghz"):
        assert not h._grid._cards[key].isEnabled()     # greyed, non-activating tile
        assert h.domain_view(key) is None              # no in-place screen built
    # Even a forced show_domain on a roadmap key is a no-op — roadmap tiles never navigate.
    h.show_domain("gps")
    assert seen == []


def test_build_operate_home_returns_a_single_operate_home(qapp):
    home = build_operate_home()
    assert isinstance(home, OperateHome)


def test_build_operate_home_navigates_wifi_ble(qapp):
    # No duplicate analyzers: wifi/ble build no in-place view — tapping navigates to the ONE real
    # analyzer, not a transmit-nothing clone double-fed from the event taps.
    home = build_operate_home()
    assert home.domain_view("wifi") is None and home.domain_view("ble") is None
    seen: list[str] = []
    home.navigate_requested.connect(seen.append)
    home._grid.domain_selected.emit("wifi")
    assert seen == ["wifi"]
    assert home.current_domain() is None


# ── WS3: the three-zone execution surface (status pill + one-tap strip + demoted grid) ──

def test_connection_pill_reflects_connection_state(qapp):
    # The pill is the survivor chip: grey disconnected, green connected, amber arming — all from
    # the same set_summary inputs (no new data source).
    h = OperateHome()
    h.set_summary(0, 0, 0, "")
    assert C.TEXT_MUTED in h._summary._pill.styleSheet()      # disconnected -> grey
    h.set_summary(1, 0, 0, "safe")
    assert C.SUCCESS in h._summary._pill.styleSheet()         # connected -> green
    h.set_summary(1, 0, 0, "arming")
    assert C.WARNING in h._summary._pill.styleSheet()         # arming -> amber


def test_set_actions_builds_the_one_tap_strip(qapp):
    from src.protocols.base import CommandInfo
    h = OperateHome()
    fired: list[str] = []
    h.set_actions([CommandInfo("scanall", "WiFi"), CommandInfo("channel", "WiFi", args="ch")],
                  run_fn=lambda ci: fired.append(ci.name), send=lambda *a, **k: None,
                  ready_fn=lambda ci: (lambda: (True, "")), safe_state_fn=lambda: None)
    assert [c.name for c, _ in h._strip._tiles] == ["scanall", "channel"]   # Zone B populated
    h._strip._tiles[0][1].click()
    assert fired == ["scanall"]                              # no-arg tap rides the host run_fn


def test_empty_catalog_shows_honest_hint_not_invented_tiles(qapp):
    h = OperateHome()
    h.set_actions([], run_fn=lambda ci: None, send=lambda *a, **k: None,
                  ready_fn=lambda ci: (lambda: (True, "")), safe_state_fn=lambda: None)
    assert h._strip._tiles == []                             # no invented tiles
    assert h._strip._stop_btn is not None                    # STOP still present
    assert h._strip._hint.text() != ""                      # honest "no one-tap actions" hint


def test_refresh_readiness_gates_a_disconnected_tile(qapp):
    from src.protocols.base import CommandInfo
    h = OperateHome()
    h.set_actions([CommandInfo("scanall", "WiFi")], run_fn=lambda ci: None,
                  send=lambda *a, **k: None,
                  ready_fn=lambda ci: (lambda: (False, "connect the device first")),
                  safe_state_fn=lambda: None)
    h.refresh_readiness()
    assert not h._strip._tiles[0][1].isEnabled()            # disabled-with-reason from ready_fn


def test_go_deeper_grid_still_navigates_after_the_rework(qapp):
    # The demote (Zone C under a "Go deeper" label) must not break the nav contract.
    h = OperateHome()
    assert h._deeper_lbl.text() == "Go deeper"
    seen: list[str] = []
    h.navigate_requested.connect(seen.append)
    h._grid.domain_selected.emit("wifi")
    assert seen == ["wifi"]


# ── WS3 step 8: the responsive collapse (chrome sheds on compact; pill + STOP never do) ──

def test_apply_home_layout_sheds_chrome_on_compact(qapp):
    from src.protocols.base import CommandInfo
    h = OperateHome()
    h.set_actions([CommandInfo("scanall", "WiFi")], lambda ci: None, lambda *a, **k: None,
                  lambda ci: (lambda: (True, "")), lambda: None)
    # expanded: metric chips + "Go deeper" label are shown
    h._apply_home_layout(operate_home_layout(layout_profile(1440, 900, touch=False, dpi=96)))
    assert not h._summary._metrics["devices"].isHidden()
    assert not h._deeper_lbl.isHidden()
    # compact: chips + label hide, but the connection pill + STOP never collapse (§5)
    h._apply_home_layout(operate_home_layout(layout_profile(400, 800, touch=False, dpi=96)))
    assert h._summary._metrics["devices"].isHidden()
    assert h._deeper_lbl.isHidden()
    assert not h._summary._pill.isHidden()          # pill is the survivor chip
    assert not h._strip._stop_btn.isHidden()        # STOP never collapses


def test_apply_home_layout_touch_hit_target(qapp):
    from src.protocols.base import CommandInfo
    h = OperateHome()
    h.set_actions([CommandInfo("scanall", "WiFi")], lambda ci: None, lambda *a, **k: None,
                  lambda ci: (lambda: (True, "")), lambda: None)
    h._apply_home_layout(operate_home_layout(layout_profile(1440, 900, touch=True, dpi=96)))
    assert h._strip._tiles[0][1].minimumHeight() == 44   # touch -> generous target
    assert h._strip._stop_btn.minimumHeight() == 44


def test_relayout_home_debounces_on_size_class(qapp):
    h = OperateHome()
    h.resize(400, 800)
    qapp.processEvents()
    h._relayout_home()
    first = h._last_home_size
    h._relayout_home()          # same size class -> no re-apply, same recorded class
    assert h._last_home_size == first and first is not None
