"""Distribution-resource declarations and installed-layout lookup."""

from __future__ import annotations

import zipfile
from pathlib import Path

from scripts import verify_distribution_resources as verify
from src.core import resources


def test_resource_path_falls_back_to_installed_share(monkeypatch, tmp_path):
    primary = tmp_path / "site-packages"
    prefix = tmp_path / "venv"
    installed = prefix / "share" / "cyber-controller" / "assets" / "cc-logo.png"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"logo")

    monkeypatch.setattr(resources, "_base_dir", lambda: primary)

    def fake_get_path(name):
        return str(primary if name in {"purelib", "platlib"} else prefix)

    monkeypatch.setattr(resources.sysconfig, "get_path", fake_get_path)

    assert resources.resource_path("assets", "cc-logo.png") == installed


def test_resource_path_supports_pip_target_layout(monkeypatch, tmp_path):
    target = tmp_path / "target"
    installed = target / "share" / "cyber-controller" / "docs" / "HOWTO.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("target install", encoding="utf-8")

    monkeypatch.setattr(resources, "_base_dir", lambda: target)
    monkeypatch.setattr(resources.site, "getusersitepackages", lambda: str(tmp_path / "elsewhere"))
    monkeypatch.setattr(resources.sys, "prefix", str(tmp_path / "system"))

    assert resources.resource_path("docs", "HOWTO.md") == installed


def test_pip_target_does_not_borrow_missing_resource_from_system(monkeypatch, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    system = tmp_path / "system"
    unrelated = system / "share" / "cyber-controller" / "assets" / "cc-logo.png"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"different installation")

    def fake_get_path(name):
        if name in {"purelib", "platlib"}:
            return str(system / "Lib" / "site-packages")
        return str(system)

    monkeypatch.setattr(resources, "_base_dir", lambda: target)
    monkeypatch.setattr(resources.site, "getusersitepackages", lambda: str(tmp_path / "user-site"))
    monkeypatch.setattr(resources.sysconfig, "get_path", fake_get_path)

    assert resources.resource_path("assets", "cc-logo.png") == target / "assets" / "cc-logo.png"


def test_nested_pip_target_does_not_borrow_from_outer_user_site(monkeypatch, tmp_path):
    user_base = tmp_path / "user"
    user_site = user_base / "Python312" / "site-packages"
    nested_target = user_site / "isolated-target"
    nested_target.mkdir(parents=True)
    unrelated = user_base / "share" / "cyber-controller" / "assets" / "cc-logo.png"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"outer user installation")

    monkeypatch.setattr(resources, "_base_dir", lambda: nested_target)
    monkeypatch.setattr(resources.site, "getusersitepackages", lambda: str(user_site))
    monkeypatch.setattr(resources.site, "getuserbase", lambda: str(user_base))

    assert (
        resources.resource_path("assets", "cc-logo.png")
        == nested_target / "assets" / "cc-logo.png"
    )


def test_resource_path_supports_pip_user_layout(monkeypatch, tmp_path):
    user_base = tmp_path / "user"
    user_site = user_base / "lib" / "site-packages"
    installed = user_base / "share" / "cyber-controller" / "assets" / "cc-logo.png"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"user install")

    monkeypatch.setattr(resources, "_base_dir", lambda: user_site)
    monkeypatch.setattr(resources.site, "getusersitepackages", lambda: str(user_site))
    monkeypatch.setattr(resources.site, "getuserbase", lambda: str(user_base))
    monkeypatch.setattr(resources.sys, "prefix", str(tmp_path / "system"))

    assert resources.resource_path("assets", "cc-logo.png") == installed


def test_resource_path_prefers_source_or_bundle_layout(monkeypatch, tmp_path):
    primary = tmp_path / "source"
    source_file = primary / "assets" / "cc-logo.png"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"source")

    prefix = tmp_path / "venv"
    installed = prefix / "share" / "cyber-controller" / "assets" / "cc-logo.png"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"installed")

    monkeypatch.setattr(resources, "_base_dir", lambda: primary)
    monkeypatch.setattr(resources.sys, "prefix", str(prefix))

    assert resources.resource_path("assets", "cc-logo.png") == source_file


def test_wheel_verifier_accepts_complete_synthetic_artifact(tmp_path):
    wheel = tmp_path / "cyber_controller-0-py3-none-any.whl"
    members = set(verify.REQUIRED_SUFFIXES)
    for prefix, minimum in verify.MINIMUM_PREFIX_COUNTS.items():
        members.update(f"{prefix}fixture-{index}.data" for index in range(minimum))
    with zipfile.ZipFile(wheel, "w") as archive:
        for member in members:
            archive.writestr(member, b"fixture")

    assert verify.missing_resources(wheel) == []


def test_wheel_verifier_reports_missing_and_truncated_groups(tmp_path):
    wheel = tmp_path / "cyber_controller-0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("src/config/profiles/only-one.json", b"{}")

    failures = verify.missing_resources(wheel)
    assert any("missing resource" in failure for failure in failures)
    assert any("src/config/profiles/" in failure for failure in failures)


def test_wheel_verifier_rejects_encrypted_executable_packs(tmp_path):
    wheel = tmp_path / "cyber_controller-0-py3-none-any.whl"
    members = set(verify.REQUIRED_SUFFIXES)
    for prefix, minimum in verify.MINIMUM_PREFIX_COUNTS.items():
        members.update(f"{prefix}fixture-{index}.data" for index in range(minimum))
    members.add("src/config/tools/aircrack-ng.pack")
    with zipfile.ZipFile(wheel, "w") as archive:
        for member in members:
            archive.writestr(member, b"fixture")

    failures = verify.missing_resources(wheel)
    assert failures == ["forbidden distribution member (.pack): src/config/tools/aircrack-ng.pack"]


def test_wheel_verifier_rejects_a_non_zip(tmp_path):
    not_a_wheel = Path(tmp_path, "broken.whl")
    not_a_wheel.write_text("not a zip", encoding="utf-8")

    failures = verify.missing_resources(not_a_wheel)
    assert len(failures) == 1
    assert "could not read wheel" in failures[0]
