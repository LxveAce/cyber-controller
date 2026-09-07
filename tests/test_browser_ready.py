"""Web-mode browser opening waits for the launched server's own readiness report, and the logs
carry only the URL the server reports, never a guess from the bind host and port.

Everything is a double: the web app module, the browser opener, the clock, the log capture and the
helper thread class. No server binds, no browser opens, no network is touched.
"""
from __future__ import annotations

import logging
import sys
import threading
import types

import pytest

from src import app

ARGS = tuple(object() for _ in range(4))
AUDIT = object()
HELPER = "open-web-browser"
JOIN = 3.0
LOGGER = "cyber-controller"
# (bind host, URL the server reports): TLS on a wildcard IPv6 bind, a literal IPv6, plain IPv4.
REPORTED = [
    ("::", "https://[::1]:5443"),
    ("2001:db8::1", "http://[2001:db8::1]:5000"),
    ("0.0.0.0", "http://127.0.0.1:5000"),
]


class TrackedThread(threading.Thread):
    instances = []
    script = {}

    def __init__(self, *args, **kwargs):
        if kwargs.get("name") == HELPER and self.script.get("ctor_raises"):
            raise RuntimeError("helper construction failed (double)")
        super().__init__(*args, **kwargs)
        TrackedThread.instances.append(self)

    def start(self):
        if self.name == HELPER and self.script.get("start_raises"):
            raise RuntimeError("helper start failed (double)")
        super().start()


@pytest.fixture
def helpers(monkeypatch):
    TrackedThread.instances.clear()
    TrackedThread.script = {}
    monkeypatch.setattr(app, "threading",
                        types.SimpleNamespace(Thread=TrackedThread, Event=threading.Event))
    yield TrackedThread
    for thread in TrackedThread.instances:
        if thread.ident is not None:
            thread.join(JOIN)
    assert all(not t.is_alive() for t in TrackedThread.instances), "helper threads finished"
    TrackedThread.instances.clear()


def wait_helper(thread):
    assert thread is not None
    thread.join(JOIN)
    assert not thread.is_alive()


def messages(caplog, needle):
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


# The signal.

def test_signal_publishes_url_before_setting_the_event():
    order = []

    class WitnessEvent(threading.Event):
        def set(self):
            order.append(("set", ready.url))
            super().set()

    ready = app.LaunchReady()
    ready.event = WitnessEvent()
    ready.serving("https://[::1]:5443")
    assert order == [("set", "https://[::1]:5443")]
    assert ready.event.is_set()


@pytest.mark.parametrize("url", [url for _, url in REPORTED])
def test_serving_logs_the_reported_url(caplog, url):
    ready = app.LaunchReady()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        ready.serving(url)
    assert messages(caplog, "Web remote serving at") == [f"Web remote serving at {url}"]
    assert ready.url == url and ready.event.is_set()


# The opener.

def test_startup_line_reports_bind_configuration_not_a_url(helpers, caplog):
    ready = app.LaunchReady()
    opened = []
    with caplog.at_level(logging.INFO, logger=LOGGER):
        thread = app._open_browser_when_ready(ready, "::", 5443, opener=opened.append, wait=2.0)
        ready.serving("https://[::1]:5443")
        wait_helper(thread)
    (line,) = messages(caplog, "bind configuration")
    assert "host ::" in line and "port 5443" in line
    assert "://" not in line, "no guessed URL before readiness"
    assert caplog.text.count("://") == 1, "the only URL in the log is the reported one"
    assert opened == ["https://[::1]:5443"], "the opener uses the reported URL"


def test_opens_once_with_the_reported_url(helpers):
    ready = app.LaunchReady()
    opened = []
    thread = app._open_browser_when_ready(ready, "::", 5443, opener=opened.append, wait=2.0)
    threading.Timer(0.05, ready.serving, args=("https://[::1]:5443",)).start()
    wait_helper(thread)
    assert opened == ["https://[::1]:5443"]


def test_already_serving_opens_immediately(helpers):
    ready = app.LaunchReady()
    ready.serving("http://127.0.0.1:5000")
    opened = []
    wait_helper(app._open_browser_when_ready(ready, "127.0.0.1", 5000, opener=opened.append,
                                             wait=1.0))
    assert opened == ["http://127.0.0.1:5000"]


def test_no_report_opens_nothing_and_warns_once(helpers, caplog):
    opened = []
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        wait_helper(app._open_browser_when_ready(app.LaunchReady(), "127.0.0.1", 5000,
                                                 opener=opened.append, wait=0.1))
    assert opened == []
    assert caplog.text.count("did not report serving within 0.1s") == 1


def test_late_report_opens_nothing(helpers):
    ready = app.LaunchReady()
    opened = []
    timer = threading.Timer(0.5, ready.serving, args=("http://127.0.0.1:5000",))
    timer.start()
    try:
        wait_helper(app._open_browser_when_ready(ready, "127.0.0.1", 5000, opener=opened.append,
                                                 wait=0.1))
    finally:
        timer.cancel()
    assert opened == []


def test_deadline_is_rechecked_after_the_wait(helpers, caplog):
    ready = app.LaunchReady()
    ready.serving("http://127.0.0.1:5000")
    ticks = iter([100.0, 100.0, 200.0, 200.0])
    opened = []
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        wait_helper(app._open_browser_when_ready(ready, "127.0.0.1", 5000, opener=opened.append,
                                                 wait=0.1, clock=lambda: next(ticks)))
    assert opened == []
    assert "did not report serving" in caplog.text


def test_opener_failure_is_logged_with_the_reported_url(helpers, caplog):
    ready = app.LaunchReady()
    ready.serving("https://[::1]:5443")

    def broken(url):
        raise RuntimeError("no browser (double)")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        wait_helper(app._open_browser_when_ready(ready, "::", 5443, opener=broken, wait=1.0))
    assert messages(caplog, "Browser did not open") == [
        "Browser did not open (no browser (double)); open https://[::1]:5443 yourself."
    ]


@pytest.mark.parametrize("failure", ["ctor_raises", "start_raises"])
@pytest.mark.parametrize("host, url", REPORTED)
def test_helper_failure_warns_without_a_guess_and_the_report_still_logs_the_real_url(
    helpers, caplog, failure, host, url,
):
    helpers.script[failure] = True
    ready = app.LaunchReady()
    opened = []
    port = int(url.rsplit(":", 1)[1])
    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = app._open_browser_when_ready(ready, host, port, opener=opened.append, wait=1.0)
        ready.serving(url)
    assert result is None
    (warning,) = messages(caplog, "Browser helper could not start")
    assert "://" not in warning, "the warning names no guessed URL"
    assert "Web remote serving at" in warning, "it points at the line that carries the real URL"
    (line,) = messages(caplog, "bind configuration")
    assert f"host {host}" in line and f"port {port}" in line and "://" not in line
    assert messages(caplog, "Web remote serving at ") == [f"Web remote serving at {url}"]
    assert caplog.text.count("://") == 1, "the reported URL is the only URL logged"
    assert opened == []


# The launcher wiring.

@pytest.fixture
def web_app(monkeypatch):
    seen = {}

    def launch_web(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        kwargs["ready"].serving("https://[::1]:5443")
        return 7

    module = types.ModuleType("src.ui.web.app")
    module.launch_web = launch_web
    monkeypatch.setitem(sys.modules, "src.ui.web.app", module)
    monkeypatch.setattr(app, "_BROWSER_READY_SECONDS", 2.0)
    return seen


def test_launch_web_hands_the_signal_to_the_server_and_opens_its_url(helpers, web_app, monkeypatch):
    opened = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", opened.append)
    code = app._launch_web(*ARGS, host="::", port=5443, audit=AUDIT)
    for thread in helpers.instances:
        thread.join(JOIN)
    assert code == 7
    assert web_app["args"] == ARGS
    assert web_app["kwargs"]["host"] == "::"
    assert web_app["kwargs"]["port"] == 5443
    assert web_app["kwargs"]["audit"] is AUDIT
    assert isinstance(web_app["kwargs"]["ready"], app.LaunchReady)
    assert opened == ["https://[::1]:5443"], "the browser gets the URL the server reported"


@pytest.mark.parametrize("failure", ["ctor_raises", "start_raises"])
def test_launch_web_runs_the_server_and_logs_the_real_url_when_the_helper_cannot_start(
    helpers, web_app, monkeypatch, caplog, failure,
):
    helpers.script[failure] = True
    opened = []
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", opened.append)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        code = app._launch_web(*ARGS, host="::", port=5443, audit=AUDIT)
    assert code == 7
    assert "Browser helper could not start" in caplog.text
    assert messages(caplog, "Web remote serving at ") == ["Web remote serving at https://[::1]:5443"]
    assert caplog.text.count("://") == 1, "the only URL in the log is the reported one"
    assert opened == []


def test_launch_web_import_failure_still_returns_1(helpers, monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "src.ui.web.app", None)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        assert app._launch_web(*ARGS, host="127.0.0.1", port=5000, audit=AUDIT) == 1
    assert "Browser UI startup import failed" in caplog.text
