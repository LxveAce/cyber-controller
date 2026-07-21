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
    # S4 regroup: Devices+Health folded into the always-shown "Connect" surface and Macros into "Operate", so
    # the core always-visible top-level tabs are the surfaces (Connect/Operate), not their members.
    lo = {"full_stack": False, "configured": True, "firmwares": ["meshtastic"], "hardware": []}
    vis = L.visible_tabs(lo)
    for core in ("Flash", "Connect", "Operate", "Settings"):
        assert core in vis
    for sub in ("Devices", "Health"):
        assert sub not in vis  # Connect sub-views now, never top-level


def test_analyze_surface_always_shown_holds_offline_tools():
    # WS-6 A + capstone fix: "Analyze" (the surface previously labelled "Network") is now ALWAYS shown, not
    # wifi-gated. Unlike the old Network (Graph + Cross-Comm only), Analyze also holds the OFFLINE Crack Lab
    # (cracks a saved .pcap/.hc22000, no radio) and the BLE Analyzer (BLE, not wifi_scanning) — gating the
    # whole surface on wifi would hide those functional, hardware-independent tools from a mesh-only loadout.
    # So no top-level surface is wifi-gated anymore; real de-bloat is firmware-level + a per-sub-tab follow-up.
    lo = {"full_stack": False, "configured": True, "firmwares": ["meshtastic"], "hardware": []}
    vis = L.visible_tabs(lo)
    assert "Analyze" in vis                # always shown now — holds the offline Crack Lab + BLE Analyzer
    assert "Network" not in vis            # the old label is gone
    for sub in ("Targets", "Broadcast", "Cross-Comm", "Crack Lab", "Graph"):
        assert sub not in vis              # sub-views now, never top-level
    # Every surface shows for any configured loadout (all surfaces are ALWAYS post-reorg).
    assert set(vis) == set(L.TAB_ORDER)


def test_operate_surface_always_shown_holds_wardrive():
    # S4 regroup: Wardrive (gps-gated) is now a sub-view of the always-shown "Operate" surface, so gps no
    # longer gates a *top-level* tab. Per-sub-tab gps gating inside Operate is a tracked follow-up (loadout is
    # surface-granularity today). Operate itself is always present because Macros (ALWAYS) anchors it.
    no_gps = {"full_stack": False, "configured": True, "firmwares": ["marauder"], "hardware": ["esp32"]}
    assert "Operate" in L.visible_tabs(no_gps)
    assert "Wardrive" not in L.visible_tabs(no_gps)  # not a top-level tab — it's an Operate sub-view
    with_gps = {**no_gps, "hardware": ["esp32", "gps"]}
    assert "Operate" in L.visible_tabs(with_gps)


def test_software_os_is_a_flash_subview():
    # S4 regroup: Software OS folded into the always-shown "Flash" surface, so it is no longer a usb_os-gated
    # top-level tab. Per-sub-tab gating (hide Software OS inside Flash when no usb_os hardware) is a documented
    # follow-up — same tradeoff as Wardrive/gps inside the Operate surface.
    no_os = {"full_stack": False, "configured": True, "firmwares": ["marauder"], "hardware": ["esp32"]}
    vis = L.visible_tabs(no_os)
    assert "Software OS" not in vis   # not a top-level tab anymore (it's a Flash sub-view)
    assert "Flash" in vis            # the surface that holds it is always shown


def test_firmware_filtering():
    lo = {"full_stack": False, "configured": True, "firmwares": ["marauder", "ghostesp"], "hardware": []}
    assert L.filter_firmwares(["marauder", "ghostesp", "bruce", "flipper"], lo) == ["marauder", "ghostesp"]
    assert not L.firmware_visible("flipper", lo)


def test_normalize_drops_junk():
    lo = L.normalize({"firmwares": ["marauder", "NOPE"], "hardware": ["gps", "bogus"], "full_stack": "x"})
    assert lo["firmwares"] == ["marauder"]
    assert lo["hardware"] == ["gps"]
    assert lo["full_stack"] is True  # bool("x")


def test_normalize_coerces_non_list_containers():
    """normalize() promises "fail-open on junk". dict.get returns the stored value when the key is
    present, so a hand-edited "firmwares": null (or a scalar) must be coerced to [] rather than
    raising TypeError from `for f in None` — the valid sibling keys are still preserved."""
    lo = L.normalize({"configured": True, "firmwares": None, "hardware": ["esp32", "gps"]})
    assert lo["firmwares"] == []
    assert lo["hardware"] == ["esp32", "gps"]
    assert lo["configured"] is True
    # a scalar container is junk too — coerced, not crashed on
    assert L.normalize({"firmwares": 5, "hardware": "esp32"})["firmwares"] == []
    assert L.normalize({"firmwares": 5, "hardware": "esp32"})["hardware"] == []


def test_default_loadout_is_unconfigured():
    d = L.default_loadout()
    assert d["configured"] is False
    assert L.is_full_stack(d)  # until configured, show everything


def test_firmware_filtering_contract_is_honest():
    """Honest-functionality guard: the module must not advertise the GUI as consuming
    ``firmware_visible()`` while no picker actually does.

    ``visible_tabs()`` is wired (main_window.apply_loadout); ``firmware_visible()`` /
    ``filter_firmwares()`` are pure helpers with ZERO src/ consumers today, so the module
    docstring must relabel firmware-level filtering as a follow-up rather than claim it is
    consumed. If a future change wires the helpers into a picker, the claim becomes true and
    this guard steps aside automatically.
    """
    from pathlib import Path

    loadout_path = Path(L.__file__).resolve()
    src_root = loadout_path.parents[1]  # .../src

    consumers: list[str] = []
    for p in src_root.rglob("*.py"):
        if p.resolve() == loadout_path:
            continue  # its own definitions don't count as consumption
        text = p.read_text(encoding="utf-8")
        if "firmware_visible(" in text or "filter_firmwares(" in text:
            consumers.append(p.name)

    doc = " ".join((L.__doc__ or "").split())
    overclaims = "consumes ``visible_tabs()`` / ``firmware_visible()``" in doc

    if consumers:
        # Wired for real -> the contract is allowed to claim consumption.
        return
    assert not overclaims, (
        "loadout docstring claims the GUI consumes firmware_visible() but no src/ picker "
        f"does (consumers={consumers}); relabel firmware-level filtering as a follow-up."
    )
