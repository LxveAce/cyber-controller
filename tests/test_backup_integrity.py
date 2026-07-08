"""verify_backup_integrity — re-hash a backup .bin against its .meta sha256 to catch on-disk corruption.

The .meta sidecar (written by FlashEngine.backup) records a SHA-256 at backup time; this re-hashes the file
later so a corrupt/truncated backup is caught BEFORE it's relied on to restore a board. No hardware.
"""
from __future__ import annotations

from src.core import backup
from src.core.flash_core import _sha256_file


def _make_backup(tmp_path, data=b"\xff" * 2048, recorded_sha=None, meta_body=None):
    """Write a .bin + its .meta sidecar; return (bin_path, real_sha). recorded_sha=None records the true hash."""
    binp = tmp_path / "bk.bin"
    binp.write_bytes(data)
    real = _sha256_file(str(binp))
    if meta_body is None:
        sha = real if recorded_sha is None else recorded_sha
        meta_body = f"chip=esp32\nflash_size=0x400000\nsize_detected=True\nsha256={sha}\n"
    (tmp_path / "bk.bin.meta").write_text(meta_body, encoding="utf-8")
    return str(binp), real


def test_integrity_ok_when_hash_matches(tmp_path):
    binp, real = _make_backup(tmp_path)
    r = backup.verify_backup_integrity(binp)
    assert r["status"] == "ok"
    assert r["recorded"] == real == r["actual"]
    assert r["size_detected"] == "True"


def test_integrity_mismatch_when_recorded_hash_differs(tmp_path):
    # the .meta records a different hash than the file actually has -> corruption/tamper is caught
    binp, real = _make_backup(tmp_path, recorded_sha="0" * 64)
    r = backup.verify_backup_integrity(binp)
    assert r["status"] == "mismatch"
    assert r["recorded"] == "0" * 64 and r["actual"] == real


def test_integrity_no_meta(tmp_path):
    binp = tmp_path / "bk.bin"
    binp.write_bytes(b"\x00" * 16)
    assert backup.verify_backup_integrity(str(binp))["status"] == "no_meta"


def test_integrity_no_sha_line(tmp_path):
    binp, _real = _make_backup(tmp_path, meta_body="chip=esp32\nflash_size=0x400000\n")
    assert backup.verify_backup_integrity(binp)["status"] == "no_sha"


def test_integrity_missing_file(tmp_path):
    assert backup.verify_backup_integrity(str(tmp_path / "nope.bin"))["status"] == "missing"


def test_list_backups_surfaces_meta(tmp_path):
    # exercises the shared _read_meta path list_backups now uses
    binp, real = _make_backup(tmp_path)
    rows = backup.list_backups(str(tmp_path))
    assert len(rows) == 1
    row = rows[0]
    assert row["file"] == "bk.bin" and row["chip"] == "esp32"
    assert row["sha256"] == real and row["size_detected"] == "True"


def test_verify_backup_cli_exit_codes(tmp_path, capsys):
    # --verify-backup: 0 when intact, 1 when corrupt (script-usable)
    binp, _ = _make_backup(tmp_path)
    assert backup.verify_backup_cli(binp) == 0
    assert "intact" in capsys.readouterr().out

    bad, _ = _make_backup(tmp_path, recorded_sha="0" * 64)
    assert backup.verify_backup_cli(bad) == 1
    assert "CORRUPT" in capsys.readouterr().out


def test_verify_backup_cli_missing_returns_1(tmp_path, capsys):
    assert backup.verify_backup_cli(str(tmp_path / "nope.bin")) == 1
    assert "no such backup" in capsys.readouterr().out


def test_list_backups_cli_empty_and_populated(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert backup.list_backups_cli(str(empty)) == 0
    assert "none found" in capsys.readouterr().out

    _make_backup(tmp_path)  # tmp_path/bk.bin + .meta
    assert backup.list_backups_cli(str(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "1 backup(s)" in out and "bk.bin" in out and "chip=esp32" in out
