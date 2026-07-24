"""OUI → vendor lookup + its TargetIngestor wiring.

Uses the REAL bundled IEEE table for the known-vendor + integration assertions (verify-never-fake:
the values are the actual registry entries), and an injected table for the loader-isolation tests.
"""
from __future__ import annotations

import pytest

from src.core import oui

# ── normalize_oui ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mac", [
    "D4:8A:FC:11:22:33", "d4-8a-fc-11-22-33", "d48a.fc11.2233", "D48AFC112233", "d4 8a fc 11 22 33",
])
def test_normalize_oui_strips_separators_and_uppercases(mac):
    assert oui.normalize_oui(mac) == "D48AFC"


@pytest.mark.parametrize("mac", [
    "",                       # empty
    "D4:8A",                  # too few hex digits
    "D4:8A:FC",               # a bare 24-bit OUI, not a full MAC — must NOT resolve on its own
    "idx:COM7:196",           # MAC-less synthetic index key (BW16 Vampire) — hex chars total DC7196
    "idx:COM3:714",           # another synthetic key that used to phantom-resolve
    "zz:zz:zz:11:22:33",      # no hex
    "02:11:22:33:44:55",      # locally administered (randomized privacy MAC) — not vendor-assigned
    "01:00:5e:00:00:fb",      # multicast / group bit set
    "FF:FF:FF:FF:FF:FF",      # broadcast
])
def test_normalize_oui_rejects_non_vendor_addresses(mac):
    assert oui.normalize_oui(mac) is None


# ── lookup_vendor against the REAL bundled table ──────────────────────────────

def test_lookup_vendor_resolves_known_ieee_ouis():
    assert "Espressif" in oui.lookup_vendor("D4:8A:FC:00:11:22")   # ESP32 vendor — CC's core domain
    assert oui.lookup_vendor("F0:EE:7A:00:11:22") == "Apple, Inc."
    assert oui.lookup_vendor("D8:3A:DD:00:11:22") == "Raspberry Pi Trading Ltd"


def test_lookup_vendor_longest_prefix_ma_s_wins():
    # 00:1B:C5 is a MA-S administrator block (no /24 vendor); the device's real org lives in a /36 sub-block.
    # A 24-bit-only lookup would return "" (or the admin); longest-prefix returns the actual vendor.
    assert oui.lookup_vendor("00:1B:C5:00:00:01") == "Converging Systems Inc."


def test_lookup_vendor_empty_for_unknown_and_randomized(monkeypatch):
    assert oui.lookup_vendor("02:AA:BB:CC:DD:EE") == ""   # locally administered -> no vendor
    assert oui.lookup_vendor("not-a-mac") == ""
    monkeypatch.setattr(oui, "_table", {"D48AFC": "Espressif Inc."})
    assert oui.lookup_vendor("3C:AB:CD:00:00:00") == ""   # valid OUI, just not in the table


# ── BYO / refresh loaders (isolated table via monkeypatch) ──────────────────────

def test_load_ieee_csv_merges_quoted_names_and_skips_private(monkeypatch):
    monkeypatch.setattr(oui, "_table", {})  # isolate: don't touch the bundled table
    csv_text = (
        "Registry,Assignment,Organization Name,Organization Address\n"
        'MA-L,3CAB01,"Acme, Inc.",1 Road City US\n'
        "MA-L,3CAB02,Private,\n"
        "MA-L,short,Bad Row,\n"
    )
    import tempfile
    from pathlib import Path
    f = Path(tempfile.mkdtemp()) / "oui.csv"
    f.write_text(csv_text, encoding="utf-8")
    added = oui.load_ieee_csv(f)
    assert added == 1                                        # only the one valid, non-Private row
    assert oui.lookup_vendor("3C:AB:01:00:00:00") == "Acme, Inc."   # comma-quoted name preserved
    assert oui.lookup_vendor("3C:AB:02:00:00:00") == ""      # 'Private' skipped


def test_load_manuf_merges_all_block_sizes_longest_prefix(monkeypatch):
    monkeypatch.setattr(oui, "_table", {})
    manuf = (
        "# a comment line\n"
        "08:AB:01\tAcmeShort\tAcme Corporation Long\n"        # /24 MA-L, full name preferred
        "08:AB:03:00/28\tMidShort\tMid Block GmbH\n"           # /28 MA-M
        "10:CD:EF\tRegAdmin\tRegistry Admin\n"                 # /24 administrator block
        "10:CD:EF:00:00/36\tRealShort\tReal Vendor Inc\n"      # /36 MA-S inside the /24 admin
    )
    import tempfile
    from pathlib import Path
    f = Path(tempfile.mkdtemp()) / "manuf"
    f.write_text(manuf, encoding="utf-8")
    added = oui.load_manuf(f)
    assert added == 4                                          # all three block sizes now merge
    assert oui.lookup_vendor("08:AB:01:aa:bb:cc") == "Acme Corporation Long"  # full name (col 3) preferred
    assert oui.lookup_vendor("08:AB:03:0a:bb:cc") == "Mid Block GmbH"         # /28 resolves
    assert oui.lookup_vendor("10:CD:EF:00:00:0f") == "Real Vendor Inc"        # /36 WINS over the /24 admin
    assert oui.lookup_vendor("10:CD:EF:99:99:99") == "Registry Admin"         # outside the /36 -> /24 admin


# ── TargetIngestor wiring (real table, real ingestor path) ──────────────────────

class _Ev:
    def __init__(self, event_type: str, **data) -> None:
        self.event_type = event_type
        self.data = data


class _Proto:
    def __init__(self, ev: _Ev) -> None:
        self._ev = ev

    def parse_line(self, _line: str):
        return self._ev


class _Conn:
    port = "COM7"

    def on_line(self, _cb) -> None:  # attach registers here; test drives the returned cb directly
        pass


def _drive(ev: _Ev):
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.target_ingest import TargetIngestor

    pool = TargetPool(EventBus())
    ing = TargetIngestor(pool)
    cb = ing.attach(_Conn(), _Proto(ev))
    cb("any raw line")   # -> parse_line -> _event_to_target -> vendor enrichment -> pool.add
    return pool


def test_ingest_populates_vendor_for_wifi_ap_from_oui():
    pool = _drive(_Ev("ap_found", bssid="D4:8A:FC:10:20:30", ssid="Lab"))
    (t,) = pool.all()
    assert "Espressif" in t.vendor      # the Espressif AP now carries its vendor label


def test_ingest_leaves_vendor_empty_for_randomized_mac():
    pool = _drive(_Ev("ap_found", bssid="02:11:22:33:44:55", ssid="Rand"))
    (t,) = pool.all()
    assert t.vendor == ""               # a randomized privacy MAC must NOT get a fabricated vendor


def test_ingest_leaves_vendor_empty_for_macless_index_key():
    # An index-only firmware (BW16 Vampire) reports an AP with a scan index but NO bssid, so the
    # ingestor keys it under the synthetic `idx:{port}:{idx}`. That is not a MAC and must never
    # resolve to a vendor — this is the FABRICATE-0713 re-open (idx:COM7:196 used to phantom Intel).
    pool = _drive(_Ev("ap_found", index=196, ssid="Vamp"))
    (t,) = pool.all()
    assert t.mac == "idx:COM7:196"      # the synthetic MAC-less key (port COM7 from _Conn)
    assert t.vendor == ""               # no phantom vendor on a target that has no MAC at all


def test_ingest_preserves_preset_flock_vendor():
    # An ALPR camera already carries "Flock Safety …"; the OUI enrichment must not overwrite it even
    # though its MAC would otherwise resolve to Espressif.
    pool = _drive(_Ev("alpr_found", mac="D4:8A:FC:10:20:30", ssid="cam"))
    (t,) = pool.all()
    assert t.vendor.startswith("Flock Safety")
