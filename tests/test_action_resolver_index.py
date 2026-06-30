"""{index} substitution + guard + source-restriction in ActionResolver (coverage P3).

A scan index is only valid for the device that produced it, so index-based actions are: (a) dropped when no
index is known (instead of sending a literal/guessed {index}), and (b) only offered by the discovering device.
"""

from __future__ import annotations

import types


def _actions():
    from src.models.action import TargetAction
    from src.models.target import TargetType
    return {
        TargetType.AP: [
            TargetAction("Deauth", "attack -t deauth", "deauth", pre_commands=["select -a {index}"]),
            TargetAction("Info", "info", "ap info"),  # non-index action, always applicable
        ]
    }


def _resolver(monkeypatch, target_actions):
    from src.core import action_resolver as AR
    mod = types.SimpleNamespace(TARGET_ACTIONS=target_actions)
    monkeypatch.setattr(AR, "get_protocol_module", lambda name: mod)
    dev = types.SimpleNamespace(port="COM3", firmware="marauder", name="marauder")
    dm = types.SimpleNamespace(list_connected=lambda: [dev])
    return AR.ActionResolver(dm)


def test_index_action_dropped_without_index(monkeypatch):
    from src.models.target import Target, TargetType
    r = _resolver(monkeypatch, _actions())
    t = Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, device_source="COM3")
    names = [a.name for a in r.resolve(t).get("COM3", [])]
    assert "Info" in names           # non-index action still offered
    assert "Deauth" not in names     # index action dropped (no literal {index} sent)


def test_index_substituted_for_discovering_device(monkeypatch):
    from src.models.target import Target, TargetType
    r = _resolver(monkeypatch, _actions())
    t = Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, device_source="COM3")
    t.extra["index"] = 3
    deauth = next(a for a in r.resolve(t)["COM3"] if a.name == "Deauth")
    assert deauth.pre_commands == ["select -a 3"]   # {index} -> 3


def test_index_action_source_restricted(monkeypatch):
    from src.models.target import Target, TargetType
    r = _resolver(monkeypatch, _actions())
    # index known, but a DIFFERENT device discovered it -> COM3 must not offer the index action
    t = Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, device_source="COM9")
    t.extra["index"] = 3
    assert "Deauth" not in [a.name for a in r.resolve(t).get("COM3", [])]


def test_render_strips_control_chars_from_ssid():
    # A scanned SSID with an embedded control char must not survive into a {ssid} command (it would trip
    # SerialConnection.write's injection guard). The resolver render path now strips control chars.
    from src.core.action_resolver import ActionResolver
    from src.models.action import TargetAction
    from src.models.target import Target, TargetType
    a = TargetAction("Beacon", "attack -t beacon -s {ssid}", "beacon clone")
    t = Target(mac="AA:BB:CC:DD:EE:FF", target_type=TargetType.AP, ssid="Cof\x07fee\x00Shop")
    rendered = ActionResolver(None)._render_action(a, t)
    assert rendered.command_template == "attack -t beacon -s CoffeeShop"
