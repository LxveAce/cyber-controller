"""GhostESP's GPS command is the single token 'gpsinfo', not 'gps info'.

Regression for audit finding [15] (broken command): the command palette (GhostESPProtocol.get_commands) and the
device menu (ghostesp_menu) both sent "gps info" — two tokens — but GhostESP's CLI command is the single token
"gpsinfo", so the device parsed "gps" as an unknown command and never returned GPS status. Proof this is a typo and
not a device quirk (GhostESP is serial-silent here, so it can't be confirmed against hardware): the project's OWN
shipped wardrive macro cc_ghostesp_wardrive_gps.json already uses "gpsinfo". Both interactive surfaces must match
that macro's token. No Qt needed.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.core.device_menus import ghostesp_menu
from src.protocols.ghost_esp import GhostESPProtocol

_MACRO = Path(__file__).resolve().parents[1] / "src" / "core" / "default_macros" / "cc_ghostesp_wardrive_gps.json"


def _macro_gps_token() -> str:
    steps = json.loads(_MACRO.read_text(encoding="utf-8"))["steps"]
    gps = [s["command"] for s in steps if "gps" in s["command"].lower()]
    assert gps, f"macro {_MACRO.name} no longer has a gps step: {steps}"
    return gps[0]


def test_ghostesp_palette_gps_command_is_single_token():
    names = {c.name for c in GhostESPProtocol().get_commands()}
    assert "gpsinfo" in names, f"palette missing 'gpsinfo': {sorted(names)}"
    assert "gps info" not in names, "palette still sends the broken two-token 'gps info'"


def _walk(nodes):
    for n in nodes:
        yield n
        yield from _walk(n.children)


def test_ghostesp_menu_gps_info_is_single_token():
    gps = [n for n in _walk(ghostesp_menu()) if n.label == "GPS Info"]
    assert gps, "GhostESP menu has no 'GPS Info' node"
    assert gps[0].command == "gpsinfo", f"menu 'GPS Info' still sends {gps[0].command!r}"


def test_both_surfaces_match_the_shipped_macro_token():
    # The macro is the SSOT that proved the fix: palette + menu must agree with it.
    token = _macro_gps_token()
    assert token == "gpsinfo"
    palette = {c.name for c in GhostESPProtocol().get_commands()}
    menu = {n.command for n in _walk(ghostesp_menu()) if n.label == "GPS Info"}
    assert token in palette
    assert menu == {token}
