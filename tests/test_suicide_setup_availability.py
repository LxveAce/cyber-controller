"""Fail-closed packaging behavior for the optional Dead Man's Switch runtime."""

import getpass
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core import suicide_setup


def test_cli_reports_unavailable_before_collecting_secrets(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(suicide_setup, "_HOST", tmp_path / "missing-host")
    monkeypatch.setattr(
        "builtins.input",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )
    monkeypatch.setattr(
        getpass,
        "getpass",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not collect password")),
    )

    assert suicide_setup.run_cli() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    output = captured.err
    assert "Provisioning unavailable" in output
    assert "submodule" in output


def test_installed_layout_message_does_not_prescribe_only_git(monkeypatch, tmp_path):
    fake_module = tmp_path / "target" / "src" / "core" / "suicide_setup.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("", encoding="utf-8")

    monkeypatch.setattr(suicide_setup, "_HOST", Path(tmp_path, "missing-host"))
    monkeypatch.setattr(suicide_setup, "__file__", str(fake_module))

    message = suicide_setup._runtime_unavailable_message()
    assert "unavailable in this installation" in message
    assert "Use a distribution" in message


def test_source_checkout_message_names_submodule_fix(monkeypatch, tmp_path):
    fake_module = tmp_path / "checkout" / "src" / "core" / "suicide_setup.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.write_text("", encoding="utf-8")
    (tmp_path / "checkout" / ".gitmodules").write_text("", encoding="utf-8")

    monkeypatch.setattr(suicide_setup, "_HOST", Path(tmp_path, "missing-host"))
    monkeypatch.setattr(suicide_setup, "__file__", str(fake_module))

    message = suicide_setup._runtime_unavailable_message()
    assert "source checkout needs" in message
    assert "git submodule update --init deadmans-switch" in message


def test_runtime_rejects_present_but_unimportable_provisioner(monkeypatch, tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    (host / "provision.py").write_text("raise ImportError('missing runtime dependency')\n", encoding="utf-8")
    monkeypatch.setattr(suicide_setup, "_HOST", host)

    available, reason = suicide_setup.dms_runtime_status()
    assert available is False
    assert "missing runtime dependency" in reason


def test_runtime_rejects_missing_nvs_generator(monkeypatch, tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    (host / "provision.py").write_text(
        "def build_bundle(*args): pass\n"
        "def _find_nvs_gen(_nvs_gen_dir=None): raise RuntimeError('generator missing')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(suicide_setup, "_HOST", host)

    available, reason = suicide_setup.dms_runtime_status()
    assert available is False
    assert "NVS generator is unavailable" in reason
    assert "generator missing" in reason


def test_runtime_rejects_incomplete_partition_data(monkeypatch, tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    (host / "provision.py").write_text(
        "def build_bundle(*args): pass\n"
        "class Generator:\n"
        "    @staticmethod\n"
        "    def generate(*args): pass\n"
        "def _find_nvs_gen(_nvs_gen_dir=None): return ('module', Generator())\n",
        encoding="utf-8",
    )
    empty_parts = tmp_path / "parts"
    empty_parts.mkdir()
    monkeypatch.setattr(suicide_setup, "_HOST", host)
    monkeypatch.setattr(suicide_setup, "_LOCAL_PARTS", empty_parts)
    monkeypatch.setattr(suicide_setup, "_PARTS", empty_parts)

    available, reason = suicide_setup.dms_runtime_status()
    assert available is False
    assert "partition data is incomplete" in reason
    assert "suicide_4MB.csv" in reason


def _patch_complete_runtime(monkeypatch, tmp_path, generator):
    table = tmp_path / "partitions.csv"
    table.write_text("# synthetic availability fixture\n", encoding="utf-8")
    parts = {
        "guardcfg": {"subtype": "nvs", "offset": 0x9000, "size": 0x3000},
        "otadata": {"subtype": "ota", "offset": 0xE000, "size": 0x2000},
        "factory": {"subtype": "factory", "offset": 0x10000, "size": 0x100000},
        "ota_0": {"subtype": "ota_0", "offset": 0x110000, "size": 0x100000},
    }
    provisioner = SimpleNamespace(
        build_bundle=lambda *_args: None,
        _find_nvs_gen=lambda _nvs_gen_dir=None: generator,
        parse_partitions_csv=lambda _path: parts,
        require_partition=lambda parsed, name: parsed[name],
        GUARDCFG_PART="guardcfg",
        OTADATA_PART="otadata",
    )
    monkeypatch.setattr(suicide_setup, "_load_provision", lambda: provisioner)
    monkeypatch.setattr(suicide_setup, "partitions_csv", lambda _cfg: table)
    return provisioner


def test_runtime_rejects_importable_module_with_no_usable_interface(monkeypatch, tmp_path):
    module = SimpleNamespace(__name__="synthetic_broken_nvs_parent")
    _patch_complete_runtime(monkeypatch, tmp_path, ("module", module))
    monkeypatch.setattr(
        suicide_setup.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, b"missing leaf dependency"),
    )

    available, reason = suicide_setup.dms_runtime_status()

    assert available is False
    assert "rejected its generate interface" in reason
    assert "missing leaf dependency" in reason


def test_runtime_accepts_nested_callable_generator_without_cli_probe(monkeypatch, tmp_path):
    module = SimpleNamespace(nvs_part_gen=SimpleNamespace(generate=lambda *_args: None))
    _patch_complete_runtime(monkeypatch, tmp_path, ("module", module))
    monkeypatch.setattr(
        suicide_setup.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not launch CLI")),
    )

    assert suicide_setup.dms_runtime_status() == (True, "ready")


def test_runtime_accepts_probed_cli_only_generator(monkeypatch, tmp_path):
    module = SimpleNamespace(__name__="synthetic_cli_nvs")
    _patch_complete_runtime(monkeypatch, tmp_path, ("module", module))
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, b"usage: synthetic_cli_nvs generate")

    monkeypatch.setattr(suicide_setup.subprocess, "run", fake_run)

    assert suicide_setup.dms_runtime_status() == (True, "ready")
    assert calls[0][0] == [
        suicide_setup.sys.executable,
        "-m",
        "synthetic_cli_nvs",
        "generate",
        "--help",
    ]
    assert calls[0][1]["stdin"] is subprocess.DEVNULL
    assert calls[0][1]["timeout"] == 5


def test_runtime_rejects_malformed_script_descriptor_without_raising(monkeypatch, tmp_path):
    _patch_complete_runtime(monkeypatch, tmp_path, ("script", object()))
    monkeypatch.setattr(
        suicide_setup.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not launch CLI")),
    )

    available, reason = suicide_setup.dms_runtime_status()

    assert available is False
    assert "invalid generator script path" in reason


def test_runtime_rejects_wrong_callable_signature_when_cli_fallback_fails(monkeypatch, tmp_path):
    module = SimpleNamespace(__name__="synthetic_wrong_signature", generate=lambda: None)
    _patch_complete_runtime(monkeypatch, tmp_path, ("module", module))
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, b"no runnable module")

    monkeypatch.setattr(suicide_setup.subprocess, "run", fake_run)

    available, reason = suicide_setup.dms_runtime_status()

    assert available is False
    assert "rejected its generate interface" in reason
    assert calls == [
        [
            suicide_setup.sys.executable,
            "-m",
            "synthetic_wrong_signature",
            "generate",
            "--help",
        ]
    ]


def test_runtime_probes_nested_noncallable_generator_fallback(monkeypatch, tmp_path):
    nested = SimpleNamespace(__name__="synthetic_nested_cli", generate=object())
    module = SimpleNamespace(__name__="synthetic_parent_cli", nvs_part_gen=nested)
    _patch_complete_runtime(monkeypatch, tmp_path, ("module", module))
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, b"nested generate usage")

    monkeypatch.setattr(suicide_setup.subprocess, "run", fake_run)

    assert suicide_setup.dms_runtime_status() == (True, "ready")
    assert calls == [
        [
            suicide_setup.sys.executable,
            "-m",
            "synthetic_nested_cli",
            "generate",
            "--help",
        ]
    ]


def test_runtime_rejects_cli_only_generator_in_frozen_app(monkeypatch, tmp_path):
    module = SimpleNamespace(__name__="synthetic_cli_nvs")
    _patch_complete_runtime(monkeypatch, tmp_path, ("module", module))
    monkeypatch.setattr(suicide_setup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        suicide_setup.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not launch app exe")),
    )

    available, reason = suicide_setup.dms_runtime_status()

    assert available is False
    assert "unavailable in this frozen application" in reason


def test_runtime_accepts_callable_generator_in_frozen_app(monkeypatch, tmp_path):
    module = SimpleNamespace(generate=lambda _namespace: None)
    _patch_complete_runtime(monkeypatch, tmp_path, ("module", module))
    monkeypatch.setattr(suicide_setup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        suicide_setup.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not launch app exe")),
    )

    assert suicide_setup.dms_runtime_status() == (True, "ready")


def test_runtime_rejects_script_generator_in_frozen_app(monkeypatch, tmp_path):
    script = tmp_path / "nvs_partition_gen.py"
    script.write_text("# synthetic CLI-only generator\n", encoding="utf-8")
    _patch_complete_runtime(monkeypatch, tmp_path, ("script", script))
    monkeypatch.setattr(suicide_setup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        suicide_setup.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not launch app exe")),
    )

    available, reason = suicide_setup.dms_runtime_status()

    assert available is False
    assert "unavailable in this frozen application" in reason


@pytest.mark.parametrize(
    "failure",
    [OSError("cannot launch generator"), subprocess.TimeoutExpired(["generator"], 5)],
)
def test_runtime_cli_probe_failures_are_unavailable(monkeypatch, tmp_path, failure):
    module = SimpleNamespace(__name__="synthetic_cli_nvs")
    _patch_complete_runtime(monkeypatch, tmp_path, ("module", module))

    def fail_probe(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(suicide_setup.subprocess, "run", fail_probe)

    available, reason = suicide_setup.dms_runtime_status()

    assert available is False
    assert "could not be validated" in reason


def test_runtime_rejects_incompatible_build_bundle_before_prompt(monkeypatch, tmp_path):
    provisioner = _patch_complete_runtime(
        monkeypatch,
        tmp_path,
        ("module", SimpleNamespace(generate=lambda _namespace: None)),
    )
    provisioner.build_bundle = lambda: None
    monkeypatch.setattr(
        "builtins.input",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )
    monkeypatch.setattr(
        getpass,
        "getpass",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not collect password")),
    )

    assert suicide_setup.run_cli() == 1


def test_runtime_rejects_incompatible_generator_finder_before_prompt(monkeypatch, tmp_path):
    provisioner = _patch_complete_runtime(
        monkeypatch,
        tmp_path,
        ("module", SimpleNamespace(generate=lambda _namespace: None)),
    )
    provisioner._find_nvs_gen = lambda: (
        "module",
        SimpleNamespace(generate=lambda _namespace: None),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )
    monkeypatch.setattr(
        getpass,
        "getpass",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not collect password")),
    )

    assert suicide_setup.run_cli() == 1


@pytest.mark.parametrize("generator_kind", ["coroutine", "async-generator", "generator"])
def test_runtime_rejects_deferred_generator_interfaces(monkeypatch, tmp_path, generator_kind):
    async def coroutine_generate(_namespace):
        return None

    async def async_generator_generate(_namespace):
        yield None

    def generator_generate(_namespace):
        yield None

    generators = {
        "coroutine": coroutine_generate,
        "async-generator": async_generator_generate,
        "generator": generator_generate,
    }
    module = SimpleNamespace(generate=generators[generator_kind])
    _patch_complete_runtime(monkeypatch, tmp_path, ("module", module))
    monkeypatch.setattr(
        suicide_setup.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not probe CLI")),
    )

    available, reason = suicide_setup.dms_runtime_status()

    assert available is False
    assert "does not execute synchronously" in reason


def test_runtime_rejects_malformed_partition_table_before_prompt(monkeypatch, tmp_path):
    provisioner = _patch_complete_runtime(
        monkeypatch,
        tmp_path,
        ("module", SimpleNamespace(generate=lambda _namespace: None)),
    )
    provisioner.parse_partitions_csv = lambda _path: (_ for _ in ()).throw(
        ValueError("contained no partition rows")
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )
    monkeypatch.setattr(
        getpass,
        "getpass",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not collect password")),
    )

    assert suicide_setup.run_cli() == 1
