"""OUI table load-resilience.

A missing, corrupt, truncated, or mis-encoded bundled OUI table must degrade vendor lookups to ""
— enrichment is optional, never critical — rather than raise into a caller (serial ingestion via
``TargetIngestor._route``, or the targets page rendering a vendor column). Before the beat-235
red-team hardening ``_load_table`` caught only ``FileNotFoundError`` and mutated the module cache in
place, so a corrupt gzip raised on the first lookup and a mid-load failure left a HALF-loaded table
cached forever. This locks the "unreadable table -> empty, never raise, never partial" invariant.
"""
from __future__ import annotations

import gzip

import pytest

from src.core import oui


@pytest.fixture(autouse=True)
def _reset_cache():
    # Start each test from an unloaded cache; restore the real path + cache afterwards so the
    # module global doesn't leak a test path/table into the rest of the suite.
    saved_path, saved_table = oui._TABLE_PATH, oui._table
    oui._table = None
    yield
    oui._TABLE_PATH, oui._table = saved_path, saved_table


def test_missing_table_degrades_to_empty(tmp_path):
    oui._TABLE_PATH = tmp_path / "does-not-exist.tsv.gz"
    assert oui.lookup_vendor("28:05:A5:26:44:40") == ""
    assert oui._table == {}   # cached empty -> no reload thrash on every subsequent lookup


def test_corrupt_gzip_degrades_not_raises(tmp_path):
    bad = tmp_path / "corrupt.tsv.gz"
    bad.write_bytes(b"this is plainly not a gzip stream")
    oui._TABLE_PATH = bad
    # A corrupt table must NOT crash the caller — it degrades to "" like a missing one.
    assert oui.lookup_vendor("28:05:A5:26:44:40") == ""
    assert oui._table == {}   # degraded to empty, not a partial/None cache


def test_good_table_still_resolves(tmp_path):
    good = tmp_path / "ok.tsv.gz"
    with gzip.open(good, "wt", encoding="utf-8") as f:
        f.write("2805A5\tEspressif Inc.\n")
    oui._TABLE_PATH = good
    assert oui.lookup_vendor("28:05:A5:26:44:40") == "Espressif Inc."
    # A locally-administered MAC has no IEEE vendor even with a loaded table.
    assert oui.lookup_vendor("02:05:A5:26:44:40") == ""
