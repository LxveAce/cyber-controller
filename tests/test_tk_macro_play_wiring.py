"""Regression — tk Macros Play + advertised keyboard shortcuts (subsystem: ui-alt / tk).

Two confirmed defects in ``src/ui/tk/app.py``:

  #1 ``_on_macro_play`` was miswired against ``MacroRecorder.play``'s async contract. play() defaults
     to ``async_=True``: it runs on its own daemon thread and returns immediately, reporting completion
     AND send errors ONLY through ``complete_callback`` (it never raises). The tk handler wrapped play()
     in its own thread and passed NO callback, so (a) the Play button was never disabled — a second
     click silently re-entered play() and was dropped; (b) the 'Playing...' status flipped back to
     'Idle' milliseconds after the click while the macro was still running; and (c) a mid-playback send
     failure surfaced no dialog (the ``except`` around play() was dead code). The Qt tab already used
     the callback contract correctly. The fix mirrors it: call play() directly with a
     ``complete_callback``, disable Play up front, and reset status / re-enable Play / show any error
     from the callback (marshalled onto the Tk loop via ``after``).

  #2 Help > Keyboard Shortcuts advertised Ctrl+Tab / Ctrl+Shift+Tab / F5, none of which were bound
     (the only key binding in the app was Ctrl+Q). The fix registers the real bindings:
     ``Notebook.enable_traversal()`` for Ctrl+Tab / Ctrl+Shift+Tab and an ``<F5>`` binding that
     refreshes ports + targets.

Builds a real ``TkLightApp`` on a visible root; skips when there is no display for Tk.
"""
from __future__ import annotations

import types

import pytest

import src.ui.tk.app as app_module


@pytest.fixture(scope="module")
def _window():
    # Build the full TkLightApp exactly once. Creating a fresh tk.Tk() root per test can
    # intermittently raise TclError (multiple roots in one interpreter), so the window is shared.
    tk = pytest.importorskip("tkinter")
    from src.core.cross_comm import EventBus, TargetPool
    from src.core.device_manager import DeviceManager
    from src.core.flash_engine import FlashEngine

    bus = EventBus()
    try:
        instance = app_module.TkLightApp(
            DeviceManager(), FlashEngine(), bus, TargetPool(bus)
        )
    except tk.TclError:  # pragma: no cover - only on a truly headless box with no Tk
        pytest.skip("no display for tkinter")

    yield instance
    try:
        instance._root.destroy()
    except tk.TclError:  # pragma: no cover
        pass


@pytest.fixture
def app(_window, tmp_path):
    """Reset the shared window to a clean, playable state for each test."""
    import tkinter as tk

    from src.core.macro_recorder import Macro, MacroRecorder, MacroStep

    # Isolate the recorder to a temp dir and give it exactly one saved macro to play.
    rec = MacroRecorder(macros_dir=tmp_path)
    rec.save_macro(Macro(name="Reboot", steps=[MacroStep(command="reboot", delay_ms=0)]))
    _window._macro_recorder = rec
    _window._refresh_macro_list()
    _window._macro_listbox.selection_clear(0, tk.END)
    _window._macro_listbox.selection_set(0)
    # A minimal active connection so _on_macro_play reaches the play() call.
    _window._active_conn = types.SimpleNamespace(write=lambda _cmd: None)
    # Reset transient UI state a prior test may have left behind.
    _window._btn_macro_play.configure(state=tk.NORMAL)
    _window._macro_status_label.configure(text="Idle")
    _window._macro_variables = {}
    return _window


# ── #1 Macro Play wiring ─────────────────────────────────────────────────────

def test_play_disables_button_and_wires_complete_callback(app):
    """Play must disable itself for the run and hand play() a real completion callback."""
    captured: dict = {}

    def fake_play(macro, send_command=None, variables=None, complete_callback=None, **kw):
        captured["complete_callback"] = complete_callback

    app._macro_recorder.play = fake_play

    app._on_macro_play()

    # Disabled synchronously so a second click can't re-enter play() (which would be silently dropped).
    assert str(app._btn_macro_play["state"]) == "disabled"
    assert app._macro_status_label["text"] == "Playing..."
    cb = captured.get("complete_callback")
    assert cb is not None, "play() must receive a complete_callback (was wired as if synchronous)"


def test_play_resets_status_and_button_on_success(app):
    """When the recorder reports success, status returns to Idle and Play is re-enabled."""
    captured: dict = {}

    def fake_play(macro, send_command=None, variables=None, complete_callback=None, **kw):
        captured["complete_callback"] = complete_callback

    app._macro_recorder.play = fake_play
    app._on_macro_play()

    cb = captured["complete_callback"]
    cb(True, "Playback complete")   # recorder finished on its playback thread
    app._root.update()              # run the after(0, _finish) marshalled onto the Tk loop

    assert app._macro_status_label["text"] == "Idle"
    assert str(app._btn_macro_play["state"]) == "normal"


def test_play_send_error_surfaces_dialog(app, monkeypatch):
    """A mid-playback send failure (reported via complete_callback) must show a Playback error dialog.

    Previously the error path was dead code: play() returned instantly without raising and no callback
    was passed, so the failure was only logged inside MacroRecorder and never reached the user.
    """
    errors: list = []
    monkeypatch.setattr(app_module.messagebox, "showerror", lambda *a, **k: errors.append(a))

    captured: dict = {}

    def fake_play(macro, send_command=None, variables=None, complete_callback=None, **kw):
        captured["complete_callback"] = complete_callback

    app._macro_recorder.play = fake_play
    app._on_macro_play()

    cb = captured.get("complete_callback")
    # Old code passed no callback: there was no path from a send failure to the user at all.
    if cb is not None:
        cb(False, "Send error at step 1: device disconnected")
        app._root.update()

    assert errors, "a failing playback must surface a 'Playback error' dialog"
    assert "Playback error" in errors[0][1]
    # Play is re-enabled even after a failure so the user can retry.
    assert str(app._btn_macro_play["state"]) == "normal"


# ── #2 Advertised keyboard shortcuts are actually bound ──────────────────────

def test_f5_is_bound_and_refreshes_ports_and_targets(app, monkeypatch):
    """F5 (advertised in Help) must be bound and refresh both the port list and the target pool."""
    assert app._root.bind_all("<F5>"), "F5 was advertised in Help but never bound"

    calls: list = []
    monkeypatch.setattr(app, "_refresh_ports", lambda: calls.append("ports"))
    monkeypatch.setattr(app, "_refresh_targets", lambda: calls.append("targets"))

    app._root.focus_force()
    app._root.update()
    app._root.event_generate("<F5>", when="now")
    app._root.update()

    assert calls == ["ports", "targets"], f"F5 did not refresh ports + targets (ran {calls})"


def test_ctrl_tab_traversal_is_enabled(app):
    """Ctrl+Tab / Ctrl+Shift+Tab (advertised in Help) require Notebook.enable_traversal().

    enable_traversal registers the cycle bindings on the notebook's toplevel; without it those
    advertised shortcuts do nothing.
    """
    fwd = app._root.bind("<Control-Key-Tab>")
    back = app._root.bind("<Shift-Control-Key-Tab>")
    assert "TLCycleTab" in fwd, "Ctrl+Tab is advertised in Help but traversal was never enabled"
    assert "TLCycleTab" in back, "Ctrl+Shift+Tab is advertised in Help but traversal was never enabled"
