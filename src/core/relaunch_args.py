"""Correlated sidecar that preserves launch arguments across a Windows self-update relaunch.

The Windows swap helper relaunches the executable with NO arguments (a cmd batch cannot carry
arbitrary argument values without shell interpretation), which would silently drop the user's launch
settings (``--ui web``, ``--port``, ...). To preserve them the updater writes the arguments to a
per-attempt sidecar file next to the executable and passes a single random token only in the
relaunched child's environment; the new process reads the token from its own environment, consumes
the correlated sidecar and applies the arguments.

This module owns the token format, path derivation, size bound and sidecar encoding used by both
the writer (``src.core.self_update``) and the reader (``src.app.main``). It does only pure encoding
and OWN-FILE I/O: it never launches a process or touches the environment (the writer passes the
token into the child's copied env; the reader pops it from its own), and never lists or sweeps
sibling files.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Sequence

# Env var carrying the per-attempt token to the relaunched child. The writer sets it in the child's
# copied environment; the reader pops it from its own before spawning any child of its own.
TOKEN_ENV = "CC_RELAUNCH_TOKEN"
# One fixed token format: secrets.token_hex(16) -> 32 lowercase hex chars.
TOKEN_HEX_LEN = 32
# The one shared bound: refuse to encode/write OR read/decode beyond this.
MAX_SIDECAR_BYTES = 64 * 1024

_TOKEN_RE = re.compile(r"\A[0-9a-f]{" + str(TOKEN_HEX_LEN) + r"}\Z")
_SUFFIX = ".cc-relaunch-"

# RelaunchArgsError.reason codes, for the entrypoint's finite startup diagnostic:
#   malformed-token | missing | oversized | not-a-list | not-strings | collision | io


class RelaunchArgsError(Exception):
    """A sidecar could not be produced or consumed. ``reason`` is a stable code the entrypoint
    maps to a finite startup failure."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def new_token() -> str:
    """A fresh per-attempt token (32 lowercase hex)."""
    return secrets.token_hex(TOKEN_HEX_LEN // 2)


def is_valid_token(value: object) -> bool:
    """True only for a ``str`` in the exact token format (32 lowercase hex)."""
    return isinstance(value, str) and _TOKEN_RE.match(value) is not None


def sidecar_path(exe_path: str, token: str) -> str:
    """Correlated sidecar path for *exe_path* + *token*.

    Validates the token BEFORE deriving any path, so a filename is never built from an unvalidated
    environment value (raises ``malformed-token``)."""
    if not is_valid_token(token):
        raise RelaunchArgsError("malformed-token")
    return os.path.realpath(exe_path) + _SUFFIX + token + ".json"


def encode_args(args: Sequence[str]) -> bytes:
    """Encode *args* as a UTF-8 JSON array of strings; an empty sequence encodes ``[]``.

    Raises ``not-strings`` on a non-str element (or a bare str/bytes passed instead of a list),
    ``oversized`` if the encoding exceeds the bound."""
    if isinstance(args, (str, bytes, bytearray)):
        # a bare string would split into one arg per character
        raise RelaunchArgsError("not-strings")
    items = list(args)
    if any(not isinstance(a, str) for a in items):
        raise RelaunchArgsError("not-strings")
    # ensure_ascii=True keeps encode_args and decode_args symmetric and lets NO raw encoding error
    # escape: a value json.loads accepts (e.g. an escaped lone surrogate UTF-8 cannot encode) is
    # re-emitted as a \uXXXX escape, so the ASCII output always encodes and round-trips.
    data = json.dumps(items, ensure_ascii=True).encode("utf-8")
    if len(data) > MAX_SIDECAR_BYTES:
        raise RelaunchArgsError("oversized")
    return data


def decode_args(data: bytes) -> list[str]:
    """Decode a sidecar payload into a list of strings.

    Raises ``oversized`` past the bound, ``not-a-list`` if not UTF-8 JSON or not a list,
    ``not-strings`` if any element is not a str."""
    if len(data) > MAX_SIDECAR_BYTES:
        raise RelaunchArgsError("oversized")
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        # deeply nested JSON (e.g. thousands of "[") makes json.loads raise RecursionError, not a
        # JSONDecodeError -- treat it as an unparseable payload, not an escaping non-contract error.
        raise RelaunchArgsError("not-a-list") from exc
    if not isinstance(parsed, list):
        raise RelaunchArgsError("not-a-list")
    if any(not isinstance(a, str) for a in parsed):
        raise RelaunchArgsError("not-strings")
    return parsed


def write_sidecar(exe_path: str, token: str, args: Sequence[str]) -> str:
    """Exclusively create the correlated sidecar, write the encoded *args*, and return its path.

    Encoding runs before any file exists (``oversized``/``not-strings`` leave nothing behind). The
    file is created ``O_EXCL`` in binary mode, mode 0o600 (``collision`` if it already exists); any
    other failure removes a partial file and raises ``io``. Empty *args* writes ``[]`` so an issued
    token always has a sidecar (token-issued iff sidecar-written)."""
    path = sidecar_path(exe_path, token)
    data = encode_args(args)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise RelaunchArgsError("collision") from exc
    except OSError as exc:
        raise RelaunchArgsError("io") from exc
    try:
        fh = os.fdopen(fd, "wb")
    except OSError as exc:
        # fdopen failed though os.open succeeded: close the raw fd before removing, else on Windows
        # the open handle keeps the file locked and the cleanup silently fails.
        os.close(fd)
        _quiet_remove(path)
        raise RelaunchArgsError("io") from exc
    try:
        with fh:
            fh.write(data)
    except OSError as exc:
        _quiet_remove(path)
        raise RelaunchArgsError("io") from exc
    return path


def consume_sidecar(exe_path: str, token: str) -> list[str]:
    """Read and DELETE the correlated sidecar, returning its argument list.

    Validates the token first (``malformed-token``). A missing file raises ``missing``. At most
    ``MAX_SIDECAR_BYTES + 1`` bytes are read (an oversized file is refused by :func:`decode_args`).
    The file -- this attempt's own, correlated by token -- is deleted whether decoding succeeds or
    fails; a delete failure raises ``io``. Never lists or sweeps sibling files."""
    path = sidecar_path(exe_path, token)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except FileNotFoundError as exc:
        raise RelaunchArgsError("missing") from exc
    except OSError as exc:
        raise RelaunchArgsError("io") from exc
    try:
        fh = os.fdopen(fd, "rb")
    except OSError as exc:
        os.close(fd)  # close the raw fd before removing (Windows locks the file while it is open)
        _quiet_remove(path)
        raise RelaunchArgsError("io") from exc
    try:
        with fh:
            data = fh.read(MAX_SIDECAR_BYTES + 1)
    except OSError as exc:
        _quiet_remove(path)
        raise RelaunchArgsError("io") from exc
    try:
        return decode_args(data)
    finally:
        _delete_or_raise(path)


def _quiet_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _delete_or_raise(path: str) -> None:
    try:
        os.remove(path)
    except OSError as exc:
        raise RelaunchArgsError("io") from exc
