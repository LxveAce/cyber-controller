r"""Tool-acquisition job registry — the in-memory state layer behind an async "Get tools" flow.

Installing/enabling a crack tool (a ~5 MB download, or a bundled-pack extract) is slow enough that the
HTTP request shouldn't block on it. This is the pure, framework-agnostic job service the web routes will
drive: it runs one worker per job in a background thread, tracks its lifecycle, streams bounded progress
to a snapshot, and supports an explicit commit boundary + cooperative cancellation.

State is **in-memory only** (a reconnect survives a page refresh, NOT a server restart). It is bounded:
per-line and per-error length caps, and terminal jobs are pruned beyond a retention count so a long-lived
server can't grow without limit. Active jobs and their destination reservations are never pruned.

Contract (matches the reviewed refinements):

* Opaque ``job_id`` (never the tool name — the tool name is not a capability/identity).
* States ``queued`` -> ``running`` -> one terminal (``succeeded`` / ``failed`` / ``cancelled``).
* **One writer per destination**: a second job for a destination with an ACTIVE job conflicts (409). The
  key is canonical (``tool_bundle.canonical_dest`` — case-folded on Windows) so aliases agree.
* **Explicit commit boundary (J1)**: the worker calls ``begin_commit()`` before its irreversible publish.
  That atomically raises :class:`JobCancelled` if a cancel is already pending, else marks the job as
  committing so a later ``cancel`` is refused (too late). A normal successful worker return is ALWAYS
  ``succeeded`` — the registry never downgrades a committed result to cancelled because a flag was set.
* **Explicit cancellation (J2)**: only a :class:`JobCancelled` raised by the worker becomes ``cancelled``;
  any other exception is ``failed`` with its message, even if a cancel was also requested — a real I/O,
  integrity, or recovery failure is never masked by a cancellation flag.
* **Owner-bound**: ``get`` / ``cancel`` / ``result`` require the owner token (authenticated session id +
  credential generation) recorded at start, so one session can't read or control another's job.

This module does NO tool I/O, network, or filesystem writes — the work is the injected ``worker``, so the
lifecycle is unit-testable with a synthetic worker.
"""

from __future__ import annotations

import json
import math
import threading
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Optional

from .tool_bundle import canonical_dest

#: Job lifecycle states. ``queued``/``running`` are active; the rest are terminal (exactly one).
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
_TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED})

DEFAULT_LOG_LINES = 500       #: retained log lines per job (a ring buffer; older lines drop)
MAX_LINE_CHARS = 2000         #: per-line cap so one giant line can't blow memory
MAX_ERROR_CHARS = 2000        #: per-error cap
MAX_PHASE_CHARS = 200         #: max phase-label length; a longer label is rejected, never truncated
MAX_RESULT_BYTES = 8192       #: cap on the JSON-serialized worker result retained per job
MAX_RESULT_FIELDS = 32        #: max number of fields in a result envelope (checked BEFORE any copy/serialize)
MAX_RESULT_KEY_CHARS = 128    #: max length of a result-envelope field name
MAX_RESULT_VALUE_CHARS = 2048  #: max length of a result-envelope string value (a path, a version, a hash)
MAX_PROGRESS_COUNT = 2 ** 53 - 1  #: max int for a counter/result number — JavaScript's exact-integer ceiling
DEFAULT_MAX_TERMINAL = 200    #: retained terminal jobs (older ones pruned; active jobs never pruned)

#: Prebuilt (no allocation on the failure path) diagnostics for a result that can't be retained as-is.
_RESULT_DROPPED_INVALID = '{"result_dropped": "worker result was not a valid tool-result envelope"}'
_RESULT_DROPPED_TOO_LARGE = f'{{"result_dropped": "worker result exceeded {MAX_RESULT_BYTES} bytes"}}'
# R-DL7: the worker DID return successfully, but formatting its result was interrupted by a control signal
# (KeyboardInterrupt/SystemExit). The job is still SUCCEEDED — the install completed — only the result payload
# is unavailable; this fixed marker preserves that honest distinction instead of implying the work failed.
_RESULT_DROPPED_INTERRUPTED = '{"result_dropped": "worker succeeded; result formatting was interrupted"}'
#: Fixed, bounded, owner-visible note when a job's cleanup finalizer raised (F1). Never carries the callback's
#: own exception text; the completed work is NOT relabelled.
_FINALIZER_FAILED_NOTE = ("[cleanup] post-job cleanup failed; this install destination may remain reserved "
                          "until the service restarts")


def _as_result_envelope(result: object) -> "tuple[bool, Optional[dict]]":
    """Validate *result* against the small tool-result envelope contract: ``None``, or a **flat** mapping of
    at most ``MAX_RESULT_FIELDS`` string keys to bounded primitives (str/int/finite-float/bool/None).

    Uses **exact** built-in types (``type(x) is …``), not ``isinstance``: an ``int``/``str`` subclass can carry
    a mutable attribute that would be retained by identity, so only plain built-ins are accepted. The field
    count is checked BEFORE iterating or copying, so a 100k-field input is rejected without any proportional
    work. Integers are bounded to the JavaScript-safe range so the route response is an exact browser number.
    Nested containers, lists, oversized/NaN/Inf values and non-string keys are rejected. Returns
    ``(ok, cleaned)`` — flat-by-construction, so ``cleaned`` also has bounded depth and finite serialization."""
    if result is None:
        return True, None
    if type(result) is not dict or len(result) > MAX_RESULT_FIELDS:
        return False, None
    clean: dict = {}
    for k, v in result.items():
        if type(k) is not str or len(k) > MAX_RESULT_KEY_CHARS:
            return False, None
        tv = type(v)
        if v is None or tv is bool:
            clean[k] = v                                   # bool checked before int: a bool is a valid flag
        elif tv is int:
            if not (-MAX_PROGRESS_COUNT <= v <= MAX_PROGRESS_COUNT):
                return False, None                         # outside JS-safe integer range
            clean[k] = v
        elif tv is float:
            if not math.isfinite(v):
                return False, None                         # NaN/Inf are not standard JSON (J8)
            clean[k] = v
        elif tv is str:
            if len(v) > MAX_RESULT_VALUE_CHARS:
                return False, None
            clean[k] = v
        else:
            return False, None                             # nested container / subtype / other -> reject
    return True, clean


def _bounded_result_json(result: object) -> str:
    """Normalize a worker result to a bounded, standard-JSON STRING (stored, not the live object).

    Exception-proof BY CONTRACT: any failure — a non-envelope shape, a non-finite number, an oversized
    payload, or a serializer error (RecursionError/MemoryError on a pathological input) — resolves to a
    prebuilt bounded diagnostic instead of propagating. That guarantee is what lets the success path release
    the destination reservation even when a result can't be retained (J7). ``allow_nan=False`` keeps the
    stored text valid standard JSON so a browser ``JSON.parse`` of the route response can't choke (J8).
    Round-tripping through JSON also makes :meth:`JobRegistry.result` hand out a fresh copy each call."""
    try:
        ok, envelope = _as_result_envelope(result)
        if not ok:
            return _RESULT_DROPPED_INVALID
        s = json.dumps(envelope, allow_nan=False)
    except Exception:  # noqa: BLE001 — normalization must NEVER raise into the terminal path (J7)
        return _RESULT_DROPPED_INVALID
    return s if len(s) <= MAX_RESULT_BYTES else _RESULT_DROPPED_TOO_LARGE


class JobConflict(RuntimeError):
    """Raised by :meth:`JobRegistry.start` when the destination already has an active job."""


class JobLaunchError(RuntimeError):
    """Raised by :meth:`JobRegistry.start` when the worker thread could not be launched."""


class JobCancelled(RuntimeError):
    """The dedicated cooperative-cancellation signal. A worker raises it (only) to mean 'I stopped because
    cancellation was requested, before committing' — distinct from a real failure so the two never mix."""


def _cap(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


_UNRENDERABLE_ERROR = "<error message was not renderable>"   #: fixed fallback when an error can't be formatted


def _safe_error_text(exc: BaseException, prefix: str = "") -> str:
    """Format an exception message that CANNOT raise. A pathological ``__str__`` (one that itself raises, or
    returns a ``str`` subclass whose own ``__str__``/``__len__`` raises) must not escape the terminal-failure
    path and strand the destination reservation. ALL conversion, the exact-``str`` check and the length cap
    happen inside one guard; a non-exact result or any raise falls back to a fixed message. Bounded by
    ``MAX_ERROR_CHARS``."""
    fallback = f"{prefix}: {_UNRENDERABLE_ERROR}" if prefix else _UNRENDERABLE_ERROR
    try:
        msg = f"{prefix}: {exc}" if prefix else str(exc)
        if type(msg) is not str:            # a str subclass could re-enter a raising __str__/__len__ below
            return fallback
        return msg if len(msg) <= MAX_ERROR_CHARS else msg[:MAX_ERROR_CHARS] + "…[truncated]"
    except Exception:  # noqa: BLE001 — a broken __str__/__len__ must never break cleanup
        return fallback


def _clean_counter(value: object) -> Optional[int]:
    """A progress counter is an EXACT ``int`` in ``[0, MAX_PROGRESS_COUNT]`` (an immutable, JS-safe primitive)
    — or ``None`` for 'no update / unknown'. ``type(value) is int`` (not ``isinstance``) rejects ``bool`` and
    any ``int`` subclass, since a subclass could carry a mutable attribute that would be retained by identity
    and mutate a later snapshot. A giant int, a float, a container or a negative all return ``None``."""
    if type(value) is not int or not (0 <= value <= MAX_PROGRESS_COUNT):
        return None
    return value


def _classify_phase(phase: object) -> "tuple[bool, Optional[str]]":
    """Classify an emitted phase argument into ``(counters_ok, new_phase)``:

    * ``None`` or an exact empty ``str`` → ``(True, None)``: the documented no-phase-change sentinel — the
      phase is left as-is and any counters on the same emit still apply (a progress-only / log-only update).
    * an EXACT ``str`` of 1..``MAX_PHASE_CHARS`` chars → ``(True, phase)``: a real phase change.
    * anything else (over-length, non-``str``, or a ``str`` subclass carrying a mutable attribute) →
      ``(False, None)``: a malformed emit. The phase is rejected (never truncated, so two long names can't
      alias) AND the counters riding on that emit are discarded rather than attached to the wrong phase.

    The EXACT-type check precedes any ``==`` so a foreign object's ``__eq__`` is never consulted for the
    sentinel — an object that equals ``""`` can't masquerade as the sentinel, and one whose ``__eq__`` raises
    can't turn a (meant-to-be-ignored) malformed phase into a worker failure."""
    if phase is None:
        return True, None
    if type(phase) is not str:
        return False, None          # non-exact/foreign object: malformed, never invoke its __eq__
    if phase == "":
        return True, None           # exact empty string = the no-change sentinel
    return (True, phase) if len(phase) <= MAX_PHASE_CHARS else (False, None)


Emit = Callable[[str, Optional[int], Optional[int], Optional[str]], None]
ShouldCancel = Callable[[], bool]
BeginCommit = Callable[[], None]
#: Worker signature. Receives ``emit(phase, completed, total, line)``, a ``should_cancel()`` predicate,
#: and ``begin_commit()`` (call before the irreversible publish). Returns a small opaque result — fetched
#: via :meth:`JobRegistry.result`, NOT in the public snapshot — or raises (:class:`JobCancelled` =>
#: cancelled; any other exception => failed).
Worker = Callable[[Emit, ShouldCancel, BeginCommit], object]


class DispatchOwnership:
    """Explicit registration/dispatch ownership signal (R-DL6).

    A caller that pre-reserves resources (a destination admission, a runtime-work reservation) and hands their
    release to :meth:`JobRegistry.start` as its ``on_finish`` needs to know, on EVERY exit path, whether the
    registry took ownership of that finalizer — because the caller cannot infer it from ``start()``'s return
    alone. ``start()`` re-raises a control exception (:class:`KeyboardInterrupt`/:class:`SystemExit`) even when
    the worker already entered and OWNS the job; a caller gating its own release on 'did start return normally'
    would then double-free an entered worker's resources.

    ``start()`` sets :attr:`registered` to ``True`` the instant the job (carrying its ``on_finish``) is entered
    into the registry — before the launch thread is created. From that point the job's finalizer runs exactly
    once on some terminal path (entered worker's terminal transition, or a not-entered launch failure that
    ``start()`` finalizes itself), so the caller MUST NOT run its own release. ``registered`` stays ``False``
    only when no job was ever registered (a :class:`JobConflict`, or a failure before registration) — the one
    case where the caller still owns the pre-start cleanup.
    """

    __slots__ = ("registered",)

    def __init__(self) -> None:
        self.registered = False


@dataclass
class _Job:
    job_id: str
    tool: str
    dest_key: str          # the canonical destination key (one writer per key)
    owner: str
    state: str = QUEUED
    phase: str = ""
    completed: Optional[int] = None
    total: Optional[int] = None
    error: str = ""
    result_json: str = "null"   # the bounded, JSON-serialized worker result (see _bounded_result_json)
    log: Deque[str] = field(default_factory=lambda: deque(maxlen=DEFAULT_LOG_LINES))
    on_finish: Optional[Callable[[], None]] = None   # cleanup run ONCE at the terminal transition
    _cancel: bool = False
    _committing: bool = False
    _finalized: bool = False
    _entered: bool = False    # the worker atomically claims this the instant it starts running (R-DL4)
    _revoked: bool = False    # start() finalized this job before the worker entered — the worker must abort

    def snapshot(self) -> dict:
        """A JSON-serializable, owner-free view for the status endpoint / an event payload. Excludes
        ``result`` (fetched separately by the owner) so a worker result is never broadcast on an event."""
        return {
            "job_id": self.job_id,
            "tool": self.tool,
            "state": self.state,
            "phase": self.phase,
            "completed": self.completed,
            "total": self.total,
            "error": self.error,
            "log": list(self.log),
            "active": self.state not in _TERMINAL,
        }


class JobRegistry:
    """Thread-safe registry of tool jobs. One shared instance drives the async Get-tools routes."""

    def __init__(self, log_lines: int = DEFAULT_LOG_LINES,
                 max_terminal: int = DEFAULT_MAX_TERMINAL) -> None:
        # Validate the retention contract up front (J4b): a bad config would otherwise give an unbounded
        # deque (log_lines=None) or break/erase pruning (a non-positive terminal cap). At least one log
        # line and at least one retained terminal job (so a just-finished job survives for reconnect).
        if not isinstance(log_lines, int) or isinstance(log_lines, bool) or log_lines < 1:
            raise ValueError(f"log_lines must be a positive int, got {log_lines!r}")
        if not isinstance(max_terminal, int) or isinstance(max_terminal, bool) or max_terminal < 1:
            raise ValueError(f"max_terminal must be a positive int, got {max_terminal!r}")
        self._log_lines = log_lines
        self._max_terminal = max_terminal
        self._jobs: "OrderedDict[str, _Job]" = OrderedDict()
        self._active_dest: dict[str, str] = {}   # canonical dest_key -> job_id (only ACTIVE jobs)
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------

    def start(self, tool: str, dest_key: str, owner: str, worker: Worker,
              on_finish: Optional[Callable[[], None]] = None,
              ownership: Optional["DispatchOwnership"] = None) -> str:
        """Register + launch a job for *dest_key*; return its opaque job_id. Raises :class:`JobConflict` if
        that (canonical) destination already has an active job — one writer per destination. On a thread
        launch failure the queued record is marked failed and its reservation released, then
        :class:`JobLaunchError` is raised (a subsequent start for the same destination then succeeds).

        *on_finish* (if given) runs EXACTLY ONCE when the job reaches ANY terminal state — success, failure,
        an explicit cancel, a cancel that landed before the worker ran, or a thread-launch failure — so a
        caller that reserved a resource for the whole job (e.g. a destination admission held from queue time)
        can release it on every terminal path. It must be fast and non-reentrant (never call back into this
        registry); it is invoked under the registry lock and its ordinary exceptions are swallowed (a control
        exception it raises is noted and re-raised — R-DL9). A JobConflict (raised before any job exists) does
        NOT run it — the caller still owns its pre-start cleanup then.

        *ownership* (if given) is stamped ``registered=True`` the instant the job is entered into the registry
        (before the launch thread starts). Once stamped, the job's finalizer owns cleanup on every exit path,
        so the caller must not run its own release — even when this method re-raises a control exception from an
        already-entered worker (R-DL6). See :class:`DispatchOwnership`."""
        key = canonical_dest(dest_key)
        with self._lock:
            if key in self._active_dest:
                raise JobConflict(f"an install for {tool!r} is already running")
            job = _Job(job_id=uuid.uuid4().hex, tool=tool, dest_key=key, owner=owner, on_finish=on_finish)
            job.log = deque(maxlen=self._log_lines)
            self._jobs[job.job_id] = job
            self._active_dest[key] = job.job_id
            if ownership is not None:
                # The registry now owns on_finish on every exit path below (return, JobLaunchError, or a
                # re-raised control exception). The caller reads this to know NOT to double-free.
                ownership.registered = True
        try:
            threading.Thread(target=self._run, args=(job.job_id, worker),
                             name=f"tool-job-{tool}", daemon=True).start()
        except BaseException as exc:  # noqa: BLE001 — control exceptions too (R-DL2): must not strand
            with self._lock:
                already_terminal = job.state in _TERMINAL
                entered = job._entered
                if not already_terminal and not entered:
                    # The worker never entered AND the job isn't already terminal: revoke it (so a delayed
                    # target aborts at entry) and finalize — release the reservation + run the finalizer +
                    # count it against the terminal cap (J4a) — so neither an ordinary launch error nor a
                    # control interruption strands the queued job or its destination (R-DL2).
                    job._revoked = True
                    job.state = FAILED
                    try:
                        job.error = _safe_error_text(exc, "could not start the install thread")
                    except BaseException as fmt_control:  # noqa: BLE001 — R-DL7: format itself interrupted by a
                        # control signal. Finalize with a fixed fallback, preserving THAT control (R-DL9: a
                        # secondary finalizer control chains under it), then re-raise it as the primary.
                        job.error = _UNRENDERABLE_ERROR
                        self._finish_preserving(job, fmt_control)
                        raise
                    if isinstance(exc, Exception):
                        self._finish_locked(job)   # ordinary launch error -> JobLaunchError below
                    else:
                        # A control launch error is the primary: R-DL9 — a secondary finalizer control must not
                        # replace it. _finish_preserving chains the cleanup failure as its cause; the `raise` at
                        # the tail then propagates this control.
                        self._finish_preserving(job, exc)
                # else: the worker ALREADY entered and OWNS the job (R-DL4), or the job is ALREADY terminal —
                # e.g. a pre-entry cancel that completed in _run (R-DL8). Either way this handler must NOT
                # finalize or relabel: an entered worker finalizes itself, and a completed cancellation is
                # immutable. A late start-thread report cannot overwrite either.
            if isinstance(exc, Exception):
                if entered or already_terminal:
                    # A genuinely admitted (or already-finished) job: report its id. R-DL8: an ordinary late
                    # start error must NOT relabel a completed cancellation as failed.
                    return job.job_id
                # Never entered, freshly finalized above: advertised JobLaunchError, built from the already-safe
                # text (never re-render str(exc), so a directly-unprintable launch exception can't turn this
                # into a raw ValueError).
                raise JobLaunchError(job.error or "could not start the install") from exc
            # R-DL6: a control exception (KeyboardInterrupt/SystemExit) ALWAYS propagates — even from an entered
            # worker (which keeps its ownership; the caller's `ownership.registered` is already True so it will
            # not double-free) and from an already-terminal job (whose state is untouched). Cleanup, if any was
            # owed, already ran above.
            raise
        return job.job_id

    def _run(self, job_id: str, worker: Worker) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return

        def emit(phase: str, completed: Optional[int], total: Optional[int],
                 line: Optional[str]) -> None:
            # J4b: every field is validated to a bounded immutable primitive BEFORE it is stored, so a
            # pathological worker can neither retain a giant/mutable object in phase/completed/total nor
            # mutate a terminal snapshot through a shared reference. An over-length phase is rejected (not
            # truncated) so two long names can't alias; a malformed phase also discards the counters riding
            # on the same emit (counters_ok=False) rather than pinning them to the wrong phase.
            counters_ok, new_phase = _classify_phase(phase)
            new_completed = _clean_counter(completed)
            new_total = _clean_counter(total)
            with self._lock:
                if job.state in _TERMINAL:
                    return   # J5: progress after a terminal transition is ignored, never mutates the final
                if new_phase is not None and new_phase != job.phase:
                    job.phase = new_phase        # a new (valid) phase resets its progress counters...
                    job.completed = None
                    job.total = None
                if counters_ok:
                    if new_completed is not None:
                        job.completed = new_completed
                    if new_total is not None:
                        job.total = new_total
                if line:
                    job.log.append(_cap(line, MAX_LINE_CHARS))

        def should_cancel() -> bool:
            with self._lock:
                return job._cancel

        def begin_commit() -> None:
            # J1: the worker's atomic "point of no return". If cancellation is already pending, stop
            # (cooperative, before any irreversible write); otherwise mark committing so cancel is
            with self._lock:
                if job._cancel:
                    raise JobCancelled("cancelled before commit")
                job._committing = True

        with self._lock:
            if job._revoked or job.state in _TERMINAL:
                return   # R-DL2: start() already failed+finalized this job before we entered — do not run
            if job._cancel:
                job.state = CANCELLED          # a cancel that landed before we started still wins
                self._finish_locked(job)
                return
            job._entered = True                # atomic "entered" claim: from here start() must not finalize us
            job.state = RUNNING
        try:
            result = worker(emit, should_cancel, begin_commit)
        except JobCancelled:
            with self._lock:
                job.state = CANCELLED          # J2: ONLY an explicit JobCancelled is a cancellation
                self._finish_locked(job)
            return
        except Exception as exc:  # noqa: BLE001 — J2: any other error is a FAILURE, even if cancel was set
            # _safe_error_text swallows an ORDINARY broken __str__, but a __str__ that raises a CONTROL signal
            # (KeyboardInterrupt/SystemExit) escapes it (R-DL7). Guard it: finalize FAILED with a fixed fallback
            # and re-raise the control, so error formatting can never leave the worker stranded RUNNING.
            try:
                error = _safe_error_text(exc)
            except BaseException as fmt_control:  # noqa: BLE001 — control from a pathological __str__ during
                # formatting. Finalize FAILED with a fixed fallback, preserving THAT control as primary (R-DL9:
                # a secondary finalizer control chains under it), then re-raise it.
                with self._lock:
                    if job.state not in _TERMINAL:
                        job.state = FAILED
                        job.error = _UNRENDERABLE_ERROR
                        self._finish_preserving(job, fmt_control)
                raise
            with self._lock:
                if job.state not in _TERMINAL:
                    job.state = FAILED
                    job.error = error
                    self._finish_locked(job)
            return
        except BaseException as exc:  # noqa: BLE001 — R-DL3: a control interruption (KeyboardInterrupt/
            # SystemExit) from the worker must still leave a terminal transition + finalizer, not a stranded
            # running job, then re-raise the ORIGINAL control object. R-DL9: if the finalizer ALSO raises a
            # control signal, keep the worker's original control as primary and chain the cleanup failure as
            # its cause — a cleanup signal must never silently replace the worker's original signal.
            try:
                error = _safe_error_text(exc)
            except BaseException:  # noqa: BLE001 — even formatting the control exc was interrupted
                error = _UNRENDERABLE_ERROR
            with self._lock:
                if job.state not in _TERMINAL:
                    job.state = FAILED
                    job.error = error
                    self._finish_preserving(job, exc)
            raise
        # J7: normalize the result OUTSIDE the lock. _bounded_result_json is exception-proof for ORDINARY errors
        # (a deep, cyclic, non-finite or oversized result all resolve to a bounded diagnostic). A CONTROL signal
        # during normalization still escapes it — R-DL7: the worker already SUCCEEDED (the install completed), so
        # finalize as SUCCEEDED with a fixed "result unavailable" marker rather than implying failure, then
        # re-raise the control (R-DL9: a secondary finalizer control chains under it, never replaces it). It also
        # keeps json.dumps off the registry-wide lock.
        try:
            result_json = _bounded_result_json(result)
        except BaseException as norm_control:  # noqa: BLE001 — control during result formatting; worker succeeded
            with self._lock:
                if job.state not in _TERMINAL:
                    job.state = SUCCEEDED
                    job.result_json = _RESULT_DROPPED_INTERRUPTED
                    self._finish_preserving(job, norm_control)
            raise
        with self._lock:
            if job.state not in _TERMINAL:
                job.state = SUCCEEDED          # J1: a successful return is SUCCEEDED regardless of the flag
                job.result_json = result_json
                self._finish_locked(job)

    def _finish_preserving(self, job: _Job, primary: BaseException) -> None:
        """Run terminal bookkeeping (:meth:`_finish_locked`) while *primary* — a control exception already
        being handled — is in flight, preserving it (R-DL9). If the finalizer ALSO raises a control signal, the
        original *primary* propagates with the cleanup failure chained as its ``__cause__``; a secondary signal
        never replaces the primary. On the ordinary path this returns and the caller re-raises *primary* itself.
        Caller holds ``self._lock``. Keeps _finish_locked's exactly-once behavior + fixed cleanup note intact."""
        try:
            self._finish_locked(job)
        except BaseException as cleanup_error:  # noqa: BLE001 — R-DL9: keep the primary; chain cleanup as cause
            raise primary from cleanup_error

    def _finish_locked(self, job: _Job) -> None:
        """On a terminal transition: release the destination reservation and prune old terminal jobs.
        Caller holds ``self._lock``."""
        if self._active_dest.get(job.dest_key) == job.job_id:
            del self._active_dest[job.dest_key]
        # J6: move the just-finished job to the end so retention is ordered by COMPLETION, not creation.
        # Without this, a long-running job that started first becomes the oldest terminal record the instant
        # it finishes and is pruned immediately — even though newer short jobs completed before it. Applies
        # to every terminal transition, launch failures included.
        self._jobs.move_to_end(job.job_id)
        # Prune oldest-completed terminal jobs beyond the retention cap; active jobs are always kept.
        terminal = [jid for jid, j in self._jobs.items() if j.state in _TERMINAL]
        for jid in terminal[:max(0, len(terminal) - self._max_terminal)]:
            self._jobs.pop(jid, None)
        # Run the per-job finalizer EXACTLY ONCE (queue-lifetime cleanup, e.g. releasing a destination
        # admission). It's a fast, non-reentrant callback (release only touches its own lock, never this
        # registry), so running it under the lock introduces no inversion.
        if job.on_finish is not None and not job._finalized:
            job._finalized = True
            cb = job.on_finish
            job.on_finish = None   # F2: drop the spent callback so its object graph isn't retained until prune
            try:
                cb()               # a falsey return (e.g. a deferred release) is NOT an error
            except Exception:  # noqa: BLE001 — an ORDINARY finalizer failure must never break the transition
                # F1: make a cleanup FAILURE owner-visible without relabelling the committed work — a fixed,
                # bounded note in the log (never the callback's own exception text). The destination may
                # remain reserved; the completed state/result are untouched.
                job.log.append(_FINALIZER_FAILED_NOTE)
            except BaseException:  # noqa: BLE001 — R-DL9: a CONTROL signal (KeyboardInterrupt/SystemExit) from
                # the finalizer is noted, then PROPAGATED so the caller can preserve it — chaining it under a
                # primary worker/start control rather than letting it silently replace that primary. Cleanup is
                # already past the point that could strand the registry: _active_dest was cleared and _finalized
                # set above, so the completed state/result stand regardless.
                job.log.append(_FINALIZER_FAILED_NOTE)
                raise

    # -- queries + control (owner-bound) ------------------------------

    def _owned(self, job_id: str, owner: str) -> Optional[_Job]:
        job = self._jobs.get(job_id)
        return job if (job is not None and job.owner == owner) else None

    def get(self, job_id: str, owner: str) -> Optional[dict]:
        """Snapshot of *job_id* if it exists AND *owner* matches; else None (no cross-session read)."""
        with self._lock:
            job = self._owned(job_id, owner)
            return job.snapshot() if job else None

    def result(self, job_id: str, owner: str) -> object:
        """The worker's result for a SUCCEEDED job the *owner* owns (else None). Kept out of the snapshot
        so a result is never broadcast on an event. Decoded fresh from the retained JSON each call, so the
        caller gets an independent copy it cannot use to mutate the stored outcome (J4b)."""
        with self._lock:
            job = self._owned(job_id, owner)
            return json.loads(job.result_json) if (job and job.state == SUCCEEDED) else None

    def status_and_result(self, job_id: str, owner: str) -> Optional[dict]:
        """Owner-scoped snapshot of BOTH status and result under ONE lock acquisition, so retention can't
        prune the job between a separate status read and result read and make the two disagree. Returns None
        for an unknown/foreign job (a route maps that to 404). ``is_success`` disambiguates a genuinely empty
        success from a non-successful/inaccessible one, so a running job never looks like a null result;
        ``result`` is the decoded outcome only when ``is_success`` (a fresh copy), else None."""
        with self._lock:
            job = self._owned(job_id, owner)
            if job is None:
                return None
            is_success = job.state == SUCCEEDED
            return {
                "snapshot": job.snapshot(),
                "result": json.loads(job.result_json) if is_success else None,
                "is_success": is_success,
            }

    def cancel(self, job_id: str, owner: str) -> Optional[bool]:
        """Request cooperative cancellation. Returns True if the flag was set on a still-cancellable job;
        False if it's too late (already terminal, or past ``begin_commit``); None if it doesn't exist / the
        owner mismatches."""
        with self._lock:
            job = self._owned(job_id, owner)
            if job is None:
                return None
            if job.state in _TERMINAL or job._committing:
                return False
            job._cancel = True
            return True
