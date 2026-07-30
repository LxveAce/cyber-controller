"""Spade v2 navigation model (src/core/nav_model.py) — pure, Qt-free IA contract.

Locks the design invariants (CC-SPADE-DESIGN §2): the surface set is hard-capped at 5 job-verbs +
a pinned Settings; the spec round-trips to JSON; and honest-functionality is STRUCTURAL — a
capability-gated node (Sense) is absent from the tree until a provider backs it. No Qt.
"""
from __future__ import annotations

from src.core import nav_model as nm

_VERB_KEYS = ("rig", "hunt", "operate", "crack", "map")


def test_five_job_surfaces_in_mission_order():
    keys = [s.key for s in nm.surfaces() if s.capability_key is None]
    # exactly the 5 always-present job-verbs, left-to-right as the mission arc
    assert keys == list(_VERB_KEYS)


def test_settings_is_separate_from_the_five():
    assert nm.settings_node().key == "settings"
    assert "settings" not in [s.key for s in nm.surfaces()]


def test_no_placeholder_browse_tiles():
    # nrf/nfc browse tiles were dropped (honest-functionality): they must not appear anywhere.
    all_keys = set()

    def walk(n):
        all_keys.add(n.key)
        for c in n.children:
            walk(c)
    for s in nm.surfaces():
        walk(s)
    assert "nrf" not in all_keys and "nfc" not in all_keys


def test_sense_is_capability_gated_hidden_by_default():
    # Sense is designed into the IA but must be ABSENT until a real "sense" provider exists.
    assert any(s.key == "sense" and s.capability_key == "sense" for s in nm.surfaces())
    visible_default = [s.key for s in nm.visible_nav()]
    assert "sense" not in visible_default
    assert visible_default == list(_VERB_KEYS)


def test_sense_appears_when_its_capability_is_provided():
    visible = [s.key for s in nm.visible_nav({"sense"})]
    assert "sense" in visible
    assert len(visible) == 6


def test_nav_spec_round_trips_to_json():
    import json
    spec = nm.nav_spec()
    # must be JSON-serializable with no Qt/object leakage, and reload equal
    dumped = json.dumps(spec)
    assert json.loads(dumped) == spec
    assert spec["version"] == 2
    assert [s["key"] for s in spec["surfaces"]] == [*_VERB_KEYS, "sense"]
    assert spec["settings"]["key"] == "settings"


def test_every_surface_declares_a_primary_action():
    # each job-surface has a headline op (the floating Start/Stop docks to it)
    for s in nm.surfaces():
        assert s.primary_action, f"{s.key} has no primary_action"


def test_module_is_qt_free():
    import inspect
    src = inspect.getsource(nm)
    assert "PyQt5" not in src and "QtWidgets" not in src and "import Qt" not in src
