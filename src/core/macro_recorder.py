"""Macro recorder — record and replay sequences of serial commands."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

_DEFAULT_MACROS_DIR = Path.home() / ".cyber-controller" / "macros"

# Variable placeholders supported in macros
_VARIABLE_PATTERN = re.compile(r"\{\{(\w+)\}\}")

# Bundled starter macros: shipped as ``cc_*.json`` under ``src/core/default_macros`` and seeded into
# the user's macros dir on first run (see ``seed_default_macros``). Resolved via ``resource_path``
# so it works in a dev checkout and a PyInstaller build alike (bundled with a matching dest). The
# ledger records which builtins were seeded so one the user deletes is NOT re-created.
_SEEDED_LEDGER = ".seeded.json"

# Command PREFIXES that mark a macro step as transmitting/disruptive and therefore needing the play-time
# arm gate (§4.3). Grounded in the ATTACK-category TargetAction templates across src/protocols/*.py
# (attack -d/-t …, beaconspam -r, blespam …, karma -s …, probe, AT+DEAUTHIDX…, subghz tx, nfc/rfid emulate,
# startportal) PLUS the HaleHound offensive verbs whose attack keyword is a NON-leading token or uses a
# different separator (wifi_deauth, ble_cinder, subghz_replay, mousejack, protokill, tag_disrupt — see
# src/protocols/halehound.py). Matched as case-insensitive PREFIXES on the whole step command — NOT exact
# first-token equality, which let 'beaconspam -r', 'karma -s <ssid>', 'AT+DEAUTHIDX=ALL' and 'probe' bypass
# the gate because their first token wasn't literally in the old verb list. Because the underscore-joined
# HaleHound verbs ('wifi_deauth' etc.) don't start with the bare keyword ('deauth'), each is listed here in
# FULL so the prefix match fires. These are the same danger verbs ``src.core.safety`` flags as 'lab-only',
# so the arm gate cannot silently disagree with the terminal's danger classifier. This is a WARN/arm gate
# (always offers "Yes, proceed"), so over-flagging a benign command is acceptable — MISSING a real attack is
# not.
_ATTACK_PREFIXES = (
    "attack", "deauth", "at+deauth", "beacon", "blespam", "spam", "rickroll", "karma", "probe",
    "startportal", "evilportal", "sourapple", "jam", "subghz tx", "nfc emulate", "rfid emulate",
    # HaleHound & underscore-joined offensive verbs the old first-token gate missed:
    "wifi_deauth", "ble_cinder", "cinder", "subghz_replay", "replay", "mousejack", "protokill",
    "tag_disrupt",
)


def _builtin_macros_dir() -> Path:
    """Absolute path to the bundled ``cc_*.json`` starter macros (frozen-safe)."""
    from src.core.resources import resource_path
    return resource_path("src", "core", "default_macros")


def is_offensive_macro(macro: Macro) -> bool:
    """Return True if a macro transmits / can disrupt and therefore needs the play-time arm gate.

    Heuristic (spec §4.3): the ``device_protocol`` ends with ``-attack``, OR the name starts with
    ``[TEMPLATE``, OR any step command starts with a known attack-command prefix (``_ATTACK_PREFIXES``,
    which now includes the underscore-joined HaleHound verbs — ``wifi_deauth``, ``ble_cinder``,
    ``subghz_replay``, ``mousejack`` — whose embedded attack keyword the old bare-keyword list missed).
    Pure logic (no Qt) so it is unit testable and reusable by the UI play path.
    """
    if macro.device_protocol.endswith("-attack"):
        return True
    if macro.name.startswith("[TEMPLATE"):
        return True
    for step in macro.steps:
        cmd = step.command.strip().lower()
        if any(cmd.startswith(prefix) for prefix in _ATTACK_PREFIXES):
            return True
    return False


#: Clamp ceiling for a step delay (1 hour). A hand-edited macro could carry an absurd delay_ms that would
#: otherwise wedge playback in an effectively unbounded interruptible-sleep.
_MAX_DELAY_MS = 3_600_000

#: Per-step timeout (seconds) handed to a wired ``read_response`` when verifying a step's
#: ``expected_response`` regex.
_RESPONSE_TIMEOUT_S = 2.0


def _coerce_delay_ms(value: Any) -> int:
    """Normalize a step's delay to a sane non-negative int in [0, _MAX_DELAY_MS].

    Macro JSON is hand-editable / untrusted: a delay_ms of ``"100"`` (string), ``None``, a negative, or an
    absurd value would otherwise crash the playback loop's ``delay_ms > 0`` compare (TypeError) or hang it.
    """
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(ms, _MAX_DELAY_MS))


@dataclass
class MacroStep:
    """A single step in a macro sequence.

    Attributes:
        command: The serial command to send.
        delay_ms: Milliseconds to wait before sending (relative to previous step).
        expected_response: Optional regex pattern to match in the device response.
    """

    command: str
    delay_ms: int = 0
    expected_response: str = ""


@dataclass
class Macro:
    """A recorded sequence of serial commands.

    Attributes:
        name: Human-readable macro name.
        description: What this macro does.
        steps: Ordered list of MacroStep objects.
        created_at: ISO-8601 creation timestamp (UTC).
        device_protocol: Protocol the macro was recorded for (e.g. 'marauder').
    """

    name: str
    description: str = ""
    steps: list[MacroStep] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    device_protocol: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON storage."""
        return {
            "name": self.name,
            "description": self.description,
            "steps": [asdict(s) for s in self.steps],
            "created_at": self.created_at,
            "device_protocol": self.device_protocol,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Macro:
        """Deserialize from a dict. Macro JSON is hand-editable, so each step's delay_ms is coerced to a
        sane non-negative int here — a string/None/huge/negative delay would otherwise crash or hang playback."""
        steps = []
        for s in data.get("steps", []):
            step = MacroStep(**s)
            step.delay_ms = _coerce_delay_ms(step.delay_ms)
            steps.append(step)
        return cls(
            name=data.get("name", "Untitled"),
            description=data.get("description", ""),
            steps=steps,
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            device_protocol=data.get("device_protocol", ""),
        )

    @property
    def total_duration_ms(self) -> int:
        """Total estimated playback time in milliseconds."""
        return sum(s.delay_ms for s in self.steps)

    @property
    def step_count(self) -> int:
        return len(self.steps)


# Playback callback types
PlaybackProgress = Callable[[int, int, str], None]  # (step_index, total_steps, message)
PlaybackComplete = Callable[[bool, str], None]  # (success, message)


class MacroRecorder:
    """Record and replay sequences of serial commands.

    Recording captures all commands sent through the recorder with
    inter-command timing. Playback replays commands with configurable
    speed and variable substitution.

    Macros are stored as JSON in ``~/.cyber-controller/macros/``.
    """

    def __init__(self, macros_dir: Path | None = None) -> None:
        self.macros_dir = macros_dir or _DEFAULT_MACROS_DIR
        self.macros_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._recording = False
        self._playing = False
        self._stop_playback = threading.Event()

        # Recording state
        self._record_steps: list[MacroStep] = []
        self._record_port: str = ""
        self._record_protocol: str = ""
        self._last_timestamp: float = 0.0

    # ── First-run seeding of bundled starter macros ──────────────────

    def seed_default_macros(self, source_dir: Path | None = None) -> list[Path]:
        """Seed bundled starter macros into the macros dir on first run (never clobbering).

        For each bundled ``cc_*.json`` builtin: write it into ``macros_dir`` ONLY if a file of that
        name does not already exist AND it is not recorded in the ``.seeded.json`` ledger. So a user
        macro is never overwritten, and a builtin the user deletes stays deleted (its filename stays
        in the ledger, so it is not resurrected on the next launch). Builtins are public, non-
        sensitive templates and are always written as plaintext to ``macros_dir`` — never into the
        secure container. Nothing transmits: seeding only writes files.

        Returns the list of files newly written this call (empty if all builtins are already present
        or previously seeded-then-deleted).
        """
        src_dir = source_dir or _builtin_macros_dir()
        if not src_dir.is_dir():
            log.debug("No bundled default_macros dir at %s — nothing to seed", src_dir)
            return []

        self.macros_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = self.macros_dir / _SEEDED_LEDGER
        seeded = self._load_seeded_ledger(ledger_path)

        written: list[Path] = []
        for src_file in sorted(src_dir.glob("cc_*.json")):
            name = src_file.name
            target = self.macros_dir / name
            if target.exists() or name in seeded:
                continue  # user has one there OR user deleted a builtin we already seeded
            try:
                target.write_text(src_file.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                log.warning("Failed to seed builtin macro %s", name, exc_info=True)
                continue
            seeded.add(name)
            written.append(target)
            log.info("Seeded builtin macro: %s", name)

        if written:
            self._save_seeded_ledger(ledger_path, seeded)
        return written

    @staticmethod
    def _load_seeded_ledger(ledger_path: Path) -> set[str]:
        """Return the builtin filenames already seeded once (empty set if absent/unreadable)."""
        try:
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
            return set()  # invalid UTF-8 / bad JSON -> treat as unseeded (safe re-seed), never crash start
        names = data.get("seeded", []) if isinstance(data, dict) else data
        return {str(n) for n in names} if isinstance(names, list) else set()

    @staticmethod
    def _save_seeded_ledger(ledger_path: Path, seeded: set[str]) -> None:
        """Persist the seeded-builtins ledger (best effort; a write error must not block start)."""
        try:
            ledger_path.write_text(
                json.dumps({"seeded": sorted(seeded)}, indent=2), encoding="utf-8"
            )
        except OSError:
            log.warning("Failed to write seeded-macros ledger %s", ledger_path, exc_info=True)

    # ── Recording ────────────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def is_playing(self) -> bool:
        return self._playing

    def start_recording(self, device_port: str, protocol: str = "") -> None:
        """Begin capturing commands.

        Args:
            device_port: The serial port being recorded.
            protocol: Protocol identifier for the macro metadata.

        Raises:
            RuntimeError: If already recording.
        """
        with self._lock:
            if self._recording:
                raise RuntimeError("Already recording a macro")
            self._recording = True
            self._record_steps = []
            self._record_port = device_port
            self._record_protocol = protocol
            self._last_timestamp = time.monotonic()
        log.info("Macro recording started on %s", device_port)

    def record_command(self, command: str) -> None:
        """Record a single command during an active recording session.

        Automatically computes the delay since the previous command.

        Args:
            command: The command string that was sent.
        """
        with self._lock:
            if not self._recording:
                return
            now = time.monotonic()
            delay_ms = int((now - self._last_timestamp) * 1000)
            self._last_timestamp = now
            step = MacroStep(command=command, delay_ms=delay_ms)
            self._record_steps.append(step)
        log.debug("Macro recorded: %s (delay=%dms)", command, delay_ms)

    def stop_recording(self, name: str = "Untitled", description: str = "") -> Macro:
        """Stop recording and return the captured macro.

        Args:
            name: Name for the macro.
            description: Description of what the macro does.

        Returns:
            The recorded Macro object.

        Raises:
            RuntimeError: If not currently recording.
        """
        with self._lock:
            if not self._recording:
                raise RuntimeError("Not currently recording")
            self._recording = False
            macro = Macro(
                name=name,
                description=description,
                steps=list(self._record_steps),
                device_protocol=self._record_protocol,
            )
            self._record_steps = []
        log.info(
            "Macro recording stopped: %s (%d steps)",
            name, len(macro.steps),
        )
        return macro

    # ── Playback ─────────────────────────────────────────────────────

    def play(
        self,
        macro: Macro,
        send_command: Callable[[str], None],
        speed_multiplier: float = 1.0,
        variables: dict[str, str] | None = None,
        progress_callback: PlaybackProgress | None = None,
        complete_callback: PlaybackComplete | None = None,
        *,
        armed: bool = False,
        read_response: Callable[[float], str] | None = None,
        async_: bool = True,
    ) -> None:
        """Replay a macro's commands.

        Args:
            macro: The Macro to replay.
            send_command: Callable that sends a command string to the device.
            speed_multiplier: Time scaling factor (2.0 = double speed).
            variables: Dict of variable substitutions (e.g. TARGET_MAC -> value).
            progress_callback: Optional (step_index, total, message) callback.
            complete_callback: Optional (success, message) callback.
            armed: Must be True to replay a transmitting/offensive macro (see
                   :func:`is_offensive_macro`). The caller sets this only after its own arm
                   confirmation. Recon macros play regardless.
            read_response: Optional ``(timeout) -> str`` reader. When wired, a step's
                   ``expected_response`` regex is checked against the device reply and a mismatch
                   FAILS the playback; without it, such checks are reported as not verified (never
                   silently claimed as matched).
            async_: If True (default), run playback in a background thread.
        """
        # Play-time arm gate, ENFORCED IN THE ENGINE (not just one UI): a transmitting/offensive
        # macro must be explicitly armed by the caller — else refuse. Previously only the Qt tab
        # gated this, so `--ui tk` (or any other caller) replayed attack templates with NO
        # confirmation. This is a confirm gate, never a hard block: the caller's arm IS the
        # always-available "Yes, proceed".
        if is_offensive_macro(macro) and not armed:
            log.warning("Refusing to play offensive macro %r: not armed", macro.name)
            if complete_callback:
                complete_callback(
                    False,
                    "Macro not armed — a transmitting/offensive macro needs arm "
                    "confirmation before playback.",
                )
            return

        with self._lock:
            if self._playing:
                if complete_callback:
                    complete_callback(False, "Playback already in progress")
                return
            self._playing = True
            self._stop_playback.clear()

        if async_:
            t = threading.Thread(
                target=self._playback_loop,
                args=(macro, send_command, speed_multiplier, variables or {},
                      progress_callback, complete_callback, read_response),
                name="macro-playback",
                daemon=True,
            )
            t.start()
        else:
            self._playback_loop(
                macro, send_command, speed_multiplier, variables or {},
                progress_callback, complete_callback, read_response,
            )

    def stop_playback(self) -> None:
        """Request playback to stop after the current step."""
        self._stop_playback.set()

    def _playback_loop(
        self,
        macro: Macro,
        send_command: Callable[[str], None],
        speed: float,
        variables: dict[str, str],
        progress: PlaybackProgress | None,
        complete: PlaybackComplete | None,
        read_response: Callable[[float], str] | None = None,
    ) -> None:
        """Internal playback loop."""
        total = len(macro.steps)
        log.info("Macro playback: %s (%d steps, speed=%.1fx)", macro.name, total, speed)
        unverified = 0  # steps that declared expected_response but had no response channel to check

        try:
            for i, step in enumerate(macro.steps):
                if self._stop_playback.is_set():
                    log.info("Macro playback stopped at step %d/%d", i + 1, total)
                    if complete:
                        complete(False, f"Stopped at step {i + 1}/{total}")
                    return

                # Apply delay (skip for the first step)
                if i > 0 and step.delay_ms > 0:
                    delay = step.delay_ms / 1000.0
                    if speed > 0:
                        delay /= speed
                    # Use stop event for interruptible sleep
                    if self._stop_playback.wait(timeout=delay):
                        if complete:
                            complete(False, f"Stopped during delay at step {i + 1}/{total}")
                        return

                # Substitute variables
                cmd = self._substitute_variables(step.command, variables)

                # Send command
                if progress:
                    progress(i, total, f"Sending: {cmd}")
                try:
                    send_command(cmd)
                except Exception as exc:
                    log.error("Macro playback send error at step %d: %s", i + 1, exc)
                    if complete:
                        complete(False, f"Send error at step {i + 1}: {exc}")
                    return

                # Response verification: a step may declare `expected_response` (a regex the
                # device's reply must match). If a read_response channel is wired, actually check
                # it and FAIL the playback on a mismatch — never map an unverified/failed step to
                # success. Without a channel the check cannot be performed, so count it and report
                # it honestly at the end rather than silently claiming a match (verify-never-fake).
                pattern = (step.expected_response or "").strip()
                if pattern:
                    if read_response is not None:
                        try:
                            resp = read_response(_RESPONSE_TIMEOUT_S / (speed or 1.0)) or ""
                        except Exception as exc:  # a reader failure is a real, honest failure
                            log.error("Macro playback read error at step %d: %s", i + 1, exc)
                            if complete:
                                complete(False, f"Response read error at step {i + 1}: {exc}")
                            return
                        if not re.search(pattern, resp):
                            log.info("Macro step %d response did not match %r", i + 1, pattern)
                            if complete:
                                complete(
                                    False,
                                    f"step {i + 1}/{total}: response did not match {pattern!r}",
                                )
                            return
                    else:
                        unverified += 1

            log.info("Macro playback complete: %s", macro.name)
            if progress:
                progress(total, total, "Playback complete")
            if complete:
                if unverified:
                    complete(True, f"Playback complete — {unverified} step(s) NOT "
                                   "response-verified (no response channel wired)")
                else:
                    complete(True, "Playback complete")

        except Exception as exc:
            # Any unexpected playback error (e.g. a malformed step surviving from a hand-edited macro) MUST
            # still notify the caller — otherwise this daemon thread dies silently and the UI's Play button
            # stays disabled forever ("Playing…" wedged). Route it through the completion callback.
            log.exception("Macro playback failed: %s", macro.name)
            if complete:
                complete(False, f"Playback error: {exc}")
        finally:
            with self._lock:
                self._playing = False

    @staticmethod
    def _substitute_variables(command: str, variables: dict[str, str]) -> str:
        """Replace ``{{VARIABLE}}`` placeholders in a command string."""
        def replacer(match: re.Match) -> str:
            key = match.group(1)
            return variables.get(key, match.group(0))
        return _VARIABLE_PATTERN.sub(replacer, command)

    # ── Persistence ──────────────────────────────────────────────────

    def _resolve_default_macro_path(self, safe_name: str, macro_name: str) -> Path:
        """Default-save path for *safe_name*, rolling over so a DIFFERENT macro that sanitizes to
        the same filename isn't silently clobbered. Re-saving the SAME macro (its stored name
        matches) overwrites in place; a distinct macro gets the next free ``name-N`` sibling."""
        n = 0
        while True:
            cand = (self.macros_dir / f"{safe_name}.json" if n == 0
                    else self.macros_dir / f"{safe_name}-{n}.json")
            if not cand.exists():
                return cand
            try:
                existing = json.loads(cand.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and existing.get("name") == macro_name:
                    return cand  # same macro -> overwrite/update in place
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                pass  # unreadable/foreign file -> don't clobber it; try the next sibling
            n += 1

    def save_macro(self, macro: Macro, path: str | Path | None = None) -> Path:
        """Save a macro to a JSON file.

        Args:
            macro: The Macro to save.
            path: Explicit file path. If None, saves to the macros directory
                  using the macro name as filename.

        Returns:
            Path to the saved file.
        """
        if path is None:
            safe_name = re.sub(r"[^\w\-]", "_", macro.name.lower().strip())
            if not safe_name.strip("_-"):
                # An empty / all-separator name would become ".json" — a hidden dotfile that
                # list_saved_macros skips, so the saved session could never be reselected. Fall back
                # to a usable stem (the collision resolver below still keeps it from clobbering).
                safe_name = "macro"
            # Internal save. When the secure container is ENABLED the recorded session MUST be
            # encrypted at rest — we never silently fall back to a plaintext file (that would leak a
            # session the user chose to protect, SEC-B1). If the gate is locked or the encrypted save
            # errors, surface it. When the container is OFF, a plaintext JSON save is the intended
            # behaviour. Explicit-path saves (exports the user chose to write elsewhere) stay plaintext.
            from src.security import secure_store
            if secure_store.enabled():
                if not secure_store.available():
                    raise RuntimeError(
                        "Secure container is enabled but locked — unlock the access gate to save "
                        "this macro (refusing to write it as plaintext)."
                    )
                p = secure_store.save("macros", safe_name, macro.to_dict())
                log.info("Macro saved to secure container: %s -> %s", macro.name, p)
                return p
            path = self._resolve_default_macro_path(safe_name, macro.name)
        else:
            path = Path(path)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(macro.to_dict(), indent=2),
            encoding="utf-8",
        )
        log.info("Macro saved: %s -> %s", macro.name, path)
        return path

    def load_macro(self, path: str | Path) -> Macro:
        """Load a macro from a JSON file.

        Args:
            path: Path to the macro JSON file.

        Returns:
            The loaded Macro object.

        Raises:
            FileNotFoundError: If the file does not exist.
            json.JSONDecodeError: If the file is not valid JSON.
        """
        # Secure-container entries (a .enc file under the container dir) decrypt via the gate key;
        # a tamper/auth failure propagates (fail-closed) rather than being silently retried as JSON.
        from src.security import secure_store
        if secure_store.is_container_path(path):
            data = secure_store.load_file(path)
            if data is None:
                raise FileNotFoundError(f"Secure container is locked or entry missing: {path}")
            macro = Macro.from_dict(data)
            log.info("Macro loaded from secure container: %s (%d steps)", macro.name, len(macro.steps))
            return macro

        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        macro = Macro.from_dict(data)
        log.info("Macro loaded: %s (%d steps)", macro.name, len(macro.steps))
        return macro

    def list_saved_macros(self) -> list[dict[str, Any]]:
        """List all macros saved in the macros directory.

        Returns:
            List of dicts with keys: name, path, step_count, protocol, created_at.
        """
        macros = []
        if self.macros_dir.is_dir():
            for f in sorted(self.macros_dir.glob("*.json")):
                if f.name.startswith("."):
                    continue  # skip metadata dotfiles (e.g. the .seeded.json seed ledger)
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        continue  # valid JSON but not a macro object (e.g. a bare array) — skip
                    steps = data.get("steps")
                    macros.append({
                        "name": data.get("name", f.stem),
                        "path": str(f),
                        "step_count": len(steps) if isinstance(steps, list) else 0,
                        "protocol": data.get("device_protocol", ""),
                        "created_at": data.get("created_at", ""),
                        "secured": False,
                    })
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    # UnicodeDecodeError (invalid UTF-8 in one file) must be caught too, or a single bad
                    # file in the macros dir crashes the entire listing and hides every other macro.
                    continue
        # Macros saved while the secure container was active (only listable while unlocked).
        try:
            from src.security import secure_store
            for name in secure_store.list_names("macros"):
                try:
                    data = secure_store.load("macros", name)
                except Exception:
                    continue  # tampered/unreadable entry — skip from the listing
                if not data or not isinstance(data, dict):
                    continue  # empty/unreadable or not a macro object — skip
                steps = data.get("steps")
                macros.append({
                    "name": data.get("name", name),
                    "path": str(secure_store.entry_path("macros", name)),
                    "step_count": len(steps) if isinstance(steps, list) else 0,
                    "protocol": data.get("device_protocol", ""),
                    "created_at": data.get("created_at", ""),
                    "secured": True,
                })
        except Exception:
            log.debug("Secure-container macro listing skipped", exc_info=True)
        return macros

    def delete_macro(self, path: str | Path) -> bool:
        """Delete a saved macro file.

        Returns:
            True if the file was deleted, False if it didn't exist.
        """
        path = Path(path)
        if not path.exists():
            return False
        # Container entries get a best-effort secure delete (overwrite then unlink) so a deleted
        # recorded session leaves no recoverable plaintext-adjacent trace.
        try:
            from src.security import secure_store
            if secure_store.is_container_path(path):
                from src.security.physical_key import _secure_delete
                _secure_delete(path)
                log.info("Secured macro deleted: %s", path)
                return True
        except Exception:
            log.exception("Secure macro delete failed; falling back to unlink")
        path.unlink()
        log.info("Macro deleted: %s", path)
        return True
