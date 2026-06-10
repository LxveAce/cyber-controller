"""Flask web remote — phone-friendly interface for headless Cyber Controller.

Provides a responsive web dashboard with SocketIO for real-time serial output,
device events, target discovery, and flash progress updates.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    session,
)
from flask_socketio import SocketIO, emit

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine, FirmwareProfile
from src.core.serial_handler import SerialConnection

log = logging.getLogger(__name__)

_PROFILES_DIR = Path(__file__).resolve().parents[3] / "src" / "config" / "profiles"
_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

# Default credentials — override via CC_WEB_USER / CC_WEB_PASS env vars
_DEFAULT_USER = "admin"
_DEFAULT_PASS = "cyber"


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
) -> tuple[Flask, SocketIO]:
    """Create and configure the Flask application and SocketIO instance."""

    app = Flask(
        __name__,
        template_folder=str(_TEMPLATE_DIR),
        static_folder=str(_STATIC_DIR),
    )
    app.secret_key = os.urandom(24)

    socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")
    profiles = _load_profiles()

    # Auth credentials from env
    auth_user = os.environ.get("CC_WEB_USER", _DEFAULT_USER)
    auth_pass = os.environ.get("CC_WEB_PASS", _DEFAULT_PASS)

    # ── Auth helpers ────────────────────────────────────────────────

    def check_auth(username: str, password: str) -> bool:
        return username == auth_user and password == auth_pass

    def requires_auth(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            auth = request.authorization
            if auth and check_auth(auth.username, auth.password):
                return f(*args, **kwargs)
            # Check session-based auth
            if session.get("authenticated"):
                return f(*args, **kwargs)
            return Response(
                "Authentication required.\n",
                401,
                {"WWW-Authenticate": 'Basic realm="Cyber Controller"'},
            )
        return decorated

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
        return render_template(
            "devices.html",
            devices=device_manager.list_devices(),
        )

    @app.route("/flash")
    @requires_auth
    def flash_page():
        ports = device_manager.scan_ports()
        return render_template(
            "flash.html",
            ports=ports,
            profiles=list(profiles.keys()),
        )

    @app.route("/targets")
    @requires_auth
    def targets_page():
        return render_template(
            "targets.html",
            targets=target_pool.all(),
        )

    @app.route("/terminal/<port>")
    @requires_auth
    def terminal_page(port: str):
        device = device_manager.get_device(port)
        return render_template(
            "terminal.html",
            port=port,
            device=device,
        )

    # ── API routes ──────────────────────────────────────────────────

    @app.route("/api/flash", methods=["POST"])
    @requires_auth
    def api_flash():
        data = request.get_json(force=True)
        port = data.get("port", "")
        profile_name = data.get("profile_id", "")

        if not port:
            return jsonify({"error": "port is required"}), 400
        if not profile_name:
            return jsonify({"error": "profile_id is required"}), 400

        profile_path = profiles.get(profile_name)
        if not profile_path:
            return jsonify({"error": f"Unknown profile: {profile_name}"}), 404

        profile = flash_engine.load_profile(profile_path)

        def progress_cb(pct: int, msg: str) -> None:
            socketio.emit("flash_progress", {"port": port, "percent": pct, "message": msg})

        def flash_thread() -> None:
            ok = flash_engine.flash(port, profile, progress_callback=progress_cb)
            socketio.emit("flash_progress", {
                "port": port,
                "percent": 100 if ok else 0,
                "message": "Flash complete" if ok else "Flash failed",
                "done": True,
                "success": ok,
            })

        threading.Thread(target=flash_thread, daemon=True).start()
        return jsonify({"status": "flashing", "port": port, "profile": profile_name})

    @app.route("/api/command", methods=["POST"])
    @requires_auth
    def api_command():
        data = request.get_json(force=True)
        port = data.get("port", "")
        command = data.get("command", "")

        if not port or not command:
            return jsonify({"error": "port and command are required"}), 400

        conn = device_manager.get_connection(port)
        if not conn or not conn.is_connected:
            return jsonify({"error": f"No active connection on {port}"}), 400

        try:
            conn.write(command)
            return jsonify({"status": "sent", "port": port, "command": command})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

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
        return jsonify({
            "status": "ok",
            "device_count": len(devices),
            "connected_count": len([d for d in devices if d.connected]),
            "target_count": target_pool.count,
            "flash_status": flash_engine.status.value,
        })

    # ── SocketIO events ─────────────────────────────────────────────

    @socketio.on("connect")
    def on_ws_connect():
        log.info("WebSocket client connected")

    @socketio.on("subscribe_serial")
    def on_subscribe_serial(data: dict) -> None:
        """Subscribe to serial output from a specific port."""
        port = data.get("port", "")
        conn = device_manager.get_connection(port)
        if conn and conn.is_connected:
            conn.on_line(lambda line: socketio.emit("serial_output", {
                "port": port, "line": line,
            }))
            emit("serial_output", {"port": port, "line": f"[Subscribed to {port}]"})
        else:
            emit("serial_output", {"port": port, "line": f"[Not connected to {port}]"})

    @socketio.on("send_command")
    def on_send_command(data: dict) -> None:
        port = data.get("port", "")
        command = data.get("command", "")
        conn = device_manager.get_connection(port)
        if conn and conn.is_connected:
            try:
                conn.write(command)
                emit("serial_output", {"port": port, "line": f"> {command}"})
            except Exception as exc:
                emit("serial_output", {"port": port, "line": f"[Error: {exc}]"})
        else:
            emit("serial_output", {"port": port, "line": f"[Not connected to {port}]"})

    return app, socketio


def launch_web(
    device_manager: DeviceManager,
    flash_engine: FlashEngine,
    event_bus: EventBus,
    target_pool: TargetPool,
    *,
    host: str = "0.0.0.0",
    port: int = 5000,
) -> int:
    """Create and run the Flask web remote UI."""
    app, socketio = create_app(device_manager, flash_engine, event_bus, target_pool)
    log.info("Starting web UI on http://%s:%d", host, port)
    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)
    return 0
