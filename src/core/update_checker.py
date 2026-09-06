"""Owned background update-availability checker (Layer 1) — pure, import-safe, NO PyQt/Flask.

This is the *availability* layer only: it decides whether a newer published release exists and exposes a
finite, immutable status snapshot. It never downloads a binary, stages, replaces, or restarts — those are
separate later layers (staging / apply). It also never writes the settings file (the manual-check route's
load-all/save-all pattern is a lost-update hazard, so last-check bookkeeping is kept in memory here).

Ownership contract (one owner per application runtime)
------------------------------------------------------
* **No thread or network activity at import or construction.** A single scheduler thread starts only on an
  explicit :meth:`UpdateChecker.start`, and stops on :meth:`UpdateChecker.stop`. Repeated ``start`` does not
  create a second owner; a late fetch result or a ``stop`` during a fetch never mutates a retired runtime.
* **Deadlines use a monotonic clock; display timestamps are UTC wall-clock.** A monotonic value is not a
  portable timestamp, so ``checked_at`` (nullable) is a UTC ISO-8601 string and every deadline/cooldown/
  interval is computed from an injected monotonic clock.
* **Immutable snapshots + a revision counter.** :meth:`snapshot` returns a frozen :class:`UpdateStatus`;
  ``revision`` increments only when a visible field actually changes, so a poller can detect real changes.

The network read is **bounded** (finite bytes/items + a total deadline) before any automatic fetching is
turned on — the shared :func:`src.core.updater.latest_releases` reads an unbounded body, which is unsafe to
schedule. Transport/DNS teardown is only as bounded as the socket allows; that limit is stated honestly rather
than claimed away. HTTPS host + redirect restrictions are preserved (the fetch reuses flash_core's opener).
"""

from __future__ import annotations

import json
import math
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence
from urllib.parse import urlsplit

from src.core import flash_core, updater

# ── States (finite) ────────────────────────────────────────────────────────────────────────────────
IDLE = "idle"                 # constructed, no check has completed yet (NOT the same as offline/up_to_date)
CHECKING = "checking"         # a check is in flight
UP_TO_DATE = "up_to_date"     # newest published release is not ahead of the running build
AVAILABLE = "available"       # at least one published release is ahead
OFFLINE = "offline"           # the check could not reach/parse GitHub
ERROR = "error"               # an unexpected classification failure (distinct from a clean offline)
_STATES = frozenset({IDLE, CHECKING, UP_TO_DATE, AVAILABLE, OFFLINE, ERROR})

# ── Finite error codes (bounded, never a raw exception string) ──────────────────────────────────────
ERR_OFFLINE = "offline"       # DNS/connect/TLS failure — could not reach the host
ERR_TIMEOUT = "timeout"       # the total transfer deadline elapsed
ERR_TOO_LARGE = "too_large"   # the response body exceeded the byte/item ceiling
ERR_PAYLOAD = "payload"       # not valid JSON, or not a releases list
ERR_HTTP = "http_error"       # a non-success HTTP status
ERR_WORKER_EXIT = "worker_exit"   # the scheduler thread exited unexpectedly (e.g. an injected control signal)

# ── Bounds ──────────────────────────────────────────────────────────────────────────────────────────
MAX_JSON_BYTES = 4 * 1024 * 1024     # a releases list well under this; refuse to buffer more
MAX_RELEASE_ITEMS = 300              # cap parsed list length (GitHub pages at 30; this is generous)
_READ_CHUNK = 1 << 16
MAX_TAG_CHARS = 120                  # bounded snapshot strings
MAX_URL_CHARS = 400

DEFAULT_INTERVAL_SECONDS = 24 * 3600     # background cadence
DEFAULT_MANUAL_COOLDOWN_SECONDS = 60     # runtime-wide floor between manual checks
DEFAULT_FETCH_DEADLINE_SECONDS = 15.0    # total budget for one metadata fetch
_STOP_JOIN_TIMEOUT = 5.0
MAX_OPERATION_WAIT_SECONDS = 25.0

# Practical finite bounds for the numeric scheduling inputs (UC-4). A nonfinite (NaN/inf) or out-of-range
# value is rejected at construction, before any ownership/network work — NaN defeats comparisons and inf can
# overflow the integer retry conversion or hang the scheduler.
_MAX_INTERVAL_SECONDS = 30 * 24 * 3600   # 30 days
_MAX_COOLDOWN_SECONDS = 24 * 3600
_MAX_DEADLINE_SECONDS = 300.0

# The only host/path a published release link may point at (UC-5). GitHub release/tag URLs live under the
# repository's /releases path; anything else (an issues page, a dot-segment escape, another host/repo, a
# non-default port, userinfo) is not a trustworthy destination.
_RELEASE_URL_HOST = "github.com"
_REPO_PATH = "/LxveAce/cyber-controller"
_RELEASES_PATH = _REPO_PATH + "/releases"


def _finite_number(value: object, name: str, *, minimum: float, maximum: float,
                   allow_min: bool = True) -> float:
    """Coerce *value* to a finite float in the range, else raise ValueError. Rejects NaN/inf and bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{name} must be finite, got {value!r}")
    low_ok = v >= minimum if allow_min else v > minimum
    if not low_ok or v > maximum:
        bound = "[" if allow_min else "("
        raise ValueError(f"{name} must be in {bound}{minimum}, {maximum}], got {v}")
    return v


def _safe_release_url(url: str) -> Optional[str]:
    """Return *url* only if it is the canonical HTTPS release page/tag link for THIS repository (UC-5), else
    None. Validates scheme, authority (exact host, no userinfo, no unexpected port) and the exact
    ``/LxveAce/cyber-controller/releases`` path, rejecting dot segments that could escape the repository.
    Validation is never a substitute done by truncation — a foreign/ambiguous URL returns None."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https":
        return None
    if parts.username or parts.password:            # reject userinfo (user:pass@host)
        return None
    try:
        if parts.port not in (None, 443):           # reject an unexpected port (host:8443)
            return None
    except ValueError:
        return None
    if parts.hostname is None or parts.hostname.lower() != _RELEASE_URL_HOST:
        return None
    path = parts.path
    lowered = path.lower()
    # A browser (WHATWG URL) normalizes encoded dot segments (%2e = '.') and treats a backslash as '/' for a
    # special scheme, so either could escape the repository path AFTER our check. Reject any backslash, any
    # percent-encoded dot/slash, and literal dot segments before validating the path (UC-5). Legitimate release
    # links contain none of these.
    if "\\" in path or "%2e" in lowered or "%2f" in lowered:
        return None
    segments = path.split("/")
    if any(seg in ("..", ".") for seg in segments):  # reject dot segments that could leave the repo path
        return None
    if path != _RELEASES_PATH and not path.startswith(_RELEASES_PATH + "/"):
        return None                                  # only the /releases page or a /releases/... subpath
    return url


class _FetchError(Exception):
    """A bounded metadata-fetch failure carrying a finite :data:`error_code`."""

    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


# ── Immutable status snapshot ────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UpdateStatus:
    """An immutable point-in-time view. ``to_dict`` is the schema_version 1 wire shape (explicit nulls)."""

    runtime_id: str
    revision: int
    enabled: bool
    state: str
    current: str
    latest_tag: Optional[str]
    latest_url: Optional[str]
    checked_at: Optional[str]          # nullable UTC ISO-8601 (completion time), never a monotonic value
    retry_after_seconds: int
    error_code: Optional[str]

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "runtime_id": self.runtime_id,
            "revision": self.revision,
            "enabled": self.enabled,
            "state": self.state,
            "current": self.current,
            "latest_tag": self.latest_tag,
            "latest_url": self.latest_url,
            "checked_at": self.checked_at,
            "retry_after_seconds": self.retry_after_seconds,
            "error_code": self.error_code,
        }


# ── Manual-request outcome ─────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RequestOutcome:
    """Result of :meth:`UpdateChecker.request_check`.

    ``accepted`` is True when the request will produce (or already shares) an in-flight check. ``reason`` is
    one of: ``started`` (a fresh check was scheduled), ``coalesced`` (joined the in-flight check),
    ``cooldown`` (denied; retry after ``retry_after_seconds``), ``closed`` (the runtime is shutting down)."""

    accepted: bool
    reason: str
    retry_after_seconds: int = 0


@dataclass(frozen=True)
class OperationResult:
    """One classification result; offline/error never carries older availability metadata.

    The configured current version remains available through the unchanged schema-1 snapshot.
    ``behind`` is exact for successful classifications and absent for offline/error.
    """

    state: str
    latest_tag: Optional[str]
    latest_url: Optional[str]
    behind: Optional[int]
    checked_at: str
    error_code: Optional[str]


@dataclass(frozen=True)
class OperationView:
    """Exact runtime/operation identity; queued/checking and retired views have no result."""

    runtime_id: str
    operation_id: str
    phase: str
    result: Optional[OperationResult] = None
    retirement_reason: Optional[str] = None


@dataclass(frozen=True)
class OperationRequest:
    """Atomic admission outcome and its exact view, or no view when admission was rejected."""

    outcome: RequestOutcome
    view: Optional[OperationView]


def _valid_operation_id(value: object) -> bool:
    return (type(value) is str and len(value) == 32
            and all(char in "0123456789abcdef" for char in value))


# ── Bounded metadata reader ────────────────────────────────────────────────────────────────────────

def _utc_now_iso(wall_clock: Callable[[], datetime]) -> str:
    return wall_clock().astimezone(timezone.utc).replace(microsecond=0).isoformat()


class _DeadlineSocket:
    """Wraps a socket so EVERY underlying blocking read re-applies the remaining transfer budget (UC-2b).

    A caller-level ``read1`` timeout reset is insufficient for chunked framing: ``HTTPResponse`` reads the
    chunk-size line and the payload through several ``recv``/``recv_into`` calls inside one ``read1``, all using
    the socket timeout that was set once. Installed as the response's underlying socket, this proxy re-applies
    the remaining budget before each recv, so a slow chunk header cannot consume the whole budget and still
    leave the body a full socket timeout. DNS/TLS teardown remain outside this bound (documented)."""

    __slots__ = ("_sock", "_remaining")

    def __init__(self, sock, remaining: Callable[[], float]):
        object.__setattr__(self, "_sock", sock)
        object.__setattr__(self, "_remaining", remaining)

    def _bound(self) -> None:
        rem = object.__getattribute__(self, "_remaining")()
        if rem <= 0:
            raise socket.timeout("update fetch deadline exceeded")
        object.__getattribute__(self, "_sock").settimeout(max(0.001, rem))

    def recv_into(self, *args, **kwargs):
        self._bound()
        return object.__getattribute__(self, "_sock").recv_into(*args, **kwargs)

    def recv(self, *args, **kwargs):
        self._bound()
        return object.__getattribute__(self, "_sock").recv(*args, **kwargs)

    def settimeout(self, _t):
        # The framing layer's own settimeout is superseded by our per-read budget; ignore it.
        return None

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_sock"), name)


def _install_deadline_socket(resp, remaining: Callable[[], float]) -> bool:
    """Best-effort: replace the response's underlying socket with a :class:`_DeadlineSocket` so chunk framing's
    sub-reads are each bounded. Returns True if installed. Only touches ``resp.fp.raw._sock`` (real
    HTTPResponse shape); leaves other response shapes for the per-read1 settimeout fallback."""
    fp = getattr(resp, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if raw is None or sock is None or not hasattr(sock, "recv_into"):
        return False
    try:
        raw._sock = _DeadlineSocket(sock, remaining)
        return True
    except Exception:  # noqa: BLE001 — an unwrappable transport just uses the fallback
        return False


def _response_socket(resp):
    """Best-effort access to the underlying socket of a urllib/http.client response, so the REMAINING budget
    can be applied to each blocking read (UC-2). Returns a socket-like object with ``settimeout`` or None."""
    fp = getattr(resp, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is not None and hasattr(sock, "settimeout"):
        return sock
    if fp is not None and hasattr(fp, "settimeout"):   # a scripted/socket-like fp used in tests
        return fp
    if hasattr(resp, "settimeout"):                    # a socket-like response object
        return resp
    return None


def _is_timeout(exc: BaseException) -> bool:
    """True if *exc* is (or wraps, as urllib's URLError does) a transport timeout — checked by type, not text."""
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (TimeoutError, socket.timeout))


def fetch_releases_bounded(deadline_seconds: float = DEFAULT_FETCH_DEADLINE_SECONDS,
                           *, url: str = updater.RELEASES_API,
                           monotonic: Callable[[], float] = time.monotonic) -> list[dict]:
    """GET the releases list with finite byte/item budgets and a total-transfer deadline.

    Reuses flash_core's SSRF guard + redirect-allowlisted opener (so the fetch and any redirect stay on the
    trusted GitHub host set). Raises :class:`_FetchError` with a finite code on any failure. The remaining
    budget is applied to the underlying socket before EACH read so a slow body cannot outlast it; a bounded
    ``read1`` is used so the budget is enforced per low-level read. If the read contract cannot be bounded
    (no reachable socket and no ``read1``), a finite transport error is returned rather than an unbounded
    read. DNS resolution/TLS teardown are only as bounded as the socket allows — no hard wall is claimed on
    those, and that limitation is deliberately left explicit."""
    start = monotonic()

    def remaining() -> float:
        return deadline_seconds - (monotonic() - start)

    try:
        flash_core._require_allowed_url(url)
    except Exception as exc:  # noqa: BLE001 — a disallowed URL is an offline/unreachable outcome
        raise _FetchError(ERR_OFFLINE) from exc
    req = urllib.request.Request(url, headers=flash_core._UA)
    try:
        if remaining() <= 0:
            raise _FetchError(ERR_TIMEOUT)
        resp = flash_core._OPENER.open(req, timeout=max(0.1, remaining()))
    except _FetchError:
        raise
    except urllib.error.HTTPError as exc:  # a real HTTP status is distinct from offline
        raise _FetchError(ERR_HTTP) from exc
    except urllib.error.URLError as exc:   # urllib wraps a connect timeout as URLError(TimeoutError(...))
        raise _FetchError(ERR_TIMEOUT if _is_timeout(exc) else ERR_OFFLINE) from exc
    except (TimeoutError, socket.timeout) as exc:  # a direct connect/transport timeout
        raise _FetchError(ERR_TIMEOUT) from exc
    except Exception as exc:  # noqa: BLE001 — connect/DNS/TLS failure => offline
        raise _FetchError(ERR_OFFLINE) from exc
    try:
        buf = bytearray()
        reader = getattr(resp, "read1", None)
        # UC-2b: bound chunk-framing's sub-reads by installing a deadline socket. If installed, the per-read1
        # settimeout below is a harmless no-op (the proxy owns the budget); otherwise it is the fallback bound.
        _install_deadline_socket(resp, remaining)
        sock = _response_socket(resp)
        is_closed = getattr(resp, "isclosed", None)
        if reader is None or sock is None:
            # Cannot enforce the per-read budget (no bounded read1, or the socket is unreachable). Return a
            # finite transport failure rather than silently allowing unbounded reads (UC-2).
            raise _FetchError(ERR_OFFLINE)
        while True:
            rem = remaining()
            if rem <= 0:
                raise _FetchError(ERR_TIMEOUT)
            # A content-length/chunked response closes its file+socket the instant the body is fully consumed,
            # so the NEXT read would be EOF over a dead socket. Applying settimeout to that closed socket raises
            # (e.g. Windows 10038) and used to be misreported as offline. When the response reports itself
            # closed, the body is complete — stop here instead of touching the dead socket.
            if is_closed is not None:
                try:
                    if is_closed():
                        break
                except Exception:  # noqa: BLE001 — a response that can't report closure just skips this hint
                    pass
            try:
                sock.settimeout(max(0.001, rem))   # apply the REMAINING budget to this blocking read
            except (OSError, ValueError) as exc:
                raise _FetchError(ERR_OFFLINE) from exc
            try:
                chunk = reader(_READ_CHUNK)
            except (TimeoutError, socket.timeout) as exc:   # a read that timed out -> timeout, not offline
                raise _FetchError(ERR_TIMEOUT) from exc
            except Exception as exc:  # noqa: BLE001 — a mid-transfer failure is offline, not a crash
                raise _FetchError(ERR_OFFLINE) from exc
            if remaining() <= 0:
                # A read (returning data OR EOF) that completed only after the deadline is a timeout, not a
                # success — the previous code accepted a post-deadline EOF and parsed it.
                raise _FetchError(ERR_TIMEOUT)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_JSON_BYTES:   # enforce the ceiling on ACTUAL bytes, not Content-Length
                raise _FetchError(ERR_TOO_LARGE)
        # UC-2a: a Content-Length response that ended before its declared length is INCOMPLETE (RFC 9112 §6.3).
        # HTTPResponse.read1 can reach EOF without raising IncompleteRead; resp.length still records the unmet
        # remaining bytes. A short body is a finite transport failure (offline), not a parseable success.
        declared_remaining = getattr(resp, "length", None)
        if isinstance(declared_remaining, int) and declared_remaining > 0:
            raise _FetchError(ERR_OFFLINE)
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        data = json.loads(bytes(buf).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — malformed body
        raise _FetchError(ERR_PAYLOAD) from exc
    if not isinstance(data, list):
        raise _FetchError(ERR_PAYLOAD)
    if len(data) > MAX_RELEASE_ITEMS:
        # UC-3: silently truncating could DROP a newer release and flip availability to up_to_date. Reject
        # an over-cap response as too_large rather than classify an incomplete list.
        raise _FetchError(ERR_TOO_LARGE)
    return data


def classify_releases(current_version: str, releases: Sequence[dict]) -> "tuple[str, Optional[str], Optional[str]]":
    """Pure classification: (state, latest_tag, latest_url). AVAILABLE when a published (non-draft,
    non-prerelease) release is strictly ahead of *current_version*, else UP_TO_DATE. Tag/URL are the newest
    published release's, bounded; None when there is no published release at all."""
    state, tag, url, _ = _classify_releases_with_count(current_version, releases)
    return state, tag, url


def _classify_releases_with_count(current_version: str, releases: Sequence[dict]) -> tuple:
    """Shared classification pass; the public classifier keeps its original three-item shape."""
    rels = list(releases)
    tags = updater.release_tags(rels)
    behind = updater.behind_count(current_version, tags)
    tag, url = updater._newest(rels)
    tag_out = _cap(tag, MAX_TAG_CHARS) if tag else None
    # UC-5: publish a link ONLY if it validates as this repository's HTTPS release URL; otherwise fall back
    # to the known releases page. Truncation is never a substitute for validation. (Validate first, then cap.)
    safe = _safe_release_url(url) if url else None
    if safe is None and tag_out is not None:
        safe = updater.RELEASES_PAGE
    url_out = _cap(safe, MAX_URL_CHARS) if safe else None
    state = AVAILABLE if behind >= 1 else UP_TO_DATE
    return state, tag_out, url_out, behind


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


# ── The owned checker ──────────────────────────────────────────────────────────────────────────────

class UpdateChecker:
    """One owned availability checker per application runtime. Import- and construction-safe (no thread,
    no network); an explicit :meth:`start` owns a single scheduler thread. All mutable state is in memory."""

    def __init__(self, current_version: str, *,
                 enabled: bool = True,
                 interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
                 manual_cooldown_seconds: float = DEFAULT_MANUAL_COOLDOWN_SECONDS,
                 fetch_deadline_seconds: float = DEFAULT_FETCH_DEADLINE_SECONDS,
                 last_checked_at: Optional[str] = None,
                 fetch: Optional[Callable[[], list[dict]]] = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._current = str(current_version)
        # UC-4: reject NaN/inf/out-of-range scheduling inputs BEFORE any ownership/network work. interval and
        # deadline must be positive; cooldown may be 0 (useful in tests) but never nonfinite.
        self._interval = _finite_number(interval_seconds, "interval_seconds",
                                        minimum=0.0, maximum=_MAX_INTERVAL_SECONDS, allow_min=False)
        self._cooldown = _finite_number(manual_cooldown_seconds, "manual_cooldown_seconds",
                                        minimum=0.0, maximum=_MAX_COOLDOWN_SECONDS, allow_min=True)
        self._deadline = _finite_number(fetch_deadline_seconds, "fetch_deadline_seconds",
                                        minimum=0.0, maximum=_MAX_DEADLINE_SECONDS, allow_min=False)
        self._fetch = fetch if fetch is not None else (
            lambda: fetch_releases_bounded(self._deadline, monotonic=monotonic))
        self._monotonic = monotonic
        self._wall = wall_clock

        self._lock = threading.Lock()
        self._operation_changed = threading.Condition(self._lock)
        self._active_operation: Optional[OperationView] = None
        self._terminal_operation: Optional[OperationView] = None
        self._wake = threading.Event()
        self._entered = threading.Event()   # UC-1: the scheduler thread sets this the instant _run begins
        self._runtime_id = uuid.uuid4().hex
        self._revision = 0
        self._enabled = bool(enabled)
        self._state = IDLE
        self._latest_tag: Optional[str] = None
        self._latest_url: Optional[str] = None
        self._checked_at: Optional[str] = self._sanitize_persisted_iso(last_checked_at)
        self._error_code: Optional[str] = None
        self._retry_after = 0

        self._in_flight = False
        self._manual_pending = False
        self._closed = False
        self._started = False
        self._thread_launched = False       # UC-1: True only once the OS thread is confirmed started
        self._thread: Optional[threading.Thread] = None
        # Seed the auto-check deadline from a valid, recent persisted timestamp so a fresh launch does not
        # re-check immediately, WITHOUT trusting a monotonic value across restart. A malformed/future stamp
        # yields no suppression (never disables checks indefinitely).
        self._last_check_mono: Optional[float] = self._seed_last_check_mono(self._checked_at)

    # -- persisted-timestamp hygiene ----------------------------------

    def _sanitize_persisted_iso(self, raw: Optional[str]) -> Optional[str]:
        """Normalize *raw* to the same explicit UTC ISO-8601 form used for completion timestamps, or None
        (UC-6). A naive (offset-less) or malformed string is rejected; a future timestamp is not evidence of a
        recent check. A valid offset timestamp is converted to UTC so the wire contract is consistent."""
        parsed = self._parse_iso(raw)
        if parsed is None:
            return None
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    def _parse_iso(self, raw: Optional[str]) -> Optional[datetime]:
        """Parse an offset-aware ISO-8601 string that is not in the future. Naive/malformed -> None (UC-6)."""
        if not isinstance(raw, str) or not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            return None        # UC-6: reject non-aware input rather than silently assuming UTC
        try:
            if dt > self._wall().astimezone(timezone.utc):
                return None    # a future timestamp is not evidence of a recent check
        except Exception:  # noqa: BLE001
            return None
        return dt

    def _seed_last_check_mono(self, checked_at: Optional[str]) -> Optional[float]:
        dt = self._parse_iso(checked_at)
        if dt is None:
            return None
        try:
            age = (self._wall().astimezone(timezone.utc) - dt).total_seconds()
        except Exception:  # noqa: BLE001
            return None
        if age < 0 or age >= self._interval:
            return None        # stale or implausible -> allow an immediate launch check
        # Seed so "now - last = interval - remaining", i.e. the first auto-check waits out the remainder only.
        return self._monotonic() - (self._interval - (self._interval - age))

    # -- snapshot -----------------------------------------------------

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    def _snapshot_locked(self) -> UpdateStatus:
        return UpdateStatus(
            runtime_id=self._runtime_id, revision=self._revision, enabled=self._enabled,
            state=self._state, current=self._current, latest_tag=self._latest_tag,
            latest_url=self._latest_url, checked_at=self._checked_at,
            retry_after_seconds=self._retry_after, error_code=self._error_code)

    def snapshot(self) -> UpdateStatus:
        with self._lock:
            return self._snapshot_locked()

    def _visible_locked(self) -> tuple:
        return (self._enabled, self._state, self._latest_tag, self._latest_url,
                self._checked_at, self._retry_after, self._error_code)

    def _bump_if_changed_locked(self, before: tuple) -> None:
        if self._visible_locked() != before:
            self._revision += 1

    def _new_operation_locked(self) -> OperationView:
        operation = OperationView(self._runtime_id, uuid.uuid4().hex, "queued")
        self._active_operation = operation
        self._operation_changed.notify_all()
        return operation

    def _retire_operation_locked(self, reason: str) -> None:
        if self._active_operation is not None:
            self._terminal_operation = replace(
                self._active_operation, phase="retired", result=None, retirement_reason=reason)
            self._active_operation = None
        self._manual_pending = self._in_flight = False
        self._operation_changed.notify_all()

    def _operation_view_locked(self, runtime_id: str, operation_id: str) -> Optional[OperationView]:
        if runtime_id != self._runtime_id:
            return None
        for view in (self._active_operation, self._terminal_operation):
            if view is not None and view.operation_id == operation_id:
                return view
        return None

    def operation_view(self, runtime_id: str, operation_id: str) -> Optional[OperationView]:
        """Pure lookup. Unknown, malformed, foreign or evicted identity never selects another result."""
        if not _valid_operation_id(runtime_id) or not _valid_operation_id(operation_id):
            return None
        with self._lock:
            return self._operation_view_locked(runtime_id, operation_id)

    def wait_operation(self, runtime_id: str, operation_id: str,
                       timeout: float = MAX_OPERATION_WAIT_SECONDS) -> Optional[OperationView]:
        """Wait at most timeout seconds for this identity to settle or disappear.

        A timeout returns its still-pending view; unknown/evicted returns None. The Condition releases
        the checker lock while waiting and uses its real monotonic deadline, independent of an injected
        scheduler clock. This performs no admission, thread creation, fetch or policy work.
        """
        timeout = _finite_number(timeout, "timeout", minimum=0.0,
                                 maximum=MAX_OPERATION_WAIT_SECONDS)
        if not _valid_operation_id(runtime_id) or not _valid_operation_id(operation_id):
            return None
        with self._operation_changed:
            def settled():
                view = self._operation_view_locked(runtime_id, operation_id)
                return view is None or view.phase in {"completed", "retired"}
            self._operation_changed.wait_for(settled, timeout)
            return self._operation_view_locked(runtime_id, operation_id)

    # -- lifecycle ----------------------------------------------------

    def start(self) -> bool:
        """Own the scheduler thread. Idempotent: a second call (or a call after close) does not create a
        second owner. Returns True iff this call owns a launched (or already-entered) worker.

        Ownership is explicit (UC-1/UC-1a). The tentatively-owned thread is recorded, then launched. If
        ``Thread.start`` reports an error, ownership is ALWAYS retained: the native thread may already be
        bootstrapping BEFORE Python sets its ``_started`` event, and ``is_alive()`` reads False in exactly that
        window — so ``is_alive() == False`` is NOT proof the launch had no effect and must never be used to
        clear ownership (that let a retry start a second concurrent scheduler). We keep the thread, mark it
        launched so ``stop`` settles it, and re-raise the original error/control. Only a construction-time
        failure (before ``_started`` is set) is definitely unstarted and leaves a clean retry."""
        with self._lock:
            if self._closed or self._started:
                return False
            try:
                thread = threading.Thread(target=self._run, name="update-checker", daemon=True)
            except BaseException:
                # Construction has no ambiguous native owner. Retire any pre-start admission so its
                # waiters settle; a later explicit start/request may safely retry this same checker.
                self._retire_operation_locked("start_failed")
                raise
            self._thread = thread
            self._started = True
        try:
            thread.start()
        except BaseException:
            # RETAIN ownership unconditionally (see docstring): a reported launch error can coincide with a
            # native thread already bootstrapping. Mark launched so stop() joins/settles it (a join-before-
            # start error remains honest incomplete cleanup), then
            # re-raise the original error/control. Never clear _thread here and never permit a second scheduler.
            with self._lock:
                self._thread_launched = True
                self._closed = True
                self._retire_operation_locked("start_failed")
            self._wake.set()
            raise
        with self._lock:
            self._thread_launched = True
        return True

    def stop(self, timeout: float = _STOP_JOIN_TIMEOUT) -> bool:
        """Signal retirement, then join a LAUNCHED scheduler. Returns True iff cleanup is confirmed complete.
        A join-before-start error cannot prove there is no native owner. An unresolved launch reports
        incomplete (False) while retaining the
        owner, rather than abandoning ownership or falsely claiming completion. Idempotent."""
        with self._lock:
            self._closed = True
            self._retire_operation_locked("closed")
            thread = self._thread
            launched = self._thread_launched
        self._wake.set()
        if thread is None or thread is threading.current_thread():
            return True
        if not launched and not self._entered.is_set():
            # start() is concurrently in flight and the OS thread is not confirmed started — joining could
            # raise. Report honest incomplete; retirement is signaled, so a later stop() confirms cleanup.
            return False
        try:
            thread.join(timeout)
        except RuntimeError:
            # join-before-start: Python's _started event is not set yet. If the thread was LAUNCHED, the native
            # worker may still be bootstrapping BEFORE _started is set (is_alive() reads False in exactly that
            # window) — a join-before-start exception CANNOT prove the launch had no effect. So report an honest
            # incomplete (False) and retain ownership; a later stop() confirms completion once the thread settles
            # and join succeeds. Only a definitely-unlaunched thread would be clean here (the guard above already
            # returns False for the not-launched/not-entered window, so `launched` is True on this path).
            return not launched
        return not thread.is_alive()

    def close(self, timeout: float = _STOP_JOIN_TIMEOUT) -> bool:
        return self.stop(timeout)

    # -- settings policy ----------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        """Disabling stops FUTURE automatic scheduling (an already-entered fetch still finishes + publishes);
        manual checks remain available. Re-enabling wakes the same scheduler and respects cadence."""
        with self._lock:
            if self._closed:
                return
            before = self._visible_locked()
            self._enabled = bool(enabled)
            self._bump_if_changed_locked(before)
        self._wake.set()

    def reset_to_defaults(self) -> None:
        """Re-apply defaults to THIS owner (re-enable + clear any launch suppression), waking the scheduler."""
        with self._lock:
            if self._closed:
                return
            before = self._visible_locked()
            self._enabled = True
            self._last_check_mono = None      # allow a fresh check on the next cycle
            self._bump_if_changed_locked(before)
        self._wake.set()

    # -- manual trigger (runtime-wide coalescing + cooldown) ----------

    def request_check(self) -> RequestOutcome:
        """Manual check. Available even while disabled. Coalesces with an in-flight check and enforces a
        runtime-wide cooldown floor between checks."""
        return self.request_operation().outcome

    def request_operation(self) -> OperationRequest:
        """Atomically admit/coalesce a manual check and return its identity, never a prior terminal."""
        with self._lock:
            if self._closed:
                return OperationRequest(RequestOutcome(False, "closed"), None)
            if self._active_operation is not None:
                return OperationRequest(RequestOutcome(True, "coalesced"), self._active_operation)
            if self._last_check_mono is not None:
                elapsed = self._monotonic() - self._last_check_mono
                if elapsed < self._cooldown:
                    return OperationRequest(RequestOutcome(
                        False, "cooldown", int(math.ceil(self._cooldown - elapsed))), None)
            operation = self._new_operation_locked()
            self._manual_pending = True
        self._wake.set()
        return OperationRequest(RequestOutcome(True, "started"), operation)

    # -- scheduler ----------------------------------------------------

    def _run(self) -> None:
        self._entered.set()   # UC-1: signal (the instant the worker begins) that this thread genuinely runs
        try:
            while True:
                with self._lock:
                    if self._closed:
                        return
                    now = self._monotonic()
                    do_check, wait_for = self._due_locked(now)
                    if do_check:
                        operation = self._active_operation or self._new_operation_locked()
                        self._active_operation = replace(operation, phase="checking")
                        self._operation_changed.notify_all()
                        self._in_flight = True
                        self._manual_pending = False
                        before = self._visible_locked()
                        self._state = CHECKING
                        self._error_code = None
                        self._bump_if_changed_locked(before)
                if do_check:
                    self._do_check_cycle(operation.operation_id)
                    continue
                self._wake.wait(wait_for)
                self._wake.clear()
        except BaseException:  # noqa: BLE001 — UC-1b: an unexpected worker exit (e.g. a control signal from
            # the injected fetch) must not leave a permanent checking/coalesced state backed by a dead thread.
            # Retire the runtime (future requests are rejected) and publish a truthful finite terminal status,
            # then re-raise the ORIGINAL exception (it reaches the thread's excepthook, as it should).
            with self._lock:
                self._closed = True
                self._retire_operation_locked(ERR_WORKER_EXIT)
                before = self._visible_locked()
                if self._state not in (UP_TO_DATE, AVAILABLE, OFFLINE, ERROR):
                    self._state = ERROR
                    self._error_code = ERR_WORKER_EXIT
                self._bump_if_changed_locked(before)
            raise

    def _due_locked(self, now: float) -> "tuple[bool, float]":
        """Decide whether a check is due now, else how long to wait. Caller holds the lock."""
        if self._manual_pending:
            return True, 0.0            # cooldown was already enforced at request time
        if not self._enabled:
            return False, self._interval    # parked until enable/manual/stop wakes us
        if self._last_check_mono is None:
            return True, 0.0            # no prior check -> check now
        elapsed = now - self._last_check_mono
        if elapsed >= self._interval:
            return True, 0.0
        return False, self._interval - elapsed

    def _do_check_cycle(self, operation_id: str) -> None:
        """Run one fetch+classify OUTSIDE the lock, then publish — unless the runtime retired meanwhile."""
        state, tag, url, error_code, behind = self._fetch_and_classify()
        with self._lock:
            self._in_flight = False
            if (self._closed or self._active_operation is None
                    or self._active_operation.operation_id != operation_id):
                return                  # retired runtime: drop the result, do not reschedule or publish
            self._last_check_mono = self._monotonic()
            before = self._visible_locked()
            self._state = state
            self._latest_tag = tag
            self._latest_url = url
            self._error_code = error_code
            self._retry_after = 0
            if state in (UP_TO_DATE, AVAILABLE, OFFLINE, ERROR):
                self._checked_at = _utc_now_iso(self._wall)
            self._bump_if_changed_locked(before)
            successful = state in (UP_TO_DATE, AVAILABLE)
            finite_error = error_code if error_code in {
                ERR_OFFLINE, ERR_TIMEOUT, ERR_TOO_LARGE, ERR_PAYLOAD, ERR_HTTP} else ERR_PAYLOAD
            result = OperationResult(
                state, tag if successful else None, url if successful else None,
                behind if successful else None, self._checked_at, None if successful else finite_error)
            self._terminal_operation = replace(self._active_operation, phase="completed", result=result)
            self._active_operation = None
            self._operation_changed.notify_all()
        self._wake.clear()

    def _fetch_and_classify(self) -> tuple:
        try:
            releases = self._fetch()
        except _FetchError as exc:
            return OFFLINE, self._latest_tag, self._latest_url, exc.error_code, None
        except Exception:  # noqa: BLE001 — an unexpected fetch failure is a clean OFFLINE, not a crash
            return OFFLINE, self._latest_tag, self._latest_url, ERR_OFFLINE, None
        try:
            # The injected fetch has the same finite list contract as the default bounded transport.
            if len(releases) > MAX_RELEASE_ITEMS:
                return ERROR, self._latest_tag, self._latest_url, ERR_TOO_LARGE, None
            state, tag, url, behind = _classify_releases_with_count(self._current, releases)
        except Exception:  # noqa: BLE001 — a classification bug is ERROR, distinct from offline
            return ERROR, self._latest_tag, self._latest_url, ERR_PAYLOAD, None
        return state, tag, url, None, behind
