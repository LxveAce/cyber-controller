"""Strict consumer and immutable local store for ``FirmwareArtifact@1`` bundles.

The format is intentionally narrow.  A bundle is one complete, six-board LxveOS build set; it is
not a loose collection of firmware files.  Every operative byte and every provenance input is
declared by ``manifest.json`` and hashed.  Loading a set verifies its complete directory topology,
then derives board compatibility from the packaged board-manifest snapshot instead of trusting the
duplicated compatibility claims in the artifact manifest.

This module is deliberately standalone and offline: it uses no Git command, network access, or
hardware.  UI/vault/flash integration belongs to a later change.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

SCHEMA = "FirmwareArtifact@1"
MANIFEST_FILENAME = "manifest.json"
PRODUCT_ID = "lxveos"
SOURCE_REPOSITORY = "https://github.com/LxveAce/lxveos"
TOOLCHAIN = "esp-idf-v6.0.2"

REQUIRED_BOARDS = (
    "bare_esp32_headless",
    "cyd_2432S028_classic",
    "cyd_3248S035_r",
    "jc3248w535_s3_qspi",
    "m5cardputer_v1",
    "m5stickc_plus2",
)

_NOTICE_PATHS = {
    "license": "metadata/LICENSE",
    "credits": "metadata/CREDITS.md",
    "third_party": "metadata/THIRD-PARTY-LICENSES.md",
    "responsible_use": "metadata/RESPONSIBLE-USE.md",
}
_TOP_KEYS = {
    "schema",
    "product",
    "source",
    "build",
    "board_manifest",
    "notices",
    "required_boards",
    "boards",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_RFC3339_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")
_SIZE_RE = re.compile(r"([1-9][0-9]*)(KB|MB|GB)\Z")
_CONTENT_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_BOARD_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_NOTICE_BYTES = 4 * 1024 * 1024
_MAX_PARTITION_BYTES = 4 * 1024 * 1024
_MAX_BUILD_METADATA_BYTES = 4 * 1024 * 1024
_MAX_FIRMWARE_BYTES = 16 * 1024 * 1024
_MAX_JSON_NESTING = 64
_DARWIN_RENAME_EXCL = 0x00000004


class FirmwareArtifactError(ValueError):
    """Base class for artifact validation and selection failures."""


class ArtifactSchemaError(FirmwareArtifactError):
    """The manifest or packaged metadata does not match ``FirmwareArtifact@1``."""


class ArtifactIntegrityError(FirmwareArtifactError):
    """A declared path, size, hash, or bundle topology is unsafe or incorrect."""


class ArtifactCompatibilityError(FirmwareArtifactError):
    """The exact requested board/chip/flash combination is not compatible."""


class ArtifactNotFoundError(FirmwareArtifactError):
    """No valid compatible artifact set is available."""


class ArtifactStoreError(FirmwareArtifactError):
    """An immutable-store import or publication failed."""


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    status: str
    record: str | None


@dataclass(frozen=True, slots=True)
class BoardEvidence:
    built: EvidenceRecord
    flashed: EvidenceRecord
    booted: EvidenceRecord
    tested: EvidenceRecord


@dataclass(frozen=True, slots=True)
class Segment:
    role: str
    offset: int
    file: FileRecord


@dataclass(frozen=True, slots=True)
class BoardArtifact:
    board_id: str
    chip: str
    flash_size_bytes: int
    psram_size_bytes: int
    partition: FileRecord
    image_model: str
    backend: str
    segments: tuple[Segment, ...]
    flasher_args: FileRecord
    dependencies_lock: FileRecord
    evidence: BoardEvidence


@dataclass(frozen=True, slots=True)
class FirmwareArtifact:
    """A fully verified artifact set rooted at ``root``."""

    root: Path
    identity: str
    version: str
    source_commit: str
    source_ref: str
    build_id: str
    built_at: str
    board_manifest: FileRecord
    notices: tuple[tuple[str, FileRecord], ...]
    boards: tuple[BoardArtifact, ...]
    files: tuple[FileRecord, ...]

    def board(self, board_id: str) -> BoardArtifact:
        """Return only an exact, case-sensitive board-id match."""
        for board in self.boards:
            if board.board_id == board_id:
                return board
        raise ArtifactCompatibilityError(f"artifact has no exact board {board_id!r}")


@dataclass(frozen=True, slots=True)
class ResolvedFirmware:
    """Verified immutable bytes ready for a later flash integration."""

    artifact_identity: str
    version: str
    board_id: str
    chip: str
    flash_size_bytes: int
    psram_size_bytes: int
    path: str
    offset: int
    sha256: str
    data: bytes


def _fail_constant(value: str) -> None:
    raise ArtifactSchemaError(f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactSchemaError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _check_json_nesting(value: str, where: str) -> None:
    """Bound structural nesting before handing untrusted text to ``json.loads``."""
    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_JSON_NESTING:
                raise ArtifactSchemaError(
                    f"{where} exceeds the maximum JSON nesting depth of {_MAX_JSON_NESTING}"
                )
        elif character in "]}":
            depth -= 1


def _decode_json(raw: bytes, where: str) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactSchemaError(f"{where} is not UTF-8") from exc
    _check_json_nesting(text, where)
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_fail_constant,
        )
    except FirmwareArtifactError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise ArtifactSchemaError(f"{where} is not valid strict JSON: {exc}") from exc


def _require_object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ArtifactSchemaError(f"{where} must be an object")
    return value


def _require_keys(value: Any, keys: set[str], where: str) -> dict[str, Any]:
    obj = _require_object(value, where)
    actual = set(obj)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise ArtifactSchemaError(f"{where} keys mismatch; missing={missing}, extra={extra}")
    return obj


def _nonempty_string(value: Any, where: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ArtifactSchemaError(
            f"{where} must be a non-empty, trimmed string without control characters"
        )
    return value


def _integer(value: Any, where: str, *, positive: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArtifactSchemaError(f"{where} must be an integer")
    if (positive and value <= 0) or (not positive and value < 0):
        qualifier = "positive" if positive else "non-negative"
        raise ArtifactSchemaError(f"{where} must be {qualifier}")
    return value


def _safe_relative_path(value: Any, where: str) -> str:
    path = _nonempty_string(value, where)
    if "\\" in path or "\x00" in path or ":" in path:
        raise ArtifactIntegrityError(f"{where} is not a portable POSIX relative path: {path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in path.split("/")):
        raise ArtifactIntegrityError(f"{where} is not a safe relative path: {path!r}")
    if pure.as_posix() != path:
        raise ArtifactIntegrityError(f"{where} is not normalized: {path!r}")
    if path == MANIFEST_FILENAME:
        raise ArtifactIntegrityError(f"{where} may not alias {MANIFEST_FILENAME}")
    return path


def _file_record(value: Any, where: str) -> FileRecord:
    obj = _require_keys(value, {"path", "size", "sha256"}, where)
    path = _safe_relative_path(obj["path"], f"{where}.path")
    size = _integer(obj["size"], f"{where}.size", positive=True)
    sha256 = obj["sha256"]
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise ArtifactSchemaError(f"{where}.sha256 must be 64 lowercase hex characters")
    return FileRecord(path=path, size=size, sha256=sha256)


def _record_with_extra_keys(
    value: Any,
    keys: set[str],
    where: str,
) -> tuple[dict[str, Any], FileRecord]:
    obj = _require_keys(value, keys | {"path", "size", "sha256"}, where)
    record = _file_record(
        {key: obj[key] for key in ("path", "size", "sha256")},
        where,
    )
    return obj, record


def _parse_evidence(value: Any, where: str) -> EvidenceRecord:
    obj = _require_keys(value, {"status", "record"}, where)
    status_value = obj["status"]
    if not isinstance(status_value, str) or status_value not in {
        "passed",
        "failed",
        "not_run",
    }:
        raise ArtifactSchemaError(f"{where}.status must be 'passed', 'failed', or 'not_run'")
    record = obj["record"]
    if status_value == "not_run":
        if record is not None:
            raise ArtifactSchemaError(f"{where}.record must be null when status is not_run")
        return EvidenceRecord(status=status_value, record=None)
    return EvidenceRecord(
        status=status_value,
        record=_nonempty_string(record, f"{where}.record"),
    )


def _parse_rfc3339_utc(value: Any, where: str) -> str:
    text = _nonempty_string(value, where)
    if not _RFC3339_UTC_RE.fullmatch(text):
        raise ArtifactSchemaError(f"{where} must be RFC3339 UTC and end in Z")
    try:
        datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise ArtifactSchemaError(f"{where} is not a real RFC3339 timestamp") from exc
    return text


def _rfc3339_sort_key(value: str) -> tuple[int, int, int, int, int, int, Decimal]:
    timestamp = value[:-1]
    base, separator, fraction = timestamp.partition(".")
    parsed = datetime.fromisoformat(base)
    subsecond = Decimal(f"0.{fraction}") if separator else Decimal(0)
    return (
        parsed.year,
        parsed.month,
        parsed.day,
        parsed.hour,
        parsed.minute,
        parsed.second,
        subsecond,
    )


def _parse_size_label(value: Any, where: str) -> int:
    if not isinstance(value, str):
        raise ArtifactSchemaError(f"{where} must be an '<N><KB|MB|GB>' string")
    match = _SIZE_RE.fullmatch(value)
    if not match:
        raise ArtifactSchemaError(f"{where} must be an '<N><KB|MB|GB>' string")
    multiplier = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}[match.group(2)]
    return int(match.group(1)) * multiplier


def _path_from_root(root: Path, relative: str) -> Path:
    return root.joinpath(*PurePosixPath(relative).parts)


def _is_reparse(info: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(flag and attributes & flag)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or _is_reparse(info)


def _assert_no_symlink_ancestors(root: Path, relative: str) -> None:
    current = root
    parts = (None, *PurePosixPath(relative).parts)
    for part in parts:
        if part is not None:
            current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise ArtifactIntegrityError(f"missing path component: {current}") from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise ArtifactIntegrityError(f"symlinks/reparse points are forbidden: {current}")


def _assert_path_components_no_reparse(path: Path, *, allow_missing: bool, where: str) -> None:
    """Reject a symlink/reparse component in an absolute store or staging path."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise ArtifactStoreError(f"missing {where} path component: {current}") from None
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise ArtifactStoreError(f"{where} contains a symlink/reparse point: {current}")


def _read_regular_file(
    root: Path,
    record: FileRecord,
    *,
    retain: bool = True,
    max_bytes: int | None = None,
) -> bytes | None:
    """Stream-verify one declared file, retaining bytes only when explicitly needed."""
    hard_limit = _MAX_FIRMWARE_BYTES
    if max_bytes is not None:
        hard_limit = min(hard_limit, max_bytes)
    if record.size > hard_limit:
        raise ArtifactIntegrityError(
            f"declared size for {record.path} exceeds safety limit {hard_limit}"
        )
    _assert_no_symlink_ancestors(root, record.path)
    path = _path_from_root(root, record.path)
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise ArtifactIntegrityError(f"missing declared file: {record.path}") from exc
    if not stat.S_ISREG(before.st_mode) or _is_reparse(before):
        raise ArtifactIntegrityError(f"declared path is not a regular file: {record.path}")
    if before.st_size != record.size:
        raise ArtifactIntegrityError(
            f"size mismatch for {record.path}: expected {record.size}, got {before.st_size}"
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ArtifactIntegrityError(f"cannot safely open {record.path}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise ArtifactIntegrityError(f"declared file changed while opening: {record.path}")
        chunks: list[bytes] | None = [] if retain else None
        digest = hashlib.sha256()
        total = 0
        while True:
            remaining = record.size - total
            read_size = min(1024 * 1024, remaining + 1, hard_limit - total + 1)
            chunk = os.read(fd, read_size)
            if not chunk:
                break
            if len(chunk) > remaining or total + len(chunk) > hard_limit:
                raise ArtifactIntegrityError(
                    f"file {record.path} grew beyond its declared size while reading"
                )
            total += len(chunk)
            if chunks is not None:
                chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ArtifactIntegrityError(f"declared file changed while reading: {record.path}")
    if total != record.size or after.st_size != record.size:
        raise ArtifactIntegrityError(
            f"size mismatch for {record.path}: expected {record.size}, got {total}"
        )
    actual_hash = digest.hexdigest()
    if actual_hash != record.sha256:
        raise ArtifactIntegrityError(
            f"SHA-256 mismatch for {record.path}: expected {record.sha256}, got {actual_hash}"
        )
    _assert_no_symlink_ancestors(root, record.path)
    return b"".join(chunks) if chunks is not None else None


def _read_manifest(root: Path) -> bytes:
    path = root / MANIFEST_FILENAME
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ArtifactIntegrityError(f"missing {MANIFEST_FILENAME}") from exc
    _assert_no_symlink_ancestors(root, MANIFEST_FILENAME)
    if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
        raise ArtifactIntegrityError(f"{MANIFEST_FILENAME} is not a regular file")
    if info.st_size <= 0 or info.st_size > _MAX_MANIFEST_BYTES:
        raise ArtifactIntegrityError(
            f"{MANIFEST_FILENAME} size must be 1..{_MAX_MANIFEST_BYTES} bytes"
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ArtifactIntegrityError(f"cannot safely open {MANIFEST_FILENAME}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise ArtifactIntegrityError(f"{MANIFEST_FILENAME} changed while opening")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, _MAX_MANIFEST_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_MANIFEST_BYTES:
                raise ArtifactIntegrityError(
                    f"{MANIFEST_FILENAME} exceeds {_MAX_MANIFEST_BYTES} bytes"
                )
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or total != info.st_size:
        raise ArtifactIntegrityError(f"{MANIFEST_FILENAME} changed while reading")
    _assert_no_symlink_ancestors(root, MANIFEST_FILENAME)
    return b"".join(chunks)


def _copy_verified_file(root: Path, record: FileRecord, target: Path) -> None:
    """Copy one already-declared file while no-follow reading and re-verifying it."""
    _assert_no_symlink_ancestors(root, record.path)
    source = _path_from_root(root, record.path)
    try:
        before = source.lstat()
    except OSError as exc:
        raise ArtifactIntegrityError(f"missing source during import: {record.path}") from exc
    if not stat.S_ISREG(before.st_mode) or _is_reparse(before):
        raise ArtifactIntegrityError(f"unsafe source during import: {record.path}")
    if before.st_size != record.size:
        raise ArtifactIntegrityError(
            f"size mismatch during import for {record.path}: "
            f"expected {record.size}, got {before.st_size}"
        )

    _assert_path_components_no_reparse(
        target.parent, allow_missing=False, where="staging destination"
    )
    source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    target_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    source_fd: int | None = None
    target_fd: int | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        source_fd = os.open(source, source_flags)
        opened = os.fstat(source_fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise ArtifactIntegrityError(f"source changed while opening: {record.path}")
        target_fd = os.open(target, target_flags, 0o600)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > record.size:
                raise ArtifactIntegrityError(f"source grew during import: {record.path}")
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(target_fd, remaining)
                if written <= 0:
                    raise OSError("short write while staging artifact")
                remaining = remaining[written:]
        os.fsync(target_fd)
        after = os.fstat(source_fd)
    finally:
        if target_fd is not None:
            os.close(target_fd)
        if source_fd is not None:
            os.close(source_fd)

    if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ArtifactIntegrityError(f"source changed while copying: {record.path}")
    if total != record.size or digest.hexdigest() != record.sha256:
        raise ArtifactIntegrityError(f"source hash/size mismatch during import: {record.path}")
    _assert_no_symlink_ancestors(root, record.path)
    _assert_path_components_no_reparse(target, allow_missing=False, where="staging destination")


def _publish_no_replace(stage: Path, destination: Path) -> None:
    """Atomically publish a directory while refusing any existing destination."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            raise ArtifactStoreError("macOS libc lacks atomic exclusive directory publication")
        renamex_np.argtypes = (
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renamex_np.restype = ctypes.c_int
        # Darwin sys/stdio.h: RENAME_EXCL refuses an existing destination atomically.
        result = renamex_np(
            os.fsencode(stage),
            os.fsencode(destination),
            _DARWIN_RENAME_EXCL,
        )
        if result == 0:
            return
        error = ctypes.get_errno()
        if error in (errno.EEXIST, errno.ENOTEMPTY):
            raise ArtifactStoreError(f"artifact identity already exists: {destination.name}")
        raise OSError(error, os.strerror(error), str(destination))

    if os.name == "nt":
        try:
            os.rename(stage, destination)
        except OSError as exc:
            if os.path.lexists(destination):
                raise ArtifactStoreError(
                    f"artifact identity already exists: {destination.name}"
                ) from exc
            raise
        return

    if os.name == "posix":
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is not None:
            renameat2.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            renameat2.restype = ctypes.c_int
            result = renameat2(
                -100,
                os.fsencode(stage),
                -100,
                os.fsencode(destination),
                1,
            )
            if result == 0:
                return
            error = ctypes.get_errno()
            if error in (errno.EEXIST, errno.ENOTEMPTY):
                raise ArtifactStoreError(f"artifact identity already exists: {destination.name}")
            raise OSError(error, os.strerror(error), str(destination))

    # A check-then-rename fallback could replace a concurrently-created empty directory.
    # Fail closed on platforms that do not expose no-replace directory promotion.
    raise ArtifactStoreError("platform lacks atomic no-replace directory publication")


def _expected_directories(files: Iterable[str]) -> set[str]:
    directories: set[str] = set()
    for relative in files:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _inventory_tree(root: Path, expected_files: set[str]) -> None:
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise ArtifactIntegrityError(f"artifact root is unavailable: {root}") from exc
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or _is_reparse(root_info)
    ):
        raise ArtifactIntegrityError(f"artifact root is not a real directory: {root}")
    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    seen_case: dict[str, str] = {}

    def add_case(relative: str) -> None:
        folded = relative.casefold()
        prior = seen_case.get(folded)
        if prior is not None and prior != relative:
            raise ArtifactIntegrityError(f"case-colliding paths: {prior!r} and {relative!r}")
        seen_case[folded] = relative

    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in dirs:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                raise ArtifactIntegrityError(f"symlink/reparse directory is forbidden: {relative}")
            if not stat.S_ISDIR(info.st_mode):
                raise ArtifactIntegrityError(f"non-directory tree entry: {relative}")
            add_case(relative)
            actual_dirs.add(relative)
        for name in files:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                raise ArtifactIntegrityError(f"symlink/reparse file is forbidden: {relative}")
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactIntegrityError(f"non-regular tree entry: {relative}")
            add_case(relative)
            actual_files.add(relative)

    missing = sorted(expected_files - actual_files)
    extra = sorted(actual_files - expected_files)
    if missing or extra:
        raise ArtifactIntegrityError(
            f"artifact file set mismatch; missing={missing}, extra={extra}"
        )
    expected_dirs = _expected_directories(expected_files)
    extra_dirs = sorted(actual_dirs - expected_dirs)
    missing_dirs = sorted(expected_dirs - actual_dirs)
    if missing_dirs or extra_dirs:
        raise ArtifactIntegrityError(
            f"artifact directory set mismatch; missing={missing_dirs}, extra={extra_dirs}"
        )


def _bundle_root(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path)
    if candidate.name == MANIFEST_FILENAME:
        if _is_link_or_reparse(candidate):
            raise ArtifactIntegrityError(f"{MANIFEST_FILENAME} may not be a symlink/reparse point")
        return candidate.parent.absolute()
    if os.path.lexists(candidate):
        if _is_link_or_reparse(candidate):
            raise ArtifactIntegrityError("artifact root may not be a symlink/reparse point")
        if not candidate.is_dir():
            raise ArtifactSchemaError(
                f"artifact path must be a directory or exact {MANIFEST_FILENAME} path"
            )
    return candidate.absolute()


def _register_file(
    records: dict[str, FileRecord],
    folded_paths: dict[str, str],
    record: FileRecord,
) -> None:
    folded = record.path.casefold()
    prior_path = folded_paths.get(folded)
    if prior_path is not None and prior_path != record.path:
        raise ArtifactIntegrityError(
            f"manifest declares case-colliding paths: {prior_path!r} and {record.path!r}"
        )
    folded_paths[folded] = record.path
    prior = records.get(record.path)
    if prior is not None and prior != record:
        raise ArtifactSchemaError(f"conflicting records for shared file {record.path!r}")
    records[record.path] = record


def _parse_board_row(value: Any, index: int) -> BoardArtifact:
    where = f"boards[{index}]"
    obj = _require_keys(
        value,
        {
            "board_id",
            "chip",
            "flash_size_bytes",
            "psram_size_bytes",
            "partition",
            "image_model",
            "backend",
            "segments",
            "build_metadata",
            "evidence",
        },
        where,
    )
    board_id = _nonempty_string(obj["board_id"], f"{where}.board_id")
    chip = _nonempty_string(obj["chip"], f"{where}.chip")
    flash_size = _integer(obj["flash_size_bytes"], f"{where}.flash_size_bytes", positive=True)
    if flash_size > _MAX_FIRMWARE_BYTES:
        raise ArtifactSchemaError(f"{where}.flash_size_bytes exceeds supported safety limit")
    psram_size = _integer(obj["psram_size_bytes"], f"{where}.psram_size_bytes", positive=False)
    if psram_size > _MAX_FIRMWARE_BYTES:
        raise ArtifactSchemaError(f"{where}.psram_size_bytes exceeds supported safety limit")
    if obj["image_model"] != "merged-single-bin":
        raise ArtifactSchemaError(f"{where}.image_model must be 'merged-single-bin'")
    if obj["backend"] != "esptool":
        raise ArtifactSchemaError(f"{where}.backend must be 'esptool'")
    partition = _file_record(obj["partition"], f"{where}.partition")
    if partition.size > _MAX_PARTITION_BYTES:
        raise ArtifactSchemaError(f"{where}.partition exceeds {_MAX_PARTITION_BYTES} bytes")

    segments_value = obj["segments"]
    if not isinstance(segments_value, list) or len(segments_value) != 1:
        raise ArtifactSchemaError(f"{where}.segments must contain exactly one merged segment")
    segment_obj, segment_file = _record_with_extra_keys(
        segments_value[0], {"role", "offset"}, f"{where}.segments[0]"
    )
    if (
        segment_obj["role"] != "merged"
        or type(segment_obj["offset"]) is not int
        or segment_obj["offset"] != 0
    ):
        raise ArtifactSchemaError(f"{where} segment must have role='merged' and integer offset=0")
    if segment_file.size > flash_size:
        raise ArtifactSchemaError(
            f"{where} merged segment size exceeds declared board flash capacity"
        )
    expected_segment = f"boards/{board_id}/{board_id}-merged.bin"
    if segment_file.path != expected_segment:
        raise ArtifactSchemaError(f"{where} merged segment path must be {expected_segment!r}")

    metadata = _require_keys(
        obj["build_metadata"], {"flasher_args", "dependencies_lock"}, f"{where}.build_metadata"
    )
    flasher_obj, flasher_args = _record_with_extra_keys(
        metadata["flasher_args"], {"operative"}, f"{where}.build_metadata.flasher_args"
    )
    if flasher_obj["operative"] is not False:
        raise ArtifactSchemaError(f"{where}.build_metadata.flasher_args.operative must be false")
    if flasher_args.size > _MAX_BUILD_METADATA_BYTES:
        raise ArtifactSchemaError(f"{where}.build_metadata.flasher_args exceeds safety limit")
    if flasher_args.path != f"boards/{board_id}/flasher_args.json":
        raise ArtifactSchemaError(f"{where} flasher_args path is not board-keyed")
    dependencies = _file_record(
        metadata["dependencies_lock"], f"{where}.build_metadata.dependencies_lock"
    )
    if dependencies.size > _MAX_BUILD_METADATA_BYTES:
        raise ArtifactSchemaError(f"{where}.build_metadata.dependencies_lock exceeds safety limit")
    if dependencies.path != f"boards/{board_id}/dependencies.lock":
        raise ArtifactSchemaError(f"{where} dependencies_lock path is not board-keyed")

    evidence_obj = _require_keys(
        obj["evidence"], {"built", "flashed", "booted", "tested"}, f"{where}.evidence"
    )
    evidence = BoardEvidence(
        built=_parse_evidence(evidence_obj["built"], f"{where}.evidence.built"),
        flashed=_parse_evidence(evidence_obj["flashed"], f"{where}.evidence.flashed"),
        booted=_parse_evidence(evidence_obj["booted"], f"{where}.evidence.booted"),
        tested=_parse_evidence(evidence_obj["tested"], f"{where}.evidence.tested"),
    )
    return BoardArtifact(
        board_id=board_id,
        chip=chip,
        flash_size_bytes=flash_size,
        psram_size_bytes=psram_size,
        partition=partition,
        image_model="merged-single-bin",
        backend="esptool",
        segments=(Segment(role="merged", offset=0, file=segment_file),),
        flasher_args=flasher_args,
        dependencies_lock=dependencies,
        evidence=evidence,
    )


def _validate_board_snapshot(
    snapshot_raw: bytes,
    snapshot_version: str,
    boards: tuple[BoardArtifact, ...],
) -> None:
    snapshot = _require_keys(
        _decode_json(snapshot_raw, "board_manifest snapshot"),
        {"_schema", "_version", "boards"},
        "board_manifest snapshot",
    )
    _nonempty_string(snapshot["_schema"], "board_manifest._schema")
    if snapshot["_version"] != snapshot_version:
        raise ArtifactSchemaError(
            "board_manifest.version does not match packaged board snapshot _version"
        )
    definitions = _require_object(snapshot["boards"], "board_manifest.boards")
    if tuple(sorted(definitions)) != REQUIRED_BOARDS:
        raise ArtifactSchemaError("packaged board snapshot does not contain the exact v1 board set")

    for board in boards:
        where = f"board_manifest.boards[{board.board_id!r}]"
        definition = _require_object(definitions[board.board_id], where)
        chip = _nonempty_string(definition.get("chip"), f"{where}.chip")
        flash_size = _parse_size_label(definition.get("flash_size"), f"{where}.flash_size")
        psram_flag = definition.get("psram")
        if type(psram_flag) is not bool:
            raise ArtifactSchemaError(f"{where}.psram must be a boolean")
        if psram_flag:
            psram_size = _parse_size_label(definition.get("psram_size"), f"{where}.psram_size")
        else:
            psram_size = 0
            if "psram_size" in definition:
                raise ArtifactSchemaError(f"{where}.psram_size must be absent when psram is false")
        build = _require_object(definition.get("build"), f"{where}.build")
        idf_target = _nonempty_string(build.get("idf_target"), f"{where}.build.idf_target")
        partition_source = _safe_relative_path(
            build.get("partition_csv"), f"{where}.build.partition_csv"
        )
        if not partition_source.startswith("partitions/"):
            raise ArtifactSchemaError(f"{where}.build.partition_csv must be below 'partitions/'")
        expected_partition = f"metadata/{partition_source}"
        if chip != idf_target:
            raise ArtifactSchemaError(f"{where}: chip and build.idf_target disagree")
        if board.chip != chip:
            raise ArtifactSchemaError(f"{board.board_id}: manifest chip disagrees with snapshot")
        if board.flash_size_bytes != flash_size:
            raise ArtifactSchemaError(
                f"{board.board_id}: manifest flash size disagrees with snapshot"
            )
        if board.psram_size_bytes != psram_size:
            raise ArtifactSchemaError(
                f"{board.board_id}: manifest PSRAM size disagrees with snapshot"
            )
        if board.partition.path != expected_partition:
            raise ArtifactSchemaError(f"{board.board_id}: partition path disagrees with snapshot")


def load_artifact_set(path: str | os.PathLike[str]) -> FirmwareArtifact:
    """Load and fully verify one complete ``FirmwareArtifact@1`` directory.

    ``path`` may be the bundle directory or its exact ``manifest.json``.  No alternate manifest
    filename is accepted.  All declared files are rehashed; missing or extra entries fail.
    """
    root = _bundle_root(path)
    manifest_raw = _read_manifest(root)
    identity = hashlib.sha256(manifest_raw).hexdigest()
    manifest = _require_keys(_decode_json(manifest_raw, MANIFEST_FILENAME), _TOP_KEYS, "manifest")
    if manifest["schema"] != SCHEMA:
        raise ArtifactSchemaError(f"manifest.schema must be {SCHEMA!r}")

    product = _require_keys(manifest["product"], {"id", "version"}, "product")
    if product["id"] != PRODUCT_ID:
        raise ArtifactSchemaError(f"product.id must be {PRODUCT_ID!r}")
    version = _nonempty_string(product["version"], "product.version")

    source = _require_keys(manifest["source"], {"repository", "commit", "ref"}, "source")
    if source["repository"] != SOURCE_REPOSITORY:
        raise ArtifactSchemaError(f"source.repository must be {SOURCE_REPOSITORY!r}")
    commit = source["commit"]
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        raise ArtifactSchemaError("source.commit must be exactly 40 lowercase hex characters")
    source_ref = _nonempty_string(source["ref"], "source.ref")

    build = _require_keys(manifest["build"], {"id", "built_at", "toolchain"}, "build")
    build_id = _nonempty_string(build["id"], "build.id")
    built_at = _parse_rfc3339_utc(build["built_at"], "build.built_at")
    if build["toolchain"] != TOOLCHAIN:
        raise ArtifactSchemaError(f"build.toolchain must be {TOOLCHAIN!r}")

    board_manifest_obj, board_manifest = _record_with_extra_keys(
        manifest["board_manifest"], {"version"}, "board_manifest"
    )
    if board_manifest.path != "metadata/cyd_boards.json":
        raise ArtifactSchemaError("board_manifest.path must be 'metadata/cyd_boards.json'")
    if board_manifest.size > _MAX_BOARD_MANIFEST_BYTES:
        raise ArtifactSchemaError(f"board_manifest exceeds {_MAX_BOARD_MANIFEST_BYTES} bytes")
    board_manifest_version = _nonempty_string(
        board_manifest_obj["version"], "board_manifest.version"
    )

    notices_value = manifest["notices"]
    if not isinstance(notices_value, list) or len(notices_value) != len(_NOTICE_PATHS):
        raise ArtifactSchemaError("notices must contain exactly the four required notice kinds")
    notices: list[tuple[str, FileRecord]] = []
    seen_kinds: set[str] = set()
    for index, value in enumerate(notices_value):
        obj, record = _record_with_extra_keys(value, {"kind"}, f"notices[{index}]")
        kind = obj["kind"]
        if not isinstance(kind, str) or kind not in _NOTICE_PATHS or kind in seen_kinds:
            raise ArtifactSchemaError(f"notices[{index}].kind is unknown or duplicated")
        if record.path != _NOTICE_PATHS[kind]:
            raise ArtifactSchemaError(f"notice {kind!r} must use path {_NOTICE_PATHS[kind]!r}")
        if record.size > _MAX_NOTICE_BYTES:
            raise ArtifactSchemaError(f"notice {kind!r} exceeds {_MAX_NOTICE_BYTES} bytes")
        seen_kinds.add(kind)
        notices.append((kind, record))
    if seen_kinds != set(_NOTICE_PATHS):
        raise ArtifactSchemaError("notices do not contain all four required kinds")

    required_value = manifest["required_boards"]
    if not isinstance(required_value, list) or tuple(required_value) != REQUIRED_BOARDS:
        raise ArtifactSchemaError("required_boards must be the exact sorted v1 board-id list")
    boards_value = manifest["boards"]
    if not isinstance(boards_value, list) or len(boards_value) != len(REQUIRED_BOARDS):
        raise ArtifactSchemaError("boards must contain exactly six board objects")
    boards = tuple(_parse_board_row(value, index) for index, value in enumerate(boards_value))
    if tuple(board.board_id for board in boards) != REQUIRED_BOARDS:
        raise ArtifactSchemaError("boards must be sorted and match required_boards exactly")

    records: dict[str, FileRecord] = {}
    folded_paths: dict[str, str] = {MANIFEST_FILENAME.casefold(): MANIFEST_FILENAME}
    _register_file(records, folded_paths, board_manifest)
    for _kind, notice in notices:
        _register_file(records, folded_paths, notice)
    for board in boards:
        _register_file(records, folded_paths, board.partition)
        _register_file(records, folded_paths, board.segments[0].file)
        _register_file(records, folded_paths, board.flasher_args)
        _register_file(records, folded_paths, board.dependencies_lock)

    expected_files = {MANIFEST_FILENAME, *records}
    _inventory_tree(root, expected_files)
    snapshot_raw = _read_regular_file(
        root,
        board_manifest,
        retain=True,
        max_bytes=_MAX_BOARD_MANIFEST_BYTES,
    )
    if snapshot_raw is None:  # pragma: no cover - retain=True guarantees bytes.
        raise ArtifactIntegrityError("packaged board snapshot was not retained")
    _validate_board_snapshot(snapshot_raw, board_manifest_version, boards)
    for relative, record in records.items():
        if relative != board_manifest.path:
            _read_regular_file(root, record, retain=False)
    return FirmwareArtifact(
        root=root,
        identity=identity,
        version=version,
        source_commit=commit,
        source_ref=source_ref,
        build_id=build_id,
        built_at=built_at,
        board_manifest=board_manifest,
        notices=tuple(notices),
        boards=boards,
        files=tuple(records[path] for path in sorted(records)),
    )


def verify_artifact_set(path: str | os.PathLike[str]) -> FirmwareArtifact:
    """Alias with an explicit verification-oriented name."""
    return load_artifact_set(path)


def select_board(
    artifact: FirmwareArtifact,
    board_id: str,
    chip: str,
    flash_size_bytes: int,
    psram_size_bytes: int | None = None,
) -> BoardArtifact:
    """Select an exact board and enforce its physical compatibility metadata.

    A board-id miss never falls back to another board that happens to share a chip.
    """
    board = artifact.board(board_id)
    if chip != board.chip:
        raise ArtifactCompatibilityError(
            f"{board_id}: chip mismatch (artifact {board.chip!r}, device {chip!r})"
        )
    actual_flash = _integer(flash_size_bytes, "flash_size_bytes", positive=True)
    if actual_flash != board.flash_size_bytes:
        raise ArtifactCompatibilityError(
            f"{board_id}: flash-size mismatch (artifact {board.flash_size_bytes}, "
            f"device {actual_flash})"
        )
    if psram_size_bytes is not None:
        actual_psram = _integer(psram_size_bytes, "psram_size_bytes", positive=False)
        if actual_psram != board.psram_size_bytes:
            raise ArtifactCompatibilityError(
                f"{board_id}: PSRAM-size mismatch (artifact {board.psram_size_bytes}, "
                f"device {actual_psram})"
            )
    return board


class ArtifactStore:
    """Content-addressed, append-only local artifact-set store.

    Each valid set is published as ``<root>/<sha256(exact manifest.json bytes)>``.  Imports are
    copied to a same-parent staging directory, verified there, and atomically renamed into place.
    Existing sets are never modified or replaced.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).absolute()

    def _ensure_root(self) -> None:
        _assert_path_components_no_reparse(
            self.root.parent, allow_missing=True, where="artifact-store parent"
        )
        if os.path.lexists(self.root):
            if _is_link_or_reparse(self.root) or not self.root.is_dir():
                raise ArtifactStoreError(
                    f"artifact-store root is not a real directory: {self.root}"
                )
            return
        try:
            self.root.mkdir(parents=True)
        except OSError as exc:
            raise ArtifactStoreError(f"could not create artifact store: {exc}") from exc
        _assert_path_components_no_reparse(
            self.root, allow_missing=False, where="artifact-store root"
        )

    def _existing(self, destination: Path, identity: str) -> FirmwareArtifact | None:
        if not os.path.lexists(destination):
            return None
        if _is_link_or_reparse(destination) or not destination.is_dir():
            raise ArtifactStoreError(f"content identity is occupied unsafely: {identity}")
        try:
            artifact = load_artifact_set(destination)
        except FirmwareArtifactError as exc:
            raise ArtifactStoreError(
                f"refusing to overwrite corrupt immutable set {identity}: {exc}"
            ) from exc
        if artifact.identity != identity:
            raise ArtifactStoreError(f"stored set identity mismatch at {destination}")
        return artifact

    def import_set(self, source: str | os.PathLike[str]) -> FirmwareArtifact:
        """Verify, stage, verify again, then atomically publish a complete artifact set."""
        source_artifact = load_artifact_set(source)
        self._ensure_root()
        destination = self.root / source_artifact.identity
        existing = self._existing(destination, source_artifact.identity)
        if existing is not None:
            return existing

        try:
            stage = Path(tempfile.mkdtemp(prefix=".incoming-", dir=self.root))
        except OSError as exc:
            raise ArtifactStoreError(
                f"could not create same-parent staging directory: {exc}"
            ) from exc
        published = False
        try:
            manifest_path = source_artifact.root / MANIFEST_FILENAME
            try:
                manifest_size = manifest_path.lstat().st_size
            except OSError as exc:
                raise ArtifactIntegrityError(
                    f"source lost {MANIFEST_FILENAME} before import"
                ) from exc
            if manifest_size <= 0 or manifest_size > _MAX_MANIFEST_BYTES:
                raise ArtifactIntegrityError(f"source {MANIFEST_FILENAME} exceeds its safety limit")
            copy_records = {record.path: record for record in source_artifact.files}
            copy_records[MANIFEST_FILENAME] = FileRecord(
                path=MANIFEST_FILENAME,
                size=manifest_size,
                sha256=source_artifact.identity,
            )
            for relative in sorted(copy_records):
                target_path = _path_from_root(stage, relative)
                try:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    _copy_verified_file(
                        source_artifact.root,
                        copy_records[relative],
                        target_path,
                    )
                except OSError as exc:
                    raise ArtifactStoreError(f"could not stage {relative}: {exc}") from exc

            staged = load_artifact_set(stage)
            if staged.identity != source_artifact.identity:
                raise ArtifactIntegrityError(
                    "manifest changed between source verification and staging"
                )
            existing = self._existing(destination, source_artifact.identity)
            if existing is not None:
                return existing
            try:
                _publish_no_replace(stage, destination)
            except (ArtifactStoreError, OSError) as exc:
                # A racing importer may have atomically published the same identity.
                existing = self._existing(destination, source_artifact.identity)
                if existing is not None:
                    return existing
                if isinstance(exc, ArtifactStoreError):
                    raise
                raise ArtifactStoreError(f"atomic artifact publication failed: {exc}") from exc
            published = True
            return load_artifact_set(destination)
        finally:
            if not published and stage.exists() and not _is_link_or_reparse(stage):
                shutil.rmtree(stage, ignore_errors=True)

    # Friendly name for callers that use artifact rather than set terminology.
    import_artifact = import_set

    def scan(self) -> tuple[FirmwareArtifact, ...]:
        """Return all valid stored sets, newest build first, silently skipping corrupt entries."""
        if not self.root.exists() or _is_link_or_reparse(self.root) or not self.root.is_dir():
            return ()
        valid: list[FirmwareArtifact] = []
        try:
            children = list(self.root.iterdir())
        except OSError:
            return ()
        for child in children:
            if (
                not _CONTENT_ID_RE.fullmatch(child.name)
                or _is_link_or_reparse(child)
                or not child.is_dir()
            ):
                continue
            try:
                artifact = load_artifact_set(child)
            except (OSError, RecursionError, ValueError):
                continue
            if artifact.identity == child.name:
                valid.append(artifact)
        valid.sort(
            key=lambda item: (_rfc3339_sort_key(item.built_at), item.identity),
            reverse=True,
        )
        return tuple(valid)

    list_valid = scan

    def list_compatible(
        self,
        board_id: str,
        chip: str,
        flash_size_bytes: int,
        psram_size_bytes: int | None = None,
    ) -> tuple[FirmwareArtifact, ...]:
        """List valid sets compatible with one exact board, newest first."""
        compatible: list[FirmwareArtifact] = []
        for artifact in self.scan():
            try:
                select_board(
                    artifact,
                    board_id,
                    chip,
                    flash_size_bytes,
                    psram_size_bytes,
                )
            except ArtifactCompatibilityError:
                continue
            compatible.append(artifact)
        return tuple(compatible)

    def resolve_bytes(
        self,
        board_id: str,
        chip: str,
        flash_size_bytes: int,
        psram_size_bytes: int | None = None,
    ) -> ResolvedFirmware:
        """Resolve the newest compatible set and return freshly rehashed firmware bytes.

        Every candidate is loaded again after scanning and its selected segment is then read and
        hashed one additional time.  If a newer set became corrupt, resolution falls back to the
        next last-known-good compatible set.  Returning bytes rather than a mutable path closes the
        re-open gap for the later flash integration.
        """
        for candidate in self.list_compatible(board_id, chip, flash_size_bytes, psram_size_bytes):
            try:
                expected_root = self.root / candidate.identity
                if candidate.root != expected_root:
                    raise ArtifactIntegrityError(
                        "stored artifact directory does not match its content identity"
                    )
                fresh = load_artifact_set(candidate.root)
                if fresh.identity != candidate.identity or fresh.root != expected_root:
                    raise ArtifactIntegrityError(
                        "stored artifact content identity changed after compatibility scan"
                    )
                board = select_board(
                    fresh,
                    board_id,
                    chip,
                    flash_size_bytes,
                    psram_size_bytes,
                )
                segment = board.segments[0]
                data = _read_regular_file(fresh.root, segment.file)
                if data is None:  # pragma: no cover - retain=True guarantees bytes.
                    raise ArtifactIntegrityError("resolved segment bytes were not retained")
            except (OSError, RecursionError, ValueError):
                continue
            return ResolvedFirmware(
                artifact_identity=fresh.identity,
                version=fresh.version,
                board_id=board.board_id,
                chip=board.chip,
                flash_size_bytes=board.flash_size_bytes,
                psram_size_bytes=board.psram_size_bytes,
                path=segment.file.path,
                offset=segment.offset,
                sha256=segment.file.sha256,
                data=data,
            )
        raise ArtifactNotFoundError(
            f"no valid artifact for exact board={board_id!r}, chip={chip!r}, "
            f"flash_size_bytes={flash_size_bytes}"
        )

    resolve = resolve_bytes


# Short aliases make the public surface unsurprising without weakening the v1 contract.
load_artifact = load_artifact_set
verify_artifact = verify_artifact_set


__all__ = [
    "ArtifactCompatibilityError",
    "ArtifactIntegrityError",
    "ArtifactNotFoundError",
    "ArtifactSchemaError",
    "ArtifactStore",
    "ArtifactStoreError",
    "BoardArtifact",
    "BoardEvidence",
    "EvidenceRecord",
    "FileRecord",
    "FirmwareArtifact",
    "FirmwareArtifactError",
    "MANIFEST_FILENAME",
    "PRODUCT_ID",
    "REQUIRED_BOARDS",
    "ResolvedFirmware",
    "SCHEMA",
    "SOURCE_REPOSITORY",
    "Segment",
    "TOOLCHAIN",
    "load_artifact",
    "load_artifact_set",
    "select_board",
    "verify_artifact",
    "verify_artifact_set",
]
