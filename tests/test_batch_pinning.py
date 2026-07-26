"""Batch deck-plan shape + the SHA-256 pin (now inherited via delegation).

BatchFlasher._flash_one delegates to FlashEngine (Atlas's (b) ruling — one flash path), so the
pinned-firmware SHA-256 integrity gate (and the zip-member extraction) are FlashEngine's /
flash_core's single impl — batch can no longer bypass them by drifting. Those semantics are covered
by `test_flash_engine_*`, `test_flash_core`, and `test_batch_flash_parity` (the delegation + the
shared download helper). This file keeps the deck-plan structure guard (independent of flashing).
"""
from __future__ import annotations

import re

from src.core import batch


def test_deck_flash_plan_docstring_count_matches_actual():
    """The '(N devices)' claim in the docstring must equal the number of jobs returned.

    Regression: the docstring said '(14 devices)' while the plan returned only 9
    FlashJob entries, so any caller trusting the documented deck size was misled.
    """
    plan = batch.create_deck_flash_plan()
    assert plan and all(isinstance(j, batch.FlashJob) for j in plan)

    doc = batch.create_deck_flash_plan.__doc__ or ""
    m = re.search(r"\((\d+)\s+devices\)", doc)
    assert m, f"docstring must state a '(N devices)' count, got: {doc!r}"
    assert int(m.group(1)) == len(plan), (
        f"docstring claims {m.group(1)} devices but the plan returns {len(plan)}"
    )


def test_pinned_firmware_flashes_through_the_engine_that_enforces_the_pin(monkeypatch):
    # A pinned profile still routes through FlashEngine (which runs verify_sha256 before writing);
    # the delegation is exactly why batch can no longer skip the pin. Assert it delegates.
    from src.core.flash_engine import FirmwareProfile, FlashEngine
    prof = FirmwareProfile(id="bluejammer_esp32", core_id="bluejammer_esp32", chip="esp32")
    monkeypatch.setattr(FirmwareProfile, "from_file", classmethod(lambda cls, p: prof))
    routed = []
    monkeypatch.setattr(FlashEngine, "flash",
                        lambda self, port, profile, progress=None: (routed.append(profile) or True))
    bf = batch.BatchFlasher(on_line=lambda _s: None)
    res = bf.flash_sequential([batch.FlashJob(port="COM3", profile_id="bluejammer_esp32")])
    assert routed and routed[0] is prof and res[0].success is True
