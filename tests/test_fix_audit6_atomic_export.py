"""Regression guard for the cc-deep-audit-6 pass-6 finding (2026-07-14, ledger pass6 row E1).

E1 (LOW, non-atomic-write) — target_export / capture_export exporters opened the operator's chosen
output in truncating mode (`open(path, "w")`) and wrote in place with no temp+rename — that call
truncates the destination to 0 bytes BEFORE the content is produced, so a write that fails partway
(ENOSPC, a removable/network drive dropping, a kill) destroyed a good prior export and left
nothing valid. The GUI callers reuse a stable default path (`~/cyber-controller-captures.csv`) that
accumulates re-exports, so the overwrite target is routinely a populated prior file. Fix: all three
exporters now go through `wardrive._atomic_write_text` (write a temp sibling, fsync, `os.replace`),
the same temp->replace pattern firmware_vault/wordlist_manager use. Operator-data/path only —
NOT attacker-reachable — but a genuine durability defect. The CSV-injection / numeric / GPS / CR-LF
invariants were separately verified to hold and are unchanged.

Pure logic + fakes: no hardware, network, or real device store is touched.
"""
from __future__ import annotations

import json
import os

import pytest

# ── the core guarantee: a failed write leaves the existing file UNTOUCHED (no truncation) ──

def test_atomic_write_text_preserves_prior_file_on_write_failure(monkeypatch, tmp_path):
    from src.core import wardrive

    dest = tmp_path / "export.csv"
    dest.write_text("PRIOR GOOD EXPORT\n", encoding="utf-8")

    # Force the write to fail after the temp is opened but before os.replace (ENOSPC / a dropped
    # drive). With the old in-place `open(dest, "w")` the destination would already be truncated to
    # 0 bytes by now; the atomic temp->replace must leave `dest` exactly as it was.
    real_replace = os.replace

    def _boom(_src, _dst):
        raise OSError("ENOSPC: no space left on device")

    monkeypatch.setattr(wardrive.os, "replace", _boom)
    with pytest.raises(OSError):
        wardrive._atomic_write_text(str(dest), "NEW CONTENT THAT FAILS TO LAND\n")

    assert dest.read_text(encoding="utf-8") == "PRIOR GOOD EXPORT\n", "prior file must survive"
    # And no temp .part debris is left behind in the directory.
    leftovers = [p for p in os.listdir(tmp_path) if p.startswith(".cc-export-")]
    assert leftovers == [], f"temp file leaked on failure: {leftovers}"
    monkeypatch.setattr(wardrive.os, "replace", real_replace)  # restore for hygiene


def test_atomic_write_text_writes_content_and_replaces(tmp_path):
    from src.core import wardrive

    dest = tmp_path / "out.txt"
    dest.write_text("old", encoding="utf-8")
    wardrive._atomic_write_text(str(dest), "brand new content")
    assert dest.read_text(encoding="utf-8") == "brand new content"
    assert [p for p in os.listdir(tmp_path) if p.startswith(".cc-export-")] == []


# ── the exporters route through the atomic helper (content still correct) ──

class _FakeTarget:
    """Minimal stand-in for src.models.target.Target for target_to_csv_row."""

    class _T:
        def __init__(self, v):
            self.value = v

    def __init__(self, ssid):
        self.target_type = self._T("ap")
        self.ssid = ssid
        self.mac = "aa:bb:cc:dd:ee:ff"
        self.rssi = -52
        self.channel = 6
        self.device_source = "marauder"
        self.encryption = "WPA2"
        self.vendor = "TestVendor"
        self.timestamp = None
        self.last_seen = None


def test_export_targets_csv_is_atomic_and_correct(tmp_path):
    from src.core import target_export

    dest = tmp_path / "targets.csv"
    dest.write_text("STALE", encoding="utf-8")
    n = target_export.export_targets_csv([_FakeTarget("HomeNet")], str(dest))
    assert n == 1
    body = dest.read_text(encoding="utf-8")
    assert body.startswith(",".join(target_export.TARGET_CSV_COLUMNS))
    assert "HomeNet" in body and "STALE" not in body
    assert [p for p in os.listdir(tmp_path) if p.startswith(".cc-export-")] == []


def test_export_targets_csv_failure_keeps_prior(monkeypatch, tmp_path):
    from src.core import target_export, wardrive

    dest = tmp_path / "targets.csv"
    dest.write_text("PRIOR TARGETS EXPORT\n", encoding="utf-8")
    monkeypatch.setattr(wardrive.os, "replace",
                        lambda _s, _d: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        target_export.export_targets_csv([_FakeTarget("X")], str(dest))
    assert dest.read_text(encoding="utf-8") == "PRIOR TARGETS EXPORT\n"


def test_export_captures_json_is_atomic_and_valid(tmp_path):
    from src.core import capture_export

    class _Cap:
        def to_dict(self):
            return {"ssid": "Net", "bssid": "aa:bb:cc:dd:ee:ff"}

    dest = tmp_path / "caps.json"
    dest.write_text("NOT JSON", encoding="utf-8")
    n = capture_export.export_captures_json([_Cap()], str(dest))
    assert n == 1
    parsed = json.loads(dest.read_text(encoding="utf-8"))  # must be valid JSON, not the stale text
    assert parsed == [{"ssid": "Net", "bssid": "aa:bb:cc:dd:ee:ff"}]
    assert [p for p in os.listdir(tmp_path) if p.startswith(".cc-export-")] == []
