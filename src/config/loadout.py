"""Loadout — which firmwares/hardware the user uses, driving which tabs/features are shown.

A *loadout* lets the GUI hide features the user won't use ("de-bloat") while keeping everything one click
away in Settings. It is orthogonal to the Simple/Pro dual-depth (which controls *depth* within a shown
feature): ``Full Stack + Pro`` == today's full UI. **Fail-open:** an empty/unconfigured/Full-Stack loadout
shows everything, so a missing or broken config never hides functionality.

This module is pure (no Qt) so it unit-tests without a display. The GUI consumes ``visible_tabs()``
(tab-level de-bloat — see ``main_window.apply_loadout``) and persists the loadout in ``settings.json``
under ``interface.loadout``. Firmware-level filtering (``firmware_visible()`` / ``filter_firmwares()``)
is pure logic that no picker consumes yet: wiring it into the Flash firmware list, the Device View
firmware chooser and the command-palette firmware entries is a tracked follow-up (the same
surface-granularity tradeoff as the per-sub-tab gating noted in ``TAB_REQUIREMENTS``).
See the internal loadout design notes.
"""

from __future__ import annotations

from typing import Iterable

# Selectable firmwares (ids align with the protocol/profile ids).
FIRMWARES = (
    "marauder", "ghostesp", "bruce", "halehound", "esp32_div",
    "flipper", "meshtastic", "bw16", "bluejammer",
)

# Selectable hardware / capability classes the user might own.
HARDWARE = (
    "esp32", "bw16", "flipper", "raspberry_pi", "android_adb", "gps", "usb_os",
)

# Wi-Fi-scanning ESP32 firmwares (gate the scan/target/broadcast tabs).
_SCANNING_FW = frozenset({"marauder", "ghostesp", "bruce", "halehound", "esp32_div"})

ALWAYS = "*"  # a tab that is always shown (core)

# Tab -> the capability tokens that make it relevant (any match -> visible). ALWAYS = core, never hidden.
# Capability tokens are firmware ids, hardware ids, or the derived groups below.
TAB_REQUIREMENTS: "dict[str, object]" = {
    # S4 regroup: "Flash" is the flashing surface (Firmware + Software OS sub-views); Firmware is ALWAYS-core, so
    # the surface is always shown. (Software OS was usb_os-gated as a top-level tab; per-sub-tab gating inside a
    # surface is a documented follow-up — same tradeoff as Wardrive/gps inside Operate.)
    "Flash": ALWAYS,
    # S4 regroup: "Connect" is the landing surface (Devices + Health sub-views). Both members are ALWAYS-core,
    # so the surface is always shown.
    "Connect": ALWAYS,
    # S4 regroup: "Operate" is the grouped action surface (Targets + Broadcast + Macros + Wardrive sub-views).
    # It gates as ONE unit at its most-permissive member: Macros is ALWAYS-available, so the surface is always
    # shown. (Per-sub-tab loadout gating — e.g. hiding Targets/Broadcast/Wardrive inside Operate for a
    # non-wifi/non-gps loadout — is a tracked follow-up; today loadout hides/shows at surface granularity.)
    "Operate": ALWAYS,
    # "Network" is the grouped surface (Graph + Cross-Comm sub-views); it gates as one wifi_scanning unit.
    "Network": {"wifi_scanning"},
    "Settings": ALWAYS,
}

# Canonical tab order (matches main_window._tab_registry); used to re-insert tabs in order.
TAB_ORDER = (
    "Flash", "Connect", "Operate", "Network", "Settings",
)


def default_loadout() -> dict:
    """A sensible first-run default (the common ESP32-WiFi case), not yet configured."""
    return {"full_stack": False, "configured": False,
            "firmwares": ["marauder"], "hardware": ["esp32"]}


def full_stack_loadout() -> dict:
    """Everything on — equivalent to today's full UI."""
    return {"full_stack": True, "configured": True,
            "firmwares": list(FIRMWARES), "hardware": list(HARDWARE)}


def normalize(loadout: "dict | None") -> dict:
    """Coerce a stored loadout into a clean dict; unknown ids dropped. Fail-open on junk."""
    if not isinstance(loadout, dict):
        return default_loadout()
    # Coerce a non-list container (null, a scalar) to [] before iterating — dict.get returns the stored
    # value when the key is present, so a hand-edited "firmwares": null would otherwise raise TypeError
    # and break the "fail-open on junk" contract for the whole loadout.
    fw_raw = loadout.get("firmwares")
    hw_raw = loadout.get("hardware")
    fw = [f for f in fw_raw if f in FIRMWARES] if isinstance(fw_raw, list) else []
    hw = [h for h in hw_raw if h in HARDWARE] if isinstance(hw_raw, list) else []
    return {
        "full_stack": bool(loadout.get("full_stack", False)),
        "configured": bool(loadout.get("configured", False)),
        "firmwares": fw,
        "hardware": hw,
    }


def is_full_stack(loadout: "dict | None") -> bool:
    """True (show everything) when Full Stack, not-yet-configured, or empty — i.e. fail-open."""
    lo = normalize(loadout)
    if lo["full_stack"] or not lo["configured"]:
        return True
    return not lo["firmwares"] and not lo["hardware"]


def capabilities(loadout: "dict | None") -> "set[str]":
    """Expand a loadout into capability tokens (firmware ids + hardware ids + derived groups)."""
    lo = normalize(loadout)
    caps: "set[str]" = set(lo["firmwares"]) | set(lo["hardware"])
    if _SCANNING_FW & set(lo["firmwares"]):
        caps.add("wifi_scanning")
    return caps


def feature_visible(tab: str, loadout: "dict | None") -> bool:
    """Whether *tab* should be shown for this loadout. Fail-open + core tabs always visible."""
    if is_full_stack(loadout):
        return True
    req = TAB_REQUIREMENTS.get(tab, ALWAYS)
    if req == ALWAYS:
        return True
    return bool(set(req) & capabilities(loadout))


def visible_tabs(loadout: "dict | None") -> "list[str]":
    """The tabs to show, in canonical order."""
    return [t for t in TAB_ORDER if feature_visible(t, loadout)]


def firmware_visible(fw_id: str, loadout: "dict | None") -> bool:
    """Pure predicate: whether a firmware *should* appear in pickers (Flash list, Device View chooser,
    command palette). Helper for the not-yet-wired firmware-level filtering follow-up — see the module
    docstring; no GUI picker consumes it today. Fail-open: Full-Stack/unconfigured shows every firmware."""
    if is_full_stack(loadout):
        return True
    return fw_id in set(normalize(loadout)["firmwares"])


def filter_firmwares(fw_ids: "Iterable[str]", loadout: "dict | None") -> "list[str]":
    """Batch form of ``firmware_visible`` (same not-yet-wired follow-up). Preserves input order."""
    return [f for f in fw_ids if firmware_visible(f, loadout)]
