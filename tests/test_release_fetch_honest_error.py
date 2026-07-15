"""A github_release fetch that 404s must explain WHY (private/missing channel), not bare-404.

Owner bug (1.8.0): "Flash LxveOS" on a CYD 3.5" logged only `could not fetch release: HTTP Error
404: Not Found`. Root cause: `LxveAce/lxveos` is a PRIVATE repo, and a distributed CyberController
build has no GitHub token — GitHub returns 404 (not 403) to an unauthenticated client for a private
resource, so the marketed feature can never fetch its firmware. A bare "404 Not Found" reads like a
CC bug; the flasher should name the real cause (private/unreachable channel or missing tag) + the
way forward (public channel, or a local-file flash).

The message builder is a pure staticmethod, so this asserts the copy without a live flash.
"""
from __future__ import annotations

import pytest

fe = pytest.importorskip("src.core.flash_engine")
_lines = fe.FlashEngine._release_fetch_error_lines


def test_404_adds_an_honest_private_channel_hint():
    exc = Exception("HTTP Error 404: Not Found")
    out = _lines("lxveos", exc)
    assert len(out) == 2
    assert out[0] == "[error] could not fetch release: HTTP Error 404: Not Found"
    hint = out[1]
    assert hint.startswith("[hint]")
    assert "lxveos" in hint           # names the firmware
    assert "private" in hint          # the real cause
    assert "404" in hint
    assert "public" in hint           # the way forward
    assert "local-file flash" in hint  # the workaround that actually exists (profile.local_path)


def test_non_404_error_stays_a_single_bare_line():
    """A transient/offline error (not a 404) must NOT get the private-channel hint — that would
    misdiagnose a network blip as a private repo."""
    out = _lines("marauder", Exception("Connection reset by peer"))
    assert len(out) == 1
    assert out[0] == "[error] could not fetch release: Connection reset by peer"


def test_prefix_is_honored_for_the_offline_fallback_path():
    """The offline-download path logs under its own tag (e.g. 'dfu'/'uf2'); the raw-error line uses
    the given prefix while the hint stays a [hint]."""
    out = _lines("lxveos", Exception("HTTP Error 404: Not Found"), "uf2")
    assert out[0].startswith("[uf2] could not fetch release:")
    assert out[1].startswith("[hint]")
