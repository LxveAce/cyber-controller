"""Behavior controls for the pure BLE-history policy boundary.

Synthetic mappings/strings only; forbidden functions are faked. No settings load, environment,
filesystem, security, journal, device, or network operation is performed or required.
"""
from __future__ import annotations

import pytest

from src.core.ble_history_policy import (
    FLAVOR_POSIX,
    FLAVOR_WINDOWS,
    MODE_DISABLED,
    MODE_MEMORY,
    MODE_PERSISTENT,
    REASON_MALFORMED_ACK,
    REASON_MALFORMED_FLAVOR,
    REASON_MALFORMED_MODE,
    REASON_MALFORMED_OVERRIDE,
    REASON_MALFORMED_SECTION,
    REASON_MALFORMED_SECURITY,
    REASON_MALFORMED_SETTINGS,
    REASON_PERSISTENT_REQUIRES_ACK,
    REASON_SECURE_CONTAINER_CONFLICT,
    REASON_UNKNOWN_MODE,
    BleHistoryPolicy,
    decide_ble_history_policy,
)


def _persistent(**over):
    s = {"ble_history": {"mode": "persistent", "plaintext_ack": True},
         "security": {"secure_container": False}}
    s.update(over)
    return s


# ── defaults / known modes ──────────

def test_missing_section_defaults_to_disabled():
    d = decide_ble_history_policy({})
    assert d.selected == MODE_DISABLED and d.requested == MODE_DISABLED
    assert d.reason is None and d.override_path is None and d.eligible


def test_missing_mode_key_defaults_to_disabled():
    d = decide_ble_history_policy({"ble_history": {}})
    assert d.selected == MODE_DISABLED and d.requested == MODE_DISABLED and d.reason is None


def test_memory_mode_selected_without_durability_or_path():
    d = decide_ble_history_policy({"ble_history": {"mode": "memory"}})
    assert d.selected == MODE_MEMORY and d.override_path is None and d.reason is None


def test_persistent_with_ack_and_secure_false_is_eligible():
    d = decide_ble_history_policy(_persistent())
    assert d.selected == MODE_PERSISTENT and d.override_path is None and d.reason is None


def test_unrelated_sections_do_not_affect_the_boundary():
    d = decide_ble_history_policy(
        {"ble_history": {"mode": "memory"}, "wifi": {"x": 1}, "safety": {}})
    assert d.selected == MODE_MEMORY and d.reason is None


# ── malformed structure / mode ──────────

def test_non_mapping_settings_refuses_unavailable():
    for bad in (None, [], "settings", 3):
        d = decide_ble_history_policy(bad)
        assert d.selected is None and d.reason == REASON_MALFORMED_SETTINGS and d.requested is None


def test_non_mapping_ble_history_section_is_malformed():
    d = decide_ble_history_policy({"ble_history": ["memory"]})
    assert d.selected is None and d.reason == REASON_MALFORMED_SECTION


def test_non_string_mode_is_malformed():
    d = decide_ble_history_policy({"ble_history": {"mode": 3}})
    assert d.selected is None and d.reason == REASON_MALFORMED_MODE and d.requested is None


def test_unknown_mode_is_bounded_error_and_not_echoed():
    d = decide_ble_history_policy({"ble_history": {"mode": "persistent-ish rm -rf"}})
    assert d.selected is None and d.reason == REASON_UNKNOWN_MODE and d.requested is None
    # the arbitrary supplied string must never appear in the decision / its repr
    assert "persistent-ish" not in repr(d)


# ── plaintext_ack: strict bool vs bool-like ──────────

@pytest.mark.parametrize("ack", [1, 0, "true", "True", "", None, 1.0], ids=repr)
def test_present_but_non_exact_bool_ack_is_malformed(ack):
    d = decide_ble_history_policy({"ble_history": {"mode": "persistent", "plaintext_ack": ack},
                                   "security": {"secure_container": False}})
    assert d.selected is None and d.reason == REASON_MALFORMED_ACK


def test_malformed_ack_is_rejected_even_for_memory_mode():
    # rule 3 validates a present plaintext_ack as an exact bool regardless of mode
    d = decide_ble_history_policy({"ble_history": {"mode": "memory", "plaintext_ack": "yes"}})
    assert d.selected is None and d.reason == REASON_MALFORMED_ACK


def test_persistent_missing_or_false_ack_refuses():
    for sect in ({"mode": "persistent"}, {"mode": "persistent", "plaintext_ack": False}):
        d = decide_ble_history_policy(
            {"ble_history": sect, "security": {"secure_container": False}})
        assert d.selected is None and d.reason == REASON_PERSISTENT_REQUIRES_ACK


# ── secure-container conflict (nested security.secure_container) ──────────

def test_secure_container_true_conflicts_and_refuses_plaintext():
    d = decide_ble_history_policy(_persistent(security={"secure_container": True}))
    assert d.selected is None and d.reason == REASON_SECURE_CONTAINER_CONFLICT


def test_secure_container_missing_uses_false_default_and_permits():
    d = decide_ble_history_policy({"ble_history": {"mode": "persistent", "plaintext_ack": True}})
    assert d.selected == MODE_PERSISTENT and d.reason is None


@pytest.mark.parametrize("secure", [1, "true", 0, None], ids=repr)
def test_secure_container_non_bool_leaf_is_malformed(secure):
    d = decide_ble_history_policy(_persistent(security={"secure_container": secure}))
    assert d.selected is None and d.reason == REASON_MALFORMED_SECURITY


def test_non_mapping_security_section_is_malformed():
    d = decide_ble_history_policy(_persistent(security=["secure_container"]))
    assert d.selected is None and d.reason == REASON_MALFORMED_SECURITY


# ── override interpretation (only for eligible persistent) ──────────

def test_directory_override_alone_does_not_enable_history():
    d = decide_ble_history_policy({"ble_history": {"mode": "disabled"}},
                                  override="/var/lib/ble", path_flavor=FLAVOR_POSIX)
    assert d.selected == MODE_DISABLED and d.override_path is None


def test_memory_ignores_an_unusable_unused_override():
    d = decide_ble_history_policy({"ble_history": {"mode": "memory"}},
                                  override="not-a-path\x00", path_flavor=FLAVOR_WINDOWS)
    assert d.selected == MODE_MEMORY and d.override_path is None and d.reason is None


def test_eligible_persistent_without_override_leaves_default_to_owner():
    d = decide_ble_history_policy(_persistent(), override=None)
    assert d.selected == MODE_PERSISTENT and d.override_path is None


@pytest.mark.parametrize("path", ["C:\\logs\\ble", "C:/logs/ble", "\\\\server\\share\\ble"])
def test_valid_windows_absolute_override_is_preserved_verbatim(path):
    d = decide_ble_history_policy(_persistent(), override=path, path_flavor=FLAVOR_WINDOWS)
    assert d.selected == MODE_PERSISTENT and d.override_path == path


def test_valid_posix_absolute_override_is_preserved_verbatim():
    d = decide_ble_history_policy(
        _persistent(), override="/var/lib/cc/ble", path_flavor=FLAVOR_POSIX)
    assert d.selected == MODE_PERSISTENT and d.override_path == "/var/lib/cc/ble"


@pytest.mark.parametrize("path,flavor", [
    ("logs/ble", FLAVOR_POSIX),            # relative
    ("relative\\ble", FLAVOR_WINDOWS),     # relative
    ("C:logs", FLAVOR_WINDOWS),            # drive-relative
    ("C:", FLAVOR_WINDOWS),                # drive-relative (no separator)
    ("\\logs", FLAVOR_WINDOWS),            # root-relative
    ("/logs", FLAVOR_WINDOWS),             # root-relative (no drive, windows flavor)
    ("relative", FLAVOR_POSIX),            # relative
    ("/a\x00b", FLAVOR_POSIX),             # embedded NUL
    ("   ", FLAVOR_POSIX),                 # whitespace-only
    ("", FLAVOR_WINDOWS),                  # empty
])
def test_invalid_override_syntax_is_malformed(path, flavor):
    d = decide_ble_history_policy(_persistent(), override=path, path_flavor=flavor)
    assert d.selected is None and d.reason == REASON_MALFORMED_OVERRIDE


def test_non_string_override_is_malformed():
    d = decide_ble_history_policy(_persistent(), override=123, path_flavor=FLAVOR_POSIX)
    assert d.selected is None and d.reason == REASON_MALFORMED_OVERRIDE


def test_malformed_path_flavor_is_rejected_only_when_override_interpreted():
    d = decide_ble_history_policy(_persistent(), override="/x", path_flavor="macos")
    assert d.selected is None and d.reason == REASON_MALFORMED_FLAVOR
    # with no override to interpret, an unusual flavor is irrelevant
    ok = decide_ble_history_policy(_persistent(), override=None, path_flavor="macos")
    assert ok.selected == MODE_PERSISTENT


# ── immutability / no sensitive echo / purity ──────────

def test_result_is_frozen_and_independent_of_later_source_mutation():
    settings = {"ble_history": {"mode": "memory"}}
    d = decide_ble_history_policy(settings)
    settings["ble_history"]["mode"] = "persistent"   # mutate the source after the call
    assert d.selected == MODE_MEMORY                 # decision unchanged (no retained mapping)
    with pytest.raises(Exception):
        d.selected = MODE_PERSISTENT                 # frozen dataclass


def test_reasons_are_finite_constants_without_paths_or_raw_input():
    secret_path = "C:\\SECRET\\override\x00"
    d = decide_ble_history_policy(_persistent(), override=secret_path, path_flavor=FLAVOR_WINDOWS)
    assert d.reason == REASON_MALFORMED_OVERRIDE
    assert "SECRET" not in repr(d) and secret_path not in repr(d)


def test_no_ambient_settings_env_filesystem_or_security_operations(monkeypatch):
    import builtins
    import os

    import src.core.ble_history_policy as mod

    # The pure module imports none of os / pathlib / secure_store.
    assert not hasattr(mod, "os")
    assert not hasattr(mod, "secure_store")
    assert not hasattr(mod, "Path")

    def boom(*_a, **_k):
        raise AssertionError("forbidden ambient operation")

    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(os, "listdir", boom)
    monkeypatch.setattr(os, "makedirs", boom)
    monkeypatch.setattr(os.path, "exists", boom)
    monkeypatch.setattr(os.environ, "get", boom)

    d = decide_ble_history_policy(
        _persistent(), override="C:\\logs\\ble", path_flavor=FLAVOR_WINDOWS)
    assert d.selected == MODE_PERSISTENT and d.override_path == "C:\\logs\\ble"


def test_policy_object_shape_is_eligibility_not_runtime():
    # The decision exposes eligibility/selection only — no running/available/durable/started field.
    fields = set(BleHistoryPolicy.__dataclass_fields__)
    assert fields == {"requested", "selected", "reason", "override_path"}
    for forbidden in ("running", "available", "durable", "started", "active", "path_resolved"):
        assert forbidden not in fields


# ── independent-review corrections (null-vs-missing, exact-str flavor, complete Windows syntax) ──

_BS = chr(92)


@pytest.mark.parametrize("settings", [{"ble_history": None}, {"ble_history": {"mode": None}}],
                         ids=["null-section", "null-mode"])
def test_present_null_section_or_mode_is_malformed_not_disabled(settings):
    d = decide_ble_history_policy(settings)
    assert d.selected is None and d.reason in (REASON_MALFORMED_SECTION, REASON_MALFORMED_MODE)


def test_truly_missing_section_and_mode_still_default_to_disabled():
    assert decide_ble_history_policy({}).selected == MODE_DISABLED
    assert decide_ble_history_policy({"ble_history": {}}).selected == MODE_DISABLED


def test_str_subclass_and_impostor_flavor_are_malformed_without_leaking():
    class _Sub(str):
        pass

    class _Explodes:
        def __eq__(self, other):
            raise ValueError("flavor equality must not escape this boundary")

    for flavor in (_Sub("posix"), _Explodes()):
        d = decide_ble_history_policy(_persistent(), override="/fixture", path_flavor=flavor)
        assert d.selected is None and d.reason == REASON_MALFORMED_FLAVOR


@pytest.mark.parametrize("value", [
    "//server/share",
    _BS * 2 + "server" + _BS + "share" + _BS + "ble",
    _BS * 2 + "server",
    _BS * 2 + "?" + _BS + "relative",
    _BS * 2 + "?" + _BS + "C:relative",
    "C:" + _BS + "logs",
    "C:logs",
])
def test_windows_override_matches_pure_path_absolute_oracle(value):
    from pathlib import PureWindowsPath
    expected = PureWindowsPath(value).is_absolute()
    d = decide_ble_history_policy(_persistent(), override=value, path_flavor=FLAVOR_WINDOWS)
    if expected:
        assert d.selected == MODE_PERSISTENT and d.override_path == value
    else:
        assert d.selected is None and d.reason == REASON_MALFORMED_OVERRIDE
