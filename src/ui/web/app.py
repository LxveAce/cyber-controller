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
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
)
from flask_socketio import SocketIO, emit

from src.config import settings as app_settings
from src.core import host_shell, node_provision
from src.core.channel_survey import survey_channels
from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FirmwareProfile, FlashEngine
from src.core.nodes_controller import NodesController
from src.core.resources import resource_path
from src.core.target_freshness import summarize_freshness
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


def _resolve_client_ip(remote_addr: str | None, forwarded_for: str,
                       trusted_proxy_ips: frozenset[str]) -> str:
    """Resolve the real client IP for rate-limiting + audit — proxy-aware but spoof-resistant (W2).

    With no trusted proxies (the default), returns ``remote_addr`` unchanged: behind a reverse proxy
    that collapses every client to the proxy IP, but that is SAFE (one shared bucket / audit id)
    versus trusting a client-forgeable header. When the direct peer IS a trusted proxy, walk
    X-Forwarded-For from the RIGHT skipping trusted-proxy hops and return the first non-trusted
    address — the real client. A client can only forge the LEFT (earlier) XFF entries, which we
    never reach past a trusted-proxy chain, so it cannot spoof its IP as long as each trusted proxy
    appends the peer it saw.

    Pure function (no Flask request/globals) so the spoofing scenarios are directly unit-testable.
    """
    peer = remote_addr or "unknown"
    if peer not in trusted_proxy_ips:
        return peer  # direct/untrusted peer — NEVER trust an XFF header it could have forged
    for hop in reversed([h.strip() for h in forwarded_for.split(",") if h.strip()]):
        if hop not in trusted_proxy_ips:
            return hop
    return peer  # XFF absent or all-trusted-proxy hops -> fall back to the proxy itself


def create_app(
    device_manager: DeviceManager,
    flash_engine: FlashEngine,
    event_bus: EventBus,
    target_pool: TargetPool,
    *,
    audit: Any = None,
    allowed_origins: list[str] | None = None,
    nodes_controller: NodesController | None = None,
    trusted_proxies: list[str] | None = None,
    desktop_token: str | None = None,
    capture_store: Any = None,
    host_shell_loopback: bool = False,
    macro_recorder: Any = None,
    auto_router: Any = None,
    tail_tracker: Any = None,
    sensing_model: Any = None,
) -> tuple[Flask, SocketIO]:
    """Create and configure the hardened Flask application and SocketIO instance.

    ``capture_store`` (optional): the shared CaptureStore from the cross-comm spine (P0-1). Threaded so
    the CRACK captures surface can read it once wired (P1-5); ``None`` keeps every existing caller (and
    the tests) unchanged.

    ``desktop_token`` (loopback desktop shell only): a one-time bootstrap secret. When set, the
    ``/desktop-auth?token=`` route consumes it once to establish a session WITHOUT credentials in the
    URL — a browser refuses relative fetch() from a ``user:pass@host`` document, so the desktop window
    must reach a clean URL. It is inert (404) for the LAN ``--ui web`` server, which never sets it, so
    this is not a network auth bypass."""

    app = Flask(
        __name__,
        template_folder=str(_TEMPLATE_DIR),
        static_folder=str(_STATIC_DIR),
    )
    # Stable, persisted secret key (0600) so signed sessions survive restarts.
    app.secret_key = load_or_create_secret_key()
    tls_enabled = bool(os.environ.get("CC_WEB_CERT") and os.environ.get("CC_WEB_KEY"))
    # SESSION_COOKIE_SECURE marks the session cookie Secure so it never rides a plaintext hop. It
    # is auto-set when THIS process terminates TLS (local cert+key). Behind a TLS-terminating
    # reverse proxy the app speaks plain HTTP locally, so the auto-detect wrongly leaves it OFF.
    # An operator can set CC_WEB_COOKIE_SECURE=1 to force it on for that deployment (an env var,
    # never a client-forgeable header, so no spoofable downgrade). =0 forces it off (bare-HTTP
    # LAN/testing); unset falls back to the local-TLS auto-detect.
    _cookie_secure_env = os.environ.get("CC_WEB_COOKIE_SECURE")
    cookie_secure = tls_enabled if _cookie_secure_env is None else _cookie_secure_env == "1"
    app.config.update(
        MAX_CONTENT_LENGTH=_MAX_CONTENT_LENGTH,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=cookie_secure,
        JSON_SORT_KEYS=False,
    )

    # Explicit CORS allowlist — NEVER '*'. Empty list => same-origin only.
    origins = allowed_origins if allowed_origins is not None else []
    socketio = SocketIO(app, async_mode="threading", cors_allowed_origins=origins)
    profiles = _load_profiles()

    creds, _generated = resolve_web_credentials(log)
    login_limiter = RateLimiter(max_events=8, window_seconds=60.0)
    cmd_limiter = RateLimiter(max_events=60, window_seconds=10.0)

    # Host-shell envelope (C2). Decided ONCE, here, from the bind's loopback-ness + the env opt-ins. When it
    # is not enabled the host_shell_* socket handlers below are never even DEFINED (not merely refused), and
    # the /api/host-shell probe reports the honest reason so the UI hides the Local tab. RCE by nature, so it
    # stays off unless CC_WEB_HOST_SHELL=1 AND the server is loopback AND not LAN-exposed (CC_WEB_ALLOW_LAN).
    host_shell_enabled, host_shell_reason = host_shell.availability_from_env(is_loopback=host_shell_loopback)
    if host_shell_enabled:
        log.info("Host shell ENABLED (loopback, opt-in) — a console onto this machine is exposed on the UI")

    # W1.1(web): the key-free wireless-node manager. Same UI-agnostic controller the Qt/tk tabs use; its
    # default vault getter reads the gate-sealed vault, so the keys never reach this process' request path.
    # Injectable so tests can drive locked/unlocked states without a real gate.
    nodes = nodes_controller if nodes_controller is not None else NodesController(device_manager)

    # One recorder for the process so its one-playback-at-a-time guard spans requests (a fresh
    # instance per request would let concurrent /api/macros/run calls both play). Injectable for tests;
    # defaults to the real MacroRecorder. Used by the macros list + run routes below.
    if macro_recorder is None:
        from src.core.macro_recorder import MacroRecorder

        macro_recorder = MacroRecorder()

    # Tail/follower detection (B11): a persistence scorer fed from the shared pool's BLE + client
    # discoveries (APs excluded — a stationary AP isn't a tail). Injectable for tests. Read via
    # /api/tails; it stays empty until a personal device reappears across time windows (awareness-
    # only, never acts). Wall-clock-free — the caller passes time.time().
    if tail_tracker is None:
        from src.core.tail_detect import PersistenceTracker

        tail_tracker = PersistenceTracker()

    # L-2: the web remote drives attack hardware; auth/flash/serial events must be auditable.
    # The normal launch path threads a durable AuditTrail through, but an embedder using the
    # create_app default would silently get no audit — warn so that's never a silent gap.
    if audit is None:
        log.warning(
            "Web remote created without an audit sink — auth/flash/serial events will NOT be "
            "recorded. Pass audit=AuditTrail(persist_path=...) for a durable forensic trail."
        )

    # ── Helpers ─────────────────────────────────────────────────────

    # Reverse-proxy IPs whose X-Forwarded-For we trust. EMPTY by default (no proxy trusted) so
    # remote_addr is used verbatim — the SAME behavior as before. Blindly trusting XFF would be a
    # regression: any client could send X-Forwarded-For: <anything> to get a fresh rate-limit bucket
    # (defeating the login/cmd limiter) and forge its audit identity. Only when the DIRECT peer is a
    # configured trusted proxy do we consult XFF (SEC-W2). Sourced from CC_WEB_TRUSTED_PROXIES.
    trusted_proxy_ips = frozenset(p.strip() for p in (trusted_proxies or ()) if p.strip())

    def _client_ip() -> str:
        return _resolve_client_ip(
            request.remote_addr, request.headers.get("X-Forwarded-For", ""), trusted_proxy_ips
        )

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
                # _json_body() coerces a non-dict body (e.g. `[1]`) to {} so `.get("_csrf")` returns
                # None -> a clean 403, instead of AttributeError -> an ungraceful 500.
                token = _json_body().get("_csrf")
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
        # Feed tail/follower detection from personal/mobile discoveries only (BLE + client stations);
        # a stationary AP is never a tail. Awareness-only — observe() just records a sighting.
        try:
            ttype = str(payload.get("target_type", ""))
            if ttype in ("ble", "client"):
                mac = str(payload.get("mac", ""))
                if mac:
                    label = payload.get("ssid") or payload.get("vendor") or mac
                    tail_tracker.observe(f"{ttype}:{mac}", time.time(), label=str(label))
        except Exception:
            log.debug("tail observe skipped", exc_info=True)

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

    def _gauge_ctx() -> dict[str, Any]:
        """Server-render seed for the reform Dashboard's System Health gauges. The reform.js poller
        refreshes the same fields every 5s from /api/system-health; this is just the first paint so
        the page is never momentarily blank."""
        from src.core.health_monitor import HealthMonitor

        def _color(v: float | None, invert: bool = False) -> str:
            # High is bad for CPU/RAM/disk; for battery it's inverted (a full battery is good).
            if v is None:
                return "var(--dim)"
            bad = (100 - v) if invert else v
            if bad < 60:
                return "var(--green)"
            if bad < 85:
                return "var(--yellow)"
            return "var(--red)"

        s = HealthMonitor.get_system_health()
        cpu, ram, disk = s["cpu_percent"], s["memory_percent"], s["disk_percent"]
        batt = s.get("battery_percent")
        return {
            "cpu_v": round(cpu), "cpu_c": _color(cpu), "cpu_n": round(cpu), "cpu_d": f"{cpu:.1f}%",
            "ram_v": round(ram), "ram_c": _color(ram), "ram_n": round(ram),
            "ram_d": f"{s['memory_used_mb'] / 1024:.1f}/{round(s['memory_total_mb'] / 1024)} GB",
            "disk_v": round(disk), "disk_c": _color(disk), "disk_n": round(disk),
            "disk_d": f"{s['disk_used_gb']}/{s['disk_total_gb']} GB",
            "batt_v": round(batt) if batt is not None else 0,
            "batt_c": _color(batt, invert=True) if batt is not None else "var(--dim)",
            "batt_n": round(batt) if batt is not None else "--",
            "batt_d": f"{round(batt)}%" if batt is not None else "no battery",
            "gps": s.get("gps_fix", False),
        }

    def _selected_caps(dev: Any) -> list[str]:
        """The connected device's self-reported runtime capabilities, upper-cased for the cap chips
        (WIFI/BLE/GPS/SD…). Empty until the firmware speaks a device_info — honest, not invented."""
        if dev is None:
            return []
        caps = getattr(dev, "runtime_capabilities", None) or ()
        return sorted(str(c).upper() for c in caps)

    def _selected_detail(dev: Any) -> str:
        """One identity/telemetry line for the Selected Device card — board/chip · fw · ui · ops ·
        heap, present keys only. Mirrors the Qt Operate tab's formatter so both read identically."""
        if dev is None:
            return ""
        t = getattr(dev, "telemetry", {}) or {}
        parts: list[str] = []
        ident = "/".join(str(t[k]) for k in ("board", "chip") if t.get(k))
        if not ident:
            ident = getattr(dev, "detected_chip", "") or ""
        if ident:
            parts.append(ident)
        if t.get("fw"):
            parts.append(f"fw {t['fw']}")
        if t.get("ui"):
            parts.append(f"ui {t['ui']}")
        ops = t.get("ops")
        if isinstance(ops, dict):
            parts.append(
                f"ops {ops.get('ready', 0)}/{ops.get('planned', 0)}/"
                f"{ops.get('attachable_unavailable', 0)}"
            )
        heap = t.get("heap")
        if isinstance(heap, int):
            parts.append(f"heap {heap // 1024} KB")
        return "  ·  ".join(parts)

    # Single-use bootstrap holder for the loopback desktop shell (see create_app docstring). A list
    # so the route can null it after one use; None (LAN web) keeps the route inert.
    _desktop_token = [desktop_token]

    @app.route("/desktop-auth")
    def desktop_auth():
        want = _desktop_token[0]
        if not want:
            abort(404)  # inert for the LAN web server — never a network auth bypass
        got = str(request.args.get("token", ""))
        if not (got and secrets.compare_digest(got, want)):
            _audit("desktop_auth_fail")
            abort(403)
        _desktop_token[0] = None  # consume: one navigation only
        session.clear()
        session["authenticated"] = True
        session["user"] = "cc-desktop"
        session["csrf"] = new_csrf_token()
        physical_key.record_successful_unlock()
        _audit("desktop_auth_ok")
        return redirect("/reform")

    @app.route("/reform")
    @requires_auth
    def reform_page():
        # Ace's approved mockup, served from CC's own core (GUI-stack pivot, Phase-1 proof). The
        # DEVICE Dashboard is wired to live data here; the other surfaces render the mockup as a
        # design preview and get the same wiring in the Phase-2 port.
        devices = _devices_for_display()
        selected = next((d for d in devices if d.connected), None)
        return render_template(
            "reform.html",
            devices=devices,
            device_count=len(devices),
            selected=selected,
            sel_caps=_selected_caps(selected),
            sel_detail=_selected_detail(selected),
            targets=[t.to_dict() for t in target_pool.all()],
            target_count=target_pool.count,
            sys=_gauge_ctx(),
            flash_profiles=list(profiles.keys()),
        )

    @app.route("/api/system-health")
    @requires_auth
    def api_system_health():
        from src.core.health_monitor import HealthMonitor

        return jsonify(HealthMonitor.get_system_health())

    @app.route("/api/flock")
    @requires_auth
    def api_flock():
        """USER-INITIATED ALPR-camera awareness import for a bbox (this request IS the user action
        the core gates on). Awareness-only OSINT from OpenStreetMap via the fixed-host Overpass API
        (no SSRF surface, drives no device, transmits nothing). GET ?bbox=S,W,N,E (decimal deg)."""
        from src.core import flock_osm

        parts = str(request.args.get("bbox", "")).split(",")
        if len(parts) != 4:
            return jsonify({"error": "bbox must be S,W,N,E (four decimal degrees)"}), 400
        try:
            bbox = tuple(float(p) for p in parts)
        except ValueError:
            return jsonify({"error": "bbox values must be numbers"}), 400
        try:
            geojson = flock_osm.fetch_alpr_geojson(bbox)
        except ValueError as exc:  # invalid bbox range from the query builder — clean 400
            return jsonify({"error": str(exc)}), 400
        except Exception:
            log.exception("flock ALPR fetch failed")
            return jsonify({"error": "Overpass fetch failed (offline or rate-limited)"}), 502
        cams = []
        for f in geojson.get("features", []):
            geo = f.get("geometry") or {}
            coords = geo.get("coordinates") or []
            if len(coords) == 2:
                props = f.get("properties") or {}
                cams.append({"lat": coords[1], "lon": coords[0], "label": props.get("ssid", "")})
        return jsonify(
            {"count": len(cams), "cameras": cams, "attribution": flock_osm.ODBL_ATTRIBUTION}
        )

    @app.route("/api/nodes-status")
    @requires_auth
    def api_nodes_status():
        """Provisioned-node status for the reform Mesh card. Fails CLOSED: a locked/unreadable vault
        returns unlocked=false with no rows, and list_rows() is already key-redacted, so no key byte
        can reach the response on any path."""
        try:
            unlocked = nodes.is_unlocked()
            rows = nodes.list_rows() if unlocked else []
            gateways = nodes.available_gateways() if unlocked else []
        except Exception:
            log.exception("nodes-status read failed")
            unlocked, rows, gateways = False, [], []
        return jsonify({"unlocked": unlocked, "rows": rows, "gateways": gateways})

    @app.route("/api/crack-tools")
    @requires_auth
    def api_crack_tools():
        """Read-only crack-engine availability for the CRACK card (native always works; hashcat +
        aircrack are optional accelerators). Detection only — running a crack stays behind its
        per-run consent gate and is NOT exposed here."""
        from src.core.crack_pipeline import available_backends, detect_tools
        from src.core.tool_installer import (
            default_tools_dir, installable_tools, tool_availability)
        from src.core import defender, tool_bundle

        tools = detect_tools()
        enable_dir = tool_bundle.enable_dir()
        return jsonify({
            "backends": available_backends(tools),
            "tools": [
                {"name": t.name, "present": t.present, "version": t.version}
                for t in tools.values()
            ],
            # Per-tool status for the "Get tools" panel: present?/where-from/can CC fetch it/how-to.
            "availability": [
                {"tool": a.tool, "present": a.present, "source": a.source,
                 "can_autofetch": a.can_autofetch, "guidance": a.guidance}
                for a in tool_availability()
            ],
            # Bundled encrypted packs — the DESIGN path (offline, no unreliable vendor fetch). Enable
            # unpacks one into the tools dir; on Windows that dir needs a one-time Defender exclusion
            # (below) or the extracted PUA binary is re-quarantined.
            "packs": [
                {"tool": p.tool, "version": p.version, "name": p.name, "platform": p.platform}
                for p in tool_bundle.list_packs()
            ],
            "defender": {
                "is_windows": defender.is_windows(),
                "pua_on": defender.pua_protection_on() is not False,
                "enable_dir": enable_dir,
                "exclusion_command": defender.exclusion_command(enable_dir),
            },
            "installable": installable_tools(),
            "tools_dir": default_tools_dir(),
        })

    @app.route("/api/crack/install-tool", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_crack_install_tool():
        """Auto-fetch ONE optional crack accelerator into CC's tools dir (today: aircrack-ng on Windows).
        Download + integrity-verify + extract + launch-probe, fail-closed — anything CC can't safely
        auto-install is refused here with the honest guidance instead. This grants NO authorization to
        crack: a crack RUN keeps its own separate per-run consent gate, never bypassed by installing a tool."""
        from src.core.tool_installer import install_tool, installable_tools, spec_for

        data = _json_body()
        tool = str(data.get("tool", "")).strip()
        if tool not in installable_tools():
            return jsonify({"error": f"{tool or 'that tool'} can't be auto-installed on this system — "
                            "see the guidance next to it"}), 400
        spec = spec_for(tool)
        if spec is None:
            return jsonify({"error": f"no install spec for {tool}"}), 400
        _audit("crack_install_tool", user=session.get("user"), tool=tool)
        log_lines: list[str] = []
        try:
            exe = install_tool(spec, on_line=lambda line: log_lines.append(str(line)))
        except Exception as exc:  # noqa: BLE001 — surface the honest failure to the panel, install nothing
            return jsonify({"ok": False, "tool": tool, "error": str(exc), "log": log_lines}), 502
        return jsonify({"ok": True, "tool": tool, "path": exe, "log": log_lines})

    @app.route("/api/crack/enable-bundled", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_crack_enable_bundled():
        """Enable a crack engine from its BUNDLED encrypted pack — offline, no network. This is the design
        path: the vendor host is unreliable, and Defender deletes the raw binary at rest, so CC ships it
        encrypted and unpacks it only into the user's Defender-excluded tools folder. Grants NO
        authorization to crack; a RUN keeps its own separate per-run consent gate."""
        from src.core import tool_bundle
        data = _json_body()
        name = str(data.get("pack") or data.get("tool", "")).strip()
        pack = next((p for p in tool_bundle.list_packs()
                     if p.name == name or p.tool == name), None)
        if pack is None:
            return jsonify({"ok": False, "error": f"no bundled pack for {name or 'that tool'}"}), 400
        _audit("crack_enable_bundled", user=session.get("user"), tool=pack.tool)
        log_lines: list[str] = []
        ok, msg = tool_bundle.enable_bundled(pack, on_line=lambda line: log_lines.append(str(line)))
        # 200 even on a non-fatal enable failure (e.g. Defender quarantine): the honest message is the
        # point of the panel, so the UI shows it via .then rather than losing it in a reject.
        return jsonify({"ok": ok, "tool": pack.tool, "message": msg, "log": log_lines})

    @app.route("/api/crack/defender-exclusion", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_crack_defender_exclusion():
        """One-click: add a Windows Defender folder exclusion for CC's tools dir via an elevated (UAC)
        PowerShell, so an enabled PUA engine isn't re-quarantined. The user sees and can decline the UAC
        prompt; the exact command is also returned so they can run it manually instead."""
        from src.core import defender, tool_bundle
        enable_dir = tool_bundle.enable_dir()
        if not defender.is_windows():
            return jsonify({"ok": False, "error": "Defender exclusions are Windows-only",
                            "dir": enable_dir}), 400
        _audit("crack_defender_exclusion", user=session.get("user"))
        ok = defender.add_exclusion_elevated(enable_dir)
        return jsonify({"ok": bool(ok), "dir": enable_dir,
                        "command": defender.exclusion_command(enable_dir)})

    @app.route("/api/wordlists")
    @requires_auth
    def api_wordlists():
        """Wordlist inventory for the CRACK card: the bundled offline WPA core + whatever's installed on
        disk + the downloadable catalog (each flagged installed?). Read-only — downloading a list is the
        separate POST below, and a crack RUN keeps its own consent gate regardless of what's installed."""
        from src.core import wordlist_manager as wl
        return jsonify({
            "bundled": wl.bundled_wordlists(),
            "installed": wl.scan_installed(),
            "catalog": [
                {"id": s.id, "name": s.name, "description": s.description, "category": s.category,
                 "size_human": wl.format_size(s.size_bytes), "installed": wl.is_installed(s)}
                for s in wl.catalog()
            ],
            "dir": wl.default_wordlist_dir(),
        })

    @app.route("/api/wordlists/download", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_wordlists_download():
        """Download ONE catalog wordlist by id into the wordlist dir (verify + install, fail-closed). The
        id must be in the curated catalog; large lists (rockyou ~134 MiB) stream with a size cap. Opt-in —
        nothing downloads without this explicit call."""
        from src.core import wordlist_manager as wl
        data = _json_body()
        wid = str(data.get("id", "")).strip()
        spec = wl.spec_by_id(wid)
        if spec is None:
            return jsonify({"error": f"unknown wordlist id: {wid or '(none)'}"}), 400
        _audit("wordlist_download", user=session.get("user"), wordlist=wid)
        log_lines: list[str] = []
        try:
            path = wl.download_wordlist(spec, on_line=lambda line: log_lines.append(str(line)))
        except Exception as exc:  # noqa: BLE001 — surface the honest failure, install nothing
            return jsonify({"ok": False, "id": wid, "error": str(exc), "log": log_lines}), 502
        return jsonify({"ok": True, "id": wid, "path": path, "log": log_lines})

    @app.route("/api/wordlists/byo", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_wordlists_byo():
        """Register a bring-your-own wordlist by path — validated + read in place (no copy, no network).
        The path is only ever passed to validate_wordlist; the response echoes it back, never file bytes."""
        from src.core import wordlist_manager as wl
        data = _json_body()
        path = str(data.get("path", "")).strip()
        if not path:
            return jsonify({"error": "path is required"}), 400
        try:
            resolved = wl.register_byo(path)
        except Exception as exc:  # noqa: BLE001 — the validation message (missing/empty file) is safe to show
            return jsonify({"ok": False, "error": str(exc)}), 400
        _audit("wordlist_byo", user=session.get("user"))
        return jsonify({"ok": True, "path": resolved})

    @app.route("/api/captures")
    @requires_auth
    def api_captures():
        """Captured handshakes/PMKIDs for the CRACK card, from the shared CaptureStore (auto-logged as the
        connected devices capture them). Exposes ONLY display fields — never the raw pcap path, the crackable
        hc22000 hashline, or the raw serial line. The recovered ``password`` is the operator's own result and
        is shown on this loopback UI only once a capture has actually been cracked."""
        if capture_store is None:
            return jsonify({"captures": []})

        def _hhmm(dt: Any) -> str:
            try:
                return dt.strftime("%H:%M")
            except Exception:  # noqa: BLE001 — a bad/absent timestamp just renders blank
                return ""

        from src.core import crack_pipeline
        out = []
        for c in capture_store.all():
            # "crackable from here" = a retrieved local .pcap OR an inline PMKID line; a capture still on
            # a device's SD (path we can't read) can't be run yet, and the UI shouldn't pretend otherwise.
            crackable = bool((c.pcap_path and os.path.isfile(c.pcap_path))
                             or crack_pipeline.hashline_from_capture(c))
            out.append({
                "key": c.key,
                "ssid": c.ssid or "",
                "bssid": c.bssid or "",
                "type": "PMKID" if c.capture_type == "pmkid" else "handshake",
                "source": c.device_source or "",
                "captured": _hhmm(c.captured_at),
                "crack_status": c.crack_status,
                "password": c.password if c.crack_status == "cracked" else "",
                "crackable": crackable,
            })
        return jsonify({"captures": out})

    @app.route("/api/captures/export")
    @requires_auth
    def api_captures_export():
        """Download the Captured Handshakes table as CSV. DISPLAY FIELDS ONLY — never the raw pcap
        path, the crackable hc22000 hashline, or the recovered password: an export file is portable
        and leaves the box, so no secret is baked in (stricter than /api/captures, which shows a
        cracked password on the loopback screen only). GET; mutates nothing."""
        # Route every string cell through the SAME formula-injection-safe field encoder the target/
        # wardrive CSV exporters use (OWASP CSV Injection): an attacker controls the SSID, so a
        # value like ``=HYPERLINK(...)`` must be neutralized before it opens in a spreadsheet.
        # _csv_field also does comma/quote/newline quoting, so rows are built by join (not writer)
        # to avoid double-quoting — mirrors src/core/target_export.py.
        from src.core.wardrive import _csv_field

        def _hhmm(dt: Any) -> str:
            try:
                return dt.strftime("%Y-%m-%d %H:%M")
            except Exception:  # noqa: BLE001 — a bad/absent timestamp just renders blank
                return ""

        lines = ["ssid,bssid,type,source,captured,crack_status"]
        if capture_store is not None:
            for c in capture_store.all():
                lines.append(",".join([
                    _csv_field(c.ssid), _csv_field(c.bssid),
                    "PMKID" if c.capture_type == "pmkid" else "handshake",
                    _csv_field(c.device_source), _csv_field(_hhmm(c.captured_at)),
                    _csv_field(c.crack_status),
                ]))
        return Response(
            "\n".join(lines) + "\n",
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=cc-captures.csv"},
        )

    # One crack at a time. The flag is read by the worker's should_stop and guarded by the lock; a UI can
    # only ever have one run in flight (409 otherwise), so we don't track per-run identity.
    _crack_run = {"busy": False, "stop": False, "proc": None}
    _crack_lock = threading.Lock()

    @app.route("/api/crack/run", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_crack_run():
        """Launch a consent-gated dictionary crack of a captured handshake, streamed to the CRACK log over
        the `crack_log` / `crack_done` socket events. The per-run authorized-use consent is RE-CHECKED here
        server-side — the UI checkbox only gates the button; a run whose body lacks `consent: true` is
        refused (403). Dictionary-only, native engine (no external tool), one run at a time. safety.py is
        untouched; this recovers a key the operator affirms they own or are authorized to test."""
        from src.core import crack_pipeline
        if not cmd_limiter.allow(_client_ip()):
            return jsonify({"error": "rate limited"}), 429
        data = _json_body()
        if data.get("consent") is not True:
            return jsonify({"error": "per-run authorized-use consent is required"}), 403
        if capture_store is None:
            return jsonify({"error": "no capture store"}), 400
        key = str(data.get("capture_key", "")).strip()
        rec = capture_store.get(key)
        if rec is None:
            return jsonify({"error": "select a captured handshake first"}), 400
        wordlist = str(data.get("wordlist", "")).strip()
        try:
            crack_pipeline.validate_wordlist(wordlist)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        # Materialize a crackable input: a retrieved local .pcap, else the inline hc22000 (PMKID) line.
        tmp_hc = ""
        if rec.pcap_path and os.path.isfile(rec.pcap_path):
            capture_path = rec.pcap_path
        else:
            hashline = crack_pipeline.hashline_from_capture(rec)
            if not hashline:
                return jsonify({"error": "this capture has no local .pcap and no crackable inline PMKID — "
                                "retrieve its capture file first"}), 400
            fd, tmp_hc = tempfile.mkstemp(prefix="cc-crack-", suffix=crack_pipeline.HASHFILE_EXT)
            os.close(fd)
            crack_pipeline.write_hc22000(hashline, tmp_hc)
            capture_path = tmp_hc

        with _crack_lock:
            if _crack_run["busy"]:
                if tmp_hc:
                    try:
                        os.remove(tmp_hc)
                    except OSError:
                        pass
                return jsonify({"error": "a crack is already running"}), 409
            _crack_run["busy"] = True
            _crack_run["stop"] = False
        engine = str(data.get("engine", "native")).strip().lower() or "native"
        _audit("crack_run", user=session.get("user"), ssid=rec.ssid, bssid=rec.bssid)

        def _emit(line: Any) -> None:
            socketio.emit("crack_log", {"line": str(line)})

        def _register_proc(proc: Any) -> None:
            # Hold the external tool's process so /api/crack/stop can kill it (native cancels via should_stop).
            with _crack_lock:
                _crack_run["proc"] = proc

        def _worker() -> None:
            tmp_hash = ""
            try:
                _emit(f"[consent affirmed] {rec.ssid or rec.bssid} · {os.path.basename(wordlist)} · engine={engine}")
                # Dispatch on the chosen backend, mirroring the Classic Crack Lab tab. native = CC's pure-Python
                # cracker (cooperative stop); aircrack/hashcat are external engines (killed on stop via on_proc).
                if engine == "aircrack":
                    tools = crack_pipeline.detect_tools()
                    res = crack_pipeline.run_aircrack(
                        capture_path, wordlist, _emit, tools=tools, bssid=rec.bssid, on_proc=_register_proc)
                elif engine == "hashcat":
                    tools = crack_pipeline.detect_tools()
                    if os.path.splitext(capture_path)[1].lower() == crack_pipeline.HASHFILE_EXT:
                        hash_file = capture_path
                        res = None
                    else:
                        fd, hash_file = tempfile.mkstemp(prefix="cc-crack-", suffix=crack_pipeline.HASHFILE_EXT)
                        os.close(fd)
                        tmp_hash = hash_file
                        n = crack_pipeline.convert_capture(capture_path, hash_file, _emit, tools=tools,
                                                           on_proc=_register_proc)
                        _emit(f"[convert] {n} crackable hash(es) extracted.")
                        if n == 0:
                            hash_file = None
                            res = crack_pipeline.CrackResult(
                                cracked=False, detail="no PMKID or handshake found in this capture")
                        else:
                            res = None
                    if hash_file is not None:
                        res = crack_pipeline.run_hashcat(hash_file, wordlist, _emit, tools=tools,
                                                         on_proc=_register_proc)
                else:  # native (default)
                    res = crack_pipeline.run_native(
                        capture_path, wordlist, _emit, bssid=rec.bssid,
                        should_stop=lambda: _crack_run["stop"])
                if res.cracked:
                    capture_store.mark_cracked(key, res.password, res.detail, wordlist)
                    _emit(f"RECOVERED -> {res.ssid or rec.ssid}: {res.password}")
                    socketio.emit("crack_done", {"cracked": True, "key": key,
                                                 "ssid": res.ssid or rec.ssid, "password": res.password,
                                                 "detail": res.detail})
                else:
                    _emit(f"[done] {res.detail or 'key not in wordlist'}")
                    socketio.emit("crack_done", {"cracked": False, "key": key, "detail": res.detail})
            except Exception as exc:  # noqa: BLE001 — surface honestly to the log, never fake a result
                _emit(f"[error] {exc}")
                socketio.emit("crack_done", {"cracked": False, "key": key, "detail": str(exc)})
            finally:
                for _t in (tmp_hc, tmp_hash):
                    if _t:
                        try:
                            os.remove(_t)
                        except OSError:
                            pass
                with _crack_lock:
                    _crack_run["busy"] = False
                    _crack_run["proc"] = None

        socketio.start_background_task(_worker)
        return jsonify({"started": True}), 202

    @app.route("/api/crack/stop", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_crack_stop():
        """Cancel the in-flight crack: the native engine polls should_stop; an external engine
        (aircrack/hashcat) is killed via the process handle registered during the run."""
        with _crack_lock:
            _crack_run["stop"] = True
            proc = _crack_run.get("proc")
        if proc is not None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001 — best-effort kill; the worker's finally still cleans up
                pass
        return jsonify({"stopping": True})

    @app.route("/api/gate-status")
    @requires_auth
    def api_gate_status():
        """Read-only access-gate status for the reform SETTINGS card. Returns ONLY booleans/policy —
        never a password, key, or verifier byte."""
        try:
            cfg = physical_key.load_config()
            lock = physical_key.lockout_status()
            return jsonify({
                "configured": bool(cfg.get("password") or cfg.get("key")),
                "policy": cfg.get("policy", "either"),
                "has_password": bool(cfg.get("password")),
                "has_key": bool(cfg.get("key")),
                "locked": bool(lock.get("locked")),
                "remaining_secs": lock.get("remaining_secs", 0),
            })
        except Exception:
            log.exception("gate-status read failed")
            return jsonify({"configured": False, "policy": "either", "has_password": False,
                            "has_key": False, "locked": False, "remaining_secs": 0})

    # ── SETTINGS (B17): live read + owner write-back to the real settings store ──
    # The reform SETTINGS surface reads and writes ~/.cyber-controller/settings.json via the same
    # src.config.settings store the desktop app uses (deep-merge + atomic 0600 write). Secrets never
    # cross the wire in the clear: the WiGLE token is exposed ONLY as a "set" boolean, and a write
    # skips the token unless the client sends a real new value (not the masked placeholder).
    _SETTINGS_BAUDS = (9600, 115200, 230400, 460800, 921600)
    _TOUCH_MODES = ("auto", "on", "off")
    _FLASH_MODES = ("dio", "qio", "dout", "qout")

    def _settings_public(s: dict) -> dict:
        """A secret-free projection of the settings the reform surface shows."""
        return {
            "serial": {"default_baud": s["serial"]["default_baud"]},
            "flash": {
                "flash_baud": s["flash"]["flash_baud"],
                "verify": s["flash"]["verify"],
                "auto_backup": s["flash"]["auto_backup"],
                "mode": s["flash"]["mode"],
            },
            "interface": {"touch_mode": s["interface"]["touch_mode"]},
            "updates": {"enabled": s["updates"]["enabled"]},
            "safety": {
                "confirm_dangerous": s["safety"]["confirm_dangerous"],
                "suppress_all_warnings": s["safety"]["suppress_all_warnings"],
            },
            "security": {"secure_container": s["security"]["secure_container"]},
            "vault": {"dir": s["vault"]["dir"]},
            # secret → boolean only; the token value itself never leaves the host.
            "uploads": {"wigle_token_set": bool(s["uploads"]["wigle_token"])},
        }

    @app.route("/api/settings")
    @requires_auth
    def api_settings_get():
        """Live settings for the reform SETTINGS surface (secret-free). GET only, no mutation."""
        return jsonify({"ok": True, "settings": _settings_public(app_settings.load_settings())})

    @app.route("/api/settings", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_settings_post():
        """Owner write-back for the reform SETTINGS surface (B17). Whitelists + validates each
        field, merges onto the current settings, then persists atomically. ``{"reset": true}``
        restores defaults. Out-of-range values 400 (nothing saved); unknown keys are not applied.
        These are the owner's own preferences — the safety toggles set confirm-friction, they do NOT
        weaken safety.py's command classification (which stays label/warn-only, never blocked)."""
        data = _json_body()
        if data.get("reset") is True:
            app_settings.save_settings(dict(app_settings.DEFAULTS))
            _audit("web_settings_reset")
            return jsonify({"ok": True, "reset": True,
                            "settings": _settings_public(app_settings.load_settings())})

        s = app_settings.load_settings()
        errors: list[str] = []

        def _sec(name: str) -> dict:
            v = data.get(name)
            return v if isinstance(v, dict) else {}

        def _apply_int(section: str, key: str, allowed) -> None:
            v = _sec(section).get(key)
            if v is None:
                return
            try:
                iv = int(v)
            except (TypeError, ValueError):
                errors.append(f"{section}.{key}")
                return
            if iv not in allowed:
                errors.append(f"{section}.{key}")
                return
            s[section][key] = iv

        def _apply_choice(section: str, key: str, allowed) -> None:
            v = _sec(section).get(key)
            if v is None:
                return
            if v not in allowed:
                errors.append(f"{section}.{key}")
                return
            s[section][key] = v

        def _apply_bool(section: str, key: str) -> None:
            v = _sec(section).get(key)
            if v is not None:
                s[section][key] = bool(v)

        _apply_int("serial", "default_baud", _SETTINGS_BAUDS)
        _apply_int("flash", "flash_baud", _SETTINGS_BAUDS)
        _apply_choice("flash", "mode", _FLASH_MODES)
        _apply_bool("flash", "verify")
        _apply_bool("flash", "auto_backup")
        _apply_choice("interface", "touch_mode", _TOUCH_MODES)
        _apply_bool("updates", "enabled")
        _apply_bool("safety", "confirm_dangerous")
        _apply_bool("safety", "suppress_all_warnings")
        _apply_bool("security", "secure_container")

        vdir = _sec("vault").get("dir")
        if vdir is not None:
            if isinstance(vdir, str) and vdir.strip():
                s["vault"]["dir"] = vdir.strip()
            else:
                errors.append("vault.dir")

        tok = _sec("uploads").get("wigle_token")
        if isinstance(tok, str):
            stripped = tok.strip()
            # A body of only bullet/star chars is the masked placeholder — keep the stored token.
            if stripped and set(stripped) <= {"•", "*", "·"}:
                pass
            else:
                s["uploads"]["wigle_token"] = stripped

        if errors:
            return jsonify({"ok": False, "errors": errors}), 400

        try:
            app_settings.save_settings(s)
        except Exception:
            log.exception("settings write failed")
            return jsonify({"ok": False, "errors": ["write_failed"]}), 500
        _audit("web_settings_saved")
        return jsonify({"ok": True, "settings": _settings_public(app_settings.load_settings())})

    @app.route("/api/version")
    @requires_auth
    def api_version():
        """Running build version for the reform SETTINGS ▸ Updates card. GET only, no network."""
        from src.version import __version__
        return jsonify({"version": __version__})

    @app.route("/api/updates/check", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_updates_check():
        """Manual 'Check now' for the Updates card — runs the same network check the desktop app
        uses (updater.check, SSRF-guarded to GitHub). Deep-link only: returns the latest tag + URL,
        never self-downloads. Records last_check_iso/last_seen_latest so the state stays honest."""
        from src.core import updater
        from src.version import __version__
        s = app_settings.load_settings()
        try:
            res = updater.check(__version__, s.get("updates"))
        except Exception:
            log.exception("update check failed")
            return jsonify({"ok": False, "status": "OFFLINE"}), 200
        upd = s.setdefault("updates", {})
        upd["last_check_iso"] = updater.now_iso()
        if res.latest_tag:
            upd["last_seen_latest"] = res.latest_tag
        try:
            app_settings.save_settings(s)
        except Exception:
            log.debug("could not persist update-check bookkeeping", exc_info=True)
        return jsonify({
            "ok": True,
            "status": res.status,           # UP_TO_DATE | NEWER | OFFLINE
            "current": __version__,
            "latest_tag": res.latest_tag,
            "latest_url": updater.apply_update_url(res) if res.status == "NEWER" else "",
            "behind": res.behind,
        })

    @app.route("/api/sensing")
    @requires_auth
    def api_sensing():
        """WS1 Wi-Fi CSI sensing rollup for a future Sense view: per-node presence/motion/confidence
        + a room-occupied summary, from the shared SensingModel (fed by a connected sensing node).
        Read-only, passive — CC authors no RF and a sensed person is NEVER a scan target. Reports
        only the PROVEN tier (presence + motion) on commodity 2.4 GHz Wi-Fi CSI."""
        import time as _time

        empty = {"total": 0, "fresh": 0, "occupied": 0, "any_occupied": False}
        if sensing_model is None:
            return jsonify({"supported": True, "summary": empty, "nodes": []})
        now = _time.monotonic()
        rows = sensing_model.nodes(now=now)
        return jsonify({
            "supported": True,
            "summary": sensing_model.summary(now),
            "nodes": [
                {"node_id": n.node_id, "presence": n.presence, "motion": round(n.motion, 3),
                 "confidence": round(n.confidence, 3), "tier": n.tier,
                 "occupied": n.occupied(now), "fresh": n.is_fresh(now),
                 "freshness": round(n.freshness(now), 3), "verdicts": n.verdicts}
                for n in rows
            ],
        })

    @app.route("/api/host-shell")
    @requires_auth
    def api_host_shell():
        """Whether the host-shell console is available on this bind, so the reform TERMINAL surface can show
        or hide its 'Local (host shell)' tab. Reports ONLY the enabled boolean + the honest reason — no host
        data. The actual shell lives behind the gated socket handlers, which don't exist unless enabled."""
        return jsonify({"enabled": host_shell_enabled, "reason": host_shell_reason})

    @app.route("/api/quick-commands")
    @requires_auth
    def api_quick_commands():
        """The connected device's one-tap command set for the reform OPERATE console, folded into the
        canonical Scanning/Attack/Network/Other buckets (A16). Commands come from the firmware's own
        protocol registry (no phantom verbs); each keeps its native category as a sub-label. Danger
        labels drive the client-side confirm (label-never-block). GET only, mutates nothing."""
        from src.core.quick_commands import canonical_grouped_quick_commands

        port = str(request.args.get("port", ""))
        dev = device_manager.get_device(port) if port else None
        firmware = getattr(dev, "firmware", "") if dev else str(request.args.get("firmware", ""))
        groups = [
            {
                "category": group,
                "commands": [
                    {"command": c.command, "label": c.label, "danger": c.danger,
                     "native": c.category}
                    for c in cmds
                ],
            }
            for group, cmds in canonical_grouped_quick_commands(firmware or "")
        ]
        return jsonify({"port": port, "firmware": firmware, "groups": groups})

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
        # Per-IP rate limit (shared with /api/command, per the module docstring). Flashing is the
        # most dangerous verb -- it can brick a board -- so an unbounded /api/flash was the worst
        # gap: a scripted caller could hammer it. Checked first, before any body parse or board op.
        if not cmd_limiter.allow(_client_ip()):
            return jsonify({"error": "rate limited"}), 429
        data = _json_body()
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
        # Honor an explicit board/variant pick from the caller (empty string -> per-chip default, i.e.
        # unchanged behavior). The engine's _resolve_variant() matches this against the release assets by
        # name/label and falls back to the default with a warning if it doesn't fit the detected chip.
        # Without this, a board whose default asset is built for a larger flash (e.g. Bruce's default is a
        # 16MB image) can only be flashed with that non-booting default from the web UI — the desktop UI
        # could already pass a variant. Kept symmetric with the engine's long-standing profile.variant.
        requested_variant = str(data.get("variant", "")).strip()
        if requested_variant:
            profile.variant = requested_variant
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

    @app.route("/api/variants")
    @requires_auth
    def api_variants():
        """List the selectable firmware variants for a profile so the flash page can offer a board
        picker. GET /api/variants?profile=<name> -> {"variants": [{"name","label","chip"}]}.

        A read-only companion to the beat-171 /api/flash `variant` field: the picker's chosen name is
        POSTed straight back as that field. Never 500s — list_variants() swallows an offline/API-error
        release fetch and returns [], so the picker just shows "Default (auto-detect)" and the flash
        falls to the per-chip default (unchanged behavior). GET, so no CSRF (it mutates nothing)."""
        profile_name = str(request.args.get("profile", ""))
        if not profile_name:
            return jsonify({"error": "profile is required"}), 400
        profile_path = profiles.get(profile_name)
        if not profile_path:
            return jsonify({"error": f"Unknown profile: {profile_name}"}), 404
        try:
            profile = flash_engine.load_profile(profile_path)
        except Exception as exc:  # noqa: BLE001 — malformed profile -> clean 400, not opaque 500
            return jsonify({"error": f"Invalid firmware profile ({profile_path.name}): {exc}"}), 400
        variants = [
            {"name": v.get("name", ""), "label": v.get("label", ""), "chip": v.get("chip", "")}
            for v in flash_engine.list_variants(profile)
            if v.get("name")
        ]
        return jsonify({"variants": variants})

    @app.route("/api/connect", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_connect():
        # Open a managed serial connection so the command surface (/api/command, subscribe_serial,
        # send_command) can actually reach the device. Without this route get_connection(port) is always
        # None — the web remote could never talk to a real board. Registers the scanned Device first (a
        # port present at startup is not yet in the registry), then opens the link.
        data = _json_body()
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
        data = _json_body()
        port = str(data.get("port", ""))
        if not port:
            return jsonify({"error": "port is required"}), 400
        if not _known_port(port):
            return jsonify({"error": f"Unknown/unregistered port: {port}"}), 400
        device_manager.close_connection(port, owner="web")
        _audit("device_disconnect", user=session.get("user"), port=port)
        return jsonify({"status": "disconnected", "port": port})

    def _command_is_offensive(command: str) -> bool:
        """The single offensive-verb floor shared by the auto-router rules gate AND the OPERATE
        Broadcast fan-out. classify() catches most offensive verbs, but some transmitting verbs
        (evilportal/startportal/subghz tx/rfid|nfc emulate/…) carry their danger in CommandInfo
        metadata and classify() returns '' for the bare string — so union with the _ATTACK_PREFIXES
        floor. ONE definition so the two gates can't drift apart (red-team finding, 2026-08-07)."""
        from src.core import safety
        from src.core.macro_recorder import _ATTACK_PREFIXES

        c = (command or "").strip().lower()
        return bool(safety.classify(command or "")) or \
            any(c.startswith(p) for p in _ATTACK_PREFIXES)

    @app.route("/api/command", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_command():
        if not cmd_limiter.allow(_client_ip()):
            return jsonify({"error": "rate limited"}), 429
        data = _json_body()
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

    @app.route("/api/broadcast", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_broadcast():
        """OPERATE Broadcast fan-out (A16 Broadcast half): send ONE command to MANY connected ports
        at once. Because fan-out AMPLIFIES an offensive verb across every selected device, an
        offensive command is refused unless the body carries consent:true (server-side re-check,
        same floor as the auto-router rules + macros). Recon/benign commands fan out freely, like
        the single-send /api/command. Each port is gated independently (busy/disconnected → per-port
        skip, not a whole-batch failure); safety.py is untouched — LABELS + gates, never blocks."""
        if not cmd_limiter.allow(_client_ip()):
            return jsonify({"error": "rate limited"}), 429
        data = _json_body()
        command = str(data.get("command", ""))
        ports = data.get("ports")
        consent = data.get("consent") is True

        if not command:
            return jsonify({"error": "command is required"}), 400
        if len(command) > _MAX_COMMAND_LEN:
            return jsonify({"error": "command too long"}), 400
        if not isinstance(ports, list) or not ports:
            return jsonify({"error": "ports must be a non-empty list"}), 400
        ports = [str(p) for p in ports]
        if len(ports) > 64:
            return jsonify({"error": "too many ports"}), 400

        offensive = _command_is_offensive(command)
        if offensive and not consent:
            return jsonify({
                "error": "This command transmits — confirm authorized use to broadcast it.",
            }), 403

        results = []
        sent = 0
        for port in ports:
            if not _known_port(port):
                results.append({"port": port, "error": "unknown/unregistered port"})
                continue
            if flash_engine.is_port_busy(port):
                results.append({"port": port, "error": "busy with a flash operation"})
                continue
            conn = device_manager.get_connection(port)
            if not conn or not conn.is_connected:
                results.append({"port": port, "error": "no active connection"})
                continue
            try:
                conn.write(command)  # SerialConnection.write rejects embedded control chars
                _audit("serial_broadcast", user=session.get("user"), port=port,
                       command=command, offensive=offensive)
                results.append({"port": port, "status": "sent"})
                sent += 1
            except ValueError as exc:
                results.append({"port": port, "error": str(exc)})
            except Exception:
                log.exception("broadcast command failed on %s", port)
                results.append({"port": port, "error": "internal error"})

        return jsonify({
            "command": command, "offensive": offensive,
            "sent": sent, "failed": len(ports) - sent, "results": results,
        })

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

    @app.route("/api/targets/clear", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_targets_clear():
        # Clear the shared target pool (the operator's own scan results; re-populated by scanning).
        # Not destructive to hardware; a client-side confirm guards accidental clicks.
        n = target_pool.clear()
        _audit("targets_clear", user=session.get("user"), count=n)
        return jsonify({"status": "cleared", "count": n})

    @app.route("/api/targets/export")
    @requires_auth
    def api_targets_export():
        # Download the live target pool as a WiGLE-style CSV (read-only; same rows shown in the UI).
        from src.core.target_export import targets_to_csv

        csv_text = targets_to_csv(target_pool.all())
        return Response(
            csv_text,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=cc-targets.csv"},
        )

    @app.route("/api/macros")
    @requires_auth
    def api_macros():
        """Saved macros for the OPERATE Macros card. Display metadata only — name/steps/protocol/
        secured; the filesystem PATH is redacted (never leak a server path to the client). Running a
        macro replays commands (possibly offensive) — see /api/macros/run for the gated playback."""
        from src.core.macro_recorder import is_offensive_macro

        try:
            rows = macro_recorder.list_saved_macros()
        except Exception:
            log.exception("macro listing failed")
            rows = []
        out = []
        for m in rows:
            offensive = False
            path = m.get("path")
            if path:  # classify from the real steps so the UI can warn + arm; file-based only
                try:
                    offensive = is_offensive_macro(macro_recorder.load_macro(path))
                except Exception:
                    offensive = False
            out.append({
                "name": m.get("name", ""),
                "step_count": m.get("step_count", 0),
                "protocol": m.get("protocol", ""),
                "secured": bool(m.get("secured", False)),
                "offensive": offensive,
            })
        return jsonify(out)

    @app.route("/api/macros/run", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_macros_run():
        # Play a saved macro on a CONNECTED device. Safety: the engine (macro_recorder.play)
        # HARD-refuses an offensive macro unless armed=True; we set armed only after a server-side
        # consent re-check (body consent:true, else 403 — parity with the crack RUN). Every step is
        # written through the guarded serial connection (SerialConnection.write classifies danger +
        # rejects control chars). safety.py is untouched. Recon macros play without consent.
        from src.core.macro_recorder import is_offensive_macro

        if not cmd_limiter.allow(_client_ip()):
            return jsonify({"error": "rate limited"}), 429
        data = _json_body()
        name = str(data.get("name", ""))
        port = str(data.get("port", ""))
        consent = data.get("consent") is True
        if not name or not port:
            return jsonify({"error": "name and port are required"}), 400
        if not _known_port(port):
            return jsonify({"error": f"Unknown/unregistered port: {port}"}), 400
        if flash_engine.is_port_busy(port):
            return jsonify({"error": f"Port {port} is busy with a flash operation"}), 409
        conn = device_manager.get_connection(port)
        if not conn or not conn.is_connected:
            return jsonify({"error": f"No active connection on {port}"}), 400
        match = next(
            (m for m in macro_recorder.list_saved_macros()
             if m.get("name") == name and m.get("path")),
            None,
        )
        if not match:
            return jsonify({"error": f"Unknown macro: {name}"}), 404
        try:
            macro = macro_recorder.load_macro(match["path"])
        except Exception:
            log.exception("macro load failed")
            return jsonify({"error": "Could not load that macro"}), 400
        offensive = is_offensive_macro(macro)
        if offensive and not consent:
            # The consent chip is the arm; without it an offensive (transmitting) macro is refused.
            return jsonify(
                {"error": "This macro transmits — confirm authorized use to run it."}
            ), 403

        _audit("macro_run", user=session.get("user"), macro=name, port=port, offensive=offensive)

        def _progress(idx: int, total: int, msg: str) -> None:
            socketio.emit(
                "macro_progress", {"macro": name, "step": idx, "total": total, "message": msg}
            )

        def _complete(ok: bool, msg: str) -> None:
            socketio.emit("macro_done", {"macro": name, "success": bool(ok), "message": msg})

        try:
            macro_recorder.play(
                macro,
                send_command=lambda c: conn.write(c),
                armed=(offensive and consent),
                progress_callback=_progress,
                complete_callback=_complete,
            )
        except Exception:
            log.exception("macro playback failed to start on %s", port)
            return jsonify({"error": "internal error starting playback"}), 500
        return jsonify({"status": "started", "macro": name, "offensive": offensive}), 202

    @app.route("/api/macros/stop", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_macros_stop():
        macro_recorder.stop_playback()
        return jsonify({"status": "stopping"})

    # ── Cross-Comm auto-routing rules (B14) — offensive-automation, doubly gated ──
    # A rule auto-fires its command on a matching target with NO human in the loop per shot. So an
    # offensive rule (safety.classify hits its command) is (1) refused unless the add carries
    # consent:true AND (2) forced to land DISABLED — never auto-fires on add. Enabling one later is
    # itself a consent-gated act (the arm). Recon rules add + enable freely. safety.py is untouched.
    def _rule_is_offensive(command: str) -> bool:
        # Delegates to the shared _command_is_offensive floor (defined near /api/command) so the rules
        # gate, the Broadcast fan-out, and the macro floor can't drift apart. (Red-team finding,
        # 2026-08-07: classify() alone misses metadata-danger transmitting verbs.)
        return _command_is_offensive(command)

    def _rule_to_dict(r: Any) -> dict:
        tt = r.target_type.value if getattr(r, "target_type", None) is not None else ""
        return {
            "name": r.name, "target_type": tt, "ssid_pattern": r.ssid_pattern,
            "min_rssi": r.min_rssi, "device_port": r.device_port,
            "command_template": r.command_template, "enabled": r.enabled,
            "offensive": _rule_is_offensive(r.command_template),
        }

    @app.route("/api/rules")
    @requires_auth
    def api_rules():
        if auto_router is None:
            return jsonify([])
        return jsonify([_rule_to_dict(r) for r in auto_router.list_rules()])

    @app.route("/api/rules", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_rules_add():
        if auto_router is None:
            return jsonify({"error": "auto-routing not available"}), 503
        from src.core.cross_comm import RoutingRule
        from src.models.target import TargetType

        data = _json_body()
        name = str(data.get("name", "")).strip()
        command = str(data.get("command_template", "")).strip()
        port = str(data.get("device_port", "")).strip()
        if not name or not command or not port:
            return jsonify({"error": "name, command_template and device_port are required"}), 400
        offensive = _rule_is_offensive(command)
        if offensive and data.get("consent") is not True:
            return jsonify({"error": "This rule auto-fires an offensive command — confirm "
                                     "authorized use to add it (it lands disabled until you arm it)."}), 403
        tt_raw = str(data.get("target_type", "") or "")
        try:
            target_type = TargetType(tt_raw) if tt_raw else None
        except ValueError:
            return jsonify({"error": f"bad target_type: {tt_raw}"}), 400
        try:
            min_rssi = int(data.get("min_rssi", -100))
            cooldown = float(data.get("cooldown", 30.0))
        except (TypeError, ValueError):
            return jsonify({"error": "min_rssi/cooldown must be numbers"}), 400
        rule = RoutingRule(
            name=name, target_type=target_type, ssid_pattern=str(data.get("ssid_pattern", "")),
            min_rssi=min_rssi, device_port=port, command_template=command,
            enabled=(not offensive),  # offensive rules land DISABLED — never auto-fire on add
            cooldown=cooldown,
        )
        auto_router.add_rule(rule)
        _audit("rule_add", user=session.get("user"), name=name, offensive=offensive,
               enabled=rule.enabled)
        return jsonify({"status": "added", "name": name, "offensive": offensive,
                        "enabled": rule.enabled}), 201

    @app.route("/api/rules/remove", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_rules_remove():
        if auto_router is None:
            return jsonify({"error": "auto-routing not available"}), 503
        name = str(_json_body().get("name", ""))
        ok = auto_router.remove_rule(name)
        _audit("rule_remove", user=session.get("user"), name=name, removed=ok)
        return jsonify({"status": "removed" if ok else "not_found", "name": name})

    @app.route("/api/rules/toggle", methods=["POST"])
    @requires_auth
    @requires_csrf
    def api_rules_toggle():
        if auto_router is None:
            return jsonify({"error": "auto-routing not available"}), 503
        data = _json_body()
        name = str(data.get("name", ""))
        enabled = data.get("enabled") is True
        rule = next((r for r in auto_router.list_rules() if r.name == name), None)
        if rule is None:
            return jsonify({"error": f"Unknown rule: {name}"}), 404
        # Arming (enabling) an offensive rule is the consent-gated act — when it can auto-fire.
        arming_offensive = enabled and _rule_is_offensive(rule.command_template)
        if arming_offensive and data.get("consent") is not True:
            return jsonify({"error": "Arming an offensive rule needs authorized-use consent."}), 403
        auto_router.remove_rule(name)
        rule.enabled = enabled
        auto_router.add_rule(rule)
        _audit("rule_toggle", user=session.get("user"), name=name, enabled=enabled)
        return jsonify({"status": "ok", "name": name, "enabled": enabled})

    @app.route("/api/tails")
    @requires_auth
    def api_tails():
        """Follower/tail detection (B11): personal devices (BLE/clients) that keep reappearing across
        recent time windows, strongest-first. Awareness-only — it flags, never acts. Empty until a
        device actually reappears; wall-clock-free scorer (we pass time.time())."""
        try:
            hits = tail_tracker.tails(time.time(), min_persistence=0.5)
        except Exception:
            log.exception("tail read failed")
            hits = []
        return jsonify([
            {"device": h.device, "label": h.label, "persistence": round(h.persistence, 2),
             "windows": h.windows}
            for h in hits
        ])

    @app.route("/api/os/images")
    @requires_auth
    def api_os_images():
        """The flashable PC/USB OS catalog for the Software-OS tab (read-only metadata)."""
        from src.core import os_catalog

        try:
            return jsonify(os_catalog.list_images())
        except Exception:
            log.exception("os catalog read failed")
            return jsonify([])

    @app.route("/api/os/drives")
    @requires_auth
    def api_os_drives():
        """Detected REMOVABLE drives for the Software-OS target picker. Uses the hardened sd_backend
        detector, which excludes system/boot disks and non-USB/SD/MMC buses — a fixed/system disk is
        never listed. Read-only enumeration; no write happens here."""
        from src.core.backends import sd_backend

        try:
            drives = sd_backend.detect_sd_cards(lambda _l: None)
        except Exception:
            log.exception("removable drive scan failed")
            drives = []
        return jsonify([
            {"device": d.get("device", ""), "name": d.get("name", ""),
             "size": d.get("size", 0), "bus": d.get("bus", "")}
            for d in drives
        ])

    @app.route("/api/channels")
    @requires_auth
    def api_channels():
        # Passive read-only channel-occupancy survey over the live pool (clear-2.4 GHz picker).
        return jsonify(survey_channels(target_pool.all()))

    @app.route("/api/freshness")
    @requires_auth
    def api_freshness():
        # Passive read-only staleness summary — how many targets are live vs stale left-overs.
        return jsonify(summarize_freshness(target_pool.all()))

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
        # Reject an empty command, mirroring the /api/command HTTP twin (which 400s on
        # `not command`). Without this the WS path fell through to conn.write(""), which appends
        # the line terminator and transmits a bare newline to the attached device — an unvalidated
        # no-op byte onto attack hardware that the HTTP sink refuses. Keep the two sinks symmetric.
        if not command:
            emit("serial_output", {"port": port, "line": "[Empty command ignored]"})
            return
        if len(command) > _MAX_COMMAND_LEN:
            emit("serial_output", {"port": port, "line": "[Command too long]"})
            return
        if not _known_port(port):
            emit("serial_output", {"port": port, "line": f"[Unknown port {port}]"})
            return
        # Never push operator bytes onto a UART esptool is mid-flash on — a stray write can
        # brick the board. Mirrors the /api/command 409 guard; /terminal needs the same shield.
        if flash_engine.is_port_busy(port):
            emit("serial_output", {"port": port, "line": f"[Port {port} is busy with a flash]"})
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

    # ── Host shell (opt-in, loopback-only, never LAN) ─────────────────────────────────────────────────
    # These handlers are DEFINED ONLY when the envelope said enabled. On a LAN bind, or without the
    # CC_WEB_HOST_SHELL opt-in, they do not exist at all — a client cannot conjure a host shell into being
    # by emitting the event, because there is nothing listening. Each socket (sid) owns at most one shell,
    # torn down on close or disconnect. Auth + rate-limit + audit mirror the serial send_command path.
    if host_shell_enabled:
        _host_shells: dict = {}          # sid -> HostShellSession
        _host_shells_lock = threading.Lock()

        def _close_host_shell(sid: str) -> None:
            with _host_shells_lock:
                sess = _host_shells.pop(sid, None)
            if sess is not None:
                sess.kill()

        @socketio.on("host_shell_open")
        def on_host_shell_open(_data=None) -> None:
            if not _socket_authed():
                return
            sid = request.sid
            with _host_shells_lock:
                if sid in _host_shells:
                    emit("host_shell_status", {"open": True})
                    return
                # bind the sid so the reader thread (no request context) can target this exact socket
                sess = host_shell.HostShellSession(
                    lambda text, s=sid: socketio.emit("host_shell_output", {"text": text}, to=s))
                _host_shells[sid] = sess
            _audit("host_shell_open", user=session.get("user"))
            sess.start()
            emit("host_shell_status", {"open": True})

        @socketio.on("host_shell_input")
        def on_host_shell_input(data=None) -> None:
            if not _socket_authed():
                return
            if not cmd_limiter.allow(_client_ip()):
                emit("host_shell_output", {"text": "[rate limited]\r\n"})
                return
            if not isinstance(data, dict):
                data = {}
            text = str(data.get("data", ""))
            if len(text) > _MAX_COMMAND_LEN:
                emit("host_shell_output", {"text": "[input too long]\r\n"})
                return
            with _host_shells_lock:
                sess = _host_shells.get(request.sid)
            if sess is None:
                emit("host_shell_output", {"text": "[host shell not open]\r\n"})
                return
            _audit("host_shell_input", user=session.get("user"))
            sess.write(text)

        @socketio.on("host_shell_resize")
        def on_host_shell_resize(data=None) -> None:
            if not _socket_authed():
                return
            if not isinstance(data, dict):
                data = {}
            with _host_shells_lock:
                sess = _host_shells.get(request.sid)
            if sess is not None:
                try:
                    sess.resize(int(data.get("cols", 80)), int(data.get("rows", 24)))
                except (TypeError, ValueError):
                    pass

        @socketio.on("host_shell_close")
        def on_host_shell_close(_data=None) -> None:
            _close_host_shell(request.sid)
            emit("host_shell_status", {"open": False})

        @socketio.on("disconnect")
        def on_host_shell_disconnect() -> None:
            # Never leak a live shell process when the socket goes away.
            _close_host_shell(request.sid)

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
    desktop_token: str | None = None,
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
    # SEC-W2: behind a reverse proxy, remote_addr is the proxy — collapsing every client to one
    # rate-limit bucket + one audit identity. Let the operator name the trusted proxy IPs (comma-
    # separated) so the real client is recovered from X-Forwarded-For; empty/unset = trust nothing
    # (remote_addr verbatim). Never trusted implicitly — a spoofable header must be opted into.
    trusted_proxies = [
        p for p in os.environ.get("CC_WEB_TRUSTED_PROXIES", "").split(",") if p.strip()
    ]
    # Composition-root unlock (P0-1): build the cross-comm spine so the shared TargetPool + CaptureStore
    # actually populate under the web/desktop UIs. CrossCommHub auto-attaches a TargetIngestor to every
    # opened connection (a scan on any device -> target.added -> the shared pool), builds the CaptureStore,
    # and owns the AutoRouter / Broadcast / Meshtastic backends — the same spine the Qt window assembles.
    # Without it the web pool stayed empty forever, starving Dashboard cross-comm, all of HUNT, and CRACK
    # captures. It reuses the SAME dm/bus/pool create_app gets, so this is behavior-additive: a device that
    # scans now feeds the existing target.added subscriber. Held for the process lifetime (launch_web blocks
    # on socketio.run below); also stashed on app.config so it can't be GC'd and future code can reach it.
    from src.core.cross_comm_hub import CrossCommHub
    hub = CrossCommHub(device_manager, event_bus, target_pool)

    app, socketio = create_app(
        device_manager, flash_engine, event_bus, target_pool,
        audit=audit, allowed_origins=origins, trusted_proxies=trusted_proxies,
        desktop_token=desktop_token, capture_store=hub.captures,
        # Only a loopback bind is even a candidate for the host shell; the env opt-ins are checked inside.
        host_shell_loopback=is_local,
        auto_router=hub.router,  # Cross-Comm rules surface (B14) — offensive rules are consent+arm gated
        sensing_model=hub.sensing,  # WS1: CSI sensing-node rollup, read by /api/sensing
    )
    app.config["cc_hub"] = hub

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
