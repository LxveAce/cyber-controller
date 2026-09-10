"""Request-shape handler for the offline SigMF metadata summarize route (Reform > Offline Data).

Pure of Flask globals: it takes the request's content type and content length plus a bounded body reader and
the ACCEPTED SigMF summarizer callable, validates the request against fixed limits, runs the summarizer on the
bounded bytes, and returns ``(status, body_bytes, headers)``. The Flask route in ``app.py`` is a thin adapter
that supplies ``request.content_type``, the strictly validated raw ``CONTENT_LENGTH`` (via
:func:`validated_content_length`, not the framework-normalized ``request.content_length``), a reader over the
raw WSGI input, and ``src.core.sigmf_metadata.summarize_sigmf_metadata`` as ``summarize``.

Boundaries this enforces:

* Only the caller-supplied request BYTES are read -- never a filename or a client-provided filesystem path.
* Descriptor bytes are capped at the parser's 262144-byte limit, which EQUALS the application's global
  ``MAX_CONTENT_LENGTH`` (256 KiB): this route does NOT widen the global cap. A declared ``Content-Length``
  above the cap is rejected (413) BEFORE any body read; it is validated up front from the raw
  ``CONTENT_LENGTH`` (an invalid/negative declaration -> 411, not a framework-normalized 0).
* The body is read as EXACTLY the validated declared length N (0 <= N <= cap), one bounded chunk at a time,
  requesting only the bytes still outstanding, and never probing past the declared framing (PEP 3333). An EOF
  before N bytes is a fixed ``incomplete-body`` failure raised BEFORE any parsing. N == 0 reads nothing and is
  summarized as the ordinary empty input.
* Error responses carry a FIXED code string with no supplied value. Raw bytes, filenames, paths and free text
  are never logged or persisted here, and nothing is sent outward. The SigMF parser is INJECTED (this module
  imports no parser) so tests can load it by file path without importing the app path; there is no second
  parser and no schema change.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional, Tuple

# The route cap is the accepted parser's own descriptor limit, which is also the app-wide MAX_CONTENT_LENGTH,
# so the global request cap already protects this route and is not widened here.
MAX_DESCRIPTOR_BYTES = 262144
_CHUNK_BYTES = 64 * 1024
_ACCEPTED_MEDIA_TYPE = "application/octet-stream"
_JSON_NO_STORE = {"Content-Type": "application/json", "Cache-Control": "no-store"}

Result = Tuple[int, bytes, Dict[str, str]]


def validated_content_length(raw: Optional[str]) -> Optional[int]:
    """Strictly parse the ORIGINAL WSGI ``CONTENT_LENGTH`` field to a bounded non-negative int, or ``None``.

    Parsed from the raw field rather than ``request.content_length`` because Werkzeug normalizes an
    invalid/negative declaration to ``0`` (indistinguishable from a valid empty body); keeping the raw field
    lets an invalid declaration stay ``None`` (the handler returns 411) while a valid ``0`` stays a no-read
    empty body. The decimal TEXT is compared to the cap first (after normalizing leading zeros) and integer
    conversion runs ONLY on a value already known to fit, so the interpreter's integer string-conversion limit
    is never approached. A syntactically valid over-cap declaration saturates to ``MAX_DESCRIPTOR_BYTES + 1``
    (its exact magnitude is irrelevant -- the handler rejects any over-cap length with a fixed 413 before
    reading). Non-digit / signed / empty / non-ASCII -> ``None``."""
    if not isinstance(raw, str) or not raw or not raw.isascii() or not raw.isdigit():
        return None
    digits = raw.lstrip("0") or "0"          # normalize leading zeros without any integer conversion
    cap = str(MAX_DESCRIPTOR_BYTES)
    if len(digits) > len(cap) or (len(digits) == len(cap) and digits > cap):
        return MAX_DESCRIPTOR_BYTES + 1      # over-cap: saturate so the handler returns fixed 413 (never reads)
    return int(digits)                       # bounded to <= len(cap) digits, so conversion is always safe


class _IncompleteBody(Exception):
    """EOF arrived before the declared length was read -- a truncated/aborted upload."""


class _InvalidBodyStream(Exception):
    """The reader returned a non-bytes value or more than was requested (a non-conformant input stream)."""


def _error(status: int, code: str) -> Result:
    """A fixed-code JSON error. The code is a constant label; no request value is ever interpolated."""
    return status, json.dumps({"error": code}).encode("utf-8"), dict(_JSON_NO_STORE)


def _read_declared_body(read_body: Callable[[int], bytes], length: int) -> bytes:
    """Read EXACTLY *length* bytes, requesting only the bytes still outstanding and never probing past the
    declared framing. *length* is pre-validated in ``[0, MAX_DESCRIPTOR_BYTES]``; ``length == 0`` reads
    nothing. A short read before *length* is a premature EOF (``_IncompleteBody``); a reader that returns
    non-bytes or more than requested is a non-conformant stream (``_InvalidBodyStream``)."""
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


# Lossless display contract: JavaScript JSON.parse rounds integers beyond +/-(2**53 - 1). Any such integer
# anywhere in the summary is serialized as its exact decimal STRING so the browser displays the true value
# instead of a silently rounded number. The accepted parser is unchanged; this affects only the JSON this
# route emits (its serialization identity), never the parser's schema. Values within JS-safe range stay
# numbers.
_JS_MAX_SAFE_INT = 2 ** 53 - 1


def _display_safe(obj: Any) -> Any:
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return str(obj) if (obj > _JS_MAX_SAFE_INT or obj < -_JS_MAX_SAFE_INT) else obj
    if isinstance(obj, list):
        return [_display_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _display_safe(v) for k, v in obj.items()}
    return obj


def summarize_response(*, content_type: Optional[str], content_length: Optional[int],
                       read_body: Callable[[int], bytes],
                       summarize: Callable[[bytes], Dict[str, Any]]) -> Result:
    """Validate the request shape and produce the metadata-summary response. ``read_body(limit)`` returns at
    most ``limit`` bytes from the raw request body; ``summarize`` is the accepted
    ``summarize_sigmf_metadata(str|bytes)`` callable. Never raises on ordinary input. A successfully processed
    request is HTTP 200 whatever the parser status (summarized / unsupported / invalid) -- HTTP 4xx is reserved
    for transport/framing problems."""
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media != _ACCEPTED_MEDIA_TYPE:
        return _error(415, "unsupported-content-type")

    # Content-Length must be present and a non-negative int (``type(x) is int`` excludes bool). An over-cap
    # declaration is rejected here, BEFORE any body read.
    if type(content_length) is not int or content_length < 0:
        return _error(411, "length-required")
    if content_length > MAX_DESCRIPTOR_BYTES:
        return _error(413, "payload-too-large")

    try:
        data = _read_declared_body(read_body, content_length)
    except _IncompleteBody:
        return _error(400, "incomplete-body")
    except _InvalidBodyStream:
        return _error(400, "invalid-body")

    # Accepted parser: bytes -> deterministic dict, never raises on ordinary bytes, bounded output. A genuine
    # internal programming error is deliberately NOT caught here (it must surface, not silently 200).
    result = summarize(data)
    return 200, json.dumps(_display_safe(result)).encode("utf-8"), dict(_JSON_NO_STORE)
