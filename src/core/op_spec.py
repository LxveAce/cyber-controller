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

from typing import Any

from src.core import safety


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


def op_modes(ci: Any) -> list[str]:
    """The ModeSegment presets for a command, replacing the raw QInputDialog.

    A command with no args runs one way (``"Run"``); a command that takes args offers ``"Manual"``
    (the operator enters the argument string). Richer structured presets (Basic/Targeted/…) land as
    the catalog grows structured args — until then this stays honest and does not invent modes the
    firmware doesn't expose.
    """
    return ["Manual"] if (getattr(ci, "args", "") or "").strip() else ["Run"]


def op_command(ci: Any, arg: str = "") -> str:
    """Resolve the command string to send: the bare verb, or ``"verb <arg>"`` when an arg is given.

    This is the one place the ``operate_tab`` QInputDialog string-building lives, so the widget can
    stop hand-splicing verbs. If the operator's *arg* already begins with the verb (they retyped it,
    as the old dialog seeded), it is used verbatim — the verb is never doubled.
    """
    name = (getattr(ci, "name", "") or "").strip()
    arg = (arg or "").strip()
    if not arg:
        return name
    if arg == name or arg.startswith(name + " "):
        return arg
    return f"{name} {arg}"
