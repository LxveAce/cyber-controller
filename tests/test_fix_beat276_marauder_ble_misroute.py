"""Beat 276 - Marauder `_RE_AP`-before-BLE misroute (mmrla-hub beat-17 twin S5, MED).

`parse_line` tries the legacy single-line `_RE_AP` branch FIRST, before the BLE branch. `_RE_AP`
is applied with `.search()` (it can match mid-line), and a BLE device's advertised Name is
attacker-controlled -- Marauder prints it verbatim after `Name:`. So a BLE device that advertises
a name embedding an AP record ("SSID: x BSSID: <mac> Ch: <n> RSSI: <n>") produces a REAL BLE serial
line that ALSO satisfies `_RE_AP`. On buggy HEAD that line is misrouted to an `ap_found` event with
an ATTACKER-CHOSEN BSSID -- a passively injected phantom AP in the shared TargetPool, sourced from a
BLE frame the operator never associated with any Wi-Fi network.

A NORMAL BLE line (`BLE: <mac> Name: <text> RSSI: <n>`) carries no embedded `BSSID:`/`Ch:`, so it
never matched `_RE_AP` -- only a crafted name reproduces the misroute, which is what makes it an
injection rather than a benign quirk.

Fix: guard the legacy `_RE_AP` branch with `not _RE_BLE.search(line) and not _RE_CLIENT.search`
-- exactly the exclusion the scanall single-line branch already uses -- so a genuine BLE (or client)
line falls through to its own branch. A real legacy AP line carries neither `BLE:` nor `Client:`, so
the guard never suppresses a true AP.

Discriminating (fail on buggy HEAD, pass on the fix):
  - test_crafted_ble_name_is_not_misrouted_to_ap: a real BLE line whose Name embeds an AP record is
    parsed as `ble_found` (with the REAL BLE MAC), NOT `ap_found` with the attacker's BSSID.
  - test_crafted_ble_name_injects_no_phantom_index: HEAD assigns the phantom AP a TargetPool ordinal
    via `_assign_index`; the fix must leave `_ap_indices` empty after the crafted BLE line.
Guards (pass on both HEAD and the fix):
  - test_legacy_single_line_ap_still_parses: a genuine legacy single-line AP (`AP:/SSID: ... BSSID:
    ... Ch: ... RSSI:`) still emits `ap_found` with its real BSSID and index 0.
  - test_normal_ble_line_still_parses: an ordinary BLE line still emits `ble_found`.
  - test_client_line_still_parses: a client line still emits `client_found`.
"""
from __future__ import annotations

from src.protocols.marauder import MarauderProtocol

# A genuine BLE serial line: the `Name:` field is whatever the device advertises over the air, so an
# attacker embeds an AP record inside it. The trailing `RSSI: -50` is the real BLE RSSI Marauder
# appends; the inner `RSSI: -40` belongs to the injected fake AP record.
_CRAFTED_BLE = (
    "BLE: aa:bb:cc:dd:ee:ff Name: x SSID: evilnet "
    "BSSID: 11:22:33:44:55:66 Ch: 6 RSSI: -40 RSSI: -50"
)
_ATTACKER_BSSID = "11:22:33:44:55:66"
_REAL_BLE_MAC = "aa:bb:cc:dd:ee:ff"


def test_crafted_ble_name_is_not_misrouted_to_ap():
    """A crafted BLE name must NOT be promoted to an AP with the attacker's BSSID."""
    p = MarauderProtocol()
    ev = p.parse_line(_CRAFTED_BLE)
    assert ev is not None
    assert ev.event_type == "ble_found", (
        f"crafted BLE line misrouted to {ev.event_type} (HEAD emits ap_found)"
    )
    # It is a BLE event for the REAL BLE MAC -- the attacker's embedded BSSID never surfaces.
    assert ev.data.get("mac") == _REAL_BLE_MAC
    assert ev.data.get("bssid") != _ATTACKER_BSSID


def test_crafted_ble_name_injects_no_phantom_index():
    """The crafted BLE line must not register a phantom AP ordinal in the TargetPool key map."""
    p = MarauderProtocol()
    p.parse_line(_CRAFTED_BLE)
    assert p._ap_indices == {}, "a BLE line must not seed _ap_indices with a phantom AP"


def test_legacy_single_line_ap_still_parses():
    """Guard: a genuine legacy single-line AP still emits ap_found with its real BSSID."""
    p = MarauderProtocol()
    ev = p.parse_line("AP: HomeNet BSSID: de:ad:be:ef:00:11 Ch: 11 RSSI: -60")
    assert ev is not None and ev.event_type == "ap_found"
    assert ev.data["bssid"] == "de:ad:be:ef:00:11"
    assert ev.data["index"] == 0


def test_normal_ble_line_still_parses():
    """Guard: an ordinary BLE line still emits ble_found (unchanged)."""
    p = MarauderProtocol()
    ev = p.parse_line("BLE: 12:34:56:78:9a:bc Name: MyWatch RSSI: -55")
    assert ev is not None and ev.event_type == "ble_found"
    assert ev.data["mac"] == "12:34:56:78:9a:bc"


def test_client_line_still_parses():
    """Guard: a client line still emits client_found (the exclusion does not swallow it)."""
    p = MarauderProtocol()
    ev = p.parse_line("Client: 11:22:33:44:55:66 AP: de:ad:be:ef:00:11")
    assert ev is not None and ev.event_type == "client_found"
    assert ev.data["client_mac"] == "11:22:33:44:55:66"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
