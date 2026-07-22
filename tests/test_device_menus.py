"""Device-View menu model (extracted, UI-agnostic) — real menu trees, separator-tolerant resolution, and
per-leaf danger tags via the shared safety classifier. Pure Python (no Qt)."""
from __future__ import annotations

from src.core.device_menus import SKINS, MenuNode, menu_tree, resolve_skin


def _leaves(nodes):
    for n in nodes:
        if "children" in n:
            yield from _leaves(n["children"])
        else:
            yield n


def test_resolve_skin_tolerates_separator_drift():
    assert resolve_skin("marauder") == "marauder"
    assert resolve_skin("ghost-esp") == "ghostesp"   # device_detect emits a different separator
    assert resolve_skin("esp32-div") == "esp32div"
    assert resolve_skin("GHOSTESP") == "ghostesp"
    assert resolve_skin("flipper") is None           # real firmware, but no reconstructed skin
    assert resolve_skin(None) is None and resolve_skin("") is None


def test_menu_tree_shape_and_realness():
    for fw in SKINS:
        tree = menu_tree(fw)
        assert tree["firmware"] == fw and tree["title"] and tree["root"]
        for leaf in _leaves(tree["root"]):
            assert "label" in leaf and "command" in leaf and "needs_arg" in leaf and "danger" in leaf
            assert leaf["danger"] in ("", "lab-only", "illegal-tx")


def test_menu_tree_unknown_firmware_is_none():
    assert menu_tree("no-such-fw") is None
    assert menu_tree(None) is None


def test_needs_arg_leaves_preserved():
    bruce = {leaf["command"]: leaf for leaf in _leaves(menu_tree("bruce")["root"])}
    assert bruce["subghz tx_from_file"]["needs_arg"] is True
    assert bruce["badusb run_from_file <script>"]["needs_arg"] is True
    assert bruce["info"]["needs_arg"] is False


def test_danger_tags_flag_offensive_not_passive():
    # (Stock ESP32-DIV is now touch-only — no command leaves — so the menu-tree danger-tag
    # mechanism is exercised via GhostESP here; DIV verb danger is covered in test_fix_esp32_div.)
    ghost = {leaf["command"]: leaf["danger"] for leaf in _leaves(menu_tree("ghostesp")["root"])}
    assert ghost["attack -d"] == "lab-only"            # offensive deauth flagged (was phantom "probe")
    assert ghost["startportal"] == "lab-only"          # danger via the protocol's CommandInfo/category
    assert ghost["stopportal"] == "" and ghost["scanap"] == ""   # cease + scan stay safe


def test_reexported_from_device_view_for_qt_importers():
    # device_view re-exports the moved names so cardputer_remote / main_window / tests keep working.
    import importlib
    dv = importlib.import_module("src.ui.qt.device_view")
    assert dv.MenuNode is MenuNode and dv.SKINS is SKINS
    assert callable(dv.marauder_menu) and callable(dv.bruce_menu)


def test_menu_tree_command_parity_with_builders():
    """menu_tree must serialize EXACTLY the builders' leaves (no dropped/renamed command) — extraction lock."""
    def builder_leaves(nodes):
        for n in nodes:
            if n.is_menu:
                yield from builder_leaves(n.children)
            else:
                yield (n.command, n.needs_arg)
    for fw, (_title, factory) in SKINS.items():
        from_builder = sorted(builder_leaves(factory()))
        from_tree = sorted((leaf["command"], leaf["needs_arg"]) for leaf in _leaves(menu_tree(fw)["root"]))
        assert from_tree == from_builder, f"{fw}: menu_tree drifted from the builder"


def test_every_offensive_leaf_across_all_skins_is_flagged():
    """Fail-open guard: known active-offense commands must be labelled in EVERY skin (label-never-block)."""
    offensive = {
        "attack -t deauth", "attack -t beacon -r", "attack -t rickroll", "attack -t probe", "blespam -t all",
        "attack -d", "beaconspam -r", "beaconspam -rr", "probe", "startportal",
        "deauth", "deauth all", "beacon", "rickroll", "blespam", "nrf jam",
    }
    for fw in SKINS:
        by = {leaf["command"]: leaf["danger"] for leaf in _leaves(menu_tree(fw)["root"])}
        for cmd, dng in by.items():
            if cmd in offensive:
                assert dng, f"{fw}: offensive command {cmd!r} is unflagged (label fail-open)"
