"""Independent deterministic ESP-IDF-like byte vectors for artifact tests.

These builders intentionally do not import the production semantic parser.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass

MIB = 1024 * 1024
PARTITION_TABLE_OFFSET = 0x8000
PARTITION_TABLE_SIZE = 0xC00


@dataclass(frozen=True)
class VectorPartition:
    name: str
    type: int
    subtype: int
    offset: int
    size: int
    flags: int = 0


PARTITIONS_4MB = (
    VectorPartition("nvs", 1, 2, 0x9000, 0x6000),
    VectorPartition("phy_init", 1, 1, 0xF000, 0x1000),
    VectorPartition("factory", 0, 0, 0x10000, 0x2D0000),
    VectorPartition("storage", 1, 0x81, 0x2E0000, 0x110000),
)
PARTITIONS_8MB = (
    VectorPartition("nvs", 1, 2, 0x9000, 0x6000),
    VectorPartition("otadata", 1, 0, 0xF000, 0x2000),
    VectorPartition("phy_init", 1, 1, 0x11000, 0x1000),
    VectorPartition("ota_0", 0, 0x10, 0x20000, 0x2A0000),
    VectorPartition("ota_1", 0, 0x11, 0x2C0000, 0x2A0000),
    VectorPartition("storage", 1, 0x81, 0x560000, 0x2A0000),
)


# Byte-exact CSV witnesses retained by the independent efc8b49/cf3e287
# differential review.  They all generate the same 4 MiB partition table with
# ESP-IDF v6.0.2; keeping the original comments and spacing makes the
# representation boundary reproducible rather than merely equivalent.
REVIEW_PARTITION_CSV_4MB = (
    "# 4 MB, factory-only (most CYDs / classic sticks). A feature-rich LVGL build barely "
    "fits two OTA slots\n"
    "# at 4 MB, so default to a single factory app + a data partition. Sizes are "
    "PROVISIONAL — set from the\n"
    "# measured release binary. Name,   Type, SubType, Offset,   Size\n"
    "nvs,      data, nvs,     0x9000,   0x6000\n"
    "phy_init, data, phy,     0xf000,   0x1000\n"
    "factory,  app,  factory, 0x10000,  0x2D0000\n"
    "storage,  data, fat,     0x2E0000, 0x110000\n"
).encode()
REVIEW_PARTITION_CSV_WHITESPACE_LINE = REVIEW_PARTITION_CSV_4MB + b" \t \n"
REVIEW_PARTITION_CSV_UPPERCASE_TYPES = (
    REVIEW_PARTITION_CSV_4MB.replace(b"factory app + a data", b"factory APP + a DATA")
    .replace(b" data,", b" DATA,")
    .replace(b"  app,", b"  APP,")
)
REVIEW_PARTITION_CSV_SUFFIX_SPACE = REVIEW_PARTITION_CSV_4MB.replace(
    b"nvs,      data, nvs,     0x9000,   0x6000",
    b"nvs,      data, nvs,     0x9000,24 K",
)


def partitions_for(flash_size_bytes: int) -> tuple[VectorPartition, ...]:
    if flash_size_bytes == 4 * MIB:
        return PARTITIONS_4MB
    if flash_size_bytes == 8 * MIB:
        return PARTITIONS_8MB
    raise ValueError(f"unsupported vector flash size: {flash_size_bytes}")


def partition_csv_bytes(flash_size_bytes: int) -> bytes:
    type_names = {0: "app", 1: "data"}
    app_names = {0: "factory", **{0x10 + index: f"ota_{index}" for index in range(16)}}
    data_names = {0: "ota", 1: "phy", 2: "nvs", 0x81: "fat"}
    lines = ["# Name, Type, SubType, Offset, Size"]
    for entry in partitions_for(flash_size_bytes):
        subtype_names = app_names if entry.type == 0 else data_names
        lines.append(
            f"{entry.name},{type_names[entry.type]},{subtype_names[entry.subtype]},"
            f"0x{entry.offset:x},0x{entry.size:x}"
        )
    return ("\n".join(lines) + "\n").encode("ascii")


def binary_partition_table(
    flash_size_bytes: int,
    *,
    entries: tuple[VectorPartition, ...] | None = None,
) -> bytes:
    rows = bytearray()
    for entry in entries or partitions_for(flash_size_bytes):
        label = entry.name.encode("ascii")
        if len(label) > 16:
            raise ValueError("partition label is too long")
        rows.extend(
            struct.pack(
                "<2sBBII16sI",
                b"\xaa\x50",
                entry.type,
                entry.subtype,
                entry.offset,
                entry.size,
                label.ljust(16, b"\x00"),
                entry.flags,
            )
        )
    marker = b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(rows, usedforsecurity=False).digest()
    result = bytes(rows) + marker
    return result.ljust(PARTITION_TABLE_SIZE, b"\xff")


def _c_string(value: bytes, size: int) -> bytes:
    if not value or len(value) >= size:
        raise ValueError("test vector C string must fit with a NUL terminator")
    return value.ljust(size, b"\x00")


def boot_descriptor(*, idf_version: bytes = b"v6.0.2") -> bytes:
    result = bytearray(80)
    result[0] = 0x50
    result[8:40] = _c_string(idf_version, 32)
    return bytes(result)


def app_descriptor(
    *,
    version: bytes = b"test-vector",
    project: bytes = b"lxveos",
    idf_version: bytes = b"v6.0.2",
    min_efuse_block_revision: int = 0,
    max_efuse_block_revision: int = 0,
) -> bytes:
    result = bytearray(256)
    struct.pack_into("<I", result, 0, 0xABCD5432)
    result[16:48] = _c_string(version, 32)
    result[48:80] = _c_string(project, 32)
    result[112:144] = _c_string(idf_version, 32)
    struct.pack_into(
        "<HH",
        result,
        176,
        min_efuse_block_revision,
        max_efuse_block_revision,
    )
    result[180] = 16
    return bytes(result)


def esp_image_bytes(
    *,
    kind: str,
    chip_id: int,
    flash_size_code: int,
    first_segment: bytes | None = None,
    extra_segments: tuple[tuple[int, bytes], ...] = (),
    spi_mode: int = 2,
    flash_frequency_code: int = 0xF,
    hash_appended: int = 1,
    entry_addr: int = 0x40000000,
    min_revision: int = 0,
    max_revision: int = 0xFFFF,
    first_load_addr: int | None = None,
) -> bytes:
    if first_segment is None:
        first_segment = boot_descriptor() if kind == "bootloader" else app_descriptor()
    if first_load_addr is None:
        if kind == "app":
            first_load_addr = 0x3F400020 if chip_id == 0 else 0x3C000020
        else:
            first_load_addr = 0x3FFF0000 if chip_id == 0 else 0x3FC80000
    segments = ((first_load_addr, first_segment), *extra_segments)
    if not 1 <= len(segments) <= 255:
        raise ValueError("invalid vector segment count")
    header = bytearray(24)
    header[0] = 0xE9
    header[1] = len(segments)
    header[2] = spi_mode
    header[3] = (flash_size_code << 4) | flash_frequency_code
    struct.pack_into("<I", header, 4, entry_addr)
    struct.pack_into("<H", header, 12, chip_id)
    struct.pack_into("<H", header, 15, min_revision)
    struct.pack_into("<H", header, 17, max_revision)
    header[23] = hash_appended
    image = bytearray(header)
    checksum = 0xEF
    for load_addr, payload in segments:
        if len(payload) % 4:
            raise ValueError("vector segment payload must be four-byte aligned")
        image.extend(struct.pack("<II", load_addr, len(payload)))
        image.extend(payload)
        for value in payload:
            checksum ^= value
    checksum_offset = len(image) + (15 - len(image) % 16)
    image.extend(b"\x00" * (checksum_offset - len(image)))
    image.append(checksum)
    image.extend(hashlib.sha256(image).digest())
    return bytes(image)


def flasher_args_bytes(
    *, chip: str, flash_size_bytes: int, bootloader_offset: int, app_offset: int
) -> bytes:
    flash_size = f"{flash_size_bytes // MIB}MB"
    flash_files = {
        f"0x{bootloader_offset:x}": "bootloader/bootloader.bin",
        "0x8000": "partition_table/partition-table.bin",
        f"0x{app_offset:x}": "lxveos.bin",
    }
    value = {
        "write_flash_args": [
            "--flash-mode",
            "dio",
            "--flash-size",
            flash_size,
            "--flash-freq",
            "80m",
        ],
        "flash_settings": {
            "flash_mode": "dio",
            "flash_size": flash_size,
            "flash_freq": "80m",
        },
        "flash_files": flash_files,
        "bootloader": {
            "offset": f"0x{bootloader_offset:x}",
            "file": "bootloader/bootloader.bin",
            "encrypted": "false",
        },
        "partition-table": {
            "offset": "0x8000",
            "file": "partition_table/partition-table.bin",
            "encrypted": "false",
        },
        "app": {"offset": f"0x{app_offset:x}", "file": "lxveos.bin", "encrypted": "false"},
        "extra_esptool_args": {
            "after": "hard-reset",
            "before": "default-reset",
            "stub": True,
            "chip": chip,
        },
    }
    if flash_size_bytes == 8 * MIB:
        value["otadata"] = {
            "offset": "0xf000",
            "file": "ota_data_initial.bin",
            "encrypted": "false",
        }
        flash_files["0xf000"] = "ota_data_initial.bin"
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def merged_image_bytes(
    *,
    chip: str,
    flash_size_bytes: int,
    seed: bytes = b"one",
    boot_extra_segments: tuple[tuple[int, bytes], ...] = (),
    app_extra_segments: tuple[tuple[int, bytes], ...] = (),
    boot_first_segment: bytes | None = None,
    app_first_segment: bytes | None = None,
    app_first_load_addr: int | None = None,
    boot_entry_addr: int | None = None,
    app_entry_addr: int | None = None,
    boot_min_revision: int = 0,
    boot_max_revision: int | None = None,
    app_min_revision: int = 0,
    app_max_revision: int | None = None,
    spi_mode: int = 2,
    flash_frequency_code: int = 0xF,
) -> tuple[bytes, bytes, bytes]:
    chip_id = {"esp32": 0, "esp32s3": 9}[chip]
    bootloader_offset = {"esp32": 0x1000, "esp32s3": 0}[chip]
    flash_size_code = {4 * MIB: 2, 8 * MIB: 3}[flash_size_bytes]
    app_offset = 0x10000 if flash_size_bytes == 4 * MIB else 0x20000
    boot_code_address = {"esp32": 0x40080400, "esp32s3": 0x403CB700}[chip]
    app_code_address = {"esp32": 0x40080000, "esp32s3": 0x40374000}[chip]
    corpus_max_revision = {"esp32": 399, "esp32s3": 99}[chip]
    bootloader = esp_image_bytes(
        kind="bootloader",
        chip_id=chip_id,
        flash_size_code=flash_size_code,
        first_segment=boot_first_segment,
        extra_segments=((boot_code_address, b"BOOT"), *boot_extra_segments),
        entry_addr=boot_code_address if boot_entry_addr is None else boot_entry_addr,
        min_revision=boot_min_revision,
        max_revision=(corpus_max_revision if boot_max_revision is None else boot_max_revision),
        spi_mode=spi_mode,
        flash_frequency_code=flash_frequency_code,
    )
    version = (b"test-" + seed)[:31]
    app = esp_image_bytes(
        kind="app",
        chip_id=chip_id,
        flash_size_code=flash_size_code,
        first_segment=app_first_segment
        or app_descriptor(
            version=version,
            max_efuse_block_revision=99 if chip == "esp32" else 199,
        ),
        extra_segments=((app_code_address, b"APP!"), *app_extra_segments),
        first_load_addr=app_first_load_addr,
        entry_addr=app_code_address if app_entry_addr is None else app_entry_addr,
        min_revision=app_min_revision,
        max_revision=corpus_max_revision if app_max_revision is None else app_max_revision,
        spi_mode=spi_mode,
        flash_frequency_code=flash_frequency_code,
    )
    merged = bytearray(b"\xff" * (app_offset + len(app)))
    merged[bootloader_offset : bootloader_offset + len(bootloader)] = bootloader
    table = binary_partition_table(flash_size_bytes)
    merged[PARTITION_TABLE_OFFSET : PARTITION_TABLE_OFFSET + len(table)] = table
    merged[app_offset : app_offset + len(app)] = app
    return (
        bytes(merged),
        partition_csv_bytes(flash_size_bytes),
        flasher_args_bytes(
            chip=chip,
            flash_size_bytes=flash_size_bytes,
            bootloader_offset=bootloader_offset,
            app_offset=app_offset,
        ),
    )
