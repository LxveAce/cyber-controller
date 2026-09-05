"""Offline tests for scripts/release_preflight.py — the gate that keeps the staged release build
from ever mutating a published release. Every case feeds a canned release-list JSON (what
`gh api --paginate --slurp repos/…/releases` returns); no network, no gh."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "release_preflight", _ROOT / "scripts" / "release_preflight.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PF = _load()
TAG = "v2.0.1"


def _release(tag=TAG, draft: object = True, assets=()):
    return {"tag_name": tag, "draft": draft,
            "assets": [{"name": n} for n in assets]}


def _run(releases, *args, tag=TAG):
    return PF.main([tag, *args], stdin_text=json.dumps(releases))


# ── the core gate: exactly one EXISTING DRAFT release ──────────────────────────────────────────

def test_single_draft_release_passes():
    assert _run([_release()]) == 0


def test_published_release_is_rejected():
    """The whole point: a published target (e.g. a rerun after publish) must never be mutated."""
    assert _run([_release(draft=False)]) == 1


def test_missing_release_is_rejected():
    assert _run([_release(tag="v2.0.0", draft=False)]) == 1
    assert _run([]) == 1


def test_two_drafts_same_tag_are_rejected():
    """GitHub allows several drafts with the same pending tag; uploads could land on the wrong one."""
    assert _run([_release(), _release()]) == 1


def test_draft_flag_must_be_literal_true():
    assert _run([_release(draft=None)]) == 1
    assert _run([_release(draft="true")]) == 1


def test_malformed_tag_is_rejected():
    assert _run([_release(tag="master")], tag="master") == 1
    assert _run([_release(tag="v2.0.1; rm -rf /")], tag="v2.0.1; rm -rf /") == 1


def test_bad_json_is_rejected():
    assert PF.main([TAG], stdin_text="not json") == 1
    assert PF.main([TAG], stdin_text='{"tag_name": "v2.0.1"}') == 1  # object, not a list


def test_slurped_page_arrays_are_flattened():
    """`gh api --paginate --slurp` wraps each page in an outer array — both pages must be seen."""
    assert PF.main([TAG], stdin_text=json.dumps([[_release(tag="v2.0.0", draft=False)],
                                                 [_release()]])) == 0
    # ...including a published match hiding on page two.
    assert PF.main([TAG], stdin_text=json.dumps([[_release(tag="v2.0.0", draft=False)],
                                                 [_release(draft=False)]])) == 1


# ── asset-shape validation (the six-asset release) ─────────────────────────────────────────────

FIVE = [
    f"cyber-controller-{TAG}-windows-x64.exe",
    f"cyber-controller-{TAG}-windows-x64-setup.exe",
    f"cyber-controller-{TAG}-linux-x64",
    f"cyber-controller-{TAG}-linux-arm64",
    f"cyber-controller-{TAG}-macos-arm64",
]


def test_expected_binaries_match_workflow_names():
    assert PF.expected_binaries(TAG) == FIVE


def test_require_assets_passes_with_all_five():
    assert _run([_release(assets=FIVE)], "--require-assets") == 0


def test_require_assets_fails_on_any_missing_binary():
    for i in range(len(FIVE)):
        short = FIVE[:i] + FIVE[i + 1:]
        assert _run([_release(assets=short)], "--require-assets") == 1, f"missing {FIVE[i]} passed"


def test_require_checksums_needs_sha256sums():
    assert _run([_release(assets=FIVE)], "--require-assets", "--require-checksums") == 1
    assert _run([_release(assets=FIVE + ["SHA256SUMS.txt"])],
                "--require-assets", "--require-checksums") == 0


def test_extra_assets_do_not_break_validation():
    assert _run([_release(assets=FIVE + ["SHA256SUMS.txt", "notes.txt"])],
                "--require-assets", "--require-checksums") == 0


def test_assets_never_required_of_a_published_release():
    """Draft status is checked before asset shape — published stays untouchable even if complete."""
    assert _run([_release(draft=False, assets=FIVE + ["SHA256SUMS.txt"])],
                "--require-assets", "--require-checksums") == 1


# ── --list-expected (what the workflow downloads by exact name) ────────────────────────────────

def test_list_expected_prints_five_names(capsys):
    assert PF.main(["--list-expected", TAG]) == 0
    assert capsys.readouterr().out.split() == FIVE


def test_list_expected_rejects_bad_tag():
    assert PF.main(["--list-expected", "junk"]) == 1
