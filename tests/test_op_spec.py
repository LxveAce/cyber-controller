"""Pure op-spec derivations (op_spec.py) — the CommandInfo -> OperationDetail seam (Spade P2).

Qt-free: these shape a firmware command into the Biscuit OperationDetail's help / modes / command
string, so the widget wiring (and the web renderer) build the SAME op UX without re-deriving it.
"""
from __future__ import annotations

from src.core import op_spec, safety


class _CI:
    """A minimal stand-in for a firmware CommandInfo."""
    def __init__(self, name, description="", args="", stream=False):
        self.name = name
        self.description = description
        self.args = args
        self.stream = stream


def test_help_spec_shape_and_danger_matches_safety():
    ci = _CI("deauth", "Deauthenticate a target AP", "-t <bssid>")
    spec = op_spec.op_help_spec(ci)
    assert spec["title"] == "deauth"
    assert spec["description"] == "Deauthenticate a target AP"
    assert spec["args"] == "-t <bssid>"
    # the help danger is the SAME classify verdict the send path enforces (never a divergent label)
    assert spec["danger"] == safety.classify("deauth", ci)


def test_modes_run_when_argless_manual_when_args():
    assert op_spec.op_modes(_CI("scan")) == ["Run"]
    assert op_spec.op_modes(_CI("deauth", args="-t <bssid>")) == ["Manual"]


def test_command_resolution():
    ci = _CI("deauth", args="-t <bssid>")
    assert op_spec.op_command(ci) == "deauth"                       # bare verb (no arg)
    assert op_spec.op_command(ci, "-t AA:BB") == "deauth -t AA:BB"  # verb + arg
    # the operator retyped the verb (the old dialog seeded it) -> never doubled
    assert op_spec.op_command(ci, "deauth -t AA:BB") == "deauth -t AA:BB"
    assert op_spec.op_command(_CI("scan"), "  ") == "scan"          # blank arg -> bare verb


def test_module_is_qt_free():
    import inspect
    src = inspect.getsource(op_spec)
    assert "PyQt5" not in src and "QtWidgets" not in src
