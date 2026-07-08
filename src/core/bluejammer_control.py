"""BlueJammer-V2 remote controller — UART-first (no AP/IP), with the Wi-Fi web UI as an option.

The device exposes two control surfaces (see the internal reverse-engineering notes):

* **UART (primary, no AP/IP):** the inter-board link the BW16 uses to command the ESP32 (115200, with a
  handshake/ack envelope). A USB-TTL adapter on that wire lets Cyber Controller send the same frames — no
  Wi-Fi, no AP, no IP. This is the deep, CC-native path.
* **Web UI (optional):** HTTP to ``http://192.168.1.1`` after joining the device's own AP.

The exact UART frames and HTTP endpoints are **closed-source** and must be **captured + validated on real
hardware** before use. Until a validated :class:`ControlMap` is supplied, the controller **refuses to send**
(``ControlUnavailable``) rather than guess — a STOP that silently does nothing is the precise safety failure
we must avoid.

Safety model:
* ``stop()`` (set **Idle**) is the primary, **ungated** action.
* ``set_mode()`` to any *jamming* mode requires an explicit ``confirm_unsafe=True`` token; the GUI gates that
  behind an "authorized RF-shielded enclosure" attestation. Operating a jammer is illegal outside such an
  enclosure (47 U.S.C. §333 + worldwide equivalents).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Protocol, Tuple

log = logging.getLogger(__name__)


class ControlUnavailable(RuntimeError):
    """Raised when no hardware-validated control map exists for the requested action (fail-safe)."""


class Mode(str, Enum):
    """The device's documented modes (from the web UI). IDLE == STOP (no emission)."""
    IDLE = "Idle"
    BLUETOOTH = "Bluetooth"
    BLE = "BLE"
    WIFI = "WiFi"
    RC_DRONE = "RC/Drone"

    @property
    def is_stop(self) -> bool:
        return self is Mode.IDLE

    @property
    def is_jamming(self) -> bool:
        return self is not Mode.IDLE


class Transport(Protocol):
    """A way to deliver a control payload to the device."""

    name: str

    def send(self, payload: object) -> None: ...


@dataclass
class ControlMap:
    """The per-mode control payloads. **Empty + unvalidated by default** — populate ONLY from a real
    hardware capture (UART sniff or live HTTP capture), then set ``validated=True``.

    * ``uart_frames``: Mode -> exact bytes to write on the inter-board UART.
    * ``http_calls``:  Mode -> (method, path, body) for the web UI.
    """

    uart_frames: "dict[Mode, bytes]" = field(default_factory=dict)
    http_calls: "dict[Mode, Tuple[str, str, Optional[str]]]" = field(default_factory=dict)
    validated: bool = False

    def has_uart(self, mode: Mode) -> bool:
        return self.validated and mode in self.uart_frames

    def has_http(self, mode: Mode) -> bool:
        return self.validated and mode in self.http_calls


class UartTransport:
    """Drives the inter-board UART (the BW16->ESP32 wire) — no AP/IP needed.

    ``write_fn`` writes raw bytes to that serial line (e.g. a pyserial port on a USB-TTL adapter wired to the
    ESP32's command UART). ``ack_fn`` (optional) blocks until the device acks, returning True/False — the
    link is documented as handshake/ack, so a real deployment should confirm the frame landed.
    """

    def __init__(self, write_fn: Callable[[bytes], None], *,
                 ack_fn: "Optional[Callable[[], bool]]" = None, name: str = "uart") -> None:
        self._write = write_fn
        self._ack = ack_fn
        self.name = name

    def send(self, payload: object) -> None:
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError("UartTransport payload must be bytes")
        self._write(bytes(payload))
        if self._ack is not None and not self._ack():
            raise ControlUnavailable("UART command was not acknowledged by the device")


class HttpTransport:
    """Optional: drives the device's web UI at http://192.168.1.1 (requires joining its AP).

    ``request_fn(method, url, body)`` performs the HTTP call (kept injectable so the core has no hard
    dependency on a specific HTTP client and stays unit-testable).
    """

    def __init__(self, request_fn: "Callable[[str, str, Optional[str]], int]", *,
                 base_url: str = "http://192.168.1.1", name: str = "web-ui (AP)") -> None:
        self._request = request_fn
        self._base = base_url.rstrip("/")
        self.name = name

    def send(self, payload: object) -> None:
        if not (isinstance(payload, tuple) and len(payload) == 3):
            raise TypeError("HttpTransport payload must be (method, path, body)")
        method, path, body = payload
        status = self._request(method, self._base + path, body)
        if not (200 <= int(status) < 300):
            raise ControlUnavailable(f"web UI returned HTTP {status}")


class BlueJammerController:
    """Mode controller. STOP-first; arming gated; refuses to send without a validated control map."""

    def __init__(self, transport: Transport, control_map: ControlMap, *,
                 on_event: "Optional[Callable[[str, Mode, str], None]]" = None) -> None:
        self._t = transport
        self._map = control_map
        self._on = on_event or (lambda *_: None)

    @property
    def available(self) -> bool:
        """True if STOP (Idle) can actually be sent over the current transport (validated frame exists)."""
        return self._payload_for(Mode.IDLE) is not None

    def stop(self) -> None:
        """Set **Idle** — the always-available STOP. Raises ``ControlUnavailable`` if no validated Idle frame."""
        self._dispatch(Mode.IDLE, confirm_unsafe=True)  # STOP is never gated

    def set_mode(self, mode: Mode, *, confirm_unsafe: bool = False) -> None:
        """Set a mode. Any jamming mode requires ``confirm_unsafe=True`` (GUI: RF-shielded attestation)."""
        if mode.is_jamming and not confirm_unsafe:
            raise PermissionError(
                "arming a jamming mode requires confirm_unsafe=True "
                "(authorized, RF-shielded, lawful use only)"
            )
        self._dispatch(mode, confirm_unsafe=confirm_unsafe)

    # ── internals ────────────────────────────────────────────────────
    def _payload_for(self, mode: Mode) -> object:
        if isinstance(self._t, UartTransport) and self._map.has_uart(mode):
            return self._map.uart_frames[mode]
        if isinstance(self._t, HttpTransport) and self._map.has_http(mode):
            return self._map.http_calls[mode]
        return None

    def _dispatch(self, mode: Mode, *, confirm_unsafe: bool) -> None:
        if not self._map.validated:
            raise ControlUnavailable(
                "BlueJammer control map is not captured/validated on hardware yet — "
                "use the device web UI / physical button / power. (See the reverse-engineering plan.)"
            )
        payload = self._payload_for(mode)
        if payload is None:
            raise ControlUnavailable(f"no validated control payload for {mode.value} over {self._t.name}")
        self._t.send(payload)
        log.info("BlueJammer %s via %s", mode.value, self._t.name)
        self._on("sent", mode, self._t.name)
