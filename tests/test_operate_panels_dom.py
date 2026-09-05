"""A01 regression: the OPERATE sub-panels (console / macros / broadcast / antenna) must be SIBLINGS, not
nested inside one another. A broken close tag once left Broadcast + Antenna inside the hidden Macros
panel, so those tabs rendered blank. This parses the real rendered /reform DOM and asserts the nesting."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

pytest.importorskip("flask")

from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.security.web_auth import new_csrf_token
from src.ui.web.app import create_app


class _SubNestingParser(HTMLParser):
    """Tracks the open-div stack and records, for each ``<div data-sub=...>``, how many OTHER data-sub
    divs were already open (its nesting depth among sub-panels)."""

    def __init__(self):
        super().__init__()
        self._div_stack: list[str | None] = []   # data-sub value (or None) per open div
        self.sub_open_depth: dict[str, int] = {}

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        sub = dict(attrs).get("data-sub")
        if sub is not None:
            self.sub_open_depth[sub] = sum(1 for s in self._div_stack if s is not None)
        self._div_stack.append(sub)

    def handle_endtag(self, tag):
        if tag == "div" and self._div_stack:
            self._div_stack.pop()


@pytest.fixture
def html(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "admin")
    monkeypatch.setenv("CC_WEB_PASS", "test-pass-123")
    app, _sio = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool())
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["csrf"] = new_csrf_token()
    return client.get("/reform").get_data(as_text=True)


def test_operate_subpanels_are_siblings(html):
    p = _SubNestingParser()
    p.feed(html)
    for name in ("console", "macros", "broadcast", "antenna"):
        assert name in p.sub_open_depth, f"OPERATE sub-panel {name!r} missing from the DOM"
    # Each OPERATE sub-panel must open at the SAME sub-depth as console (i.e. none is nested inside
    # another sub-panel). console is the reference sibling.
    base = p.sub_open_depth["console"]
    for name in ("macros", "broadcast", "antenna"):
        assert p.sub_open_depth[name] == base, (
            f"{name!r} is nested inside another sub-panel (depth {p.sub_open_depth[name]} != {base})")


def test_antenna_controls_present(html):
    # the calculator's inputs must actually be in the served markup
    assert 'id="ant-freq"' in html and 'id="ant-calc"' in html
