"""FlashEngine must tell the truth when a source-only firmware ships no prebuilt binary.

Nine profiles are source-only (``resolver_params.on_error == "source_only_empty"``): halehound,
minigotchi, flock_you, airtag_scanner, cyt_ng, m5gotchi, oui_spy, porkchop, sky_spy. Their GitHub
releases exist but attach NO ``.bin`` — a *successful* fetch legitimately yields an empty list.
The engine used to report the generic ``no firmware asset for chip esp32``, which reads like a
wrong-chip bug; it must instead say the firmware is source-only so the user knows to build it.

Found on real hardware (beat 221): driving ``FlashEngine.flash("COM4", halehound)`` against the live
``JesseCHale/HaleHound-CYD`` v3.7.2 release (0 assets) produced the confusing message. The engine
correctly refused to flash (returned False) — this only sharpens the *why*. Network mocked here.
"""

from __future__ import annotations

import pytest

flash_engine = pytest.importorskip("src.core.flash_engine")
from src.core import flash_core  # noqa: E402
from src.core.flash_engine import FirmwareProfile, FlashEngine  # noqa: E402


class _FakeCore:
    """Stand-in flash_core profile whose release fetch yields the given ``assets``."""

    def __init__(self, assets):
        self._assets = assets

    def latest_release(self):
        return ("v3.7.2", [dict(a) for a in self._assets])

    def variants_for_chip(self, assets, chip):
        return [a for a in assets if a.get("chip") == chip]

    def default_variant(self, assets, chip):
        cands = self.variants_for_chip(assets, chip)
        return cands[0] if cands else None


def _profile(**over) -> FirmwareProfile:
    # chip pinned so no real chip-detect serial runs; core_id is a real profile key so the engine
    # keeps it (not "custom"); raw carries the source-only flag the fix keys off.
    kw = dict(backend="esptool", chip="esp32", core_id="halehound",
              raw={"resolver_params": {"on_error": "source_only_empty"}})
    kw.update(over)
    return FirmwareProfile(**kw)


def _run(monkeypatch, profile, assets):
    monkeypatch.setattr(flash_core, "get_profile", lambda pid: _FakeCore(assets))
    lines: list[str] = []
    ok = FlashEngine().flash("COM5", profile, lambda pct, msg: lines.append(msg))
    return ok, " ".join(lines).lower()


def test_source_only_empty_release_says_build_from_source(monkeypatch):
    ok, joined = _run(monkeypatch, _profile(), [])
    assert ok is False
    assert "source-only" in joined and "build it from source" in joined
    assert "no firmware asset for chip" not in joined  # the confusing message is gone


def test_nonempty_release_with_no_chip_match_keeps_generic_message(monkeypatch):
    # A release that DOES ship binaries but none for this chip is a real chip-coverage gap, NOT
    # source-only — the generic message must stay so a genuine hole is not mislabeled.
    ok, joined = _run(monkeypatch, _profile(), [{"name": "x-esp32s3.bin", "chip": "esp32s3"}])
    assert ok is False
    assert "no firmware asset for chip esp32" in joined
    assert "source-only" not in joined


def test_non_source_only_profile_keeps_generic_message(monkeypatch):
    # An empty release on a profile WITHOUT the flag stays generic — we only make the source-only
    # claim when the profile itself declares it.
    ok, joined = _run(monkeypatch, _profile(raw={"resolver_params": {}}), [])
    assert ok is False
    assert "no firmware asset for chip esp32" in joined
    assert "source-only" not in joined
