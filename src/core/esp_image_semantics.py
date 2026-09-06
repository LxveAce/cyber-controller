"""Offline structural validation for current LxveOS ESP-IDF merged images.

This module deliberately does not import esptool, invoke a command, access the network, or inspect
hardware.  It recognizes only the narrow plaintext image layouts admitted by
``FirmwareArtifact@1``: ESP32 and ESP32-S3 images built by ESP-IDF v6.0.2 for 4 MiB or 8 MiB DIO
flash at 80 MHz.  Authenticity and live device compatibility remain separate gates.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import struct
from dataclasses import dataclass
from typing import Any, Literal, Mapping

ESP_IMAGE_MAGIC = 0xE9
ESP_IMAGE_HEADER_SIZE = 24
ESP_PARTITION_TABLE_OFFSET = 0x8000
ESP_PARTITION_TABLE_SIZE = 0xC00
ESP_PARTITION_SECTOR_END = 0x9000
ESP_IMAGE_CHECKSUM_INITIAL = 0xEF
ESP_IMAGE_MAX_SEGMENTS = 16
ESP_IMAGE_MAX_SEGMENT_BYTES = 0x1000000
POLICY_ID = "LxveOSEspImagePolicy@1"

_EXPECTED_IDF_VERSION = "v6.0.2"
_EXPECTED_PROJECT_NAME = "lxveos"
_MAX_JSON_NESTING = 64
_MAX_METADATA_BYTES = 4 * 1024 * 1024
_CHIP_LAYOUTS = {
    "esp32": (0x1000, 0),
    "esp32s3": (0x0000, 9),
}
_FLASH_SIZE_CODES = {
    4 * 1024 * 1024: 2,
    8 * 1024 * 1024: 3,
}
_FLASH_SIZE_LABELS = {
    4 * 1024 * 1024: "4MB",
    8 * 1024 * 1024: "8MB",
}
_LXVEOS_CHIP_FLASH_MATRIX = {
    ("esp32", 4 * 1024 * 1024),
    ("esp32", 8 * 1024 * 1024),
    ("esp32s3", 8 * 1024 * 1024),
}
_DEFAULT_MMU_PAGE_SIZE = 0x10000
_DROM_RANGES_BY_CHIP_ID = {
    0: (0x3F400000, 0x3F800000),
    9: (0x3C000000, 0x3E000000),
}
_IROM_RANGES_BY_CHIP_ID = {
    0: (0x400D0000, 0x40400000),
    9: (0x42000000, 0x44000000),
}

_TYPE_NAMES = {"app": 0x00, "data": 0x01}
_APP_SUBTYPES = {
    "factory": 0x00,
    **{f"ota_{index}": 0x10 + index for index in range(16)},
    "test": 0x20,
}
_DATA_SUBTYPES = {
    "ota": 0x00,
    "phy": 0x01,
    "nvs": 0x02,
    "coredump": 0x03,
    "nvs_keys": 0x04,
    "efuse": 0x05,
    "undefined": 0x06,
    "esphttpd": 0x80,
    "fat": 0x81,
    "spiffs": 0x82,
    "littlefs": 0x83,
}


class SemanticValidationError(ValueError):
    """The bytes are not an admitted current LxveOS ESP image set."""


# Kept as a descriptive alias for callers that prefer an ESP-specific exception name.
EspSemanticError = SemanticValidationError


@dataclass(frozen=True, slots=True)
class EspSegmentInfo:
    """One parsed ESP image segment."""

    load_addr: int
    data_length: int
    data_offset: int


@dataclass(frozen=True, slots=True)
class EspImageInfo:
    """Bounds and header values for one verified ESP image."""

    start: int
    end: int
    entry_addr: int
    chip_id: int
    spi_mode: int
    flash_size_code: int
    flash_freq_code: int
    min_revision: int
    max_revision: int
    segments: tuple[EspSegmentInfo, ...]


@dataclass(frozen=True, slots=True)
class PartitionInfo:
    """Normalized binary/CSV partition record."""

    name: str
    type: int
    subtype: int
    offset: int
    size: int
    flags: int


@dataclass(frozen=True, slots=True)
class MergedImageInfo:
    """Verified structural summary for a merged board image."""

    chip: str
    flash_size_bytes: int
    bootloader: EspImageInfo
    app: EspImageInfo
    app_partition: PartitionInfo
    partitions: tuple[PartitionInfo, ...]


def _byte_view(blob: bytes | bytearray | memoryview, label: str) -> memoryview:
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise EspSemanticError(f"{label} must be bytes-like")
    try:
        return memoryview(blob).cast("B")
    except (TypeError, ValueError) as exc:
        raise EspSemanticError(f"{label} must be a flat byte buffer") from exc


def _plain_int(value: Any, label: str) -> int:
    if type(value) is not int:
        raise EspSemanticError(f"{label} must be an integer")
    return value


def _expected_lxveos_partitions(flash_size_bytes: int) -> tuple[PartitionInfo, ...]:
    if flash_size_bytes == 4 * 1024 * 1024:
        return (
            PartitionInfo("nvs", 1, 2, 0x9000, 0x6000, 0),
            PartitionInfo("phy_init", 1, 1, 0xF000, 0x1000, 0),
            PartitionInfo("factory", 0, 0, 0x10000, 0x2D0000, 0),
            PartitionInfo("storage", 1, 0x81, 0x2E0000, 0x110000, 0),
        )
    return (
        PartitionInfo("nvs", 1, 2, 0x9000, 0x6000, 0),
        PartitionInfo("otadata", 1, 0, 0xF000, 0x2000, 0),
        PartitionInfo("phy_init", 1, 1, 0x11000, 0x1000, 0),
        PartitionInfo("ota_0", 0, 0x10, 0x20000, 0x2A0000, 0),
        PartitionInfo("ota_1", 0, 0x11, 0x2C0000, 0x2A0000, 0),
        PartitionInfo("storage", 1, 0x81, 0x560000, 0x2A0000, 0),
    )


def _require_region(
    view: memoryview,
    start: int,
    length: int,
    region_end: int,
    label: str,
) -> memoryview:
    if start < 0 or length < 0 or region_end < 0:
        raise EspSemanticError(f"{label} has a negative bound")
    end = start + length
    if end < start or end > region_end or end > len(view):
        raise EspSemanticError(
            f"{label} is truncated or crosses its region (offset=0x{start:x}, size={length})"
        )
    return view[start:end]


def _decode_descriptor_string(raw: memoryview, label: str, *, require_nonempty: bool = True) -> str:
    value = raw.tobytes()
    head, separator, tail = value.partition(b"\x00")
    if not separator:
        raise EspSemanticError(f"{label} must be NUL-terminated")
    if any(tail):
        raise EspSemanticError(f"{label} has nonzero bytes after its NUL terminator")
    try:
        text = head.decode("ascii")
    except UnicodeDecodeError as exc:
        raise EspSemanticError(f"{label} must be ASCII") from exc
    if require_nonempty and not text:
        raise EspSemanticError(f"{label} must be non-empty")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in text):
        raise EspSemanticError(f"{label} contains a control character")
    return text


def _decode_partition_name(raw: memoryview, label: str) -> str:
    """Decode the fixed 16-byte label, which may validly occupy the whole field."""
    value = raw.tobytes()
    head, separator, tail = value.partition(b"\x00")
    if separator and any(tail):
        raise EspSemanticError(f"{label} has nonzero bytes after its NUL terminator")
    try:
        text = head.decode("ascii")
    except UnicodeDecodeError as exc:
        raise EspSemanticError(f"{label} must be ASCII") from exc
    if not text:
        raise EspSemanticError(f"{label} must be non-empty")
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in text):
        raise EspSemanticError(f"{label} must contain printable ASCII")
    return text


def _xor_bytes(checksum: int, raw: memoryview) -> int:
    for value in raw:
        checksum ^= value
    return checksum


def _in_range(value: int, bounds: tuple[int, int]) -> bool:
    return bounds[0] <= value < bounds[1]


def validate_esp_image(
    blob: bytes | bytearray | memoryview,
    *,
    image_offset: int,
    region_end: int,
    expected_chip_id: int,
    expected_flash_size_code: int,
    expected_mode: int = 2,
    expected_frequency: int = 0xF,
    kind: Literal["bootloader", "app"],
) -> EspImageInfo:
    """Parse one ESP image with caller-supplied header policy inside a bounded region.

    Product identity, descriptors, address maps, and merged-image layout are intentionally enforced
    by :func:`validate_lxveos_idf602_merged`, not this reusable format primitive.
    """
    if kind not in ("bootloader", "app"):
        raise EspSemanticError(f"unknown ESP image kind: {kind!r}")
    image_offset = _plain_int(image_offset, "image_offset")
    region_end = _plain_int(region_end, "region_end")
    expected_chip_id = _plain_int(expected_chip_id, "expected_chip_id")
    expected_flash_size_code = _plain_int(expected_flash_size_code, "expected_flash_size_code")
    expected_mode = _plain_int(expected_mode, "expected_mode")
    expected_frequency = _plain_int(expected_frequency, "expected_frequency")
    view = _byte_view(blob, f"{kind} image blob")
    header = _require_region(view, image_offset, ESP_IMAGE_HEADER_SIZE, region_end, kind)
    if header[0] != ESP_IMAGE_MAGIC:
        raise EspSemanticError(
            f"{kind} image at 0x{image_offset:x} has invalid magic 0x{header[0]:02x}"
        )
    segment_count = header[1]
    if not 1 <= segment_count <= ESP_IMAGE_MAX_SEGMENTS:
        raise EspSemanticError(
            f"{kind} image segment count {segment_count} is outside 1..{ESP_IMAGE_MAX_SEGMENTS}"
        )
    spi_mode = header[2]
    if spi_mode != expected_mode:
        raise EspSemanticError(
            f"{kind} image SPI mode {spi_mode} does not match expected mode {expected_mode}"
        )
    flash_frequency = header[3] & 0x0F
    flash_size_code = header[3] >> 4
    if flash_frequency != expected_frequency:
        raise EspSemanticError(
            f"{kind} image flash-frequency code 0x{flash_frequency:x} "
            f"does not match 0x{expected_frequency:x}"
        )
    if flash_size_code != expected_flash_size_code:
        raise EspSemanticError(
            f"{kind} image flash-size code {flash_size_code} does not match the board"
        )
    entry_addr = struct.unpack_from("<I", header, 4)[0]
    chip_id = struct.unpack_from("<H", header, 12)[0]
    if chip_id != expected_chip_id:
        raise EspSemanticError(
            f"{kind} image chip ID {chip_id} does not match expected {expected_chip_id}"
        )
    min_revision = struct.unpack_from("<H", header, 15)[0]
    max_revision = struct.unpack_from("<H", header, 17)[0]
    hash_appended = header[23]
    if hash_appended not in (0, 1):
        raise EspSemanticError(f"{kind} image SHA-256 flag {hash_appended} is malformed")
    if hash_appended != 1:
        raise EspSemanticError(
            f"{kind} image must carry an appended SHA-256 (flag is {hash_appended})"
        )

    cursor = image_offset + ESP_IMAGE_HEADER_SIZE
    checksum = ESP_IMAGE_CHECKSUM_INITIAL
    segments: list[EspSegmentInfo] = []
    for index in range(segment_count):
        segment_header = _require_region(
            view, cursor, 8, region_end, f"{kind} segment {index} header"
        )
        load_addr, data_length = struct.unpack_from("<II", segment_header)
        cursor += 8
        if data_length >= ESP_IMAGE_MAX_SEGMENT_BYTES:
            raise EspSemanticError(
                f"{kind} segment {index} size {data_length} exceeds the structural limit"
            )
        if data_length % 4:
            raise EspSemanticError(
                f"{kind} segment {index} size {data_length} is not four-byte aligned"
            )
        data = _require_region(
            view, cursor, data_length, region_end, f"{kind} segment {index} data"
        )
        segments.append(
            EspSegmentInfo(
                load_addr=load_addr,
                data_length=data_length,
                data_offset=cursor,
            )
        )
        checksum = _xor_bytes(checksum, data)
        cursor += data_length

    checksum_offset = cursor + (15 - cursor % 16)
    padding = _require_region(
        view,
        cursor,
        checksum_offset - cursor,
        region_end,
        f"{kind} image checksum padding",
    )
    if any(padding):
        raise EspSemanticError(f"{kind} image has nonzero checksum padding")
    footer = _require_region(view, checksum_offset, 1, region_end, f"{kind} checksum")
    if footer[0] != checksum:
        raise EspSemanticError(
            f"{kind} image checksum mismatch: expected 0x{checksum:02x}, got 0x{footer[0]:02x}"
        )
    digest_start = checksum_offset + 1
    image_end = digest_start
    if hash_appended:
        appended_digest = _require_region(
            view, digest_start, 32, region_end, f"{kind} appended SHA-256"
        )
        expected_digest = hashlib.sha256(view[image_offset:digest_start]).digest()
        if appended_digest.tobytes() != expected_digest:
            raise EspSemanticError(f"{kind} image appended SHA-256 does not match its bytes")
        image_end += 32

    return EspImageInfo(
        start=image_offset,
        end=image_end,
        entry_addr=entry_addr,
        chip_id=chip_id,
        spi_mode=spi_mode,
        flash_size_code=flash_size_code,
        flash_freq_code=flash_frequency,
        min_revision=min_revision,
        max_revision=max_revision,
        segments=tuple(segments),
    )


def _validate_lxveos_image_policy(
    view: memoryview,
    image: EspImageInfo,
    *,
    kind: Literal["bootloader", "app"],
) -> None:
    first = image.segments[0]
    mmu_page_size = _DEFAULT_MMU_PAGE_SIZE
    if kind == "bootloader":
        descriptor = _require_region(
            view,
            first.data_offset,
            80,
            first.data_offset + first.data_length,
            "bootloader descriptor",
        )
        if descriptor[0] != 0x50:
            raise EspSemanticError("bootloader descriptor magic byte is not 0x50")
        idf_version = _decode_descriptor_string(descriptor[8:40], "bootloader IDF version")
        if idf_version != _EXPECTED_IDF_VERSION:
            raise EspSemanticError(
                f"bootloader IDF version {idf_version!r} is not {_EXPECTED_IDF_VERSION!r}"
            )
    else:
        descriptor = _require_region(
            view,
            first.data_offset,
            256,
            first.data_offset + first.data_length,
            "application descriptor",
        )
        if struct.unpack_from("<I", descriptor)[0] != 0xABCD5432:
            raise EspSemanticError("application descriptor magic is not 0xABCD5432")
        _decode_descriptor_string(descriptor[16:48], "application version")
        project_name = _decode_descriptor_string(descriptor[48:80], "application project name")
        if project_name != _EXPECTED_PROJECT_NAME:
            raise EspSemanticError(
                f"application project name {project_name!r} is not {_EXPECTED_PROJECT_NAME!r}"
            )
        idf_version = _decode_descriptor_string(descriptor[112:144], "application IDF version")
        if idf_version != _EXPECTED_IDF_VERSION:
            raise EspSemanticError(
                f"application IDF version {idf_version!r} is not {_EXPECTED_IDF_VERSION!r}"
            )
        min_efuse_block_revision, max_efuse_block_revision = struct.unpack_from(
            "<HH", descriptor, 176
        )
        revision_unset = (0, 0xFFFF)
        if (
            min_efuse_block_revision not in revision_unset
            and max_efuse_block_revision not in revision_unset
            and min_efuse_block_revision > max_efuse_block_revision
        ):
            raise EspSemanticError(
                "application minimum eFuse-block revision exceeds its bounded maximum"
            )
        mmu_exponent = descriptor[180]
        if mmu_exponent != 16:
            raise EspSemanticError(
                f"application MMU-page exponent {mmu_exponent} does not describe 64 KiB"
            )
        mmu_page_size = 1 << mmu_exponent
        drom = _DROM_RANGES_BY_CHIP_ID[image.chip_id]
        if not (
            _in_range(first.load_addr, drom) and first.load_addr + first.data_length <= drom[1]
        ):
            raise EspSemanticError("application descriptor segment is not within chip DROM")

    mapped_ranges = (
        _DROM_RANGES_BY_CHIP_ID[image.chip_id],
        _IROM_RANGES_BY_CHIP_ID[image.chip_id],
    )
    for index, segment in enumerate(image.segments):
        mapped_range = next(
            (bounds for bounds in mapped_ranges if _in_range(segment.load_addr, bounds)),
            None,
        )
        if mapped_range is None:
            continue
        if segment.load_addr + segment.data_length > mapped_range[1]:
            raise EspSemanticError(
                f"{kind} mapped segment {index} is not contained in its mapped range"
            )
        if segment.data_offset % mmu_page_size != segment.load_addr % mmu_page_size:
            raise EspSemanticError(
                f"{kind} mapped segment {index} flash/load MMU alignment does not match"
            )

    if not any(
        segment.data_length
        and segment.load_addr <= image.entry_addr < segment.load_addr + segment.data_length
        for segment in image.segments
    ):
        raise EspSemanticError(f"{kind} entry point is not contained in a nonempty image segment")

    # ESP-IDF treats zero and UINT16_MAX as unbounded maximum-revision sentinels.
    if image.max_revision not in (0, 0xFFFF) and image.min_revision > image.max_revision:
        raise EspSemanticError(f"{kind} minimum chip revision exceeds its bounded maximum")


def _md5_compat(raw: bytes | memoryview) -> bytes:
    try:
        return hashlib.md5(raw, usedforsecurity=False).digest()
    except TypeError:  # pragma: no cover - compatibility with unusual Python builds.
        return hashlib.md5(raw).digest()


def _validate_partition_entries(
    entries: tuple[PartitionInfo, ...], flash_size_bytes: int, label: str
) -> None:
    if not entries:
        raise EspSemanticError(f"{label} contains no partitions")
    names: set[str] = set()
    folded_names: set[str] = set()
    ranges: list[tuple[int, int, str]] = []
    for entry in entries:
        folded = entry.name.casefold()
        if entry.name in names or folded in folded_names:
            raise EspSemanticError(f"{label} contains duplicate partition name {entry.name!r}")
        names.add(entry.name)
        folded_names.add(folded)
        if entry.type not in _TYPE_NAMES.values():
            raise EspSemanticError(f"{label} partition {entry.name!r} has unsupported type")
        known_subtypes = _APP_SUBTYPES if entry.type == _TYPE_NAMES["app"] else _DATA_SUBTYPES
        if entry.subtype not in known_subtypes.values():
            raise EspSemanticError(f"{label} partition {entry.name!r} has unsupported subtype")
        if entry.flags != 0:
            raise EspSemanticError(
                f"{label} partition {entry.name!r} uses unsupported flags 0x{entry.flags:x}"
            )
        if entry.offset < ESP_PARTITION_SECTOR_END:
            raise EspSemanticError(
                f"{label} partition {entry.name!r} begins inside reserved metadata sectors"
            )
        if entry.offset % 0x1000 or entry.size <= 0 or entry.size % 0x1000:
            raise EspSemanticError(
                f"{label} partition {entry.name!r} is not sector-aligned with positive size"
            )
        if entry.type == _TYPE_NAMES["app"] and entry.offset % 0x10000:
            raise EspSemanticError(f"{label} app partition {entry.name!r} is not 0x10000-aligned")
        end = entry.offset + entry.size
        if end > flash_size_bytes:
            raise EspSemanticError(
                f"{label} partition {entry.name!r} extends beyond declared flash"
            )
        ranges.append((entry.offset, end, entry.name))
    ranges.sort()
    for previous, current in zip(ranges, ranges[1:]):
        if current[0] < previous[1]:
            raise EspSemanticError(f"{label} partitions {previous[2]!r} and {current[2]!r} overlap")


def parse_binary_partition_table(
    blob: bytes | bytearray | memoryview, *, flash_size_bytes: int
) -> tuple[PartitionInfo, ...]:
    """Parse the fixed ESP-IDF binary partition-table sector and verify its MD5 marker."""
    flash_size_bytes = _plain_int(flash_size_bytes, "flash_size_bytes")
    view = _byte_view(blob, "merged image")
    table_end = ESP_PARTITION_TABLE_OFFSET + ESP_PARTITION_TABLE_SIZE
    table = _require_region(
        view,
        ESP_PARTITION_TABLE_OFFSET,
        ESP_PARTITION_TABLE_SIZE,
        table_end,
        "binary partition table",
    )
    entries: list[PartitionInfo] = []
    raw_entries = bytearray()
    md5_seen = False
    for cursor in range(0, ESP_PARTITION_TABLE_SIZE, 32):
        record = table[cursor : cursor + 32]
        magic = record[:2].tobytes()
        if magic == b"\xaa\x50":
            if md5_seen:
                raise EspSemanticError("binary partition entry appears after the MD5 marker")
            raw = record.tobytes()
            raw_entries.extend(raw)
            type_id = record[2]
            subtype = record[3]
            offset, size = struct.unpack_from("<II", record, 4)
            name = _decode_partition_name(record[12:28], "binary partition name")
            flags = struct.unpack_from("<I", record, 28)[0]
            entries.append(PartitionInfo(name, type_id, subtype, offset, size, flags))
            continue
        if magic == b"\xeb\xeb":
            if md5_seen:
                raise EspSemanticError("binary partition table has multiple MD5 markers")
            if record[:16].tobytes() != b"\xeb\xeb" + b"\xff" * 14:
                raise EspSemanticError("binary partition-table MD5 marker is malformed")
            expected = _md5_compat(bytes(raw_entries))
            if record[16:32].tobytes() != expected:
                raise EspSemanticError("binary partition-table MD5 does not match its entries")
            md5_seen = True
            continue
        if record.tobytes() == b"\xff" * 32:
            if not md5_seen:
                raise EspSemanticError("binary partition table ended before its MD5 marker")
            if any(value != 0xFF for value in table[cursor:]):
                raise EspSemanticError("binary partition table has non-erased bytes after its end")
            break
        raise EspSemanticError(
            f"binary partition table has invalid record magic {magic.hex()} at +0x{cursor:x}"
        )
    if not md5_seen:
        raise EspSemanticError("binary partition table is missing its MD5 marker")
    result = tuple(entries)
    _validate_partition_entries(result, flash_size_bytes, "binary partition table")
    return result


def _parse_number(value: str, label: str, *, allow_suffix_space: bool = False) -> int:
    text = value.strip()
    if not text:
        raise EspSemanticError(f"{label} must be explicit")
    if "_" in text:
        raise EspSemanticError(f"{label} is not a canonical integer")
    multiplier = 1
    if text[-1:].upper() in ("K", "M"):
        multiplier = 1024 if text[-1].upper() == "K" else 1024 * 1024
        text = text[:-1]
        if allow_suffix_space:
            text = text.rstrip()
        elif text != text.strip():
            raise EspSemanticError(f"{label} is not a canonical integer")
    try:
        number = int(text, 0)
    except (TypeError, ValueError) as exc:
        raise EspSemanticError(f"{label} is not an integer: {value!r}") from exc
    if number < 0:
        raise EspSemanticError(f"{label} must not be negative")
    return number * multiplier


def _parse_csv_enum(
    value: str,
    names: Mapping[str, int],
    label: str,
    *,
    casefold_keywords: bool = False,
) -> int:
    text = value.strip()
    keyword = text.lower() if casefold_keywords else text
    if keyword in names:
        return names[keyword]
    parsed = _parse_number(text, label, allow_suffix_space=True)
    if parsed not in names.values():
        raise EspSemanticError(f"{label} value {value!r} is outside the admitted set")
    return parsed


def parse_partition_csv(
    raw: bytes | bytearray | memoryview, *, flash_size_bytes: int
) -> tuple[PartitionInfo, ...]:
    """Parse the restricted explicit CSV syntax admitted by the current bundle schema."""
    flash_size_bytes = _plain_int(flash_size_bytes, "flash_size_bytes")
    view = _byte_view(raw, "partition CSV")
    if not 0 < len(view) <= _MAX_METADATA_BYTES:
        raise EspSemanticError(f"partition CSV size must be in 1..{_MAX_METADATA_BYTES} bytes")
    try:
        text = view.tobytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EspSemanticError("partition CSV is not UTF-8") from exc
    for line_number, source_line in enumerate(text.splitlines(), 1):
        line = source_line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line or '"' in line or "'" in line:
            raise EspSemanticError(
                f"partition CSV line {line_number} uses unsupported quoting/comment syntax"
            )
    entries: list[PartitionInfo] = []
    try:
        rows = csv.reader(io.StringIO(text, newline=""), strict=True)
        for line_number, row in enumerate(rows, 1):
            if not row or (len(row) == 1 and not row[0].strip()) or row[0].lstrip().startswith("#"):
                continue
            if len(row) not in (5, 6):
                raise EspSemanticError(
                    f"partition CSV line {line_number} must have five or six columns"
                )
            fields = [field.strip() for field in row]
            name = fields[0]
            if not name or len(name.encode("ascii", errors="ignore")) != len(name):
                raise EspSemanticError(
                    f"partition CSV line {line_number} name must be non-empty ASCII"
                )
            if len(name.encode("ascii")) > 16 or any(
                ord(char) < 0x20 or ord(char) > 0x7E for char in name
            ):
                raise EspSemanticError(
                    f"partition CSV line {line_number} name is not a valid binary label"
                )
            type_id = _parse_csv_enum(
                fields[1],
                _TYPE_NAMES,
                f"CSV line {line_number} type",
                casefold_keywords=True,
            )
            subtype_names = _APP_SUBTYPES if type_id == _TYPE_NAMES["app"] else _DATA_SUBTYPES
            subtype = _parse_csv_enum(fields[2], subtype_names, f"CSV line {line_number} subtype")
            offset = _parse_number(
                fields[3], f"CSV line {line_number} offset", allow_suffix_space=True
            )
            size = _parse_number(fields[4], f"CSV line {line_number} size", allow_suffix_space=True)
            flags_text = fields[5].lower() if len(fields) == 6 else ""
            if flags_text:
                raise EspSemanticError(
                    f"partition CSV line {line_number} uses unsupported flags {fields[5]!r}"
                )
            entries.append(PartitionInfo(name, type_id, subtype, offset, size, 0))
    except csv.Error as exc:
        raise EspSemanticError(f"partition CSV is malformed: {exc}") from exc
    result = tuple(entries)
    _validate_partition_entries(result, flash_size_bytes, "partition CSV")
    return result


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EspSemanticError(f"flasher_args.json has duplicate key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise EspSemanticError(f"flasher_args.json contains non-finite number {value}")


def _check_json_nesting(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
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
                raise EspSemanticError("flasher_args.json exceeds the JSON nesting limit")
        elif character in "]}":
            depth -= 1


def _decode_flasher_json(raw: bytes | bytearray | memoryview) -> Mapping[str, Any]:
    view = _byte_view(raw, "flasher_args.json")
    if not 0 < len(view) <= _MAX_METADATA_BYTES:
        raise EspSemanticError(f"flasher_args.json size must be in 1..{_MAX_METADATA_BYTES} bytes")
    try:
        text = view.tobytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EspSemanticError("flasher_args.json is not UTF-8") from exc
    _check_json_nesting(text)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except EspSemanticError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise EspSemanticError(f"flasher_args.json is not valid strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise EspSemanticError("flasher_args.json must contain an object")
    return value


def _mapping_field(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise EspSemanticError(f"flasher_args.json {label} must be an object")
    return value


def _offset_value(value: Any, label: str) -> int:
    if type(value) is not str or not value or value != value.strip() or "_" in value:
        raise EspSemanticError(f"flasher_args.json {label} must be a canonical integer string")
    return _parse_number(value, f"flasher_args.json {label}")


def validate_flasher_args(
    raw: bytes | bytearray | memoryview,
    *,
    chip: str,
    flash_size_bytes: int,
    bootloader_offset: int,
    app_offset: int,
) -> None:
    """Cross-check non-operative ESP-IDF flasher metadata without following its paths."""
    if type(chip) is not str:
        raise EspSemanticError("flasher_args.json chip policy must be a string")
    flash_size_bytes = _plain_int(flash_size_bytes, "flash_size_bytes")
    bootloader_offset = _plain_int(bootloader_offset, "bootloader_offset")
    app_offset = _plain_int(app_offset, "app_offset")
    if chip not in _CHIP_LAYOUTS:
        raise EspSemanticError(f"flasher_args.json has unsupported chip policy {chip!r}")
    if flash_size_bytes not in _FLASH_SIZE_LABELS:
        raise EspSemanticError(f"flasher_args.json has unsupported flash size {flash_size_bytes}")
    document = _decode_flasher_json(raw)
    required_top = {
        "write_flash_args",
        "flash_settings",
        "flash_files",
        "bootloader",
        "partition-table",
        "app",
        "extra_esptool_args",
    }
    optional_top = {"ota_data_initial"}
    actual_top = set(document)
    if not required_top <= actual_top or actual_top - required_top - optional_top:
        raise EspSemanticError(
            "flasher_args.json top-level keys do not match the pinned ESP-IDF output"
        )
    expected_write_args = [
        "--flash-mode",
        "dio",
        "--flash-size",
        _FLASH_SIZE_LABELS[flash_size_bytes],
        "--flash-freq",
        "80m",
    ]
    if document.get("write_flash_args") != expected_write_args:
        raise EspSemanticError("flasher_args.json write_flash_args does not match pinned policy")
    settings = _mapping_field(document.get("flash_settings"), "flash_settings")
    expected_settings = {
        "flash_mode": "dio",
        "flash_size": _FLASH_SIZE_LABELS[flash_size_bytes],
        "flash_freq": "80m",
    }
    if set(settings) != set(expected_settings):
        raise EspSemanticError("flasher_args.json flash_settings keys do not match pinned policy")
    for key, expected in expected_settings.items():
        if settings.get(key) != expected:
            raise EspSemanticError(f"flasher_args.json flash_settings.{key} must be {expected!r}")
    extra = _mapping_field(document.get("extra_esptool_args"), "extra_esptool_args")
    if set(extra) != {"after", "before", "stub", "chip"}:
        raise EspSemanticError(
            "flasher_args.json extra_esptool_args keys do not match pinned policy"
        )
    if extra.get("after") != "hard-reset" or extra.get("before") != "default-reset":
        raise EspSemanticError("flasher_args.json reset policy does not match pinned defaults")
    if extra.get("stub") is not True:
        raise EspSemanticError("flasher_args.json must enable the pinned esptool stub")
    if extra.get("chip") != chip:
        raise EspSemanticError(
            f"flasher_args.json chip {extra.get('chip')!r} does not match {chip!r}"
        )
    flash_files = _mapping_field(document.get("flash_files"), "flash_files")
    normalized_files: dict[int, str] = {}
    for raw_offset, raw_path in flash_files.items():
        if (
            not isinstance(raw_offset, str)
            or type(raw_path) is not str
            or not raw_path
            or raw_path != raw_path.strip()
        ):
            raise EspSemanticError(
                "flasher_args.json flash_files must map offsets to trimmed paths"
            )
        offset = _offset_value(raw_offset, f"flash_files key {raw_offset!r}")
        if offset in normalized_files:
            raise EspSemanticError(
                f"flasher_args.json flash_files repeats normalized offset 0x{offset:x}"
            )
        normalized_files[offset] = raw_path

    expected_entries = [
        ("bootloader", bootloader_offset),
        ("partition-table", ESP_PARTITION_TABLE_OFFSET),
        ("app", app_offset),
    ]
    if "ota_data_initial" in document and flash_size_bytes != 8 * 1024 * 1024:
        raise EspSemanticError("flasher_args.json ota_data_initial is only valid for 8 MiB OTA")
    if "ota_data_initial" in document:
        expected_entries.append(("ota_data_initial", 0xF000))
    expected_offsets: set[int] = set()
    for name, expected_offset in expected_entries:
        entry = _mapping_field(document.get(name), name)
        if set(entry) != {"offset", "file", "encrypted"}:
            raise EspSemanticError(f"flasher_args.json {name} keys do not match pinned policy")
        if entry.get("encrypted") != "false":
            raise EspSemanticError(f"flasher_args.json {name} must describe plaintext output")
        actual_offset = _offset_value(entry.get("offset"), f"{name}.offset")
        if actual_offset != expected_offset:
            raise EspSemanticError(
                f"flasher_args.json {name}.offset 0x{actual_offset:x} "
                f"does not match 0x{expected_offset:x}"
            )
        path = entry.get("file")
        if type(path) is not str or not path or path != path.strip():
            raise EspSemanticError(
                f"flasher_args.json {name}.file must be a non-empty trimmed string"
            )
        if normalized_files.get(expected_offset) != path:
            raise EspSemanticError(
                f"flasher_args.json flash_files disagrees with {name} at 0x{expected_offset:x}"
            )
        expected_offsets.add(expected_offset)
    if set(normalized_files) != expected_offsets:
        raise EspSemanticError(
            "flasher_args.json flash_files contains offsets outside its named image entries"
        )


def validate_lxveos_idf602_merged(
    blob: bytes | bytearray | memoryview,
    *,
    chip: str,
    flash_size_bytes: int,
    partition_csv: bytes | bytearray | memoryview,
    flasher_args: bytes | bytearray | memoryview,
) -> MergedImageInfo:
    """Validate one complete current LxveOS merged image and its hashed metadata witnesses."""
    if type(chip) is not str:
        raise EspSemanticError("chip must be a string")
    flash_size_bytes = _plain_int(flash_size_bytes, "flash_size_bytes")
    if chip not in _CHIP_LAYOUTS:
        raise EspSemanticError(f"unsupported ESP chip for FirmwareArtifact@1: {chip!r}")
    if flash_size_bytes not in _FLASH_SIZE_CODES:
        raise EspSemanticError(f"unsupported flash size for FirmwareArtifact@1: {flash_size_bytes}")
    if (chip, flash_size_bytes) not in _LXVEOS_CHIP_FLASH_MATRIX:
        raise EspSemanticError(
            f"chip/flash combination is outside {POLICY_ID}: {chip}/{flash_size_bytes}"
        )
    view = _byte_view(blob, "merged image")
    if not 0 < len(view) <= flash_size_bytes:
        raise EspSemanticError(
            f"merged image size {len(view)} is outside 1..{flash_size_bytes} bytes"
        )
    bootloader_offset, chip_id = _CHIP_LAYOUTS[chip]
    flash_size_code = _FLASH_SIZE_CODES[flash_size_bytes]
    bootloader = validate_esp_image(
        view,
        image_offset=bootloader_offset,
        region_end=ESP_PARTITION_TABLE_OFFSET,
        expected_chip_id=chip_id,
        expected_flash_size_code=flash_size_code,
        kind="bootloader",
    )
    _validate_lxveos_image_policy(view, bootloader, kind="bootloader")
    binary_partitions = parse_binary_partition_table(view, flash_size_bytes=flash_size_bytes)
    csv_partitions = parse_partition_csv(partition_csv, flash_size_bytes=flash_size_bytes)
    if binary_partitions != csv_partitions:
        raise EspSemanticError("binary partition table does not exactly match packaged CSV")
    if binary_partitions != _expected_lxveos_partitions(flash_size_bytes):
        raise EspSemanticError("partition layout is not the pinned LxveOS ESP-IDF v6.0.2 layout")

    expected_name = "factory" if flash_size_bytes == 4 * 1024 * 1024 else "ota_0"
    matches = [
        entry
        for entry in binary_partitions
        if entry.name == expected_name and entry.type == _TYPE_NAMES["app"]
    ]
    if len(matches) != 1:
        raise EspSemanticError(f"partition table must contain exactly one {expected_name!r} app")
    app_partition = matches[0]
    app = validate_esp_image(
        view,
        image_offset=app_partition.offset,
        region_end=app_partition.offset + app_partition.size,
        expected_chip_id=chip_id,
        expected_flash_size_code=flash_size_code,
        kind="app",
    )
    _validate_lxveos_image_policy(view, app, kind="app")
    admitted_intervals = (
        (bootloader.start, bootloader.end),
        (
            ESP_PARTITION_TABLE_OFFSET,
            ESP_PARTITION_TABLE_OFFSET + ESP_PARTITION_TABLE_SIZE,
        ),
        (app.start, app.end),
    )
    cursor = 0
    for start, end in admitted_intervals:
        present_end = min(start, len(view))
        unexpected = next(
            (offset for offset in range(cursor, present_end) if view[offset] != 0xFF),
            None,
        )
        if unexpected is not None:
            raise EspSemanticError(
                "merged image has a non-erased byte outside the admitted bootloader, "
                f"partition table, and selected app intervals at 0x{unexpected:x}"
            )
        cursor = min(end, len(view))
    unexpected = next(
        (offset for offset in range(cursor, len(view)) if view[offset] != 0xFF),
        None,
    )
    if unexpected is not None:
        raise EspSemanticError(
            "merged image has a non-erased byte outside the admitted bootloader, "
            f"partition table, and selected app intervals at 0x{unexpected:x}"
        )

    validate_flasher_args(
        flasher_args,
        chip=chip,
        flash_size_bytes=flash_size_bytes,
        bootloader_offset=bootloader_offset,
        app_offset=app_partition.offset,
    )
    return MergedImageInfo(
        chip=chip,
        flash_size_bytes=flash_size_bytes,
        bootloader=bootloader,
        app=app,
        app_partition=app_partition,
        partitions=binary_partitions,
    )


# Producer and consumer intentionally expose the same primary vocabulary.
PartitionEntry = PartitionInfo
validate_merged_image = validate_lxveos_idf602_merged
validate_merged_firmware = validate_lxveos_idf602_merged


__all__ = [
    "ESP_IMAGE_HEADER_SIZE",
    "ESP_IMAGE_MAGIC",
    "ESP_PARTITION_TABLE_OFFSET",
    "ESP_PARTITION_TABLE_SIZE",
    "EspImageInfo",
    "EspSegmentInfo",
    "EspSemanticError",
    "MergedImageInfo",
    "POLICY_ID",
    "PartitionEntry",
    "PartitionInfo",
    "SemanticValidationError",
    "parse_binary_partition_table",
    "parse_partition_csv",
    "validate_esp_image",
    "validate_flasher_args",
    "validate_lxveos_idf602_merged",
    "validate_merged_firmware",
    "validate_merged_image",
]
