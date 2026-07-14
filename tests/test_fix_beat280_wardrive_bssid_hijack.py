"""Beat 280 - MAC-format SSID hijacks the BSSID field (cc-deep-audit-12 rank 1, MED).

`_extract_ap_fields` took the FIRST MAC on the line (`_MAC_RE.search`) as the BSSID. In the
SSID-before-BSSID single-line forms (legacy ``SSID: X BSSID: Y ...`` and GhostESP ``SSID: X | BSSID:
Y | ...``) an AP whose SSID is itself a legal MAC string (e.g. ``aa:aa:aa:aa:aa:aa``) supplied that
first MAC, so the SSID value was recorded as the BSSID and the real BSSID dropped. That bad BSSID
becomes the WiGLE export column AND the dedup key, so a nearby attacker advertising a MAC-format
SSID steers the recorded/exported row to a chosen MAC.

Fix: prefer the MAC that follows a literal ``BSSID`` label (`_BSSID_LABELLED_RE`), falling back to
first-MAC only when no label is present. The label-less Marauder scanall form prints a BARE bssid
BEFORE the ESSID, so first-MAC is already the real bssid there -- that shared path must NOT regress.

Discriminating (fail on buggy HEAD, pass on the fix):
  - test_legacy_space_form_mac_ssid_does_not_hijack_bssid
  - test_ghostesp_pipe_form_mac_ssid_does_not_hijack_bssid
Guards (pass on both HEAD and the fix) -- prove the SHARED extractor's other paths are unaffected:
  - test_scanall_bssid_first_still_correct  (label-less scanall -> first-MAC is the real bssid)
  - test_normal_legacy_line_unchanged / test_labelled_bssid_only_line
"""
from __future__ import annotations

from src.core import wardrive as wd


def test_legacy_space_form_mac_ssid_does_not_hijack_bssid():
    """Legacy 'SSID: <mac> BSSID: <mac> ...' -> the labelled BSSID wins, not the SSID's MAC."""
    obs = wd.parse_marauder_ap("SSID: de:ad:be:ef:00:11 BSSID: aa:bb:cc:dd:ee:ff Ch: 6 RSSI: -40")
    assert obs is not None and obs.bssid == "aa:bb:cc:dd:ee:ff"


def test_ghostesp_pipe_form_mac_ssid_does_not_hijack_bssid():
    """GhostESP 'SSID: <mac> | BSSID: <mac> | ...' -> the labelled BSSID wins."""
    line = "SSID: aa:aa:aa:aa:aa:aa | BSSID: 11:22:33:44:55:66 | Ch: 6 | RSSI: -42"
    obs = wd.parse_marauder_ap(line)
    assert obs is not None and obs.bssid == "11:22:33:44:55:66"


def test_scanall_bssid_first_still_correct():
    """Guard (SHARED path): the label-less scanall form (bare bssid before ESSID) still resolves
    the real bssid via the first-MAC fallback -- the fix must not break the TargetPool feed."""
    fields = wd._extract_ap_fields("-50 Ch: 6 11:22:33:44:55:66 ESSID: MyNet 11 05")
    assert fields.get("bssid") == "11:22:33:44:55:66"
    assert fields.get("ssid") == "MyNet"


def test_normal_legacy_line_unchanged():
    """Guard: a normal legacy line (SSID not a MAC) still resolves its labelled BSSID."""
    obs = wd.parse_marauder_ap("SSID: HomeNet BSSID: 11:22:33:44:55:66 Ch: 6 RSSI: -50")
    assert obs is not None and obs.bssid == "11:22:33:44:55:66"


def test_labelled_bssid_only_line():
    """Guard: a bare 'BSSID: <mac>' line (multi-line accumulator) resolves that MAC."""
    assert wd._extract_ap_fields("BSSID: 11:22:33:44:55:66").get("bssid") == "11:22:33:44:55:66"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
