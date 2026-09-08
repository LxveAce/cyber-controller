"""The optional ``--log-file`` must never end the launch in a traceback: a path the process cannot
create (its parent is a regular file, it is itself a directory, it sits on a drive that does not
exist, or Python rejects it outright) is reported once as a warning through the console and ring
handlers that are already attached, and startup continues without a file handler. A usable path
keeps exactly today's behaviour.

Inert: temporary paths only, the root logger's handlers and level restored after every test, no
process, no UI, no device.
"""
from __future__ import annotations

import logging
import os
import string
import sys

import pytest

from src import app


@pytest.fixture
def root_logger():
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    yield root
    for h in list(root.handlers):
        if h not in saved_handlers:
            root.removeHandler(h)
            h.close()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def _file_handlers_added(root, before):
    # Only handlers this call attached: the process may already carry a file handler (a NUL sink
    # installed elsewhere for the test session), which is not what these tests measure.
    return [h for h in root.handlers if isinstance(h, logging.FileHandler) and h not in before]


def _warnings(caplog):
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and "log file" in r.getMessage()]


def test_usable_path_attaches_one_file_handler(root_logger, tmp_path):
    target = tmp_path / "logs" / "cc.log"
    before = list(root_logger.handlers)
    app._setup_logging("INFO", str(target))
    handlers = _file_handlers_added(root_logger, before)
    assert len(handlers) == 1 and os.path.samefile(handlers[0].baseFilename, target)
    assert target.exists(), "the parent directory is still created for a usable path"


def test_empty_string_means_no_file_handler(root_logger):
    before = list(root_logger.handlers)
    app._setup_logging("INFO", "")
    assert _file_handlers_added(root_logger, before) == []


@pytest.mark.parametrize("shape", ["parent-is-a-file", "path-is-a-directory", "missing-drive",
                                   "embedded-nul"])
def test_unusable_path_warns_and_continues(root_logger, tmp_path, caplog, shape):
    if shape == "parent-is-a-file":
        blocker = tmp_path / "afile"
        blocker.write_text("x", encoding="ascii")
        target = str(blocker / "cc.log")
    elif shape == "path-is-a-directory":
        target = str(tmp_path)
    elif shape == "missing-drive":
        if sys.platform != "win32":
            pytest.skip("drive letters are a Windows shape")
        free = next((c for c in reversed(string.ascii_uppercase) if not os.path.exists(f"{c}:\\")),
                    None)
        if free is None:
            pytest.skip("every drive letter exists on this machine")
        target = f"{free}:\\cc-log-probe\\cc.log"
    else:
        target = str(tmp_path / "bad\0name.log")
    caplog.set_level(logging.WARNING)
    before = list(root_logger.handlers)
    app._setup_logging("INFO", target)   # must not raise
    assert _file_handlers_added(root_logger, before) == [], "no file handler for an unusable path"
    warned = _warnings(caplog)
    assert len(warned) == 1, "exactly one warning for the unusable path"
    message = warned[0].getMessage()
    assert target in message, "the warning names the requested path"
    assert "console" in message, "the warning says logging continues on the console"


def test_console_logging_still_works_after_an_unusable_path(root_logger, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    app._setup_logging("INFO", str(tmp_path))   # a directory: unusable
    logging.getLogger("cyber-controller").info("startup continues")
    assert any(r.getMessage() == "startup continues" for r in caplog.records)


def test_main_reaches_the_parser_past_an_unusable_log_file(monkeypatch, root_logger, tmp_path,
                                                            caplog):
    # Ordinary startup after the guard: main() gets past logging setup with a directory as the
    # log file, then the (patched) next step runs instead of a traceback escaping main().
    caplog.set_level(logging.WARNING)
    reached = {}

    def _stop_here():
        reached["install"] = True
        raise SystemExit(0)

    real_parse = app._parse_args
    monkeypatch.setattr(app, "_parse_args",
                        lambda argv: real_parse(["--ui", "web", "--log-file", str(tmp_path)]))
    import src.core.install as install
    monkeypatch.setattr(install, "reconcile", _stop_here)
    with pytest.raises(SystemExit):
        app.main([])
    assert reached.get("install") is True
    assert len(_warnings(caplog)) == 1
