"""Beat 263 — node_provision AES-GCM nonce-reuse cluster (cc-deep-audit-10 [2] MED + [6] LOW).

Both defects rewind or duplicate the (key, nonce) space AES-GCM sealing depends on — GCM's one hard
rule is *never reuse a (key, nonce) pair*, and a nonce here is epoch(u32)-counter(u64).

[6] provision_node(overwrite=True, key=<same bytes>) unconditionally reset tx_epoch/tx_counter to 0.
    A fresh key legitimately starts at 0 (unused key = unused nonce space), but overwriting with the
    SAME key rewinds the nonce into already-used pairs -> catastrophic reuse. Fix: preserve the
    cursors when the key is unchanged; reset only on a genuine key change (like rotate_key()).

[2] The stale-lock steal judged staleness from a pre-rename stat of `path`, then renamed it.
    Between that stat and the rename a racer can steal+recreate a FRESH lock (a DIFFERENT inode);
    the old code renamed THAT live file away and double-acquired -> two openers reserve the same
    tx_epoch -> nonce reuse. Fix: pin the judged inode and after the rename confirm it moved that
    SAME (dev, ino); if it moved a fresh inode instead, restore it and back off, don't reclaim.

Discriminating: test_overwrite_same_key_preserves_nonce_cursors and
test_toctou_recreated_fresh_lock_is_not_stolen fail on HEAD (cursors reset to 0; the fresh lock is
stolen) and pass on the fix; the two guards pass on both.
"""
from __future__ import annotations

import os

import pytest

from src.core import node_provision as NP

FAKE_KEY = bytes(range(32))


def _raw(vault, node_id):
    return vault.get(NP._VAULT_NS)[str(node_id)]


class _Vault:
    """Dict-backed vault: get() returns a deep copy (disk read), set() replaces (disk write)."""

    def __init__(self):
        import copy
        self._copy = copy.deepcopy
        self._d = {}

    def get(self, key, default=None):
        return self._copy(self._d.get(key, default))

    def set(self, key, value):
        self._d[key] = self._copy(value)


# ── [6] overwrite with the same key must not rewind the nonce cursors ──

def test_overwrite_same_key_preserves_nonce_cursors():
    v = _Vault()
    NP.provision_node(v, 5, key=FAKE_KEY)
    tbl = v.get(NP._VAULT_NS)
    tbl["5"].update(tx_epoch=7, tx_counter=42, rx_epoch=3, rx_highest=99)
    v.set(NP._VAULT_NS, tbl)

    NP.provision_node(v, 5, key=FAKE_KEY, label="relabel", overwrite=True)  # SAME key

    rec = _raw(v, 5)
    # HEAD reset these to 0/0/None/-1 (nonce replay under the reused key); the fix preserves them.
    assert rec["tx_epoch"] == 7
    assert rec["tx_counter"] == 42
    assert rec["rx_epoch"] == 3
    assert rec["rx_highest"] == 99
    assert rec["label"] == "relabel"  # the role/label update still applied


def test_overwrite_different_key_resets_cursors():
    """No-regression: a DIFFERENT key IS a new nonce space, so cursors reset to 0. Both HEAD+fix."""
    v = _Vault()
    NP.provision_node(v, 5, key=FAKE_KEY)
    tbl = v.get(NP._VAULT_NS)
    tbl["5"].update(tx_epoch=7, tx_counter=42)
    v.set(NP._VAULT_NS, tbl)

    NP.provision_node(v, 5, key=bytes([9]) * 32, overwrite=True)  # DIFFERENT key

    rec = _raw(v, 5)
    assert rec["tx_epoch"] == 0
    assert rec["tx_counter"] == 0


# ── [2] the stale-steal must not reclaim a fresh lock recreated mid-race ──

def test_toctou_recreated_fresh_lock_is_not_stolen(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_VAULT_DIR", str(tmp_path))
    lockpath = str(tmp_path / NP._LOCK_NAME)
    with open(lockpath, "wb"):
        pass
    os.utime(lockpath, (0, 0))  # far in the past -> _stat_if_stale pins it as stale

    real_rename = os.rename
    swapped = {"done": False}

    def racing_rename(src, dst):
        # On the steal rename, simulate a racer that already stole+recreated a FRESH lock: replace
        # the lockfile with a brand-new different-inode file BEFORE the real rename moves it.
        if (not swapped["done"] and os.path.normpath(src) == os.path.normpath(lockpath)
                and ".stale." in os.fspath(dst)):
            swapped["done"] = True
            os.unlink(lockpath)
            fd = os.open(lockpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)  # new inode+mtime
            os.close(fd)
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", racing_rename)

    # HEAD renamed the fresh file away, unlinked it, and O_EXCL-created its own -> returned a handle
    # (double-acquire). The fix pins the judged inode, sees it moved a DIFFERENT one, restores it,
    # and times out instead of stealing a live lock.
    with pytest.raises(NP.NodeProvisionError):
        NP._acquire_file_lock(timeout=0.3, stale=30.0)
    assert swapped["done"], "the steal/TOCTOU path was never exercised"
    assert os.path.exists(lockpath), "the fresh lock was not restored (it was stolen)"


def test_genuinely_stale_lock_still_stolen(tmp_path, monkeypatch):
    """No-regression: a truly stale lock (same inode throughout) is still reclaimed, no deadlock."""
    monkeypatch.setenv("CC_VAULT_DIR", str(tmp_path))
    lockpath = str(tmp_path / NP._LOCK_NAME)
    with open(lockpath, "wb"):
        pass
    os.utime(lockpath, (0, 0))

    handle = NP._acquire_file_lock(timeout=1.0, stale=30.0)
    try:
        assert handle is not None
        assert os.path.exists(lockpath)
        assert list(tmp_path.glob(NP._LOCK_NAME + ".stale.*")) == []  # no rename debris
    finally:
        NP._release_file_lock(handle)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
