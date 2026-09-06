"""BLE sightings/report journal — bounded, durable M1 core/store. Pure core: no HTTP/UI, no device I/O, no
ingestor/hub edits, no default-path resolution. A later adapter candidate wires this to the ingestor seam.

Storage contract:

* One typed, allowlisted fact per row — both explicit-address sightings (``ble_found``) and addressless reports
  (``ble_observation``), never a raw event/Target, never raw serial lines or secrets. Null RSSI stays distinct
  from an explicit 0.
* Admission, durability and loss are three different things. ``submit`` allocates identity + enqueues and
  returns an admission result only; it never blocks on disk. A single owning writer thread appends a COMPLETE
  line per ``fsync``; a short/partial write is never acknowledged. ``flush`` reports durable progress.
* Segments have stable, never-reused identities (a monotonic ordering + a fresh random id) in the filename; the
  cursor binds to that identity. A fresh segment is created each writer lifetime; prior segments are immutable
  input (never appended behind a torn tail). Capacity is reserved BEFORE creating any segment (startup and
  rotation); a delete that fails does not authorize a new segment. Ordering exhaustion and an incomplete
  ownership catalogue both fail visibly.
* Exactly one owner holds an OS lock on the store before any enumeration/recovery/deletion/creation, and keeps
  it until the writer has actually exited — including thread-start uncertainty and a timed-out close, which
  return an honest retryable/unresolved handle rather than releasing while a worker may be alive.
* Reads never cross the acknowledged (confirmed-fsync) byte boundary of the active segment, respect their byte
  and scan budgets on every row (including the first), only return record-boundary cursors, and never promote a
  corrupt line's suffix to a record. Memory reads return detached row copies with stable (run_id, seq) cursors.
* ``persist_path=None`` performs NO filesystem activity and keeps a bounded in-memory history only.

Durability is scoped to the tested local Windows/POSIX file contract: a confirmed record is a COMPLETE line
followed by a successful ``fsync``. Directory-metadata power-loss survival, POSIX/ACL and network-filesystem
behaviour are not claimed.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

_MAX_ORDERING = 1_000_000_000    # reject an exhausted ordering rather than wrap/reuse an identity


@dataclass(frozen=True)
class JournalLimits:
    max_row_bytes: int = 8 * 1024
    max_queue_rows: int = 1024
    max_queue_bytes: int = 8 * 1024 * 1024
    max_segment_bytes: int = 16 * 1024 * 1024
    max_data_segments: int = 4
    max_label_bytes: int = 512
    max_meta_keys: int = 32
    max_meta_value_bytes: int = 256
    max_read_rows: int = 256
    max_read_bytes: int = 256 * 1024
    max_scan_bytes: int = 4 * 1024 * 1024
    max_mem_rows: int = 4096
    max_dir_entries: int = 4096

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"journal limit {name!r} must be a positive int (got {value!r})")
        if self.max_row_bytes > self.max_segment_bytes:
            raise ValueError("max_row_bytes cannot exceed max_segment_bytes")
        if self.max_row_bytes > self.max_queue_bytes:
            raise ValueError("max_row_bytes cannot exceed max_queue_bytes")


class JournalError(Exception):
    """A journal contract failure."""


class JournalUnavailable(JournalError):
    """Persistence could not be owned/created (lock unavailable, quota unreclaimable, ordering exhausted, or an
    incomplete ownership catalogue) or the store is degraded and must be reopened."""


class JournalStateError(JournalError):
    """An operation was called in the wrong lifecycle state."""


_SCHEMA_VERSION = 1
_KINDS = ("ble_found", "ble_observation")
_ADDR_TYPES = (None, "public", "random")


@dataclass(frozen=True)
class Cursor:
    """A read position bound to a segment's STABLE identity (ordering + id). Normally at a record boundary; when
    ``discarding`` is set the offset is mid-way through an over-long corrupt line that a bounded read is still
    skipping to its next newline (so its suffix is never promoted to a record)."""
    ordering: int
    seg_id: str
    offset: int
    discarding: bool = False

    def token(self) -> str:
        return f"{self.ordering}:{self.seg_id}:{self.offset}:{int(self.discarding)}"

    @classmethod
    def parse(cls, token: str) -> "Cursor":
        try:
            parts = token.split(":")
            if len(parts) == 3:
                ordering, seg_id, offset = parts
                return cls(int(ordering), seg_id, int(offset))
            ordering, seg_id, offset, discarding = parts
            return cls(int(ordering), seg_id, int(offset), bool(int(discarding)))
        except (ValueError, AttributeError) as exc:
            raise JournalError(f"invalid cursor token {token!r}") from exc


@dataclass
class ReadPage:
    rows: list = field(default_factory=list)
    next_cursor: Optional[Cursor] = None
    expired: bool = False
    earliest_ordering: Optional[int] = None
    earliest_seq: Optional[int] = None      # memory mode: earliest retained (run_id, seq)
    scanned_bytes: int = 0
    budget_too_small: bool = False          # a page byte budget smaller than the next row (explicit outcome)


@dataclass
class FlushResult:
    confirmed_seq: int
    undrained: int
    degraded: bool


@dataclass
class CloseResult:
    confirmed_seq: int
    undrained: int
    degraded: bool
    lock_released: bool                     # False => honest unresolved/retryable cleanup handle
    resolved: bool                          # False => still closing (worker alive); retry close()


# ── Fact allowlist / bounded serialization ─────────────────────────────────────────────────────────

def _bounded_str(value: Any, limit: int) -> Optional[str]:
    if type(value) is not str:              # strict: no str subclasses masquerading
        return None
    try:
        encoded = value.encode("utf-8")     # a lone surrogate / non-encodable primitive is invalid, not fatal
    except UnicodeEncodeError:
        return None
    if len(encoded) > limit:
        return None
    return value


def _bounded_meta(value: Any, limits: JournalLimits) -> Optional[dict]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > limits.max_meta_keys:
        return None
    out: dict[str, Any] = {}
    for key, item in value.items():
        if _bounded_str(key, limits.max_meta_value_bytes) is None:   # type + encoding + length bounded
            return None
        if item is None or type(item) is bool or type(item) is int:
            out[key] = item
        elif type(item) is str:
            b = _bounded_str(item, limits.max_meta_value_bytes)
            if b is None:
                return None
            out[key] = b
        else:
            return None                     # never nested/arbitrary values
    return out


def build_row(run_id: str, seq: int, observed_at: str, fact: Mapping[str, Any], limits: JournalLimits):
    """Allowlist a caller fact into a bounded typed row: ``(row_dict, serialized_line)`` or ``None``. The
    caller's original object is never retained on rejection; raw serial lines / secrets are excluded by field
    allowlisting, not escaping."""
    if not isinstance(fact, Mapping):
        return None
    kind = fact.get("kind")
    if kind not in _KINDS:
        return None
    source = fact.get("source")
    if not isinstance(source, Mapping):
        return None
    port = _bounded_str(source.get("port", ""), limits.max_meta_value_bytes)
    firmware = _bounded_str(source.get("firmware", ""), limits.max_meta_value_bytes)
    connection_id = _bounded_str(source.get("connection_id", ""), limits.max_meta_value_bytes)
    if port is None or firmware is None or connection_id is None:
        return None
    address = fact.get("address")
    if address is not None:
        address = _bounded_str(address, limits.max_meta_value_bytes)
        if address is None:
            return None
    address_type = fact.get("address_type")
    if address_type not in _ADDR_TYPES:
        return None
    label = _bounded_str(fact.get("label", ""), limits.max_label_bytes)
    if label is None:
        return None
    rssi = fact.get("rssi", None)
    if rssi is not None and (type(rssi) is not int or not -128 <= rssi <= 127):
        return None                         # strict int (rejects bool and int subclasses); null stays != 0
    report_meta = _bounded_meta(fact.get("report_meta"), limits)
    meta = _bounded_meta(fact.get("meta"), limits)
    if report_meta is None or meta is None:
        return None
    row = {
        "schema_version": _SCHEMA_VERSION, "run_id": run_id, "seq": seq, "observed_at": observed_at,
        "kind": kind, "source": {"port": port, "firmware": firmware, "connection_id": connection_id},
        "address": address, "address_type": address_type, "addressable": address is not None,
        "label": label, "rssi": rssi, "report_meta": report_meta, "meta": meta,
    }
    try:
        line = json.dumps(row, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    if len(line.encode("utf-8")) + 1 > limits.max_row_bytes:
        return None
    return row, line


# ── Portable exclusive store lock ────────────────────────────────────────────────────────────────────

class _StoreLock:
    """An OS advisory lock on a stable ``ble-journal.lock`` file, held for the owner's whole lifetime. Windows
    uses ``msvcrt.locking``; POSIX uses ``fcntl.flock``. The file is never unlinked on release, so a contender
    always locks the SAME file. Network-filesystem locking is not assumed reliable (not exercised here)."""

    def __init__(self, path: str):
        self._path = path
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise JournalUnavailable(f"journal store is already owned: {self._path}") from exc
        self._fd = fd

    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> bool:
        fd = self._fd
        if fd is None:
            return True
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            self._fd = None
            return True
        except OSError:
            return False                     # honest: ownership not cleanly resolved


_COUNTER_NAMES = (
    "admitted", "invalid", "queue_full", "confirmed", "uncertain", "degraded_rejected",
    "closing_rejected", "retention_deleted", "retention_failed", "undrained_at_close", "recovery_skipped",
)

_SEG_PREFIX = "ble-"
_SEG_SUFFIX = ".jsonl"
_LOCK_NAME = "ble-journal.lock"
_O_BINARY = getattr(os, "O_BINARY", 0)   # Windows text mode would translate \n -> \r\n and break byte accounting


def _parse_segment_name(name: str) -> Optional[tuple[int, str]]:
    core = name[len(_SEG_PREFIX):-len(_SEG_SUFFIX)]
    parts = core.split("-", 1)
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    return int(parts[0]), parts[1]


class BleJournal:
    """A single-owner bounded durable BLE fact journal. The OS lock enforces exactly one owner per store."""

    def __init__(self, persist_path: Optional[str] = None, *, limits: Optional[JournalLimits] = None,
                 clock: Optional[Callable[[], float]] = None, run_id: Optional[str] = None):
        self._dir = os.path.abspath(persist_path) if persist_path is not None else None   # canonical aliasing
        self._limits = limits or JournalLimits()
        self._clock = clock
        self._run_id = run_id or secrets.token_hex(8)

        self._state = "new"                  # new -> started -> closing -> closed
        self._admit_lock = threading.Lock()
        self._not_empty = threading.Condition(self._admit_lock)
        self._queue: list[tuple[int, dict, str]] = []
        self._queue_bytes = 0
        self._seq = 0
        self._counts = {name: 0 for name in _COUNTER_NAMES}
        self._confirmed_seq = -1
        self._active_row: Optional[tuple[int, dict, str]] = None
        self._fenced = False
        self._degraded = False
        self._unresolved = False
        self._worker_failed = False
        self._worker_exc: Optional[str] = None

        self._lock: Optional[_StoreLock] = None
        self._active_path: Optional[str] = None
        self._active_ordering: Optional[int] = None
        self._active_seg_id: Optional[str] = None
        self._active_bytes = 0               # acknowledged (confirmed-fsync) boundary of the active segment
        self._writer: Optional[threading.Thread] = None

        self._mem: list[dict] = []
        self._mem_earliest_seq = 1           # earliest retained seq (advances on eviction)

    def _now_iso(self) -> str:
        if self._clock is not None:
            return datetime.fromtimestamp(self._clock(), tz=timezone.utc).isoformat()
        return datetime.now(timezone.utc).isoformat()

    # ── lifecycle ─────────────────────────────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._state != "new":
            raise JournalStateError(f"start() in state {self._state!r}")
        if self._dir is None:
            self._state = "started"
            return
        os.makedirs(self._dir, exist_ok=True)
        lock = _StoreLock(os.path.join(self._dir, _LOCK_NAME))
        lock.acquire()
        self._lock = lock
        # Pre-launch region: recovery, capacity, segment creation AND thread CONSTRUCTION. Any failure here has
        # no running worker, so ownership can be rolled back (retaining a retryable handle if release itself
        # fails). Thread construction is inside this region so a constructor failure also rolls back (BJS-05).
        try:
            ordering = self._recover_and_next_ordering()   # checked catalogue; ordering exhaustion
            self._reserve_capacity(protect_ordering=None)  # reserve BEFORE creating (may delete oldest)
            self._open_new_segment(ordering)
            writer = threading.Thread(target=self._run_writer, name="ble-journal-writer", daemon=True)
        except BaseException:
            self._rollback_prelaunch()
            raise
        self._writer = writer
        self._state = "started"
        # Launch: a Thread.start() failure is post-launch uncertainty. The native thread MAY be bootstrapping,
        # so RETAIN the lock + thread ownership (a second owner stays excluded), mark unresolved, and re-raise
        # the original control. close() returns an honest retryable unresolved handle.
        try:
            writer.start()
        except BaseException:
            self._degraded = True
            self._unresolved = True
            raise

    def _rollback_prelaunch(self) -> None:
        # Set a definite failed state and RETAIN handles BEFORE the risky release, and never raise, so a cleanup
        # failure cannot replace the primary startup exception (BJT-02). A later close() retries the release.
        self._writer = None
        self._fenced = True
        self._unresolved = True
        self._state = "closing"
        try:
            released = self._lock.release() if self._lock is not None else True
        except BaseException:                 # BaseException ONLY here (a control raised by release must not
            return                            # replace the primary startup error); start() re-raises it (BJU-01)
        if released:
            self._lock = None
            self._unresolved = False
            self._state = "closed"

    def _recover_and_next_ordering(self) -> int:
        segs = self._list_segments_checked()            # raises on an incomplete ownership catalogue
        max_ordering = max((o for o, _s, _p in segs), default=0)
        nxt = max_ordering + 1
        if nxt > _MAX_ORDERING:
            raise JournalUnavailable("journal segment ordering is exhausted")
        return nxt

    def _list_segments_checked(self) -> list[tuple[int, str, str]]:
        segs: list[tuple[int, str, str]] = []
        count = 0
        with os.scandir(self._dir) as it:
            for entry in it:
                count += 1
                if count > self._limits.max_dir_entries:
                    raise JournalUnavailable("journal directory catalogue is incomplete (over max_dir_entries)")
                if not (entry.name.startswith(_SEG_PREFIX) and entry.name.endswith(_SEG_SUFFIX)):
                    continue
                parsed = _parse_segment_name(entry.name)
                if parsed is None:
                    self._counts["recovery_skipped"] += 1
                    continue
                segs.append((parsed[0], parsed[1], entry.path))
        segs.sort()
        return segs

    def _reserve_capacity(self, protect_ordering: Optional[int]) -> None:
        # Ensure a new segment fits the data-segment quota. Delete oldest eligible FIRST; a failed delete does
        # NOT authorize creating a new segment.
        while True:
            segs = self._list_segments_checked()
            if len(segs) < self._limits.max_data_segments:
                return
            eligible = [s for s in segs if s[0] != protect_ordering]
            if not eligible:
                raise JournalUnavailable("cannot reserve segment capacity (only the active segment remains)")
            _ordering, _seg_id, path = eligible[0]
            try:
                os.remove(path)
                self._counts["retention_deleted"] += 1
            except OSError as exc:
                self._counts["retention_failed"] += 1
                raise JournalUnavailable("cannot reserve segment capacity: retention delete failed") from exc

    def _open_new_segment(self, ordering: int) -> None:
        if ordering > _MAX_ORDERING:                    # centralised exhaustion check for EVERY creation
            raise JournalUnavailable("journal segment ordering is exhausted")
        seg_id = secrets.token_hex(6)
        name = f"{_SEG_PREFIX}{ordering:09d}-{seg_id}{_SEG_SUFFIX}"
        path = os.path.join(self._dir, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
        os.close(fd)
        self._active_path = path
        self._active_ordering = ordering
        self._active_seg_id = seg_id
        self._active_bytes = 0

    # ── admission ─────────────────────────────────────────────────────────────────────────────────────
    def submit(self, fact: Mapping[str, Any]) -> str:
        if self._state != "started":
            raise JournalStateError(f"submit() in state {self._state!r}")
        observed_at = self._now_iso()
        with self._admit_lock:
            if self._fenced:
                self._counts["closing_rejected"] += 1
                return "rejected:closing"
            if self._degraded:
                self._counts["degraded_rejected"] += 1
                return "rejected:degraded"          # reject new work until a deliberate reopen (BJM-02)
            seq = self._seq + 1
            built = build_row(self._run_id, seq, observed_at, fact, self._limits)
            if built is None:
                self._counts["invalid"] += 1
                return "rejected:invalid"
            row, line = built
            line_bytes = len(line.encode("utf-8")) + 1
            active_bytes = 0 if self._active_row is None else len(self._active_row[2].encode("utf-8")) + 1
            if (len(self._queue) + 1 > self._limits.max_queue_rows
                    or self._queue_bytes + active_bytes + line_bytes > self._limits.max_queue_bytes):
                self._counts["queue_full"] += 1
                return "rejected:queue_full"
            self._seq = seq
            self._counts["admitted"] += 1
            if self._dir is None:
                self._mem.append(row)
                while len(self._mem) > self._limits.max_mem_rows:
                    self._mem.pop(0)
                    self._mem_earliest_seq += 1
                self._confirmed_seq = seq
                self._counts["confirmed"] += 1
                return "queued"
            self._queue.append((seq, row, line))
            self._queue_bytes += line_bytes
            self._not_empty.notify()
            return "queued"

    # ── writer ────────────────────────────────────────────────────────────────────────────────────────
    def _run_writer(self) -> None:
        try:
            while True:
                with self._admit_lock:
                    if self._degraded:
                        return                          # stop draining: queued rows remain undrained (BJM-02)
                    while not self._queue and not self._fenced:
                        self._not_empty.wait()
                    if self._degraded:
                        return
                    if not self._queue:
                        if self._fenced:
                            return
                        continue
                    item = self._queue.pop(0)
                    self._queue_bytes -= len(item[2].encode("utf-8")) + 1
                    self._active_row = item
                self._write_one(item)
        except BaseException:                           # noqa: BLE001 — an UNEXPECTED worker death must fence
            # Terminal worker-failure transition (BJT-03): reject new admission, keep the in-flight row as
            # uncertain (never confirmed), notify waiters and record a CONSTANT diagnostic — never format the
            # exception (its repr could itself raise and replace the primary, BJU-02) — then RE-RAISE so the
            # thread dies and the primary exception reaches threading.excepthook unchanged.
            with self._admit_lock:
                if not self._degraded:
                    self._degraded = True
                    if self._active_row is not None:
                        self._counts["uncertain"] += 1
                self._worker_failed = True
                self._worker_exc = "unexpected_worker_failure"
                self._not_empty.notify_all()
            raise

    def _write_one(self, item: tuple[int, dict, str]) -> None:
        seq, _row, line = item
        payload = (line + "\n").encode("utf-8")
        if self._active_bytes + len(payload) > self._limits.max_segment_bytes:
            try:
                self._rotate()                          # reserve-before-create; may raise on unreclaimable quota
            except (OSError, JournalUnavailable):
                self._enter_degraded(seq)
                return
        try:
            fd = os.open(self._active_path, os.O_WRONLY | os.O_APPEND | _O_BINARY)
            try:
                n = os.write(fd, payload)               # a single append; a short/zero write is uncertain,
                if n != len(payload):                   # not retried (no glue) and not acknowledged (BJM-01)
                    self._enter_degraded(seq)
                    return
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            self._enter_degraded(seq)
            return
        with self._admit_lock:
            self._active_bytes += len(payload)          # advance the acknowledged boundary only now
            self._confirmed_seq = seq
            self._counts["confirmed"] += 1
            self._active_row = None

    def _enter_degraded(self, seq: int) -> None:
        # Keep the uncertain row charged (active_row stays set) so it is counted undrained, not discarded or
        # confirmed. Stop durable writing; a deliberate reopen is required.
        with self._admit_lock:
            if not self._degraded:
                self._counts["uncertain"] += 1
            self._degraded = True

    def _rotate(self) -> None:
        self._reserve_capacity(protect_ordering=self._active_ordering)
        self._open_new_segment((self._active_ordering or 0) + 1)

    # ── flush / counters / read ─────────────────────────────────────────────────────────────────────────
    def flush(self, timeout: float = 5.0) -> FlushResult:
        if self._state not in ("started", "closing", "closed"):
            raise JournalStateError(f"flush() in state {self._state!r}")
        if self._dir is None:
            with self._admit_lock:
                return FlushResult(self._confirmed_seq, 0, False)
        deadline = time.monotonic() + timeout
        while True:
            with self._admit_lock:
                undrained = len(self._queue) + (1 if self._active_row is not None and not self._degraded else 0)
                if undrained == 0 or self._degraded:
                    return FlushResult(self._confirmed_seq, undrained, self._degraded)
            if time.monotonic() >= deadline:
                with self._admit_lock:
                    return FlushResult(self._confirmed_seq,
                                       len(self._queue) + (1 if self._active_row is not None and not self._degraded else 0),
                                       self._degraded)
            time.sleep(0.003)

    def counters(self) -> dict:
        with self._admit_lock:
            snap: dict[str, Any] = dict(self._counts)
            snap["undrained"] = len(self._queue) + (1 if self._active_row is not None and not self._degraded else 0)
            snap["degraded"] = self._degraded
            snap["unresolved"] = self._unresolved
            snap["worker_failed"] = self._worker_failed
            snap["worker_exc"] = self._worker_exc
            snap["confirmed_seq"] = self._confirmed_seq
            snap["run_id"] = self._run_id
            return snap

    def read(self, cursor: Optional[Cursor] = None, *, max_rows: Optional[int] = None,
             max_bytes: Optional[int] = None) -> ReadPage:
        if self._state == "new":
            raise JournalStateError("read() before start()")
        max_rows = min(max_rows or self._limits.max_read_rows, self._limits.max_read_rows)
        max_bytes = min(max_bytes or self._limits.max_read_bytes, self._limits.max_read_bytes)
        if self._dir is None:
            return self._read_memory(cursor, max_rows, max_bytes)
        return self._read_segments(cursor, max_rows, max_bytes)

    def _read_memory(self, cursor, max_rows, max_bytes) -> ReadPage:
        with self._admit_lock:
            earliest = self._mem_earliest_seq
            # Stable identity: the cursor names THIS run's lifetime (seg_id == run_id) and the last-returned seq.
            # A cursor from another journal lifetime is expired, not silently re-based (BJS-02).
            if cursor is not None and cursor.seg_id != self._run_id:
                return ReadPage(earliest_seq=earliest, expired=True)
            after = cursor.offset if cursor is not None else (earliest - 1)
            if cursor is not None and after < earliest - 1:
                return ReadPage(earliest_seq=earliest, expired=True)   # requested position was evicted
            page = ReadPage(earliest_ordering=0, earliest_seq=earliest)
            out_bytes = 0
            last_seq = after
            for row in self._mem:
                if row["seq"] <= after:
                    continue
                enc = len(json.dumps(row, separators=(",", ":")).encode("utf-8"))
                if out_bytes + enc > max_bytes:
                    if not page.rows:
                        page.budget_too_small = True   # explicit bounded outcome, never a silent overshoot
                    break
                if len(page.rows) >= max_rows:
                    break
                page.rows.append(copy.deepcopy(row))    # detached view: callers cannot rewrite retained state
                out_bytes += enc
                last_seq = row["seq"]
            page.next_cursor = Cursor(0, self._run_id, last_seq) if page.rows and last_seq < self._seq else None
            return page

    def _read_segments(self, cursor, max_rows, max_bytes) -> ReadPage:
        with self._admit_lock:
            ack_ordering, ack_seg_id, ack_bytes = self._active_ordering, self._active_seg_id, self._active_bytes
        try:
            segs = self._list_segments_checked()
        except JournalUnavailable:
            return ReadPage(expired=True)               # incomplete catalogue: no false records
        page = ReadPage(earliest_ordering=(segs[0][0] if segs else None))
        if not segs:
            return page
        if cursor is not None and not any(o == cursor.ordering and s == cursor.seg_id for o, s, _ in segs):
            page.expired = True
            return page
        scanned = 0
        reached = cursor is None
        for ordering, seg_id, path in segs:
            if ack_ordering is not None:
                # One coherent read generation: a segment newer than the captured active identity (a rotation
                # that raced this read), or a foreign segment at the active ordering, is NOT unlimited history
                # and must not expose unacknowledged bytes (BJS-01).
                if ordering > ack_ordering:
                    continue
                if ordering == ack_ordering and seg_id != ack_seg_id:
                    continue
            at_cursor = cursor is not None and ordering == cursor.ordering and seg_id == cursor.seg_id
            if not reached:
                if at_cursor:
                    reached = True
                else:
                    continue
            start_off = max(0, cursor.offset) if (at_cursor and cursor is not None) else 0
            resume_discarding = bool(at_cursor and cursor is not None and cursor.discarding)
            seg_limit = ack_bytes if (ordering == ack_ordering and seg_id == ack_seg_id) else None
            done, scanned = self._scan_segment(path, start_off, seg_limit, ordering, seg_id, page,
                                               max_rows, max_bytes, scanned, resume_discarding)
            if done:
                page.scanned_bytes = scanned
                return page
            cursor = None
        page.scanned_bytes = scanned
        return page

    def _discard_to_newline(self, fh, off, seg_limit, scanned):
        """Skip an over-long corrupt line to just past its next newline WITHOUT loading it, bounded by the scan
        budget and the acknowledged segment boundary. Returns (found, new_off, scanned)."""
        limit = self._limits
        fh.seek(off)
        while scanned < limit.max_scan_bytes:
            cap = min(4096, limit.max_scan_bytes - scanned)
            if seg_limit is not None:
                cap = min(cap, max(0, seg_limit - off))
            if cap <= 0:
                return True, off, scanned                    # reached the acknowledged boundary: stop cleanly
            block = fh.read(cap)
            if not block:
                return True, off, scanned                    # EOF while discarding: at a boundary
            scanned += len(block)
            nl = block.find(b"\n")
            if nl != -1:
                off = off + nl + 1
                fh.seek(off)
                return True, off, scanned
            off += len(block)
        return False, off, scanned                            # scan budget exhausted mid-discard

    def _scan_segment(self, path, start_off, seg_limit, ordering, seg_id, page, max_rows, max_bytes, scanned,
                      resume_discarding=False):
        limit = self._limits
        row_cap = limit.max_row_bytes + 2
        out_bytes = sum(len(json.dumps(r, separators=(",", ":")).encode("utf-8")) for r in page.rows)
        try:
            with open(path, "rb") as fh:
                off = start_off
                if resume_discarding:
                    found, off, scanned = self._discard_to_newline(fh, off, seg_limit, scanned)
                    if not found:
                        page.next_cursor = Cursor(ordering, seg_id, off, discarding=True)
                        return True, scanned
                    self._counts["recovery_skipped"] += 1
                fh.seek(off)
                while True:
                    if len(page.rows) >= max_rows or scanned >= limit.max_scan_bytes:
                        page.next_cursor = Cursor(ordering, seg_id, off)   # record boundary
                        return True, scanned
                    if seg_limit is not None and off >= seg_limit:
                        return False, scanned                              # acknowledged boundary reached
                    remaining_scan = limit.max_scan_bytes - scanned
                    read_cap = min(row_cap, remaining_scan)
                    if seg_limit is not None:
                        read_cap = min(read_cap, seg_limit - off)
                    if read_cap <= 0:
                        page.next_cursor = Cursor(ordering, seg_id, off)
                        return True, scanned
                    raw = fh.readline(read_cap)
                    if not raw:
                        return False, scanned                              # EOF / boundary of this segment
                    if raw.endswith(b"\n"):
                        scanned += len(raw)
                        if len(raw) > limit.max_row_bytes:
                            self._counts["recovery_skipped"] += 1          # complete but over-long: skip whole
                            off += len(raw)
                            continue
                        if out_bytes + len(raw) > max_bytes:               # byte budget, even the first row
                            if not page.rows:
                                page.budget_too_small = True
                            page.next_cursor = Cursor(ordering, seg_id, off)
                            return True, scanned
                        off += len(raw)
                        try:
                            row = json.loads(raw.decode("utf-8"))
                        except (ValueError, UnicodeDecodeError):
                            self._counts["recovery_skipped"] += 1          # malformed complete line: skip
                            continue
                        page.rows.append(row)
                        out_bytes += len(raw)
                        continue
                    # No newline within read_cap.
                    scanned += len(raw)
                    if read_cap < row_cap:
                        # Cut short by the scan (or acknowledged-segment) budget, not a full-row read. If rows
                        # were already returned, resume next page; if the page is empty the configured budget
                        # cannot fit a single row -> explicit non-progress, never a silent stuck cursor (BJS-04).
                        if page.rows:
                            page.next_cursor = Cursor(ordering, seg_id, off)
                        else:
                            page.budget_too_small = True
                        return True, scanned
                    if len(raw) < read_cap:
                        return False, scanned                              # EOF before a newline: tail, withhold
                    # A full row_cap read with no newline: a genuinely over-long corrupt line. Discard the whole
                    # line to its next newline; if the scan budget runs out, a discarding cursor resumes it so
                    # its suffix is never promoted to a record.
                    found, off, scanned = self._discard_to_newline(fh, off + len(raw), seg_limit, scanned)
                    if not found:
                        page.next_cursor = Cursor(ordering, seg_id, off, discarding=True)
                        return True, scanned
                    self._counts["recovery_skipped"] += 1
        except OSError:
            pass
        return False, scanned

    # ── close ────────────────────────────────────────────────────────────────────────────────────────
    def close(self, timeout: float = 5.0) -> CloseResult:
        if self._state == "new":
            self._state = "closed"
            return CloseResult(-1, 0, False, True, True)
        if self._dir is None:
            self._state = "closed"
            return CloseResult(self._confirmed_seq, 0, False, True, True)
        if self._state == "closed":
            # Truly closed only if the lock is no longer held; a still-held lock means an unresolved retry.
            if self._lock is None or not self._lock.held():
                return CloseResult(self._confirmed_seq, 0, self._degraded, True, True)
            # else fall through and retry the release below
        with self._admit_lock:
            self._fenced = True
            self._not_empty.notify_all()
        writer = self._writer
        alive = False
        if writer is not None:
            try:
                writer.join(timeout)
                alive = writer.is_alive()
            except RuntimeError:
                # join-before-start: the thread was never confirmed started; we cannot prove the native thread
                # is dead -> honest unresolved, retain ownership (BJS-05).
                alive = True
        with self._admit_lock:
            undrained = len(self._queue) + (1 if self._active_row is not None and not self._degraded else 0)
            self._counts["undrained_at_close"] = undrained
        if alive:
            # Uncertain native ownership (a join-before-start / still-running worker): retain and stay
            # unresolved until termination is known. This qualification is ONLY for a launched worker.
            self._state = "closing"
            self._unresolved = True
            return CloseResult(self._confirmed_seq, undrained, self._degraded, False, False)
        released = self._lock.release() if self._lock is not None else True
        if released:
            self._lock = None
            self._unresolved = False
            self._state = "closed"
        else:
            self._unresolved = True          # release failed: retain the handle, later close() retries
            self._state = "closing"
        return CloseResult(self._confirmed_seq, undrained, self._degraded, released, released)

    def __enter__(self) -> "BleJournal":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
