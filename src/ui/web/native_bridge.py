"""Native file-picker specs for the QWebChannel desktop bridge.

Pure, Qt-free data + helpers so the mapping from a picker ``kind`` (what the reform UI asks for) to
its OS-dialog title + file filter is unit-testable headless. The Qt glue in
:mod:`src.ui.web.desktop_qt` feeds these to ``QFileDialog``; the browser build never touches this
module (there is no native dialog on the web path — the UI falls back to a manual text field there).

Keeping this separate means the load-bearing part — which extensions each picker offers, and that an
unknown kind degrades to a safe generic "All files" rather than raising — is covered by CI even
though ``PyQtWebEngine`` is not installable on the headless test rig.
"""
from __future__ import annotations

# kind -> (dialog title, Qt getOpenFileName filter). The filter's first group is the default; a
# trailing "All files (*)" always lets the operator override if their file has an odd extension.
NATIVE_OPEN_PICKERS: dict[str, tuple[str, str]] = {
    "firmware": ("Select firmware image", "Firmware (*.bin *.zip *.uf2 *.hex);;All files (*)"),
    "wordlist": ("Select wordlist", "Wordlists (*.txt *.lst *.dic *.gz);;All files (*)"),
    "os_image": ("Select OS image", "Disk images (*.img *.iso *.gz *.xz *.zip);;All files (*)"),
    "capture": ("Select capture", "Captures (*.pcap *.pcapng *.cap *.hc22000);;All files (*)"),
}

# Safe fallback for a kind the bridge doesn't recognize — never raise into the Qt slot.
_DEFAULT_OPEN = ("Select file", "All files (*)")


def open_picker_spec(kind: str) -> tuple[str, str]:
    """Return ``(dialog_title, file_filter)`` for an open-file picker *kind*.

    An unknown/empty kind degrades to a generic "All files" picker rather than raising, so a typo on
    the JS side can never crash the native slot — the worst case is a less-specific dialog.
    """
    return NATIVE_OPEN_PICKERS.get(kind, _DEFAULT_OPEN)


def is_known_kind(kind: str) -> bool:
    """True if *kind* has a dedicated spec (vs. falling back to the generic picker)."""
    return kind in NATIVE_OPEN_PICKERS
