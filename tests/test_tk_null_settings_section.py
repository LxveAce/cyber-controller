"""A JSON-null section in settings.json must not crash the Tk Lite UI's settings load/save.

Regression: _on_load_settings did settings.get("serial", {}).get(...) — but when the key is present
with a null value the {} default is skipped, so serial was None and .get() raised AttributeError out of
__init__ (via _build_settings_tab), so the whole Lite UI failed to launch. The "ui" section has no
default, so _deep_merge can't coerce it; the consumer must. Built via __new__ so no Tk display is
needed (same pattern as test_tk_serial_baud_autoconnect)."""
from __future__ import annotations

import pytest

pytest.importorskip("tkinter")


class _Combo:
    """Minimal combobox stand-in: .get/.set plus cget/getitem/setitem for 'values'."""

    def __init__(self, value=None):
        self._value = value
        self._values = ()

    def get(self):
        return self._value

    def set(self, v):
        self._value = v

    def cget(self, _name):
        return self._values

    def __getitem__(self, _k):
        return self._values

    def __setitem__(self, _k, v):
        self._values = v


class _Var:
    def __init__(self, value=None):
        self._value = value

    def set(self, v):
        self._value = v

    def get(self):
        return self._value


def _app():
    import src.ui.tk.app as tkapp
    app = tkapp.TkLightApp.__new__(tkapp.TkLightApp)
    app._settings_baud = _Combo("115200")
    app._settings_port = _Combo("")
    app._settings_autoconnect = _Var(False)
    app._settings_theme = _Var("Dark")
    return tkapp, app


def test_on_load_settings_survives_null_serial_and_ui(monkeypatch):
    tkapp, app = _app()
    monkeypatch.setattr(tkapp, "_HAS_SETTINGS", True)
    monkeypatch.setattr(tkapp, "load_settings", lambda: {"serial": None, "ui": None})
    # Was: AttributeError: 'NoneType' object has no attribute 'get' -> Lite UI failed to launch.
    tkapp.TkLightApp._on_load_settings(app)
    assert app._settings_baud.get() == "115200"   # fell back to the default baud
    assert app._settings_theme.get() == "Dark"     # "dark".capitalize()


def test_on_save_settings_survives_null_serial_section(monkeypatch):
    tkapp, app = _app()
    saved = {}
    monkeypatch.setattr(tkapp, "_HAS_SETTINGS", True)
    monkeypatch.setattr(tkapp, "load_settings", lambda: {"serial": None, "ui": None})
    monkeypatch.setattr(tkapp, "save_settings", lambda s: saved.update(s))
    monkeypatch.setattr(tkapp, "messagebox", type("M", (), {
        "showinfo": staticmethod(lambda *a, **k: None),
        "showerror": staticmethod(lambda *a, **k: None),
        "showwarning": staticmethod(lambda *a, **k: None)}))
    app._settings_baud = _Combo("9600")
    app._settings_port = _Combo("")
    app._settings_autoconnect = _Var(True)
    app._settings_theme = _Var("Light")
    # Was: TypeError: 'NoneType' object does not support item assignment (setdefault returned None).
    tkapp.TkLightApp._on_save_settings(app)
    assert saved["serial"]["default_baud"] == 9600
    assert saved["ui"]["theme"] == "light"
