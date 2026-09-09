"""Offline SigMF recording-metadata inspector (bounded, pure, read-only).

A small original, standard-library-only summarizer/validator for the text of a SigMF
``.sigmf-meta`` document. It never touches a radio, driver, filesystem, network, clock, or
child process; a referenced filename or URL is inert metadata, never followed.

API
---
    summarize_sigmf_metadata(data) -> dict

``data`` is the explicitly supplied metadata as ``str`` or ``bytes`` (JSON text). The result is
deterministic::

    {
      "status": "summarized" | "unsupported" | "invalid",
      "summary": <object when summarized, else None>,
      "diagnostics": [ {"field": str, "code": str, "detail": str}, ... up to 16 ],
      "diagnostics_truncated": bool,
    }

``"summarized"`` means the recognized core descriptors listed below were checked -- it is NOT a
whole-document SigMF conformance verdict. Missing information stays explicit; no sample-count,
duration, hardware, byte-order (beyond the datatype suffix), or file-existence is inferred.

Example
-------
    >>> r = summarize_sigmf_metadata(
    ...     '{"global":{"core:datatype":"cf32_le","core:version":"1.2.6"},'
    ...     ' "captures":[{"core:sample_start":0}], "annotations":[]}')
    >>> r["status"]
    'summarized'
    >>> r["summary"]["datatype"]["bytes_per_channel_sample"]
    8

Supported subset
----------------
Initial declared-version support is exactly SigMF ``1.2.6``. Any other well-formed ``X.Y.Z``
version returns ``"unsupported"`` (this is a deliberate initial product subset, not a claim that
other files are invalid). Recognized ``core:`` descriptors are validated at the input boundary;
geolocation, datetime, uuid, uri/doi and collection relationships are preserved but left
explicitly uninterpreted. Unknown ``core:`` fields and non-core namespaces are preserved within
bounds, distinctly labeled uninterpreted; a string type check is not a claim of URI/calendar/
GeoJSON compliance.

Reference / terms
-----------------
Written as original scoped code from the SigMF specification, release v1.2.6:
  - prose:  https://raw.githubusercontent.com/sigmf/SigMF/v1.2.6/additional_content.md
  - schema: https://raw.githubusercontent.com/sigmf/SigMF/v1.2.6/sigmf-schema.json
The SigMF specification text/schema are licensed CC BY-SA 4.0 (observed in the repository's
LICENSE.md); this module copies neither the schema JSON nor the specification prose.
"""

import json
import math
import re

SPEC_VERSION = "1.2.6"

# Application limits (NOT SigMF limits).
MAX_INPUT_BYTES = 262144
MAX_CAPTURES = 4096
MAX_ANNOTATIONS = 4096
MAX_EXTENSIONS = 64
MAX_KEYS_PER_OBJECT = 128
MAX_STRING_BYTES = 8192
MAX_DEPTH = 32               # root object is level 1
MAX_TOTAL_VALUES = 32768     # dict/list/scalar nodes; object keys are checked separately
MAX_DIAG_RECORDS = 16
MAX_DETAIL_CHARS = 160
MAX_FIELD_CHARS = 96
MAX_SUMMARY_ENTRIES = 256    # bounded capture/annotation entries surfaced in the summary

_INT64_MAX = 9223372036854775807
_FREQ_ABS_MAX = 1000000000000        # +/- 1e12 (schema)
_RATE_MAX = 1000000000000            # exclusiveMinimum 0, maximum 1e12 (schema)

# Complete datatype grammar (prose): (real|complex) ((multibyte-type endianness) | byte-type).
# These are used with re.fullmatch so the WHOLE token must match: `$` alone would accept a trailing
# newline (it matches just before a final "\n"), which is not a complete/valid token.
_DATATYPE_RE = re.compile(r"(c|r)(?:(f32|f64|i32|i16|u32|u16)(_le|_be)|(i8|u8))")
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
_SHA512_RE = re.compile(r"[0-9a-fA-F]{128}")
_ELEMENT_BYTES = {"f32": 4, "f64": 8, "i32": 4, "i16": 2, "u32": 4, "u16": 2, "i8": 1, "u8": 1}
_ELEMENT_KIND = {"f": "float", "i": "int", "u": "uint"}

# Recognized core descriptors (used to separate recognized from uninterpreted/unknown).
_GLOBAL_SCALARS = {
    "core:datatype", "core:version", "core:sample_rate", "core:num_channels", "core:sha512",
    "core:offset", "core:trailing_bytes", "core:metadata_only", "core:dataset", "core:author",
    "core:hw", "core:description", "core:recorder", "core:license", "core:collection",
    "core:data_doi", "core:meta_doi",
}
_INERT_REFERENCE_FIELDS = ("core:dataset", "core:license", "core:data_doi", "core:meta_doi")
# Preserved but explicitly uninterpreted (semantics deferred).
_UNINTERPRETED_GLOBAL = {"core:geolocation", "core:extensions"}
# Recognized (validated + interpreted) numeric/index descriptors per array entry; everything else in an
# entry is preserved with its value but left uninterpreted.
_CAPTURE_RECOGNIZED = ("core:sample_start", "core:frequency", "core:global_index", "core:header_bytes")
_ANNOTATION_RECOGNIZED = ("core:sample_start", "core:sample_count", "core:freq_lower_edge", "core:freq_upper_edge")


class _Rejected(Exception):
    """Raised at the external input boundary for a malformed document."""
    def __init__(self, field, code, detail=""):
        self.field, self.code, self.detail = field, code, detail


class _DupKey(Exception):
    pass


def _utf8_len(s):
    """UTF-8 byte length. Raises _Rejected (not a crash) on an escaped/unpaired surrogate, which JSON can
    decode into a Python string that cannot be UTF-8 encoded."""
    try:
        return len(s.encode("utf-8"))
    except UnicodeEncodeError:
        raise _Rejected("$", "escaped_surrogate", "unpaired surrogate in a string/key")


def _diag(field, code, detail=""):
    return {
        "field": str(field)[:MAX_FIELD_CHARS],
        "code": str(code)[:MAX_FIELD_CHARS],
        "detail": str(detail)[:MAX_DETAIL_CHARS],
    }


def _result(status, summary, diags):
    truncated = len(diags) > MAX_DIAG_RECORDS
    return {
        "status": status,
        "summary": summary if status == "summarized" else None,
        "diagnostics": diags[:MAX_DIAG_RECORDS],
        "diagnostics_truncated": truncated,
    }


def _invalid(field, code, detail=""):
    return _result("invalid", None, [_diag(field, code, detail)])


def _object_pairs(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise _DupKey(k)
        obj[k] = v
    return obj


def _reject_constant(token):
    raise _Rejected("$", "non_finite_number", token)


def _decode(data):
    """Return validated UTF-8 text within the byte limit, or raise _Rejected."""
    if isinstance(data, bytes):
        if len(data) > MAX_INPUT_BYTES:
            raise _Rejected("$", "oversized_input", "%d bytes" % len(data))
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _Rejected("$", "invalid_utf8", str(exc)[:MAX_DETAIL_CHARS])
    if isinstance(data, str):
        try:
            raw = data.encode("utf-8")   # rejects lone/unpaired surrogates
        except UnicodeEncodeError as exc:
            raise _Rejected("$", "invalid_utf8", str(exc)[:MAX_DETAIL_CHARS])
        if len(raw) > MAX_INPUT_BYTES:
            raise _Rejected("$", "oversized_input", "%d bytes" % len(raw))
        return data
    raise _Rejected("$", "bad_input_type", type(data).__name__)


def _bounded_scan(root):
    """Bounded iterative shape scan before any semantic read. Enforces, and raises _Rejected on:
      - CONTAINER nesting depth (root object = level 1; scalar leaves do NOT add a level),
      - total value count (object keys are counted separately, via the per-object key-count limit),
      - keys-per-object, per-string/key UTF-8 byte length, escaped/unpaired surrogates, and
      - non-finite floats ANYWHERE (incl. exponent overflow such as 1e999, which JSON accepts as a number
        token and Python decodes to inf -- parse_constant only covers the NaN/Infinity literals).
    """
    stack = [(root, 1)]
    total = 0
    while stack:
        node, depth = stack.pop()
        total += 1
        if total > MAX_TOTAL_VALUES:
            raise _Rejected("$", "max_values", "> %d values" % MAX_TOTAL_VALUES)
        if isinstance(node, bool):
            continue                                  # bool is an int subclass; nothing to bound
        if isinstance(node, float):
            if not math.isfinite(node):
                raise _Rejected("$", "non_finite_number", "NaN/Infinity or numeric overflow")
            continue
        if isinstance(node, dict):
            if depth > MAX_DEPTH:                      # depth is checked only for containers
                raise _Rejected("$", "max_depth", "container level > %d" % MAX_DEPTH)
            if len(node) > MAX_KEYS_PER_OBJECT:
                raise _Rejected("$", "max_keys", "> %d keys in an object" % MAX_KEYS_PER_OBJECT)
            for k, v in node.items():
                if not isinstance(k, str) or _utf8_len(k) > MAX_STRING_BYTES:
                    raise _Rejected("$", "max_key_bytes", "key over %d bytes" % MAX_STRING_BYTES)
                stack.append((v, depth + 1))
        elif isinstance(node, list):
            if depth > MAX_DEPTH:
                raise _Rejected("$", "max_depth", "container level > %d" % MAX_DEPTH)
            for v in node:
                stack.append((v, depth + 1))
        elif isinstance(node, str):
            if _utf8_len(node) > MAX_STRING_BYTES:
                raise _Rejected("$", "max_string_bytes", "string over %d bytes" % MAX_STRING_BYTES)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and not (isinstance(v, float) and not math.isfinite(v)))


def _need_int(diags, field, v, lo, hi):
    if not _is_int(v):
        diags.append(_diag(field, "type", "expected integer"))
        return None
    if v < lo or v > hi:
        diags.append(_diag(field, "range", "outside [%d, %d]" % (lo, hi)))
        return None
    return v


def _need_number(diags, field, v, lo, hi, exclusive_lo=False):
    if not _is_number(v):
        diags.append(_diag(field, "type", "expected finite number"))
        return None
    if (v <= lo if exclusive_lo else v < lo) or v > hi:
        diags.append(_diag(field, "range", "outside allowed range"))
        return None
    return v


def _validate_datatype(diags, token):
    if not isinstance(token, str):
        diags.append(_diag("global.core:datatype", "type", "expected string"))
        return None
    m = _DATATYPE_RE.fullmatch(token)   # whole token must match; no trailing newline/junk
    if not m:
        diags.append(_diag("global.core:datatype", "grammar", "not a complete SigMF datatype token"))
        return None
    complex_ = m.group(1) == "c"
    element = m.group(2) or m.group(4)
    suffix = m.group(3)
    width = _ELEMENT_BYTES[element]
    if suffix:
        byte_order = "little" if suffix == "_le" else "big"
    else:
        byte_order = "inapplicable"   # single-byte formats have no endianness
    return {
        "raw": token,
        "complex": complex_,
        "element_format": element,
        "element_kind": _ELEMENT_KIND[element[0]],
        "element_bits": width * 8,
        "byte_order": byte_order,
        "bytes_per_channel_sample": width * (2 if complex_ else 1),
    }


def _classify_extensions(diags, ext_value):
    """Return (opaque_list, has_unsupported_required). Malformed shape -> raises via diags/invalid."""
    if not isinstance(ext_value, list):
        diags.append(_diag("global.core:extensions", "type", "expected array"))
        return None, False
    if len(ext_value) > MAX_EXTENSIONS:
        diags.append(_diag("global.core:extensions", "max_extensions", "> %d" % MAX_EXTENSIONS))
        return None, False
    opaque = []
    required_unsupported = False
    for i, e in enumerate(ext_value):
        where = "global.core:extensions[%d]" % i
        if not isinstance(e, dict):
            diags.append(_diag(where, "type", "expected object"))
            return None, False
        if set(e.keys()) != {"name", "version", "optional"}:
            diags.append(_diag(where, "shape", "must contain exactly name, version, optional"))
            return None, False
        if not isinstance(e["name"], str) or not isinstance(e["version"], str) or not isinstance(e["optional"], bool):
            diags.append(_diag(where, "type", "name/version string, optional boolean"))
            return None, False
        # Global extension description + example: optional==false means the extension is REQUIRED.
        if e["optional"] is False:
            required_unsupported = True   # this subset supports no extensions
        else:
            opaque.append({"name": e["name"], "version": e["version"], "optional": True,
                           "interpreted": False})
    return opaque, required_unsupported


def _check_starts(diags, items, field):
    """Validate each item's core:sample_start (type/range/presence) and its ordering.

    Per SigMF v1.2.6 BOTH captures and annotations MUST be sorted ascending by core:sample_start; a
    decreasing start is a diagnostic (i.e. invalid). Duplicates/order are preserved -- input is never
    sorted into apparent validity.
    """
    last = None
    for i, it in enumerate(items):
        loc = "%s[%d]" % (field, i)
        if not isinstance(it, dict):
            diags.append(_diag(loc, "type", "expected object"))
            continue
        if "core:sample_start" not in it:
            diags.append(_diag(loc + ".core:sample_start", "missing", "required"))
            continue
        v = _need_int(diags, loc + ".core:sample_start", it["core:sample_start"], 0, _INT64_MAX)
        if v is None:
            continue
        if last is not None and v < last:
            diags.append(_diag(loc + ".core:sample_start", "order", "%s must be ascending" % field))
        last = v


def _semantics_of(key):
    # A defined core: field whose meaning this subset does not interpret is "deferred"; anything else unknown.
    return "deferred" if key.startswith("core:") else "unknown"


def _entry_summary(entry, recognized_keys):
    """Split one capture/annotation object into recognized (interpreted) descriptors with their values and a
    separately labeled uninterpreted bucket that PRESERVES every other supplied value (deferred core fields
    like datetime/geolocation/uuid and unknown/non-core metadata) -- nothing is silently dropped."""
    recognized, uninterpreted = {}, {}
    for k, v in entry.items():
        if k in recognized_keys:
            recognized[k] = v
        else:
            uninterpreted[k] = {"value": v, "interpreted": False, "semantics": _semantics_of(k)}
    return {"recognized": recognized, "uninterpreted": uninterpreted}


def _entries(items, recognized_keys):
    """Bounded per-entry summaries preserving values. Returns (entries, truncated)."""
    out = [_entry_summary(it, recognized_keys) for it in items[:MAX_SUMMARY_ENTRIES] if isinstance(it, dict)]
    return out, len(items) > MAX_SUMMARY_ENTRIES


def summarize_sigmf_metadata(data):
    """Summarize/validate SigMF metadata text. Never raises for malformed data."""
    try:
        text = _decode(data)
        try:
            doc = json.loads(text, object_pairs_hook=_object_pairs, parse_constant=_reject_constant)
        except _DupKey as exc:
            raise _Rejected("$", "duplicate_key", str(exc)[:MAX_DETAIL_CHARS])
        except (ValueError, RecursionError) as exc:   # json.JSONDecodeError is a ValueError
            raise _Rejected("$", "json_parse", str(exc)[:MAX_DETAIL_CHARS])
        _bounded_scan(doc)
    except _Rejected as exc:
        return _invalid(exc.field, exc.code, exc.detail)

    diags = []

    # --- top-level shape (required containers) ---
    if not isinstance(doc, dict):
        return _invalid("$", "type", "top level must be an object")
    for key, typ, name in (("global", dict, "object"), ("captures", list, "array"),
                           ("annotations", list, "array")):
        if key not in doc:
            return _invalid(key, "missing", "required top-level %s" % name)
        if not isinstance(doc[key], typ):
            return _invalid(key, "type", "expected %s" % name)
    g, captures, annotations = doc["global"], doc["captures"], doc["annotations"]
    if len(captures) > MAX_CAPTURES:
        return _invalid("captures", "max_captures", "> %d" % MAX_CAPTURES)
    if len(annotations) > MAX_ANNOTATIONS:
        return _invalid("annotations", "max_annotations", "> %d" % MAX_ANNOTATIONS)

    # --- required global scalars: datatype + version (complete tokens) ---
    if "core:datatype" not in g:
        return _invalid("global.core:datatype", "missing", "required")
    if "core:version" not in g:
        return _invalid("global.core:version", "missing", "required")
    ver = g["core:version"]
    if not isinstance(ver, str) or not _VERSION_RE.fullmatch(ver):
        return _invalid("global.core:version", "grammar", "expected complete X.Y.Z version string")

    # --- version gate: exactly 1.2.6 supported; other well-formed versions -> unsupported ---
    if ver != SPEC_VERSION:
        return _result("unsupported", None,
                       [_diag("global.core:version", "unsupported_version",
                              "only %s is supported by this subset" % SPEC_VERSION)])

    # --- validate datatype token ---
    datatype = _validate_datatype(diags, g["core:datatype"])
    if datatype is None:
        return _result("invalid", None, diags)

    # --- recognized global numeric/scalar descriptors ---
    if "core:sample_rate" in g:
        _need_number(diags, "global.core:sample_rate", g["core:sample_rate"], 0, _RATE_MAX, exclusive_lo=True)
    num_channels = None
    if "core:num_channels" in g:
        num_channels = _need_int(diags, "global.core:num_channels", g["core:num_channels"], 1, _INT64_MAX)
    if "core:offset" in g:
        _need_int(diags, "global.core:offset", g["core:offset"], 0, _INT64_MAX)
    if "core:trailing_bytes" in g:
        _need_int(diags, "global.core:trailing_bytes", g["core:trailing_bytes"], 0, _INT64_MAX)
    if "core:sha512" in g:
        if not isinstance(g["core:sha512"], str) or not _SHA512_RE.fullmatch(g["core:sha512"]):
            diags.append(_diag("global.core:sha512", "grammar", "expected 128 hex chars (shape only)"))
    if "core:metadata_only" in g and not isinstance(g["core:metadata_only"], bool):
        diags.append(_diag("global.core:metadata_only", "type", "expected boolean"))
    for f in ("core:author", "core:hw", "core:description", "core:recorder", "core:license",
              "core:collection", "core:data_doi", "core:meta_doi", "core:dataset"):
        if f in g and not isinstance(g[f], str):
            diags.append(_diag("global." + f, "type", "expected string"))

    # --- captures / annotations required member + ordering ---
    for i, c in enumerate(captures):
        if isinstance(c, dict):
            for nf, lo, hi in (("core:frequency", -_FREQ_ABS_MAX, _FREQ_ABS_MAX),):
                if nf in c:
                    _need_number(diags, "captures[%d].%s" % (i, nf), c[nf], lo, hi)
            for intf in ("core:global_index", "core:header_bytes"):
                if intf in c:
                    _need_int(diags, "captures[%d].%s" % (i, intf), c[intf], 0, _INT64_MAX)
    _check_starts(diags, captures, "captures")
    for i, a in enumerate(annotations):
        if isinstance(a, dict):
            if "core:sample_count" in a:
                _need_int(diags, "annotations[%d].core:sample_count" % i, a["core:sample_count"], 0, _INT64_MAX)
            for nf in ("core:freq_lower_edge", "core:freq_upper_edge"):
                if nf in a:
                    _need_number(diags, "annotations[%d].%s" % (i, nf), a[nf], -_FREQ_ABS_MAX, _FREQ_ABS_MAX)
    _check_starts(diags, annotations, "annotations")

    # --- extensions: malformed shape -> invalid; a required extension -> unsupported ---
    opaque_ext = []
    required_ext_unsupported = False
    if "core:extensions" in g:
        opaque_ext, required_ext_unsupported = _classify_extensions(diags, g["core:extensions"])
        if opaque_ext is None:   # malformed extension shape
            return _result("invalid", None, diags)

    # Any recognized-descriptor failure so far -> invalid (not a whole-doc conformance verdict).
    if diags:
        return _result("invalid", None, diags)

    # --- unsupported semantic conditions (well-formed but this subset cannot fully honour) ---
    if required_ext_unsupported:
        return _result("unsupported", None,
                       [_diag("global.core:extensions", "unsupported_required_extension",
                              "a required (optional=false) extension is not supported")])
    # Conservative product-subset rule (NOT a universal SigMF conformance claim): if BOTH core:dataset and
    # core:metadata_only are present, the combination is unsupported regardless of the boolean's value.
    if "core:dataset" in g and "core:metadata_only" in g:
        return _result("unsupported", None,
                       [_diag("global", "dataset_and_metadata_only_present",
                              "core:dataset and core:metadata_only both present")])

    # --- build the bounded summary; preserve supplied values, never invent ---
    inert = {f: g[f] for f in _INERT_REFERENCE_FIELDS if f in g}
    recognized = {k: g[k] for k in _GLOBAL_SCALARS
                  if k in g and k not in ("core:datatype", "core:version") and k not in inert}
    uninterpreted_global = {}
    for k in g:
        if k == "core:extensions":
            continue                                    # surfaced via extensions_opaque (payload retained)
        if k in _UNINTERPRETED_GLOBAL or k not in _GLOBAL_SCALARS:
            uninterpreted_global[k] = {"value": g[k], "interpreted": False, "semantics": _semantics_of(k)}
    cap_entries, cap_trunc = _entries(captures, _CAPTURE_RECOGNIZED)
    ann_entries, ann_trunc = _entries(annotations, _ANNOTATION_RECOGNIZED)
    summary = {
        "format": "SigMF",
        "version": ver,
        "conformance_note": "Listed core descriptors were checked; not a whole-document SigMF conformance verdict.",
        "datatype": datatype,
        "num_channels": num_channels,   # present value or None; the spec default is NOT applied here
        "recognized_global": recognized,             # recognized scalar core fields, with values
        "uninterpreted_global": uninterpreted_global,  # deferred/unknown global metadata, values preserved
        "captures": {"count": len(captures), "empty": len(captures) == 0, "truncated": cap_trunc,
                     "entries": cap_entries,
                     "note": "empty: recording begins at sample 0, no capture metadata (no values invented)"
                             if len(captures) == 0 else ""},
        "annotations": {"count": len(annotations), "truncated": ann_trunc, "entries": ann_entries},
        "extensions_opaque": opaque_ext,        # optional extensions, payload retained, uninterpreted
        "inert_references": inert,              # filenames/URIs/DOIs: never followed or opened
    }
    return _result("summarized", summary, [])
