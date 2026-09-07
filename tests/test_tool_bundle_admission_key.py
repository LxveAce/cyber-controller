"""Deterministic controls for the tool_bundle destination admission key (Windows extended-path stability).

These pin the corrected contract: one destination has ONE canonical reservation key no matter how
``os.path.realpath`` spelled it (with or without the extended-length device prefix), the captured
operation path and the reservation key never disagree (so two leases can't authorize one operation path),
and an extended-spelling lease stays borrowable via its own captured path. Pure admission-gate logic under
a mocked resolver plus one real extended local path; no extraction, subprocess, or device work. Backslashes
are built from ``chr(92)`` so the file carries no fragile escapes.
"""
from __future__ import annotations

import os

import pytest

from src.core import tool_bundle as tb

_BS = chr(92)
_EXT = _BS * 2 + "?" + _BS
_UNC = _EXT + "UNC" + _BS
_DRIVE_PLAIN = "C:" + _BS + "fixture" + _BS + "destination"
_DRIVE_EXT = _EXT + _DRIVE_PLAIN
_UNC_PLAIN = _BS * 2 + "fixture.invalid" + _BS + "share" + _BS + "destination"
_UNC_EXT = _UNC + "fixture.invalid" + _BS + "share" + _BS + "destination"
_PAIRS = [(_DRIVE_PLAIN, _DRIVE_EXT), (_UNC_PLAIN, _UNC_EXT)]
_IDS = ["drive", "unc"]
_win_only = pytest.mark.skipif(os.name != "nt", reason="the extended-length prefix is a Windows path form")


def _pin_resolver(monkeypatch, *literals):
    """Make realpath identity for the given literal spellings (deterministic, no real FS namespace)."""
    real = os.path.realpath
    keep = set(literals)
    monkeypatch.setattr(os.path, "realpath", lambda p: str(p) if str(p) in keep else real(p))


@_win_only
@pytest.mark.parametrize("plain,extended", _PAIRS, ids=_IDS)
def test_equivalent_spelling_is_one_key_and_one_reservation(monkeypatch, plain, extended):
    _pin_resolver(monkeypatch, plain, extended)
    assert tb.canonical_dest(plain) == tb.canonical_dest(extended)
    first = tb.acquire_destination(plain)
    try:
        with pytest.raises(tb.DestinationBusy):
            tb.acquire_destination(extended)   # same destination, other spelling -> refused
    finally:
        tb.release_destination(first)


@_win_only
@pytest.mark.parametrize("plain,extended", _PAIRS, ids=_IDS)
def test_extended_spelling_lease_is_borrowable_via_its_path(monkeypatch, plain, extended):
    _pin_resolver(monkeypatch, plain, extended)
    lease = tb.acquire_destination(extended)
    try:
        with tb._admission(lease.path, lease) as borrowed:
            assert borrowed is lease            # A5 borrow of a genuine extended-spelling lease succeeds
    finally:
        tb.release_destination(lease)


def test_key_and_captured_path_never_diverge_under_a_changing_resolver(monkeypatch):
    # DAC-01: the key must come from the ONE captured resolved string, never a second resolution. A
    # resolver that changes its answer between observations must not split path and key across two
    # destinations, and re-acquiring the lease's pinned path is refused (no two leases for one path).
    alias = "C:" + _BS + "fixture" + _BS + "alias"
    a = "C:" + _BS + "fixture" + _BS + "a"
    b = "C:" + _BS + "fixture" + _BS + "b"
    real = os.path.realpath
    seen = {"n": 0}

    def resolve(p):
        if str(p) == alias:
            seen["n"] += 1
            return a if seen["n"] == 1 else b
        if str(p) in (a, b):
            return str(p)
        return real(p)

    monkeypatch.setattr(os.path, "realpath", resolve)
    lease = tb.acquire_destination(alias)
    try:
        assert seen["n"] == 1, "acquire resolved the alias more than once"
        assert tb.canonical_dest(lease.path) == lease.dest
        with pytest.raises(tb.DestinationBusy):
            tb.acquire_destination(lease.path)
    finally:
        tb.release_destination(lease)


@_win_only
def test_real_extended_local_path_borrows_its_lease(tmp_path):
    plain = str(tmp_path / "destination")
    extended = _EXT + plain
    lease = tb.acquire_destination(extended)
    try:
        with tb._admission(lease.path, lease) as borrowed:
            assert borrowed is lease
    finally:
        tb.release_destination(lease)


def test_distinct_destinations_stay_separate_with_deferred_and_stale_release(tmp_path):
    a = tb.acquire_destination(str(tmp_path / "a"))
    b = tb.acquire_destination(str(tmp_path / "b"))
    try:
        assert a.path != b.path
        with tb.borrow_destination(a):
            assert tb.release_destination(a) is False        # deferred while a borrow is live
            with pytest.raises(tb.DestinationBusy):
                tb.acquire_destination(a.path)
        again = tb.acquire_destination(a.path)                # borrow exit honored the deferred release
        assert tb.release_destination(a) is False             # stale owner: no-op
        assert tb.release_destination(again) is True
    finally:
        tb.release_destination(a)
        tb.release_destination(b)
