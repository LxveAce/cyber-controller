"""Project admitted BLE ingestion facts into the journal's typed fact shape (adapter slice 1).

Pure allowlisting from the parsed event's own fields (addressed sightings) or the retained
observation record (addressless reports), plus the immutable attachment provenance captured at
attach. Never a raw event, Target, serial line, or unlisted key. Each function returns a bounded
fact dict or ``None`` (rejected); it never raises on bad input, never normalizes content, no I/O.

The adapter enforces its OWN default primitive/UTF-8/byte bounds here, so an optional custom
sink never receives an invalid or oversized fact; a journal with SMALLER limits may still reject
a projected fact separately on submit. Each fact gets a FRESH ``source`` dict, so a sink that
mutates one fact's source cannot affect another fact or the internal attachment provenance.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

# Fixed set of submit receipts the adapter records; any out-of-set value counts as unknown.
SUBMIT_RECEIPTS = (
    "queued", "rejected:invalid", "rejected:queue_full", "rejected:degraded", "rejected:closing",
)
RECEIPT_UNKNOWN = "submission_unknown"

# Adapter default bounds — match the journal core defaults, enforced here regardless of sink limits.
_MAX_LABEL_BYTES = 512
_MAX_SOURCE_BYTES = 256
_RSSI_MIN, _RSSI_MAX = -128, 127
_MAC_RE = re.compile(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}")
_ADDRESS_TYPES = ("public", "random")


def _bounded_str(value: Any, cap: int) -> Optional[str]:
    """*value* if it is a real ``str`` encoding to valid UTF-8 within *cap* bytes, else None."""
    if type(value) is not str:
        return None
    try:
        encoded = value.encode("utf-8")     # a lone surrogate / non-encodable primitive is invalid
    except UnicodeEncodeError:
        return None
    if len(encoded) > cap:
        return None
    return value


def _valid_mac(value: Any) -> Optional[str]:
    """The lowercased address if *value* is a whole-string six-colon-hex-octet MAC, else None.

    Uses ``fullmatch``, so a trailing newline (or any extra character) is rejected.
    """
    if type(value) is not str or _MAC_RE.fullmatch(value) is None:
        return None
    return value.lower()


def _rssi(data: Mapping[str, Any]) -> tuple[bool, Optional[int]]:
    """``(ok, rssi)``: a present int in [-128, 127] (never bool) stays itself; an absent key or
    explicit null stays None (distinct from zero). A bool/out-of-range/other type rejects."""
    if "rssi" not in data:
        return True, None
    value = data["rssi"]
    if value is None:
        return True, None
    if type(value) is int and _RSSI_MIN <= value <= _RSSI_MAX:
        return True, value
    return False, None


def _source(source: Mapping[str, Any]) -> Optional[dict]:
    """A FRESH ``{port, firmware, connection_id}`` dict, each value bounded to the source byte cap,
    or None if any is not a bounded str. Fresh per call, so no fact shares a mutable source dict."""
    if not isinstance(source, Mapping):
        return None
    out = {}
    for key in ("port", "firmware", "connection_id"):
        bounded = _bounded_str(source.get(key, ""), _MAX_SOURCE_BYTES)
        if bounded is None:
            return None
        out[key] = bounded
    return out


def project_addressed(data: Mapping[str, Any], source: Mapping[str, Any]) -> Optional[dict]:
    """A ``ble_found`` parsed event -> a typed ``ble_found`` fact, or None.

    Every present ``mac``/``addr`` must be a whole-string MAC (a present null/malformed value
    rejects) and they must agree when both present. ``name`` when present must be a bounded str
    (absent stays ""); ``rssi`` keeps null distinct from zero and rejects bool/out-of-range/text;
    only an explicit public/random ``type`` maps. Uses no Target and infers no address type.
    """
    if not isinstance(data, Mapping):
        return None
    src = _source(source)
    if src is None:
        return None
    addresses = []
    for key in ("mac", "addr"):
        if key in data:
            norm = _valid_mac(data[key])
            if norm is None:
                return None             # a present but null/malformed address rejects the sighting
            addresses.append(norm)
    if not addresses or any(a != addresses[0] for a in addresses):
        return None                     # no address at all, or mac/addr disagree
    ok, rssi = _rssi(data)
    if not ok:
        return None
    if "name" in data:
        label = _bounded_str(data["name"], _MAX_LABEL_BYTES)
        if label is None:
            return None                 # a present invalid/oversized name rejects (no invented "")
    else:
        label = ""
    raw_type = data.get("type")
    address_type = raw_type if (type(raw_type) is str and raw_type in _ADDRESS_TYPES) else None
    return {
        "kind": "ble_found", "source": src, "address": addresses[0],
        "address_type": address_type, "label": label, "rssi": rssi,
        "report_meta": {}, "meta": {},
    }


def project_report(retained: Mapping[str, Any], source: Mapping[str, Any]) -> Optional[dict]:
    """A retained addressless observation -> a typed ``ble_observation`` fact, or None.

    Uses only the snapshot's bounded fields and its captured scan epoch; address/address_type are
    always null and a MAC-shaped label stays a label. The label is bounded to the adapter cap, so
    an otherwise-valid oversized report is rejected here rather than offered.
    """
    if not isinstance(retained, Mapping):
        return None
    src = _source(source)
    if src is None:
        return None
    label = _bounded_str(retained.get("label"), _MAX_LABEL_BYTES)
    if label is None:
        return None
    return {
        "kind": "ble_observation", "source": src, "address": None, "address_type": None,
        "label": label, "rssi": retained.get("rssi"),
        "report_meta": {
            "reported_index": retained.get("reported_index"),
            "format": retained.get("format"),
            "label_truncated": retained.get("label_truncated"),
            "scan_epoch": retained.get("scan_epoch"),
        },
        "meta": {},
    }
