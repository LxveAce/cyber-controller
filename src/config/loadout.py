"""Loadout — which firmwares/hardware the user uses, driving which tabs/features are shown.

A *loadout* lets the GUI hide features the user won't use ("de-bloat") while keeping everything one click
away in Settings. It is orthogonal to the Simple/Pro dual-depth (which controls *depth* within a shown
feature): ``Full Stack + Pro`` == today's full UI. **Fail-open:** an empty/unconfigured/Full-Stack loadout
shows everything, so a missing or broken config never hides functionality.

This module is pure (no Qt) so it unit-tests without a display. The GUI consumes ``visible_tabs()``
(tab-level de-bloat — see ``main_window.apply_loadout``) and persists the loadout in ``settings.json``
under ``interface.loadout``. Firmware-level filtering (``firmware_visible()`` / ``filter_firmwares()``)
is pure logic that no picker consumes yet: wiring it into the Flash firmware list and the
command-palette firmware entries is a tracked follow-up (the same surface-granularity tradeoff as
the per-sub-tab gating noted in ``TAB_REQUIREMENTS``).
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
#
# Spade v2 verb IA (P2.5): the top-level surfaces are the 5 job-verbs + pinned Settings. Every verb
# surface groups hardware-independent tools alongside hardware-gated ones (e.g. RIG holds the offline
# firmware vault + host Health; HUNT holds the offline analyzers; MAP holds Flock's saved map; CRACK is
# the fully-offline cracker). Gating a whole verb on one radio would hide those functional, radio-free
# tools — exactly the functionality-hiding the fail-open rule forbids — so every verb is ALWAYS (per-
# sub-view loadout gating inside a surface stays the documented follow-up).
TAB_REQUIREMENTS: "dict[str, object]" = {
    "RIG": ALWAYS,       # Devices + Health + Nodes + Firmware + Software OS + Mesh
    "HUNT": ALWAYS,      # Wi-Fi + BLE analyzers + Targets + Graph (passive awareness)
    "OPERATE": ALWAYS,   # Home launcher + Control (merged fan-out + console) + Macros
    "CRACK": ALWAYS,     # the offline Crack Lab (no radio needed)
    "MAP": ALWAYS,       # Wardrive + Multi-Wardrive + Flock Map (Flock loads a saved map hardware-free)
    "Settings": ALWAYS,
}

# Canonical tab order (matches main_window._tab_registry / nav_model.visible_nav); re-inserts tabs in order.
TAB_ORDER = (
    "RIG", "HUNT", "OPERATE", "CRACK", "MAP", "Settings",
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
    """Pure predicate: whether a firmware *should* appear in pickers (Flash list, command palette).
    Helper for the not-yet-wired firmware-level filtering follow-up — see the module
    docstring; no GUI picker consumes it today. Fail-open: Full-Stack/unconfigured shows every firmware."""
    if is_full_stack(loadout):
        return True
    return fw_id in set(normalize(loadout)["firmwares"])


def filter_firmwares(fw_ids: "Iterable[str]", loadout: "dict | None") -> "list[str]":
    """Batch form of ``firmware_visible`` (same not-yet-wired follow-up). Preserves input order."""
    return [f for f in fw_ids if firmware_visible(f, loadout)]
