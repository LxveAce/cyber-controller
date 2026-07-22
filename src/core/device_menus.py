"""Device-View menu model (UI-agnostic, pure Python — no Qt).

The ESP32 firmwares render their TFT menu locally and only expose a serial CLI, so Cyber Controller
*reconstructs* each firmware's on-board menu as a tree of :class:`MenuNode` and binds every leaf to the
firmware's real serial command. This model was extracted from the Qt Device View so BOTH the Qt view
(`src/ui/qt/device_view.py`) and the web Device View (`src/ui/web`) render the SAME reconstruction — an
honest skin, not a pixel mirror.

`menu_tree(firmware)` serializes a skin to a JSON-able tree with a per-leaf `danger` label (via the shared
:mod:`src.core.safety` classifier) so any front-end can label/confirm dangerous commands without re-deriving
the classification.
"""
from __future__ import annotations

from typing import Optional

from src.core import safety


class MenuNode:
    """A single menu entry: a submenu (children) or a leaf bound to a serial command."""

    def __init__(self, label: str, *, command: Optional[str] = None,
                 needs_arg: bool = False, children: "Optional[list[MenuNode]]" = None):
        self.label = label
        self.command = command
        self.needs_arg = needs_arg   # leaf whose real command REQUIRES an argument (e.g. a file/app name);
        self.children = children or []   # the skin must not fire it as a bare button (would send broken input)

    @property
    def is_menu(self) -> bool:
        return bool(self.children)


# ── a faithful ESP32 Marauder menu (leaves are real Marauder serial commands) ──
def marauder_menu() -> "list[MenuNode]":
    M = MenuNode
    return [
        M("WiFi", children=[
            M("Scan APs", command="scanall"),
            M("Scan Stations", command="scanall"),
            M("Attacks", children=[
                M("Beacon Spam", command="attack -t beacon -r"),
                M("Rick Roll", command="attack -t rickroll"),
                M("Deauth", command="attack -t deauth"),
                M("Probe Flood", command="attack -t probe"),
            ]),
            M("Sniffers", children=[
                M("Beacon Sniff", command="sniffbeacon"),
                M("Deauth Sniff", command="sniffdeauth"),
                M("PMKID", command="sniffpmkid"),
                M("Raw", command="sniffraw"),
            ]),
            M("Channel", command="channel"),
        ]),
        M("Bluetooth", children=[
            M("BLE Scan", command="sniffbt"),
            M("BLE Spam", command="blespam -t all"),
            M("BLE Track", command="sniffbt -t airtag"),
        ]),
        M("Device", children=[
            M("Info", command="info"),
            M("Settings", command="settings"),
            M("Reboot", command="reboot"),
        ]),
    ]


# ── a faithful GhostESP menu (leaves are real GhostESP serial commands) ──
def ghostesp_menu() -> "list[MenuNode]":
    M = MenuNode
    return [
        M("WiFi", children=[
            M("Scan APs", command="scanap"),
            M("Scan Stations", command="scansta"),
            M("Attacks", children=[
                M("Deauth", command="attack -d"),
                M("Beacon Spam", command="beaconspam -r"),
                M("Rick Roll", command="beaconspam -rr"),
            ]),
            M("Capture", children=[
                M("Start", command="capture -eapol"),
                M("Stop", command="capture -stop"),
            ]),
            M("Evil Portal", children=[
                M("Start", command="startportal"),
                M("Stop", command="stopportal"),
            ]),
        ]),
        M("Bluetooth", children=[
            M("BLE Scan", command="blescan"),
            M("BLE Track", command="trackgatt"),
        ]),
        M("Wardrive", children=[
            M("Start", command="startwd"),
            M("Stop", command="startwd -s"),
        ]),
        M("Device", children=[
            M("Info", command="chipinfo"),
            M("GPS Info", command="gpsinfo"),
            M("SD Info", command="sd info"),
            M("Settings", command="settings"),
            M("Reboot", command="reboot"),
        ]),
    ]


# ── a faithful ESP32-DIV menu (leaves are real ESP32-DIV serial commands) ──
def esp32div_menu() -> "list[MenuNode]":
    M = MenuNode
    return [
        M("WiFi", children=[
            M("Scan APs", command="scanwifi"),
            M("Scan Stations", command="scansta"),
            M("Capture", children=[
                M("Sniff", command="sniff"),
                M("PMKID", command="pmkid"),
                M("Handshake", command="handshake"),
            ]),
            M("Attacks", children=[
                M("Deauth", command="deauth"),
                M("Deauth All", command="deauth all"),
                M("Beacon", command="beacon"),
                M("Rick Roll", command="rickroll"),
            ]),
            M("Channel", command="getch"),
        ]),
        M("Bluetooth", children=[
            M("BLE Scan", command="scanble"),
            M("BLE Spam", command="blespam"),
        ]),
        M("2.4GHz", children=[
            M("NRF Scan", command="nrf scan"),
            M("NRF Sniff", command="nrf sniff"),
            M("NRF Jam", command="nrf jam"),
        ]),
        M("Device", children=[
            M("Info", command="info"),
            M("SD Info", command="sd info"),
            M("Settings", command="settings"),
            M("Reboot", command="reboot"),
        ]),
    ]


# ── a faithful Bruce menu (leaves are real Bruce serial commands — see src/protocols/bruce.py) ──
def bruce_menu() -> "list[MenuNode]":
    # Every leaf's command is a REAL Bruce command from BruceProtocol().get_commands(); the three that take
    # an argument (a file/app the menu can't supply) are marked needs_arg so they don't fire a broken line.
    M = MenuNode
    return [
        M("System", children=[
            M("Info", command="info"),
            M("Free Heap", command="free"),
            M("Uptime", command="uptime"),
            M("Reboot", command="reboot"),
        ]),
        M("Infrared", children=[
            M("IR Receive", command="ir rx"),
            M("IR Transmit", command="ir tx"),
        ]),
        M("Sub-GHz", children=[
            M("SubGHz Receive", command="subghz rx"),
            M("SubGHz Transmit", command="subghz tx"),
            M("Replay from File…", command="subghz tx_from_file", needs_arg=True),
        ]),
        M("BadUSB", children=[
            M("Run Ducky Script…", command="badusb run_from_file <script>", needs_arg=True),
        ]),
        M("Apps", children=[
            M("Open App…", command="loader open <app>", needs_arg=True),
        ]),
    ]


# firmware -> (display title, menu factory) for the Device View chooser
SKINS = {
    "marauder": ("ESP32 Marauder", marauder_menu),
    "ghostesp": ("GhostESP", ghostesp_menu),
    "esp32div": ("ESP32-DIV", esp32div_menu),
    "bruce": ("Bruce", bruce_menu),
}


def resolve_skin(firmware: "Optional[str]") -> "Optional[str]":
    """Map a firmware identifier to a SKINS key, tolerating separator drift (device_detect emits
    'esp32-div'/'ghostesp' while the skin keys are 'esp32div'/'ghostesp'). None if there is no skin."""
    if not firmware:
        return None
    key = firmware.strip().lower()
    if key in SKINS:
        return key
    squashed = key.replace("_", "").replace("-", "")
    return next((k for k in SKINS if k.replace("_", "").replace("-", "") == squashed), None)


def _command_info_index(skin_key: str) -> dict:
    """command-string -> CommandInfo for the firmware's protocol, so a menu leaf can pick up an AUTHORITATIVE
    danger annotation (e.g. Bruce 'subghz tx'). Best-effort — empty on any error."""
    try:
        from src.protocols import get_protocol
        return {getattr(ci, "name", ""): ci for ci in get_protocol(skin_key).get_commands()}
    except Exception:  # noqa: BLE001
        return {}


def _leaf_danger(command: "Optional[str]", label: str, info_by_cmd: dict) -> str:
    """Danger level for a menu leaf. Prefer the protocol's real CommandInfo (authoritative annotation);
    otherwise classify the command with the menu label as a description hint so the shared classifier can
    still catch a command whose danger is only in its label (e.g. 'Probe Flood')."""
    if not command:
        return ""
    ci = info_by_cmd.get(command)
    if ci is not None:
        return safety.classify(command, ci)
    from src.protocols.base import CommandInfo
    return safety.classify(command, CommandInfo(name=command, description=label or ""))


def _node_to_dict(node: MenuNode, info_by_cmd: dict) -> dict:
    if node.is_menu:
        return {"label": node.label, "children": [_node_to_dict(c, info_by_cmd) for c in node.children]}
    return {
        "label": node.label,
        "command": node.command,
        "needs_arg": node.needs_arg,
        "danger": _leaf_danger(node.command, node.label, info_by_cmd),
    }


def menu_tree(firmware: "Optional[str]") -> "Optional[dict]":
    """Serialize a firmware's reconstructed menu to a JSON-able tree (title + nested nodes, each leaf tagged
    with `command`/`needs_arg`/`danger`). None if the firmware has no skin."""
    key = resolve_skin(firmware)
    if key is None:
        return None
    title, factory = SKINS[key]
    info_by_cmd = _command_info_index(key)
    return {"firmware": key, "title": title,
            "root": [_node_to_dict(n, info_by_cmd) for n in factory()]}
