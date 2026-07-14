"""Beat 272 - Marauder BSSID case index-drift in `_ap_indices` (cc-deep-audit-11 [8] LOW).

`MarauderProtocol` assigns each scanned AP a stable ordinal (its `list -a` / `select -a <idx>`
position) keyed in `self._ap_indices` by BSSID. The dict was keyed by the RAW regex-captured BSSID
(`[\\da-fA-F:]{17}` accepts either case), and Marauder prints a BSSID in DIFFERENT case across its
output paths -- the AP-scan lines (`_RE_AP` one-liner and the multi-line ESSID/BSSID/RSSI path) vs
the client line's AP MAC (`_RE_CLIENT` group 2). So a lookup with the "wrong" case silently MISSED
a known AP: a `client_found` for an AP that WAS scanned (upper) but printed lower on the client line
never got its `index` attached, so the resolver drops the `select -a` deauth; and the same AP
re-observed through a second parse path in the other case was keyed as a SECOND entry (duplicate
ordinal) instead of deduping onto its original index.

Fix: canonicalize the key with `.lower()` at the single write chokepoint (`_assign_index`) and at
the read site (`_ap_indices.get(ap_mac.lower())`) -- matching the codebase's `.lower()` MAC
convention (capture_correlate / crack_pipeline / target_ingest / models.capture).

Discriminating (fail on buggy HEAD, pass on the fix):
  - test_client_resolves_ap_index_across_bssid_case: AP scanned UPPER, client line references it
    lower -> the client event resolves to the AP's index (HEAD misses -> no index attached).
  - test_same_ap_deduped_across_case_paths: same AP via the one-liner (UPPER) then the multi-line
    path (lower) -> keeps index 0 (HEAD assigns a duplicate index 1).
Guards (pass on both HEAD and the fix):
  - test_client_same_case_still_resolves: matching case still resolves.
  - test_unknown_ap_client_has_no_index: an unseen AP never gets a guessed index.
"""
from __future__ import annotations

from src.protocols.marauder import MarauderProtocol


def test_client_resolves_ap_index_across_bssid_case():
    """A client line whose AP MAC differs only in case from the scanned AP resolves its index."""
    p = MarauderProtocol()
    ap = p.parse_line("SSID: MyNet BSSID: AA:BB:CC:DD:EE:FF Ch: 6 RSSI: -50")
    assert ap is not None and ap.event_type == "ap_found"
    assert ap.data["index"] == 0
    ev = p.parse_line("Client: 11:22:33:44:55:66 AP: aa:bb:cc:dd:ee:ff")
    assert ev is not None and ev.event_type == "client_found"
    assert ev.data.get("index") == 0, "client must resolve the known AP's index despite BSSID case"


def test_same_ap_deduped_across_case_paths():
    """The SAME AP re-seen through a second parse path in the other case must keep its index."""
    p = MarauderProtocol()
    first = p.parse_line("SSID: MyNet BSSID: AA:BB:CC:DD:EE:FF Ch: 6 RSSI: -50")
    assert first is not None and first.data["index"] == 0
    # Re-observe the same AP via the multi-line accumulator with a lowercase BSSID.
    p.parse_line("ESSID: MyNet")
    p.parse_line("BSSID: aa:bb:cc:dd:ee:ff")
    done = p.parse_line("RSSI: -52")
    assert done is not None and done.event_type == "ap_found"
    assert done.data["index"] == 0, "a re-seen AP must keep its index regardless of BSSID case"


def test_client_same_case_still_resolves():
    """Guard: a client line whose AP MAC matches the scanned case still resolves (unchanged)."""
    p = MarauderProtocol()
    p.parse_line("SSID: MyNet BSSID: aa:bb:cc:dd:ee:ff Ch: 6 RSSI: -50")
    ev = p.parse_line("Client: 11:22:33:44:55:66 AP: aa:bb:cc:dd:ee:ff")
    assert ev is not None and ev.data.get("index") == 0


def test_unknown_ap_client_has_no_index():
    """Guard: a client for an AP never scanned must NOT get a guessed index."""
    p = MarauderProtocol()
    ev = p.parse_line("Client: 11:22:33:44:55:66 AP: de:ad:be:ef:00:11")
    assert ev is not None and ev.event_type == "client_found"
    assert "index" not in ev.data, "an unseen AP must not be assigned a guessed index"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
