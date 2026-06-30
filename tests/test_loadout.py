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
    lo = {"full_stack": False, "configured": True, "firmwares": ["meshtastic"], "hardware": []}
    vis = L.visible_tabs(lo)
    for core in ("Flash", "Devices", "Health", "Macros", "Settings", "How-To"):
        assert core in vis


def test_wifi_scanning_gates_targets_broadcast():
    # Meshtastic is not a wifi-scanning fw -> no Targets/Broadcast/Cross-Comm
    lo = {"full_stack": False, "configured": True, "firmwares": ["meshtastic"], "hardware": []}
    vis = L.visible_tabs(lo)
    assert "Targets" not in vis and "Broadcast" not in vis and "Cross-Comm" not in vis
    # add Marauder -> they appear
    lo2 = {**lo, "firmwares": ["meshtastic", "marauder"]}
    vis2 = L.visible_tabs(lo2)
    assert "Targets" in vis2 and "Broadcast" in vis2 and "Cross-Comm" in vis2


def test_gps_gates_wardrive():
    no_gps = {"full_stack": False, "configured": True, "firmwares": ["marauder"], "hardware": ["esp32"]}
    assert "Wardrive" not in L.visible_tabs(no_gps)
    with_gps = {**no_gps, "hardware": ["esp32", "gps"]}
    assert "Wardrive" in L.visible_tabs(with_gps)


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
