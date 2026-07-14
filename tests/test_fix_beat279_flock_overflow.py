"""Beat 279 - flock from_checkpoint sinks the whole load on adversarial numerics (cc-audit-12, MED).

`FlockSession.from_checkpoint` promises (docstring) that a malformed checkpoint yields an
empty/partial session and NEVER raises -- a bad feature is skipped, not fatal. Its per-feature
`except` caught (KeyError, TypeError, ValueError, IndexError) and the `_as_int` helper caught
(TypeError, ValueError). But `OverflowError` is a sibling of ValueError under ArithmeticError, NOT a
subclass, so two adversarial numeric shapes escaped BOTH guards and propagated out of the load:
  - a giant-integer coordinate -> `float(lat)` raises OverflowError("int too large to convert")
  - an infinite property (e.g. JSON ``"rssi": 1e400`` -> float('inf')) -> `_as_int` does `int(inf)`.
Either one aborted the entire resume (every camera), defeating the crash-recovery contract.

Fix: add OverflowError to both except tuples so the one bad feature is skipped, not the whole load.

Discriminating (fail on buggy HEAD, pass on the fix):
  - test_giant_int_coord_feature_skipped_not_fatal / test_infinite_property_does_not_sink_load
  - test_as_int_infinity_is_zero (HEAD raises OverflowError on int(inf); fix returns the 0 sentinel)
Guard (pass on both HEAD and the fix):
  - test_clean_checkpoint_loads (a well-formed checkpoint, no overflow, loads unchanged either way)
"""
from __future__ import annotations

import json

from src.core import flock as fk


def _write(tmp_path, obj):
    p = tmp_path / "cameras.geojson"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def _feature(mac, coords, props=None):
    f = {"geometry": {"coordinates": coords}, "properties": {"mac": mac}}
    f["properties"].update(props or {})
    return f


def test_giant_int_coord_feature_skipped_not_fatal(tmp_path):
    """A giant-integer coordinate skips ONLY that feature; a following good feature still loads."""
    bad = _feature("AA:BB:CC:11:22:33", [int("9" * 400), 6])
    good = _feature("DD:EE:FF:00:11:22", [5, 6], {"rssi": -40})
    s = fk.FlockSession.from_checkpoint(_write(tmp_path, {"features": [bad, good]}))
    assert set(s.cameras) == {"DD:EE:FF:00:11:22"}


def test_infinite_property_does_not_sink_load(tmp_path):
    """An infinite numeric property (1e400 -> inf) must not raise out of the whole load."""
    bad = _feature("AA:BB:CC:11:22:33", [5, 6], {"rssi": 1e400})
    good = _feature("DD:EE:FF:00:11:22", [7, 8])
    s = fk.FlockSession.from_checkpoint(_write(tmp_path, {"features": [bad, good]}))
    # The good camera always survives; the inf-rssi one is either skipped or coerced (rssi 0),
    # but crucially the load never raises.
    assert "DD:EE:FF:00:11:22" in s.cameras


def test_clean_checkpoint_loads(tmp_path):
    """Guard: a well-formed checkpoint loads its cameras unchanged."""
    good = _feature("DD:EE:FF:00:11:22", [5, 6], {"rssi": -40, "ssid": "home"})
    s = fk.FlockSession.from_checkpoint(_write(tmp_path, {"features": [good]}))
    assert set(s.cameras) == {"DD:EE:FF:00:11:22"}
    assert s.cameras["DD:EE:FF:00:11:22"].rssi == -40


def test_as_int_infinity_is_zero():
    """Guard: _as_int coerces inf to the 0 sentinel rather than raising."""
    assert fk._as_int(float("inf")) == 0
    assert fk._as_int("5") == 5


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
