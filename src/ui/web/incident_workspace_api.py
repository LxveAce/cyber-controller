"""Request-shape handler for the offline incident-import route (HUNT > Incidents workspace).

Pure of Flask globals: it takes the request's content type and content length plus a bounded body reader,
validates them against fixed limits, runs the accepted ``import_incidents``/``export_bytes`` core, and returns
``(status, body_bytes, headers)``. The Flask route in ``app.py`` is a thin adapter that supplies
``request.content_type``, the strictly validated raw ``CONTENT_LENGTH`` (via :func:`validated_content_length`,
not the framework-normalized ``request.content_length``), and a reader over the raw WSGI input.

Boundaries this enforces:

* Only the caller-supplied request BYTES are read — never a filename or a client-provided filesystem path.
* A route-specific 2 MiB cap, independent of the application's global ``MAX_CONTENT_LENGTH`` (256 KiB, left
  intact for every other endpoint). It does NOT rely on a per-request ``request.max_content_length`` setter
  (not dependable across the declared Flask>=3.0 range): the declared ``Content-Length`` is validated up front
  and an over-cap declaration is rejected BEFORE any read.
* The body is then read as EXACTLY the validated declared length N (0 <= N <= 2 MiB), one bounded chunk at a
  time, requesting only the bytes still outstanding and accumulating partial reads. Per PEP 3333 an application
  must not read past ``CONTENT_LENGTH`` — the server may simulate EOF there — so the handler never probes for a
  byte beyond the declared framing, and detecting an over-declared/under-declared body is the server's job, not
  this route's. An EOF before N bytes (e.g. a client that disconnected mid-upload) is a fixed ``incomplete-body``
  failure raised BEFORE any parsing. N == 0 reads nothing and yields the ordinary empty report.
* Error responses carry a FIXED code string with no supplied value. Raw bytes, filenames, paths and free text
  are never logged or persisted here — the report returned is the accepted default-redacted projection. A byte
  bound is not a timeout; server framing and timeouts are separate concerns outside this handler.
"""
from __future__ import annotations

import json
from typing import Callable, Dict, Optional, Tuple

from src.core.incident_export import export_bytes
from src.core.incident_import import MAX_FILE_BYTES, import_incidents

# The route body cap is the accepted core's own file limit, so the core never receives more than it validates.
ROUTE_MAX_BYTES = MAX_FILE_BYTES  # 2 MiB
_CHUNK_BYTES = 64 * 1024
_ACCEPTED_MEDIA_TYPE = "application/octet-stream"
_JSON_NO_STORE = {"Content-Type": "application/json", "Cache-Control": "no-store"}

Result = Tuple[int, bytes, Dict[str, str]]


def validated_content_length(raw: Optional[str]) -> Optional[int]:
    """Strictly parse the ORIGINAL WSGI ``CONTENT_LENGTH`` field to a bounded non-negative int, or ``None``.

    The raw field is parsed here rather than trusting ``request.content_length`` for two reasons:

    * Werkzeug normalizes an invalid/negative declaration to ``0`` — indistinguishable from a valid empty body.
      Keeping the raw field lets an invalid declaration stay ``None`` (the handler returns 411) while a valid
      ``0`` stays a no-read empty body.
    * ``int(raw)`` on an arbitrarily long digit string can raise from the interpreter's integer string-conversion
      limit BEFORE the route reaches its fixed error handling. So the decimal TEXT is compared to the route cap
      first (after normalizing leading zeros), and integer conversion runs ONLY on a value already known to fit
      — at most the cap's digit count — so the conversion limit is never approached.

    A syntactically valid declaration above the cap is saturated to ``ROUTE_MAX_BYTES + 1``: its exact magnitude
    is irrelevant because the unchanged handler rejects any over-cap length with a fixed 413 before reading, so
    the true over-cap value is deliberately never computed. Non-digit / signed / empty / non-ASCII -> ``None``."""
    if not isinstance(raw, str) or not raw or not raw.isascii() or not raw.isdigit():
        return None
    digits = raw.lstrip("0") or "0"          # normalize leading zeros without any integer conversion
    cap = str(ROUTE_MAX_BYTES)
    if len(digits) > len(cap) or (len(digits) == len(cap) and digits > cap):
        return ROUTE_MAX_BYTES + 1           # over-cap: saturate so the handler returns fixed 413 (never reads)
    return int(digits)                       # bounded to <= len(cap) digits, so conversion is always safe


class _IncompleteBody(Exception):
    """EOF arrived before the declared length was read — a truncated/aborted upload."""


class _InvalidBodyStream(Exception):
    """The reader returned a non-bytes value or more than was requested (a non-conformant input stream)."""


def _error(status: int, code: str) -> Result:
    """A fixed-code JSON error. The code is a constant label; no request value is ever interpolated."""
    return status, json.dumps({"error": code}).encode("utf-8"), dict(_JSON_NO_STORE)


def _read_declared_body(read_body: Callable[[int], bytes], length: int) -> bytes:
    """Read EXACTLY *length* bytes, requesting only the bytes still outstanding and never probing past the
    declared framing. *length* is pre-validated in ``[0, ROUTE_MAX_BYTES]``; ``length == 0`` reads nothing.
    A short read before *length* is a premature EOF (``_IncompleteBody``); a reader that returns non-bytes or
    more than requested is a non-conformant stream (``_InvalidBodyStream``)."""
    data = bytearray()
    while len(data) < length:
        requested = min(_CHUNK_BYTES, length - len(data))
        chunk = read_body(requested)
        if not isinstance(chunk, (bytes, bytearray)) or len(chunk) > requested:
            raise _InvalidBodyStream
        if not chunk:
            raise _IncompleteBody
        data.extend(chunk)
    return bytes(data)


def import_response(*, content_type: Optional[str], content_length: Optional[int],
                    read_body: Callable[[int], bytes]) -> Result:
    """Validate the request shape and produce the redacted report response. ``read_body(limit)`` returns at most
    ``limit`` bytes from the raw request body. Never raises on ordinary input."""
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media != _ACCEPTED_MEDIA_TYPE:
        return _error(415, "unsupported-content-type")

    # Content-Length must be present and a non-negative int (``type(x) is int`` excludes bool). An over-cap
    # declaration is rejected here, BEFORE any body read.
    if type(content_length) is not int or content_length < 0:
        return _error(411, "length-required")
    if content_length > ROUTE_MAX_BYTES:
        return _error(413, "payload-too-large")

    try:
        data = _read_declared_body(read_body, content_length)
    except _IncompleteBody:
        return _error(400, "incomplete-body")
    except _InvalidBodyStream:
        return _error(400, "invalid-body")

    # Accepted core: never raises on ordinary bytes; returns the normalized, default-redacted projection.
    return 200, export_bytes(import_incidents(data)), dict(_JSON_NO_STORE)
