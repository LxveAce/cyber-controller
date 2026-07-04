"""tk Remote view — the Tkinter mirror of the web touch-first Remote home. Runs headless on a hidden Tk root.

Asserts the same guarantees the web Remote gives: it renders the SAME UI-agnostic quick-command catalog,
fires a command through the injected send, gates a flagged command behind a confirm (label-never-block), never
fabricates a command (all come from grouped_quick_commands), and degrades to an empty panel on an unknown fw.
"""
from __future__ import annotations

import pytest

from src.core.quick_commands import grouped_quick_commands, quick_commands_for


@pytest.fixture(scope="module")
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:  # pragma: no cover — only on a truly headless CI with no Tk
        pytest.skip("no display for tkinter")
    root.withdraw()
    yield root
    root.destroy()


def _view(tk_root, firmware=None, sent=None, confirm=None, firmwares=None):
    from src.ui.tk.remote_view import RemoteView
    send = (lambda cmd: sent.append(cmd)) if sent is not None else None
    return RemoteView(tk_root, firmware=firmware, send=send, confirm=confirm, firmwares=firmwares)


def test_loads_quick_commands_matching_catalog(tk_root):
    v = _view(tk_root, firmware="marauder")
    shown = [c for c, _d in v.shown_commands()]
    catalog = [qc.command for qc in quick_commands_for("marauder")]
    assert shown == catalog and len(shown) > 0


def test_safe_command_sends_real_command(tk_root):
    sent = []
    v = _view(tk_root, firmware="marauder", sent=sent)
    safe = next(c for c, d in v.shown_commands() if not d)
    v.activate(safe)
    assert sent == [safe]


def test_flagged_command_requires_confirm(tk_root):
    sent, asked = [], []
    def deny(danger, cmd): asked.append((danger, cmd)); return False
    v = _view(tk_root, firmware="marauder", sent=sent, confirm=deny)
    flagged, danger = next((c, d) for c, d in v.shown_commands() if d)
    v.activate(flagged)
    assert asked == [(danger, flagged)] and sent == []          # deny → not sent

    sent2 = []
    v2 = _view(tk_root, firmware="marauder", sent=sent2, confirm=lambda d, c: True)
    v2.activate(flagged)
    assert sent2 == [flagged]                                   # allow → sent


def test_every_flagged_command_across_firmwares_requires_confirm(tk_root):
    """Fail-open guard (label-never-block): EVERY flagged one-tap command in EVERY firmware must gate on
    confirm — deny → 0 sends, allow → exactly 1 send."""
    from src.ui.tk.remote_view import firmwares_with_quick_commands
    checked = 0
    for fw in firmwares_with_quick_commands():
        flagged = [c for c, d in [(qc.command, qc.danger) for qc in quick_commands_for(fw)] if d]
        for cmd in flagged:
            checked += 1
            v = _view(tk_root, firmware=fw, sent=(deny_sent := []), confirm=lambda d, c: False)
            v.activate(cmd)
            assert deny_sent == [], f"{fw}:{cmd!r} SENT without confirm (fail-open!)"
            v2 = _view(tk_root, firmware=fw, sent=(allow_sent := []), confirm=lambda d, c: True)
            v2.activate(cmd)
            assert allow_sent == [cmd], f"{fw}:{cmd!r} not sent on allow"
    assert checked >= 20, f"expected many flagged commands, only checked {checked}"


def test_unknown_firmware_degrades(tk_root):
    v = _view(tk_root, firmware="no-such-fw")
    assert v.shown_commands() == []


def test_switching_firmware_rebuilds(tk_root):
    v = _view(tk_root, firmware="marauder")
    assert v.shown_commands()
    v.set_firmware("bruce")
    assert [c for c, _ in v.shown_commands()] == [qc.command for qc in quick_commands_for("bruce")]


def test_send_error_does_not_crash(tk_root):
    from src.ui.tk.remote_view import RemoteView
    def boom(_cmd): raise ConnectionError("no active connection")
    v = RemoteView(tk_root, firmware="marauder", send=boom)
    safe = next(c for c, d in v.shown_commands() if not d)
    v.activate(safe)
    assert "no active connection" in v._status.cget("text")


def test_no_phantom_commands(tk_root):
    # every command the Remote can fire must exist in the firmware's real protocol catalog
    for fw in ("marauder", "ghost-esp", "esp32-div", "bruce"):
        v = _view(tk_root, firmware=fw)
        catalog = {qc.command for qc in quick_commands_for(fw)}
        for cmd, _d in v.shown_commands():
            assert cmd in catalog, f"{fw}: phantom command {cmd!r}"


def test_activate_unknown_command_is_safe(tk_root):
    sent = []
    v = _view(tk_root, firmware="marauder", sent=sent)
    v.activate("definitely not a real command")     # no match → no crash, no send
    assert sent == []


def _all_buttons(widget):
    import tkinter as tk
    out = []
    for ch in widget.winfo_children():
        if isinstance(ch, tk.Button):
            out.append(ch)
        out.extend(_all_buttons(ch))
    return out


def test_button_invoke_fires_distinct_commands(tk_root):
    """Fire through the REAL tk.Button (not the headless activate()) — guards the lambda q=qc closure:
    a classic closure-in-loop bug would bind every button to the last command and pass activate()-based tests."""
    sent = []
    v = _view(tk_root, firmware="marauder", sent=sent, confirm=lambda d, c: True)
    buttons = _all_buttons(v._inner)
    assert len(buttons) == len(v.shown_commands()) > 0       # one button per shown command
    for b in buttons:
        b.invoke()
    assert set(sent) == {c for c, _ in v.shown_commands()}   # distinct binding (bug → set size 1)
    assert len(sent) == len(buttons)


def test_zero_command_firmware_shows_notice(tk_root):
    v = _view(tk_root, firmware="no-such-fw")
    texts = []
    for w in v._inner.winfo_children():
        try:
            texts.append(w.cget("text"))
        except Exception:
            pass
    assert any("No one-tap commands" in t for t in texts)


def test_switch_destroys_old_buttons(tk_root):
    v = _view(tk_root, firmware="marauder")
    old = _all_buttons(v._inner)
    assert old
    v.set_firmware("bruce")
    assert all(not b.winfo_exists() for b in old)            # no stale buttons wired to the old firmware
    assert len(_all_buttons(v._inner)) == len(v.shown_commands())
