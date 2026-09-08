"""The launcher's Windows self-update relaunch handoff: the per-attempt token is scrubbed from the
process environment first and unconditionally, the saved arguments are restored only on the
intended launch shape (frozen Windows build, no explicit argv, no real arguments, token present),
the restored values are normalised once into sys.argv, and a marked handoff that cannot be
honoured is a finite nonzero startup failure before ordinary startup.

The shared module src.core.relaunch_args is replaced by a stub carrying the agreed API surface (the
token environment name, the error type with a reason, consume_sidecar), so these tests exercise the
launcher's hook alone. No process is launched or spawned; the parser is patched to fail if ordinary
startup is ever reached where it must not be.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

from src import app

TOKEN = "0123456789abcdef0123456789abcdef"
ENV = "CC_RELAUNCH_TOKEN"


class _StubError(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(detail or reason)
        self.reason = reason


def _stub(monkeypatch, consume):
    """Install a stub src.core.relaunch_args with the agreed surface; returns the stub module.

    Bound both in sys.modules and as the attribute of the src.core package: ``from src.core import
    relaunch_args`` resolves the package attribute first, so a real module imported earlier in the
    same process would otherwise shadow the stub."""
    import src.core as core
    stub = types.ModuleType("src.core.relaunch_args")
    stub.TOKEN_ENV = ENV
    stub.RelaunchArgsError = _StubError
    stub.consume_sidecar = consume
    monkeypatch.setitem(sys.modules, "src.core.relaunch_args", stub)
    monkeypatch.setattr(core, "relaunch_args", stub, raising=False)
    return stub


@pytest.fixture
def launch(monkeypatch, tmp_path):
    """The intended launch shape: frozen, Windows, started with no arguments, no explicit argv."""
    exe = tmp_path / "cyber-controller.exe"
    exe.write_bytes(b"double")
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "argv", [str(exe)])
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv(ENV, TOKEN)
    return exe


def _no_ordinary_startup(monkeypatch):
    monkeypatch.setattr(app, "_parse_args", lambda *a: pytest.fail("ordinary startup reached"))


def _consume_ok(values):
    calls = []

    def consume(exe_path, token):
        calls.append((exe_path, token))
        return list(values)

    return consume, calls


# ---- scrubbing and the intended launch shape -----------------------------------------------------

def test_token_is_removed_from_the_environment_before_anything_else(monkeypatch, launch):
    consume, calls = _consume_ok(["--ui", "web"])
    _stub(monkeypatch, consume)
    assert app._relaunch_handoff(None) is None
    assert ENV not in os.environ, "no child of this process can inherit the token"
    assert calls == [(str(launch), TOKEN)]
    assert sys.argv == [str(launch), "--ui", "web"]


def test_no_token_is_an_ordinary_launch(monkeypatch, launch):
    monkeypatch.delenv(ENV)
    consume, calls = _consume_ok(["--ui", "web"])
    _stub(monkeypatch, consume)
    assert app._relaunch_handoff(None) is None
    assert calls == [] and sys.argv == [str(launch)]


@pytest.mark.parametrize("shape", ["explicit-argv", "real-arguments", "not-frozen", "not-windows"])
def test_other_launch_shapes_keep_their_arguments_and_still_scrub(monkeypatch, launch, shape):
    consume, calls = _consume_ok(["--ui", "web"])
    _stub(monkeypatch, consume)
    argv = None
    if shape == "explicit-argv":
        argv = ["--ui", "desktop"]
    elif shape == "real-arguments":
        monkeypatch.setattr(sys, "argv", [str(launch), "--_run-esptool", "chip_id"])
    elif shape == "not-frozen":
        monkeypatch.setattr(sys, "frozen", False, raising=False)
    else:
        monkeypatch.setattr(sys, "platform", "linux")
    before = list(sys.argv)
    assert app._relaunch_handoff(argv) is None
    assert calls == [], "the sidecar is never consumed on a launch that is not the helper's"
    assert sys.argv == before
    assert ENV not in os.environ, "scrubbed even when nothing is restored"


# ---- values survive exactly ---------------------------------------------------------------------

HOSTILE = ["--ui", "web", "", "with space", "quo\"te", "percent%", "amp&caret^", "pipe|",
           "new\nline", "unicodé 中", "--port=8080", "trailing\\"]


def test_restored_values_and_boundaries_survive_exactly(monkeypatch, launch):
    consume, _ = _consume_ok(HOSTILE)
    _stub(monkeypatch, consume)
    assert app._relaunch_handoff(None) is None
    assert sys.argv[1:] == HOSTILE, "empty values, spaces, quotes, punctuation and Unicode intact"


def test_empty_saved_arguments_restore_to_an_argument_free_launch(monkeypatch, launch):
    consume, calls = _consume_ok([])
    _stub(monkeypatch, consume)
    assert app._relaunch_handoff(None) is None
    assert calls and sys.argv == [str(launch)]


def test_restored_arguments_reach_the_real_parser(monkeypatch, launch):
    consume, _ = _consume_ok(["--ui", "web"])
    _stub(monkeypatch, consume)
    assert app._relaunch_handoff(None) is None
    # The real parser reads sys.argv when argv is None: the normalisation above is what it sees.
    assert app._parse_args(None).ui == "web"


# ---- marked handoffs that cannot be honoured ----------------------------------------------------

@pytest.mark.parametrize("reason", ["malformed-token", "missing", "oversized", "not-a-list",
                                    "not-strings", "io"])
def test_marked_invalid_handoff_is_a_finite_nonzero_startup_failure(monkeypatch, launch, capsys,
                                                                    reason):
    def consume(exe_path, token):
        raise _StubError(reason, f"injected {reason}")

    _stub(monkeypatch, consume)
    code = app._relaunch_handoff(None)
    assert code == 1
    assert sys.argv == [str(launch)], "no default launch is silently substituted"
    assert ENV not in os.environ
    err = capsys.readouterr().err
    assert reason in err and "launch settings" in err
    crumb = launch.parent / "cyber-controller.exe.update-failed"
    assert crumb.exists() and reason in crumb.read_text(encoding="ascii")


def test_failure_report_tolerates_a_windowed_build_without_stderr(monkeypatch, launch):
    def consume(exe_path, token):
        raise _StubError("missing", "injected")

    _stub(monkeypatch, consume)
    monkeypatch.setattr(sys, "stderr", None)
    assert app._relaunch_handoff(None) == 1
    assert (launch.parent / "cyber-controller.exe.update-failed").exists()


def test_failure_report_tolerates_an_unwritable_breadcrumb(monkeypatch, launch, capsys):
    def consume(exe_path, token):
        raise _StubError("io", "injected")

    _stub(monkeypatch, consume)
    from src.core import self_update
    monkeypatch.setattr(self_update, "failed_update_marker",
                        lambda cur=None: str(launch.parent / "missing-dir" / "x.update-failed"))
    assert app._relaunch_handoff(None) == 1
    assert "injected" in capsys.readouterr().err


def test_repeated_consumption_is_a_marked_invalid_handoff(monkeypatch, launch):
    seen = []

    def consume(exe_path, token):
        seen.append(token)
        if len(seen) > 1:
            raise _StubError("missing", "already consumed")
        return ["--ui", "web"]

    _stub(monkeypatch, consume)
    assert app._relaunch_handoff(None) is None
    monkeypatch.setenv(ENV, TOKEN)
    monkeypatch.setattr(sys, "argv", [str(launch)])
    assert app._relaunch_handoff(None) == 1, "a second launch with the same token is refused"


def test_missing_relaunch_module_on_a_marked_launch_is_a_finite_failure(monkeypatch, launch,
                                                                         capsys):
    import src.core as core
    monkeypatch.delattr(core, "relaunch_args", raising=False)
    monkeypatch.setitem(sys.modules, "src.core.relaunch_args", None)   # import raises ImportError
    assert app._relaunch_handoff(None) == 1
    assert "unavailable" in capsys.readouterr().err and ENV not in os.environ


def test_a_failure_outside_the_module_contract_is_still_finite(monkeypatch, launch, capsys):
    # A defect or an unexpected payload in the shared module must not crash the launcher with a
    # traceback: it is reported through the same finite diagnostic as a contract error.
    def consume(exe_path, token):
        raise RecursionError("maximum recursion depth exceeded")

    _stub(monkeypatch, consume)
    assert app._relaunch_handoff(None) == 1
    err = capsys.readouterr().err
    assert "RecursionError" in err and "launch settings" in err
    assert (launch.parent / "cyber-controller.exe.update-failed").exists()
    assert sys.argv == [str(launch)] and ENV not in os.environ


def test_environment_name_matches_the_shared_module_when_present():
    module = pytest.importorskip("src.core.relaunch_args")
    assert module.TOKEN_ENV == app._RELAUNCH_TOKEN_ENV


# ---- main() ordering ----------------------------------------------------------------------------

def test_main_returns_the_handoff_failure_before_ordinary_startup(monkeypatch, launch):
    _no_ordinary_startup(monkeypatch)
    monkeypatch.setattr(app, "_relaunch_handoff", lambda argv: 7)
    assert app.main() == 7


def test_main_dispatches_restored_arguments_to_the_first_consumer(monkeypatch, launch):
    # A restored --_smoke-startup reaches the smoke dispatcher (an inert fixture runner) through
    # sys.argv, proving the restored arguments are what main() consumes; the runner is a double.
    _no_ordinary_startup(monkeypatch)
    consume, _ = _consume_ok(["--_smoke-startup"])
    _stub(monkeypatch, consume)
    smoke = types.ModuleType("src.ui.packaged_smoke")
    smoke.run = lambda: 42
    monkeypatch.setitem(sys.modules, "src.ui.packaged_smoke", smoke)
    assert app.main() == 42
    assert ENV not in os.environ


def test_children_spawned_after_the_hook_cannot_see_the_token(monkeypatch, launch):
    consume, _ = _consume_ok(["--ui", "web"])
    _stub(monkeypatch, consume)
    app._relaunch_handoff(None)
    # subprocess inherits os.environ when env is not given; the token is gone from it.
    assert ENV not in dict(os.environ)
