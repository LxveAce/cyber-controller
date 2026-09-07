"""Deterministic structural tests for current LxveOS ESP image admission."""

from __future__ import annotations

import hashlib
import json
import struct

import pytest

from src.core import esp_image_semantics as semantics
from tests.esp_image_vectors import (
    MIB,
    PARTITION_TABLE_OFFSET,
    PARTITION_TABLE_SIZE,
    PARTITIONS_4MB,
    REVIEW_PARTITION_CSV_SUFFIX_SPACE,
    REVIEW_PARTITION_CSV_UPPERCASE_TYPES,
    REVIEW_PARTITION_CSV_WHITESPACE_LINE,
    VectorPartition,
    app_descriptor,
    binary_partition_table,
    boot_descriptor,
    esp_image_bytes,
    flasher_args_bytes,
    merged_image_bytes,
)


def _replace_json(raw: bytes, mutation) -> bytes:
    value = json.loads(raw)
    mutation(value)
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _with_table(merged: bytes, table: bytes) -> bytes:
    result = bytearray(merged)
    result[PARTITION_TABLE_OFFSET : PARTITION_TABLE_OFFSET + PARTITION_TABLE_SIZE] = table
    return bytes(result)


def _empty_app_version() -> bytes:
    value = bytearray(app_descriptor())
    value[16:48] = bytes(32)
    return bytes(value)


def _noncanonical_boot_string() -> bytes:
    value = bytearray(boot_descriptor())
    value[8:40] = b"v6.0.2\x00unexpected".ljust(32, b"\x00")
    return bytes(value)


def _unterminated_app_version() -> bytes:
    value = bytearray(app_descriptor())
    value[16:48] = b"A" * 32
    return bytes(value)


def test_aligned_segment_cursor_uses_fifteen_padding_bytes_before_checksum():
    assert semantics.POLICY_ID == "LxveOSEspImagePolicy@1"
    image = esp_image_bytes(kind="bootloader", chip_id=0, flash_size_code=2)

    # 24-byte header + 8-byte segment header + 80-byte descriptor ends at aligned offset 112.
    # The ESP footer checksum is at 127, not 111.
    assert image[112:127] == b"\x00" * 15
    info = semantics.validate_esp_image(
        image,
        image_offset=0,
        region_end=len(image),
        expected_chip_id=0,
        expected_flash_size_code=2,
        kind="bootloader",
    )
    assert info.end == 160
    assert info.segments[0].data_offset == 32


def test_zero_length_segment_is_structurally_valid():
    image = esp_image_bytes(
        kind="app",
        chip_id=9,
        flash_size_code=3,
        extra_segments=((0x3FC88000, b""),),
    )
    info = semantics.validate_esp_image(
        image,
        image_offset=0,
        region_end=len(image),
        expected_chip_id=9,
        expected_flash_size_code=3,
        kind="app",
    )

    assert tuple(segment.data_length for segment in info.segments) == (256, 0)


def test_generic_image_parser_can_apply_a_non_lxveos_spi_policy():
    image = esp_image_bytes(
        kind="bootloader",
        chip_id=0,
        flash_size_code=2,
        spi_mode=0,
        flash_frequency_code=0,
    )

    parsed = semantics.validate_esp_image(
        image,
        image_offset=0,
        region_end=len(image),
        expected_chip_id=0,
        expected_flash_size_code=2,
        expected_mode=0,
        expected_frequency=0,
        kind="bootloader",
    )

    assert parsed.spi_mode == 0
    assert parsed.flash_freq_code == 0


def test_public_parsers_normalize_non_bytes_inputs():
    with pytest.raises(semantics.SemanticValidationError, match="bytes-like"):
        semantics.validate_esp_image(
            "not bytes",
            image_offset=0,
            region_end=9,
            expected_chip_id=0,
            expected_flash_size_code=2,
            kind="bootloader",
        )
    with pytest.raises(semantics.SemanticValidationError, match="bytes-like"):
        semantics.parse_partition_csv("not bytes", flash_size_bytes=4 * MIB)
    with pytest.raises(semantics.SemanticValidationError, match="bytes-like"):
        semantics.validate_flasher_args(
            "not bytes",
            chip="esp32",
            flash_size_bytes=4 * MIB,
            bootloader_offset=0x1000,
            app_offset=0x10000,
        )


def test_lxveos_policy_rejects_an_otherwise_valid_non_dio_image():
    merged, partition_csv, flasher_args = merged_image_bytes(
        chip="esp32",
        flash_size_bytes=4 * MIB,
        spi_mode=0,
        flash_frequency_code=0,
    )

    with pytest.raises(semantics.SemanticValidationError, match="SPI mode"):
        semantics.validate_lxveos_idf602_merged(
            merged,
            chip="esp32",
            flash_size_bytes=4 * MIB,
            partition_csv=partition_csv,
            flasher_args=flasher_args,
        )


@pytest.mark.parametrize(
    "chip,flash_size,boot_offset,app_offset,chip_id",
    [
        ("esp32", 4 * MIB, 0x1000, 0x10000, 0),
        ("esp32", 8 * MIB, 0x1000, 0x20000, 0),
        ("esp32s3", 8 * MIB, 0x0000, 0x20000, 9),
    ],
)
def test_accepts_current_classic_and_s3_merged_layouts(
    chip, flash_size, boot_offset, app_offset, chip_id
):
    merged, partition_csv, flasher_args = merged_image_bytes(chip=chip, flash_size_bytes=flash_size)

    result = semantics.validate_merged_image(
        merged,
        chip=chip,
        flash_size_bytes=flash_size,
        partition_csv=partition_csv,
        flasher_args=flasher_args,
    )

    assert result.bootloader.start == boot_offset
    assert result.app.start == app_offset
    assert result.bootloader.chip_id == result.app.chip_id == chip_id
    assert result.app_partition.name == ("factory" if flash_size == 4 * MIB else "ota_0")


def test_named_policy_rejects_s3_four_megabyte_non_board_combination():
    merged, partition_csv, flasher_args = merged_image_bytes(
        chip="esp32s3", flash_size_bytes=4 * MIB
    )

    with pytest.raises(semantics.SemanticValidationError, match="outside"):
        semantics.validate_lxveos_idf602_merged(
            merged,
            chip="esp32s3",
            flash_size_bytes=4 * MIB,
            partition_csv=partition_csv,
            flasher_args=flasher_args,
        )


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"chip_id": 9}, "chip ID"),
        ({"flash_size_code": 3}, "flash-size code"),
        ({"spi_mode": 0}, "SPI mode"),
        ({"flash_frequency_code": 0}, "flash-frequency"),
        ({"hash_appended": 0}, "appended SHA-256"),
        ({"hash_appended": 2}, "SHA-256 flag"),
    ],
)
def test_rejects_internally_hashed_but_incompatible_headers(overrides, match):
    values = {"kind": "bootloader", "chip_id": 0, "flash_size_code": 2, **overrides}
    image = esp_image_bytes(**values)

    with pytest.raises(semantics.SemanticValidationError, match=match):
        semantics.validate_esp_image(
            image,
            image_offset=0,
            region_end=len(image),
            expected_chip_id=0,
            expected_flash_size_code=2,
            kind="bootloader",
        )


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda data: data.__setitem__(0, 0), "magic"),
        (lambda data: data.__setitem__(1, 0), "segment count"),
        (lambda data: data.__setitem__(1, 17), "segment count"),
        (lambda data: struct.pack_into("<I", data, 28, 3), "four-byte aligned"),
        (lambda data: struct.pack_into("<I", data, 28, 0x1000000), "structural limit"),
    ],
)
def test_rejects_malformed_image_boundaries_before_trusting_footers(mutation, match):
    image = bytearray(esp_image_bytes(kind="bootloader", chip_id=0, flash_size_code=2))
    mutation(image)

    with pytest.raises(semantics.SemanticValidationError, match=match):
        semantics.validate_esp_image(
            image,
            image_offset=0,
            region_end=len(image),
            expected_chip_id=0,
            expected_flash_size_code=2,
            kind="bootloader",
        )


@pytest.mark.parametrize("cut", [1, 23, 24, 31, 32, 111, 127, 128, 159])
def test_truncation_at_each_image_phase_is_normalized(cut):
    image = esp_image_bytes(kind="bootloader", chip_id=0, flash_size_code=2)

    with pytest.raises(semantics.SemanticValidationError, match="truncated|crosses"):
        semantics.validate_esp_image(
            image[:cut],
            image_offset=0,
            region_end=cut,
            expected_chip_id=0,
            expected_flash_size_code=2,
            kind="bootloader",
        )


@pytest.mark.parametrize(
    "offset,match",
    [
        (112, "padding"),
        (127, "checksum mismatch"),
        (128, "SHA-256"),
    ],
)
def test_rejects_bad_padding_checksum_and_appended_digest(offset, match):
    image = bytearray(esp_image_bytes(kind="bootloader", chip_id=0, flash_size_code=2))
    image[offset] ^= 1

    with pytest.raises(semantics.SemanticValidationError, match=match):
        semantics.validate_esp_image(
            image,
            image_offset=0,
            region_end=len(image),
            expected_chip_id=0,
            expected_flash_size_code=2,
            kind="bootloader",
        )


@pytest.mark.parametrize(
    "kind,first_segment,match",
    [
        ("bootloader", bytes(80), "descriptor magic"),
        ("bootloader", boot_descriptor(idf_version=b"v5.5"), "IDF version"),
        ("bootloader", _noncanonical_boot_string(), "NUL terminator"),
        ("app", bytes(256), "descriptor magic"),
        ("app", app_descriptor(project=b"other"), "project name"),
        ("app", app_descriptor(idf_version=b"v5.5"), "IDF version"),
        ("app", _empty_app_version(), "must be non-empty"),
        ("app", _unterminated_app_version(), "NUL-terminated"),
    ],
)
def test_rejects_wrong_boot_and_app_descriptors(kind, first_segment, match):
    merged, partition_csv, flasher_args = merged_image_bytes(
        chip="esp32",
        flash_size_bytes=4 * MIB,
        boot_first_segment=first_segment if kind == "bootloader" else None,
        app_first_segment=first_segment if kind == "app" else None,
    )

    with pytest.raises(semantics.SemanticValidationError, match=match):
        semantics.validate_lxveos_idf602_merged(
            merged,
            chip="esp32",
            flash_size_bytes=4 * MIB,
            partition_csv=partition_csv,
            flasher_args=flasher_args,
        )


@pytest.mark.parametrize(
    "chip,load_addr",
    [
        ("esp32", 0x3F400024),
        ("esp32s3", 0x3C000024),
    ],
)
def test_rejects_four_byte_mapped_load_shift_with_recomputed_inner_digest(chip, load_addr):
    merged, partition_csv, flasher_args = merged_image_bytes(
        chip=chip,
        flash_size_bytes=8 * MIB,
        app_first_load_addr=load_addr,
    )

    with pytest.raises(semantics.SemanticValidationError, match="MMU alignment"):
        semantics.validate_lxveos_idf602_merged(
            merged,
            chip=chip,
            flash_size_bytes=8 * MIB,
            partition_csv=partition_csv,
            flasher_args=flasher_args,
        )


@pytest.mark.parametrize(
    "chip,load_addr",
    [
        ("esp32", 0x3F400134),
        ("esp32s3", 0x3C000134),
    ],
)
def test_zero_length_mapped_segment_still_requires_flash_load_congruence(chip, load_addr):
    merged, partition_csv, flasher_args = merged_image_bytes(
        chip=chip,
        flash_size_bytes=8 * MIB,
        app_extra_segments=((load_addr, b""),),
    )
    result = semantics.validate_lxveos_idf602_merged(
        merged,
        chip=chip,
        flash_size_bytes=8 * MIB,
        partition_csv=partition_csv,
        flasher_args=flasher_args,
    )
    assert result.app.segments[-1].data_length == 0

    shifted, partition_csv, flasher_args = merged_image_bytes(
        chip=chip,
        flash_size_bytes=8 * MIB,
        app_extra_segments=((load_addr + 4, b""),),
    )
    with pytest.raises(semantics.SemanticValidationError, match="MMU alignment"):
        semantics.validate_lxveos_idf602_merged(
            shifted,
            chip=chip,
            flash_size_bytes=8 * MIB,
            partition_csv=partition_csv,
            flasher_args=flasher_args,
        )


def test_reserved_zero_length_dummy_load_addresses_remain_valid():
    merged, partition_csv, flasher_args = merged_image_bytes(
        chip="esp32",
        flash_size_bytes=4 * MIB,
        app_extra_segments=((0, b""), (4, b"")),
    )

    result = semantics.validate_lxveos_idf602_merged(
        merged,
        chip="esp32",
        flash_size_bytes=4 * MIB,
        partition_csv=partition_csv,
        flasher_args=flasher_args,
    )

    assert tuple(segment.load_addr for segment in result.app.segments[-2:]) == (0, 4)


@pytest.mark.parametrize(
    "chip,mapped_end",
    [
        ("esp32", 0x3F800000),
        ("esp32", 0x40400000),
        ("esp32s3", 0x3E000000),
        ("esp32s3", 0x44000000),
    ],
)
def test_nonempty_mapped_segment_must_fit_entirely_in_its_range(chip, mapped_end):
    # The third segment's absolute data offset is page-congruent to 0x134. Start one page
    # below the mapped upper bound so length, rather than alignment, causes the rejection.
    load_addr = mapped_end - 0x10000 + 0x134
    merged, partition_csv, flasher_args = merged_image_bytes(
        chip=chip,
        flash_size_bytes=8 * MIB,
        app_extra_segments=((load_addr, b"X" * 0x10000),),
    )

    with pytest.raises(semantics.SemanticValidationError, match="contained in its mapped range"):
        semantics.validate_lxveos_idf602_merged(
            merged,
            chip=chip,
            flash_size_bytes=8 * MIB,
            partition_csv=partition_csv,
            flasher_args=flasher_args,
        )


@pytest.mark.parametrize("kind", ["bootloader", "app"])
def test_named_policy_entrypoint_must_be_in_a_nonempty_segment(kind):
    overrides = {f"{kind.replace('loader', '')}_entry_addr": 0xDEADBEEF}
    merged, partition_csv, flasher_args = merged_image_bytes(
        chip="esp32",
        flash_size_bytes=4 * MIB,
        **overrides,
    )

    with pytest.raises(semantics.SemanticValidationError, match="entry point"):
        semantics.validate_lxveos_idf602_merged(
            merged,
            chip="esp32",
            flash_size_bytes=4 * MIB,
            partition_csv=partition_csv,
            flasher_args=flasher_args,
        )


def test_zero_length_segment_does_not_satisfy_named_entrypoint_policy():
    merged, partition_csv, flasher_args = merged_image_bytes(
        chip="esp32",
        flash_size_bytes=4 * MIB,
        app_entry_addr=0x3FFB0000,
        app_extra_segments=((0x3FFB0000, b""),),
    )

    with pytest.raises(semantics.SemanticValidationError, match="nonempty image segment"):
        semantics.validate_lxveos_idf602_merged(
            merged,
            chip="esp32",
            flash_size_bytes=4 * MIB,
            partition_csv=partition_csv,
            flasher_args=flasher_args,
        )


@pytest.mark.parametrize("kind", ["boot", "app"])
def test_named_policy_rejects_inverted_bounded_revision_interval(kind):
    overrides = {
        f"{kind}_min_revision": 400,
        f"{kind}_max_revision": 399,
    }
    merged, partition_csv, flasher_args = merged_image_bytes(
        chip="esp32",
        flash_size_bytes=4 * MIB,
        **overrides,
    )

    with pytest.raises(semantics.SemanticValidationError, match="revision"):
        semantics.validate_lxveos_idf602_merged(
            merged,
            chip="esp32",
            flash_size_bytes=4 * MIB,
            partition_csv=partition_csv,
            flasher_args=flasher_args,
        )


@pytest.mark.parametrize("unbounded_max", [0, 0xFFFF])
def test_named_revision_policy_honors_unbounded_maximum_sentinels(unbounded_max):
    merged, partition_csv, flasher_args = merged_image_bytes(
        chip="esp32",
        flash_size_bytes=4 * MIB,
        app_min_revision=400,
        app_max_revision=unbounded_max,
    )

    result = semantics.validate_lxveos_idf602_merged(
        merged,
        chip="esp32",
        flash_size_bytes=4 * MIB,
        partition_csv=partition_csv,
        flasher_args=flasher_args,
    )

    assert result.app.max_revision == unbounded_max


def test_named_policy_rejects_inverted_bounded_app_efuse_revision_interval():
    merged, partition_csv, flasher_args = merged_image_bytes(
        chip="esp32s3",
        flash_size_bytes=8 * MIB,
        app_first_segment=app_descriptor(
            min_efuse_block_revision=2,
            max_efuse_block_revision=1,
        ),
    )

    with pytest.raises(semantics.SemanticValidationError, match="eFuse-block revision"):
        semantics.validate_lxveos_idf602_merged(
            merged,
            chip="esp32s3",
            flash_size_bytes=8 * MIB,
            partition_csv=partition_csv,
            flasher_args=flasher_args,
        )


@pytest.mark.parametrize(
    "min_revision,max_revision",
    [(2, 0), (2, 0xFFFF), (0, 1), (0xFFFF, 1)],
)
def test_app_efuse_revision_policy_honors_unset_sentinels(min_revision, max_revision):
    merged, partition_csv, flasher_args = merged_image_bytes(
        chip="esp32s3",
        flash_size_bytes=8 * MIB,
        app_first_segment=app_descriptor(
            min_efuse_block_revision=min_revision,
            max_efuse_block_revision=max_revision,
        ),
    )

    semantics.validate_lxveos_idf602_merged(
        merged,
        chip="esp32s3",
        flash_size_bytes=8 * MIB,
        partition_csv=partition_csv,
        flasher_args=flasher_args,
    )


def test_generic_parser_only_reports_entrypoint_and_revision_values():
    image = esp_image_bytes(
        kind="bootloader",
        chip_id=0,
        flash_size_code=2,
        entry_addr=0xDEADBEEF,
        min_revision=2,
        max_revision=1,
    )

    result = semantics.validate_esp_image(
        image,
        image_offset=0,
        region_end=len(image),
        expected_chip_id=0,
        expected_flash_size_code=2,
        kind="bootloader",
    )

    assert (result.entry_addr, result.min_revision, result.max_revision) == (
        0xDEADBEEF,
        2,
        1,
    )


def test_partition_table_md5_and_csv_must_describe_the_same_ordered_layout():
    merged, partition_csv, flasher_args = merged_image_bytes(chip="esp32", flash_size_bytes=4 * MIB)
    changed = list(PARTITIONS_4MB)
    changed[0] = VectorPartition("settings", 1, 2, 0x9000, 0x6000)
    internally_valid_table = binary_partition_table(4 * MIB, entries=tuple(changed))

    with pytest.raises(semantics.SemanticValidationError, match="does not exactly match"):
        semantics.validate_merged_image(
            _with_table(merged, internally_valid_table),
            chip="esp32",
            flash_size_bytes=4 * MIB,
            partition_csv=partition_csv,
            flasher_args=flasher_args,
        )

    bad_md5 = bytearray(merged)
    md5_offset = PARTITION_TABLE_OFFSET + 32 * len(PARTITIONS_4MB) + 16
    bad_md5[md5_offset] ^= 1
    with pytest.raises(semantics.SemanticValidationError, match="MD5"):
        semantics.validate_merged_image(
            bad_md5,
            chip="esp32",
            flash_size_bytes=4 * MIB,
            partition_csv=partition_csv,
            flasher_args=flasher_args,
        )


def test_full_sixteen_byte_partition_label_does_not_require_a_nul():
    full_label = "abcdefghijklmnop"
    changed = list(PARTITIONS_4MB)
    changed[-1] = VectorPartition(full_label, 1, 0x81, 0x2E0000, 0x110000)
    merged, _partition_csv, _flasher_args = merged_image_bytes(
        chip="esp32", flash_size_bytes=4 * MIB
    )

    parsed = semantics.parse_binary_partition_table(
        _with_table(merged, binary_partition_table(4 * MIB, entries=tuple(changed))),
        flash_size_bytes=4 * MIB,
    )

    assert parsed[-1].name == full_label


def test_rejects_self_consistent_but_unpinned_partition_layout():
    merged, partition_csv, flasher_args = merged_image_bytes(chip="esp32", flash_size_bytes=4 * MIB)
    changed = list(PARTITIONS_4MB)
    changed[0] = VectorPartition("settings", 1, 2, 0x9000, 0x6000)
    changed_csv = partition_csv.replace(b"nvs,data,nvs", b"settings,data,nvs")

    with pytest.raises(semantics.SemanticValidationError, match="pinned LxveOS"):
        semantics.validate_lxveos_idf602_merged(
            _with_table(merged, binary_partition_table(4 * MIB, entries=tuple(changed))),
            chip="esp32",
            flash_size_bytes=4 * MIB,
            partition_csv=changed_csv,
            flasher_args=flasher_args,
        )


def test_partition_csv_rejects_quoted_fields_outside_idf_grammar():
    _merged, partition_csv, _flasher_args = merged_image_bytes(
        chip="esp32", flash_size_bytes=4 * MIB
    )
    quoted = partition_csv.replace(b"nvs,data,nvs", b'"nvs",data,nvs')

    with pytest.raises(semantics.SemanticValidationError, match="unsupported quoting"):
        semantics.parse_partition_csv(quoted, flash_size_bytes=4 * MIB)


def test_partition_csv_rejects_numeric_zero_as_non_idf_flag_grammar():
    _merged, partition_csv, _flasher_args = merged_image_bytes(
        chip="esp32", flash_size_bytes=4 * MIB
    )
    explicit_zero = partition_csv.replace(
        b"nvs,data,nvs,0x9000,0x6000",
        b"nvs,data,nvs,0x9000,0x6000,0",
    )

    with pytest.raises(semantics.SemanticValidationError, match="unsupported flags"):
        semantics.parse_partition_csv(explicit_zero, flash_size_bytes=4 * MIB)


@pytest.mark.parametrize(
    ("partition_csv", "expected_sha256"),
    [
        (
            REVIEW_PARTITION_CSV_WHITESPACE_LINE,
            "44f4b55b81a3ce603e409cf2025decb69cb0259507be2af8e287acde08bbb340",
        ),
        (
            REVIEW_PARTITION_CSV_UPPERCASE_TYPES,
            "1b1e92efd9cdd9fd3c53d2e379aaa7dd61c8641d7e0c1cf9a7052d6ff1cb18c9",
        ),
        (
            REVIEW_PARTITION_CSV_SUFFIX_SPACE,
            "958e6c9dfba82064fda505d909bacc9dcf530a7ed901d779f821a74d595c1efb",
        ),
    ],
    ids=("whitespace-only-line", "uppercase-types", "suffix-space"),
)
def test_accepts_exact_esp_idf_equivalent_csv_review_witnesses(partition_csv, expected_sha256):
    assert hashlib.sha256(partition_csv).hexdigest() == expected_sha256
    expected = tuple(
        (entry.name, entry.type, entry.subtype, entry.offset, entry.size, entry.flags)
        for entry in PARTITIONS_4MB
    )

    parsed = semantics.parse_partition_csv(partition_csv, flash_size_bytes=4 * MIB)
    assert (
        tuple(
            (entry.name, entry.type, entry.subtype, entry.offset, entry.size, entry.flags)
            for entry in parsed
        )
        == expected
    )

    merged, _default_csv, flasher_args = merged_image_bytes(chip="esp32", flash_size_bytes=4 * MIB)
    result = semantics.validate_lxveos_idf602_merged(
        merged,
        chip="esp32",
        flash_size_bytes=4 * MIB,
        partition_csv=partition_csv,
        flasher_args=flasher_args,
    )
    assert result.partitions == parsed


def test_partition_csv_keeps_subtype_keywords_case_sensitive():
    changed = REVIEW_PARTITION_CSV_UPPERCASE_TYPES.replace(b", nvs,", b", NVS,", 1)

    with pytest.raises(semantics.SemanticValidationError, match="subtype is not an integer"):
        semantics.parse_partition_csv(changed, flash_size_bytes=4 * MIB)


def test_partition_csv_size_limit_is_inclusive_and_checked_before_decode():
    _merged, partition_csv, _flasher_args = merged_image_bytes(
        chip="esp32", flash_size_bytes=4 * MIB
    )
    chunks = [partition_csv]
    remaining = 4 * MIB - len(partition_csv)
    while remaining:
        line_size = min(64 * 1024, remaining)
        if line_size == 1:
            chunks.append(b"#")
        else:
            chunks.append(b"#" + b"x" * (line_size - 2) + b"\n")
        remaining -= line_size
    exact_limit = b"".join(chunks)

    parsed = semantics.parse_partition_csv(exact_limit, flash_size_bytes=4 * MIB)
    assert tuple(entry.name for entry in parsed) == ("nvs", "phy_init", "factory", "storage")

    for invalid in (b"", exact_limit + b"\xff"):
        with pytest.raises(semantics.SemanticValidationError, match="size must be"):
            semantics.parse_partition_csv(invalid, flash_size_bytes=4 * MIB)


def test_rejects_malformed_or_duplicate_partition_md5_markers():
    merged, _partition_csv, _flasher_args = merged_image_bytes(
        chip="esp32", flash_size_bytes=4 * MIB
    )
    marker_offset = 32 * len(PARTITIONS_4MB)
    malformed = bytearray(binary_partition_table(4 * MIB))
    malformed[marker_offset + 2] = 0
    duplicate = bytearray(binary_partition_table(4 * MIB))
    duplicate[marker_offset + 32 : marker_offset + 64] = duplicate[
        marker_offset : marker_offset + 32
    ]

    with pytest.raises(semantics.SemanticValidationError, match="marker is malformed"):
        semantics.parse_binary_partition_table(
            _with_table(merged, malformed), flash_size_bytes=4 * MIB
        )
    with pytest.raises(semantics.SemanticValidationError, match="multiple MD5"):
        semantics.parse_binary_partition_table(
            _with_table(merged, duplicate), flash_size_bytes=4 * MIB
        )


@pytest.mark.parametrize(
    "entry,match",
    [
        (VectorPartition("nvs", 1, 2, 0x8000, 0x6000), "reserved"),
        (VectorPartition("nvs", 1, 2, 0x9001, 0x6000), "sector-aligned"),
        (VectorPartition("nvs", 1, 2, 0x9000, 0x6001), "sector-aligned"),
        (VectorPartition("factory", 0, 0, 0x11000, 0x1000), "0x10000-aligned"),
        (VectorPartition("nvs", 1, 2, 0x3FF000, 0x2000), "beyond"),
        (VectorPartition("nvs", 1, 2, 0x9000, 0x6000, 1), "flags"),
    ],
)
def test_rejects_unsafe_partition_geometry(entry, match):
    remaining = tuple(item for item in PARTITIONS_4MB if item.name != entry.name)
    table = binary_partition_table(4 * MIB, entries=(entry, *remaining))
    merged, _partition_csv, _flasher_args = merged_image_bytes(
        chip="esp32", flash_size_bytes=4 * MIB
    )

    with pytest.raises(semantics.SemanticValidationError, match=match):
        semantics.parse_binary_partition_table(_with_table(merged, table), flash_size_bytes=4 * MIB)


def test_rejects_duplicate_and_overlapping_partitions():
    duplicate = (*PARTITIONS_4MB, PARTITIONS_4MB[0])
    overlapping = (
        *PARTITIONS_4MB,
        VectorPartition("overlap", 1, 2, 0xA000, 0x1000),
    )
    merged, _partition_csv, _flasher_args = merged_image_bytes(
        chip="esp32", flash_size_bytes=4 * MIB
    )

    with pytest.raises(semantics.SemanticValidationError, match="duplicate"):
        semantics.parse_binary_partition_table(
            _with_table(merged, binary_partition_table(4 * MIB, entries=duplicate)),
            flash_size_bytes=4 * MIB,
        )
    with pytest.raises(semantics.SemanticValidationError, match="overlap"):
        semantics.parse_binary_partition_table(
            _with_table(merged, binary_partition_table(4 * MIB, entries=overlapping)),
            flash_size_bytes=4 * MIB,
        )


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda value: value["write_flash_args"].__setitem__(0, "--flash_mode"), "write_flash"),
        (lambda value: value["extra_esptool_args"].__setitem__("chip", "esp32s3"), "chip"),
        (lambda value: value["extra_esptool_args"].__setitem__("after", "no-reset"), "reset"),
        (lambda value: value["app"].__setitem__("encrypted", "true"), "plaintext"),
        (lambda value: value["flash_settings"].__setitem__("flash_mode", "qio"), "flash_mode"),
        (lambda value: value["flash_settings"].__setitem__("flash_size", "8MB"), "flash_size"),
        (lambda value: value["flash_settings"].__setitem__("flash_freq", "40m"), "flash_freq"),
        (lambda value: value["bootloader"].__setitem__("offset", "0x0"), "bootloader.offset"),
        (
            lambda value: value["app"].__setitem__("offset", 0x10000),
            "canonical integer string",
        ),
        (
            lambda value: value["app"].__setitem__("offset", " 0x10000 "),
            "canonical integer string",
        ),
        (
            lambda value: value["app"].__setitem__("offset", "0x1_0000"),
            "canonical integer string",
        ),
        (
            lambda value: value["app"].__setitem__("offset", "64 K"),
            "canonical integer",
        ),
        (
            lambda value: value["partition-table"].__setitem__("offset", "0x9000"),
            "partition-table.offset",
        ),
        (lambda value: value["app"].__setitem__("offset", "0x20000"), "app.offset"),
        (lambda value: value["flash_files"].__setitem__("0x10000", "wrong.bin"), "disagrees"),
        (lambda value: value["flash_files"].__setitem__("0x30000", "extra.bin"), "outside"),
        (lambda value: value.__setitem__("unexpected", True), "top-level keys"),
    ],
)
def test_flasher_args_is_a_nonoperative_cross_check(mutation, match):
    raw = flasher_args_bytes(
        chip="esp32",
        flash_size_bytes=4 * MIB,
        bootloader_offset=0x1000,
        app_offset=0x10000,
    )
    changed = _replace_json(raw, mutation)

    with pytest.raises(semantics.SemanticValidationError, match=match):
        semantics.validate_flasher_args(
            changed,
            chip="esp32",
            flash_size_bytes=4 * MIB,
            bootloader_offset=0x1000,
            app_offset=0x10000,
        )


def test_flasher_offsets_keep_compact_suffixes_without_csv_whitespace_relaxation():
    value = json.loads(
        flasher_args_bytes(
            chip="esp32",
            flash_size_bytes=4 * MIB,
            bootloader_offset=0x1000,
            app_offset=0x10000,
        )
    )
    value["bootloader"]["offset"] = "4K"
    value["app"]["offset"] = "64K"
    value["flash_files"]["4K"] = value["flash_files"].pop("0x1000")
    value["flash_files"]["64K"] = value["flash_files"].pop("0x10000")

    semantics.validate_flasher_args(
        (json.dumps(value, sort_keys=True) + "\n").encode(),
        chip="esp32",
        flash_size_bytes=4 * MIB,
        bootloader_offset=0x1000,
        app_offset=0x10000,
    )


@pytest.mark.parametrize("chip,bootloader_offset", [("esp32", 0x1000), ("esp32s3", 0)])
def test_optional_otadata_entry_is_cross_checked_but_never_followed(
    chip, bootloader_offset, monkeypatch
):
    raw = flasher_args_bytes(
        chip=chip,
        flash_size_bytes=8 * MIB,
        bootloader_offset=bootloader_offset,
        app_offset=0x20000,
    )
    value = json.loads(raw)
    value["otadata"] = {
        "offset": "0xf000",
        "file": "../../opaque/ota_data_initial.bin",
        "encrypted": "false",
    }
    value["flash_files"]["0xf000"] = "../../opaque/ota_data_initial.bin"
    changed = (json.dumps(value, sort_keys=True) + "\n").encode()

    def reject_open(*args, **kwargs):
        pytest.fail("metadata paths must remain opaque")

    with monkeypatch.context() as guard:
        guard.setattr("builtins.open", reject_open)
        guard.setattr("io.open", reject_open)
        guard.setattr("os.open", reject_open)
        semantics.validate_flasher_args(
            changed,
            chip=chip,
            flash_size_bytes=8 * MIB,
            bootloader_offset=bootloader_offset,
            app_offset=0x20000,
        )


@pytest.mark.parametrize("chip", ["esp32", "esp32s3"])
def test_otadata_metadata_matches_real_shaped_8mib_layout(chip):
    merged, csv, raw = merged_image_bytes(chip=chip, flash_size_bytes=8 * MIB)
    value = json.loads(raw)
    assert value["otadata"] == {
        "offset": "0xf000", "file": "ota_data_initial.bin", "encrypted": "false"
    }
    assert value["flash_files"]["0xf000"] == "ota_data_initial.bin"
    semantics.validate_flasher_args(
        raw, chip=chip, flash_size_bytes=8 * MIB,
        bootloader_offset=0x1000 if chip == "esp32" else 0, app_offset=0x20000,
    )
    result = semantics.validate_lxveos_idf602_merged(
        merged, chip=chip, flash_size_bytes=8 * MIB, partition_csv=csv, flasher_args=raw,
    )
    assert result.app_partition.name == "ota_0"


@pytest.mark.parametrize("chip", ["esp32", "esp32s3"])
@pytest.mark.parametrize("keep_real_key", [False, True], ids=["invented-alias", "both-keys"])
def test_otadata_rejects_invented_top_level_alias(chip, keep_real_key):
    value = json.loads(flasher_args_bytes(
        chip=chip, flash_size_bytes=8 * MIB,
        bootloader_offset=0x1000 if chip == "esp32" else 0, app_offset=0x20000,
    ))
    value["ota_data_initial"] = value["otadata"]
    if not keep_real_key:
        del value["otadata"]
    with pytest.raises(semantics.SemanticValidationError, match="top-level keys"):
        semantics.validate_flasher_args(
            json.dumps(value).encode(), chip=chip, flash_size_bytes=8 * MIB,
            bootloader_offset=0x1000 if chip == "esp32" else 0, app_offset=0x20000,
        )


@pytest.mark.parametrize("chip", ["esp32", "esp32s3"])
@pytest.mark.parametrize("mutation,match", [
    (lambda value: value["otadata"].__setitem__("offset", "0xe000"), "otadata.offset"),
    (lambda value: value["otadata"].__setitem__("encrypted", "true"), "plaintext"),
    (lambda value: value["otadata"].__setitem__("encrypted", False), "plaintext"),
    (lambda value: value["flash_files"].__setitem__("0xf000", "wrong.bin"), "disagrees"),
    (lambda value: value["flash_files"].pop("0xf000"), "disagrees"),
    (lambda value: value["flash_files"].__setitem__("0x12000", "extra.bin"), "outside"),
])
def test_otadata_entry_reaches_its_offset_plaintext_and_map_checks(chip, mutation, match):
    raw = flasher_args_bytes(
        chip=chip, flash_size_bytes=8 * MIB,
        bootloader_offset=0x1000 if chip == "esp32" else 0, app_offset=0x20000,
    )
    with pytest.raises(semantics.SemanticValidationError, match=match):
        semantics.validate_flasher_args(
            _replace_json(raw, mutation), chip=chip, flash_size_bytes=8 * MIB,
            bootloader_offset=0x1000 if chip == "esp32" else 0, app_offset=0x20000,
        )


@pytest.mark.parametrize("chip", ["esp32", "esp32s3"])
def test_otadata_omission_preserves_existing_8mib_policy(chip):
    merged, csv, raw = merged_image_bytes(chip=chip, flash_size_bytes=8 * MIB)
    value = json.loads(raw)
    del value["otadata"]
    del value["flash_files"]["0xf000"]
    result = semantics.validate_lxveos_idf602_merged(
        merged, chip=chip, flash_size_bytes=8 * MIB, partition_csv=csv,
        flasher_args=json.dumps(value).encode(),
    )
    assert result.app_partition.name == "ota_0"


def test_otadata_entry_stays_invalid_in_4mib_factory_metadata():
    value = json.loads(flasher_args_bytes(
        chip="esp32", flash_size_bytes=4 * MIB, bootloader_offset=0x1000, app_offset=0x10000,
    ))
    value["otadata"] = {
        "offset": "0xf000", "file": "ota_data_initial.bin", "encrypted": "false"
    }
    value["flash_files"]["0xf000"] = "ota_data_initial.bin"
    with pytest.raises(semantics.SemanticValidationError, match="only valid for 8 MiB OTA"):
        semantics.validate_flasher_args(
            json.dumps(value).encode(), chip="esp32", flash_size_bytes=4 * MIB,
            bootloader_offset=0x1000, app_offset=0x10000,
        )


def test_flasher_args_size_limit_is_inclusive_and_checked_before_decode():
    raw = flasher_args_bytes(
        chip="esp32",
        flash_size_bytes=4 * MIB,
        bootloader_offset=0x1000,
        app_offset=0x10000,
    )
    exact_limit = raw + b" " * (4 * MIB - len(raw))
    semantics.validate_flasher_args(
        exact_limit,
        chip="esp32",
        flash_size_bytes=4 * MIB,
        bootloader_offset=0x1000,
        app_offset=0x10000,
    )

    for invalid in (b"", exact_limit + b"\xff"):
        with pytest.raises(semantics.SemanticValidationError, match="size must be"):
            semantics.validate_flasher_args(
                invalid,
                chip="esp32",
                flash_size_bytes=4 * MIB,
                bootloader_offset=0x1000,
                app_offset=0x10000,
            )


def test_rejects_duplicate_or_deep_flasher_json_without_following_paths():
    duplicate = b'{"flash_settings":{},"flash_settings":{}}'
    deep = b"[" * 65 + b"0" + b"]" * 65

    for raw in (duplicate, deep):
        with pytest.raises(semantics.SemanticValidationError):
            semantics.validate_flasher_args(
                raw,
                chip="esp32",
                flash_size_bytes=4 * MIB,
                bootloader_offset=0x1000,
                app_offset=0x10000,
            )


def test_rejects_non_erased_prefix_boot_gap_and_app_tail():
    merged, partition_csv, flasher_args = merged_image_bytes(chip="esp32", flash_size_bytes=4 * MIB)
    positions = (0, 0x1100, len(merged))
    for position in positions:
        changed = bytearray(merged)
        if position == len(changed):
            changed.append(0)
        else:
            changed[position] = 0
        with pytest.raises(semantics.SemanticValidationError, match="outside the admitted"):
            semantics.validate_merged_image(
                changed,
                chip="esp32",
                flash_size_bytes=4 * MIB,
                partition_csv=partition_csv,
                flasher_args=flasher_args,
            )


@pytest.mark.parametrize("position", [0x8C00, 0xF000, 0x2C0000])
def test_every_physically_present_unpopulated_interval_must_be_erased(position):
    merged, partition_csv, flasher_args = merged_image_bytes(
        chip="esp32s3", flash_size_bytes=8 * MIB
    )
    changed = bytearray(merged)
    if position >= len(changed):
        changed.extend(b"\xff" * (position + 1 - len(changed)))
    changed[position] = 0

    with pytest.raises(semantics.SemanticValidationError, match="outside the admitted"):
        semantics.validate_lxveos_idf602_merged(
            changed,
            chip="esp32s3",
            flash_size_bytes=8 * MIB,
            partition_csv=partition_csv,
            flasher_args=flasher_args,
        )


def test_short_merge_ending_exactly_at_selected_app_end_is_valid():
    merged, partition_csv, flasher_args = merged_image_bytes(
        chip="esp32s3", flash_size_bytes=8 * MIB
    )

    result = semantics.validate_lxveos_idf602_merged(
        merged,
        chip="esp32s3",
        flash_size_bytes=8 * MIB,
        partition_csv=partition_csv,
        flasher_args=flasher_args,
    )

    assert result.app.end == len(merged)
    assert result.app.end < result.app_partition.offset + result.app_partition.size


def test_app_must_fit_inside_its_declared_partition():
    merged, partition_csv, flasher_args = merged_image_bytes(
        chip="esp32",
        flash_size_bytes=4 * MIB,
        app_extra_segments=((0x3FFB0000, b"A" * 0x2D0000),),
    )

    with pytest.raises(semantics.SemanticValidationError, match="truncated|crosses"):
        semantics.validate_merged_image(
            merged,
            chip="esp32",
            flash_size_bytes=4 * MIB,
            partition_csv=partition_csv,
            flasher_args=flasher_args,
        )


def test_binary_partition_table_md5_matches_independent_hash():
    table = binary_partition_table(4 * MIB)
    marker = 32 * len(PARTITIONS_4MB)

    assert table[marker : marker + 16] == b"\xeb\xeb" + b"\xff" * 14
    assert (
        table[marker + 16 : marker + 32]
        == hashlib.md5(table[:marker], usedforsecurity=False).digest()
    )
