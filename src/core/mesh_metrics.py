"""Meshtastic telemetry -> canonical metrics bridge.

Meshtastic's real path is the protobuf stream (``meshtastic_stream``), which keeps every node's
telemetry — battery, voltage, GPS position, SNR, uptime, hop distance — in its ``nodes`` state. That
state never flowed into the canonical metrics layer, so a mesh node's battery/position/link didn't
show on the reformed Dashboard. This module is the missing map: a ``MeshNode`` becomes canonical
:class:`~src.core.metrics.Reading`s, keyed per node, so a Meshtastic node surfaces
through the SAME MetricsModel tiles as any serial device.

- :func:`node_to_readings` — pure: a MeshNode -> Readings (battery / GPS / link SNR / identity).
- :func:`feed_mesh_node` — push those into a :class:`~src.core.metrics.MetricsModel`.

Read-only: this only maps state the stream already decoded. No send, no channel crypto, no Qt. The
app wires it from its ``on_event`` handler — on a ``mesh_node`` event, look up the full node in
``stream.nodes`` (the event dict is lossy) and call :func:`feed_mesh_node`. safety.py untouched.
"""
from __future__ import annotations

from typing import Any

from src.core.metrics import Medium, MetricsModel, Reading, ReadingKind


def _node_source(node: Any) -> str:
    """A stable per-node device key for the metrics store: ``mesh:<node_id or num>``."""
    ident = getattr(node, "node_id", "") or getattr(node, "num", "")
    return f"mesh:{ident}"


def _node_name(node: Any) -> str:
    return (getattr(node, "long_name", "") or getattr(node, "short_name", "")
            or getattr(node, "node_id", "") or str(getattr(node, "num", "")))


def node_to_readings(node: Any, device_source: str = "") -> list[Reading]:
    """Map a Meshtastic ``MeshNode``'s telemetry to canonical readings. Only present fields become
    readings (a node that reported no battery yields no BATTERY reading — never a phantom value)."""
    src = device_source or _node_source(node)
    out: list[Reading] = []

    # DEVICE_INFO — node identity + topology (always emitted so the node appears on the Dashboard).
    parts = [_node_name(node)]
    hw = getattr(node, "hw_model_name", "")
    if hw:
        parts.append(hw)
    role = getattr(node, "role_label", "")
    if role:
        parts.append(role)
    hops = getattr(node, "hops_away", None)
    if hops is not None:
        parts.append("direct" if hops == 0 else f"{hops} hops")
    summary = " · ".join(p for p in parts if p) or "mesh node"
    out.append(Reading(ReadingKind.DEVICE_INFO, Medium.LORA, summary, "", summary, src,
                       extra={"num": getattr(node, "num", None),
                              "via_mqtt": getattr(node, "via_mqtt", False)}))

    # BATTERY — 0..100 %, or 101 = externally powered (Meshtastic's sentinel).
    batt = getattr(node, "battery", None)
    if batt is not None:
        volt = getattr(node, "voltage", None)
        if batt == 101:
            label, value = "external power", "external"
        else:
            label, value = f"{batt}%", batt
        extra = {"voltage": volt} if volt is not None else {}
        unit = "" if value == "external" else "%"
        out.append(Reading(ReadingKind.BATTERY, Medium.LORA, value, unit, label, src, extra=extra))

    # GPS_FIX — from Position.latitude_i/longitude_i (already decoded to decimal degrees).
    if getattr(node, "has_position", False):
        lat, lon = node.latitude, node.longitude
        extra = {"lat": lat, "lon": lon}
        alt = getattr(node, "altitude", None)
        if alt is not None:
            extra["alt"] = alt
        out.append(Reading(ReadingKind.GPS_FIX, Medium.LORA, f"{lat},{lon}", "",
                           f"{lat:.5f}, {lon:.5f}", src, extra=extra))

    # LINK — LoRa link quality (SNR), plus channel-utilization telemetry in extra.
    snr = getattr(node, "snr", None)
    if snr is not None:
        extra = {}
        for key in ("channel_util", "air_util_tx", "uptime", "hops_away"):
            v = getattr(node, key, None)
            if v is not None:
                extra[key] = v
        out.append(Reading(ReadingKind.LINK, Medium.LORA, snr, "dB", f"snr {snr} dB", src,
                           extra=extra))

    return out


def feed_mesh_node(model: MetricsModel, node: Any, device_source: str = "") -> list[Reading]:
    """Map *node* + store its readings in *model*, keyed per node. The app calls this on a
    ``mesh_node`` event (resolve the node from ``stream.nodes`` first). Returns the readings."""
    return [model.update(r) for r in node_to_readings(node, device_source)]
