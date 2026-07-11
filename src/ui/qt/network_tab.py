"""Network graph (experimental "test" tab) — a draggable spider-web of how everything is connected.

Each connected device/board is a boxy node; every target it discovered (APs, clients, BLE) is a node linked
to it; cross-comm routing rules and AP<->client links are edges. Drag nodes to orient the web however you
like, and double-click a node to bring up its command/action list and execute from it. Especially handy for
wardriving + network-based work (see one device fan out to all the APs it found), and it's where the future
wireless-node mesh will surface as more nodes.

Pure-Qt, offscreen-testable: the scene/items build without a live device; data comes from the DeviceManager +
TargetPool when present.
"""

from __future__ import annotations

import math
from typing import Callable, Optional

from PyQt5.QtCore import QTimer, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QPainter, QPen
from PyQt5.QtWidgets import (
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# Node palette (mirrors the cyber_dark theme). kind -> (fill, border).
_KIND_COLORS = {
    "device": ("#16321a", "#3fb950"),   # the controllers / boards — green
    "ap": ("#10243d", "#58a6ff"),        # discovered access points — blue
    "client": ("#3a2410", "#f0883e"),    # client stations — orange
    "ble": ("#2a1a3d", "#d2a8ff"),       # BLE devices — purple
    "alpr": ("#3d1a1a", "#f85149"),      # Flock-style ALPR surveillance cameras — crimson (watching you)
    "node": ("#0d1117", "#8b949e"),      # generic / future remote nodes — grey
}
_NODE_W, _NODE_H = 150.0, 46.0

# Short subtitle badge for nodes with no text command channel (keyed by Device.driver_type). text-cli devices
# get no badge (the common case — keep the graph uncluttered).
_DRIVER_MARKERS = {
    "stream": "stream",
    "controlmap": "web-UI",
}

# Honest "nothing to send here" note per driver kind — a stream/control-map device has no serial command
# channel at all, unlike a text-CLI firmware that just exposes no commands in this menu.
_NO_COMMAND_NOTES = {
    "stream": "(no serial commands — protobuf stream, not a text CLI)",
    "controlmap": "(no serial commands — controlled via its web UI)",
}


class _Node(QGraphicsRectItem):
    """A draggable boxy node. Double-click pops its command/action menu."""

    def __init__(self, label: str, sub: str, kind: str,
                 actions: "list[tuple[str, Callable[[], None]]]") -> None:
        super().__init__(0, 0, _NODE_W, _NODE_H)
        self.kind = kind
        self.actions = actions
        self._edges: "list[_Edge]" = []
        fill, border = _KIND_COLORS.get(kind, _KIND_COLORS["node"])
        self.setBrush(QBrush(QColor(fill)))
        self.setPen(QPen(QColor(border), 1.6))
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setZValue(1)
        title = QGraphicsSimpleTextItem(label[:22], self)
        title.setBrush(QBrush(QColor("#e6edf3")))
        title.setPos(9, 6)
        if sub:
            st = QGraphicsSimpleTextItem(sub[:26], self)
            st.setBrush(QBrush(QColor(border)))
            f = st.font(); f.setPointSizeF(max(6.0, f.pointSizeF() - 2)); st.setFont(f)
            st.setPos(9, 24)

    def add_edge(self, edge: "_Edge") -> None:
        self._edges.append(edge)

    def itemChange(self, change, value):  # noqa: N802 (Qt signature)
        if change == QGraphicsItem.ItemPositionHasChanged:
            for e in self._edges:
                e.adjust()
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, event):  # noqa: N802 (Qt signature)
        self._show_menu(event)
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):  # noqa: N802 (Qt signature) — right-click also opens it
        self._show_menu(event)

    def _show_menu(self, event) -> None:
        if not self.actions:
            return
        menu = QMenu()
        for label, cb in self.actions:
            menu.addAction(label, cb)
        try:
            menu.exec_(event.screenPos())
        except Exception:  # noqa: BLE001 (offscreen / no screen pos)
            menu.exec_()


class _Edge(QGraphicsLineItem):
    """A line between two nodes that follows them as they're dragged."""

    def __init__(self, src: _Node, dst: _Node, color: str = "#30363d") -> None:
        super().__init__()
        self._src = src
        self._dst = dst
        self.setPen(QPen(QColor(color), 1.4))
        self.setZValue(0)
        src.add_edge(self)
        dst.add_edge(self)
        self.adjust()

    def adjust(self) -> None:
        s = self._src.sceneBoundingRect().center()
        d = self._dst.sceneBoundingRect().center()
        self.setLine(s.x(), s.y(), d.x(), d.y())


class _GraphView(QGraphicsView):
    """QGraphicsView with wheel-zoom; node items keep their own drag (view is NoDrag)."""

    def __init__(self, scene: QGraphicsScene) -> None:
        super().__init__(scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setBackgroundBrush(QBrush(QColor("#0d1117")))
        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self._user_zoomed = False   # once the user zooms, stop auto-framing
        self._refitting = False

    def wheelEvent(self, event):  # noqa: N802 (Qt signature)
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
        self._user_zoomed = True

    def fit_content(self) -> None:
        """Frame all nodes into the viewport (until the user zooms). Fixes the launch-render bug: the
        graph was built while the view had no size, so nodes painted off-centre/clipped and never
        auto-framed. Called on resize + after a rebuild."""
        from PyQt5.QtCore import Qt
        if self._user_zoomed or self._refitting:
            return
        rect = self.scene().itemsBoundingRect()
        if rect.isValid() and not rect.isEmpty() and self.viewport().width() > 1:
            self._refitting = True
            try:
                self.fitInView(rect, Qt.KeepAspectRatio)
            finally:
                self._refitting = False

    def resizeEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(ev)
        self.fit_content()


class NetworkTab(QWidget):
    """The experimental network-graph tab."""

    # Emitted (from any thread) when the shared TargetPool changes, so the graph auto-fills as a device
    # scans instead of only on a manual Rebuild. Delivered queued to the GUI thread, then debounced.
    _targets_changed = pyqtSignal()

    def __init__(self, device_manager=None, target_pool=None, action_resolver=None,
                 send_cmd: "Optional[Callable[[str, str], None]]" = None, event_bus=None) -> None:
        super().__init__()
        self._dm = device_manager
        self._pool = target_pool
        self._resolver = action_resolver
        self._send_cmd = send_cmd
        self._bus = event_bus
        self._nodes: "dict[str, _Node]" = {}

        root = QVBoxLayout(self)
        # Toolbar lives in its own container so Simple mode can hide the whole control row at once.
        self._controls = QWidget()
        bar = QHBoxLayout(self._controls)
        bar.setContentsMargins(0, 0, 0, 0)
        btn_rebuild = QPushButton("Rebuild")
        btn_rebuild.setToolTip("Re-read connected devices + discovered targets (keeps the layout you arranged)")
        btn_rebuild.clicked.connect(lambda: self.rebuild())
        bar.addWidget(btn_rebuild)
        btn_arrange = QPushButton("Auto-arrange")
        btn_arrange.setToolTip("Reset every node to the default fan-out layout")
        btn_arrange.clicked.connect(lambda: self._auto_arrange())
        bar.addWidget(btn_arrange)
        bar.addStretch(1)
        hint = QLabel("Drag nodes to orient the web · double-click a node for its commands · scroll to zoom")
        hint.setStyleSheet("color:#8b949e;")
        bar.addWidget(hint)
        root.addWidget(self._controls)

        self._scene = QGraphicsScene(self)
        self._view = _GraphView(self._scene)
        root.addWidget(self._view, 1)

        # Simple-mode stand-in for the (advanced, send-capable) graph. Hidden in Pro.
        self._simple_notice = QLabel(
            "The network graph is an advanced, experimental send surface — double-clicking a node "
            "fires a command at a device. Switch to Pro mode (View ▸ Interface Mode, or Ctrl+M) to "
            "use it."
        )
        self._simple_notice.setWordWrap(True)
        self._simple_notice.setStyleSheet("color:#8b949e;")
        self._simple_notice.hide()
        root.addWidget(self._simple_notice, 1)

        self.rebuild()

        # Live refresh: auto-rebuild when the shared TargetPool changes, so the graph fills in as a device
        # scans (WiFi/BLE) instead of only on a manual Rebuild. The bus callback fires on the ingest thread,
        # so it just emits a queued signal; a short single-shot debounce coalesces a burst of scan hits into
        # one relayout (the graph preserves dragged positions). No bus → no auto-refresh (manual Rebuild only).
        self._dirty = False   # a bus target-change arrived while hidden -> rebuild on the next show
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(400)
        self._refresh_timer.timeout.connect(self._debounced_rebuild)
        self._targets_changed.connect(self._refresh_timer.start)  # queued → GUI thread restarts the timer
        if self._bus is not None:
            for topic in ("target.added", "target.updated", "target.removed", "target.cleared"):
                try:
                    self._bus.subscribe(topic, self._on_bus_target_event)
                except Exception:  # noqa: BLE001 — never let bus wiring break tab construction
                    pass

    def _on_bus_target_event(self, _topic: str, _payload) -> None:
        """EventBus callback (any thread) — request a debounced GUI-thread rebuild of the graph."""
        self._targets_changed.emit()

    def _debounced_rebuild(self) -> None:
        """Rebuild only while visible; if hidden, mark dirty and defer to the next showEvent, so a scan
        burst doesn't repeatedly rebuild an off-screen QGraphicsScene."""
        if self.isVisible():
            self.rebuild()
        else:
            self._dirty = True

    def showEvent(self, ev) -> None:  # noqa: N802 (Qt override)
        super().showEvent(ev)
        if self._dirty:
            self._dirty = False
            self.rebuild()          # catch up on any target changes that arrived while hidden
        else:
            self._view.fit_content()   # re-frame in case the view was resized while hidden

    # ── interface mode (dual-depth Simple / Pro) ─────────────────────
    def set_ui_mode(self, mode: str) -> None:
        """Simple hides the whole experimental graph — a real send surface (double-click a node to
        fire a command at a device) that Simple deliberately keeps out of reach — and shows a short
        notice pointing at Pro. Pro restores the graph and its Rebuild / Auto-arrange controls."""
        pro = str(mode).lower() != "simple"
        self._controls.setVisible(pro)
        self._view.setVisible(pro)
        self._simple_notice.setVisible(not pro)

    # ── build ────────────────────────────────────────────────────────
    def _devices(self):
        dm = self._dm
        if dm is None:
            return []
        try:
            return list(dm.list_devices())
        except Exception:  # noqa: BLE001
            return []

    def _targets(self):
        pool = self._pool
        if pool is None or not hasattr(pool, "all"):
            return []
        try:
            return list(pool.all())
        except Exception:  # noqa: BLE001
            return []

    def rebuild(self) -> None:
        """Rebuild the graph from current devices + targets, preserving any layout the user dragged.

        Nodes that already existed keep their position, so re-running after a fresh scan adds the new
        APs without scrambling the web you arranged (key for wardriving, where you Rebuild as targets
        stream in). Only brand-new nodes get auto-placed; the Auto-arrange button forces a full re-layout.
        """
        prev = {k: (n.x(), n.y()) for k, n in self._nodes.items()}
        self._scene.clear()
        self._nodes = {}

        for dev in self._devices():
            port = getattr(dev, "port", "?")
            label = getattr(dev, "display_name", None) or getattr(dev, "name", None) or port
            fw = getattr(dev, "firmware", "") or ""
            # Mark a node that has no text command channel (Meshtastic protobuf stream / BlueJammer web-UI)
            # right in the subtitle, so the graph doesn't imply you can drive every connected box over serial.
            sub = str(port)
            if fw:
                sub = f"{sub} · {fw}"
            marker = _DRIVER_MARKERS.get(getattr(dev, "driver_type", "text-cli"))
            if marker:
                sub = f"{sub} · {marker}"
            node = _Node(str(label), sub, "device", self._device_actions(dev))
            self._scene.addItem(node)
            self._nodes["dev:" + str(port)] = node

        for t in self._targets():
            kind = self._target_kind(t)
            label = getattr(t, "ssid", "") or getattr(t, "mac", "") or "target"
            sub = getattr(t, "mac", "")
            node = _Node(str(label), str(sub), kind, self._target_actions(t))
            self._scene.addItem(node)
            key = "tgt:" + str(getattr(t, "mac", id(t)))
            self._nodes[key] = node
            src = self._nodes.get("dev:" + str(getattr(t, "device_source", "")))
            if src is not None:
                self._scene.addItem(_Edge(src, node))

        if not self._nodes:
            placeholder = _Node("No devices / targets yet", "connect a device, scan, then Rebuild", "node", [])
            self._scene.addItem(placeholder)
            self._nodes["_placeholder"] = placeholder
        # Restore the positions of nodes that survived the rebuild; only new ones get auto-placed.
        restored: "set[str]" = set()
        for k, (x, y) in prev.items():
            n = self._nodes.get(k)
            if n is not None:
                n.setPos(x, y)
                restored.add(k)
        self._auto_arrange(skip=restored)
        self._view.fit_content()   # frame the (re)built graph until the user zooms

    @staticmethod
    def _target_kind(t) -> str:
        tt = getattr(getattr(t, "target_type", None), "value", "") or str(getattr(t, "target_type", ""))
        tt = tt.lower()
        if "alpr" in tt:
            return "alpr"
        if "client" in tt:
            return "client"
        if "ble" in tt:
            return "ble"
        return "ap"

    # ── actions (the command list per node) ──────────────────────────
    def _device_actions(self, dev) -> "list[tuple[str, Callable[[], None]]]":
        port = getattr(dev, "port", "")
        fw = getattr(dev, "firmware", "") or ""
        out: "list[tuple[str, Callable[[], None]]]" = []
        try:
            from src.protocols import get_protocol
            proto = get_protocol(fw) if fw else None
            cmds = proto.get_commands() if proto else []
        except Exception:  # noqa: BLE001
            cmds = []
        for ci in cmds[:40]:  # cap the menu length
            name = getattr(ci, "name", str(ci))
            if "<" in name or ">" in name:
                # A placeholder template (e.g. "select -a <idx>", "channel -s <ch>") the Network tab
                # can't fill in — sending it raw would transmit the literal "<idx>" to the radio. These
                # stay reachable via the Devices tab, which collects the argument first.
                continue
            out.append((name, lambda c=name, p=port, info=ci: self._run_device_cmd(p, c, info)))
        if not out:
            # Be honest about WHY there's nothing to send: a stream/control-map device has no text command
            # channel at all, versus a text-CLI firmware that simply exposes no commands here.
            note = _NO_COMMAND_NOTES.get(getattr(dev, "driver_type", "text-cli"),
                                         "(no commands for this firmware)")
            out.append((note, lambda: None))
        return out

    def _target_actions(self, t) -> "list[tuple[str, Callable[[], None]]]":
        out: "list[tuple[str, Callable[[], None]]]" = []
        resolver = self._resolver
        if resolver is not None:
            try:
                by_port = resolver.resolve(t)
            except Exception:  # noqa: BLE001
                by_port = {}
            for port, actions in (by_port or {}).items():
                for a in actions:
                    label = f"{getattr(a, 'name', 'action')}  →  {port}"
                    out.append((label, lambda act=a, p=port: self._run_target_action(act, p)))
        if not out:
            out.append(("(no actions — connect the discovering device)", lambda: None))
        return out

    def _run_device_cmd(self, port: str, cmd: str, ci=None) -> None:
        if self._send_cmd is None or not port:
            return
        # This is a real send surface, so dangerous commands (deauth / jam / spam) must clear the same
        # safety gate as the Devices tab (_on_send) and Device View — otherwise the experimental Network
        # tab is a silent bypass that fires attack commands with no confirmation.
        from src.config.settings import load_settings
        from src.core import safety
        danger = safety.classify(cmd, ci)
        if safety.should_confirm(danger, load_settings()):
            from PyQt5.QtWidgets import QMessageBox
            reply = QMessageBox.warning(
                self, "Confirm dangerous command",
                safety.lab_only_warning_text(cmd, danger),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        try:
            self._send_cmd(port, cmd)
        except Exception:  # noqa: BLE001
            pass

    def _run_target_action(self, action, port: str) -> None:
        if self._dm is None:
            return
        # A target action (Deauth AP / Beacon Clone / Karma evil-twin) is a real attack send, so it must
        # clear the SAME safety gate as _run_device_cmd — otherwise the experimental Network tab is a silent
        # bypass that fires attack commands with no confirmation. Classify over the action's rendered
        # command AND its pre-commands (worst wins), and floor an ATTACK-category action at lab-only so a
        # keyword-free attack template is still gated.
        from src.config.settings import load_settings
        from src.core import safety
        from src.models.action import ActionCategory
        cmds = [getattr(action, "command_template", "") or ""]
        cmds += list(getattr(action, "pre_commands", None) or [])
        danger = safety.worst_of(*(safety.classify(c) for c in cmds))
        if getattr(action, "category", None) == ActionCategory.ATTACK:
            danger = safety.worst_of(danger, safety.LAB_ONLY)
        if safety.should_confirm(danger, load_settings()):
            from PyQt5.QtWidgets import QMessageBox
            reply = QMessageBox.warning(
                self, "Confirm dangerous command",
                safety.lab_only_warning_text(getattr(action, "command_template", ""), danger),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        try:
            from src.core.action_resolver import execute_action
            execute_action(action, port, self._dm)
        except Exception:  # noqa: BLE001
            pass

    # ── layout ───────────────────────────────────────────────────────
    def _auto_arrange(self, skip: "Optional[set]" = None) -> None:
        """Place device nodes in a column on the left and fan their targets out to the right; other nodes
        spread on a ring. The user then drags to taste. ``skip`` keeps already-positioned nodes put (used
        by Rebuild to preserve a dragged layout); the Auto-arrange button passes no skip to reset all."""
        skip = skip or set()
        devices = [k for k in self._nodes if k.startswith("dev:") and k not in skip]
        targets = [k for k in self._nodes if k.startswith("tgt:") and k not in skip]
        for i, k in enumerate(devices):
            self._nodes[k].setPos(40.0, 40.0 + i * 90.0)
        # fan each target near... simplest: a grid to the right
        cols = max(1, int(math.sqrt(max(1, len(targets)))) + 1)
        for i, k in enumerate(targets):
            r, c = divmod(i, cols)
            self._nodes[k].setPos(300.0 + c * 190.0, 30.0 + r * 80.0)
        for k, n in self._nodes.items():
            if not k.startswith(("dev:", "tgt:")) and k not in skip:
                n.setPos(300.0, 30.0)
        # refresh edges + fit the scene rect around everything
        for n in self._nodes.values():
            for e in n._edges:  # noqa: SLF001
                e.adjust()
        self._scene.setSceneRect(self._scene.itemsBoundingRect().adjusted(-60, -60, 60, 60))
