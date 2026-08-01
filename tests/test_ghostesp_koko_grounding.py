"""GhostESP support grounded against its real upstream (GhostESP-Revival/GhostESP + docs.ghostesp.net).

From the `ghostesp-grounding` research (workflow wf_82c335d9, 2026-08-01): the additive fixes that survived
verify-first. The research also proposed a BLE_SPAM broadcast mapping and a `blescan -ds` relabel, but the
existing safety tests refuted both (BLE_SPAM is palette-only by a 2026-07-15 decision; `blescan -ds`'s
"spam" description is load-bearing for its lab-only gate) — so those were NOT taken. See test_fix_ghostesp /
test_safety for those invariants.
"""
from __future__ import annotations

from src.core.safety import LAB_ONLY, classify
from src.protocols.ghost_esp import GhostESPProtocol


def _catalog():
    return {c.name: c for c in GhostESPProtocol().get_commands()}


def test_real_capture_modes_present():
    # Real GhostESP `capture` modes the catalog previously omitted — incl. `-skimmer`, which a stale
    # comment had wrongly denied ("skimmer detection is Marauder's, not this fw"). All RX/capture.
    names = _catalog()
    for mode in ("capture -ble", "capture -wiresharkble", "capture -skimmer"):
        assert mode in names, f"missing real GhostESP capture mode: {mode}"


def test_attack_csa_and_gtk_present_and_gated():
    # Confirmed verbatim from docs.ghostesp.net: `attack -c` (CSA) and `attack -g <ssid> <password>`
    # (GTK, ssid-then-password). Offensive → must classify lab-only (via the "attack" keyword, no
    # safety.py change) so the two-factor arm still gates them.
    c = _catalog()
    assert "attack -c" in c and "attack -g <ssid> <password>" in c
    assert classify("attack -c", c["attack -c"]) == LAB_ONLY
    assert classify("attack -g <ssid> <password>", c["attack -g <ssid> <password>"]) == LAB_ONLY


def test_wigle_api_uses_the_case_sensitive_name_colon_token_form():
    # Ground truth: `wigle API <APIName>:<APIToken>` — the `API` subcommand is case-SENSITIVE (uppercase),
    # creds are name:token colon-joined. The old lowercase `wigle api <token>` would fail on a real board.
    names = _catalog()
    assert "wigle API <name>:<token>" in names
    assert "wigle api <token>" not in names          # the old, wrong (lowercase, token-only) form is gone


def test_ble_spam_stays_palette_only_not_broadcast():
    # Guard the verify-first revert: the research's proposed BLE_SPAM broadcast mapping was NOT taken
    # (deliberate 2026-07-15 decision — blespam is palette-only). Pinned here + in test_fix_ghostesp.
    from src.core.broadcast import BroadcastVerb
    from src.protocols.ghost_esp import BROADCAST_CAPABILITIES
    assert BroadcastVerb.BLE_SPAM not in BROADCAST_CAPABILITIES
