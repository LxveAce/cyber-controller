"""Beat 261 — backup.py flash-dump permission hardening (pass-9 finding [6]).

A full flash dump (``backup_flash``) includes the NVS partition, which stores Wi-Fi SSIDs/PSKs and
other device secrets. Before this fix the backups dir and the ``.bin`` dump + ``.meta`` sidecar were
created with no explicit permissions: on POSIX, under umask 022, that is a 0755 dir and 0644
files — world-readable, so any second local account on a shared host could read the secrets. The fix
routes each path through ``_secure_dir`` / ``_secure_file``, which apply 0o700/0o600 on POSIX and an
owner-only NTFS ACL (``win_acl.secure_dir`` / ``restrict_to_current_user``) on Windows.

These tests run on Windows, where ``os.chmod(..., 0o600)`` is a POSIX no-op — so they assert the
enforcement live here: that the win_acl hardening is INVOKED on the dir, the dump, and the sidecar.
The POSIX-mode (0o700/0o600) side is owner-rig-verified (see the beat-261 ledger).

Discriminating: with backup.py reverted to HEAD (no ``_secure_dir``/``_secure_file``), the first two
tests fail — the helpers don't exist (AttributeError) and the win_acl calls never fire; the third
(happy-path no-regression) passes on both. Restored → all green.
"""

from __future__ import annotations

import pytest

from src.core import backup as bmod
from src.security import win_acl


def test_secure_helpers_invoke_chmod_and_win_acl(tmp_path, monkeypatch):
    """The helpers themselves must reach the win_acl layer with the exact path they were handed."""
    d = tmp_path / "backups"
    d.mkdir()
    f = d / "dump.bin"
    f.write_bytes(b"\x00" * 16)

    secured_dirs: list[str] = []
    restricted: list[str] = []
    monkeypatch.setattr(win_acl, "secure_dir", lambda p: secured_dirs.append(str(p)) or True)
    monkeypatch.setattr(
        win_acl, "restrict_to_current_user", lambda p, **k: restricted.append(str(p)) or True
    )

    bmod._secure_dir(str(d))
    bmod._secure_file(str(f))

    assert str(d) in secured_dirs
    assert str(f) in restricted


def test_backup_flash_hardens_dir_dump_and_sidecar(tmp_path, monkeypatch):
    """End-to-end: a completed backup_flash secures the dest dir and restricts the .bin + .meta."""
    # Avoid hardware: the esptool read_flash call just writes a fake dump to the dest path.
    def fake_run_stream(argv, on_line):
        dest = argv[-1]
        with open(dest, "wb") as fh:
            fh.write(b"\x00" * 4096)
        return 0

    monkeypatch.setattr(bmod, "_run_stream", fake_run_stream)

    secured_dirs: list[str] = []
    restricted: list[str] = []
    monkeypatch.setattr(win_acl, "secure_dir", lambda p: secured_dirs.append(str(p)) or True)
    monkeypatch.setattr(
        win_acl, "restrict_to_current_user", lambda p, **k: restricted.append(str(p)) or True
    )

    out = bmod.backup_flash(
        "COM9", on_line=lambda s: None, chip="esp32",
        output_dir=str(tmp_path), flash_size="0x1000",
    )

    assert out is not None                       # backup succeeded
    assert str(tmp_path) in secured_dirs         # the dest dir was ACL-hardened
    assert out in restricted                     # the .bin dump was restricted to the user
    assert out + ".meta" in restricted           # the .meta sidecar too


def test_backup_flash_happy_path_still_writes_dump_and_meta(tmp_path, monkeypatch):
    """No-regression: hardening must not break a backup — dump + sidecar still land on disk.

    Passes on HEAD and on the fix, so it does NOT discriminate — it guards against the hardening
    aborting a backup (the helpers are best-effort and must never raise into the path)."""
    def fake_run_stream(argv, on_line):
        with open(argv[-1], "wb") as fh:
            fh.write(b"\x00" * 2048)
        return 0

    monkeypatch.setattr(bmod, "_run_stream", fake_run_stream)

    out = bmod.backup_flash(
        "COM9", on_line=lambda s: None, chip="esp32",
        output_dir=str(tmp_path), flash_size="0x800", label="unit",
    )

    assert out is not None
    import os
    assert os.path.isfile(out)
    assert os.path.isfile(out + ".meta")
    with open(out + ".meta", encoding="utf-8") as fh:
        meta = fh.read()
    assert "chip=esp32" in meta
    assert "label=unit" in meta


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
