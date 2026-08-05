"""RF-transmit commands must be danger-annotated so the lab-only safety gate fires.

Regression: several TX commands carried danger="" and no keyword the substring scan catches, so
safety.classify returned SAFE and device_tab._on_send transmitted them with NO confirmation —
Bruce/Flipper 'subghz tx' / 'subghz tx_from_file' (SubGHz replay, which the disclaimer explicitly
names). Pure logic.

(HaleHound's tesla_charge / nfc_clone were also here, but HaleHound has no scriptable serial CLI —
its whole command catalog was removed as fictional, so there is nothing left to gate. See
src/protocols/halehound.py and cc-control-coverage-PLAN.md.)"""

from __future__ import annotations

import pytest

from src.core import safety


def _ci(fw, name):
    from src.protocols import get_protocol
    proto = get_protocol(fw)
    for ci in proto.get_commands():
        # A command may embed its arg template in the name (e.g. the Flipper's
        # "subghz tx <key_hex> <freq_hz> ..."), so match the verb exactly OR as the leading
        # token(s) before the first placeholder. A trailing space guards "subghz tx" from also
        # matching "subghz tx_from_file".
        if ci.name == name or ci.name.startswith(name + " "):
            return ci
    raise AssertionError(f"{fw}: command {name!r} not found")


@pytest.mark.parametrize("fw,name", [
    ("bruce", "subghz tx"),
    ("bruce", "subghz tx_from_file"),
    ("flipper", "subghz tx"),
    # flipper `ir tx` was danger="" (classified SAFE/ungated); the Offensive rollout gates it
    # lab-only like Bruce. Regression: the category+safety coupling is a safety invariant.
    ("flipper", "ir tx"),
])
def test_rf_transmit_commands_are_gated(fw, name):
    ci = _ci(fw, name)
    # Must classify as at least lab-only so should_confirm() pops a confirmation before sending.
    assert safety.classify(ci.name, ci) == safety.LAB_ONLY, f"{fw}:{name} evades the safety gate"
    assert safety.should_confirm(safety.classify(ci.name, ci), {}) is True
