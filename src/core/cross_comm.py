"""Cross-device communication — shared target pool, event bus, and auto-routing."""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from src.models.target import Target, TargetType

log = logging.getLogger(__name__)


# ── Event Bus ────────────────────────────────────────────────────────

EventCallback = Callable[[str, dict[str, Any]], None]  # (topic, payload)


class EventBus:
    """Thread-safe publish/subscribe event bus.

    Subscribers register callbacks for string topics.  Publishers
    fire events to a topic, and every matching subscriber is called
    synchronously in the publisher's thread.

    Wildcard ``*`` matches all topics.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventCallback]] = {}
        self._lock = threading.Lock()

    def subscribe(self, topic: str, callback: EventCallback) -> None:
        """Register *callback* for *topic* (use ``*`` for all)."""
        with self._lock:
            self._subscribers.setdefault(topic, []).append(callback)

    def unsubscribe(self, topic: str, callback: EventCallback) -> None:
        """Remove a previously registered callback."""
        with self._lock:
            cbs = self._subscribers.get(topic, [])
            if callback in cbs:
                cbs.remove(callback)

    def publish(self, topic: str, payload: dict[str, Any] | None = None) -> None:
        """Fire an event on *topic*."""
        payload = payload or {}
        with self._lock:
            specific = list(self._subscribers.get(topic, []))
            wildcard = list(self._subscribers.get("*", []))
        for cb in specific + wildcard:
            try:
                cb(topic, payload)
            except Exception:
                log.exception("EventBus callback error (topic=%s)", topic)

    @property
    def topics(self) -> list[str]:
        """Return all topics with at least one subscriber."""
        with self._lock:
            return [t for t, cbs in self._subscribers.items() if cbs]


# ── Target Pool ──────────────────────────────────────────────────────

class TargetPool:
    """Thread-safe shared collection of discovered wireless targets.

    Targets are keyed by their :attr:`Target.key` (``type:mac``).
    Adding a duplicate key updates the existing entry's last_seen
    and signal fields instead of creating a new one.

    An :class:`EventBus` is used to broadcast ``target.added`` and
    ``target.updated`` events.
    """

    def __init__(self, bus: EventBus | None = None) -> None:
        self._targets: dict[str, Target] = {}
        self._lock = threading.Lock()
        self.bus = bus or EventBus()

    # ── Accessors ────────────────────────────────────────────────────

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._targets)

    def all(self) -> list[Target]:
        """Return a snapshot of all targets."""
        with self._lock:
            return list(self._targets.values())

    def by_type(self, tt: TargetType) -> list[Target]:
        with self._lock:
            return [t for t in self._targets.values() if t.target_type == tt]

    def get(self, key: str) -> Target | None:
        with self._lock:
            return self._targets.get(key)

    # ── Mutation ─────────────────────────────────────────────────────

    def add(self, target: Target) -> bool:
        """Add or update a target.

        Returns:
            True if this is a new target, False if updated.
        """
        updated_payload: dict | None = None
        with self._lock:
            existing = self._targets.get(target.key)
            if existing:
                # Don't let a re-observation that omits a field clobber a known value: channel/rssi 0 is
                # the unknown-sentinel everywhere, so pass None (a no-op in update_seen) when it's 0.
                existing.update_seen(
                    rssi=target.rssi if target.rssi else None,
                    channel=target.channel if target.channel else None,
                )
                # Latest-wins on a non-empty SSID (never clobber a known SSID with an empty one). Also
                # fixes synthetic idx:<port>:<index> keys keeping a stale SSID across re-ordered re-scans.
                if target.ssid:
                    existing.ssid = target.ssid
                # A per-device scan index (extra['index']) is only meaningful paired with the device
                # whose current scan produced it. On a re-observation that carries an index, refresh
                # BOTH the index and device_source together — otherwise a reordered rescan strands a
                # stale ordinal that an {index}-based action (BW16 AT+DEAUTHIDX) would then fire at a
                # DIFFERENT AP. If the new observation has no index, leave the pair untouched (don't
                # orphan a prior index under a later non-index source).
                incoming_extra = getattr(target, "extra", None) or {}
                if incoming_extra.get("index") is not None:
                    existing.extra["index"] = incoming_extra["index"]
                    existing.device_source = target.device_source
                updated_payload = existing.to_dict()
            else:
                self._targets[target.key] = target
        # Publish OUTSIDE the lock: the non-reentrant pool lock must not be held across subscriber
        # callbacks, or a blocking subscriber that reads the pool would deadlock the reader thread.
        if updated_payload is not None:
            self.bus.publish("target.updated", updated_payload)
            return False
        self.bus.publish("target.added", target.to_dict())
        return True

    def remove(self, key: str) -> Target | None:
        with self._lock:
            t = self._targets.pop(key, None)
        if t:
            self.bus.publish("target.removed", t.to_dict())
        return t

    def clear(self) -> int:
        """Remove all targets, return the count removed."""
        with self._lock:
            n = len(self._targets)
            self._targets.clear()
        self.bus.publish("target.cleared", {"count": n})
        return n

    def prune(self, max_age_seconds: float = 300.0) -> int:
        """Remove targets older than *max_age_seconds*."""
        now = datetime.now(timezone.utc)
        to_remove: list[str] = []
        with self._lock:
            for key, t in self._targets.items():
                age = (now - t.last_seen).total_seconds()
                if age > max_age_seconds:
                    to_remove.append(key)
        removed = 0
        for key in to_remove:
            if self.remove(key):
                removed += 1
        return removed


# ── Auto Router ──────────────────────────────────────────────────────

@dataclass
class RoutingRule:
    """A rule that routes targets matching criteria to a device command.

    Attributes:
        name: Rule identifier.
        target_type: Target type to match (None = any).
        ssid_pattern: Substring match on SSID (empty = any).
        min_rssi: Minimum RSSI to qualify.
        device_port: Device port to route the command to.
        command_template: Command string with ``{mac}`` / ``{ssid}`` / ``{channel}`` placeholders.
        enabled: Whether the rule is active.
        cooldown: Minimum seconds between firings for the same target.
    """

    name: str
    target_type: TargetType | None = None
    ssid_pattern: str = ""
    min_rssi: int = -100
    device_port: str = ""
    command_template: str = ""
    enabled: bool = True
    cooldown: float = 30.0


# Only these placeholders are ever substituted into a routing command template. Using an
# explicit regex sub (NOT str.format) is the fix for a format-string injection: str.format on
# attacker-influenced data allows '{mac.__class__.__init__.__globals__[...]}' object traversal.
_PLACEHOLDER_RE = re.compile(r"\{(mac|ssid|channel)\}")
# Control chars (incl. newline) must never reach the serial command — a crafted SSID like
# "foo\nreboot" could otherwise inject extra commands (defense-in-depth with SerialConnection.write).
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAC_RE = re.compile(r"^[0-9A-Fa-f:]{0,17}$")
_MAX_VALUE_LEN = 64


def _sanitize_value(value: Any) -> str:
    """Strip control characters and cap length on an over-the-air value before it is
    interpolated into a serial command."""
    s = str(value)
    s = _CTRL_RE.sub("", s)
    return s[:_MAX_VALUE_LEN]


def _safe_render(template: str, mac: str, ssid: str, channel: Any) -> str:
    """Render a routing command template by substituting ONLY the fixed {mac}/{ssid}/{channel}
    placeholders with sanitized values — no str.format, so no attribute/format-string traversal."""
    values = {
        "mac": _sanitize_value(mac),
        "ssid": _sanitize_value(ssid),
        "channel": str(int(channel)) if str(channel).lstrip("-").isdigit() else "",
    }
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), ""), template)


class AutoRouter:
    """Rules engine that routes targets to device commands.

    When a target arrives (via :class:`EventBus`), the router evaluates
    all enabled rules and invokes a ``send_command`` callback for each
    match.
    """

    def __init__(
        self,
        bus: EventBus,
        send_command: Callable[[str, str], None],
    ) -> None:
        """
        Args:
            bus: EventBus to subscribe to ``target.added`` events.
            send_command: Callable(port, command) to execute matched rules.
        """
        self._bus = bus
        self._send = send_command
        self._rules: list[RoutingRule] = []
        self._cooldowns: dict[str, float] = {}  # "rule:target_key" -> last_fire
        self._lock = threading.Lock()

        self._bus.subscribe("target.added", self._on_target)

    # ── Rules ────────────────────────────────────────────────────────

    def add_rule(self, rule: RoutingRule) -> None:
        with self._lock:
            self._rules.append(rule)
        log.info("AutoRouter: added rule %r", rule.name)

    def remove_rule(self, name: str) -> bool:
        with self._lock:
            before = len(self._rules)
            self._rules = [r for r in self._rules if r.name != name]
            return len(self._rules) < before

    def list_rules(self) -> list[RoutingRule]:
        with self._lock:
            return list(self._rules)

    # ── Internal ─────────────────────────────────────────────────────

    def _on_target(self, _topic: str, payload: dict[str, Any]) -> None:
        target_type = TargetType(payload.get("target_type", "ap"))
        mac = payload.get("mac", "")
        ssid = payload.get("ssid", "")
        rssi = payload.get("rssi", 0)
        channel = payload.get("channel", 0)
        target_key = f"{target_type.value}:{mac}"

        with self._lock:
            rules = [r for r in self._rules if r.enabled]

        now = time.monotonic()
        for rule in rules:
            if not self._matches(rule, target_type, ssid, rssi):
                continue
            cooldown_key = f"{rule.name}:{target_key}"
            # Atomic check-and-stamp: _on_target runs in EACH device's serial reader thread
            # (target.added is published synchronously from TargetPool.add), so with several radios
            # connected two threads can see the same target at once. Doing the get/compare/set outside
            # the lock let both observe an expired cooldown and BOTH fire the routed command.
            with self._lock:
                last = self._cooldowns.get(cooldown_key, 0.0)
                if now - last < rule.cooldown:
                    continue  # exits the with-block -> lock released
                self._cooldowns[cooldown_key] = now
                # Bound growth (otherwise one entry per rule x target lives forever — a slow leak over a
                # long wardrive): drop entries older than the largest current cooldown. Such an entry is
                # already expired for its own rule, so evicting it can never enable a blocked fire.
                if len(self._cooldowns) > 4096:
                    cutoff = now - max((r.cooldown for r in rules), default=60.0)
                    self._cooldowns = {k: t for k, t in self._cooldowns.items() if t >= cutoff}

            # Validate the MAC shape before it is interpolated; reject anything odd outright.
            if mac and not _MAC_RE.match(str(mac)):
                log.warning("AutoRouter: rejecting target with malformed MAC %r", mac)
                continue
            cmd = _safe_render(rule.command_template, mac, ssid, channel)
            log.info("AutoRouter: rule %r matched %s -> %s", rule.name, target_key, cmd)
            try:
                self._send(rule.device_port, cmd)
            except Exception:
                log.exception("AutoRouter send_command failed")

    @staticmethod
    def _matches(
        rule: RoutingRule,
        target_type: TargetType,
        ssid: str,
        rssi: int,
    ) -> bool:
        if rule.target_type is not None and rule.target_type != target_type:
            return False
        if rule.ssid_pattern and rule.ssid_pattern.lower() not in ssid.lower():
            return False
        if rssi < rule.min_rssi:
            return False
        return True
