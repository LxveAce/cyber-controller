"""Deterministic, allowlist-only preview of explicitly-supplied report facts.

This is the *safe* counterpart to the log-and-host-state bundle in :mod:`src.core.diagnostics`.
Where ``collect_report`` reads real host state (platform, OS username, home path, the log ring) and
then relies on pattern-matched redaction to remove secrets, this module does the opposite and much
smaller job: it takes a dict of facts the caller *already holds and explicitly hands in* (an app name,
a version, a build/channel, a status/error-code) and projects only the keys on a fixed ALLOWLIST into
a preview object, while listing every supplied-but-withheld key under ``omitted`` so the user can see
exactly what is being left out.

Guarantees, by construction:

* It reads NOTHING. No logs, settings, environment variables, file paths, identities, clocks, or any
  host state — the only input is the ``facts`` mapping the caller passes. Output is a pure function of
  that input (byte-identical across runs, machines, and dict insertion order).
* It ALLOWLISTS; it does not redact. A field is present because its key is on the allowlist AND its
  value is a simple, bounded scalar — never because a regex judged it "clean". So the module makes no
  claim to detect or scrub PII hidden inside a supplied value; the honesty note says so out loud.
* It NEVER uploads, submits, or opens an issue. It builds a local preview object (and an optional text
  rendering); moving that anywhere is a separate, caller-initiated act.

Values are restricted to ``None``/bool/int-with-bounded-decimal-width/finite-float/bounded-``str`` so
the preview stays small, JSON-portable, and always renderable. A supplied key that is off-allowlist, an
unsupported type, an over-cap string, or an over-wide integer is dropped and reported in ``omitted``
with a fixed reason rather than silently discarded or truncated. Every emitted label (an included key,
an omitted name, or an allowlist entry) is itself required to be a bounded, UTF-8-renderable string; an
unrenderable label is never copied into the output — it is replaced with a fixed placeholder and a
fixed reason, so building or rendering a preview can never raise on something it accepted. The count of
supplied fields and the allowlist size are bounded too, so a caller cannot force an unbounded preview.
"""

from __future__ import annotations

import itertools
from typing import Any, Iterable, Mapping

FORMAT = "cc-report-preview-v1"

# The default set of known-safe, structured report facts. Deliberately identifiers only — no free-text
# field is on the default allowlist, because free text is exactly where an un-noticed identifier would
# ride along. A caller that wants to surface a bounded free-text field opts in via the ``allow`` arg,
# with full knowledge that this module does not scrub what it lets through.
DEFAULT_ALLOW: tuple[str, ...] = (
    "app",
    "build",
    "channel",
    "component",
    "error_code",
    "status",
    "version",
)

# A supplied string value larger than this (encoded as UTF-8) is omitted, not truncated: a truncated
# value can still leak, and truncation would make the output depend on the cap in surprising ways.
MAX_VALUE_BYTES = 256

# An emitted label (an included key, an omitted name, or an allowlist entry) larger than this, encoded
# as UTF-8, is treated as unrenderable and replaced with a placeholder rather than copied into output.
MAX_KEY_BYTES = 128

# A supplied integer whose decimal representation would exceed this many digits is omitted, not
# rendered. CPython caps int<->str conversion (``sys.get_int_max_str_digits()``, default 4300) and
# raises ``ValueError`` past it, so an unbounded int placed in ``included`` would make ``render_text``
# raise on a value the preview claimed to accept. The bound is checked WITHOUT converting the int to a
# string, by comparison against a fixed power of ten, so the check itself can never trip that ceiling.
MAX_INT_DIGITS = 40
_INT_ABS_LIMIT = 10 ** MAX_INT_DIGITS  # |value| >= this has more than MAX_INT_DIGITS decimal digits

# Bounded totals: a caller may not force an unbounded preview by supplying an unbounded number of fields
# or an unbounded allowlist. Exceeding either is a structural misuse and raises, deterministically.
MAX_FIELDS = 256
MAX_ALLOW = 256

# The fixed, ASCII-only stand-in copied into the output in place of any label that is not itself a
# bounded, UTF-8-renderable string. It carries no caller-supplied bytes, so it is always renderable.
PLACEHOLDER_LABEL = "<unrenderable-label>"

# Fixed, honest notes carried in every preview. They state the privacy limits plainly, including the one
# the brief insists on: allowlisting is the guarantee, pattern-matched PII detection is NOT promised.
NOTES: tuple[str, ...] = (
    "Preview includes only explicitly-supplied fields on the allowlist; every other field you "
    "supplied is listed under 'omitted'.",
    "It reads no logs, settings, environment variables, file paths, identities, clocks, or host "
    "state — the only input is the facts you pass in.",
    "Fields are chosen by allowlist, not removed by pattern matching, so this makes no claim to "
    "detect or scrub personal data inside a value you supply.",
    "Nothing here is uploaded or submitted; this is a local preview only.",
)

# Fixed omission reason codes (stable strings, safe to branch on downstream).
REASON_NOT_ALLOWLISTED = "not_allowlisted"
REASON_UNSUPPORTED_TYPE = "unsupported_type"
REASON_TOO_LONG = "too_long"
REASON_INVALID_STRING = "invalid_string"
REASON_INVALID_KEY = "invalid_key"
REASON_TOO_MANY_FIELDS = "too_many_fields"
REASON_TOO_MANY_ALLOW = "too_many_allow"


def _normalize_allow(allow: Iterable[str] | None) -> tuple[str, ...]:
    """Return a sorted, de-duplicated tuple of allowlist keys. ``None`` yields :data:`DEFAULT_ALLOW`.

    Raises ``TypeError`` if *allow* is not an iterable of ``str`` (a defensive guard so a caller cannot
    accidentally pass a bare string — which is iterable per-character — or non-string keys).

    Consumes at most ``MAX_ALLOW + 1`` entries from *allow* so an unbounded (or infinite) iterable is
    never fully materialized: pulling one entry past the cap is enough to reject deterministically. An
    over-cap allowlist is a bounded rejection (``ValueError``), not a hang."""
    if allow is None:
        return DEFAULT_ALLOW
    if isinstance(allow, (str, bytes)):
        raise TypeError("allow must be an iterable of str keys, not a single string")
    # Bounded consumption: take only up to MAX_ALLOW + 1 entries. If the iterable yields that many, it
    # is definitively over the cap and we reject WITHOUT draining the rest (which may be infinite).
    keys = list(itertools.islice(allow, MAX_ALLOW + 1))
    if len(keys) > MAX_ALLOW:
        raise ValueError(f"allow has too many entries (max {MAX_ALLOW}): {REASON_TOO_MANY_ALLOW}")
    for key in keys:
        if type(key) is not str:
            raise TypeError(f"allow keys must be str, got {type(key).__name__}")
    return tuple(sorted(set(keys)))


def _check_value(value: Any) -> tuple[bool, str | None]:
    """``(ok, reason)`` for a supplied value. Accepts ``None``/bool/int/finite float/bounded str."""
    # ``None`` is a legitimate explicit value (e.g. status not yet known) and is kept as JSON null.
    if value is None:
        return True, None
    # bool is a subclass of int and renders as ``true``/``false`` (never via ``str(int)``), so it is
    # always accepted. A genuine int is accepted only when its decimal width is bounded, so a huge int
    # can never reach ``included`` and then make ``render_text`` raise at ``str(value)`` (see
    # MAX_INT_DIGITS). The bound is a magnitude comparison, so it never converts the int to a string.
    if isinstance(value, bool):
        return True, None
    if type(value) is int:
        if -_INT_ABS_LIMIT < value < _INT_ABS_LIMIT:
            return True, None
        return False, REASON_TOO_LONG
    if type(value) is float:
        # Reject NaN/inf so the projection stays strict-JSON portable and fully deterministic.
        if value != value or value in (float("inf"), float("-inf")):
            return False, REASON_UNSUPPORTED_TYPE
        return True, None
    if type(value) is str:
        try:
            encoded = value.encode("utf-8")  # a lone surrogate is not encodable
        except UnicodeEncodeError:
            return False, REASON_INVALID_STRING
        if len(encoded) > MAX_VALUE_BYTES:
            return False, REASON_TOO_LONG
        return True, None
    return False, REASON_UNSUPPORTED_TYPE


def _safe_label(key: Any) -> tuple[str, str | None]:
    """Return ``(display_label, reason)`` for an emitted label.

    A valid, bounded, UTF-8-encodable ``str`` key returns ``(key, None)`` — it may be copied into the
    output verbatim. Anything else — a non-``str`` key, a ``str`` carrying an un-encodable code point
    (e.g. a lone surrogate), or a ``str`` whose UTF-8 length exceeds :data:`MAX_KEY_BYTES` — returns
    ``(PLACEHOLDER_LABEL, REASON_INVALID_KEY)``. The placeholder carries no caller bytes, so no
    unrenderable text is ever copied into the preview object or its text rendering."""
    if type(key) is not str:
        return PLACEHOLDER_LABEL, REASON_INVALID_KEY
    try:
        encoded = key.encode("utf-8")  # a lone surrogate is not encodable
    except UnicodeEncodeError:
        return PLACEHOLDER_LABEL, REASON_INVALID_KEY
    if len(encoded) > MAX_KEY_BYTES:
        return PLACEHOLDER_LABEL, REASON_INVALID_KEY
    return key, None


def build_preview(facts: Mapping[str, Any], *, allow: Iterable[str] | None = None) -> dict[str, Any]:
    """Project *facts* into a deterministic, allowlist-only preview object.

    *facts* is the caller's explicitly-supplied mapping — nothing is read from the environment. The
    result is a plain dict with sorted, stable contents:

    * ``format`` — the fixed :data:`FORMAT` tag.
    * ``allow`` — the sorted allowlist actually applied (self-describing).
    * ``included`` — allowlisted keys whose supplied value is a valid bounded scalar, in key order.
    * ``omitted`` — sorted labels of supplied keys that were withheld.
    * ``omitted_detail`` — ``label -> reason`` for each omitted key.
    * ``notes`` — the fixed honesty/privacy notes.

    A key that is on the allowlist but never supplied is simply absent from both ``included`` and
    ``omitted`` (this module never invents a value for it). ``omitted`` reflects only fields the caller
    actually supplied and this projection declined to include. Every label placed in ``included``,
    ``omitted``, ``omitted_detail`` or ``allow`` is a bounded, UTF-8-renderable string; an unrenderable
    supplied key is reported under a fixed placeholder (:data:`PLACEHOLDER_LABEL`) with a fixed reason
    instead of being copied through. Raises ``TypeError`` if *facts* is not a mapping, and ``ValueError``
    if the number of supplied fields exceeds :data:`MAX_FIELDS` (the allowlist size is bounded by
    :data:`MAX_ALLOW` in the same way). Does not mutate *facts*; ``included`` values are the original
    immutable scalars."""
    if not isinstance(facts, Mapping):
        raise TypeError(f"facts must be a mapping, got {type(facts).__name__}")
    if len(facts) > MAX_FIELDS:
        raise ValueError(f"facts has too many keys (max {MAX_FIELDS}): {REASON_TOO_MANY_FIELDS}")
    allowed = _normalize_allow(allow)
    allowed_set = set(allowed)

    included: dict[str, Any] = {}
    omitted_detail: dict[str, str] = {}
    saw_invalid_key = False

    for key, value in facts.items():
        label, label_reason = _safe_label(key)
        if label_reason is not None:
            # The key itself is not a renderable str, so it can never be allowlisted. Every such key
            # collapses to the SAME fixed placeholder label with the SAME reason, so we do not write it
            # into ``omitted_detail`` here — only record that one was seen. Writing it in-loop would let
            # a caller-supplied valid string literally equal to PLACEHOLDER_LABEL and a sanitized
            # invalid key overwrite each other by insertion order; tracking the invalid case in a
            # separate flag and applying it below (with fixed precedence) keeps the result
            # order-independent. No caller bytes from an invalid key are copied into output.
            saw_invalid_key = True
            continue
        # label == key here: a valid, bounded, UTF-8-renderable str.
        if key not in allowed_set:
            omitted_detail[label] = REASON_NOT_ALLOWLISTED
            continue
        ok, reason = _check_value(value)
        if ok:
            included[key] = value
        else:
            omitted_detail[label] = reason or REASON_UNSUPPORTED_TYPE

    # Report any unrenderable key exactly once, under the fixed placeholder label. This is applied
    # AFTER the loop with fixed precedence: if a caller also supplied a valid off-allowlist key literally
    # equal to PLACEHOLDER_LABEL (which the loop recorded as ``not_allowlisted``), the invalid-key reason
    # wins deterministically — the same result for every insertion order, and the presence of a genuinely
    # unrenderable key is always surfaced rather than being masked by a coincidental collision.
    if saw_invalid_key:
        omitted_detail[PLACEHOLDER_LABEL] = REASON_INVALID_KEY

    # Every allowlist entry is also an emitted label; sanitize any that is not itself renderable. An
    # unrenderable entry is inert for matching (no renderable supplied key can equal it), so using the
    # raw entries above for ``allowed_set`` is safe while the displayed list stays fully encodable.
    allow_display = sorted({_safe_label(entry)[0] for entry in allowed})

    return {
        "format": FORMAT,
        "allow": allow_display,
        "included": {k: included[k] for k in sorted(included)},
        "omitted": sorted(omitted_detail),
        "omitted_detail": {k: omitted_detail[k] for k in sorted(omitted_detail)},
        "notes": list(NOTES),
    }


def _render_scalar(value: Any) -> str:
    """Deterministic display form of an included scalar."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_text(preview: Mapping[str, Any]) -> str:
    """Render a :func:`build_preview` object to a deterministic, human-readable preview string.

    Pure formatting of an already-built preview — reads nothing, computes nothing new."""
    lines: list[str] = [f"# {preview.get('format', FORMAT)}", ""]

    included = preview.get("included") or {}
    lines.append("## Included fields")
    if included:
        for key in included:  # already stored in sorted order by build_preview
            lines.append(f"{key}: {_render_scalar(included[key])}")
    else:
        lines.append("(none)")

    detail = preview.get("omitted_detail") or {}
    lines += ["", "## Omitted fields (withheld from this preview)"]
    omitted = preview.get("omitted") or []
    if omitted:
        for label in omitted:
            lines.append(f"{label}  -- {detail.get(label, 'omitted')}")
    else:
        lines.append("(none)")

    lines += ["", "## Notes"]
    for note in preview.get("notes") or NOTES:
        lines.append(f"- {note}")

    return "\n".join(lines)
