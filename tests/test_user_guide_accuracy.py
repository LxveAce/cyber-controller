"""The in-app User Guide must not advertise features the app doesn't ship.

Regression for audit finding [12] (doc-vs-implementation drift): the User Guide's "Available Settings" list named
five controls SettingsTab never builds — Auto-reconnect, Theme, Macro directory, Health polling interval, and
Cross-comm auto-routing (the last deliberately removed as "a toggle that lies") — while omitting the real ones
(Flash baud, Updates, Safety, Access Gate, Secure Container). The Performance guide also named a per-device
"temperature" readout the Health table has no column for (Port, Firmware, Uptime, Signal, Last Seen). The guide is
static help text built inside CyberControllerWindow._on_user_guide, so this asserts on that method's source region —
deterministic, and needs no Qt/window build.
"""

from __future__ import annotations

from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "ui" / "qt" / "main_window.py"


def _user_guide_region() -> str:
    src = _SRC.read_text(encoding="utf-8")
    start = src.index("def _on_user_guide")
    end = src.index("def _on_keyboard_shortcuts", start)
    return src[start:end]


def test_user_guide_does_not_advertise_absent_settings():
    guide = _user_guide_region()
    for phantom in ("Auto-reconnect", "Theme", "Macro directory", "Health polling interval",
                    "Cross-comm auto-routing"):
        assert phantom not in guide, f"User Guide still advertises the non-existent setting {phantom!r}"


def test_user_guide_does_not_claim_a_temperature_readout():
    # The Health table columns are Port, Firmware, Uptime, Signal, Last Seen — there is no temperature.
    assert "temperature" not in _user_guide_region().lower()


def test_user_guide_lists_settings_that_actually_exist():
    guide = _user_guide_region()
    for real in ("Flash baud", "Secure Container", "Access Gate", "Firmware vault"):
        assert real in guide, f"User Guide no longer documents the real setting {real!r}"
