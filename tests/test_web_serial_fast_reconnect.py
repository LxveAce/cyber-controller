"""Fast disconnect+reconnect: serial delivery must follow the connection lifecycle.

When a device drops and reconnects entirely between two inventory polls, the frontend never samples
a "disconnected" state, so the client-side transition re-subscribe can't fire — yet the managed
connection OBJECT was replaced, orphaning the serial callback on the dead object. The fix reconciles
the active subscription on the DeviceManager ``on_connection_opened`` hook, which fires for every
real replacement path (a serial ``open_connection`` rebuild and an injected ``attach_connection``).
These tests drive that boundary with fake links only (no real serial) and pin the acceptance points:

* a new connection object on the same port delivers output without a re-subscribe;
* repeated replacements never duplicate lines;
* one port's stream never surfaces under another;
* a re-subscribe stays a single callback; an ordinary disconnect doesn't crash and reconnect works.

Note on the reproduction: the fix binds at the managed connection boundary. A faithful reconnect
goes through ``open_connection`` / ``attach_connection`` (both fire the hook); a fixture that swaps
the private ``_connections`` dict directly bypasses every managed path and models no real behavior.
"""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("flask")

from flask import session

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.models.device import Device
from src.ui.web import app as webapp


@pytest.fixture(autouse=True)
def _creds(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")


class FakeLink:
    """A connection-shaped stand-in: mirrors the SerialConnection callback bookkeeping and emits a
    line to whatever callbacks are currently bound to IT (so a callback left on a replaced link
    would show up as a delivery here — which is what the isolation assertions check)."""

    is_connected = True

    def __init__(self, port: str) -> None:
        self.port = port
        self.callbacks: list = []

    def on_line(self, cb) -> None:
        self.callbacks.append(cb)

    def remove_line_callback(self, cb) -> None:
        try:
            self.callbacks.remove(cb)
        except ValueError:
            pass

    def on_state_change(self, cb) -> None:  # attach_connection wires state mirroring; no-op here
        pass

    def connect(self) -> None:
        self.is_connected = True

    def disconnect(self) -> None:
        self.is_connected = False

    def write(self, text) -> None:  # pragma: no cover - guard: this UI path never writes
        raise AssertionError("no device writes in this test")

    def emit_line(self, text: str) -> None:
        for cb in list(self.callbacks):
            cb(text)


def _capture_socket_handlers(monkeypatch) -> dict:
    captured: dict = {}
    orig_on = webapp.SocketIO.on

    def patched_on(self, message, namespace=None):
        deco = orig_on(self, message, namespace=namespace)

        def capturing(handler):
            captured[message] = handler
            return deco(handler)

        return capturing

    monkeypatch.setattr(webapp.SocketIO, "on", patched_on)
    return captured


def _build(monkeypatch, ports):
    """Wire a DeviceManager with a live FakeLink per port, build the app, and return the pieces a
    test drives: the subscribe handler, the captured serial_output emits, and the per-port links."""
    captured = _capture_socket_handlers(monkeypatch)
    monkeypatch.setattr(webapp, "emit", lambda *a, **k: None)  # neutralize the ack (no socket ctx)

    dm = DeviceManager()
    links: dict = {}
    for port in ports:
        dev = Device(port=port, name="Sim " + port, firmware="generic", connected=True)
        dm.add_device(dev)
        link = FakeLink(port)
        dm.attach_connection(dev, link)   # initial managed connection
        links[port] = link

    app, sio = webapp.create_app(dm, FlashEngine(), EventBus(), TargetPool())
    emits: list = []
    monkeypatch.setattr(sio, "emit", lambda *a, **k: emits.append(a))
    subscribe = captured["subscribe_serial"]

    def do_subscribe(port, ip="10.0.0.1"):
        with app.test_request_context(environ_base={"REMOTE_ADDR": ip}):
            session["authenticated"] = True
            session["cred_gen"] = app.extensions["cc_web_credentials"].generation
            subscribe({"port": port})

    def lines_for(port):
        return [a[1]["line"] for a in emits if a[0] == "serial_output" and a[1].get("port") == port]

    return dm, links, emits, do_subscribe, lines_for


def _replace(dm, port, links):
    """Reconnect on the SAME port with a brand-new connection object, with NO observed disconnect
    sample — the fast-reconnect scenario. Uses the real managed boundary (attach_connection)."""
    dev = dm.get_device(port)
    new_link = FakeLink(port)
    dm.attach_connection(dev, new_link)
    links[port] = new_link
    return new_link


def test_fast_reconnect_delivers_on_replaced_connection(monkeypatch):
    dm, links, emits, do_subscribe, lines_for = _build(monkeypatch, ["COM7"])
    do_subscribe("COM7")
    old = links["COM7"]
    old.emit_line("BEFORE")
    assert lines_for("COM7") == ["BEFORE"]

    new = _replace(dm, "COM7", links)   # connection object swapped, connected never observed False
    emits.clear()
    new.emit_line("C7_FAST_RECONNECT")
    assert lines_for("COM7") == ["C7_FAST_RECONNECT"]   # delivery followed the new connection

    emits.clear()
    old.emit_line("SHOULD_NOT_APPEAR")
    assert lines_for("COM7") == []   # the callback was detached from the replaced object


def test_repeated_replacement_never_duplicates(monkeypatch):
    dm, links, emits, do_subscribe, lines_for = _build(monkeypatch, ["COM7"])
    do_subscribe("COM7")
    first = links["COM7"]
    second = _replace(dm, "COM7", links)
    third = _replace(dm, "COM7", links)

    emits.clear()
    third.emit_line("ONCE")
    assert lines_for("COM7") == ["ONCE"]          # exactly one delivery, not one-per-replacement
    assert len(third.callbacks) == 1              # the live link holds a single callback

    emits.clear()
    first.emit_line("stale1")
    second.emit_line("stale2")
    assert lines_for("COM7") == []                # no callback lingers on a replaced object


def test_output_isolation_across_ports_after_replace(monkeypatch):
    dm, links, emits, do_subscribe, lines_for = _build(monkeypatch, ["COM7", "COM8"])
    do_subscribe("COM7", ip="10.0.0.1")
    do_subscribe("COM8", ip="10.0.0.2")
    _replace(dm, "COM8", links)   # replace only B

    emits.clear()
    links["COM7"].emit_line("A_LINE")
    assert lines_for("COM7") == ["A_LINE"] and lines_for("COM8") == []   # A stays under A

    emits.clear()
    links["COM8"].emit_line("B_LINE")
    assert lines_for("COM8") == ["B_LINE"] and lines_for("COM7") == []   # B's stream stays under B


def test_resubscribe_stays_single_callback(monkeypatch):
    dm, links, emits, do_subscribe, lines_for = _build(monkeypatch, ["COM7"])
    do_subscribe("COM7")
    do_subscribe("COM7")   # socket reconnect / repeat subscribe to the same live connection
    link = links["COM7"]
    assert len(link.callbacks) == 1

    emits.clear()
    link.emit_line("ONE")
    assert lines_for("COM7") == ["ONE"]   # one line -> one emit (no leaked duplicate)


def test_ordinary_disconnect_then_reconnect_recovers(monkeypatch):
    dm, links, emits, do_subscribe, lines_for = _build(monkeypatch, ["COM7"])
    do_subscribe("COM7")
    dm.close_connection("COM7")           # a real, observed disconnect — must not raise
    assert dm.get_connection("COM7") is None

    new = _replace(dm, "COM7", links)     # reconnect brings a fresh connection object
    emits.clear()
    new.emit_line("AFTER_RECONNECT")
    assert lines_for("COM7") == ["AFTER_RECONNECT"]   # the hook rebinds the surviving subscription


def test_hook_ignores_stale_out_of_order_open_callback(monkeypatch):
    # A delayed / out-of-order ``on_connection_opened`` notification can name a connection now stale
    # by the time the hook runs. The hook must reconcile against the port's CURRENT connection (via
    # get_connection), never the announced argument — else a late callback would pin delivery to a
    # replaced object. Pre-fix (the hook trusted its ``conn`` arg) this bound delivery to ``old``.
    dm, links, emits, do_subscribe, lines_for = _build(monkeypatch, ["COM7"])
    do_subscribe("COM7")
    old = links["COM7"]
    new = _replace(dm, "COM7", links)          # current is now new; the callback is on new

    dm._fire_conn_opened("COM7", old)          # a stale open notification naming the replaced link
    emits.clear()
    new.emit_line("NEW")
    assert lines_for("COM7") == ["NEW"]        # delivery stayed on the current connection
    emits.clear()
    old.emit_line("OLD")
    assert lines_for("COM7") == []             # the stale arg did NOT move the callback onto old

    stray = FakeLink("COM7")                   # an open notification for a link never made current
    dm._fire_conn_opened("COM7", stray)
    emits.clear()
    stray.emit_line("STRAY")
    assert lines_for("COM7") == []
    emits.clear()
    new.emit_line("STILL")
    assert lines_for("COM7") == ["STILL"]


def test_concurrent_subscribe_and_replace_delivers_on_current(monkeypatch):
    # The subscribe critical section resolves AND binds the connection under one lock, so a managed
    # replace racing the subscribe cannot leave delivery pinned to a captured-then-replaced conn.
    # Across every interleaving the invariant holds: the current connection delivers, no prior one.
    # Run repeatedly with a barrier to exercise the overlap (the pre-fix TOCTOU fails this when a
    # captures the old connection before the replace commits, then binds the stale object).
    for _ in range(40):
        dm, links, emits, do_subscribe, lines_for = _build(monkeypatch, ["COM7"])
        do_subscribe("COM7")
        first = links["COM7"]
        gate = threading.Barrier(2, timeout=5.0)
        errors: list = []

        def resubscribe():
            try:
                gate.wait()
                do_subscribe("COM7")
            except Exception as exc:  # pragma: no cover - surfaced via errors
                errors.append(exc)

        def replace():
            try:
                gate.wait()
                _replace(dm, "COM7", links)
            except Exception as exc:  # pragma: no cover - surfaced via errors
                errors.append(exc)

        t1 = threading.Thread(target=resubscribe)
        t2 = threading.Thread(target=replace)
        t1.start()
        t2.start()
        t1.join(10)
        t2.join(10)
        assert not (t1.is_alive() or t2.is_alive()), "subscribe/replace deadlocked"
        assert not errors, f"race raised: {errors!r}"

        current = dm.get_connection("COM7")
        emits.clear()
        current.emit_line("X")
        assert lines_for("COM7") == ["X"], "delivery is not on the current connection"
        emits.clear()
        first.emit_line("STALE")            # the original connection must never still deliver
        assert lines_for("COM7") == []
