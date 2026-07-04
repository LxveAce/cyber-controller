"""Device View for the Tkinter frontend — the tk mirror of the Qt ``device_view`` and the web ``/device``.

Renders a firmware's reconstructed on-screen menu (an honest SKIN, not a pixel mirror) as a navigable list,
driven by the SAME UI-agnostic model the Qt/web views use (:func:`src.core.device_menus.menu_tree`). Every leaf
is bound to the firmware's real serial command and carries a danger label from the shared safety classifier;
a flagged leaf confirms before sending (label-never-block — the confirm always offers "proceed"), and a leaf
whose command needs an argument is shown but not fired. No serial/crypto logic lives here: the view calls the
injected ``send`` callback (the app wires it to the active connection's guarded write) and an injectable
``confirm`` (defaults to a yes/no dialog) so it is fully headless-testable.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Optional

from src.core.device_menus import SKINS, menu_tree

_LAB_FG = "#e08a3c"       # lab-only  → amber
_ILLEGAL_FG = "#e5534b"   # illegal-tx → red
_MENU_FG = "#8b8fa3"      # submenu caret / back


class DeviceView(ttk.Frame):
    """A navigable, firmware-skinned device screen. ``send(command)`` fires a leaf's real command;
    ``confirm(danger, command) -> bool`` gates a flagged leaf (defaults to a messagebox)."""

    def __init__(self, parent: "tk.Misc", *, firmware: Optional[str] = None,
                 send: "Optional[Callable[[str], None]]" = None,
                 confirm: "Optional[Callable[[str, str], bool]]" = None) -> None:
        super().__init__(parent)
        self._send = send
        self._confirm = confirm or self._default_confirm
        self._skin_keys = list(SKINS)
        self._tree: Optional[dict] = None
        self._path: "list[int]" = []          # indices into nested menus (mirrors the Qt model's path)

        # ── firmware chooser ──
        top = ttk.Frame(self)
        ttk.Label(top, text="Firmware skin:").pack(side=tk.LEFT, padx=(0, 6))
        self._fw_combo = ttk.Combobox(top, state="readonly", width=24,
                                      values=[SKINS[k][0] for k in self._skin_keys])
        self._fw_combo.pack(side=tk.LEFT)
        self._fw_combo.bind("<<ComboboxSelected>>", self._on_fw_selected)
        top.pack(fill=tk.X, padx=8, pady=(8, 4))

        # ── breadcrumb + back ──
        nav = ttk.Frame(self)
        self._back_btn = ttk.Button(nav, text="‹ Back", command=self._on_back, state=tk.DISABLED)
        self._back_btn.pack(side=tk.LEFT)
        self._crumb = ttk.Label(nav, text="", foreground=_MENU_FG)
        self._crumb.pack(side=tk.LEFT, padx=(8, 0))
        nav.pack(fill=tk.X, padx=8, pady=(0, 4))

        # ── the "screen": a navigable list of the current menu level ──
        self._list = tk.Listbox(self, height=12, activestyle="dotbox",
                                bg="#0d1117", fg="#e6edf3", selectbackground="#1f6feb",
                                highlightthickness=1, highlightbackground="#30363d", exportselection=False)
        self._list.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))
        self._list.bind("<Double-Button-1>", self._on_activate)
        self._list.bind("<Return>", self._on_activate)

        self._status = ttk.Label(self, text="Select a firmware skin to preview its on-screen menu.",
                                 foreground=_MENU_FG)
        self._status.pack(fill=tk.X, padx=8, pady=(0, 8))

        if firmware is not None:
            self.set_firmware(firmware)

    # ── public API ────────────────────────────────────────────────────
    def set_firmware(self, firmware: Optional[str]) -> None:
        """Load a firmware's reconstructed menu (None/unknown → an empty screen with a notice)."""
        self._tree = menu_tree(firmware)
        self._path = []
        if self._tree is not None:
            # reflect the resolved skin in the chooser
            try:
                self._fw_combo.current(self._skin_keys.index(self._tree["firmware"]))
            except (ValueError, tk.TclError):
                pass
        self._render()

    def current_items(self) -> "list[dict]":
        """The menu nodes at the current depth (empty if no/failed skin) — exposed for tests."""
        if self._tree is None:
            return []
        items = self._tree["root"]
        for idx in self._path:
            items = items[idx]["children"]
        return items

    def breadcrumb(self) -> str:
        if self._tree is None:
            return ""
        node, parts = self._tree["root"], []
        for idx in self._path:
            parts.append(node[idx]["label"])
            node = node[idx]["children"]
        return " › ".join([self._tree["title"]] + parts)

    def activate(self, index: int) -> None:
        """Enter a submenu or fire a leaf at ``index`` in the current level (the headless entry point)."""
        items = self.current_items()
        if not (0 <= index < len(items)):
            return
        node = items[index]
        if node.get("children") is not None and "command" not in node:
            self._path.append(index)
            self._render()
            return
        self._fire_leaf(node)

    # ── internals ─────────────────────────────────────────────────────
    def _fire_leaf(self, node: dict) -> None:
        command = node.get("command")
        if node.get("needs_arg"):
            self._set_status(f"needs an argument: {command} — use the terminal", _LAB_FG)
            return
        if not command:
            return
        danger = node.get("danger") or ""
        if danger and not self._confirm(danger, command):
            self._set_status(f"cancelled: {command}", _MENU_FG)
            return
        if self._send is None:
            self._set_status(f"preview (no connection): {command}", _MENU_FG)
            return
        try:
            self._send(command)
        except Exception as exc:  # noqa: BLE001  — surface, never crash the view
            self._set_status(f"error: {exc}", _ILLEGAL_FG)
            return
        self._set_status(f"» sent: {command}", "#3fb950")

    def _default_confirm(self, danger: str, command: str) -> bool:
        return bool(messagebox.askyesno(
            "Confirm command",
            f"Controlled / authorized use only ({danger}):\n\n{command}\n\nProceed?",
            icon=messagebox.WARNING, parent=self))

    def _render(self) -> None:
        self._list.delete(0, tk.END)
        self._crumb.config(text=self.breadcrumb())
        self._back_btn.config(state=(tk.NORMAL if self._path else tk.DISABLED))
        if self._tree is None:
            self._set_status("No reconstructed on-screen menu for this firmware — use the terminal.", _MENU_FG)
            return
        for i, node in enumerate(self.current_items()):
            if node.get("children") is not None and "command" not in node:
                self._list.insert(tk.END, f"{node['label']}    ›")
                self._list.itemconfig(i, foreground=_MENU_FG)
            else:
                suffix, fg = "", None
                if node.get("needs_arg"):
                    suffix = "   (needs arg)"
                elif node.get("danger") == "illegal-tx":
                    suffix, fg = "   [illegal-tx]", _ILLEGAL_FG
                elif node.get("danger") == "lab-only":
                    suffix, fg = "   [lab-only]", _LAB_FG
                self._list.insert(tk.END, f"{node['label']}{suffix}")
                if fg:
                    self._list.itemconfig(i, foreground=fg)
        if self._path:
            self._set_status("", _MENU_FG)
        else:
            self._set_status(f"{self._tree['title']} — authorized use only. Flagged commands confirm first.",
                             _MENU_FG)

    def _on_fw_selected(self, _evt=None) -> None:
        idx = self._fw_combo.current()
        if 0 <= idx < len(self._skin_keys):
            self.set_firmware(self._skin_keys[idx])

    def _on_back(self) -> None:
        if self._path:
            self._path.pop()
            self._render()

    def _on_activate(self, _evt=None) -> None:
        sel = self._list.curselection()
        if sel:
            self.activate(sel[0])

    def _set_status(self, text: str, fg: str) -> None:
        self._status.config(text=text, foreground=fg)
