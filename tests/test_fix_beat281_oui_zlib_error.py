"""Beat 281 - oui._load_table omits zlib.error (cc-deep-audit-12 rank 2, MED).

`_load_table`'s degrade-to-empty guard caught `(OSError, EOFError, UnicodeDecodeError)`. In-body
DEFLATE corruption raises `zlib.error`, which subclasses `Exception` directly (NOT OSError), so it
escaped the guard, propagated out of `_load_table`, and -- because `_table` was never assigned --
re-raised on EVERY subsequent lookup (the empty fallback is never cached). That breaks the module's
load-bearing invariant (docstring: "corrupt gzip ... degrades to empty ... must return '' rather
than raise into serial ingestion or the targets page"). Note `gzip.BadGzipFile` (a bad magic/header
or trailer CRC) IS an OSError and was already caught; the gap is specifically the mid-stream decode
failure. Fix: add `zlib.error` to the except tuple.

Discriminating (fails on buggy HEAD, passes on the fix):
  - test_corrupt_deflate_body_degrades_to_empty
Guards (pass on BOTH HEAD and the fix) -- prove the fix didn't swallow good/other-error tables:
  - test_valid_table_still_loads / test_missing_table_degrades_to_empty / test_bad_header_degrades
"""
from __future__ import annotations

import gzip

import pytest

from src.core import oui

# Deterministic invalid-DEFLATE: a valid 10-byte gzip header then a 0xFF body byte whose low bits
# decode as BFINAL=1/BTYPE=11 (a reserved block type) -> zlib.error "invalid block type", raised
# before any CRC/EOF check and stable across zlib builds. gzip.BadGzipFile (a bad header or trailer
# CRC) is an OSError and was already caught; this is the mid-stream decompress failure that was not.
_CORRUPT_DEFLATE = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03" + b"\xff\xff\xff\xff" + b"\x00" * 8


@pytest.fixture
def oui_table(tmp_path, monkeypatch):
    """Point oui at a throwaway table path + cleared cache; monkeypatch restores globals after."""
    path = tmp_path / "oui_table.tsv.gz"
    monkeypatch.setattr(oui, "_TABLE_PATH", path)
    monkeypatch.setattr(oui, "_table", None)
    return path


def test_corrupt_deflate_body_degrades_to_empty(oui_table):
    """DISCRIMINATING: an in-body DEFLATE failure (zlib.error) degrades to empty, doesn't escape."""
    oui_table.write_bytes(_CORRUPT_DEFLATE)
    assert oui._load_table() == {}          # no raise on the first call
    assert oui._table == {}                 # empty fallback CACHED so lookups stop re-decompressing
    assert oui.lookup_vendor("00:11:22:33:44:55") == ""   # universal MAC -> _load_table, no raise


def test_valid_table_still_loads(oui_table):
    """GUARD: a well-formed gzip table still loads (the fix must not swallow good tables)."""
    oui_table.write_bytes(gzip.compress(b"001122\tGlobex\nAABBCC\tAcme\n"))
    assert oui._load_table().get("001122") == "Globex"
    assert oui.lookup_vendor("00:11:22:33:44:55") == "Globex"


def test_missing_table_degrades_to_empty(oui_table):
    """GUARD: a missing table degrades to empty (FileNotFoundError path, unchanged)."""
    assert oui._load_table() == {}          # oui_table path was never written


def test_bad_header_degrades_to_empty(oui_table):
    """GUARD: a bad-magic gzip (gzip.BadGzipFile, an OSError) still degrades to empty."""
    oui_table.write_bytes(b"not a gzip file at all, no magic bytes here")
    assert oui._load_table() == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
