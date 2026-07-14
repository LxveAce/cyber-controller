"""Regression tests for the beat-260 target_ingest hardening (pass-9 ledger [4] + [5]).

Each test is DISCRIMINATING: it fails against the pre-fix code and passes against the fix.

- [4] (index-ordinal-drift): a device-list clear (`clearlist`/`reboot`) must invalidate the stale
  per-device scan index on that port's POOL targets, not just the parser ordinal. Otherwise a target
  seen before the clear keeps an ordinal that now maps to a different on-device row, so a later
  Deauth-AP `select ap {index}` binds to the WRONG AP.
- [5] LOW (phantom-enrichment): a MAC-less synthetic `idx:{port}:{index}` AP key must NOT be routed
  into MAC-typed OUI enrichment (normalize_oui's 12-hex floor still passes for a large index, so a
  lookup could fabricate a vendor for a target that has no real MAC).
"""

from __future__ import annotations

import unittest.mock as mock
from types import SimpleNamespace

from src.core import oui as oui_mod
from src.core.cross_comm import TargetPool
from src.core.target_ingest import TargetIngestor
from src.models.target import Target, TargetType


class _ParserStub:
    """Stands in for a per-port protocol parser (the reset hooks are no-ops here)."""

    def reset_scan_index(self) -> None:
        pass

    def reset_station_index(self) -> None:
        pass


class _RecordingPool:
    """Minimal pool that just records add()ed targets (for the [5] enrichment test)."""

    def __init__(self) -> None:
        self.added: list[Target] = []

    def add(self, t: Target) -> bool:
        self.added.append(t)
        return True


# ── [4] clearlist/reboot invalidates the stale pool scan index ────────────────────────────────────

def test_clearlist_invalidates_stale_pool_index():
    pool = TargetPool()
    ap = Target(mac="aa:bb:cc:dd:ee:ff", target_type=TargetType.AP, ssid="HomeNet",
                rssi=-40, channel=6, device_source="COM4", extra={"index": 0})
    pool.add(ap)

    ing = TargetIngestor(pool)
    ing._parsers["COM4"] = _ParserStub()
    ing.note_command_sent("COM4", "clearlist -a")

    stored = pool.get(ap.key)
    assert stored is not None
    # Pre-fix: only the parser ordinal was reset; the pool target kept its stale extra['index'].
    # Fixed: the stale index is dropped, so the resolver stops offering the {index} Deauth action.
    assert "index" not in stored.extra

    # A re-observation carrying a fresh index re-assigns it (the target recovers after a re-scan).
    pool.add(Target(mac="aa:bb:cc:dd:ee:ff", target_type=TargetType.AP,
                     device_source="COM4", extra={"index": 0}))
    assert pool.get(ap.key).extra.get("index") == 0


def test_clearlist_leaves_other_ports_index_untouched():
    # No-regression guard (passes pre- and post-fix): a COM4 clear must not touch a COM7 target.
    pool = TargetPool()
    ap7 = Target(mac="11:22:33:44:55:66", target_type=TargetType.AP,
                 device_source="COM7", extra={"index": 2})
    pool.add(ap7)

    ing = TargetIngestor(pool)
    ing._parsers["COM4"] = _ParserStub()
    ing.note_command_sent("COM4", "clearlist -a")

    assert pool.get(ap7.key).extra.get("index") == 2


def test_reboot_invalidates_both_ap_and_client_index():
    pool = TargetPool()
    ap = Target(mac="aa:bb:cc:dd:ee:ff", target_type=TargetType.AP,
                device_source="COM4", extra={"index": 0})
    client = Target(mac="de:ad:be:ef:00:01", target_type=TargetType.CLIENT,
                    device_source="COM4", extra={"index": 1})
    pool.add(ap)
    pool.add(client)

    ing = TargetIngestor(pool)
    ing._parsers["COM4"] = _ParserStub()
    ing.note_command_sent("COM4", "reboot")

    # reboot clears both the AP and station lists -> both indexes invalidated.
    assert "index" not in pool.get(ap.key).extra
    assert "index" not in pool.get(client.key).extra


def test_targetpool_invalidate_index_filters_by_type():
    pool = TargetPool()
    ap = Target(mac="aa:bb:cc:dd:ee:ff", target_type=TargetType.AP,
                device_source="COM4", extra={"index": 0})
    ble = Target(mac="de:ad:be:ef:00:01", target_type=TargetType.BLE,
                 device_source="COM4", extra={"index": 3})
    pool.add(ap)
    pool.add(ble)

    n = pool.invalidate_index("COM4", TargetType.AP)
    assert n == 1
    assert "index" not in pool.get(ap.key).extra
    assert pool.get(ble.key).extra.get("index") == 3   # BLE untouched by an AP-only invalidation


# ── [5] synthetic idx: key must not be routed into OUI enrichment ─────────────────────────────────

def _ap_event(bssid: str, index):
    return SimpleNamespace(
        event_type="ap_found",
        data={"bssid": bssid, "index": index, "ssid": "Net", "rssi": -40, "channel": 6},
        raw="",
    )


def test_synthetic_idx_key_not_enriched():
    pool = _RecordingPool()
    ing = TargetIngestor(pool)
    with mock.patch.object(oui_mod, "lookup_vendor", return_value="FAKE VENDOR") as looked:
        ing._route(_ap_event(bssid="", index=5), "COM4")   # no BSSID + index -> idx:COM4:5 key
    assert len(pool.added) == 1
    # Pre-fix: lookup_vendor was called on the synthetic key and stamped a phantom vendor. Fixed:
    # the idx: guard skips the lookup, so the MAC-less target stays vendor-less.
    assert pool.added[0].mac == "idx:COM4:5"
    assert pool.added[0].vendor == ""
    looked.assert_not_called()


def test_real_mac_still_enriched():
    # No-regression guard: a real BSSID must still be OUI-enriched.
    pool = _RecordingPool()
    ing = TargetIngestor(pool)
    with mock.patch.object(oui_mod, "lookup_vendor", return_value="FAKE VENDOR"):
        ing._route(_ap_event(bssid="aa:bb:cc:dd:ee:ff", index=None), "COM4")
    assert len(pool.added) == 1
    assert pool.added[0].mac == "aa:bb:cc:dd:ee:ff"
    assert pool.added[0].vendor == "FAKE VENDOR"
