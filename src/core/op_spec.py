"""Pure derivations from a firmware ``CommandInfo`` to the Biscuit ``OperationDetail``'s inputs.

The Spade design (P2) makes ``biscuit.OperationDetail`` the one way an operator runs any op — its
HelpSheet, its ModeSegment, and the command string it sends all come from the connected firmware's
``CommandInfo`` (``ci``). Today ``operate_tab`` re-derives that inline (a raw ``QInputDialog`` for
args, a hand-built tooltip), which is neither reusable nor testable. This module is that derivation
pulled out **pure and Qt-free**, so the widget wiring reads one clean seam and every renderer (Qt,
web) builds the SAME op UX from the same catalog. It classifies danger with :func:`safety.classify`
— the exact call the send path uses — so the help label and the enforcement never disagree.

Nothing here sends or touches a device; it only shapes data. The guarded send (``safety.classify``
+ ``tx_hard_block`` two-factor arm + confirm) stays exactly where it is in ``operate_tab._send``.
"""
from __future__ import annotations

import re
from typing import Any

from src.core import safety
from src.core.placeholders import placeholder_tokens, sanitize_arg, substitute_tokens


def op_help_spec(ci: Any) -> dict:
    """The HelpSheet spec for a command: what it does, the args it takes, and its danger label.

    Danger is the authoritative :func:`safety.classify` verdict (not the raw catalog field), so the
    help sheet reads the same class the send path enforces (``""`` / ``lab-only`` / ``illegal-tx``).
    """
    name = (getattr(ci, "name", "") or "").strip()
    return {
        "title": name,
        "description": (getattr(ci, "description", "") or "").strip(),
        "args": (getattr(ci, "args", "") or "").strip(),
        "danger": safety.classify(name, ci),
    }


def pretty_label(ci: Any) -> str:
    """A human display label for a command that has no ``description`` — a DISPLAY-ONLY fallback.
    The firmware catalogs populate ``description``, so this mainly serves description-less test
    stubs. It NEVER feeds :func:`op_command` or the send path; the raw ``name`` stays the sole sent
    string. Drops ``<...>`` / ``[...]`` placeholder tokens and ``-flag`` args, then title-cases.
    """
    name = (getattr(ci, "name", "") or "").strip()
    if not name:
        return "?"
    cleaned = re.sub(r"[<\[][^>\]]*[>\]]", " ", name)      # <mac>, [idx] -> gone
    cleaned = re.sub(r"(?:^|\s)-\S+", " ", cleaned)         # -t, -b flags -> gone
    words = [w for w in re.split(r"[\s_-]+", cleaned) if w]
    if not words:
        return name
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def op_modes(ci: Any) -> list[str]:
    """The ModeSegment presets for a command, replacing the raw QInputDialog.

    A command with no args runs one way (``"Run"``); a command that takes args offers ``"Manual"``
    (the operator enters the argument string). Richer structured presets (Basic/Targeted/…) land as
    the catalog grows structured args — until then this stays honest and does not invent modes the
    firmware doesn't expose.
    """
    return ["Manual"] if (getattr(ci, "args", "") or "").strip() else ["Run"]


def op_command(ci: Any, arg: str = "") -> str:
    """Resolve the command string to send from a catalog verb + the operator's argument input.

    Two shapes, matching how the catalog names commands:

    * **Templated verb** — the name carries ``<...>`` placeholders (``add -c -b <mac> -ap <idx>``).
      The arg is split into one value per placeholder occurrence and substituted IN PLACE, exactly
      as the Devices terminal's :func:`~src.core.placeholders.substitute_tokens` does — so both
      surfaces send the byte-identical line (the invariant pinned by the cross-surface test). A
      single placeholder takes the whole arg (so a spaced value survives); the last of several
      absorbs any remainder. If the arg does not fill EVERY placeholder, ``""`` is returned so the
      guarded send no-ops — mirroring the terminal's blank-field cancel, an incomplete templated
      verb is never sent (no literal ``<mac>`` on the wire, no dangling half-command).
    * **Plain verb** — no placeholders: the bare verb, or ``"verb arg"`` when an arg is given. If
      the operator's *arg* already begins with the verb (they retyped it), it is used verbatim so
      the verb is never doubled.

    Pure string-building only — the guarded send (``safety.classify`` + ``tx_hard_block`` two-factor
    arm + confirm) still authorizes the write in ``operate_tab._send``; this only shapes the string.
    """
    name = (getattr(ci, "name", "") or "").strip()
    arg = (arg or "").strip()
    tokens = placeholder_tokens(name)
    if tokens:
        n = len(tokens)
        # One arg field -> one value per placeholder occurrence (the last absorbs any remainder).
        parts = arg.split(None, n - 1) if arg else []
        values = [sanitize_arg(v) for v in parts]
        if len(values) < n or any(v == "" for v in values):
            # Not every placeholder is filled -> "" so the guarded send no-ops, mirroring the
            # Devices terminal's blank-field cancel. Never emit a literal "<path>" or a dangling
            # half-command, and never StopIteration in substitute_tokens (values are complete here).
            return ""
        return substitute_tokens(name, values)
    if not arg:
        return name
    if arg == name or arg.startswith(name + " "):
        return arg
    return f"{name} {arg}"
