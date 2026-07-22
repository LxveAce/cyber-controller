"""tk Device View — the Tkinter mirror of the Qt device_view / web /device. Runs headless on a hidden Tk root.

Asserts the same guarantees the Qt/web views give: it renders the SAME UI-agnostic menu model, navigates
(descend / back / breadcrumb), fires a leaf's real command through the injected send, gates a flagged leaf
behind a confirm (label-never-block), never sends a needs_arg leaf, and never crashes on a send error / no skin.
"""
from __future__ import annotations

import pytest

from src.core.device_menus import menu_tree


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


def _view(tk_root, firmware=None, sent=None, confirm=None):
    from src.ui.tk.device_view import DeviceView
    send = (lambda cmd: sent.append(cmd)) if sent is not None else None
    return DeviceView(tk_root, firmware=firmware, send=send, confirm=confirm)


def _index_of(view, label_startswith):
    for i, node in enumerate(view.current_items()):
        if node["label"].startswith(label_startswith):
            return i
    raise AssertionError(f"no menu item starting {label_startswith!r} in {[n['label'] for n in view.current_items()]}")


def test_loads_skin_and_lists_top_menu(tk_root):
    v = _view(tk_root, firmware="marauder")
    labels = [n["label"] for n in v.current_items()]
    assert labels == [n["label"] for n in menu_tree("marauder")["root"]]
    assert "ESP32 Marauder" in v.breadcrumb()


def test_navigate_descend_and_back(tk_root):
    v = _view(tk_root, firmware="marauder")
    v.activate(_index_of(v, "WiFi"))                 # descend into WiFi
    assert "WiFi" in v.breadcrumb()
    labels = [n["label"] for n in v.current_items()]
    assert "Attacks" in labels and "Scan APs" in labels
    v._on_back()                                     # back to top
    assert v.breadcrumb().endswith("Marauder") or "›" not in v.breadcrumb()
    assert [n["label"] for n in v.current_items()] == [n["label"] for n in menu_tree("marauder")["root"]]


def test_safe_leaf_sends_real_command(tk_root):
    sent = []
    v = _view(tk_root, firmware="marauder", sent=sent)
    v.activate(_index_of(v, "Device"))               # Device submenu
    v.activate(_index_of(v, "Info"))                 # leaf → "info"
    assert sent == ["info"]


def test_flagged_leaf_requires_confirm(tk_root):
    # (Stock ESP32-DIV is now touch-only with no flagged leaves; use GhostESP's deauth, a real
    # lab-only leaf. illegal-tx confirm is covered by the safety tests + the DIV serial-fork grid.)
    # deny path: confirm returns False → NOT sent (but the option to proceed always exists)
    sent, asked = [], []
    def deny(danger, cmd): asked.append((danger, cmd)); return False
    v = _view(tk_root, firmware="ghostesp", sent=sent, confirm=deny)
    v.activate(_index_of(v, "WiFi"))
    v.activate(_index_of(v, "Attacks"))
    v.activate(_index_of(v, "Deauth"))               # attack -d, lab-only
    assert asked and asked[0][0] == "lab-only" and sent == []

    # allow path: confirm returns True → sent
    sent2, allow = [], (lambda danger, cmd: True)
    v2 = _view(tk_root, firmware="ghostesp", sent=sent2, confirm=allow)
    v2.activate(_index_of(v2, "WiFi"))
    v2.activate(_index_of(v2, "Attacks"))
    v2.activate(_index_of(v2, "Deauth"))
    assert sent2 == ["attack -d"]


def test_needs_arg_leaf_never_sends(tk_root):
    sent = []
    v = _view(tk_root, firmware="bruce", sent=sent, confirm=lambda d, c: True)
    v.activate(_index_of(v, "Sub-GHz"))
    v.activate(_index_of(v, "Replay from File"))     # needs_arg → shown, not fired
    assert sent == []


def test_send_error_does_not_crash(tk_root):
    from src.ui.tk.device_view import DeviceView
    def boom(_cmd): raise ConnectionError("no active connection")
    v = DeviceView(tk_root, firmware="marauder", send=boom)
    v.activate(_index_of(v, "Device"))
    v.activate(_index_of(v, "Info"))                 # send raises → view stays alive, shows the error
    assert "no active connection" in v._status.cget("text")


def test_no_skin_firmware_degrades(tk_root):
    v = _view(tk_root, firmware="flipper")           # real fw, no reconstructed skin
    assert v.current_items() == [] and v.breadcrumb() == ""


def test_activate_out_of_range_is_safe(tk_root):
    sent = []
    v = _view(tk_root, firmware="marauder", sent=sent)
    v.activate(99)                                   # no such index → no crash, no send
    v.activate(-1)
    assert sent == []


def test_reuses_shared_menu_model_no_duplication(tk_root):
    # the tk view must drive the SAME model as Qt/web — every command it can fire exists in menu_tree
    v = _view(tk_root, firmware="ghostesp")
    def leaves(nodes):
        for n in nodes:
            if "children" in n:
                yield from leaves(n["children"])
            else:
                yield n["command"]
    model_cmds = set(leaves(menu_tree("ghostesp")["root"]))
    v.activate(_index_of(v, "WiFi"))
    v.activate(_index_of(v, "Attacks"))
    for node in v.current_items():
        assert node["command"] in model_cmds


def _flagged_paths(firmware):
    """(path-of-indices, command, danger) for every flagged, sendable (non-needs_arg) leaf in a skin."""
    def walk(nodes, path):
        for i, n in enumerate(nodes):
            if "children" in n:
                yield from walk(n["children"], path + [i])
            elif n.get("danger") and not n.get("needs_arg"):
                yield path + [i], n["command"], n["danger"]
    yield from walk(menu_tree(firmware)["root"], [])


def test_every_flagged_leaf_across_all_skins_requires_confirm(tk_root):
    """Fail-open guard (label-never-block): EVERY flagged leaf in EVERY skin must gate on confirm —
    deny → 0 sends, allow → exactly 1 send. Navigated the real widget down to each leaf."""
    from src.core.device_menus import SKINS
    checked = 0
    for fw in SKINS:
        for path, cmd, danger in _flagged_paths(fw):
            checked += 1
            asked = []
            v = _view(tk_root, firmware=fw, sent=(deny_sent := []),
                      confirm=lambda d, c, _a=asked: (_a.append((d, c)), False)[1])
            for idx in path[:-1]:
                v.activate(idx)
            v.activate(path[-1])
            assert deny_sent == [], f"{fw}:{cmd!r} SENT without confirm (deny path — fail-open!)"
            assert asked == [(danger, cmd)], f"{fw}:{cmd!r} confirm not called with {danger!r}"

            v2 = _view(tk_root, firmware=fw, sent=(allow_sent := []), confirm=lambda d, c: True)
            for idx in path[:-1]:
                v2.activate(idx)
            v2.activate(path[-1])
            assert allow_sent == [cmd], f"{fw}:{cmd!r} not sent on allow"
    # (Stock ESP32-DIV's offensive leaves moved off the device-view skins to the serial-fork grid,
    # so the SKINS menus now carry ~13 flagged leaves — still "many", all confirm-gated.)
    assert checked >= 12, f"expected many flagged leaves, only checked {checked}"


def test_listbox_event_path_fires_leaf(tk_root):
    # exercise the REAL UI entry point (_on_activate, bound to <Double-Button-1>/<Return>), not just activate()
    sent = []
    v = _view(tk_root, firmware="marauder", sent=sent)
    v.activate(_index_of(v, "Device"))
    info_idx = _index_of(v, "Info")
    v._list.selection_clear(0, "end")
    v._list.selection_set(info_idx)
    v._on_activate(None)
    assert sent == ["info"]


def test_switching_firmware_resets_navigation(tk_root):
    v = _view(tk_root, firmware="marauder")
    v.activate(_index_of(v, "WiFi"))
    assert "WiFi" in v.breadcrumb()
    v.set_firmware("bruce")                           # switch mid-navigation
    assert v._path == [] and "Bruce" in v.breadcrumb()
    assert [n["label"] for n in v.current_items()] == [n["label"] for n in menu_tree("bruce")["root"]]
