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
import threading
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    render_template,
    request,
    send_from_directory,
    session,
)
from flask_socketio import SocketIO, emit

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FirmwareProfile, FlashEngine
from src.core.nodes_controller import NodesController
from src.core.resources import resource_path
from src.core import node_provision
from src.security import physical_key
from src.security.web_auth import (
    RateLimiter,
    csrf_valid,
    load_or_create_secret_key,
    new_csrf_token,
    resolve_web_credentials,
)

log = logging.getLogger(__name__)

_PROFILES_DIR = resource_path("src", "config", "profiles")
# Resolve bundled web assets via resource_path (sys._MEIPASS-aware), NOT Path(__file__): in the frozen
# build __file__ points into a MEIPASS path that was never populated, so Flask would raise
# TemplateNotFound (HTTP 500) on every page and 404 every /static asset. build.py bundles both dirs.
_TEMPLATE_DIR = resource_path("src", "ui", "web", "templates")
_STATIC_DIR = resource_path("src", "ui", "web", "static")

_MAX_CONTENT_LENGTH = 256 * 1024  # cap request bodies (no giant uploads)
_MAX_COMMAND_LEN = 256
_MAX_LABEL_LEN = 64


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
    nodes_controller: NodesController | None = None,
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

    # W1.1(web): the key-free wireless-node manager. Same UI-agnostic controller the Qt/tk tabs use; its
    # default vault getter reads the gate-sealed vault, so the keys never reach this process' request path.
    # Injectable so tests can drive locked/unlocked states without a real gate.
    nodes = nodes_controller if nodes_controller is not None else NodesController(device_manager)

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
            # SEC-A1: the per-IP RateLimiter above is in-memory and resets on restart, so on its own
            # it lets a "relaunch and keep guessing" brute force through. Honor the SAME persistent,
            # restart-surviving lockout the console/Qt gate uses (physical_key), so all three UIs
            # share one failure counter + cooldown (and the owner's opt-in duress wipe).
            lockout = physical_key.lockout_status()
            if lockout["locked"]:
                _audit("web_auth_locked", remaining=lockout["remaining_secs"])
                return Response(
                    f"Locked: too many failed attempts. Try again in {lockout['remaining_secs']}s.\n",
                    429,
                )
            auth = request.authorization
            if auth and check_auth(auth.username, auth.password):
                physical_key.record_successful_unlock()  # reset the shared persistent counter
                # M-3: rotate the session + CSRF token at the auth boundary so any token an
                # attacker could have observed or seeded *pre-auth* is invalidated (session
                # fixation defense-in-depth — parity with the rest of the auth code).
                session.clear()
                session["authenticated"] = True
                session["user"] = auth.username
                session["csrf"] = new_csrf_token()
                _audit("web_auth_ok", user=auth.username)
                return f(*args, **kwargs)
            # Only count a failure when credentials were actually PRESENTED but wrong. A request with no
            # Authorization header (the browser's normal pre-auth 401 handshake, or a cross-site no-cred
            # GET) must not drive the shared lockout — otherwise an unauthenticated party can lock the
            # owner out of the local gate without ever guessing a password. And allow_wipe=False: the
            # network surface may never trigger the physical duress wipe.
            if auth:
                physical_key.record_failed_attempt(allow_wipe=False)
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
        """True if *port* is a registered device port OR a live, currently-present serial port.

        The Flash page dropdown is built from a LIVE ``scan_ports()`` enumeration, but a device that was
        already plugged in when the server started is NEVER hot-plug-registered (HotPlugMonitor seeds it
        into its ``_known_ports`` set without ``add_device``-ing it), so the registry alone would reject
        the very port the user just selected — /api/flash would 400 every visible port. We therefore
        also accept any port present in a fresh scan (the same source the page renders from). This still
        rejects a port that does not physically exist, so it is not an accept-all gate.
        """
        if any(d.port == port for d in device_manager.list_devices()):
            return True
        return any(d.port == port for d in device_manager.scan_ports())

    def _devices_for_display() -> list:
        """Registered devices, merged with any live-scanned port not yet in the registry.

        A device already plugged in at startup is never hot-plug-registered, so the raw registry is
        empty and /devices reads 'No devices detected' even with hardware attached — leaving the user no
        way to open a connection (and, downstream, the whole serial-command surface unreachable). Showing
        the live-scanned ports too gives every present device a Connect action. A registered entry (which
        carries live connection state) always wins over the fresh scan Device for the same port.
        """
        registered = {d.port: d for d in device_manager.list_devices()}
        merged = list(registered.values())
        for d in device_manager.scan_ports():
            if d.port not in registered:
                merged.append(d)
        return merged

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
            # No external script origin: the Socket.IO client is vendored + served same-origin, so
            # script-src is now 'self' + the per-request nonce only (cdnjs removed — tighter, and the
            # web remote no longer breaks offline or if a CDN is compromised).
            f"script-src 'self' 'nonce-{nonce}'; "
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
        # Merged live scan, not the raw registry: a board plugged in BEFORE the server started is seeded
        # into the hotplug monitor's _known_ports without an add_device, so list_devices() alone reports
        # "0 devices" on the landing page while /devices (which already uses this helper) shows it. Count
        # what's actually attached. connected_count still keys off d.connected, so a scanned-but-unconnected
        # port correctly reads present-not-connected.
        devices = _devices_for_display()
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
        return render_template("devices.html", devices=_devices_for_display())

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

    @app.route("/nodes")
    @requires_auth
    def nodes_page():
        # Fail CLOSED server-side: if the vault is locked (or any read error) render the notice and NEVER the
        # table. list_rows() is already key-redacted, so no key byte can reach the response even on this path.
        try:
            unlocked = nodes.is_unlocked()
            rows = nodes.list_rows() if unlocked else []
            gateways = nodes.available_gateways() if unlocked else []
        except Exception:
            log.exception("nodes page read failed")
            unlocked, rows, gateways = False, [], []
        return render_template("nodes.html", unlocked=unlocked, rows=rows, gateways=gateways)

    @app.route("/remote")
    @requires_auth
    def remote_page():
        # Touch-first quick-command home (MB). Buttons fire the SAME guarded /api/command path; flagged
        # commands are LABELLED (never blocked) and confirmed client-side. Commands come from the real
        # per-firmware protocol registries via quick_commands — no phantom commands.
        from src.core.quick_commands import grouped_quick_commands
        remotes = []
        for d in device_manager.list_devices():
            if not d.connected:
                continue
            remotes.append({
                "port": d.port,
                "name": d.name,
                "firmware": d.firmware,
                "groups": grouped_quick_commands(d.firmware),
            })
        return render_template("remote.html", remotes=remotes, active="remote")

    @app.route("/device/<port>")
    @requires_auth
    def device_view_page(port: str):
        # Web Device View (MB P3): render the firmware's reconstructed on-screen menu (the SAME MenuNode tree
        # the Qt Device View uses, via src.core.device_menus) as a navigable screen. Leaves fire the EXISTING
        # guarded /api/command; flagged commands are labelled + confirmed client-side (label-never-block).
        import json as _json
        from src.core.device_menus import menu_tree
        device = device_manager.get_device(port)
        tree = menu_tree(device.firmware) if device else None
        # Escape <,>,& so the JSON embedded in a <script> tag can never break out (defense-in-depth; the menu
        # data is developer-authored, but never trust a serialized blob inside markup).
        tree_json = "null"
        if tree is not None:
            tree_json = (_json.dumps(tree).replace("<", "\\u003c").replace(">", "\\u003e")
                         .replace("&", "\\u0026"))
        return render_template("device.html", port=port, device=device, tree=tree,
                               tree_json=tree_json, active="device")

    # ── PWA shell (MB cluster: installable LAN wireless remote) ─────
    # manifest + service worker are PUBLIC (carry no secrets) so the browser can read them before auth
    # completes — standard PWA practice. The SW is served from the ORIGIN ROOT (a /static/ worker could
    # only control /static/) with Service-Worker-Allowed so its scope is the whole app, and it is
    # structurally forbidden from caching authenticated data (see static/sw.js).

    @app.route("/manifest.webmanifest")
    def web_manifest():
        resp = send_from_directory(_STATIC_DIR, "manifest.webmanifest")
        resp.headers["Content-Type"] = "application/manifest+json"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/sw.js")
    def service_worker():
        resp = send_from_directory(_STATIC_DIR, "sw.js")
        resp.headers["Content-Type"] = "text/javascript"
        resp.headers["Service-Worker-Allowed"] = "/"   # allow root scope despite the /sw.js path
        resp.headers["Cache-Control"] = "no-cache"      # always revalidate so SW updates land
        return resp

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

        try:
            profile = flash_engine.load_profile(profile_path)
        except Exception as exc:  # noqa: BLE001 — a malformed profile must surface as a clean 400, not an opaque 500
            return jsonify({"error": f"Invalid firmware profile ({profile_path.name}): {exc}"}), 400
        # Reject fast if the port is already mid flash/backup/erase — a second esptool on the same UART
        # can brick the board. (The engine's per-port guard is the hard backstop against the TOCTOU
        # window; this 409 is the clean API answer so a scripted caller doesn't kick off a doomed thread.)
        if flash_engine.is_port_busy(port):
            return jsonify({"error": f"Port {port} is busy with another operation"}), 409
        _audit("flash", user=session.get("user"), port=port, profile=profile_name)

        # Free the UART before esptool takes it. A web-opened monitor connection (/api/connect, /terminal)
        # still holds this port: on Windows the handle is exclusive so esptool's open fails with
        # "Access is denied" and the flash dies with no hint to disconnect; on POSIX the reader thread and
        # esptool read the same tty concurrently and corrupt the flash. Force-release any managed
        # connection (no owner) so the port is clear before flashing. (The /api/connect + /api/command
        # busy-guards below stop a client from re-grabbing it mid-flash.)
        device_manager.close_connection(port)

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

    @app.route("/api/connect", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_connect():
        # Open a managed serial connection so the command surface (/api/command, subscribe_serial,
        # send_command) can actually reach the device. Without this route get_connection(port) is always
        # None — the web remote could never talk to a real board. Registers the scanned Device first (a
        # port present at startup is not yet in the registry), then opens the link.
        data = request.get_json(force=True, silent=True) or {}
        port = str(data.get("port", ""))
        if not port:
            return jsonify({"error": "port is required"}), 400
        # Refuse to open a serial connection on a port that is mid-flash: esptool owns the UART and a
        # second opener would contend with it (brick risk). 409 mirrors /api/flash's own busy answer.
        if flash_engine.is_port_busy(port):
            return jsonify({"error": f"Port {port} is busy with a flash operation"}), 409
        if device_manager.get_device(port) is None:
            match = next((d for d in device_manager.scan_ports() if d.port == port), None)
            if match is None:
                return jsonify({"error": f"Unknown/unregistered port: {port}"}), 400
            device_manager.add_device(match)
        try:
            device_manager.open_connection(port, owner="web")
        except Exception:  # noqa: BLE001 — surface a clean 400; the OS/serial error text is logged, not leaked
            log.exception("web connect failed on %s", port)
            return jsonify({"error": f"Could not open a connection on {port}"}), 400
        _audit("device_connect", user=session.get("user"), port=port)
        return jsonify({"status": "connected", "port": port})

    @app.route("/api/disconnect", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_disconnect():
        data = request.get_json(force=True, silent=True) or {}
        port = str(data.get("port", ""))
        if not port:
            return jsonify({"error": "port is required"}), 400
        if not _known_port(port):
            return jsonify({"error": f"Unknown/unregistered port: {port}"}), 400
        device_manager.close_connection(port, owner="web")
        _audit("device_disconnect", user=session.get("user"), port=port)
        return jsonify({"status": "disconnected", "port": port})

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
        # Never push operator bytes onto a UART that esptool is mid-flash on — a stray write during the
        # flash can brick the board. 409, consistent with /api/flash and /api/connect.
        if flash_engine.is_port_busy(port):
            return jsonify({"error": f"Port {port} is busy with a flash operation"}), 409

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

    # ── Node mutations (W1.1) — CSRF+auth-gated, delegate to the controller ──

    def _json_body() -> dict:
        # force=True parses even without a JSON content-type; coerce ANY non-object body (a bare scalar/array
        # like `5` or `[1,2]`) to {} so the routes' `.get(...)` can't AttributeError into an ungraceful 500.
        data = request.get_json(force=True, silent=True)
        return data if isinstance(data, dict) else {}

    def _node_id_arg(data: dict) -> int:
        """Parse + range-check a node id (0–65535) from a request body, or raise ValueError."""
        raw = data.get("node_id")
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise ValueError("node_id is required")
        try:
            nid = int(raw)
        except (TypeError, ValueError):
            raise ValueError("node_id must be an integer")
        if not (0 <= nid <= 65535):
            raise ValueError("node_id out of range (0–65535)")
        return nid

    def _node_action(fn, expose: str | None = None):
        """Run a controller mutation and map results to JSON. The controller's return value (a provisioning
        dict / NodeLink) is NEVER serialized — only an explicit boolean via *expose* — so no key material can
        leak. Known, key-free errors surface their text (api_command idiom); everything else is genericized.
        A locked vault makes the controller raise VaultLockedError, so mutations fail CLOSED regardless of UI."""
        try:
            result = fn()
        except node_provision.VaultLockedError:
            return jsonify({"error": "vault is locked"}), 403
        except (ValueError, node_provision.NodeProvisionError) as exc:
            # These messages are f-strings over node_id/role/port — never key bytes. Safe to surface.
            return jsonify({"error": str(exc)}), 400
        except Exception:
            log.exception("node action failed")
            return jsonify({"error": "internal error"}), 500
        payload = {"status": "ok"}
        if expose is not None:
            payload[expose] = bool(result)   # only ever a bool, never a controller object
        return jsonify(payload)

    @app.route("/api/nodes/provision", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_nodes_provision():
        data = _json_body()
        try:
            nid = _node_id_arg(data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        role = str(data.get("role", "host"))
        label = str(data.get("label", ""))
        if len(label) > _MAX_LABEL_LEN:   # enforce the template's 64-char intent server-side too
            return jsonify({"error": "label too long"}), 400
        _audit("node_provision", user=session.get("user"), node_id=nid, role=role)
        return _node_action(lambda: nodes.provision(nid, role=role, label=label))

    @app.route("/api/nodes/rotate", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_nodes_rotate():
        data = _json_body()
        try:
            nid = _node_id_arg(data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        _audit("node_rotate", user=session.get("user"), node_id=nid)
        return _node_action(lambda: nodes.rotate(nid))

    @app.route("/api/nodes/deprovision", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_nodes_deprovision():
        data = _json_body()
        try:
            nid = _node_id_arg(data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        _audit("node_deprovision", user=session.get("user"), node_id=nid)
        return _node_action(lambda: nodes.deprovision(nid), expose="removed")

    @app.route("/api/nodes/attach", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_nodes_attach():
        data = _json_body()
        try:
            nid = _node_id_arg(data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        port = str(data.get("gateway_port", ""))
        if not port:
            return jsonify({"error": "gateway_port is required"}), 400
        _audit("node_attach", user=session.get("user"), node_id=nid, port=port)
        return _node_action(lambda: nodes.attach_via_port(nid, port))

    @app.route("/api/nodes/detach", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_nodes_detach():
        data = _json_body()
        try:
            nid = _node_id_arg(data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        _audit("node_detach", user=session.get("user"), node_id=nid)
        return _node_action(lambda: nodes.detach(nid), expose="detached")

    @app.route("/api/devices")
    @requires_auth
    def api_devices():
        # Merged live scan (see dashboard): count/list what's physically attached, not just the registry,
        # so a board present before server start isn't reported as absent.
        return jsonify([d.to_dict() for d in _devices_for_display()])

    @app.route("/api/targets")
    @requires_auth
    def api_targets():
        return jsonify([t.to_dict() for t in target_pool.all()])

    @app.route("/api/health")
    @requires_auth
    def api_health():
        devices = _devices_for_display()
        # The engine's scalar status is a single shared field; with parallel multi-board flashing a
        # finished op sets it to DONE while another port is still writing. Report per-port truth: while
        # any port is busy, never surface a terminal status (a poller would re-enable controls mid-flash).
        active = flash_engine.active_ports()
        return jsonify(
            {
                "status": "ok",
                "device_count": len(devices),
                "connected_count": len([d for d in devices if d.connected]),
                "target_count": target_pool.count,
                "flash_status": "flashing" if active else flash_engine.status.value,
                "busy_ports": active,
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
        # Coerce a non-dict handshake `auth` (client-controlled: could be a JSON string/array/number) to {}
        # so the CSRF read below can't AttributeError past the clean refuse-and-audit path — mirrors the
        # isinstance guard in on_subscribe_serial / on_send_command. `auth or {}` only handles falsy.
        if not isinstance(auth, dict):
            auth = {}
        if not csrf_valid(session.get("csrf"), auth.get("csrf")):
            log.warning("Rejected WebSocket with bad CSRF from %s", _client_ip())
            _audit("ws_reject_csrf")
            return False
        log.info("WebSocket client authenticated (%s)", session.get("user"))
        return True

    # One fan-out callback per port (audit M-1): without this, every subscribe_serial registered a
    # NEW on_line callback that was never removed, so K subscribes => K emits per serial line
    # (callback leak + self-amplifying DoS). We keep exactly one callback per port.
    #
    # SocketIO runs with async_mode="threading", so two authenticated clients subscribing to the SAME
    # port execute this handler concurrently in separate threads. The check-then-act below (read prev,
    # remove_line_callback, on_line, store) must be atomic per the shared map: without the lock, two
    # threads can both see the same/None prev, both call conn.on_line(cb), and the last writer wins the
    # store — orphaning the earlier callback on the SerialConnection with no way to ever remove it,
    # re-introducing the exact untracked-callback leak this map exists to prevent.
    _serial_subs: dict = {}
    _serial_subs_lock = threading.Lock()

    @socketio.on("subscribe_serial")
    def on_subscribe_serial(data: dict) -> None:
        if not _socket_authed():
            return
        if not cmd_limiter.allow(_client_ip()):  # subscribe is now rate-limited too
            emit("serial_output", {"port": "", "line": "[Rate limited]"})
            return
        # Coerce any non-object payload (a bare scalar/array) to {} — mirrors _json_body() on the HTTP
        # twin so .get() below can't AttributeError on e.g. a list. `data or {}` only handles falsy.
        if not isinstance(data, dict):
            data = {}
        port = str(data.get("port", ""))
        if not _known_port(port):
            emit("serial_output", {"port": port, "line": f"[Unknown port {port}]"})
            return
        conn = device_manager.get_connection(port)
        if conn and conn.is_connected:
            with _serial_subs_lock:
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
        # Coerce any non-object payload (a bare scalar/array) to {} — mirrors _json_body() on the HTTP
        # twin so .get() below can't AttributeError on e.g. a list. `data or {}` only handles falsy.
        if not isinstance(data, dict):
            data = {}
        port = str(data.get("port", ""))
        command = str(data.get("command", ""))
        if len(command) > _MAX_COMMAND_LEN:
            emit("serial_output", {"port": port, "line": "[Command too long]"})
            return
        if not _known_port(port):
            emit("serial_output", {"port": port, "line": f"[Unknown port {port}]"})
            return
        conn = device_manager.get_connection(port)
        if conn and conn.is_connected:
            try:
                conn.write(command)  # SerialConnection.write rejects embedded control chars
                _audit("serial_command_ws", user=session.get("user"), port=port, command=command)
                emit("serial_output", {"port": port, "line": f"> {command}"})
            except ValueError as exc:
                # The validation message (e.g. "embedded control character") is useful + not sensitive.
                emit("serial_output", {"port": port, "line": f"[Error: {exc}]"})
            except Exception:
                # Parity with the HTTP /api/command path: never leak internal exception text (device
                # paths / OS errno) to the client. Log server-side, surface a generic message.
                log.exception("serial command (ws) failed on %s", port)
                emit("serial_output", {"port": port, "line": "[Error sending command]"})
        else:
            emit("serial_output", {"port": port, "line": f"[Not connected to {port}]"})

    return app, socketio


def _compute_allowed_origins(host: str, port: int) -> list[str]:
    """Build the explicit CORS/WebSocket origin allowlist for this bind."""
    origins: set[str] = set()
    for h in ("127.0.0.1", "localhost"):
        origins.add(f"http://{h}:{port}")
        origins.add(f"https://{h}:{port}")
    hosts: list[str] = []
    if host in ("0.0.0.0", "::"):
        # Wildcard bind: a LAN client's Origin header is the server's REAL LAN IP, which is neither
        # localhost nor "0.0.0.0". Without adding it, engineio rejects the Socket.IO handshake and every
        # real-time feature silently dies. Enumerate the machine's own addresses (best-effort).
        try:
            import socket as _socket
            name = _socket.gethostname()
            try:
                hosts.extend(_socket.gethostbyname_ex(name)[2])
            except Exception:  # noqa: BLE001
                pass
            try:
                hosts.extend(info[4][0] for info in _socket.getaddrinfo(name, None))
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
    elif host not in ("127.0.0.1", "localhost", "::1"):
        hosts.append(host)
    for h in hosts:
        if not h or h in ("127.0.0.1", "::1", "localhost"):
            continue
        hh = f"[{h}]" if ":" in h else h  # bracket IPv6 literals in a URL origin
        origins.add(f"http://{hh}:{port}")
        origins.add(f"https://{hh}:{port}")
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
