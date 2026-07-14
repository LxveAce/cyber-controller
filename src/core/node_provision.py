"""Host-side per-node key provisioning for the wireless-node link (W1.0).

Mints and persists the AES-256 keys **and** the monotonic nonce state that :class:`NodeLink`
(``src/core/node_link.py``) needs, storing them INSIDE the gate-keyed vault (``src/security/vault.py``).

Why host-side only / where the secrets live
--------------------------------------------
The per-node key and its nonce cursors ARE the security of the wireless link, so they live ONLY in the
vault at ``~/.cyber-controller/vault.enc`` — AES-256-GCM encrypted at rest under the access gate, written
0600 / owner-ACL, OUTSIDE the repo tree and gitignored. This module holds NO secret material of its own:
it reads/writes the vault and hands key bytes only to :class:`NodeLink` at connect time. Nothing here is
ever committed with key bytes, and :func:`list_nodes` never returns a key.

Restart nonce-safety (the point of provisioning)
------------------------------------------------
AES-GCM dies on (key, nonce) reuse. ``NodeLink`` alone defaults to a *random* sender epoch when no state
is restored — safe only probabilistically. Provisioning makes it **deterministic** with an
epoch-reservation scheme that is correct even across a crash:

  * Each :func:`open_node_link` reserves the record's current ``tx_epoch`` for THIS session and immediately
    persists ``tx_epoch + 1`` back to the vault *before* returning the link. The session then seals under
    ``(reserved_epoch, counter 0..N)``.
  * If the process dies mid-session, the vault already points at ``reserved_epoch + 1``, so the next open
    uses a fresh epoch — two sessions can NEVER share an epoch, hence never a (key, nonce) pair, with no
    need to fsync every frame's counter.
  * The receiver window head (``rx_epoch``/``rx_highest``) is persisted on teardown via
    :func:`persist_rx_state` so anti-replay also survives a restart.

Rotating a key (:func:`rotate_key`) resets the cursors to zero — legitimate, because a brand-new key is a
brand-new nonce space.

Concurrency: the reservation is a read-modify-write, so it is serialized by an in-process lock AND a
cross-process advisory lockfile (:func:`_reservation_lock`) — otherwise two overlapping opens could both
read the same ``tx_epoch`` and reuse a nonce. (Known theoretical bound: a single session would only rotate
into the next reserved epoch after 2^64 frames — physically unreachable at any real frame rate; overflow
should trigger :func:`rotate_key`, not an epoch step.)
"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from src.core.node_crypto import KEY_LEN, NonceExhaustedError, _MAX_U16, _MAX_U32
from src.core.node_link import NodeLink

log = logging.getLogger(__name__)

# Reservation must be ATOMIC: two overlapping opens that both read the same tx_epoch would both seal under
# the same (key, nonce) — catastrophic for AES-GCM. So every vault-table mutation is serialized by an
# in-process lock (threads) AND a cross-process advisory lockfile (a second app instance on the same vault).
_RES_LOCK = threading.RLock()
_LOCK_NAME = "node_provision.lock"


def _lock_dir() -> Path:
    return Path(os.environ.get("CC_VAULT_DIR") or (Path.home() / ".cyber-controller"))


def _stat_if_stale(path: Path, stale: float) -> Optional[tuple]:
    """If *path* is older than *stale* seconds, return its pinned ``(st_dev, st_ino)``; else None.

    Pins the EXACT inode judged stale so the caller can confirm — after an atomic rename — that it
    moved THAT inode, not a fresh lock a racer recreated in the check-rename gap."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if time.time() - st.st_mtime <= stale:
        return None
    return (st.st_dev, st.st_ino)


def _same_inode(path: str, ident: tuple) -> bool:
    """True iff *path* currently resolves to the pinned ``(st_dev, st_ino)`` in *ident*."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == ident


def _acquire_file_lock(timeout: float = 10.0, stale: float = 30.0) -> Optional[tuple]:
    """Best-effort exclusive lock via O_CREAT|O_EXCL (works on Windows + POSIX). Returns a handle
    ``(fd, path, ident)`` where *ident* is the created file's ``(st_dev, st_ino)``, or None if the lock
    directory can't be used (then the in-process lock is the only guard — fine for single-process)."""
    try:
        d = _lock_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / _LOCK_NAME
    except OSError:
        return None
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # Steal a lock left by a crashed holder, ATOMICALLY, and only the STALE inode we judged.
            # os.rename is atomic (one racer moves an inode; losers' rename raises), but judging
            # staleness from a pre-rename stat of `path` is a TOCTOU: a racer can steal+recreate a
            # FRESH lock between that stat and the rename, and we'd rename THAT live file away and
            # double-hold -> two openers reserve one tx_epoch = catastrophic AES-GCM reuse. So
            # PIN the inode we judged and, after the rename, confirm we grabbed that same (dev,ino);
            # if we moved a different (fresh) inode instead, restore it and back off, don't reclaim.
            pinned = _stat_if_stale(path, stale)
            if pinned is not None:
                stolen = f"{path}.stale.{os.getpid()}.{time.monotonic_ns()}"
                try:
                    os.rename(path, stolen)  # atomic claim of whatever inode is at `path` right now
                except OSError:
                    pass  # a losing racer (name already moved) — fall through and retry
                else:
                    if _same_inode(stolen, pinned):
                        with contextlib.suppress(OSError):
                            os.unlink(stolen)  # reclaimed exactly the stale inode we pinned
                        continue
                    # Moved a DIFFERENT (fresh) inode: put it back so its holder's identity-based
                    # release still works, then treat the lock as held and back off.
                    with contextlib.suppress(OSError):
                        os.rename(stolen, path)
            if time.monotonic() > deadline:
                raise NodeProvisionError("could not acquire node-provision lock (another op holds it)")
            time.sleep(0.02)
        else:
            # Record the created file's identity so release only removes OUR lockfile. If another
            # process stole this lock as stale and recreated it, that successor's file has a different
            # inode — deleting it BY NAME here would collapse mutual exclusion and let a third opener in.
            try:
                st = os.fstat(fd)
                ident = (st.st_dev, st.st_ino)
            except OSError:
                ident = None
            return (fd, str(path), ident)


def _release_file_lock(handle: Optional[tuple]) -> None:
    if handle is None:
        return
    fd, path, ident = handle
    with contextlib.suppress(OSError):
        os.close(fd)
    # Release BY IDENTITY, not by name: only unlink the lockfile if it is still the exact file we
    # created. After a stale-steal a successor may have unlinked+recreated it; removing that live file
    # would drop the guard against a concurrent reservation (AES-GCM epoch/nonce reuse).
    try:
        cur = os.stat(path)
        if ident is None or (cur.st_dev, cur.st_ino) == ident:
            os.unlink(path)
    except OSError:
        pass


@contextlib.contextmanager
def _reservation_lock():
    """Serialize a read-modify-write against the vault table (in-process + cross-process)."""
    with _RES_LOCK:
        fd = _acquire_file_lock()
        try:
            yield
        finally:
            _release_file_lock(fd)

# Vault namespace. Value is {"<node_id>": {key, role, label, tx_epoch, tx_counter, rx_epoch, rx_highest}}.
_VAULT_NS = "node_keys"
_ROLES = ("host", "node")

__all__ = [
    "NodeProvisionError",
    "VaultLockedError",
    "NodeExistsError",
    "NodeNotFoundError",
    "provision_node",
    "rotate_key",
    "deprovision_node",
    "list_nodes",
    "is_provisioned",
    "open_node_link",
    "persist_rx_state",
    "current_vault",
]


class NodeProvisionError(Exception):
    """Base class for provisioning failures."""


class VaultLockedError(NodeProvisionError):
    """The gate-keyed vault is locked or not set up — no key material is reachable (fail closed)."""


class NodeExistsError(NodeProvisionError):
    """Refusing to overwrite an existing node key (would orphan its nonce state / break the peer)."""


class NodeNotFoundError(NodeProvisionError):
    """No provisioned key for this node id."""


# ── vault plumbing ───────────────────────────────────────────────────
def current_vault() -> Any:
    """Return the currently-unlocked gate vault, or raise :class:`VaultLockedError`.

    Imported lazily so this module (and its tests) don't drag in the whole access-gate/Qt stack.
    """
    from src.security import access_gate

    v = access_gate.get_current_vault()
    if v is None:
        raise VaultLockedError("access gate is locked or not provisioned; unlock it before node ops")
    return v


def _table(vault: Any) -> dict:
    if vault is None:
        raise VaultLockedError("no vault provided (gate locked?)")
    return dict(vault.get(_VAULT_NS, {}) or {})


def _write_table(vault: Any, table: dict) -> None:
    vault.set(_VAULT_NS, table)


def _nid(node_id: int) -> str:
    if isinstance(node_id, bool) or not isinstance(node_id, int) or not (0 <= node_id <= _MAX_U16):
        raise ValueError(f"node_id must be an int in 0..{_MAX_U16}")
    return str(node_id)


def _redact(node_id: str, rec: dict) -> dict:
    """A key-free summary safe to log / return to the UI."""
    return {
        "node_id": int(node_id),
        "role": rec.get("role"),
        "label": rec.get("label", ""),
        "tx_epoch": rec.get("tx_epoch", 0),
        "rx_epoch": rec.get("rx_epoch"),
    }


# ── provisioning API ─────────────────────────────────────────────────
def provision_node(
    vault: Any,
    node_id: int,
    *,
    role: str = "host",
    label: str = "",
    key: Optional[bytes] = None,
    overwrite: bool = False,
) -> dict:
    """Mint (or import) a per-node key and store it in the vault. Returns a KEY-FREE summary.

    A fresh key starts at ``(tx_epoch 0, tx_counter 0)`` — safe because the key has never been used.
    Refuses to clobber an existing node unless ``overwrite=True`` (silently replacing a key would strand
    the peer and the nonce cursors). Pass ``key`` only to import a known 32-byte key; otherwise it is
    minted with :func:`os.urandom`.
    """
    nid = _nid(node_id)
    if role not in _ROLES:
        raise ValueError(f"role must be one of {_ROLES}")
    if key is None:
        key = os.urandom(KEY_LEN)
    elif not isinstance(key, (bytes, bytearray)) or len(key) != KEY_LEN:
        raise ValueError(f"key must be exactly {KEY_LEN} bytes")

    key_hex = bytes(key).hex()
    with _reservation_lock():
        table = _table(vault)
        existing = table.get(nid)
        if existing is not None and not overwrite:
            raise NodeExistsError(f"node {nid} already provisioned; pass overwrite=True or rotate_key()")
        # A brand-new key is a brand-new nonce space, so cursors start at 0. But overwrite with the
        # SAME key is NOT new: zeroing tx_epoch/tx_counter rewinds the nonce into already-used
        # (key, nonce) pairs -> catastrophic AES-GCM reuse. So when the key is unchanged, PRESERVE
        # the existing cursors (a role/label update); reset only on a genuine key change.
        keep = existing is not None and existing.get("key") == key_hex
        table[nid] = {
            "key": key_hex,
            "role": role,
            "label": str(label),
            "tx_epoch": int(existing.get("tx_epoch", 0)) if keep else 0,
            "tx_counter": int(existing.get("tx_counter", 0)) if keep else 0,
            "rx_epoch": existing.get("rx_epoch") if keep else None,
            "rx_highest": int(existing.get("rx_highest", -1)) if keep else -1,
        }
        _write_table(vault, table)
    log.info("provisioned node %s (role=%s)", nid, role)  # NB: no key material logged
    return _redact(nid, table[nid])


def rotate_key(vault: Any, node_id: int) -> dict:
    """Replace a node's key with a fresh one and reset the nonce cursors (new key = new nonce space)."""
    nid = _nid(node_id)
    with _reservation_lock():
        table = _table(vault)
        rec = table.get(nid)
        if rec is None:
            raise NodeNotFoundError(f"node {nid} is not provisioned")
        rec["key"] = os.urandom(KEY_LEN).hex()
        rec["tx_epoch"] = 0
        rec["tx_counter"] = 0
        rec["rx_epoch"] = None
        rec["rx_highest"] = -1
        _write_table(vault, table)
    log.info("rotated key for node %s", nid)
    return _redact(nid, rec)


def deprovision_node(vault: Any, node_id: int) -> bool:
    """Remove a node's key + state. Returns True if something was removed."""
    nid = _nid(node_id)
    with _reservation_lock():
        table = _table(vault)
        if nid not in table:
            return False
        del table[nid]
        _write_table(vault, table)
    log.info("deprovisioned node %s", nid)
    return True


def list_nodes(vault: Any) -> list[dict]:
    """Key-FREE summaries of every provisioned node (safe for the UI / logs)."""
    table = _table(vault)
    return [_redact(nid, rec) for nid, rec in sorted(table.items(), key=lambda kv: int(kv[0]))]


def is_provisioned(vault: Any, node_id: int) -> bool:
    return _nid(node_id) in _table(vault)


# ── link construction with crash-safe epoch reservation ──────────────
def open_node_link(vault: Any, node_id: int, gateway: Any, **link_kw: Any) -> NodeLink:
    """Build a :class:`NodeLink` for a provisioned node, RESERVING an epoch so a crash can't reuse a nonce.

    Reserves the record's current ``tx_epoch`` for this session and persists ``tx_epoch + 1`` *before*
    the link is handed back. ``role``/``epoch``/``counter``/``rx_epoch``/``rx_highest`` come from the
    vault; any other keyword (``window_size``, ``line_ending``, ``port`` …) is forwarded to ``NodeLink``.
    """
    nid = _nid(node_id)
    # ATOMIC reserve: read tx_epoch, persist tx_epoch+1, and capture the fields — all under the lock — so
    # two overlapping opens can never hand out the same epoch (which would mean an identical (key, nonce)).
    with _reservation_lock():
        table = _table(vault)
        rec = table.get(nid)
        if rec is None:
            raise NodeNotFoundError(f"node {nid} is not provisioned")
        reserved_epoch = int(rec.get("tx_epoch", 0))
        if reserved_epoch >= _MAX_U32:
            # 4 billion sessions on one key — refuse rather than wrap into a reused epoch.
            raise NonceExhaustedError(f"node {nid} epoch space exhausted; rotate_key() to reset")
        rec["tx_epoch"] = reserved_epoch + 1  # reserved BEFORE the link exists → crash-safe
        rec["tx_counter"] = 0
        _write_table(vault, table)
        key = bytes.fromhex(rec["key"])
        role = rec.get("role", "host")
        rx_epoch = rec.get("rx_epoch")
        rx_highest = int(rec.get("rx_highest", -1))

    # NB: `key` is deliberately NOT "zeroed" afterward — it is an immutable bytes, and NodeLink already
    # holds the HKDF-derived directional keys, so scrubbing one local buys nothing. Real protection is the
    # gate-encrypted vault at rest, not in-process memory hygiene.
    return NodeLink(
        gateway, key, int(nid),
        role=role, epoch=reserved_epoch, counter=0,
        rx_epoch=rx_epoch, rx_highest=rx_highest, **link_kw,
    )


def persist_rx_state(vault: Any, node_id: int, link: NodeLink) -> None:
    """Save a link's receiver window head (``rx_epoch``/``rx_highest``) so anti-replay survives a restart.

    Call on teardown (or periodically). The sender epoch is already reserved at open, so tx state does not
    need per-frame persistence.
    """
    nid = _nid(node_id)
    with _reservation_lock():
        table = _table(vault)
        rec = table.get(nid)
        if rec is None:
            raise NodeNotFoundError(f"node {nid} is not provisioned")
        rec["rx_epoch"] = link.rx_epoch
        rec["rx_highest"] = int(link.rx_highest)
        _write_table(vault, table)
