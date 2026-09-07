"""Concurrent web writes must preserve preferences acknowledged by another request."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.config import settings
from src.core import update_checker as uc
from src.core.cross_comm import EventBus, TargetPool
from src.core.device_manager import DeviceManager
from src.core.flash_engine import FlashEngine
from src.core.lifecycle import CallbackScope
from src.ui.web.app import create_app
from src.ui.web.update_runtime import UpdateAvailability, WebRuntimeCleanup


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "gate.json"))
    monkeypatch.setenv("CC_WEB_USER", "fixture")
    monkeypatch.setenv("CC_WEB_PASS", "inert-test-password-456")
    return tmp_path / "settings.json"


@pytest.fixture
def app(store):
    application, _ = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool())
    yield application
    application.extensions["cc_begin_close"]()
    application.extensions["cc_finish_close"]()


def post(app, endpoint, body):
    client = app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["cred_gen"] = app.extensions["cc_web_credentials"].generation
        session["csrf"] = "fixture"
    return client.post(endpoint, json=body, headers={"X-CSRF-Token": "fixture"})


def start_call(call):
    outcome = {}

    def run():
        try:
            outcome["value"] = call()
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run, name="settings-test-request")
    thread.start()
    return thread, outcome


def settle(thread, outcome):
    thread.join(5)
    assert not thread.is_alive(), "fixture request did not settle"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


_STAMP = datetime(2026, 9, 6, 19, tzinfo=timezone.utc)


def _release(tag):
    return {"tag_name": tag, "html_url": uc.updater.RELEASES_PAGE + "/tag/" + tag}


class _Fetch:
    """Inert releases fetch: gates on entered/release so a manual check can be held in flight, and
    returns one current-version release (UP_TO_DATE), unless an error is set (a raised fetch is
    OFFLINE)."""

    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()
        self.rows = [_release("v2.0.1")]
        self.error = None

    def __call__(self):
        self.entered.set()
        assert self.release.wait(15), "inert fetch was not released"
        if self.error is not None:
            raise self.error
        return self.rows


class _Hub:
    """Inert cross-comm hub stand-in for the runtime cleanup owner (records closes, no I/O)."""

    captures = router = sensing = None

    def __init__(self, *args):
        self.closes = 0

    def fence(self):
        pass

    def close(self):
        self.closes += 1


@pytest.fixture
def updates_app(store):
    """A settings app with a fixture-OWNED manual update availability service: a real UpdateChecker
    over an inert fetch, wrapped by UpdateAvailability, whose runtime is closed and settled in
    teardown (error paths included). The missing-owner 503 contract is covered in
    test_update_availability_runtime."""
    fetch = _Fetch()
    checker = uc.UpdateChecker("2.0.1", enabled=False, manual_cooldown_seconds=0,
                               fetch=fetch, wall_clock=lambda: _STAMP)
    scope = CallbackScope()
    owner = WebRuntimeCleanup(scope, _Hub())
    owner.checker = checker
    application, _ = create_app(DeviceManager(), FlashEngine(), EventBus(), TargetPool(),
                                lifecycle=scope, availability=UpdateAvailability(checker))
    owner.attach_app(application)
    try:
        yield SimpleNamespace(app=application, fetch=fetch, checker=checker, owner=owner)
    finally:
        fetch.release.set()   # never leave a held fetch, even on an error path
        owner.close()         # closes + settles the fixture-owned checker/app runtime


@pytest.mark.parametrize("reset", [False, True])
def test_update_completion_preserves_concurrent_settings(updates_app, store, reset):
    settings.save_settings({"serial": {"default_baud": 230400},
                            "uploads": {"wigle_token": "old-fixture-token"}})
    env = updates_app
    env.checker.start()
    thread, outcome = start_call(lambda: post(env.app, "/api/updates/check", {}))
    try:
        assert env.fetch.entered.wait(5)   # the manual check is in flight (held by the fetch)
        body = {"reset": True} if reset else {
            "serial": {"default_baud": 9600}, "interface": {"touch_mode": "on"},
            "updates": {"enabled": False}, "uploads": {"wigle_token": "new-fixture-token"}}
        saved = post(env.app, "/api/settings", body)
        assert saved.status_code == 200
        acknowledged = json.loads(store.read_text(encoding="utf8"))
    finally:
        env.fetch.release.set()
        result = settle(thread, outcome)
    assert result.status_code == 200
    after = settings.load_settings()
    for section in ("serial", "interface", "uploads", "safety", "vault"):
        assert after[section] == acknowledged[section]
    assert after["updates"]["enabled"] == acknowledged["updates"]["enabled"]
    assert after["updates"]["last_check_iso"] == "2026-09-06T19:00:00+00:00"
    assert after["updates"]["last_seen_latest"] == "v2.0.1"


def test_validated_web_patch_uses_current_store_at_commit(app, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = settings.patch_settings

    def delayed(changes, **kwargs):
        if "serial" in changes:
            entered.set()
            assert release.wait(5)
        return original(changes, **kwargs)

    monkeypatch.setattr(settings, "patch_settings", delayed)
    thread, outcome = start_call(lambda: post(app, "/api/settings", {
        "serial": {"default_baud": 9600}, "uploads": {"wigle_token": "••••"}}))
    try:
        assert entered.wait(5)
        other = post(app, "/api/settings", {
            "interface": {"touch_mode": "on"}, "uploads": {"wigle_token": "new-secret"}})
        assert other.status_code == 200
    finally:
        release.set()
        first = settle(thread, outcome)
    assert first.status_code == 200
    assert first.get_json()["settings"]["interface"]["touch_mode"] == "on"
    after = settings.load_settings()
    assert after["serial"]["default_baud"] == 9600
    assert after["interface"]["touch_mode"] == "on"
    assert after["uploads"]["wigle_token"] == "new-secret"
    assert "new-secret" not in first.get_data(as_text=True)


def test_patch_preserves_unknown_fields_and_explicit_values(store):
    settings.save_settings({"interface": {"loadout": {"name": "fixture"}},
                            "future": {"left": 1, "right": 2},
                            "uploads": {"wigle_token": "fixture-token"}})
    changes = {"flash": {"flash_baud": None}, "uploads": {"wigle_token": ""},
               "future": {"right": 3}}
    committed = settings.patch_settings(changes)
    assert committed == settings.load_settings()
    assert changes == {"flash": {"flash_baud": None}, "uploads": {"wigle_token": ""},
                       "future": {"right": 3}}
    assert committed["future"] == {"left": 1, "right": 3}
    assert committed["interface"]["loadout"] == {"name": "fixture"}
    assert committed["uploads"]["wigle_token"] == ""
    assert committed["flash"]["flash_baud"] is None


def test_web_response_projects_its_own_committed_state(app, monkeypatch):
    original = settings.patch_settings

    def interleaved(changes, **kwargs):
        committed = original(changes, **kwargs)
        if "serial" in changes:
            response = post(app, "/api/settings", {"interface": {"touch_mode": "on"}})
            assert response.status_code == 200
        return committed

    monkeypatch.setattr(settings, "patch_settings", interleaved)
    response = post(app, "/api/settings", {"serial": {"default_baud": 9600}})
    assert response.status_code == 200
    assert response.get_json()["settings"]["interface"]["touch_mode"] == "auto"
    assert settings.load_settings()["interface"]["touch_mode"] == "on"


def test_patch_serializes_read_merge_write(store, monkeypatch):
    first_loaded, release = threading.Event(), threading.Event()
    second_attempted, second_acquired = threading.Event(), threading.Event()
    original_lock = settings._WRITE_LOCK
    original_load = settings.load_settings
    first_id = []
    second_id = []

    class ObservedLock:
        def __enter__(self):
            second = threading.get_ident() in second_id
            if second:
                second_attempted.set()
            original_lock.acquire()
            if second:
                second_acquired.set()

        def __exit__(self, *args):
            original_lock.release()

    def delayed_load():
        value = original_load()
        if threading.get_ident() in first_id:
            first_loaded.set()
            assert release.wait(5)
        return value

    monkeypatch.setattr(settings, "_WRITE_LOCK", ObservedLock())
    monkeypatch.setattr(settings, "load_settings", delayed_load)

    def first():
        first_id.append(threading.get_ident())
        return settings.patch_settings({"serial": {"default_baud": 9600}})

    def second():
        second_id.append(threading.get_ident())
        return settings.patch_settings({"interface": {"touch_mode": "on"}})

    thread1, outcome1 = start_call(first)
    thread2 = outcome2 = None
    try:
        assert first_loaded.wait(5)
        thread2, outcome2 = start_call(second)
        assert second_attempted.wait(5)
        assert not second_acquired.is_set()
    finally:
        release.set()
        settle(thread1, outcome1)
        if thread2 is not None:
            settle(thread2, outcome2)
    assert second_acquired.is_set()
    stored = original_load()
    assert stored["serial"]["default_baud"] == 9600
    assert stored["interface"]["touch_mode"] == "on"


def test_reset_removes_unknowns_and_replacement_api_stays_replacement(store):
    settings.save_settings({"future": {"x": 1}, "serial": {"default_baud": 9600}})
    committed = settings.patch_settings({}, reset=True)
    assert committed == settings._defaults_copy() == settings.load_settings()
    settings.patch_settings({"interface": {"touch_mode": "on"}})
    assert settings.save_settings({"serial": {"default_baud": 230400}}) is None
    assert settings.load_settings()["interface"]["touch_mode"] == "auto"


@pytest.mark.parametrize("reset", [False, True])
def test_failed_web_write_preserves_disk_and_next_request_can_save(app, store, monkeypatch, reset):
    settings.save_settings({"serial": {"default_baud": 9600}})
    previous = store.read_bytes()
    original = settings.os.replace

    def fail(*args):
        raise OSError("fixture replace failure")

    with monkeypatch.context() as context:
        context.setattr(settings.os, "replace", fail)
        response = post(app, "/api/settings", {"reset": True} if reset else {
            "serial": {"default_baud": 230400}})
    assert response.status_code == 500
    assert response.get_json() == {"ok": False, "errors": ["write_failed"]}
    assert store.read_bytes() == previous
    assert settings.os.replace is original
    thread, outcome = start_call(lambda: post(app, "/api/settings", {
        "interface": {"touch_mode": "on"}}))
    assert settle(thread, outcome).status_code == 200
    assert settings.load_settings()["serial"]["default_baud"] == 9600


def test_no_write_for_invalid_patch(app, store):
    settings.save_settings({"serial": {"default_baud": 9600}})
    previous = store.read_bytes()
    response = post(app, "/api/settings", {
        "interface": {"touch_mode": "on"}, "serial": {"default_baud": 123}})
    assert response.status_code == 400
    assert store.read_bytes() == previous


def test_failure_after_replace_reports_error_without_claiming_rollback(app, monkeypatch):
    def fail(path):
        raise OSError("fixture post-replace ACL failure")

    with monkeypatch.context() as context:
        context.setattr(settings, "restrict_to_current_user", fail)
        response = post(app, "/api/settings", {"serial": {"default_baud": 9600}})
    assert response.status_code == 500
    assert response.get_json() == {"ok": False, "errors": ["write_failed"]}
    assert settings.load_settings()["serial"]["default_baud"] == 9600
    thread, outcome = start_call(lambda: post(app, "/api/settings", {
        "interface": {"touch_mode": "on"}}))
    assert settle(thread, outcome).status_code == 200


def test_offline_completion_retains_previous_tag_and_new_preferences(updates_app):
    env = updates_app
    settings.save_settings({"updates": {"last_seen_latest": "v2.0.1"}})
    env.fetch.error = OSError("inert offline")   # a raised fetch is OFFLINE (no fallback)
    env.fetch.release.set()
    env.checker.start()
    response = post(env.app, "/api/updates/check", {})
    assert response.status_code == 200
    assert settings.load_settings()["updates"]["last_seen_latest"] == "v2.0.1"  # retained
    assert settings.load_settings()["updates"]["last_check_iso"]
