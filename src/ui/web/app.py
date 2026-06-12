"""Flask web remote — phone-friendly interface for headless Cyber Controller.

Security posture (hardened — see SECURITY findings remediation):
    * Binds 127.0.0.1 by DEFAULT. Exposing to a LAN requires CC_WEB_ALLOW_LAN=1
      (and TLS is strongly recommended via CC_WEB_CERT / CC_WEB_KEY).
    * NO usable default credentials — a strong one-time password is generated and
      printed if CC_WEB_PASS is unset. Credentials are verified in constant time.
    * The SocketIO layer is AUTHENTICATED: the connect handler rejects any socket
      whose session is not authenticated or whose CSRF/connection token is wrong,
      and every event re-checks auth and validates the target port. (Previously the
      socket handlers were completely unauthenticated — anyone on the network could
      drive attached attack hardware.)
    * cors_allowed_origins is an explicit allowlist (never '*').
    * CSRF token required on state-changing POSTs and on the socket handshake.
    * Per-IP rate limiting on auth and on command/flash actions.
    * Stable, file-persisted (0600) secret key so signed sessions survive restarts.
    * Strict security headers + Secure/HttpOnly/SameSite=Strict session cookie.
    * Optional shared AuditTrail records every flash, command, and auth event.
"""

from __future__ import annotations

import functools
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, g, jsonify, render_template, request, session
from flask_socketio import SocketIO, emit

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine, FirmwareProfile
from src.security.web_auth import (
    RateLimiter,
    csrf_valid,
    load_or_create_secret_key,
    new_csrf_token,
    resolve_web_credentials,
)

log = logging.getLogger(__name__)

_PROFILES_DIR = Path(__file__).resolve().parents[3] / "src" / "config" / "profiles"
_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

_MAX_CONTENT_LENGTH = 256 * 1024  # cap request bodies (no giant uploads)
_MAX_COMMAND_LEN = 256


def _load_profiles() -> dict[str, Path]:
    """Load firmware profile names and paths from the profiles directory."""
    profiles: dict[str, Path] = {}
    if _PROFILES_DIR.is_dir():
        for f in sorted(_PROFILES_DIR.glob("*.json")):
            try:
                p = FirmwareProfile.from_file(f)
                name = p.name or f.stem
            except Exception:
                name = f.stem
            profiles[name] = f
    return profiles


def create_app(
    device_manager: DeviceManager,
    flash_engine: FlashEngine,
    event_bus: EventBus,
    target_pool: TargetPool,
    *,
    audit: Any = None,
    allowed_origins: list[str] | None = None,
) -> tuple[Flask, SocketIO]:
    """Create and configure the hardened Flask application and SocketIO instance."""

    app = Flask(
        __name__,
        template_folder=str(_TEMPLATE_DIR),
        static_folder=str(_STATIC_DIR),
    )
    # Stable, persisted secret key (0600) so signed sessions survive restarts.
    app.secret_key = load_or_create_secret_key()
    tls_enabled = bool(os.environ.get("CC_WEB_CERT") and os.environ.get("CC_WEB_KEY"))
    app.config.update(
        MAX_CONTENT_LENGTH=_MAX_CONTENT_LENGTH,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=tls_enabled,
        JSON_SORT_KEYS=False,
    )

    # Explicit CORS allowlist — NEVER '*'. Empty list => same-origin only.
    origins = allowed_origins if allowed_origins is not None else []
    socketio = SocketIO(app, async_mode="threading", cors_allowed_origins=origins)
    profiles = _load_profiles()

    creds, _generated = resolve_web_credentials(log)
    login_limiter = RateLimiter(max_events=8, window_seconds=60.0)
    cmd_limiter = RateLimiter(max_events=60, window_seconds=10.0)

    # L-2: the web remote drives attack hardware; auth/flash/serial events must be auditable.
    # The normal launch path threads a durable AuditTrail through, but an embedder using the
    # create_app default would silently get no audit — warn so that's never a silent gap.
    if audit is None:
        log.warning(
            "Web remote created without an audit sink — auth/flash/serial events will NOT be "
            "recorded. Pass audit=AuditTrail(persist_path=...) for a durable forensic trail."
        )

    # ── Helpers ─────────────────────────────────────────────────────

    def _client_ip() -> str:
        return request.remote_addr or "unknown"

    def _audit(action: str, **details: Any) -> None:
        if audit is not None:
            try:
                audit.record(action, {"ip": _client_ip(), **details})
            except Exception:
                log.exception("audit record failed")

    def _ensure_csrf() -> str:
        token = session.get("csrf")
        if not token:
            token = new_csrf_token()
            session["csrf"] = token
        return token

    def _csp_nonce() -> str:
        # One per-request nonce, shared by the template render (context processor) and the CSP
        # header (after_request) via the request-scoped ``g`` (L-4).
        nonce = getattr(g, "_csp_nonce", None)
        if nonce is None:
            nonce = secrets.token_urlsafe(16)
            g._csp_nonce = nonce
        return nonce

    def check_auth(username: str | None, password: str | None) -> bool:
        return creds.verify(username, password)

    def requires_auth(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            if session.get("authenticated"):
                _ensure_csrf()
                return f(*args, **kwargs)
            ip = _client_ip()
            if not login_limiter.allow(ip):
                _audit("web_auth_ratelimited")
                return Response("Too many attempts. Try again later.\n", 429)
            auth = request.authorization
            if auth and check_auth(auth.username, auth.password):
                # M-3: rotate the session + CSRF token at the auth boundary so any token an
                # attacker could have observed or seeded *pre-auth* is invalidated (session
                # fixation defense-in-depth — parity with the rest of the auth code).
                session.clear()
                session["authenticated"] = True
                session["user"] = auth.username
                session["csrf"] = new_csrf_token()
                _audit("web_auth_ok", user=auth.username)
                return f(*args, **kwargs)
            _audit("web_auth_fail", user=(auth.username if auth else None))
            return Response(
                "Authentication required.\n",
                401,
                {"WWW-Authenticate": 'Basic realm="Cyber Controller"'},
            )

        return decorated

    def requires_csrf(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            token = request.headers.get("X-CSRF-Token")
            if not token:
                body = request.get_json(silent=True) or {}
                token = body.get("_csrf")
            if not csrf_valid(session.get("csrf"), token):
                _audit("web_csrf_fail", path=request.path)
                abort(403)
            return f(*args, **kwargs)

        return decorated

    def _known_port(port: str) -> bool:
        """True only if *port* is a currently-registered device port."""
        return any(d.port == port for d in device_manager.list_devices())

    @app.context_processor
    def _inject_csrf() -> dict[str, str]:
        return {"csrf_token": session.get("csrf", ""), "csp_nonce": _csp_nonce()}

    @app.after_request
    def _security_headers(resp: Response) -> Response:
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        # CSP (L-4): script-src uses a per-request nonce instead of 'unsafe-inline', so the inline
        # <script> blocks (each tagged nonce="{{ csp_nonce }}") run while ANY injected/inline
        # script without the nonce is blocked — a real backstop behind the textContent rendering,
        # and the reason all former inline on*= handlers were moved into nonce'd scripts. A
        # browser that honors the nonce ignores 'unsafe-inline' entirely. style-src keeps
        # 'unsafe-inline' (no script execution there; styles are static/Jinja-escaped).
        nonce = _csp_nonce()
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' ws: wss:; "
            "img-src 'self' data:; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ── Event bus wiring ────────────────────────────────────────────

    def _on_target_added(_topic: str, payload: dict) -> None:
        socketio.emit("target_discovered", payload)

    def _on_device_connected(device) -> None:
        socketio.emit("device_connected", device.to_dict())

    def _on_device_disconnected(device) -> None:
        socketio.emit("device_disconnected", device.to_dict())

    event_bus.subscribe("target.added", _on_target_added)
    device_manager.on_device_connected(_on_device_connected)
    device_manager.on_device_disconnected(_on_device_disconnected)

    # ── Page routes ─────────────────────────────────────────────────

    @app.route("/")
    @requires_auth
    def dashboard():
        devices = device_manager.list_devices()
        n_connected = len([d for d in devices if d.connected])
        return render_template(
            "dashboard.html",
            devices=devices,
            device_count=len(devices),
            connected_count=n_connected,
            target_count=target_pool.count,
        )

    @app.route("/devices")
    @requires_auth
    def devices_page():
        return render_template("devices.html", devices=device_manager.list_devices())

    @app.route("/flash")
    @requires_auth
    def flash_page():
        ports = device_manager.scan_ports()
        return render_template("flash.html", ports=ports, profiles=list(profiles.keys()))

    @app.route("/targets")
    @requires_auth
    def targets_page():
        return render_template("targets.html", targets=target_pool.all())

    @app.route("/terminal/<port>")
    @requires_auth
    def terminal_page(port: str):
        device = device_manager.get_device(port)
        return render_template("terminal.html", port=port, device=device)

    # ── API routes ──────────────────────────────────────────────────

    @app.route("/api/flash", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_flash():
        data = request.get_json(force=True, silent=True) or {}
        port = str(data.get("port", ""))
        profile_name = str(data.get("profile_id", ""))

        if not port:
            return jsonify({"error": "port is required"}), 400
        if not profile_name:
            return jsonify({"error": "profile_id is required"}), 400
        if not _known_port(port):
            return jsonify({"error": f"Unknown/unregistered port: {port}"}), 400

        profile_path = profiles.get(profile_name)
        if not profile_path:
            return jsonify({"error": f"Unknown profile: {profile_name}"}), 404

        profile = flash_engine.load_profile(profile_path)
        _audit("flash", user=session.get("user"), port=port, profile=profile_name)

        def progress_cb(pct: int, msg: str) -> None:
            socketio.emit("flash_progress", {"port": port, "percent": pct, "message": msg})

        import threading

        def flash_thread() -> None:
            ok = flash_engine.flash(port, profile, progress_callback=progress_cb)
            socketio.emit(
                "flash_progress",
                {
                    "port": port,
                    "percent": 100 if ok else 0,
                    "message": "Flash complete" if ok else "Flash failed",
                    "done": True,
                    "success": ok,
                },
            )

        threading.Thread(target=flash_thread, daemon=True).start()
        return jsonify({"status": "flashing", "port": port, "profile": profile_name})

    @app.route("/api/command", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_command():
        if not cmd_limiter.allow(_client_ip()):
            return jsonify({"error": "rate limited"}), 429
        data = request.get_json(force=True, silent=True) or {}
        port = str(data.get("port", ""))
        command = str(data.get("command", ""))

        if not port or not command:
            return jsonify({"error": "port and command are required"}), 400
        if len(command) > _MAX_COMMAND_LEN:
            return jsonify({"error": "command too long"}), 400
        if not _known_port(port):
            return jsonify({"error": f"Unknown/unregistered port: {port}"}), 400

        conn = device_manager.get_connection(port)
        if not conn or not conn.is_connected:
            return jsonify({"error": f"No active connection on {port}"}), 400

        try:
            conn.write(command)  # SerialConnection.write rejects embedded control chars
            _audit("serial_command", user=session.get("user"), port=port, command=command)
            return jsonify({"status": "sent", "port": port, "command": command})
        except ValueError as exc:
            # The validation message (e.g. "embedded control character") is useful to the
            # operator and not sensitive — safe to surface.
            return jsonify({"error": str(exc)}), 400
        except Exception:
            # Never leak internal exception text (an AI-codegen classic). Log server-side,
            # return a generic message.
            log.exception("serial command failed on %s", port)
            return jsonify({"error": "internal error sending command"}), 500

    @app.route("/api/devices")
    @requires_auth
    def api_devices():
        return jsonify([d.to_dict() for d in device_manager.list_devices()])

    @app.route("/api/targets")
    @requires_auth
    def api_targets():
        return jsonify([t.to_dict() for t in target_pool.all()])

    @app.route("/api/health")
    @requires_auth
    def api_health():
        devices = device_manager.list_devices()
        return jsonify(
            {
                "status": "ok",
                "device_count": len(devices),
                "connected_count": len([d for d in devices if d.connected]),
                "target_count": target_pool.count,
                "flash_status": flash_engine.status.value,
            }
        )

    # ── SocketIO events (AUTHENTICATED) ─────────────────────────────

    def _socket_authed() -> bool:
        return bool(session.get("authenticated"))

    @socketio.on("connect")
    def on_ws_connect(auth=None):
        """Reject any socket that is not from an authenticated session with a valid
        CSRF/connection token. Returning False refuses the connection."""
        if not _socket_authed():
            log.warning("Rejected unauthenticated WebSocket from %s", _client_ip())
            _audit("ws_reject_unauth")
            return False
        if not csrf_valid(session.get("csrf"), (auth or {}).get("csrf")):
            log.warning("Rejected WebSocket with bad CSRF from %s", _client_ip())
            _audit("ws_reject_csrf")
            return False
        log.info("WebSocket client authenticated (%s)", session.get("user"))
        return True

    # One fan-out callback per port (audit M-1): without this, every subscribe_serial registered a
    # NEW on_line callback that was never removed, so K subscribes => K emits per serial line
    # (callback leak + self-amplifying DoS). We keep exactly one callback per port.
    _serial_subs: dict = {}

    @socketio.on("subscribe_serial")
    def on_subscribe_serial(data: dict) -> None:
        if not _socket_authed():
            return
        if not cmd_limiter.allow(_client_ip()):  # subscribe is now rate-limited too
            emit("serial_output", {"port": "", "line": "[Rate limited]"})
            return
        port = str((data or {}).get("port", ""))
        if not _known_port(port):
            emit("serial_output", {"port": port, "line": f"[Unknown port {port}]"})
            return
        conn = device_manager.get_connection(port)
        if conn and conn.is_connected:
            prev = _serial_subs.get(port)
            if prev is not None:
                conn.remove_line_callback(prev)  # drop any prior/stale callback first
            cb = (lambda line, p=port: socketio.emit("serial_output", {"port": p, "line": line}))
            conn.on_line(cb)
            _serial_subs[port] = cb
            emit("serial_output", {"port": port, "line": f"[Subscribed to {port}]"})
        else:
            emit("serial_output", {"port": port, "line": f"[Not connected to {port}]"})

    @socketio.on("send_command")
    def on_send_command(data: dict) -> None:
        if not _socket_authed():
            return
        if not cmd_limiter.allow(_client_ip()):
            emit("serial_output", {"port": "", "line": "[Rate limited]"})
            return
        port = str((data or {}).get("port", ""))
        command = str((data or {}).get("command", ""))
        if len(command) > _MAX_COMMAND_LEN:
            emit("serial_output", {"port": port, "line": "[Command too long]"})
            return
        if not _known_port(port):
            emit("serial_output", {"port": port, "line": f"[Unknown port {port}]"})
            return
        conn = device_manager.get_connection(port)
        if conn and conn.is_connected:
            try:
                conn.write(command)
                _audit("serial_command_ws", user=session.get("user"), port=port, command=command)
                emit("serial_output", {"port": port, "line": f"> {command}"})
            except Exception as exc:
                emit("serial_output", {"port": port, "line": f"[Error: {exc}]"})
        else:
            emit("serial_output", {"port": port, "line": f"[Not connected to {port}]"})

    return app, socketio


def _compute_allowed_origins(host: str, port: int) -> list[str]:
    """Build the explicit CORS/WebSocket origin allowlist for this bind."""
    origins: set[str] = set()
    for h in ("127.0.0.1", "localhost"):
        origins.add(f"http://{h}:{port}")
        origins.add(f"https://{h}:{port}")
    if host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        origins.add(f"http://{host}:{port}")
        origins.add(f"https://{host}:{port}")
    for extra in os.environ.get("CC_WEB_ORIGINS", "").split(","):
        if extra.strip():
            origins.add(extra.strip())
    return sorted(origins)


def launch_web(
    device_manager: DeviceManager,
    flash_engine: FlashEngine,
    event_bus: EventBus,
    target_pool: TargetPool,
    *,
    host: str = "127.0.0.1",
    port: int = 5000,
    audit: Any = None,
) -> int:
    """Create and run the hardened Flask web remote UI.

    Defaults to binding 127.0.0.1. Binding to a non-local address requires the
    explicit opt-in CC_WEB_ALLOW_LAN=1 (and TLS via CC_WEB_CERT/CC_WEB_KEY is
    strongly recommended for LAN exposure).
    """
    is_local = host in ("127.0.0.1", "localhost", "::1")
    if not is_local and os.environ.get("CC_WEB_ALLOW_LAN") != "1":
        log.error(
            "Refusing to bind the web remote to %s (non-local). The web UI controls "
            "attack hardware — only expose it deliberately. Set CC_WEB_ALLOW_LAN=1 to "
            "opt in, and provide TLS via CC_WEB_CERT/CC_WEB_KEY.",
            host,
        )
        return 2

    origins = _compute_allowed_origins(host, port)
    app, socketio = create_app(
        device_manager, flash_engine, event_bus, target_pool,
        audit=audit, allowed_origins=origins,
    )

    ssl_args: dict[str, Any] = {}
    certfile = os.environ.get("CC_WEB_CERT")
    keyfile = os.environ.get("CC_WEB_KEY")
    if certfile and keyfile:
        ssl_args["certfile"] = certfile
        ssl_args["keyfile"] = keyfile
        log.info("Web remote TLS enabled (cert=%s)", certfile)
    elif not is_local:
        log.warning("Binding to %s WITHOUT TLS — credentials/serial output are in cleartext.", host)

    scheme = "https" if ssl_args else "http"
    # H-2: this app runs SocketIO in threading mode (async_mode="threading" at construction) for
    # stability with the serial/threading-heavy core — so it serves on the Werkzeug DEV server,
    # which needs allow_unsafe_werkzeug and is explicitly not hardened for hostile exposure
    # (single-process, weak request parsing). We must never *silently* serve LAN traffic on it:
    # for a non-local bind, require either a fronting reverse proxy (the recommended path) or an
    # extra explicit opt-in (CC_WEB_ALLOW_DEV_SERVER=1) acknowledging the risk. Localhost is
    # unchanged. (If a future build switches to a real eventlet/gevent worker, async_mode won't be
    # "threading" and this gate steps aside automatically.)
    using_dev_server = getattr(socketio, "async_mode", "threading") == "threading"
    if not is_local and using_dev_server and os.environ.get("CC_WEB_ALLOW_DEV_SERVER") != "1":
        log.error(
            "Refusing to serve the web remote to %s on the Werkzeug DEV server. It is not "
            "hardened for hostile exposure (single-process, weak request parsing), and the web UI "
            "drives attack hardware. Put a hardened TLS-terminating reverse proxy in front (and "
            "keep the bind on localhost), or set CC_WEB_ALLOW_DEV_SERVER=1 to accept the risk on a "
            "trusted/isolated LAN.",
            host,
        )
        return 3
    run_kwargs: dict[str, Any] = dict(ssl_args)
    if using_dev_server:
        # Only the dev-server path takes (and needs) this flag; production workers reject it.
        run_kwargs["allow_unsafe_werkzeug"] = True
    server_kind = "Werkzeug dev server" if using_dev_server else getattr(socketio, "async_mode", "?")
    log.info(
        "Starting web UI on %s://%s:%d (origins=%s, server=%s)",
        scheme, host, port, origins, server_kind,
    )
    socketio.run(app, host=host, port=port, debug=False, **run_kwargs)
    return 0
