"""Loadout model (src/config/loadout.py) — pure tab/firmware visibility logic. No Qt/display."""

from __future__ import annotations

from src.config import loadout as L


def test_full_stack_shows_everything():
    lo = L.full_stack_loadout()
    assert L.is_full_stack(lo)
    assert L.visible_tabs(lo) == list(L.TAB_ORDER)
    assert L.firmware_visible("bluejammer", lo)


def test_unconfigured_fails_open():
    assert L.is_full_stack(None)
    assert L.is_full_stack({"configured": False})
    assert L.visible_tabs(None) == list(L.TAB_ORDER)


def test_empty_configured_fails_open():
    # configured but nothing selected -> still show everything (never strand the user with no tabs)
    lo = {"full_stack": False, "configured": True, "firmwares": [], "hardware": []}
    assert L.is_full_stack(lo)


def test_core_tabs_always_visible():
    # S4 regroup: Macros folded into the always-shown "Operate" surface, so the core always-visible top-level
    # tab is now "Operate" (it anchors Macros + the action sub-views) rather than "Macros".
    lo = {"full_stack": False, "configured": True, "firmwares": ["meshtastic"], "hardware": []}
    vis = L.visible_tabs(lo)
    for core in ("Flash", "Devices", "Health", "Operate", "Settings", "How-To"):
        assert core in vis


def test_wifi_scanning_gates_network_surface():
    # S4 regroup: the wifi_scanning-gated *top-level* surface is now "Network" (holds Cross-Comm). Targets and
    # Broadcast folded into the always-shown "Operate" surface (Macros anchors it), so wifi gating for those
    # became a documented per-sub-tab follow-up — none of Targets/Broadcast/Cross-Comm are top-level tabs.
    lo = {"full_stack": False, "configured": True, "firmwares": ["meshtastic"], "hardware": []}
    vis = L.visible_tabs(lo)
    assert "Network" not in vis            # no wifi fw -> the wifi-gated Network surface is hidden
    assert "Operate" in vis                # always shown (contains always-available Macros)
    for sub in ("Targets", "Broadcast", "Cross-Comm"):
        assert sub not in vis              # sub-views now, never top-level
    # add Marauder -> the wifi-gated Network surface appears (Operate was already shown)
    lo2 = {**lo, "firmwares": ["meshtastic", "marauder"]}
    vis2 = L.visible_tabs(lo2)
    assert "Network" in vis2 and "Operate" in vis2


def test_operate_surface_always_shown_holds_wardrive():
    # S4 regroup: Wardrive (gps-gated) is now a sub-view of the always-shown "Operate" surface, so gps no
    # longer gates a *top-level* tab. Per-sub-tab gps gating inside Operate is a tracked follow-up (loadout is
    # surface-granularity today). Operate itself is always present because Macros (ALWAYS) anchors it.
    no_gps = {"full_stack": False, "configured": True, "firmwares": ["marauder"], "hardware": ["esp32"]}
    assert "Operate" in L.visible_tabs(no_gps)
    assert "Wardrive" not in L.visible_tabs(no_gps)  # not a top-level tab — it's an Operate sub-view
    with_gps = {**no_gps, "hardware": ["esp32", "gps"]}
    assert "Operate" in L.visible_tabs(with_gps)


def test_usb_os_gates_software_tab():
    no_os = {"full_stack": False, "configured": True, "firmwares": ["marauder"], "hardware": ["esp32"]}
    assert "Software OS" not in L.visible_tabs(no_os)
    with_os = {**no_os, "hardware": ["esp32", "usb_os"]}
    assert "Software OS" in L.visible_tabs(with_os)


def test_firmware_filtering():
    lo = {"full_stack": False, "configured": True, "firmwares": ["marauder", "ghostesp"], "hardware": []}
    assert L.filter_firmwares(["marauder", "ghostesp", "bruce", "flipper"], lo) == ["marauder", "ghostesp"]
    assert not L.firmware_visible("flipper", lo)


def test_normalize_drops_junk():
    lo = L.normalize({"firmwares": ["marauder", "NOPE"], "hardware": ["gps", "bogus"], "full_stack": "x"})
    assert lo["firmwares"] == ["marauder"]
    assert lo["hardware"] == ["gps"]
    assert lo["full_stack"] is True  # bool("x")


def test_default_loadout_is_unconfigured():
    d = L.default_loadout()
    assert d["configured"] is False
    assert L.is_full_stack(d)  # until configured, show everything
