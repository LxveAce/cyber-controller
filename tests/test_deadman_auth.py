"""Dead Man's Switch serial auth detection (src/core/deadman_auth.py).

Verifies the prompt/result patterns match the deadmans-switch boot-gate's ACTUAL serial strings
(`suicide-gate: enter ...` / `suicide-gate: wrong. attempts left: N` / `suicide-gate: locked for Ns.`),
that a detected prompt triggers exactly one password send, and that benign `SM>{...}` status JSON is
never mistaken for an auth prompt (it must be safe to poll).
"""

from __future__ import annotations

from src.core.deadman_auth import DeadManAuth


def _auth(password="hunter2"):
    sent = []
    results = []
    a = DeadManAuth()
    a.set_auth_handler(lambda: password)
    a.set_result_handler(lambda ok, msg: results.append((ok, msg)))
    return a, sent, results


def test_real_prompt_triggers_one_send():
    a, sent, _ = _auth("s3cret")
    handled = a.check_line(
        "suicide-gate: enter `unlock <password>` (or just the password). `wipe` to erase.",
        sent.append,
    )
    assert handled is True
    assert sent == ["s3cret"]


def test_wrong_attempt_line_is_a_failure():
    a, sent, results = _auth()
    handled = a.check_line("suicide-gate: wrong. attempts left: 2", sent.append)
    assert handled is True
    assert results and results[-1][0] is False
    assert sent == []  # a failure notice must NOT send anything


def test_locked_backoff_line_is_a_failure():
    a, sent, results = _auth()
    assert a.check_line("suicide-gate: locked for 30s.", sent.append) is True
    assert results[-1][0] is False
    assert sent == []


def test_status_json_is_not_an_auth_prompt():
    a, sent, results = _auth()
    line = 'SM>{"cmd":"STATUS","provisioned":true,"armed":1,"att_ct":0,"max_att":3}'
    handled = a.check_line(line, sent.append)
    assert handled is False  # safe to poll — never treated as auth
    assert sent == [] and results == []


def test_cancel_sends_nothing():
    a = DeadManAuth()
    a.set_auth_handler(lambda: None)  # user cancels
    sent = []
    a.check_line("suicide-gate: enter the password", sent.append)
    assert sent == []


def test_ordinary_line_is_ignored():
    a, sent, results = _auth()
    assert a.check_line("scanap: 14 APs found", sent.append) is False
    assert sent == [] and results == []
