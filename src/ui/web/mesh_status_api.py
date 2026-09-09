"""Read-only Meshtastic status workspace API — the owned node's honest status, nothing more.

The reformed DEVICE > Mesh workspace shows the *already-owned* node's identity, battery/external-power, and
link SNR, and how long ago the node was last heard — distinguishing an unavailable observation from a decoded
reading. It is strictly read-only: it consumes an INJECTED snapshot provider (the existing caller-owned
``MeshConnectionSession.snapshot()``), never acquires a session, opens/scans/probes a transport, or mutates any
model. When no provider is wired (the default production input today) it reports unavailable.

Availability, inventory readiness, transport state, and last-heard time are separate axes read from the ACTUAL
snapshot envelope. Telemetry is exposed only for an admitted, config-ready node on a healthy transport; a
missing/failed provider, an unadmitted owner, an unready/resyncing inventory, or a retired/lost/unbound
transport is an unavailable *observation* — never proof of physical disconnection. Only the owned node is read
— the row whose validated integer ``num`` equals the validated ``my_node_num``; a neighbor, or a boolean/float
that only aliases the owner under raw equality, is never selected. Battery ``101`` is external power (not 101%);
a missing SNR is unknown (not zero). Position/location fields are projected out BEFORE the canonical mapper, so
a GPS mapping is never attempted; neighbors, channels/config, free text, and raw snapshots are never emitted.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, Optional, Tuple

from src.core.mesh_metrics import node_to_readings
from src.core.metrics import ReadingKind


# ── scalar-value validation ──────────────────────────────────────────────────────────────────────
# Selecting field NAMES is not enough: a malformed injected provider can carry nested dicts/lists or
# non-finite numbers in a selected value. These reject anything that is not a finite in-domain scalar, so a
# malformed value becomes unknown/unavailable and never reaches the mapper or the response (no passthrough,
# no stringify). bool is rejected because ``type(True)`` is ``bool`` (not int/float).

def _finite_number(value: Any, *, minimum: Optional[float] = None, maximum: Optional[float] = None) -> Optional[float]:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number):
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is not None and number > maximum:
        return None
    return number


def _whole_number(value: Any, *, minimum: int = 0, maximum: int = 0xFFFFFFFF) -> Optional[int]:
    number = _finite_number(value, minimum=minimum, maximum=maximum)
    return int(number) if number is not None and number.is_integer() else None


def _bounded_text(value: Any, *, limit: int = 256) -> Optional[str]:
    return value if type(value) is str and len(value) <= limit else None


def _epoch(value: Any) -> Optional[int]:
    """A finite epoch second as an int (floored); non-finite / bool / non-number -> None. Used for the node's
    last-heard and the request time — never raises (a non-finite provider value can't OverflowError here)."""
    number = _finite_number(value, minimum=0, maximum=0xFFFFFFFFFF)
    return int(number) if number is not None else None


def _node_id(value: Any) -> Optional[int]:
    """A node-number identity in the unsigned 32-bit integer domain. Strict on purpose: ``type(x) is int``
    excludes ``bool`` (so ``True`` cannot alias owner ``1`` under raw equality), and a float — even an
    integer-valued one — is rejected as coercion, because a node number is an exact integer identity, not a
    normalized measurement. Any non-int or out-of-domain value -> ``None``. Used to validate the caller's owner
    id and each candidate row id BEFORE they are compared, so a malformed row is never selected as the owner."""
    return value if type(value) is int and 0 <= value <= 0xFFFFFFFF else None

# The allowed identity/battery/link node fields are projected + value-validated in _project_node below.
# Location fields (latitude/longitude/altitude and the computed has_position) are deliberately never copied, so
# the canonical mapper's GPS branch is unreachable and a malformed coordinate can never crash the read. Note:
# asdict(MeshNode) serialises raw fields only — the computed hw_model_name/role_label/has_position are NOT
# present, so identity is the reported name, never invented hardware/role facts.


class _AttrNode:
    """Attribute view over an ALREADY-PROJECTED row dict (missing key -> ``None``), so the pure
    ``node_to_readings`` mapper runs unchanged over only the allowed fields."""

    __slots__ = ("_d",)

    def __init__(self, projected: Dict[str, Any]) -> None:
        self._d = projected

    def __getattr__(self, name: str) -> Any:
        return self._d.get(name)


def _project_node(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project the allowed identity/battery/link fields AND validate each value to a finite in-domain scalar
    (location dropped entirely). A malformed value (nested dict/list, non-finite, bool, out-of-domain) becomes
    ``None`` so the canonical mapper emits unknown for it — it is never passed through or stringified."""
    out: Dict[str, Any] = {}
    out["num"] = _whole_number(row.get("num"))
    for key in ("node_id", "long_name", "short_name"):
        text = _bounded_text(row.get(key))
        if text is not None:
            out[key] = text
    out["battery"] = _whole_number(row.get("battery"), minimum=0, maximum=101)     # 0..100 %, 101 = external
    out["voltage"] = _finite_number(row.get("voltage"), minimum=0, maximum=100)
    out["snr"] = _finite_number(row.get("snr"), minimum=-128, maximum=128)
    out["hops_away"] = _whole_number(row.get("hops_away"), minimum=0, maximum=255)
    out["via_mqtt"] = row.get("via_mqtt") if type(row.get("via_mqtt")) is bool else False
    return out


def _unavailable(reason: str, now: Any, transport: str = "unknown") -> Dict[str, Any]:
    return {"available": False, "reason": reason, "transport": transport, "read_at_epoch": _epoch(now)}


def _transport(td: Any) -> Tuple[str, bool]:
    """Summarise the real ``_managed_status`` transport dict ``{bound, retired, cleanup_pending, input_lost}``
    into ``(summary, healthy)``. Healthy = bound and not retired/cleanup_pending/input_lost."""
    if not isinstance(td, dict):
        return ("unknown", False)
    if bool(td.get("input_lost")):
        return ("input-lost", False)
    if bool(td.get("retired")):
        return ("retired", False)
    if bool(td.get("cleanup_pending")):
        return ("cleanup-pending", False)
    if not bool(td.get("bound")):
        return ("unbound", False)
    return ("bound", True)


def _freshness(last_heard: Optional[int], now: Optional[int]) -> Dict[str, Any]:
    """Last-HEARD age only (time since the node's last packet) — never a battery/link *measurement* age, which
    the snapshot carries no timestamp for. A future/invalid last-heard is clock ambiguity, not a fresh 0.
    ``read_at_epoch`` is the request/render time, not a provider capture time (the provider supplies none)."""
    base = {"last_heard_epoch": last_heard, "telemetry_age": "unknown",
            "read_at_epoch": now, "read_at_is": "request-time"}
    if last_heard is None or now is None:
        return {**base, "last_heard_age_s": "unknown", "clock": "unknown"}
    if last_heard > now:
        return {**base, "last_heard_age_s": "unknown", "clock": "ambiguous"}
    return {**base, "last_heard_age_s": now - last_heard, "clock": "ok"}


def build_status(provider: Optional[Callable[[], Any]], *, now: Any) -> Dict[str, Any]:
    """Build the read-only status dict from the injected snapshot *provider*. Never raises. Telemetry is
    exposed only for an admitted, config-ready owner on a healthy transport; every other case is an explicit
    ``available: false`` unavailable observation."""
    if provider is None:
        return _unavailable("provider-absent", now)
    try:
        snap = provider()
    except Exception:  # noqa: BLE001 — a provider fault is an unavailable observation, not a handler crash
        return _unavailable("provider-error", now)
    if not isinstance(snap, dict):
        return _unavailable("provider-error", now)

    # Transport summary is read from the real envelope up front, so every unavailable result reports it.
    transport, transport_healthy = _transport(snap.get("transport_status"))
    # Validate the caller's owner id to a strict integer identity BEFORE any equality, then select the owner
    # by comparing validated integer ids — never raw ``==``, which would let a boolean ``True`` alias owner
    # ``1`` (or a float coerce). A malformed row is never selected, and no neighbor is substituted.
    my_num = _node_id(snap.get("my_node_num"))
    if my_num is None:
        return _unavailable("owner-not-admitted", now, transport)
    nodes = snap.get("nodes")
    rows = nodes if isinstance(nodes, list) else []
    owner_row = next((r for r in rows if isinstance(r, dict) and _node_id(r.get("num")) == my_num), None)
    if owner_row is None:
        return _unavailable("owner-not-admitted", now, transport)

    # Readiness — admission, inventory readiness, and transport are separate layers.
    if bool(snap.get("retired")):
        return _unavailable("transport-retired", now, transport)
    if not transport_healthy:
        return _unavailable("transport-uncertain", now, transport)
    boot = snap.get("bootstrap_status")
    admitted = bool(boot.get("publication_admitted")) if isinstance(boot, dict) else False
    if not admitted:
        return _unavailable("owner-not-admitted", now, transport)
    cfg = snap.get("config_status")
    if not isinstance(cfg, dict):
        cfg = {}
    config_ready = (bool(snap.get("config_complete")) and cfg.get("state") == "ready"
                    and not cfg.get("inventory_stale") and not cfg.get("resync_required"))
    if not config_ready:
        return _unavailable("inventory-not-ready", now, transport)

    # Admitted + config-ready + healthy transport: project allowed fields, then map (never raises).
    try:
        readings = {r.kind: r for r in node_to_readings(_AttrNode(_project_node(owner_row)))}
    except Exception:  # noqa: BLE001 — a malformed selected value is unavailable, never a crash
        return _unavailable("provider-error", now, transport)

    return {
        "available": True,
        "identity": _identity(readings.get(ReadingKind.DEVICE_INFO), my_num),
        "battery": _battery(readings.get(ReadingKind.BATTERY)),
        "link": _link(readings.get(ReadingKind.LINK)),
        "freshness": _freshness(_epoch(owner_row.get("last_heard")), _epoch(now)),
        "readiness": {"config_complete": True, "publication_admitted": True, "transport": transport},
    }


def _identity(reading: Any, my_num: int) -> Dict[str, Any]:
    if reading is None:
        return {"num": my_num, "name": str(my_num)}
    extra = reading.extra if isinstance(reading.extra, dict) else {}
    return {"num": extra.get("num", my_num), "name": str(reading.label or reading.value or my_num)}


def _battery(reading: Any) -> Dict[str, Any]:
    if reading is None:
        return {"state": "unknown", "percent": None, "voltage": None}
    extra = reading.extra if isinstance(reading.extra, dict) else {}
    voltage = extra.get("voltage")
    if reading.value == "external":
        return {"state": "external", "percent": None, "voltage": voltage}
    return {"state": "percent", "percent": reading.value, "voltage": voltage}


def _link(reading: Any) -> Dict[str, Any]:
    # Only the SNR link-quality signal; channel-utilization is intentionally not surfaced in this slice.
    if reading is None:
        return {"state": "unknown", "snr_db": None}
    return {"state": "known", "snr_db": reading.value}
