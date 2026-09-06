"""Regression coverage for access-gate prompt selection.

The active native UI name is ``desktop``.  A stale legacy-only check made that
mode fall through to the console prompt even though the Qt unlock dialog is the
appropriate access-gate surface.
"""

from src import app
from src.security import access_gate
from src.security.access_gate import _uses_gui_unlock


def test_current_desktop_mode_uses_gui_unlock():
    assert _uses_gui_unlock("desktop") is True


def test_browser_mode_keeps_console_unlock():
    assert _uses_gui_unlock("web") is False


def test_launcher_and_legacy_native_aliases_remain_compatible():
    assert _uses_gui_unlock(None) is True
    for ui in ("qt", "qtweb", "webview", "gui", "tk"):
        assert _uses_gui_unlock(ui) is True


def test_desktop_launch_receives_the_bootstrap_audit_trail(monkeypatch):
    """The desktop path must retain the same durable trail as browser mode."""
    audit = object()

    class DeviceManagerStub:
        def shutdown(self):
            pass

    services = (DeviceManagerStub(), object(), object(), object(),
                object(), object(), object(), audit)
    received = {}

    def launch(*args, **kwargs):
        received.update(kwargs)
        return 0

    monkeypatch.setattr(app, "_acquire_instance_lock", lambda: True)
    monkeypatch.setattr(app, "_bootstrap", lambda: services)
    monkeypatch.setattr(access_gate, "enforce", lambda ui: True)
    monkeypatch.setitem(app._LAUNCHERS, "desktop", launch)

    assert app.main(["--ui", "desktop"]) == 0
    assert received["audit"] is audit
