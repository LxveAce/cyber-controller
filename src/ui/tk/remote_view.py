"""Remote view for the Tkinter frontend — the tk mirror of the web touch-first Remote home (MB).

A one-tap quick-command panel: for a firmware, it renders the SAME UI-agnostic catalog the web Remote uses
(:func:`src.core.quick_commands.grouped_quick_commands`) — argument-free commands sourced straight from the
real protocol registry (never a phantom command), grouped by category, each tagged with its
:mod:`src.core.safety` danger level. A flagged button confirms before sending (label-never-block — the confirm
always offers "proceed"); a safe button fires straight through. No serial/crypto here: the view calls the
injected ``send`` callback (the app wires it to the active connection's guarded write) and an injectable
``confirm`` (defaults to a yes/no dialog), so it is fully headless-testable — exactly like the tk Device View.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, List, Optional

from src.core.quick_commands import QuickCommand, grouped_quick_commands

_LAB_FG = "#e08a3c"       # lab-only  → amber
_ILLEGAL_FG = "#e5534b"   # illegal-tx → red
_DIM_FG = "#8b8fa3"
_BTN_BG = "#161b22"
_MAX_LABEL = 30


def firmwares_with_quick_commands() -> "List[str]":
    """Firmware ids that expose ≥1 one-tap command — the Remote's picker options. Never raises."""
    try:
        from src.protocols import PROTOCOLS
    except Exception:  # noqa: BLE001
        return []
    out = []
    for name in PROTOCOLS:
        if any(cmds for _cat, cmds in grouped_quick_commands(name)):
            out.append(name)
    return out


class RemoteView(ttk.Frame):
    """A one-tap quick-command grid. ``send(command)`` fires a command; ``confirm(danger, command) -> bool``
    gates a flagged one (defaults to a messagebox)."""

    def __init__(self, parent: "tk.Misc", *, firmware: Optional[str] = None,
                 send: "Optional[Callable[[str], None]]" = None,
                 confirm: "Optional[Callable[[str, str], bool]]" = None,
                 firmwares: "Optional[List[str]]" = None) -> None:
        super().__init__(parent)
        self._send = send
        self._confirm = confirm or self._default_confirm
        self._firmwares = firmwares if firmwares is not None else firmwares_with_quick_commands()
        self._shown: "List[QuickCommand]" = []

        # ── firmware chooser ──
        top = ttk.Frame(self)
        ttk.Label(top, text="Firmware:").pack(side=tk.LEFT, padx=(0, 6))
        self._fw_combo = ttk.Combobox(top, state="readonly", width=22, values=self._firmwares)
        self._fw_combo.pack(side=tk.LEFT)
        self._fw_combo.bind("<<ComboboxSelected>>", self._on_fw_selected)
        top.pack(fill=tk.X, padx=8, pady=(8, 4))

        # ── scrollable command area (Canvas + inner Frame) ──
        mid = ttk.Frame(self)
        mid.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))
        self._canvas = tk.Canvas(mid, bg="#0d1117", highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._inner = ttk.Frame(self._canvas)
        self._inner_id = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>", lambda _e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfigure(self._inner_id, width=e.width))

        self._status = ttk.Label(self, text="Select a firmware to load its one-tap commands.", foreground=_DIM_FG)
        self._status.pack(fill=tk.X, padx=8, pady=(0, 8))

        if firmware is not None:
            self.set_firmware(firmware)

    # ── public API ────────────────────────────────────────────────────
    def set_firmware(self, firmware: Optional[str]) -> None:
        """Load a firmware's one-tap commands (unknown/empty → an empty panel with a notice)."""
        groups = grouped_quick_commands(firmware) if firmware else []
        self._shown = [qc for _cat, cmds in groups for qc in cmds]
        if firmware and firmware in self._firmwares:
            try:
                self._fw_combo.current(self._firmwares.index(firmware))
            except (ValueError, tk.TclError):
                pass
        self._render(groups)

    def shown_commands(self) -> "List[tuple]":
        """(command, danger) for every button currently shown — exposed for tests."""
        return [(qc.command, qc.danger) for qc in self._shown]

    def activate(self, command: str) -> None:
        """Fire the shown command with the given command string (the headless entry point)."""
        for qc in self._shown:
            if qc.command == command:
                self._fire(qc)
                return

    # ── internals ─────────────────────────────────────────────────────
    def _fire(self, qc: QuickCommand) -> None:
        if qc.danger and not self._confirm(qc.danger, qc.command):
            self._set_status(f"cancelled: {qc.command}", _DIM_FG)
            return
        if self._send is None:
            self._set_status(f"preview (no connection): {qc.command}", _DIM_FG)
            return
        try:
            self._send(qc.command)
        except Exception as exc:  # noqa: BLE001 — surface, never crash the view
            self._set_status(f"error: {exc}", _ILLEGAL_FG)
            return
        self._set_status(f"» sent: {qc.command}", "#3fb950")

    def _default_confirm(self, danger: str, command: str) -> bool:
        return bool(messagebox.askyesno(
            "Confirm command",
            f"Controlled / authorized use only ({danger}):\n\n{command}\n\nProceed?",
            icon=messagebox.WARNING, parent=self))

    def _render(self, groups) -> None:
        for child in self._inner.winfo_children():
            child.destroy()
        if not groups:
            ttk.Label(self._inner, text="No one-tap commands for this firmware — use the terminal.",
                      foreground=_DIM_FG).pack(anchor=tk.W, padx=4, pady=6)
            self._set_status("", _DIM_FG)
            return
        for category, cmds in groups:
            box = ttk.LabelFrame(self._inner, text=category)
            box.pack(fill=tk.X, expand=True, padx=2, pady=4)
            for i, qc in enumerate(cmds):
                fg = _ILLEGAL_FG if qc.danger == "illegal-tx" else (_LAB_FG if qc.danger else "#e6edf3")
                text = qc.label if len(qc.label) <= _MAX_LABEL else qc.label[:_MAX_LABEL - 1] + "…"
                if qc.danger:
                    text = f"{text}  [{qc.danger}]"
                btn = tk.Button(box, text=text, anchor=tk.W, fg=fg, bg=_BTN_BG,
                                activebackground="#1f6feb", activeforeground="#ffffff",
                                relief=tk.FLAT, padx=8, pady=4, highlightthickness=0,
                                command=lambda q=qc: self._fire(q))
                btn.grid(row=i // 2, column=i % 2, sticky="ew", padx=3, pady=2)
            box.columnconfigure(0, weight=1)
            box.columnconfigure(1, weight=1)
        n = len(self._shown)
        flagged = sum(1 for qc in self._shown if qc.danger)
        self._set_status(f"{n} one-tap commands ({flagged} flagged — confirm first). Authorized use only.",
                         _DIM_FG)

    def _on_fw_selected(self, _evt=None) -> None:
        idx = self._fw_combo.current()
        if 0 <= idx < len(self._firmwares):
            self.set_firmware(self._firmwares[idx])

    def _set_status(self, text: str, fg: str) -> None:
        self._status.config(text=text, foreground=fg)
