"""Quick-command catalog (MB Remote) — real protocol commands only, correctly classified, no phantoms."""
from __future__ import annotations

from src.core.quick_commands import QuickCommand, grouped_quick_commands, quick_commands_for
from src.protocols import get_protocol


def _real_names(firmware):
    return {getattr(c, "name", "") for c in get_protocol(firmware).get_commands()}


def test_marauder_catalog_is_real_one_tap_and_classified():
    qs = quick_commands_for("marauder")
    assert qs, "marauder should surface one-tap commands"
    real = _real_names("marauder")
    for q in qs:
        assert q.command in real, f"phantom command: {q.command}"      # NEVER invent a command
        assert "<" not in q.command and q.command                       # one-tap only (no <arg>)
    by_cmd = {q.command: q for q in qs}
    assert by_cmd["scanall"].danger == ""                               # a scan is safe
    assert by_cmd["attack -t deauth"].danger == "lab-only"              # a deauth is flagged
    assert by_cmd["attack -t beacon -l"].danger == "lab-only"


def test_no_arg_commands_leak_across_firmwares():
    for fw in ("marauder", "bruce", "flipper", "esp32div", "ghostesp", "halehound", "meshtastic"):
        for q in quick_commands_for(fw):
            assert "<" not in q.command
            assert q.command in _real_names(fw), f"{fw}: phantom {q.command}"


def test_unknown_firmware_is_empty_not_error():
    assert quick_commands_for("no-such-fw") == []
    assert grouped_quick_commands("no-such-fw") == []
    assert quick_commands_for("") == []


def test_ghostesp_resolves_despite_naming_mismatch():
    # device_detect emits 'ghostesp' but the registry key is 'ghost-esp'. get_protocol must still resolve it,
    # so the Remote surfaces GhostESP's REAL commands (this returned [] before the missing-separator fix).
    qs = quick_commands_for("ghostesp")
    assert qs, "ghostesp must resolve to GhostESP's real command set"
    assert all(q.command in _real_names("ghostesp") for q in qs)


def test_offensive_commands_flagged_even_when_name_lacks_keyword():
    # The danger lives in the DESCRIPTION/CATEGORY, not the command name — the label must not fail open.
    cases = {"marauder": "sniffpwn", "esp32div": "probe", "halehound": "iot_recon", "ghostesp": "startportal"}
    for fw, cmd in cases.items():
        by = {q.command: q.danger for q in quick_commands_for(fw)}
        assert by.get(cmd), f"{fw}/{cmd} must be flagged (label-never-block), got {by.get(cmd)!r}"


def test_passive_commands_not_over_flagged():
    by = {q.command: q.danger for q in quick_commands_for("marauder")}
    assert by.get("scanall") == "" and by.get("list -a") == "" and by.get("stopscan") == ""


def test_grouping_preserves_categories_and_membership():
    groups = grouped_quick_commands("marauder")
    assert groups, "expected grouped commands"
    seen = set()
    for category, cmds in groups:
        assert category and cmds
        assert category not in seen, "categories must not repeat"       # first-seen order, no dupes
        seen.add(category)
        for c in cmds:
            assert isinstance(c, QuickCommand) and c.category == category
    # every quick command appears exactly once across the groups
    flat = [c.command for _cat, cmds in groups for c in cmds]
    assert sorted(flat) == sorted(q.command for q in quick_commands_for("marauder"))
