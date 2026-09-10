"""Standalone tests for device_readiness (no pytest / no conftest / no package import).

Loads the module under test by ABSOLUTE FILE PATH via importlib, exposes discoverable ``test_*``
functions, and runs them only when executed directly:

    python test_device_readiness.py

Importing this file under a non-main name executes NO assertions, NO print, and NO sys.exit — it only
loads the pure module under test and defines functions. Every real assertion lives inside a ``test_*``
function; ``main()`` (guarded by ``if __name__ == "__main__":``) discovers and runs them.

All fixtures use generic, fictional device values (COM ports, firmware names) — no real host paths and
no secrets.
"""
from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.normpath(os.path.join(_HERE, "..", "src", "core", "device_readiness.py"))


def _load_module_under_test():
    """Load ONLY the one module under test by file path. Checks ``spec`` AND ``spec.loader`` explicitly
    before use, and registers the module before exec so ``from __future__ import annotations`` dataclass
    field types resolve via ``sys.modules[cls.__module__]``. No package/app/conftest import."""
    spec = importlib.util.spec_from_file_location("device_readiness_under_test", _MOD_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot build an import spec for {_MOD_PATH!r}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


dr = _load_module_under_test()
State = dr.ReadinessState

_ASSERTIONS = 0


def ok(cond, msg):
    """Real assertion: raise AssertionError on failure; tally on pass."""
    global _ASSERTIONS
    _ASSERTIONS += 1
    if not cond:
        raise AssertionError(msg)


def eq(a, b, msg):
    global _ASSERTIONS
    _ASSERTIONS += 1
    if a != b:
        raise AssertionError(f"{msg}: expected {b!r}, got {a!r}")


def facts(**kw):
    return dr.DeviceStatusFacts(**kw)


def state_of(**kw):
    return dr.explain_readiness(facts(**kw)).state


# ── 1. Disconnected variants (positive controls) ─────────────────────────────
def test_disconnected_variants():
    r = dr.explain_readiness(facts(status="disconnected"))
    eq(r.state, State.DISCONNECTED, "closed link is DISCONNECTED")
    ok(len(r.missing) >= 1, "disconnected report names something missing")
    ok(len(r.inspect_next) >= 1, "disconnected report offers an inspect-next step")
    ok(any("port" in s.lower() or "plugged" in s.lower() for s in r.inspect_next),
       "disconnected inspect_next points at the Devices tab / port")

    r = dr.explain_readiness(facts(status="registered"))
    eq(r.state, State.DISCONNECTED, "detected-but-never-connected is DISCONNECTED")
    ok("never" in r.summary.lower(), "registered summary says it was never connected")

    r = dr.explain_readiness(facts(status="not_registered"))
    eq(r.state, State.DISCONNECTED, "not_registered is DISCONNECTED")

    r = dr.explain_readiness(facts(status="error"))
    eq(r.state, State.DISCONNECTED, "error status is DISCONNECTED")
    ok("error" in r.summary.lower(), "error status summary mentions the error")

    r = dr.explain_readiness(facts(status=""))
    eq(r.state, State.DISCONNECTED, "empty status is DISCONNECTED (a recognised no-link value)")


# ── 2. Unsupported (CC has no driver for this kind) ──────────────────────────
def test_unsupported_driver():
    r = dr.explain_readiness(facts(status="connected", driver_type="unsupported"))
    eq(r.state, State.UNSUPPORTED, "open link + unsupported driver is UNSUPPORTED")
    ok(len(r.missing) >= 1, "unsupported names a missing driver")
    ok(any("driver" in s.lower() for s in r.missing), "unsupported missing mentions a driver")


# ── 3. Missing dependency (external host tool absent) ────────────────────────
def test_missing_dependency():
    r = dr.explain_readiness(facts(status="connected", missing_dependency="dfu-util", health="alive"))
    eq(r.state, State.MISSING_DEPENDENCY, "open link + missing tool is MISSING_DEPENDENCY")
    ok(any("dfu-util" in s for s in r.missing), "the missing tool name appears in missing[]")
    ok(any("dfu-util" in s for s in r.inspect_next), "the missing tool name appears in inspect_next[]")


# ── 4. Precedence / distinctness (positive controls) ─────────────────────────
def test_precedence_and_distinctness():
    # Unsupported outranks a missing dependency (a tool cannot fix a structural mismatch).
    eq(state_of(status="connected", driver_type="unsupported", missing_dependency="dfu-util"),
       State.UNSUPPORTED, "unsupported outranks missing-dependency")
    # Disconnected outranks everything — nothing device-side is knowable without a link.
    eq(state_of(status="disconnected", driver_type="unsupported", missing_dependency="dfu-util",
                health="no-reply"),
       State.DISCONNECTED, "disconnected outranks all other states")
    # The four ambiguous states are genuinely distinct enum members.
    distinct = {State.DISCONNECTED, State.UNSUPPORTED, State.MISSING_DEPENDENCY,
                State.HARDWARE_UNVERIFIED}
    eq(len(distinct), 4, "the four target states are distinct enum members")


# ── 5. Hardware unverified — never probed (unknown) ──────────────────────────
def test_hardware_unverified_unprobed():
    r = dr.explain_readiness(facts(status="connected", health="unknown"))
    eq(r.state, State.HARDWARE_UNVERIFIED, "open link + unprobed health is HARDWARE_UNVERIFIED")
    ok(any("handshake" in s.lower() for s in r.missing + r.unknown + r.inspect_next),
       "unprobed report references the handshake")
    ok(len(r.inspect_next) >= 1, "hardware-unverified offers an inspect-next step")


# ── 6. Hardware unverified — probed but silent (no-reply) ────────────────────
def test_hardware_unverified_no_reply():
    r = dr.explain_readiness(facts(status="no-reply", health="no-reply"))
    eq(r.state, State.HARDWARE_UNVERIFIED, "open link + no-reply health is HARDWARE_UNVERIFIED")
    ok(any("baud" in s.lower() for s in r.inspect_next),
       "no-reply inspect_next suggests reviewing baud/firmware")
    ok("not verified" in r.summary.lower() or "not replied" in r.summary.lower(),
       "no-reply summary states the hardware is not verified")

    # no-reply with unidentified firmware still adds a firmware-unknown note.
    r = dr.explain_readiness(facts(status="no-reply", health="no-reply", firmware="unknown"))
    ok(any("identity" in s.lower() or "identified" in s.lower() for s in r.unknown),
       "no-reply + unknown firmware surfaces a firmware-identity unknown")


# ── 7. Ready — firmware answered (alive) ─────────────────────────────────────
def test_ready_alive_known_firmware():
    r = dr.explain_readiness(facts(status="connected", health="alive", firmware="marauder"))
    eq(r.state, State.READY, "connected + alive + known firmware is READY")
    eq(r.missing, (), "ready has nothing missing")
    eq(r.unknown, (), "ready with a known firmware has nothing unknown")
    eq(r.inspect_next, (), "ready needs no inspect-next step")
    ok("answered" in r.summary.lower(), "READY summary states the firmware answered")


def test_ready_alive_generic_firmware():
    # Ready but firmware still generic: hardware responded, name unidentified -> residual unknown only.
    r = dr.explain_readiness(facts(status="connected", health="alive", firmware="generic"))
    eq(r.state, State.READY, "alive with generic firmware is still READY (the board responded)")
    ok(len(r.unknown) >= 1, "generic-firmware READY carries a residual unknown (the name)")
    eq(r.missing, (), "generic-firmware READY still has nothing missing")


# ── 8. NEGATIVE CONTROL (finding #1): no-cli is NOT an answered firmware ──────
def test_no_cli_is_not_ready_and_transport_is_visible():
    # A stream / control-map node marked no-cli: the handshake made NO write and read NO reply, so it is
    # NOT evidence the firmware answered. It must NOT be READY; the transport distinction must stay
    # visible, and it must be explained that a text handshake is inapplicable / response is unavailable.
    r = dr.explain_readiness(facts(status="connected", health="no-cli", driver_type="stream",
                                   firmware="meshtastic"))
    ok(r.state != State.READY, "no-cli must NOT be classified READY (no firmware answer was observed)")
    eq(r.state, State.HARDWARE_UNVERIFIED,
       "no-cli is HARDWARE_UNVERIFIED — link open but response evidence unavailable")
    ok("answer" not in r.summary.lower(),
       "no-cli summary must NOT claim the firmware answered")
    blob = " ".join((r.summary,) + r.unknown + r.inspect_next).lower()
    ok("handshake" in blob and ("inapplicable" in blob or "no text" in blob),
       "no-cli explanation states a text handshake is inapplicable / there is no text channel")
    ok("unavailable" in blob or "cannot be confirmed" in blob or "unconfirmed" in blob,
       "no-cli explanation states response evidence is unavailable / unconfirmed")
    ok("stream" in blob or "control-map" in blob or "command channel" in blob,
       "no-cli explanation keeps the stream / control-map transport distinction visible")


# ── 9. NEGATIVE CONTROL (finding #2a): unrecognised STATUS stays UNKNOWN ──────
def test_unknown_status_stays_unknown_not_disconnected():
    # An unrecognised status with NO explicit connection override is not "closed" — it is UNKNOWN.
    r = dr.explain_readiness(facts(status="frobulated"))
    eq(r.state, State.UNKNOWN, "an unrecognised status is UNKNOWN, not DISCONNECTED")
    ok(any("frobulated" in s for s in r.unknown),
       "UNKNOWN-status report echoes the raw unrecognised status value")
    # PRESERVE the explicit overrides even when the status string is unrecognised.
    eq(state_of(status="frobulated", connected=False), State.DISCONNECTED,
       "explicit connected=False makes an unrecognised status DISCONNECTED (override preserved)")
    eq(state_of(status="frobulated", connected=True, health="alive", driver_type="text-cli"),
       State.READY, "explicit connected=True opens the link even for an unrecognised status")


# ── 10. NEGATIVE CONTROL (finding #2b): unrecognised DRIVER stays UNKNOWN ─────
def test_unknown_driver_stays_unknown_not_ready():
    # An unrecognised driver_type (neither a known transport nor the 'unsupported' sentinel) + alive
    # must NOT reach READY — CC cannot tell which transport this is.
    r = dr.explain_readiness(facts(status="connected", health="alive", driver_type="mystery-bus",
                                   firmware="marauder"))
    ok(r.state != State.READY, "an unrecognised driver + alive must NOT be READY")
    eq(r.state, State.UNKNOWN, "an unrecognised driver_type yields UNKNOWN")
    ok(any("mystery-bus" in s for s in r.unknown),
       "UNKNOWN-driver report echoes the raw unrecognised driver value")
    # A recognised driver with the same facts DOES reach READY (positive control for the gate).
    eq(state_of(status="connected", health="alive", driver_type="controlmap", firmware="marauder"),
       State.READY, "a recognised driver (control-map) + alive is READY")


# ── 11. NEGATIVE CONTROL (finding #2 normalization): link decision is shared ──
def test_normalization_is_shared_between_link_open_and_explain():
    # A padded/cased 'connected' status must be treated identically by link_open() and explain_readiness
    # (the original bug: explain_readiness normalized but link_open read the RAW status, disagreeing).
    padded_open = facts(status="  CONNECTED  ", health="alive", firmware="bruce")
    ok(padded_open.link_open() is True, "link_open() normalizes a padded/cased 'connected' status to open")
    eq(padded_open.link_state(), "open", "link_state() reports the shared normalized decision as open")
    eq(dr.explain_readiness(padded_open).state, State.READY,
       "explain_readiness agrees with link_open() on a padded 'connected' status (both see it open)")

    padded_closed = facts(status="  DISCONNECTED  ")
    ok(padded_closed.link_open() is False, "link_open() normalizes a padded 'disconnected' status to closed")
    eq(dr.explain_readiness(padded_closed).state, State.DISCONNECTED,
       "explain_readiness agrees with link_open() on a padded 'disconnected' status (both see it closed)")


# ── 12. link_open() override semantics (positive controls) ───────────────────
def test_link_open_override_semantics():
    # Explicit connected=True with an empty status => link treated as open (not disconnected).
    eq(state_of(status="", connected=True, health="alive", firmware="bruce"), State.READY,
       "explicit connected=True opens the link even with empty status")
    # Explicit connected=False overrides a 'connected' status => DISCONNECTED.
    eq(state_of(status="connected", connected=False), State.DISCONNECTED,
       "explicit connected=False overrides a connected status")
    ok(facts(status="connected").link_open() is True, "link_open derives True from a connected status")
    ok(facts(status="disconnected").link_open() is False, "link_open derives False from a closed status")
    ok(facts(status="disconnected", connected=True).link_open() is True,
       "explicit connected override wins over status in link_open")


# ── 13. facts_from_health mapping (positive controls) ────────────────────────
def test_facts_from_health_mapping():
    row = {"port": "COM7", "status": "connected", "firmware_version": "ghost-esp", "last_seen": "x"}
    f = dr.facts_from_health(row, health="alive", driver_type="text-cli")
    eq(f.status, "connected", "facts_from_health maps status")
    eq(f.firmware, "ghost-esp", "facts_from_health maps firmware_version -> firmware")
    eq(f.health, "alive", "facts_from_health carries the supplied handshake health")
    eq(dr.explain_readiness(f).state, State.READY, "facts_from_health round-trips into a READY verdict")
    # A row missing keys must not crash (defensive, no I/O).
    f2 = dr.facts_from_health({}, health="unknown")
    eq(dr.explain_readiness(f2).state, State.DISCONNECTED, "empty health row -> DISCONNECTED, no crash")


# ── 14. Report shape + labels + determinism (positive controls) ──────────────
def test_report_shape_labels_determinism():
    d = dr.explain_readiness(facts(status="connected", health="unknown")).to_dict()
    for key in ("state", "label", "summary", "missing", "unknown", "inspect_next"):
        ok(key in d, f"to_dict has key {key}")
    ok(isinstance(d["missing"], list) and isinstance(d["inspect_next"], list),
       "to_dict list fields are lists")
    eq(d["state"], "hardware_unverified", "to_dict state is the enum value string")
    # Every state has a human label.
    for st in State:
        ok(st in dr.STATE_LABEL and dr.STATE_LABEL[st], f"STATE_LABEL covers {st}")
    # Determinism: identical facts -> identical report (frozen dataclasses compare by value).
    a = dr.explain_readiness(facts(status="no-reply", health="no-reply", firmware="x"))
    b = dr.explain_readiness(facts(status="no-reply", health="no-reply", firmware="x"))
    eq(a, b, "explain_readiness is deterministic for identical facts")


# ── 15. Distinctness matrix — one representative per required state ───────────
def test_distinctness_matrix():
    matrix = {
        State.DISCONNECTED: facts(status="disconnected"),
        State.UNSUPPORTED: facts(status="connected", driver_type="unsupported"),
        State.MISSING_DEPENDENCY: facts(status="connected", health="alive", missing_dependency="adb"),
        State.HARDWARE_UNVERIFIED: facts(status="connected", health="no-reply"),
        State.READY: facts(status="connected", health="alive", firmware="marauder"),
    }
    seen = {}
    for expected, fct in matrix.items():
        got = dr.explain_readiness(fct).state
        eq(got, expected, f"matrix: {expected} classified correctly")
        ok(got not in seen, f"matrix: {got} is distinct from earlier rows")
        seen[got] = True


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}", file=sys.stderr)
        except Exception as e:  # an unexpected error is a failure too
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}", file=sys.stderr)
    print(f"ran {len(tests)} tests, {_ASSERTIONS} assertions, {failures} failure(s); "
          f"module loaded from {_MOD_PATH}")
    if failures:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
