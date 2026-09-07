"""The single-instance mutex in src.app._acquire_instance_lock, with an injected fake kernel32.

No OS mutex is created: ctypes.windll is replaced for the duration of each test, so the cases run
on every platform and never touch a real handle.
"""
from __future__ import annotations

import ctypes
import sys
import types

import pytest

from src import app

ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "CyberController_SingleInstance"


class FakeKernel32:
    def __init__(self):
        self.last_error = 0
        self.created = []

    def CreateMutexW(self, attributes, initial_owner, name):  # noqa: N802 (Win32 name)
        self.created.append((attributes, initial_owner, name))
        return 4242

    def GetLastError(self):  # noqa: N802 (Win32 name)
        return self.last_error


@pytest.fixture
def kernel32(monkeypatch):
    fake = FakeKernel32()
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=fake), raising=False)
    return fake


def test_non_windows_acquires_without_touching_ctypes(monkeypatch, kernel32):
    monkeypatch.setattr(sys, "platform", "linux")
    assert app._acquire_instance_lock() is True
    assert kernel32.created == []


def test_windows_first_instance_creates_the_named_mutex(monkeypatch, kernel32):
    monkeypatch.setattr(sys, "platform", "win32")
    assert app._acquire_instance_lock() is True
    assert kernel32.created == [(None, False, MUTEX_NAME)]


def test_windows_second_instance_is_refused(monkeypatch, kernel32):
    monkeypatch.setattr(sys, "platform", "win32")
    kernel32.last_error = ERROR_ALREADY_EXISTS
    assert app._acquire_instance_lock() is False
    assert kernel32.created == [(None, False, MUTEX_NAME)]
