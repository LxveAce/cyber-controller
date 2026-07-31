"""Pure, Qt-free cross-surface hand-off descriptor for the Spade GUI (P3 flow-spine).

A ``FlowIntent`` is the app taking the operator's *next step* for them: one surface hands an object
to another and (optionally) asks it to act — "this handshake -> Crack Lab", "this target ->
Operate", "this drive -> the map" — instead of making the operator hand-carry data between screens.
Like :mod:`src.core.nav_model`, this is the IA expressed once as data, with **no Qt import and no
side effects**, so it is fully headless-testable and every renderer dispatches the same intents.

The intent carries KEYS + a REFERENCE — never widgets, never copies:

* ``surface_key`` / ``sub_view`` are nav_model keys (surface e.g. ``"crack"``, child e.g.
  ``"crack_lab"``) — the destination.
* ``object_ref`` is a reference to an existing shared-model object (a pooled Target, a
  ``CaptureRecord``, a device port), handed to the destination's receive method.
* ``action`` is the destination widget's receive-verb (e.g. ``"load_capture"`` / ``"select_device"``)
  — the method :meth:`main_window.dispatch_intent` calls on the destination.
* ``auto`` defaults False = navigate + deliver only. A True intent MAY additionally trigger the
  action, but any device send still routes through the EXISTING guarded path — a FlowIntent never
  introduces a new send path and never auto-arms.

Honest-functionality is structural here too: :func:`is_routable` checks the destination against the
SAME visible-nav key set the rail is built from, so an intent to an unprovided surface (e.g. the
reserved ``"sense"``) is inert by construction rather than silently mis-routed.

P3 substrate only — the dispatcher + receive methods land alongside this; the per-surface *emitters*
(the "Send to Crack Lab" / "Operate this device" / "View on map" affordances) are the later slices.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class FlowIntent:
    """One typed cross-surface hand-off. Immutable + Qt-free — see the module docstring."""

    surface_key: str
    action: str = ""
    object_ref: object = None
    sub_view: Optional[str] = None
    auto: bool = False


def is_routable(intent: "FlowIntent", visible_keys: Iterable[str]) -> bool:
    """True iff *intent*'s destination surface is currently backed (present in *visible_keys*).

    *visible_keys* is the surface-key set the rail is built from — pass
    ``[n.key for n in nav_model.visible_nav(caps)]`` (plus the settings key). An intent to a
    capability-gated surface with no provider (e.g. ``"sense"``) is therefore NOT routable, and the
    dispatcher drops it with no effect — the same honest-functionality property as the nav tree.
    """
    return bool(intent.surface_key) and intent.surface_key in set(visible_keys)
