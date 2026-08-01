"""Shared ``<...>`` placeholder resolution for firmware command templates (pure, Qt-free).

A catalog ``CommandInfo`` name can carry ``<...>`` placeholders (``add -c -b <mac> -ap <idx>``).
The Devices terminal and the Operate console both turn such a template + operator input into the
concrete line sent to the device — and they MUST produce the byte-identical string, or the same op
sends differently by surface. Single-sourcing the primitives here (the regex, the token list, the
sanitizer, the occurrence-ordered substitution) makes that identity structural rather than a
coincidence two copies happen to share.

Extracted verbatim from the Devices tab's private helpers; ``device_tab`` and ``op_spec.op_command``
both delegate here so neither can drift.
"""
from __future__ import annotations

import re

PLACEHOLDER_RE = re.compile(r"<([^>]+)>")


def placeholder_tokens(cmd: str) -> "list[str]":
    """The ``<...>`` placeholder names in *cmd*, in order, duplicates kept (e.g. ``['v', 'v']``)."""
    return PLACEHOLDER_RE.findall(cmd)


def sanitize_arg(value: str) -> str:
    """Clean a user-entered argument: strip, drop control chars + DEL, strip angle brackets (so a
    value can't smuggle a new ``<token>``), cap at 64 chars (mirrors ``action_resolver``)."""
    value = value.strip()
    value = "".join(ch for ch in value if ch >= " " and ch != "\x7f")
    return value.replace("<", "").replace(">", "")[:64]


def substitute_tokens(cmd: str, values: "list[str]") -> str:
    """Occurrence-ordered substitution: replace each ``<...>`` with the next value (handles a
    repeated token like ``<v> <v>``). The caller supplies at least one value per token."""
    it = iter(values)
    return PLACEHOLDER_RE.sub(lambda _: next(it), cmd)
