"""Tests for the read-only Meshtastic status API, using the ACTUAL snapshot shapes: asdict(MeshNode) raw fields
(no computed hw_model_name/role_label/has_position), the real transport dict {bound,retired,cleanup_pending,
input_lost}, and the config_status envelope. Synthetic dicts + a frozen clock; no app/server/transport/thread.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from src.ui.web.mesh_status_api import build_status

NOW = 1_000_000  # frozen clock (epoch seconds)


def _row(**over: Any) -> Dict[str, Any]:
    # asdict(MeshNode) RAW fields — hw_model/role are ints; hw_model_name/role_label/has_position are @property
    # and are NOT serialised, so they are absent here on purpose.
    row = {"num": 7, "node_id": "!deadbeef", "long_name": "Owner Node", "short_name": "OWN",
           "hw_model": 9, "role": 0, "snr": 6.5, "last_heard": NOW - 30, "battery": 84, "voltage": 4.02,
           "channel_util": 12.0, "air_util_tx": 3.0, "hops_away": 0, "via_mqtt": False, "is_local": True}
    row.update(over)
    return row


def _tp(**over: Any) -> Dict[str, Any]:
    tp = {"bound": True, "retired": False, "cleanup_pending": False, "input_lost": False}
    tp.update(over)
    return tp


def _cfg(**over: Any) -> Dict[str, Any]:
    cfg = {"state": "ready", "inventory_stale": False, "resync_required": False, "reason": None}
    cfg.update(over)
    return cfg


def _snap(**over: Any) -> Dict[str, Any]:
    snap = {"my_node_num": 7, "config_complete": True, "config_status": _cfg(), "retired": False,
            "nodes": [_row()], "bootstrap_status": {"publication_admitted": True}, "transport_status": _tp()}
    snap.update(over)
    return snap


def _p(snap):
    return lambda: snap


def _keys_and_strings(obj, out=None):
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(str(k)); _keys_and_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _keys_and_strings(v, out)
    else:
        out.append(str(obj))
    return out


# ── provider / owner availability ──────────────────────────────────────────────────────────────────

def test_provider_absent_and_error_and_non_dict_are_unavailable():
    assert build_status(None, now=NOW)["reason"] == "provider-absent"

    def boom():
        raise RuntimeError("x")
    assert build_status(boom, now=NOW)["reason"] == "provider-error"
    assert build_status(lambda: None, now=NOW)["reason"] == "provider-error"


def test_absent_owner_and_neighbor_only_never_substitute():
    assert build_status(_p(_snap(my_node_num=None)), now=NOW)["reason"] == "owner-not-admitted"
    neigh = _snap(my_node_num=7, nodes=[_row(num=99, long_name="Neighbor")])
    assert build_status(_p(neigh), now=NOW) == {"available": False, "reason": "owner-not-admitted",
                                                "transport": "bound", "read_at_epoch": NOW}


# ── healthy admitted config-ready owner ──────────────────────────────────────────────────────────────

def test_healthy_admitted_ready_owner_is_available_with_real_shapes():
    r = build_status(_p(_snap()), now=NOW)
    assert r["available"] is True
    # name + reported hops ("direct"); hw_model_name/role_label absent from asdict, so never invented.
    assert r["identity"] == {"num": 7, "name": "Owner Node · direct"}
    assert r["battery"] == {"state": "percent", "percent": 84, "voltage": 4.02}
    assert r["link"] == {"state": "known", "snr_db": 6.5}
    assert r["readiness"] == {"config_complete": True, "publication_admitted": True, "transport": "bound"}
    assert r["freshness"]["last_heard_age_s"] == 30
    assert r["freshness"]["telemetry_age"] == "unknown"          # battery/link have no own timestamp
    assert r["freshness"]["read_at_is"] == "request-time"        # not a provider capture time
    assert r["freshness"]["clock"] == "ok"


# ── MS-1: real transport dict + admission/config gating WITHHOLDS telemetry ──────────────────────────

@pytest.mark.parametrize("tp,reason", [
    (_tp(retired=True), "transport-uncertain"),  # transport-dict retired -> unhealthy transport
    (_tp(input_lost=True), "transport-uncertain"),
    (_tp(cleanup_pending=True), "transport-uncertain"),
    (_tp(bound=False), "transport-uncertain"),
    ("connected", "transport-uncertain"),        # a bare string (the old fixture shape) is NOT healthy
    (None, "transport-uncertain"),
])
def test_transport_dict_states_withhold_telemetry(tp, reason):
    r = build_status(_p(_snap(transport_status=tp)), now=NOW)
    assert r["available"] is False and r["reason"] == reason
    assert "battery" not in r and "identity" not in r           # telemetry withheld


def test_retired_snapshot_is_unavailable_not_telemetry():
    r = build_status(_p(_snap(retired=True)), now=NOW)
    assert r == {"available": False, "reason": "transport-retired", "transport": "bound", "read_at_epoch": NOW}


def test_unadmitted_withholds_telemetry():
    assert build_status(_p(_snap(bootstrap_status={"publication_admitted": False})), now=NOW)["reason"] == "owner-not-admitted"
    snap = _snap(); del snap["bootstrap_status"]
    assert build_status(_p(snap), now=NOW)["reason"] == "owner-not-admitted"


@pytest.mark.parametrize("over", [
    {"config_complete": False},
    {"config_status": _cfg(state="sync_failed", inventory_stale=True, reason="timeout"), "config_complete": False},
    {"config_status": _cfg(inventory_stale=True)},
    {"config_status": _cfg(resync_required=True)},
])
def test_incomplete_or_stale_inventory_withholds_telemetry(over):
    r = build_status(_p(_snap(**over)), now=NOW)
    assert r["available"] is False and r["reason"] == "inventory-not-ready"
    assert "battery" not in r


# ── battery / link edge values ─────────────────────────────────────────────────────────────────────

def test_battery_101_external_missing_battery_and_snr_unknown():
    assert build_status(_p(_snap(nodes=[_row(battery=101)])), now=NOW)["battery"] == {
        "state": "external", "percent": None, "voltage": 4.02}
    assert build_status(_p(_snap(nodes=[_row(battery=None, voltage=None)])), now=NOW)["battery"] == {
        "state": "unknown", "percent": None, "voltage": None}
    assert build_status(_p(_snap(nodes=[_row(snr=None)])), now=NOW)["link"] == {"state": "unknown", "snr_db": None}


# ── MS-2: last-heard age is not measurement age; future = clock ambiguity, not 0 ─────────────────────

def test_missing_last_heard_age_unknown_clock_unknown():
    f = build_status(_p(_snap(nodes=[_row(last_heard=None)])), now=NOW)["freshness"]
    assert f["last_heard_epoch"] is None and f["last_heard_age_s"] == "unknown" and f["clock"] == "unknown"
    assert f["telemetry_age"] == "unknown"


def test_future_last_heard_is_clock_ambiguous_not_zero_age():
    f = build_status(_p(_snap(nodes=[_row(last_heard=NOW + 500)])), now=NOW)["freshness"]
    assert f["last_heard_age_s"] == "unknown"    # never a fabricated 0
    assert f["clock"] == "ambiguous"
    assert f["last_heard_epoch"] == NOW + 500


# ── MS-3: position projected out BEFORE the mapper — never a GPS map, never a crash ──────────────────

def test_has_position_with_none_coords_does_not_crash_and_is_available():
    # Root's crash witness: has_position true + None coords would crash node_to_readings' GPS format.
    r = build_status(_p(_snap(nodes=[_row(has_position=True, latitude=None, longitude=None)])), now=NOW)
    assert r["available"] is True and r["identity"]["num"] == 7   # position dropped; status still read


def test_malformed_location_never_reaches_gps_or_fails_the_read():
    r = build_status(_p(_snap(nodes=[_row(latitude="junk", longitude=[1, 2], altitude={})])), now=NOW)
    assert r["available"] is True and "battery" in r             # location projected out, no exception


def test_no_gps_neighbor_channel_config_leakage_in_whole_response():
    snap = _snap(
        nodes=[_row(latitude=40.7, longitude=-74.0, altitude=10, has_position=True),
               _row(num=99, long_name="Neighbor", latitude=1.0, longitude=2.0)],
        channels=[{"psk": "secret", "name": "LongFast"}], lora_config={"region": "US"})
    r = build_status(_p(snap), now=NOW)
    tokens = [t.lower() for t in _keys_and_strings(r)]
    for f in ("lat", "latitude", "lon", "longitude", "altitude", "gps", "position", "neighbor",
              "channel", "psk", "lora_config", "longfast", "40.7", "-74.0", "secret"):
        assert not any(f in t for t in tokens), f"leaked {f!r}"
    assert set(r) == {"available", "identity", "battery", "link", "freshness", "readiness"}


# ── V2-1: malformed selected VALUES become unknown, never passed through or leaked ───────────────────

def test_nested_or_list_telemetry_values_become_unknown_and_do_not_leak():
    snap = _snap(nodes=[_row(battery={"percent": {"psk": "SECRET"}, "lat": 1.0},
                             voltage={"psk": "x"}, snr=[1, 2], long_name={"private": "MARKER"})])
    r = build_status(_p(snap), now=NOW)
    assert r["available"] is True
    assert r["battery"] == {"state": "unknown", "percent": None, "voltage": None}
    assert r["link"] == {"state": "unknown", "snr_db": None}
    tokens = [t.lower() for t in _keys_and_strings(r)]
    for f in ("psk", "secret", "marker", "private", "'lat'", "lat'"):
        assert not any(f in t for t in tokens), f"leaked {f!r}"


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), 10 ** 20, {"x": 1}, [1], "84"])
def test_malformed_battery_value_is_unknown(bad):
    assert build_status(_p(_snap(nodes=[_row(battery=bad)])), now=NOW)["battery"]["state"] == "unknown"


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), {"x": 1}, [1], "6.5"])
def test_malformed_snr_value_is_unknown(bad):
    assert build_status(_p(_snap(nodes=[_row(snr=bad)])), now=NOW)["link"]["state"] == "unknown"


def test_malformed_voltage_becomes_none_not_passthrough():
    r = build_status(_p(_snap(nodes=[_row(voltage={"psk": "x"})])), now=NOW)
    assert r["battery"] == {"state": "percent", "percent": 84, "voltage": None}


# ── V2-2: non-finite / malformed last_heard does not raise; returns unknown ───────────────────────────

@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), True, {"x": 1}, [1], "1000", 10 ** 20])
def test_nonfinite_or_malformed_last_heard_is_unknown_not_a_crash(bad):
    f = build_status(_p(_snap(nodes=[_row(last_heard=bad)])), now=NOW)["freshness"]
    assert f["last_heard_epoch"] is None and f["last_heard_age_s"] == "unknown"


# ── inert Flask-test-client route/JSON path (no server) ─────────────────────────────────────────────

def test_route_serializes_json_under_an_auth_wrapper():
    from flask import Flask, jsonify
    app = Flask(__name__)
    calls = {"auth": 0}

    def fake_auth(fn):
        def w(*a, **k):
            calls["auth"] += 1
            return fn(*a, **k)
        w.__name__ = fn.__name__
        return w

    provider = _p(_snap())

    @app.route("/api/mesh/status")
    @fake_auth
    def _mesh_status():
        return jsonify(build_status(provider, now=NOW))

    resp = app.test_client().get("/api/mesh/status")
    assert resp.status_code == 200 and calls["auth"] == 1
    assert resp.get_json()["available"] is True
    assert "latitude" not in resp.get_data(as_text=True)


# ── V3-1: validate node identity before selecting the owner (reject bool/coercion aliases) ───────────

def test_boolean_row_num_does_not_alias_a_valid_integer_owner():
    # my_node_num=1 with a row whose num is the boolean True: raw equality treats True == 1 as a match and
    # would select the malformed row, then the scalar normalizer nulls it -> available:true with a null
    # identity. The identity must be validated FIRST: the row is not the owner, so this is owner-not-admitted.
    snap = _snap(my_node_num=1, nodes=[_row(num=True, long_name="Owned note node")])
    r = build_status(_p(snap), now=NOW)
    assert r == {"available": False, "reason": "owner-not-admitted", "transport": "bound", "read_at_epoch": NOW}


@pytest.mark.parametrize("bad_owner", [True, False, 1.0, "1", [1], {"x": 1}, -1, 2 ** 32])
def test_non_integer_owner_id_is_owner_not_admitted(bad_owner):
    # the caller's owner id must be a valid integer identity (bool and float coercion rejected)
    r = build_status(_p(_snap(my_node_num=bad_owner)), now=NOW)
    assert r["available"] is False and r["reason"] == "owner-not-admitted"


@pytest.mark.parametrize("bad_num", [True, False, 1.0, "1", None, [1], {"x": 1}])
def test_non_integer_row_id_is_never_selected_as_owner(bad_num):
    # owner id is the valid integer 1; a candidate row whose num is a bool/float/etc. that could alias 1 under
    # raw equality must not be selected, and no neighbor is substituted -> owner-not-admitted.
    r = build_status(_p(_snap(my_node_num=1, nodes=[_row(num=bad_num)])), now=NOW)
    assert r["available"] is False and r["reason"] == "owner-not-admitted"


def test_valid_integer_owner_is_still_selected_and_emitted():
    # regression: a genuine integer owner id with a matching integer row num is selected and emits that identity
    r = build_status(_p(_snap(my_node_num=1, nodes=[_row(num=1, long_name="Owner One")])), now=NOW)
    assert r["available"] is True and r["identity"] == {"num": 1, "name": "Owner One · direct"}
