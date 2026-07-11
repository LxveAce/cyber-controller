"""Process-wide activity-log bus — one Qt signal every subsystem emits to and the persistent
terminal subscribes to.

Before this, each activity source (flashing, command execution, broadcasts, crack runs,
background routing) dead-ended in its OWN tab's private log widget, so the always-visible bottom
terminal only ever showed serial RX. This bus fixes that: any source calls
``activity_log().emit_line(...)`` and the terminal (and anything else subscribed) sees it.

Why a QObject and not the plain ``cross_comm.EventBus``: a ``pyqtSignal`` auto-marshals a
cross-thread emit onto the RECEIVER's (GUI) thread via a queued connection — exactly the
thread-safety the non-Qt EventBus lacks. Flash/crack/broadcast all emit from background threads,
and touching a widget from those threads is undefined behaviour. Kept dependency-light
(``PyQt5.QtCore`` only) so it stays import-safe under the frozen PyInstaller build.

The subscriber escapes untrusted text — device/tool bytes are untrusted, so the terminal
``html.escape``s the message and trusts only its own colour span (the same anti-forgery model as
the serial path). This module only carries plain strings.
"""
from __future__ import annotations

from PyQt5.QtCore import QObject, pyqtSignal

# Recognised levels (drive the terminal colour). Unknown levels render in the muted default.
LEVELS = ("info", "warn", "error", "success")


class ActivityLog(QObject):
    """A single app-wide signal carrying one activity line: ``(source, level, text)``.

    ``source`` is a short tag the terminal shows in brackets (e.g. ``"flash"``, ``"crack"``,
    ``"broadcast"``, ``"cmd"``, ``"macro"``); ``level`` is one of :data:`LEVELS`; ``text`` is the
    human-readable line. Emit from any thread via :meth:`emit_line`.
    """

    line = pyqtSignal(str, str, str)  # (source, level, text)

    def emit_line(self, source: str, text: str, level: str = "info") -> None:
        """Publish one activity line. Safe to call from any thread (the connection queues it to the
        subscriber's thread). A blank *text* is dropped so callers can emit unconditionally."""
        if not text:
            return
        self.line.emit(source, level if level in LEVELS else "info", text)


_singleton: ActivityLog | None = None


def _alive(obj: ActivityLog) -> bool:
    """True if the QObject's underlying C++ side is still valid. A cached QObject can outlive its
    C++ object (sip/PyQt teardown, e.g. between tests); touching it then raises RuntimeError."""
    try:
        obj.objectName()
        return True
    except RuntimeError:
        return False


def activity_log() -> ActivityLog:
    """Return the process-wide :class:`ActivityLog` singleton (created on first use, and re-created
    if its C++ object was torn down).

    First-touch it on the GUI thread (the persistent terminal does, at window build) so its object
    affinity is the GUI thread; emits from worker threads then queue correctly to it.
    """
    global _singleton
    if _singleton is None or not _alive(_singleton):
        _singleton = ActivityLog()
    return _singleton
