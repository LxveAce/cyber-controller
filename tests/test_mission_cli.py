"""The app's `--mission` CLI entry (deep-dive audit / fix-list #9).

The mission RUNNER + planner shipped, but nothing reachable consumed them — a user running the app
could not run a mission. These lock in the `--mission FILE` app entry (a safe dry-run that delegates
to `src.core.mission_runner`), so the built+tested runner is actually reachable.
"""
from __future__ import annotations

import json

from src.app import _parse_args, _run_mission_cli, main


def _write_mission(tmp_path, name="recon"):
    mission = {
        "name": name,
        "devices": ["COM3"],
        "steps": [{"device_port": "COM3", "command": "scan", "condition": "always"}],
    }
    p = tmp_path / "m.json"
    p.write_text(json.dumps(mission), encoding="utf-8")
    return str(p)


def test_argparse_exposes_mission_flag():
    assert _parse_args(["--mission", "x.json"]).mission == "x.json"
    assert _parse_args([]).mission is None


def test_run_mission_cli_dry_runs_a_valid_mission(tmp_path, capsys):
    rc = _run_mission_cli(_write_mission(tmp_path))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Mission: recon" in out and "scan" in out
    assert "dry-run" in out  # the safe default — no device I/O


def test_run_mission_cli_bad_path_returns_nonzero(tmp_path):
    assert _run_mission_cli(str(tmp_path / "does-not-exist.json")) == 1


def test_main_dispatches_mission_and_never_launches_a_ui(tmp_path, capsys):
    # The whole point of #9: `main(["--mission", FILE])` must reach the runner (early-exit, no GUI).
    rc = main(["--mission", _write_mission(tmp_path, name="chain")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Mission: chain" in out
