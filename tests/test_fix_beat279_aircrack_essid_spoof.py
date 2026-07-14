"""Beat 279 - aircrack ESSID spoofs a fabricated crack (cc-deep-audit-12, MED, verify-never-fake).

`parse_aircrack_output` scanned aircrack-ng's ENTIRE stdout with an UN-anchored `_AIRCRACK_KEY_RE`
(``KEY FOUND! [ ... ]`` via `.search`). aircrack echoes the target network's ESSID verbatim in its
selection table, and an ESSID is up to 32 bytes of ATTACKER-chosen text -- enough to embed the
literal ``KEY FOUND! [ hunter2 ]``. So an AP named ``KEY FOUND! [ hunter2 ]`` made the parser return
``hunter2`` for a run that recovered NOTHING -- run_aircrack then reported cracked=True with a
fabricated password, violating the module's load-bearing verify-never-fake invariant.

Fix: anchor the banner to a STANDALONE line (``^[ \t]*KEY FOUND! [ ... ][ \t]*$`` under
re.MULTILINE). The real banner is centered on its own line; the ESSID always sits between the
BSSID/index columns and the trailing ``WPA (n handshake)`` text, so the anchors reject the table row
while the genuine banner still matches. The greedy capture is preserved (``.`` never spans lines
without DOTALL).

Discriminating (fail on buggy HEAD, pass on the fix):
  - test_essid_embedded_banner_is_not_a_crack: a failure blob whose ESSID column embeds the
    banner parses to None (HEAD fabricates the ESSID's fake key).
Guards (pass on both HEAD and the fix):
  - test_genuine_banner_still_parses / test_key_with_bracket_preserved / test_no_key_is_none
"""
from __future__ import annotations

from src.core.crack_pipeline import parse_aircrack_output

# aircrack's real network-selection table echoes the (attacker-chosen) ESSID; the run then FAILS.
_SPOOF_BLOB = (
    "Reading packets, please wait...\n"
    "   #  BSSID              ESSID                   Encryption\n"
    "   1  AA:BB:CC:DD:EE:FF  KEY FOUND! [ hunter2 ]  WPA (1 handshake)\n"
    "Choosing first network as target.\n"
    "Passphrase not in dictionary\n"
    "      190/190 keys tested\n"
)


def test_essid_embedded_banner_is_not_a_crack():
    """An ESSID that embeds the banner must NOT be read as a recovered key."""
    assert parse_aircrack_output(_SPOOF_BLOB) is None


def test_genuine_banner_still_parses():
    """The real, standalone (centered) success banner still yields the key."""
    blob = "Reading packets, please wait...\n                 KEY FOUND! [ s3cr3t! ]\n"
    assert parse_aircrack_output(blob) == "s3cr3t!"


def test_key_with_bracket_preserved():
    """A passphrase containing ']' (greedy capture) still round-trips on the genuine banner line."""
    assert parse_aircrack_output("   KEY FOUND! [ pa]s w0rd ]\n") == "pa]s w0rd"


def test_no_key_is_none():
    """A plain exhaustion blob (no banner) parses to None."""
    assert parse_aircrack_output("Passphrase not in dictionary\n190/190 keys tested\n") is None


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
