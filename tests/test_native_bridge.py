"""Native file-picker specs (the Qt-free part of the QWebChannel desktop bridge, #14). Verifies the
kind->(title, filter) mapping + the safe generic fallback headless — PyQtWebEngine isn't installable
on the test rig, so this is where the bridge's load-bearing logic is actually covered by CI."""
from __future__ import annotations

from src.ui.web import native_bridge as nb


def test_known_kinds_have_specific_filters():
    for kind in ("firmware", "wordlist", "os_image", "capture"):
        title, filt = nb.open_picker_spec(kind)
        assert title and filt
        assert nb.is_known_kind(kind)
        # every filter ends with an All-files escape hatch so an odd extension stays selectable
        assert "All files (*)" in filt


def test_filters_target_the_right_extensions():
    assert ".bin" in nb.open_picker_spec("firmware")[1]
    assert ".txt" in nb.open_picker_spec("wordlist")[1]
    os_filter = nb.open_picker_spec("os_image")[1]
    assert ".img" in os_filter and ".iso" in os_filter
    assert ".pcapng" in nb.open_picker_spec("capture")[1]


def test_unknown_kind_degrades_to_generic_not_raises():
    # A typo on the JS side must never crash the native slot — worst case is a generic dialog.
    title, filt = nb.open_picker_spec("bogus-kind")
    assert title == "Select file" and filt == "All files (*)"
    assert nb.open_picker_spec("")[1] == "All files (*)"
    assert not nb.is_known_kind("bogus-kind")


def test_desktop_qt_module_imports_without_qtwebengine():
    # The QtWebEngine imports are lazy (inside launch_desktop_qt), so importing the module must not
    # require PyQtWebEngine — it degrades to an error-message return only when actually launched.
    import importlib

    mod = importlib.import_module("src.ui.web.desktop_qt")
    assert hasattr(mod, "launch_desktop_qt")
