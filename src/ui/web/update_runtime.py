"""Manual update availability and the resources owned by one web application lifetime."""
from __future__ import annotations

import threading

from src.core.update_checker import AVAILABLE, ERROR, OFFLINE, UP_TO_DATE, UpdateChecker
from src.ui.web.server_lifecycle import raise_cleanup_failure


def valid_identity(value: object) -> bool:
    return (type(value) is str and len(value) == 32
            and all(char in "0123456789abcdef" for char in value))


class UpdateAvailability:
    """A protocol view over an injected checker; construction and reads never start work."""

    def __init__(self, checker: UpdateChecker, *, legacy_wait_seconds: float = 25.0):
        if (isinstance(legacy_wait_seconds, bool)
                or not isinstance(legacy_wait_seconds, (int, float))
                or not 0 <= legacy_wait_seconds <= 25):
            raise ValueError("legacy wait must be between zero and 25 seconds")
        self.checker = checker
        self.legacy_wait_seconds = legacy_wait_seconds

    def terminal(self, view):
        if view.phase != "completed" or view.result is None:
            return None
        result = view.result
        current = self.checker.snapshot().current
        if result.state == OFFLINE:
            return {"ok": False, "status": "OFFLINE", "current": current}
        if result.state not in (UP_TO_DATE, AVAILABLE):
            return None
        return {"ok": True, "status": "NEWER" if result.state == AVAILABLE else "UP_TO_DATE",
                "current": current, "behind": result.behind,
                "latest_tag": result.latest_tag or "",
                "latest_url": (result.latest_url or "") if result.state == AVAILABLE else ""}

    def wire_view(self, view):
        wire = {"schema_version": 2, "runtime_id": view.runtime_id,
                "operation_id": view.operation_id, "phase": view.phase,
                "result": self.terminal(view), "retirement_reason": view.retirement_reason}
        if view.phase == "retired":
            wire["error"] = "retired"
        elif view.phase == "completed" and view.result is not None and view.result.state == ERROR:
            wire["error"] = "classification_error"
        return wire


class WebRuntimeCleanup:
    """Retain one runtime's unfinished cleanup, including a factory that never returned an app."""

    def __init__(self, callbacks, hub, history=None):
        self.callbacks = callbacks
        self.hub = hub
        self.history = history
        self.checker = None
        self.app_begin = self.app_finish = None
        self._close_lock = threading.Lock()
        self._fence_lock = threading.Lock()
        self._fenced = set()
        self._checker_done = self._app_done = self._hub_done = self._history_done = False
        self.closed = False

    def attach_app(self, app):
        self.app_begin = app.extensions["cc_begin_close"]
        self.app_finish = app.extensions["cc_finish_close"]
        app.config["cc_runtime_cleanup"] = self

    def begin_close(self):
        errors = []
        with self._fence_lock:
            if self.closed:
                return
            # The application fence always precedes any drain, and no failed fence skips the checker.
            stages = [("callbacks", self.callbacks.fence)]
            if self.app_begin is not None:
                stages.append(("app", self.app_begin))
            for name, fence in stages:
                if name not in self._fenced:
                    try:
                        fence()
                        self._fenced.add(name)
                    except BaseException as exc:
                        errors.append(exc)
            if self.checker is not None and not self._checker_done:
                try:
                    if self.checker.stop(0):
                        self._checker_done = True
                except BaseException as exc:
                    errors.append(exc)
            if "hub" not in self._fenced:
                try:
                    self.hub.fence()
                    self._fenced.add("hub")
                except BaseException as exc:
                    errors.append(exc)
        if errors:
            raise_cleanup_failure(self, errors, "web runtime fencing did not complete")

    def close(self):
        with self._close_lock:
            if self.closed:
                return
            errors = []
            try:
                self.begin_close()
            except BaseException as exc:
                errors.append(exc)
            # This bounded join is independent of, and precedes, the app's indefinite commit drain.
            if self.checker is not None and not self._checker_done:
                try:
                    if not self.checker.stop(5):
                        raise RuntimeError("update worker cleanup is incomplete")
                    self._checker_done = True
                except BaseException as exc:
                    errors.append(exc)
            if not self._app_done:
                try:
                    if self.app_finish is None:
                        self.callbacks.close()
                    else:
                        self.app_finish()
                    self._app_done = True
                except BaseException as exc:
                    errors.append(exc)
            # The hub is a dependency of admitted callbacks/commits, not an independent teardown.
            if self._app_done and not self._hub_done:
                try:
                    self.hub.close()
                    self._hub_done = True
                except BaseException as exc:
                    errors.append(exc)
            # The memory history journal is the last owned resource. An already-admitted ingestion
            # callback may still hold its final submit until the hub has drained, so history closes
            # ONLY after the hub, through the core's own bounded close. An unresolved close (a
            # self-close from an active callback, or an incomplete drain) leaves the SAME owner
            # pending for a later retry of this exact stage — never a replacement journal, never a
            # dropped row or handle. No history (disabled/non-memory) resolves this stage at once.
            if self._hub_done and not self._history_done:
                try:
                    result = self.history.close() if self.history is not None else None
                    if result is None or (result.resolved and result.lock_released):
                        self._history_done = True
                    else:
                        raise RuntimeError("BLE history journal cleanup is incomplete")
                except BaseException as exc:
                    errors.append(exc)
            if errors:
                raise_cleanup_failure(self, errors, "web runtime cleanup did not complete")
            self.closed = True
