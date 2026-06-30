"""Command-correctness (coverage P2): confirmed-phantom target actions are removed rather than shipped as
broken buttons. HaleHound 'analyze', Meshtastic 'relay', and Flipper 'bt spam' referenced commands absent from
their own get_commands / firmware CLI."""

from __future__ import annotations


def test_phantom_target_actions_removed():
    from src.models.target import TargetType
    from src.protocols import flipper, halehound, meshtastic
    assert not halehound.TARGET_ACTIONS.get(TargetType.AP)   # was phantom "analyze {channel}"
    assert not meshtastic.TARGET_ACTIONS.get(TargetType.AP)  # was phantom "relay {mac}" (protobuf link)
    assert not flipper.TARGET_ACTIONS.get(TargetType.BLE)    # was phantom "bt spam" (no stock CLI cmd)


def test_flipper_ble_spam_broadcast_removed():
    from src.core.broadcast import BroadcastVerb
    from src.protocols import flipper
    assert BroadcastVerb.BLE_SPAM not in flipper.BROADCAST_CAPABILITIES


def test_surviving_flipper_actions_intact():
    """The real Flipper actions are untouched."""
    from src.models.target import TargetType
    from src.protocols import flipper
    assert flipper.TARGET_ACTIONS.get(TargetType.SUBGHZ)
    assert flipper.TARGET_ACTIONS.get(TargetType.NFC)
