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
        """Deserialize from a dict."""
        steps = [MacroStep(**s) for s in data.get("steps", [])]
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
            async_: If True (default), run playback in a background thread.
        """
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
                      progress_callback, complete_callback),
                name="macro-playback",
                daemon=True,
            )
            t.start()
        else:
            self._playback_loop(
                macro, send_command, speed_multiplier, variables or {},
                progress_callback, complete_callback,
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
    ) -> None:
        """Internal playback loop."""
        total = len(macro.steps)
        log.info("Macro playback: %s (%d steps, speed=%.1fx)", macro.name, total, speed)

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

            log.info("Macro playback complete: %s", macro.name)
            if progress:
                progress(total, total, "Playback complete")
            if complete:
                complete(True, "Playback complete")

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
            path = self.macros_dir / f"{safe_name}.json"
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
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    macros.append({
                        "name": data.get("name", f.stem),
                        "path": str(f),
                        "step_count": len(data.get("steps", [])),
                        "protocol": data.get("device_protocol", ""),
                        "created_at": data.get("created_at", ""),
                    })
                except (json.JSONDecodeError, OSError):
                    continue
        return macros

    def delete_macro(self, path: str | Path) -> bool:
        """Delete a saved macro file.

        Returns:
            True if the file was deleted, False if it didn't exist.
        """
        path = Path(path)
        if path.exists():
            path.unlink()
            log.info("Macro deleted: %s", path)
            return True
        return False
