"""Tests for scan_commands_for — per-firmware wardrive scan verbs (F1 slice 3).

The fix: a mixed deck must not be blasted a hardcoded "scanap" only Marauder understands. Each firmware's
own BROADCAST_CAPABILITIES (FIND_APS / STOP_ALL) drives the start/stop command + line ending; unknown
firmware falls back to the Marauder default. Pure — no Qt, no serial.
"""
from src.core import wardrive as wd


def test_unknown_firmware_uses_marauder_default():
    default = wd.ScanCommands(("scanap",), "stopscan", "\n")
    assert wd.scan_commands_for("") == default
    assert wd.scan_commands_for("   ") == default
    assert wd.scan_commands_for("no-such-firmware") == default   # unresolvable name -> safe default


def test_marauder_resolves_to_its_native_caps():
    from src.core.broadcast import BroadcastVerb
    from src.protocols import get_protocol_module, line_ending_for

    caps = get_protocol_module("marauder").BROADCAST_CAPABILITIES
    sc = wd.scan_commands_for("marauder")
    pre, cmd = caps[BroadcastVerb.FIND_APS]
    assert sc.start == tuple(pre) + (cmd,)              # tracks the source-of-truth caps table
    assert sc.start == ("scanall",)                    # (concretely, Marauder's native FIND_APS verb)
    assert sc.stop == caps[BroadcastVerb.STOP_ALL][1]
    assert sc.line_ending == line_ending_for("marauder")


def test_a_declaring_firmware_is_not_sent_the_marauder_default():
    # Any firmware that declares FIND_APS must resolve to ITS command, proving non-Marauder boards get
    # their own verb (that's the whole point of the slice). We check every protocol module that declares it.
    from src.core.broadcast import BroadcastVerb
    from src.protocols import get_protocol_module

    checked = 0
    for fw in ("marauder", "ghost_esp", "esp32_div", "bruce", "bw16", "halehound", "flipper", "meshtastic"):
        mod = get_protocol_module(fw)
        caps = getattr(mod, "BROADCAST_CAPABILITIES", {}) if mod else {}
        find = caps.get(BroadcastVerb.FIND_APS)
        if find is None:
            continue
        pre, cmd = find
        assert wd.scan_commands_for(fw).start == tuple(pre) + (cmd,), f"{fw} mis-resolved"
        checked += 1
    assert checked >= 1                                 # at least one non-default firmware actually exercised
