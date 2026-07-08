"""clear_cli must tell the truth about the next launch when an encrypted vault is present.

Every normal gate-setup path provisions the vault (set_factor writes vault.hdr.json), so after
--clear-gate the vault still exists and enforce() fails closed on the next launch. clear_cli therefore
must NOT print "the app will start without prompting" in that state (a false promise); it must say the
app stays locked until the vault is removed and point at the exact files. With no vault, the original
prompt-free message is correct.
"""

from __future__ import annotations

import pytest

from src.security import access_gate
from src.security import vault


@pytest.fixture()
def isolated_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_GATE_CONFIG", str(tmp_path / "access_gate.json"))
    monkeypatch.setenv("CC_VAULT_DIR", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    return tmp_path


def test_clear_with_vault_does_not_promise_prompt_free_start(isolated_gate, capsys):
    vault.set_factor("password", b"correct horse battery staple")
    assert vault.exists()
    rc = access_gate.clear_cli()
    out = capsys.readouterr().out
    assert rc == 0
    assert "start without prompting" not in out, out
    assert "LOCKED" in out and "vault" in out.lower()
    assert str(vault._hdr_path()) in out  # points the owner at the exact file to remove


def test_clear_without_vault_keeps_prompt_free_message(isolated_gate, capsys):
    assert not vault.exists()
    rc = access_gate.clear_cli()
    out = capsys.readouterr().out
    assert rc == 0
    assert "start without prompting" in out
