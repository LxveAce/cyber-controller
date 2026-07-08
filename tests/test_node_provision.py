"""Tests for node_provision (W1.0) — per-node key provisioning into the gate-keyed vault.

Covers: minting/importing keys, refuse-overwrite, key-free summaries (no secret leakage), the crash-safe
epoch-reservation guarantee (two sessions never share an epoch even without a clean teardown), rx-state
persistence, rotate/deprovision, fail-closed on a locked vault, and one REAL gate-vault integration test
proving the key is encrypted at rest (never appears in vault.enc). All keys here are OBVIOUSLY FAKE.
"""
from __future__ import annotations

import copy
import time

import pytest

from src.core import node_provision as NP
from src.core.node_crypto import KEY_LEN, NonceExhaustedError
from src.core.node_link import NodeLink
from src.core.serial_handler import ConnectionState

FAKE_KEY = bytes(range(32))   # 00 01 02 ... 1f — obviously not a real key


class FakeVault:
    """Dict-backed stand-in for security.vault.Vault: get() returns a COPY (like a disk read), set()
    replaces the whole value (like a disk write). Deep-copies so callers can't mutate stored state."""

    def __init__(self):
        self._d = {}

    def get(self, key, default=None):
        return copy.deepcopy(self._d.get(key, default))

    def set(self, key, value):
        self._d[key] = copy.deepcopy(value)


class MockGateway:
    def __init__(self, port="gw"):
        self.port = port
        self._state = ConnectionState.DISCONNECTED
        self._line_cbs, self._state_cbs = [], []
        self.sent = []

    @property
    def is_connected(self):
        return self._state == ConnectionState.CONNECTED

    @property
    def state(self):
        return self._state

    def on_line(self, cb):
        self._line_cbs.append(cb)

    def remove_line_callback(self, cb):
        try:
            self._line_cbs.remove(cb)
        except ValueError:
            pass

    def on_state_change(self, cb):
        self._state_cbs.append(cb)

    def connect(self):
        self._state = ConnectionState.CONNECTED
        for cb in list(self._state_cbs):
            cb(self._state)

    def disconnect(self):
        self._state = ConnectionState.DISCONNECTED

    def write(self, data):
        self.sent.append(data)

    def deliver(self, line):
        for cb in list(self._line_cbs):
            cb(line)


def _raw(vault, node_id):
    return vault.get(NP._VAULT_NS)[str(node_id)]


# ── minting / import / summaries ─────────────────────────────────────
def test_provision_mints_key_and_returns_key_free_summary():
    v = FakeVault()
    summ = NP.provision_node(v, 7, role="host", label="pager")
    assert "key" not in summ                    # NEVER leak the key in the summary
    assert summ == {"node_id": 7, "role": "host", "label": "pager", "tx_epoch": 0, "rx_epoch": None}
    assert NP.is_provisioned(v, 7)
    assert len(bytes.fromhex(_raw(v, 7)["key"])) == KEY_LEN


def test_minted_keys_are_random():
    v = FakeVault()
    NP.provision_node(v, 1)
    NP.provision_node(v, 2)
    assert _raw(v, 1)["key"] != _raw(v, 2)["key"]


def test_import_known_key_and_length_validation():
    v = FakeVault()
    NP.provision_node(v, 3, key=FAKE_KEY)
    assert bytes.fromhex(_raw(v, 3)["key"]) == FAKE_KEY
    with pytest.raises(ValueError):
        NP.provision_node(v, 4, key=bytes(16))   # wrong length


def test_refuse_overwrite_unless_forced():
    v = FakeVault()
    NP.provision_node(v, 5, key=FAKE_KEY)
    with pytest.raises(NP.NodeExistsError):
        NP.provision_node(v, 5)                  # would strand the peer + nonce state
    NP.provision_node(v, 5, key=bytes([9]) * 32, overwrite=True)
    assert bytes.fromhex(_raw(v, 5)["key"]) == bytes([9]) * 32


def test_list_nodes_is_key_free():
    v = FakeVault()
    NP.provision_node(v, 2, label="b")
    NP.provision_node(v, 1, label="a")
    rows = NP.list_nodes(v)
    assert [r["node_id"] for r in rows] == [1, 2]      # sorted
    assert all("key" not in r for r in rows)


@pytest.mark.parametrize("bad", [-1, 65536, True, "3", 1.0])
def test_bad_node_id_rejected(bad):
    with pytest.raises(ValueError):
        NP.provision_node(FakeVault(), bad)


# ── crash-safe epoch reservation (the core guarantee) ────────────────
def test_open_reserves_epoch_so_sessions_never_share_one():
    v = FakeVault()
    NP.provision_node(v, 9, key=FAKE_KEY, role="host")
    l1 = NP.open_node_link(v, 9, MockGateway())
    assert l1.tx_epoch == 0                    # this session seals under epoch 0
    assert _raw(v, 9)["tx_epoch"] == 1          # ...and the vault already reserved the next one
    l2 = NP.open_node_link(v, 9, MockGateway())
    assert l2.tx_epoch == 1                    # a second session gets a DIFFERENT epoch
    assert _raw(v, 9)["tx_epoch"] == 2


def test_crash_without_persist_still_advances_epoch():
    """If a session dies before any teardown, the reserved-ahead epoch means the next open can't reuse
    the dead session's (epoch, counter) — the whole point of reserving before returning the link."""
    v = FakeVault()
    NP.provision_node(v, 4, key=FAKE_KEY, role="host")
    l1 = NP.open_node_link(v, 4, MockGateway())
    e1 = l1.tx_epoch
    del l1                                      # "crash" — no persist_rx_state, no clean close
    l2 = NP.open_node_link(v, 4, MockGateway())
    assert l2.tx_epoch != e1 and l2.tx_epoch == e1 + 1


def test_concurrent_opens_never_share_an_epoch():
    """The HIGH-severity case: many threads opening the SAME node concurrently must each get a DISTINCT
    epoch (else two sessions seal under the same (key, nonce)). The reservation lock must serialize them."""
    import threading

    class SlowVault(FakeVault):
        # widen the read->write window so an unlocked RMW would definitely collide
        def get(self, key, default=None):
            time.sleep(0.001)
            return super().get(key, default)

    v = SlowVault()
    NP.provision_node(v, 12, key=FAKE_KEY, role="host")
    epochs, errors, lock = [], [], threading.Lock()

    def worker():
        try:
            link = NP.open_node_link(v, 12, MockGateway())
            with lock:
                epochs.append(link.tx_epoch)
        except Exception as e:  # pragma: no cover - surfaced via assert below
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"unexpected errors: {errors}"
    assert len(epochs) == 12
    assert len(set(epochs)) == 12                 # every session got a UNIQUE epoch — no nonce reuse
    assert _raw(v, 12)["tx_epoch"] == 12          # vault advanced exactly once per open


def test_reservation_lockfile_is_released(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_VAULT_DIR", str(tmp_path))
    v = FakeVault()
    NP.provision_node(v, 1, key=FAKE_KEY)
    NP.open_node_link(v, 1, MockGateway())
    assert not (tmp_path / NP._LOCK_NAME).exists()   # lock released after the op


def test_release_only_removes_its_own_lockfile(tmp_path, monkeypatch):
    """The advisory lock must be released BY IDENTITY, not by name. If a holder is stale-stolen (its
    lockfile unlinked+recreated by a successor), releasing by name would delete the SUCCESSOR's live
    lockfile and collapse mutual exclusion — permitting a duplicate epoch reservation (AES-GCM nonce
    reuse). _release_file_lock must leave a differently-identified lockfile in place."""
    import os

    monkeypatch.setenv("CC_VAULT_DIR", str(tmp_path))
    lockpath = tmp_path / NP._LOCK_NAME

    # Positive: a holder releasing its OWN lockfile removes it (identity matches).
    handle = NP._acquire_file_lock()
    assert lockpath.exists()
    NP._release_file_lock(handle)
    assert not lockpath.exists()

    # Negative: if the on-disk lockfile is NOT the one this holder created — a successor stole the
    # (assumed-stale) lock and recreated it, a DIFFERENT identity — release must leave that live file
    # in place. Simulate the successor's file by handing release a handle with a mismatched identity.
    handle = NP._acquire_file_lock()
    fd, path, ident = handle
    mismatched = (fd, path, (ident[0], ident[1] + 1) if ident else (0, 1))
    NP._release_file_lock(mismatched)   # must NOT unlink a lockfile it did not create
    assert lockpath.exists(), "release-by-name deleted a lockfile the holder did not create"
    os.unlink(lockpath)                 # cleanup (fd already closed by _release_file_lock)


def test_open_forwards_role_and_seeds_rx_state():
    v = FakeVault()
    NP.provision_node(v, 6, key=FAKE_KEY, role="node")
    # pretend a prior session persisted an rx head
    tbl = v.get(NP._VAULT_NS)
    tbl["6"]["rx_epoch"], tbl["6"]["rx_highest"] = 42, 100
    v.set(NP._VAULT_NS, tbl)
    link = NP.open_node_link(v, 6, MockGateway())
    assert link.rx_epoch == 42 and link.rx_highest == 100


def test_epoch_exhaustion_refuses_rather_than_wrap():
    v = FakeVault()
    NP.provision_node(v, 1, key=FAKE_KEY)
    tbl = v.get(NP._VAULT_NS)
    tbl["1"]["tx_epoch"] = (1 << 32) - 1        # at the ceiling
    v.set(NP._VAULT_NS, tbl)
    with pytest.raises(NonceExhaustedError):
        NP.open_node_link(v, 1, MockGateway())


# ── rx persistence round-trip through a real NodeLink pair ───────────
def test_persist_rx_state_captures_the_window_head():
    v = FakeVault()
    NP.provision_node(v, 8, key=FAKE_KEY, role="host")
    host_gw = MockGateway("host")
    host = NP.open_node_link(v, 8, host_gw)
    host.connect()
    got = []
    host.on_line(got.append)
    # a peer node (same key, opposite role) sends one frame
    node_gw = MockGateway("node")
    node = NodeLink(node_gw, FAKE_KEY, 8, role="node")
    node.connect()
    node.write("scan-result")
    for line in node_gw.sent:
        host_gw.deliver(line)
    assert got == ["scan-result"]               # round-trip works through the provisioned link
    NP.persist_rx_state(v, 8, host)
    assert _raw(v, 8)["rx_epoch"] == host.rx_epoch
    assert _raw(v, 8)["rx_highest"] == host.rx_highest


# ── rotate / deprovision ─────────────────────────────────────────────
def test_rotate_key_changes_key_and_resets_cursors():
    v = FakeVault()
    NP.provision_node(v, 3, key=FAKE_KEY)
    NP.open_node_link(v, 3, MockGateway())       # advances tx_epoch to 1
    NP.rotate_key(v, 3)
    rec = _raw(v, 3)
    assert bytes.fromhex(rec["key"]) != FAKE_KEY  # fresh key
    assert rec["tx_epoch"] == 0 and rec["tx_counter"] == 0   # new key => fresh nonce space


def test_deprovision():
    v = FakeVault()
    NP.provision_node(v, 5, key=FAKE_KEY)
    assert NP.deprovision_node(v, 5) is True
    assert NP.is_provisioned(v, 5) is False
    assert NP.deprovision_node(v, 5) is False


def test_missing_node_ops_raise():
    v = FakeVault()
    with pytest.raises(NP.NodeNotFoundError):
        NP.open_node_link(v, 1, MockGateway())
    with pytest.raises(NP.NodeNotFoundError):
        NP.rotate_key(v, 1)


# ── fail closed when the gate is locked ──────────────────────────────
def test_current_vault_fails_closed_when_locked(monkeypatch):
    import src.security.access_gate as ag
    monkeypatch.setattr(ag, "get_current_vault", lambda: None)
    with pytest.raises(NP.VaultLockedError):
        NP.current_vault()
    sentinel = FakeVault()
    monkeypatch.setattr(ag, "get_current_vault", lambda: sentinel)
    assert NP.current_vault() is sentinel


# ── REAL gate-keyed vault: encrypted at rest, key never in the file ──
def test_real_vault_roundtrip_and_at_rest_encryption(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_VAULT_DIR", str(tmp_path))
    import importlib

    from src.security import vault as V
    importlib.reload(V)   # ensure it re-reads CC_VAULT_DIR
    try:
        V.set_factor("password", b"test-passphrase")          # bootstraps the vault
        vlt = V.open_vault({"password": b"test-passphrase"})
        assert vlt is not None
        NP.provision_node(vlt, 11, key=FAKE_KEY, role="host", label="node-11")

        # reopen from disk — the key survives the encrypt/decrypt round-trip
        vlt2 = V.open_vault({"password": b"test-passphrase"})
        assert NP.is_provisioned(vlt2, 11)
        assert bytes.fromhex(vlt2.get(NP._VAULT_NS)["11"]["key"]) == FAKE_KEY

        # at rest: the key hex must NOT appear anywhere in the ciphertext file
        enc = (tmp_path / "vault.enc").read_bytes()
        assert FAKE_KEY.hex().encode() not in enc
        assert FAKE_KEY not in enc
    finally:
        importlib.reload(V)   # restore module state for other tests
