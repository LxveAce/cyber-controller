"""Nodes view (W1.1) for the Tkinter frontend — the tk mirror of the Qt ``nodes_tab``.

Binds to the SAME UI-agnostic :class:`~src.core.nodes_controller.NodesController`: a KEY-FREE table (never
any key bytes — the keys live only in the gate-sealed vault), a gate-locked notice that FAILS CLOSED when the
vault is locked, and provision / rotate / deprovision / attach / detach actions that delegate straight to the
controller. No crypto or vault logic lives here — the view only reads ``list_rows()`` / ``available_gateways()``
/ ``is_unlocked()`` and calls the controller's mutators.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Optional


class NodesView(ttk.Frame):
    _COLS = ("node", "label", "role", "tx", "rx", "connected", "attached")
    _HEADS = ("Node", "Label", "Role", "TX epoch", "RX epoch", "Connected", "Attached")
    _REFRESH_MS = 2000   # re-poll the gate every 2 s (parity with the Qt tab's QTimer)

    def __init__(self, parent: "tk.Misc", controller) -> None:
        super().__init__(parent)
        self._ctrl = controller
        self._after_id = None

        self._banner = ttk.Label(
            self,
            text=("⚠  Preview — the relay/node ESP32 sketches now ship in firmware/ (source-only: compile "
                  "and flash them yourself). Live attach/detach over the air from this view isn't wired up "
                  "yet — it's coming in a later release."),
            wraplength=560, foreground="#8a6d00", padding=(8, 6),
        )
        self._banner.pack(fill=tk.X, padx=4, pady=(4, 0))

        self._locked_label = ttk.Label(
            self, text="\N{LOCK}  Vault locked — unlock the access gate to manage nodes.")

        self._tree = ttk.Treeview(self, columns=self._COLS, show="headings", height=10)
        for c, h in zip(self._COLS, self._HEADS):
            self._tree.heading(c, text=h)
            self._tree.column(c, width=90, anchor=tk.W)

        self._btn_row = ttk.Frame(self)
        self._buttons: "list[ttk.Button]" = []
        for text, cmd in (
            ("Provision…", self._on_provision),
            ("Rotate key", self._on_rotate),
            ("Deprovision", self._on_deprovision),
            ("Attach…", self._on_attach),
            ("Detach", self._on_detach),
            ("Refresh", self._refresh),
        ):
            b = ttk.Button(self._btn_row, text=text, command=cmd)
            b.pack(side=tk.LEFT, padx=2)
            self._buttons.append(b)

        self._refresh()
        self._schedule_next()

    # ── periodic gate re-poll ────────────────────────────────────────
    def _schedule_next(self) -> None:
        # Without this the tab would freeze on whatever gate-state it was BUILT in: constructed while locked
        # (the normal startup order) it disables/hides its own Refresh button, so it could never recover to a
        # usable state after the user unlocks — and a relock would leave stale rows up. The timer re-polls so
        # the view tracks the gate both ways. No Tk mainloop in tests -> this never fires there (safe).
        if self.winfo_exists():
            self._after_id = self.after(self._REFRESH_MS, self._tick)

    def _tick(self) -> None:
        if not self.winfo_exists():   # widget torn down between scheduling and firing
            return
        self._refresh()
        self._schedule_next()

    # ── refresh / gate state (fails CLOSED) ──────────────────────────
    def _refresh(self) -> None:
        # ANY error (incl. the gate racing unlocked->locked between the check and the read) fails CLOSED —
        # table hidden + actions disabled — rather than showing stale or secret-adjacent state.
        try:
            unlocked = bool(self._ctrl.is_unlocked())
            rows = self._ctrl.list_rows() if unlocked else []
        except Exception:  # noqa: BLE001
            unlocked, rows = False, []
        self._tree.delete(*self._tree.get_children())
        if unlocked:
            self._locked_label.pack_forget()
            self._tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
            self._btn_row.pack(fill=tk.X, padx=4, pady=(0, 4))
            for r in rows:
                rx = r.get("rx_epoch")
                self._tree.insert("", "end", values=(
                    str(r["node_id"]),
                    r.get("label", ""),
                    r.get("role", ""),
                    str(r.get("tx_epoch", "")),
                    str(rx) if rx is not None else "",
                    "yes" if r.get("connected") else "no",
                    "yes" if r.get("attached") else "no",
                ))
            for b in self._buttons:
                b.state(["!disabled"])
        else:
            self._tree.pack_forget()
            self._btn_row.pack_forget()
            self._locked_label.pack(padx=12, pady=12)
            for b in self._buttons:
                b.state(["disabled"])

    def _selected_node_id(self) -> Optional[int]:
        sel = self._tree.selection()
        if not sel:
            return None
        try:
            return int(self._tree.item(sel[0], "values")[0])
        except (ValueError, IndexError):
            return None

    # ── delegated actions (unit-tested; no dialogs) ──────────────────
    def _do_provision(self, node_id: int, role: str = "host", label: str = "") -> None:
        self._ctrl.provision(node_id, role=role, label=label)
        self._refresh()

    def _do_rotate(self, node_id: int) -> None:
        self._ctrl.rotate(node_id)
        self._refresh()

    def _do_deprovision(self, node_id: int) -> None:
        self._ctrl.deprovision(node_id)
        self._refresh()

    def _do_attach(self, node_id: int, gateway_port: str) -> None:
        self._ctrl.attach_via_port(node_id, gateway_port)
        self._refresh()

    def _do_detach(self, node_id: int) -> None:
        self._ctrl.detach(node_id)
        self._refresh()

    # ── dialog handlers (delegate to _do_*; not unit-tested) ─────────
    def _on_provision(self) -> None:
        from tkinter import messagebox, simpledialog
        nid = simpledialog.askinteger("Provision node", "Node ID (0–65535):",
                                      parent=self, minvalue=0, maxvalue=65535)
        if nid is None:
            return
        role = simpledialog.askstring("Provision node", "Role (host/node):",
                                      parent=self, initialvalue="host") or "host"
        label = simpledialog.askstring("Provision node", "Label (optional):", parent=self) or ""
        try:
            self._do_provision(nid, role, label)
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("Provision failed", str(exc), parent=self)

    def _on_rotate(self) -> None:
        from tkinter import messagebox
        nid = self._selected_node_id()
        if nid is None:
            return
        if not messagebox.askyesno(
            "Rotate key", f"Rotate the key for node {nid}? The old key stops working — "
                          "the node must be re-flashed.", parent=self):
            return
        try:
            self._do_rotate(nid)
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("Rotate failed", str(exc), parent=self)

    def _on_deprovision(self) -> None:
        from tkinter import messagebox
        nid = self._selected_node_id()
        if nid is None:
            return
        if not messagebox.askyesno("Deprovision node",
                                   f"Delete node {nid} and its key from the vault?", parent=self):
            return
        try:
            self._do_deprovision(nid)
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("Deprovision failed", str(exc), parent=self)

    def _on_attach(self) -> None:
        from tkinter import messagebox, simpledialog
        nid = self._selected_node_id()
        if nid is None:
            return
        gateways = self._ctrl.available_gateways()
        if not gateways:
            messagebox.showinfo("Attach node",
                                "No connected gateway device. Connect a serial dongle (Devices tab) to "
                                "relay this node first.", parent=self)
            return
        ports = [g["port"] for g in gateways]
        choice = simpledialog.askstring(
            "Attach node", "Gateway port (one of: " + ", ".join(ports) + "):",
            parent=self, initialvalue=ports[0])
        if not choice:
            return
        if choice not in ports:
            messagebox.showwarning("Attach node", f"{choice!r} is not a connected gateway.", parent=self)
            return
        try:
            self._do_attach(nid, choice)
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("Attach failed", str(exc), parent=self)

    def _on_detach(self) -> None:
        from tkinter import messagebox
        nid = self._selected_node_id()
        if nid is None:
            return
        try:
            self._do_detach(nid)
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("Detach failed", str(exc), parent=self)
