"""P3 flow-spine substrate — src/core/flow_intent.py (pure, Qt-free)."""
from __future__ import annotations

import dataclasses
import inspect

from src.core import flow_intent as fi
from src.core import nav_model as nm


def test_flowintent_is_frozen_immutable():
    intent = fi.FlowIntent("crack", "load_capture", object(), sub_view="crack_lab")
    assert intent.surface_key == "crack" and intent.action == "load_capture"
    assert intent.sub_view == "crack_lab" and intent.auto is False
    try:
        intent.surface_key = "hunt"   # frozen -> must raise
        raised = False
    except dataclasses.FrozenInstanceError:
        raised = True
    assert raised, "FlowIntent must be immutable (frozen dataclass)"


def test_defaults_are_navigate_and_deliver_only():
    intent = fi.FlowIntent("map")
    assert intent.action == "" and intent.object_ref is None
    assert intent.sub_view is None and intent.auto is False   # auto defaults off: never auto-acts


def test_is_routable_present_vs_absent():
    keys = ["rig", "hunt", "operate", "crack", "map", "settings"]
    assert fi.is_routable(fi.FlowIntent("crack", sub_view="crack_lab"), keys) is True
    assert fi.is_routable(fi.FlowIntent("nope"), keys) is False
    assert fi.is_routable(fi.FlowIntent(""), keys) is False


def test_sense_intent_is_inert_by_construction():
    # Honesty shared with the rail: an intent to the capability-gated 'sense' surface (no provider)
    # is NOT routable, exactly as visible_nav() drops it from the tree.
    visible = [n.key for n in nm.visible_nav(set())] + [nm.settings_node().key]
    assert "sense" not in visible
    assert fi.is_routable(fi.FlowIntent("sense", sub_view="detect"), visible) is False
    # ...but routable the moment a real provider backs it.
    provided = [n.key for n in nm.visible_nav({"sense"})] + [nm.settings_node().key]
    assert fi.is_routable(fi.FlowIntent("sense"), provided) is True


def test_module_is_qt_free():
    src = inspect.getsource(fi)
    assert "PyQt5" not in src and "QtWidgets" not in src and "import Qt" not in src
