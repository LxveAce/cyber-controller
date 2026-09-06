"""Single-use credentials shared only by the local desktop shell and its server."""

from __future__ import annotations

import secrets
import threading
from enum import Enum


class BootstrapResult(Enum):
    ABSENT = "absent"
    INVALID = "invalid"
    CONSUMED = "consumed"


class DesktopBootstrap:
    """Keep at most one pending token, with atomic rotation and consumption.

    The desktop shell owns this object and may rotate it when changing renderers.
    HTTP handlers can only consume the current token; there is no issuance route.
    A string seed preserves existing desktop callers that supply their own token.
    """

    def __init__(self, token: str | None = None) -> None:
        self._lock = threading.Lock()
        self._token = token.encode("utf-8") if token else None

    def rotate(self) -> str:
        """Issue a fresh token and invalidate any preceding unused token."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._token = token.encode("ascii")
        return token

    def invalidate(self) -> None:
        """Retire any unused credential when its local runtime closes."""
        with self._lock:
            self._token = None

    def consume(self, candidate: str) -> BootstrapResult:
        """Compare and consume under one lock, so concurrent requests cannot win twice."""
        with self._lock:
            if self._token is None:
                return BootstrapResult.ABSENT
            if not candidate or not secrets.compare_digest(candidate.encode("utf-8"), self._token):
                return BootstrapResult.INVALID
            self._token = None
            return BootstrapResult.CONSUMED
