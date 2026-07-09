"""Detection-aware firmware auto-detect (1.7.0 multi-firmware integration).

`resolve_protocol_name` maps a loose/detected firmware string to a REAL registered protocol (or None), and
`DeviceTab._detected_protocol_name` uses it — plus a banner fallback — so a probed GhostESP / Bruce / DIV /
BW16 device auto-routes to its OWN parser instead of defaulting every ESP32 board to Marauder.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.protocols import resolve_protocol_name  # noqa: E402


def test_resolve_exact_registry_keys():
    assert resolve_protocol_name("marauder") == "marauder"
    assert resolve_protocol_name("ghost-esp") == "ghost-esp"
    assert resolve_protocol_name("esp32-div") == "esp32-div"
    assert resolve_protocol_name("bw16") == "bw16"


def test_resolve_tolerates_separator_and_case_drift():
    # device_detect emits 'ghostesp'; a connect stamps the display 'Ghost ESP'; both must resolve.
    assert resolve_protocol_name("ghostesp") == "ghost-esp"
    assert resolve_protocol_name("ghost_esp") == "ghost-esp"
    assert resolve_protocol_name("Ghost ESP") == "ghost-esp"
    assert resolve_protocol_name("esp32div") == "esp32-div"
    assert resolve_protocol_name("ESP32-DIV") == "esp32-div"


def test_resolve_returns_none_for_unknown_or_generic():
    # None means "no real firmware identified" so the caller keeps its default heuristic.
    assert resolve_protocol_name("") is None
    assert resolve_protocol_name("generic") is None
    assert resolve_protocol_name("raw") is None
    assert resolve_protocol_name("totally-not-a-firmware") is None


def test_detected_protocol_name_from_firmware_field():
    from src.ui.qt.device_tab import DeviceTab

    dev = SimpleNamespace(firmware="ghostesp", fw_banner="")
    assert DeviceTab._detected_protocol_name(dev) == "ghost-esp"

    dev2 = SimpleNamespace(firmware="Bruce", fw_banner="")
    assert DeviceTab._detected_protocol_name(dev2) == "bruce"


def test_detected_protocol_name_from_banner_fallback():
    from src.ui.qt.device_tab import DeviceTab

    # No explicit firmware id, but a captured probe banner names the firmware -> match_firmware picks it up.
    dev = SimpleNamespace(firmware="", fw_banner="GhostESP v1.4.2 ready")
    assert DeviceTab._detected_protocol_name(dev) == "ghost-esp"


def test_detected_protocol_name_none_when_undetected():
    from src.ui.qt.device_tab import DeviceTab

    assert DeviceTab._detected_protocol_name(None) is None
    assert DeviceTab._detected_protocol_name(SimpleNamespace(firmware="", fw_banner="")) is None
    # A banner with no recognisable firmware signature must not force a wrong match.
    assert DeviceTab._detected_protocol_name(
        SimpleNamespace(firmware="", fw_banner="boot ok, heap 200000")
    ) is None
