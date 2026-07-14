"""Staged pinned-firmware guard.

A `pinned_release` profile whose pinned reference was never finalized (a `<...>` placeholder still
in the built URL) must fail EARLY at resolve time with a clear "staged" error — not emit a bogus
flash command (a `verify:` offset, a placeholder path) or a confusing mid-download 404. Covers the
two shipped staged stubs: `bluestress` (parked to its own lane, published later) and
`nrf802154_sniffer` (deferred until a real dongle flash). A finalized pin (rtl8720, real vampel
URLs) must STILL resolve — the guard only trips on an unresolved placeholder.

Motivated by the beat-233 pinned_release staleness sweep (24 pinned URLs → 22 live, 2 staged stubs).
"""
from __future__ import annotations

import pytest

from src.core import flash_core


@pytest.mark.parametrize("pid", ["nrf802154_sniffer", "bluestress"])
def test_staged_pinned_profile_raises_clear_error(pid):
    core = flash_core.get_profile(pid)
    with pytest.raises(ValueError) as ei:
        core.latest_release()
    msg = str(ei.value)
    assert pid in msg, "the error must name the staged profile"
    assert "staged" in msg.lower() or "not finalized" in msg.lower()


def test_pinned_url_rejects_unresolved_placeholder():
    cfg = {
        "id": "stub",
        "resolver_params": {"url_sources": {"raw": "https://raw.githubusercontent.com/o/r/<sha>"}},
    }
    with pytest.raises(ValueError):
        flash_core._pinned_url(cfg, "raw", "fw.bin")


def test_finalized_pin_still_resolves():
    # rtl8720 pins real vampel URLs — the guard must NOT trip on a finalized pin.
    core = flash_core.get_profile("rtl8720")
    _tag, assets = core.latest_release()
    assert assets
    assert all("<" not in a["url"] and ">" not in a["url"] for a in assets)
