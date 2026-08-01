"""HaleHound support grounded against the real firmware source (Wontfallo/CYD-ESP32, the readable
mirror of binary-only JesseCHale/HaleHound-CYD). Grounding wf_f7e16fbf, 2026-08-01.

The source confirms HaleHound is touchscreen-only with NO serial command CLI, so the honest support is
a read-only parser + an EMPTY command/broadcast/target catalog. These guards pin that: CC must never
present a "send" button (or fabricate a "sent" offensive command) for a firmware with no command channel.
"""
from __future__ import annotations

from src.protocols.halehound import HaleHoundProtocol


def test_no_serial_command_catalog():
    # HaleHound has no scriptable CLI (source-confirmed) — the catalog + broadcast + target maps stay empty
    # so the UI shows no fake command buttons.
    p = HaleHoundProtocol()
    assert p.get_commands() == []
    assert p.driver_type == "controlmap"


def test_format_command_never_builds_a_serial_string():
    # A controlmap firmware has no command channel; format_command must return "" so a future accidental
    # feed can't fabricate a "sent" (offensive) serial command the device would never parse.
    p = HaleHoundProtocol()
    assert p.format_command("wifi_deauth", {"target": "AA:BB:CC:DD:EE:FF"}) == ""
    assert p.format_command("anything") == ""


def test_gps_capability_present_for_the_real_wardriver():
    # HaleHound has a real WiGLE-1.6 Wardriver (needs external GPS) — capabilities must advertise gps.
    assert "gps" in HaleHoundProtocol().capabilities
