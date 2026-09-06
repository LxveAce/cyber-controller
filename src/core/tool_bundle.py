r"""Bundled crack-tool packs — the "it comes with it" storage layer.

CC ships the crack tools (aircrack-ng, ...) as AES-encrypted ``.pack`` files in ``src/config/tools/``
because Windows Defender flags the raw binaries as PUA and DELETES them on sight (see
``scripts/build_tool_packs.py`` for the why + provenance). An encrypted archive can't be scanned
inside, so the pack survives at rest in the repo / clone / built app.

This module only LISTS the packs and EXTRACTS one into a destination directory the caller has already
prepared. It never touches antivirus and never extracts on its own — the opt-in, disclaimer, and the
one-time Defender exclusion (so the extracted binaries aren't re-quarantined) live in the UI + a
platform helper. Extraction verifies every file against the manifest's SHA-256, fail-closed.

The pack password is intentionally NOT secret (it lives here in the open): its only purpose is to stop
AV from false-positive-deleting a legitimate, standard FOSS tool before the user has consented — not
to hide anything. Anyone can regenerate the packs with ``scripts/build_tool_packs.py``.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Optional

from .resources import resource_path

Line = Callable[[str], None]
ShouldCancel = Callable[[], bool]
#: Called ONCE at the transaction's point of no return — right before the first publish mutation, after
#: staging/verification and the last cancel check. May raise (e.g. a pending cancel) to abort fail-closed
#: before anything irreversible; the prior install is then left intact.
BeginCommit = Callable[[], None]

#: Public by design — see the module docstring + scripts/build_tool_packs.py.
PACK_PASSWORD = b"cyber-controller-tools"


class ToolCancelled(RuntimeError):
    """Raised when an enable/extract was cancelled cooperatively BEFORE the atomic publish."""


# One lock per destination tool dir so two concurrent enables of one tool can't interleave their
# stage/promote (one writer per destination). Keyed by the realpath of the final dir.
_dest_locks: dict[str, threading.Lock] = {}
_dest_locks_guard = threading.Lock()


def canonical_dest(dest_dir: str) -> str:
    """A platform-appropriate canonical key for a destination dir, shared by the transaction and (later)
    the job layer so they agree on identity. ``os.path.normcase`` folds case on Windows (where
    ``tools/aircrack-ng`` and ``tools/AIRCRACK-NG`` are the SAME dir, but ``realpath`` alone doesn't
    normalize the case of a not-yet-existing trailing segment — T4) and is identity on POSIX (where they
    are distinct). NOTE: these locks are in-process only, not cross-process serialization."""
    return os.path.normcase(os.path.realpath(dest_dir))


def _lock_for_dest(dest_dir: str) -> threading.Lock:
    key = canonical_dest(dest_dir)
    with _dest_locks_guard:
        lock = _dest_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _dest_locks[key] = lock
        return lock


# ── destination admission: ONE process-wide reservation per canonical destination ─────────────────────
# The per-dest transaction lock above only serializes overlapping extract_pack windows; it does NOT stop a
# synchronous enable from publishing while an async job merely holds a reservation (they take different
# locks). This admission gate is the shared arbitration EVERY mutation entry point (sync enable, sync
# install, the async job worker, and the Qt _ToolEnableWorker) holds for the WHOLE operation. A held
# reservation makes a competing acquire raise DestinationBusy; the route layer maps that to 409.

class DestinationBusy(RuntimeError):
    """Raised when a destination is already reserved, or a supplied lease is not the active reservation."""


@dataclass(frozen=True)
class _Lease:
    """An opaque, operation-specific reservation of one canonical destination, minted only by
    :func:`acquire_destination`. NOT the authenticated job owner — purely internal.

    ``dest`` is the CAPTURED canonical map key (``normcase(realpath)``); every later map operation uses this
    stored key, never a re-resolution of the visible path. ``path`` is the CAPTURED resolved real path
    (``realpath``): the operation mutates, probes and cleans up THIS pinned path, not the caller's visible
    spelling (A2), so replacing a junction mid-operation can't leave the reservation on one directory while
    the bytes land on another. ``token`` + ``epoch`` identify this exact reservation."""
    dest: str
    path: str
    token: str
    epoch: int


_admissions: dict[str, _Lease] = {}       # captured key -> active lease
_borrows: dict[str, int] = {}             # captured key -> live borrow count
_pending_release: set[str] = set()        # keys whose owner released while borrowers were still active
_admissions_guard = threading.Lock()
_admission_epoch = 0


def _minted_lease(lease: object) -> bool:
    """A4: True only for a genuinely minted lease with EXACT built-in field types. Checked before any
    protected lookup or comparison, so a forged ``_Lease`` carrying equality/hash-overriding dest/token/epoch
    objects can't release or borrow a real reservation (and no foreign ``__eq__``/``__hash__`` runs under the
    map lock). Identity (``held is lease``) is the actual authority; this guards the dict-key + type surface."""
    return (type(lease) is _Lease and type(lease.dest) is str and type(lease.path) is str
            and type(lease.token) is str and type(lease.epoch) is int)


def acquire_destination(dest_dir: str) -> _Lease:
    """Reserve *dest_dir* for one operation, capturing both its resolved real path (for the operation) and
    its canonical map key (for the reservation). Raises :class:`DestinationBusy` if a different operation
    already holds it. Only dict work happens under the guard (never I/O/callbacks)."""
    global _admission_epoch
    resolved = os.path.realpath(dest_dir)
    key = os.path.normcase(resolved)
    with _admissions_guard:
        if key in _admissions:
            raise DestinationBusy(f"another install is already using {dest_dir}")
        _admission_epoch += 1
        lease = _Lease(key, resolved, uuid.uuid4().hex, _admission_epoch)
        _admissions[key] = lease
        return lease


def release_destination(lease: _Lease) -> bool:
    """Release *lease*'s reservation, keyed by the lease's OWN captured ``dest`` (A2 — never re-resolves the
    visible path). Removes it only when the currently held lease IS this exact object (A4 identity), so a
    stale/foreign/forged release is a no-op. If borrowers are still active the removal is DEFERRED until the
    last borrow exits (A3), so cancellation/finalizer cleanup can't clear admission out from under a running
    borrower. Total (never raises — it runs in ``finally`` blocks): a forged/foreign/stale lease is a no-op
    returning False. Returns whether it removed the reservation now."""
    if not _minted_lease(lease):
        return False
    with _admissions_guard:
        if _admissions.get(lease.dest) is not lease:
            return False
        if _borrows.get(lease.dest, 0) > 0:
            _pending_release.add(lease.dest)
            return False
        del _admissions[lease.dest]
        _pending_release.discard(lease.dest)
        return True


@contextmanager
def borrow_destination(lease: _Lease):
    """Scoped borrow of an owner's active reservation (A3): a nested public entry point runs under the
    owner's lease without acquiring twice, and NEVER releases it. Raises :class:`DestinationBusy` if *lease*
    is not the current active reservation. While any borrow is live the owner's release is deferred."""
    if not _minted_lease(lease):
        raise DestinationBusy("invalid destination lease")
    with _admissions_guard:
        if _admissions.get(lease.dest) is not lease:
            raise DestinationBusy("borrowed reservation is not the active one")
        _borrows[lease.dest] = _borrows.get(lease.dest, 0) + 1
    try:
        yield lease
    finally:
        with _admissions_guard:
            remaining = _borrows.get(lease.dest, 0) - 1
            if remaining > 0:
                _borrows[lease.dest] = remaining
            else:
                _borrows.pop(lease.dest, None)
                # honor an owner release that was deferred while this borrow was live
                if lease.dest in _pending_release and _admissions.get(lease.dest) is lease:
                    del _admissions[lease.dest]
                _pending_release.discard(lease.dest)


@contextmanager
def _admission(dest_dir: str, lease: Optional[_Lease]):
    """Hold the destination admission for a public backend entry point, yielding the active lease. Acquire +
    release our own reservation when *lease* is None, else borrow the caller's (never releasing it).

    A5: on the borrow path the requested *dest_dir* is BOUND to the lease — the lease must be for exactly this
    canonical destination, else :class:`DestinationBusy`. This stops a genuine lease for A from being used to
    mutate a different destination B (which may have its own owner). Callers must operate on the yielded
    lease's ``.path`` (the pinned resolved destination), never their own visible spelling."""
    if lease is None:
        own = acquire_destination(dest_dir)
        try:
            yield own
        finally:
            release_destination(own)
    else:
        if not _minted_lease(lease) or os.path.normcase(os.path.realpath(dest_dir)) != lease.dest:
            raise DestinationBusy("lease does not match the requested destination")
        with borrow_destination(lease) as borrowed:
            yield borrowed


def packs_dir() -> str:
    """Where the shipped ``.pack`` / ``.manifest.json`` files live (dev + frozen, via resource_path)."""
    return str(resource_path("src", "config", "tools"))


@dataclass(frozen=True)
class ToolPack:
    """One bundled tool pack + its manifest (provenance + per-file hashes)."""

    name: str
    tool: str
    version: str
    platform: str
    primary_exe: str
    pack_path: str
    manifest: dict


def list_packs() -> list[ToolPack]:
    """Every bundled pack that has both a ``.manifest.json`` and its ``.pack`` on disk."""
    directory = packs_dir()
    out: list[ToolPack] = []
    if not os.path.isdir(directory):
        return out
    for fn in sorted(os.listdir(directory)):
        if not fn.endswith(".manifest.json"):
            continue
        try:
            with open(os.path.join(directory, fn), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        pack_path = os.path.join(directory, str(m.get("name", "")) + ".pack")
        if os.path.isfile(pack_path):
            out.append(ToolPack(
                name=str(m.get("name", "")), tool=str(m.get("tool", "")),
                version=str(m.get("version", "")), platform=str(m.get("platform", "")),
                primary_exe=str(m.get("primary_exe", "")), pack_path=pack_path, manifest=m))
    return out


def pack_for_tool(tool: str, platform_key: str) -> Optional[ToolPack]:
    """The bundled pack for *tool* on *platform_key* (e.g. ``"aircrack-ng"``, ``"windows"``), or None."""
    return next((p for p in list_packs() if p.tool == tool and p.platform == platform_key), None)


def _extract_verified(pack: ToolPack, into_dir: str, log: Line,
                      should_cancel: Optional[ShouldCancel]) -> None:
    """Decrypt + verify EVERY member of *pack* into *into_dir* (a fresh, owned staging dir).
    Fail-closed: raises on an unlisted or SHA-mismatched member (the public pack password makes the
    manifest SHA-256 the ONLY integrity control), a zip-slip path, an INCOMPLETE archive (a manifest
    member missing), or cancellation. Each member is written to a ``.part`` sibling then renamed, so
    staging leaves no partial final-named file. Requires ``pyzipper`` (a CC dep)."""
    import pyzipper  # local import: only needed when actually extracting (keeps import graph light)

    want = {f["name"]: f["sha256"] for f in pack.manifest.get("files", []) if "sha256" in f}
    seen: set[str] = set()
    root = os.path.realpath(into_dir)
    with pyzipper.AESZipFile(pack.pack_path) as z:
        z.setpassword(PACK_PASSWORD)
        for name in z.namelist():
            if name.endswith("/"):
                continue  # directory entry — nothing to write or verify
            if should_cancel and should_cancel():
                raise ToolCancelled("cancelled before install completed")
            blob = z.read(name)
            exp = want.get(name)
            if exp is None:
                raise RuntimeError(f"{name}: not listed in the manifest — refusing to install")
            if hashlib.sha256(blob).hexdigest() != exp:
                raise RuntimeError(f"{name}: SHA-256 mismatch on extract — refusing to install")
            out_path = os.path.join(into_dir, name)
            if not os.path.realpath(out_path).startswith(root + os.sep):
                raise RuntimeError(f"unsafe pack member path: {name!r}")
            os.makedirs(os.path.dirname(out_path) or into_dir, exist_ok=True)  # hashcat: kernels/
            tmp = out_path + ".part"
            with open(tmp, "wb") as out:
                out.write(blob)
            os.replace(tmp, out_path)   # atomic in staging: no partial final-named file on a tear
            seen.add(name)
            log(f"[tools] extracted {name}")
    missing = sorted(set(want) - seen)
    if missing:
        raise RuntimeError(f"pack incomplete — manifest member(s) missing: {missing[:3]}")


def extract_pack(pack: ToolPack, dest_dir: str, on_line: Optional[Line] = None, *,
                 should_cancel: Optional[ShouldCancel] = None,
                 begin_commit: Optional[BeginCommit] = None,
                 lease: Optional[_Lease] = None) -> str:
    """Transactionally publish *pack* at *dest_dir* and return the primary-exe path (public entry point).

    Holds the shared destination admission for the whole call: acquires its own reservation, or — when
    *lease* is passed — validates and BORROWS a reservation an outer operation already owns (a borrow never
    releases the owner's lease). Raises :class:`DestinationBusy` if the destination is already reserved by a
    different operation. The transaction itself is :func:`_extract_pack_transaction`, run against the lease's
    pinned resolved path (A2)."""
    with _admission(dest_dir, lease) as active:
        return _extract_pack_transaction(pack, active.path, on_line,
                                         should_cancel=should_cancel, begin_commit=begin_commit)


def _extract_pack_transaction(pack: ToolPack, dest_dir: str, on_line: Optional[Line] = None, *,
                              should_cancel: Optional[ShouldCancel] = None,
                              begin_commit: Optional[BeginCommit] = None) -> str:
    """The staged + atomically-promoted publish, WITHOUT admission (the caller holds the reservation).

    Decrypt + verify EVERY manifest member and confirm the primary exe into a UNIQUE staging dir — a
    sibling of *dest_dir* (same filesystem, for an atomic rename) that is NOT a resolver search path, so
    a partial/failed extraction is never discoverable by ``installed_tools``/``detect_tools`` — then
    ATOMICALLY swap it in. A prior install at *dest_dir* is preserved until the swap commits and RESTORED
    if promotion fails; only this call's own staging is cleaned up. Concurrent calls to the same
    *dest_dir* are serialized (one writer per destination).

    Fail-closed (leaving *dest_dir*'s prior contents intact) on any bad/unlisted member, an incomplete
    archive, a zip-slip path, an I/O error, or cancellation before the swap. The caller MUST have already
    Defender-excluded the tools tree (else the PUA binaries return). Needs pyzipper."""
    log: Line = on_line or (lambda *_a: None)
    dest_dir = os.path.abspath(dest_dir)
    parent = os.path.dirname(dest_dir) or "."
    os.makedirs(parent, exist_ok=True)
    with _lock_for_dest(dest_dir):
        # staging_root holds the new tree; the prior install's backup lives in its OWN root (NOT
        # staging_root), so the finally that cleans staging can never delete the last surviving copy
        # good install. Both are nested so their exes sit two levels below the tools dir — deeper
        # installed_tools()'s one-level scan, hence invisible — while still inside the
        # on Windows) tree so the PUA binary isn't quarantined mid-stage.
        staging_root = os.path.join(parent, ".cc-stage-" + uuid.uuid4().hex)
        backup_root = os.path.join(parent, ".cc-backup-" + uuid.uuid4().hex)
        pkg = os.path.join(staging_root, "pkg")
        backup = os.path.join(backup_root, "old")
        # T5: the backup is retained BY DEFAULT and deleted only once its replacement is verifiably
        # published or the prior tree has been restored — never merely because a particular exception
        # type was caught. An interruption (KeyboardInterrupt/SystemExit) unwinds through the try
        # WITHOUT going through `except OSError`, so a caught-exception gate would let the finally
        # delete the only surviving prior copy. Completion flags, not a caught type, drive the cleanup.
        had_prev = False
        published = False
        restored = False
        try:
            os.makedirs(pkg, exist_ok=True)
            _extract_verified(pack, pkg, log, should_cancel)
            if not os.path.isfile(os.path.join(pkg, pack.primary_exe)):
                raise RuntimeError(f"pack primary exe {pack.primary_exe!r} missing after extract")
            if should_cancel and should_cancel():
                raise ToolCancelled("cancelled before install completed")
            # Point of no return: staging + verification are done and this is the LAST cancel-safe moment.
            # begin_commit() lets a job registry atomically refuse a late cancel (or raise to abort). If it
            # raises, we unwind here — before any publish mutation — so the prior install stays intact.
            if begin_commit is not None:
                begin_commit()
            # Publish: move any prior install aside, move the verified tree in. On a failed move-in,
            # restore the prior install; if the RESTORE also fails (a persistent destination error —
            # a sharing violation, or another process recreating the dir — can hit both), the backup
            # is the only surviving copy, so report BOTH errors. It is retained by the default policy.
            # Two renames are not one atomic exchange: a crash between them leaves the prior install
            # recoverable at backup_root/old.
            had_prev = os.path.exists(dest_dir)
            if had_prev:
                os.makedirs(backup_root, exist_ok=True)
                os.replace(dest_dir, backup)
            try:
                os.replace(pkg, dest_dir)
                published = True   # set IMMEDIATELY after the successful rename, before anything else
            except OSError as promote_err:
                if had_prev:
                    try:
                        os.replace(backup, dest_dir)   # restore the previous install
                        restored = True
                    except OSError as restore_err:
                        raise RuntimeError(
                            f"install failed and the previous {pack.tool} install could not be restored "
                            f"automatically; a recovered copy is preserved at {backup} "
                            f"(promote: {promote_err}; restore: {restore_err})") from restore_err
                raise
            # B2: the publish is done and irreversible. A failing progress OBSERVER after this point must
            # NOT propagate — a caught observer exception would relabel a COMPLETED install as failed/cancelled
            # and invite the UI to retry an already-published operation. Swallow ordinary observer exceptions
            # here (never emitting their text); KeyboardInterrupt/SystemExit still propagate. The real
            # verification/probe outcome is judged by the caller, not by whether this notification succeeded.
            try:
                log(f"[tools] {pack.tool} {pack.version} published to {dest_dir}")
            except Exception:  # noqa: BLE001 — a post-publish notification failure can't undo the publish
                pass
            return os.path.join(dest_dir, pack.primary_exe)
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)   # clean ONLY this job's owned staging
            # Delete the backup only when it is provably safe: the replacement is published (the old
            # is superseded) or the old was restored (the backup was consumed by that rename). Any
            # other unwind — a double-I/O error, or an interruption between move-aside and publish —
            # RETAINS it so the only prior copy is never lost; keeping an extra backup is the
            # acceptable failure mode.
            if had_prev and (published or restored):
                shutil.rmtree(backup_root, ignore_errors=True)


def enable_dir() -> str:
    """The folder bundled tools are extracted into — the one the user Defender-excludes. Parallel to the
    resolver's tools dir so :func:`crack_pipeline.detect_tools` finds the enabled tools afterward."""
    from .tool_installer import default_tools_dir
    return default_tools_dir()


@dataclass(frozen=True)
class EnableOutcome:
    """Typed result of a bundled enable so cancel / failure / success are DISTINGUISHABLE (the legacy
    ``(ok, message)`` tuple flattened all three, which an exception-only job wrapper can't tell apart).

    ``status`` is exactly one of ``"succeeded"`` / ``"cancelled"`` / ``"failed"``. ``exe`` and
    ``verification_method`` are set only on success (``verification_method`` names the integrity check that
    actually ran — bundled packs verify every member by manifest SHA-256, so ``"sha256"``)."""
    status: str
    message: str
    exe: Optional[str] = None
    verification_method: Optional[str] = None


def enable_bundled_result(pack: ToolPack, on_line: Optional[Line] = None, *,
                          should_cancel: Optional[ShouldCancel] = None,
                          begin_commit: Optional[BeginCommit] = None,
                          lease: Optional[_Lease] = None) -> EnableOutcome:
    """Transactionally publish *pack* into ``enable_dir()/<tool>/`` and return a typed :class:`EnableOutcome`.

    Same transaction as :func:`extract_pack` (staged + atomically promoted; a failed/cancelled enable leaves
    any prior install intact and exposes no partial tree). ``begin_commit`` is threaded to the publish
    boundary. A pre-commit cancellation is reported as ``cancelled``; any extraction error or a Defender
    block is ``failed`` — never a fake success.

    Holds the destination admission across the WHOLE operation, including the post-install probe: acquires
    its own reservation, or BORROWS *lease* when an outer operation (the async job) already owns it. Raises
    :class:`DestinationBusy` if the destination is reserved by another operation. The caller MUST have
    already Defender-excluded :func:`enable_dir`."""
    from . import defender
    log: Line = on_line or (lambda *_a: None)
    dest = os.path.join(enable_dir(), pack.tool)
    # Hold admission (own or borrowed) across BOTH the transaction and the post-install probe, and operate on
    # the lease's pinned resolved path (A2). The inner extract_pack borrows this same lease (A5-bound to that
    # path) so it doesn't acquire twice.
    with _admission(dest, lease) as active:
        try:
            exe = extract_pack(pack, active.path, log, should_cancel=should_cancel,
                               begin_commit=begin_commit, lease=active)
        except ToolCancelled as exc:
            return EnableOutcome("cancelled", str(exc))
        except Exception as exc:  # noqa: BLE001
            return EnableOutcome("failed", f"extract failed: {exc}")
        if not os.path.isfile(exe):
            return EnableOutcome("failed", "extracted, but the tool binary is missing — Windows Defender "
                                 "likely quarantined it. Add the exclusion (see the notice) for this folder "
                                 "and try again.")
        if defender.is_windows() and not defender.exe_runs(exe):
            return EnableOutcome("failed", "extracted, but the tool won't launch — Defender is still blocking "
                                 "it. Make sure the exclusion covers this folder, then try again.")
        return EnableOutcome("succeeded", f"{pack.tool} enabled: {exe}", exe=exe, verification_method="sha256")


def enable_bundled(pack: ToolPack, on_line: Optional[Line] = None, *,
                   should_cancel: Optional[ShouldCancel] = None) -> tuple[bool, str]:
    """Back-compat ``(ok, message)`` wrapper over :func:`enable_bundled_result` for the synchronous callers.

    Extraction is staged + atomically promoted (see :func:`extract_pack`): a failed or cancelled
    enable leaves any prior install untouched and no partial tree becomes discoverable. The caller
    MUST have already Defender-excluded :func:`enable_dir` (else the extracted PUA binary returns).
    Returns (ok, message) — a cancel or Defender block is reported honestly, never a fake success."""
    outcome = enable_bundled_result(pack, on_line, should_cancel=should_cancel)
    return (outcome.status == "succeeded", outcome.message)
