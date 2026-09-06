"""Synthetic, fully-offline tests for the ``FirmwareArtifact@1`` consumer/store."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import struct
from pathlib import Path

import pytest

from src.core import firmware_artifact as fa
from tests.esp_image_vectors import merged_image_bytes

_MIB = 1024 * 1024
_BOARD_SPECS = {
    "bare_esp32_headless": ("esp32", 4 * _MIB, 0, "partitions/4mb.csv"),
    "cyd_2432S028_classic": ("esp32", 4 * _MIB, 0, "partitions/4mb.csv"),
    "cyd_3248S035_r": ("esp32", 4 * _MIB, 0, "partitions/4mb.csv"),
    "jc3248w535_s3_qspi": ("esp32s3", 8 * _MIB, 8 * _MIB, "partitions/8mb.csv"),
    "m5cardputer_v1": ("esp32s3", 8 * _MIB, 0, "partitions/8mb.csv"),
    "m5stickc_plus2": ("esp32", 8 * _MIB, 2 * _MIB, "partitions/8mb.csv"),
}
_NOTICE_NAMES = {
    "license": "LICENSE",
    "credits": "CREDITS.md",
    "third_party": "THIRD-PARTY-LICENSES.md",
    "responsible_use": "RESPONSIBLE-USE.md",
}


def _canonical_json(value) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _write(root: Path, relative: str, data: bytes) -> dict:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "path": relative,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _make_bundle(
    root: Path,
    *,
    version: str = "0.1.0-test",
    build_id: str = "build-one",
    built_at: str = "2026-09-06T12:00:00Z",
    seed: bytes = b"one",
) -> Path:
    root.mkdir()
    snapshot_boards = {}
    for board_id, (chip, flash_size, psram_size, partition_source) in _BOARD_SPECS.items():
        definition = {
            "chip": chip,
            "flash_size": f"{flash_size // _MIB}MB",
            "psram": psram_size > 0,
            "build": {
                "idf_target": chip,
                "partition_csv": partition_source,
            },
        }
        if psram_size:
            definition["psram_size"] = f"{psram_size // _MIB}MB"
        snapshot_boards[board_id] = definition
    snapshot = {
        "_schema": "LxveOSBoardManifest@1",
        "_version": "0.1.0-m0",
        "boards": snapshot_boards,
    }
    board_manifest = _write(
        root,
        "metadata/cyd_boards.json",
        _canonical_json(snapshot),
    )
    board_manifest["version"] = snapshot["_version"]

    notices = []
    for kind, filename in _NOTICE_NAMES.items():
        record = _write(root, f"metadata/{filename}", f"{kind}\n".encode())
        notices.append({"kind": kind, **record})

    vectors = {}
    partition_records = {}
    for chip, flash_size, _psram, source in _BOARD_SPECS.values():
        vector_key = (chip, flash_size)
        if vector_key not in vectors:
            vectors[vector_key] = merged_image_bytes(
                chip=chip,
                flash_size_bytes=flash_size,
                seed=seed,
            )
        packaged = f"metadata/{source}"
        if packaged not in partition_records:
            partition_records[packaged] = _write(root, packaged, vectors[vector_key][1])

    boards = []
    for board_id in fa.REQUIRED_BOARDS:
        chip, flash_size, psram_size, partition_source = _BOARD_SPECS[board_id]
        merged, _partition_csv, flasher_bytes = vectors[(chip, flash_size)]
        segment = _write(root, f"boards/{board_id}/{board_id}-merged.bin", merged)
        flasher = _write(
            root,
            f"boards/{board_id}/flasher_args.json",
            flasher_bytes,
        )
        dependencies = _write(
            root,
            f"boards/{board_id}/dependencies.lock",
            f"esp-idf==6.0.2\nboard={board_id}\n".encode(),
        )
        boards.append(
            {
                "board_id": board_id,
                "chip": chip,
                "flash_size_bytes": flash_size,
                "psram_size_bytes": psram_size,
                "partition": dict(partition_records[f"metadata/{partition_source}"]),
                "image_model": "merged-single-bin",
                "backend": "esptool",
                "segments": [{"role": "merged", "offset": 0, **segment}],
                "build_metadata": {
                    "flasher_args": {"operative": False, **flasher},
                    "dependencies_lock": dependencies,
                },
                "evidence": {
                    "built": {"status": "passed", "record": f"build {build_id}"},
                    "flashed": {"status": "not_run", "record": None},
                    "booted": {"status": "not_run", "record": None},
                    "tested": {"status": "not_run", "record": None},
                },
            }
        )
    manifest = {
        "schema": fa.SCHEMA,
        "product": {"id": fa.PRODUCT_ID, "version": version},
        "source": {
            "repository": fa.SOURCE_REPOSITORY,
            "commit": "a" * 40,
            "ref": "refs/heads/main",
        },
        "build": {
            "id": build_id,
            "built_at": built_at,
            "toolchain": fa.TOOLCHAIN,
        },
        "board_manifest": board_manifest,
        "notices": notices,
        "required_boards": list(fa.REQUIRED_BOARDS),
        "boards": boards,
    }
    (root / fa.MANIFEST_FILENAME).write_bytes(_canonical_json(manifest))
    return root


def _manifest(root: Path) -> dict:
    return json.loads((root / fa.MANIFEST_FILENAME).read_text(encoding="utf-8"))


def _rewrite_manifest(root: Path, manifest: dict) -> None:
    (root / fa.MANIFEST_FILENAME).write_bytes(_canonical_json(manifest))


def _refresh_record(root: Path, record: dict) -> None:
    data = root.joinpath(*record["path"].split("/")).read_bytes()
    record["size"] = len(data)
    record["sha256"] = hashlib.sha256(data).hexdigest()


def test_loads_complete_set_offline_and_identity_is_exact_manifest_hash(tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    artifact = fa.load_artifact_set(bundle / fa.MANIFEST_FILENAME)

    assert (
        artifact.identity
        == hashlib.sha256((bundle / fa.MANIFEST_FILENAME).read_bytes()).hexdigest()
    )
    assert tuple(board.board_id for board in artifact.boards) == fa.REQUIRED_BOARDS
    assert len(artifact.notices) == 4
    assert "subprocess" not in vars(fa) and "requests" not in vars(fa)


def test_selects_only_exact_compatible_board(tmp_path):
    artifact = fa.load_artifact(_make_bundle(tmp_path / "bundle"))
    selected = fa.select_board(artifact, "bare_esp32_headless", "esp32", 4 * _MIB, 0)

    assert selected.board_id == "bare_esp32_headless"
    assert selected.segments[0].offset == 0
    assert selected.segments[0].role == "merged"


@pytest.mark.parametrize(
    "board,chip,flash,psram,match",
    [
        ("missing-but-esp32", "esp32", 4 * _MIB, 0, "exact board"),
        ("bare_esp32_headless", "esp32s3", 4 * _MIB, 0, "chip mismatch"),
        ("bare_esp32_headless", "esp32", 8 * _MIB, 0, "flash-size mismatch"),
        ("bare_esp32_headless", "esp32", 4 * _MIB, 2 * _MIB, "PSRAM-size mismatch"),
    ],
)
def test_rejects_wrong_board_chip_flash_or_psram(tmp_path, board, chip, flash, psram, match):
    artifact = fa.load_artifact(_make_bundle(tmp_path / "bundle"))
    with pytest.raises(fa.ArtifactCompatibilityError, match=match):
        fa.select_board(artifact, board, chip, flash, psram)


@pytest.mark.parametrize(
    "bad_path",
    [
        "../escape.bin",
        "/absolute.bin",
        "C:/drive.bin",
        "boards\\evil.bin",
        "a//b.bin",
        ".",
        "a/./b.bin",
    ],
)
def test_rejects_traversal_absolute_and_nonportable_paths(tmp_path, bad_path):
    bundle = _make_bundle(tmp_path / "bundle")
    manifest = _manifest(bundle)
    manifest["boards"][0]["segments"][0]["path"] = bad_path
    _rewrite_manifest(bundle, manifest)

    with pytest.raises(fa.ArtifactIntegrityError):
        fa.load_artifact(bundle)


def test_rejects_symlinked_declared_file(tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    segment = bundle / "boards/bare_esp32_headless/bare_esp32_headless-merged.bin"
    target = tmp_path / "outside.bin"
    target.write_bytes(segment.read_bytes())
    segment.unlink()
    try:
        segment.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(fa.ArtifactIntegrityError, match="symlink"):
        fa.load_artifact(bundle)


def test_rejects_missing_and_extra_files(tmp_path):
    missing = _make_bundle(tmp_path / "missing")
    (missing / "metadata/LICENSE").unlink()
    with pytest.raises(fa.ArtifactIntegrityError, match="missing"):
        fa.load_artifact(missing)

    extra = _make_bundle(tmp_path / "extra")
    (extra / "surprise.bin").write_bytes(b"extra")
    with pytest.raises(fa.ArtifactIntegrityError, match="extra"):
        fa.load_artifact(extra)


def test_rejects_case_colliding_tree_entries(tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    collision = bundle / "metadata/license"
    collision.write_bytes(b"collision")
    if collision.samefile(bundle / "metadata/LICENSE"):
        pytest.skip("filesystem is case-insensitive")

    with pytest.raises(fa.ArtifactIntegrityError, match="case-colliding"):
        fa.load_artifact(bundle)


@pytest.mark.parametrize("mode", ["tamper", "truncate"])
def test_rejects_tampered_or_truncated_firmware(tmp_path, mode):
    bundle = _make_bundle(tmp_path / "bundle")
    segment = bundle / "boards/bare_esp32_headless/bare_esp32_headless-merged.bin"
    if mode == "tamper":
        data = segment.read_bytes()
        segment.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    else:
        segment.write_bytes(segment.read_bytes()[:-1])

    with pytest.raises(fa.ArtifactIntegrityError, match="mismatch"):
        fa.verify_artifact(bundle)


def test_rejects_unknown_schema_fields_and_duplicate_json_keys(tmp_path):
    extra = _make_bundle(tmp_path / "extra")
    manifest = _manifest(extra)
    manifest["unexpected"] = True
    _rewrite_manifest(extra, manifest)
    with pytest.raises(fa.ArtifactSchemaError, match="keys mismatch"):
        fa.load_artifact(extra)

    duplicate = _make_bundle(tmp_path / "duplicate")
    raw = (duplicate / fa.MANIFEST_FILENAME).read_text(encoding="utf-8")
    raw = raw.replace("{\n", '{\n  "schema": "FirmwareArtifact@1",\n', 1)
    (duplicate / fa.MANIFEST_FILENAME).write_text(raw, encoding="utf-8")
    with pytest.raises(fa.ArtifactSchemaError, match="duplicate JSON key"):
        fa.load_artifact(duplicate)


@pytest.mark.parametrize(
    "raw",
    [
        b"[" * (fa._MAX_JSON_NESTING + 1) + b"0" + b"]" * (fa._MAX_JSON_NESTING + 1),
        b'{"value":' + b"9" * 5000 + b"}",
    ],
    ids=["excessive-nesting", "huge-integer"],
)
def test_json_resource_failures_are_normalized(tmp_path, raw):
    bundle = _make_bundle(tmp_path / "bundle")
    (bundle / fa.MANIFEST_FILENAME).write_bytes(raw)

    with pytest.raises(fa.ArtifactSchemaError):
        fa.load_artifact(bundle)


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda m: m["source"].__setitem__("commit", "A" * 40), "40 lowercase"),
        (lambda m: m["source"].__setitem__("commit", "a" * 39), "40 lowercase"),
        (lambda m: m["source"].__setitem__("ref", ""), "non-empty"),
        (lambda m: m["source"].__setitem__("ref", "refs/heads/\x01main"), "control"),
        (lambda m: m["build"].__setitem__("built_at", "2026-09-06"), "RFC3339"),
        (lambda m: m["build"].__setitem__("toolchain", "esp-idf-latest"), "toolchain"),
        (
            lambda m: m.__setitem__("required_boards", list(reversed(fa.REQUIRED_BOARDS))),
            "required_boards",
        ),
    ],
)
def test_rejects_source_build_and_board_set_metadata(tmp_path, mutation, match):
    bundle = _make_bundle(tmp_path / "bundle")
    manifest = _manifest(bundle)
    mutation(manifest)
    _rewrite_manifest(bundle, manifest)
    with pytest.raises(fa.ArtifactSchemaError, match=match):
        fa.load_artifact(bundle)


@pytest.mark.parametrize(
    "status,record",
    [("maybe", None), ("passed", None), ("failed", ""), ("not_run", "claimed")],
)
def test_rejects_nonfinite_or_inconsistent_evidence(tmp_path, status, record):
    bundle = _make_bundle(tmp_path / "bundle")
    manifest = _manifest(bundle)
    manifest["boards"][0]["evidence"]["tested"] = {
        "status": status,
        "record": record,
    }
    _rewrite_manifest(bundle, manifest)
    with pytest.raises(fa.ArtifactSchemaError):
        fa.load_artifact(bundle)


def test_compatibility_is_derived_from_hashed_board_snapshot(tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    snapshot_path = bundle / "metadata/cyd_boards.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["boards"]["bare_esp32_headless"]["chip"] = "esp32s3"
    snapshot_path.write_bytes(_canonical_json(snapshot))
    manifest = _manifest(bundle)
    _refresh_record(bundle, manifest["board_manifest"])
    _rewrite_manifest(bundle, manifest)

    with pytest.raises(fa.ArtifactSchemaError, match="idf_target disagree"):
        fa.load_artifact(bundle)


def test_partition_path_must_derive_from_board_snapshot(tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    manifest = _manifest(bundle)
    manifest["boards"][0]["partition"] = dict(manifest["boards"][3]["partition"])
    _rewrite_manifest(bundle, manifest)

    with pytest.raises(fa.ArtifactSchemaError, match="partition path disagrees"):
        fa.load_artifact(bundle)


def test_notice_kinds_are_complete_and_semantically_bound_to_paths(tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    manifest = _manifest(bundle)
    manifest["notices"][0]["kind"] = "credits"
    _rewrite_manifest(bundle, manifest)
    with pytest.raises(fa.ArtifactSchemaError, match="must use path|unknown or duplicated"):
        fa.load_artifact(bundle)


def test_rejects_boolean_offset_and_image_larger_than_flash(tmp_path):
    boolean = _make_bundle(tmp_path / "boolean")
    manifest = _manifest(boolean)
    manifest["boards"][0]["segments"][0]["offset"] = False
    _rewrite_manifest(boolean, manifest)
    with pytest.raises(fa.ArtifactSchemaError, match="integer offset=0"):
        fa.load_artifact(boolean)

    oversized = _make_bundle(tmp_path / "oversized")
    manifest = _manifest(oversized)
    manifest["boards"][0]["segments"][0]["size"] = 4 * _MIB + 1
    _rewrite_manifest(oversized, manifest)
    with pytest.raises(fa.ArtifactSchemaError, match="flash capacity"):
        fa.load_artifact(oversized)


def test_rejects_partition_outside_partition_tree_and_false_psram_size(tmp_path):
    outside = _make_bundle(tmp_path / "outside")
    snapshot_path = outside / "metadata/cyd_boards.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["boards"]["bare_esp32_headless"]["build"]["partition_csv"] = "other.csv"
    snapshot_path.write_bytes(_canonical_json(snapshot))
    manifest = _manifest(outside)
    _refresh_record(outside, manifest["board_manifest"])
    _rewrite_manifest(outside, manifest)
    with pytest.raises(fa.ArtifactSchemaError, match="below 'partitions/'"):
        fa.load_artifact(outside)

    stray = _make_bundle(tmp_path / "stray")
    snapshot_path = stray / "metadata/cyd_boards.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["boards"]["bare_esp32_headless"]["psram_size"] = "0MB"
    snapshot_path.write_bytes(_canonical_json(snapshot))
    manifest = _manifest(stray)
    _refresh_record(stray, manifest["board_manifest"])
    _rewrite_manifest(stray, manifest)
    with pytest.raises(fa.ArtifactSchemaError, match="must be absent"):
        fa.load_artifact(stray)


def test_complete_verification_retains_exact_firmware_only_for_semantic_validation(
    tmp_path, monkeypatch
):
    bundle = _make_bundle(tmp_path / "bundle")
    original_read = fa._read_regular_file
    calls = []

    def observe_read(root, record, *, retain=True, max_bytes=None):
        calls.append((record.path, retain))
        return original_read(root, record, retain=retain, max_bytes=max_bytes)

    monkeypatch.setattr(fa, "_read_regular_file", observe_read)
    fa.load_artifact(bundle)

    assert ("metadata/cyd_boards.json", True) in calls
    assert all(retain for path, retain in calls if path.endswith("-merged.bin"))
    assert all(retain for path, retain in calls if path.endswith("flasher_args.json"))
    assert all(retain for path, retain in calls if path.endswith(".csv"))


@pytest.mark.parametrize("retain", [False, True])
def test_regular_file_growth_is_bounded_before_excess_is_retained(tmp_path, monkeypatch, retain):
    bundle = _make_bundle(tmp_path / "bundle")
    artifact = fa.load_artifact(bundle)
    record = artifact.board("bare_esp32_headless").segments[0].file
    path = bundle.joinpath(*record.path.split("/"))
    original_read = fa.os.read
    appended = False

    def append_before_first_read(fd, size):
        nonlocal appended
        if not appended:
            with path.open("ab") as handle:
                handle.write(b"growth")
            appended = True
        return original_read(fd, size)

    monkeypatch.setattr(fa.os, "read", append_before_first_read)
    with pytest.raises(fa.ArtifactIntegrityError, match="grew beyond its declared size"):
        fa._read_regular_file(bundle, record, retain=retain)

    assert appended is True
    assert path.stat().st_size > record.size


def test_store_import_is_content_addressed_idempotent_and_complete(tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    store = fa.ArtifactStore(tmp_path / "store")
    first = store.import_set(bundle)
    second = store.import_artifact(bundle)

    assert first.identity == second.identity
    assert first.root == store.root / first.identity
    assert fa.load_artifact(first.root).identity == first.identity
    assert not any(path.name.startswith(".incoming-") for path in store.root.iterdir())


def test_import_write_failure_preserves_every_existing_good_set(tmp_path, monkeypatch):
    old = _make_bundle(
        tmp_path / "old", version="old", built_at="2026-09-05T12:00:00Z", seed=b"old"
    )
    new = _make_bundle(
        tmp_path / "new", version="new", built_at="2026-09-06T12:00:00Z", seed=b"new"
    )
    store = fa.ArtifactStore(tmp_path / "store")
    old_stored = store.import_set(old)
    original_copy = fa._copy_verified_file
    calls = {"count": 0}

    def fail_during_copy(root, record, target):
        calls["count"] += 1
        if calls["count"] == 3:
            raise OSError("synthetic disk full")
        return original_copy(root, record, target)

    monkeypatch.setattr(fa, "_copy_verified_file", fail_during_copy)
    with pytest.raises(fa.ArtifactStoreError, match="disk full"):
        store.import_set(new)

    assert tuple(item.identity for item in store.scan()) == (old_stored.identity,)
    assert not any(path.name.startswith(".incoming-") for path in store.root.iterdir())


def test_publish_failure_preserves_every_existing_good_set(tmp_path, monkeypatch):
    old = _make_bundle(
        tmp_path / "old", version="old", built_at="2026-09-05T12:00:00Z", seed=b"old"
    )
    new = _make_bundle(
        tmp_path / "new", version="new", built_at="2026-09-06T12:00:00Z", seed=b"new"
    )
    store = fa.ArtifactStore(tmp_path / "store")
    old_stored = store.import_set(old)

    def fail_publication(stage, destination):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(fa, "_publish_no_replace", fail_publication)
    with pytest.raises(fa.ArtifactStoreError, match="publication failure"):
        store.import_set(new)

    assert tuple(item.identity for item in store.scan()) == (old_stored.identity,)
    assert not any(path.name.startswith(".incoming-") for path in store.root.iterdir())


def test_import_losing_race_to_empty_identity_directory_preserves_winner(tmp_path, monkeypatch):
    bundle = _make_bundle(tmp_path / "bundle")
    source = fa.load_artifact(bundle)
    store = fa.ArtifactStore(tmp_path / "store")
    destination = store.root / source.identity
    original_publish = fa._publish_no_replace
    raced_stat = None

    def race(stage, target):
        nonlocal raced_stat
        assert target == destination
        target.mkdir()
        raced_stat = target.stat()
        return original_publish(stage, target)

    monkeypatch.setattr(fa, "_publish_no_replace", race)
    with pytest.raises(fa.ArtifactStoreError, match="corrupt immutable set"):
        store.import_set(bundle)

    assert raced_stat is not None
    after = destination.stat()
    assert destination.is_dir()
    assert list(destination.iterdir()) == []
    if raced_stat.st_ino and after.st_ino:
        assert os.path.samestat(raced_stat, after)
    assert not any(path.name.startswith(".incoming-") for path in store.root.iterdir())


@pytest.mark.parametrize("result", [0, -1], ids=["success", "destination-exists"])
def test_darwin_publish_uses_atomic_renamex_np_exclusive(tmp_path, monkeypatch, result):
    calls = []

    class FakeRename:
        argtypes = None
        restype = None

        def __call__(self, *args):
            calls.append(args)
            return result

    class FakeLibc:
        renamex_np = FakeRename()

    monkeypatch.setattr(fa.sys, "platform", "darwin")
    monkeypatch.setattr(fa.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())
    monkeypatch.setattr(fa.ctypes, "get_errno", lambda: errno.EEXIST)
    stage = tmp_path / "stage"
    destination = tmp_path / ("a" * 64)

    if result == 0:
        fa._publish_no_replace(stage, destination)
    else:
        with pytest.raises(fa.ArtifactStoreError, match="identity already exists"):
            fa._publish_no_replace(stage, destination)

    assert calls == [(os.fsencode(stage), os.fsencode(destination), fa._DARWIN_RENAME_EXCL)]


def test_import_reverifies_staged_copy_before_publication(tmp_path, monkeypatch):
    bundle = _make_bundle(tmp_path / "bundle")
    store = fa.ArtifactStore(tmp_path / "store")
    original_copy = fa._copy_verified_file
    changed = {"done": False}

    def corrupt_one_staged_copy(root, record, target):
        result = original_copy(root, record, target)
        if str(target).endswith("bare_esp32_headless-merged.bin"):
            Path(target).write_bytes(Path(target).read_bytes() + b"corrupt")
            changed["done"] = True
        return result

    monkeypatch.setattr(fa, "_copy_verified_file", corrupt_one_staged_copy)
    with pytest.raises(fa.ArtifactIntegrityError, match="mismatch"):
        store.import_set(bundle)

    assert changed["done"] is True
    assert store.scan() == ()


def test_corrupt_newer_set_falls_back_to_last_known_good(tmp_path):
    old = _make_bundle(
        tmp_path / "old", version="old", built_at="2026-09-05T12:00:00Z", seed=b"old"
    )
    new = _make_bundle(
        tmp_path / "new", version="new", built_at="2026-09-06T12:00:00Z", seed=b"new"
    )
    store = fa.ArtifactStore(tmp_path / "store")
    old_stored = store.import_set(old)
    new_stored = store.import_set(new)
    new_segment = new_stored.root / new_stored.board("bare_esp32_headless").segments[0].file.path
    new_segment.write_bytes(b"corrupt")

    resolved = store.resolve_bytes("bare_esp32_headless", "esp32", 4 * _MIB, 0)

    assert resolved.artifact_identity == old_stored.identity
    assert resolved.version == "old"
    assert b"old" in resolved.data


def test_semantically_invalid_newer_entry_cannot_suppress_last_known_good(tmp_path):
    old = _make_bundle(
        tmp_path / "old", version="old", built_at="2026-09-05T12:00:00Z", seed=b"old"
    )
    invalid = _make_bundle(
        tmp_path / "invalid", version="new", built_at="2026-09-06T12:00:00Z", seed=b"new"
    )
    invalid_manifest = _manifest(invalid)
    invalid_record = invalid_manifest["boards"][0]["segments"][0]
    invalid_image = invalid.joinpath(*invalid_record["path"].split("/"))
    changed = bytearray(invalid_image.read_bytes())
    changed[0] = 0
    invalid_image.write_bytes(changed)
    _refresh_record(invalid, invalid_record)
    _rewrite_manifest(invalid, invalid_manifest)

    store = fa.ArtifactStore(tmp_path / "store")
    old_stored = store.import_set(old)
    invalid_identity = hashlib.sha256((invalid / fa.MANIFEST_FILENAME).read_bytes()).hexdigest()
    shutil.copytree(invalid, store.root / invalid_identity)

    assert tuple(item.identity for item in store.scan()) == (old_stored.identity,)
    resolved = store.resolve_bytes("bare_esp32_headless", "esp32", 4 * _MIB, 0)
    assert resolved.artifact_identity == old_stored.identity


@pytest.mark.parametrize(
    "board_id,app_offset",
    [
        ("m5stickc_plus2", 0x20000),
        ("m5cardputer_v1", 0x20000),
    ],
)
def test_rejects_mapped_load_shift_after_inner_and_outer_hashes_are_recomputed(
    tmp_path, board_id, app_offset
):
    bundle = _make_bundle(tmp_path / "bundle")
    manifest = _manifest(bundle)
    board = next(row for row in manifest["boards"] if row["board_id"] == board_id)
    segment_record = board["segments"][0]
    image_path = bundle.joinpath(*segment_record["path"].split("/"))
    merged = bytearray(image_path.read_bytes())

    load_address = struct.unpack_from("<I", merged, app_offset + 24)[0]
    struct.pack_into("<I", merged, app_offset + 24, load_address + 4)
    # The independent fixture has a 256-byte descriptor segment and a four-byte entry segment.
    # Their checksum is at +303 and the appended digest covers through it, so repair the inner
    # digest as an adversary could before also repairing the outer artifact record.
    digest_start = app_offset + 304
    merged[digest_start : digest_start + 32] = hashlib.sha256(
        merged[app_offset:digest_start]
    ).digest()
    image_path.write_bytes(merged)
    _refresh_record(bundle, segment_record)
    _rewrite_manifest(bundle, manifest)

    with pytest.raises(fa.ArtifactIntegrityError, match="MMU alignment"):
        fa.load_artifact_set(bundle)


def test_deeply_nested_store_entry_cannot_suppress_last_known_good(tmp_path):
    source = _make_bundle(tmp_path / "source")
    store = fa.ArtifactStore(tmp_path / "store")
    good = store.import_set(source)
    malformed = store.root / ("f" * 64)
    malformed.mkdir()
    (malformed / fa.MANIFEST_FILENAME).write_bytes(
        b"[" * (fa._MAX_JSON_NESTING + 1) + b"0" + b"]" * (fa._MAX_JSON_NESTING + 1)
    )

    assert tuple(item.identity for item in store.scan()) == (good.identity,)
    resolved = store.resolve_bytes("bare_esp32_headless", "esp32", 4 * _MIB, 0)
    assert resolved.artifact_identity == good.identity


def test_resolve_rehashes_after_an_earlier_compatible_scan(tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    store = fa.ArtifactStore(tmp_path / "store")
    stored = store.import_set(bundle)
    assert store.list_compatible("bare_esp32_headless", "esp32", 4 * _MIB, 0)
    segment = stored.root / stored.board("bare_esp32_headless").segments[0].file.path
    segment.write_bytes(segment.read_bytes() + b"late tamper")

    with pytest.raises(fa.ArtifactNotFoundError):
        store.resolve("bare_esp32_headless", "esp32", 4 * _MIB, 0)


def test_resolve_rejects_complete_bundle_replacing_scanned_identity(tmp_path, monkeypatch):
    old = _make_bundle(
        tmp_path / "old",
        version="old",
        built_at="2026-09-05T12:00:00Z",
        seed=b"old",
    )
    candidate = _make_bundle(
        tmp_path / "candidate",
        version="candidate",
        built_at="2026-09-06T12:00:00Z",
        seed=b"candidate",
    )
    replacement = _make_bundle(
        tmp_path / "replacement",
        version="replacement",
        built_at="2026-09-07T12:00:00Z",
        seed=b"replacement",
    )
    store = fa.ArtifactStore(tmp_path / "store")
    old_stored = store.import_set(old)
    candidate_stored = store.import_set(candidate)
    replacement_identity = fa.load_artifact(replacement).identity
    assert replacement_identity != candidate_stored.identity
    original_list = store.list_compatible
    raced = False

    def replace_after_scan(*args, **kwargs):
        nonlocal raced
        compatible = original_list(*args, **kwargs)
        assert compatible[0].identity == candidate_stored.identity
        shutil.rmtree(candidate_stored.root)
        shutil.copytree(replacement, candidate_stored.root)
        raced = True
        return compatible

    monkeypatch.setattr(store, "list_compatible", replace_after_scan)
    resolved = store.resolve_bytes("bare_esp32_headless", "esp32", 4 * _MIB, 0)

    assert raced is True
    assert fa.load_artifact(candidate_stored.root).identity == replacement_identity
    assert resolved.artifact_identity == old_stored.identity
    assert resolved.version == "old"
    assert b"old" in resolved.data


def test_never_overwrites_corrupt_existing_content_identity(tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    store = fa.ArtifactStore(tmp_path / "store")
    stored = store.import_set(bundle)
    segment = stored.root / stored.board("bare_esp32_headless").segments[0].file.path
    segment.write_bytes(b"corrupt existing")

    with pytest.raises(fa.ArtifactStoreError, match="refusing to overwrite"):
        store.import_set(bundle)
    assert segment.read_bytes() == b"corrupt existing"


def test_scan_skips_invalid_names_symlinks_and_corrupt_sets(tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    store = fa.ArtifactStore(tmp_path / "store")
    good = store.import_set(bundle)
    (store.root / "not-a-content-id").mkdir()
    corrupt = store.root / ("f" * 64)
    corrupt.mkdir()
    (corrupt / fa.MANIFEST_FILENAME).write_text("{}", encoding="utf-8")
    try:
        (store.root / ("e" * 64)).symlink_to(good.root, target_is_directory=True)
    except OSError:
        pass

    assert tuple(item.identity for item in store.list_valid()) == (good.identity,)


def test_scan_orders_fractional_rfc3339_timestamps_chronologically(tmp_path):
    whole = _make_bundle(
        tmp_path / "whole",
        version="whole",
        built_at="2026-09-06T12:00:00Z",
        seed=b"whole",
    )
    fractional = _make_bundle(
        tmp_path / "fractional",
        version="fractional",
        built_at="2026-09-06T12:00:00.9Z",
        seed=b"fractional",
    )
    store = fa.ArtifactStore(tmp_path / "store")
    store.import_set(whole)
    store.import_set(fractional)

    assert tuple(item.version for item in store.scan()) == ("fractional", "whole")


def test_wrong_manifest_filename_is_not_accepted(tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    (bundle / fa.MANIFEST_FILENAME).rename(bundle / "firmware-artifact.json")

    with pytest.raises(fa.ArtifactIntegrityError, match="missing manifest.json"):
        fa.load_artifact(bundle)


def test_store_resolution_never_falls_back_by_chip(tmp_path):
    bundle = _make_bundle(tmp_path / "bundle")
    store = fa.ArtifactStore(tmp_path / "store")
    store.import_set(bundle)

    assert store.list_compatible("invented-board", "esp32", 4 * _MIB, 0) == ()
    with pytest.raises(fa.ArtifactNotFoundError):
        store.resolve_bytes("invented-board", "esp32", 4 * _MIB, 0)


def test_artifact_data_models_are_frozen(tmp_path):
    artifact = fa.load_artifact(_make_bundle(tmp_path / "bundle"))
    with pytest.raises((AttributeError, TypeError)):
        artifact.version = "mutated"
    with pytest.raises((AttributeError, TypeError)):
        artifact.boards[0].chip = "mutated"


def test_import_source_mutation_cannot_replace_last_known_good(tmp_path, monkeypatch):
    old = _make_bundle(
        tmp_path / "old", version="old", built_at="2026-09-05T12:00:00Z", seed=b"old"
    )
    new = _make_bundle(
        tmp_path / "new", version="new", built_at="2026-09-06T12:00:00Z", seed=b"new"
    )
    store = fa.ArtifactStore(tmp_path / "store")
    old_stored = store.import_set(old)
    original_copy = fa._copy_verified_file
    changed = {"done": False}

    def mutate_source_manifest(root, record, target):
        result = original_copy(root, record, target)
        if not changed["done"]:
            source_manifest = Path(root) / fa.MANIFEST_FILENAME
            source_manifest.write_bytes(source_manifest.read_bytes() + b"\n")
            changed["done"] = True
        return result

    monkeypatch.setattr(fa, "_copy_verified_file", mutate_source_manifest)
    with pytest.raises(fa.ArtifactIntegrityError, match="mismatch during import"):
        store.import_set(new)

    assert tuple(item.identity for item in store.scan()) == (old_stored.identity,)
    assert os.path.isdir(old_stored.root)
