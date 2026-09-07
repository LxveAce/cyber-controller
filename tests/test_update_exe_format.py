"""Unit tests for executable-format validation (src/core/update_exe_format.py). Pure — synthetic, structurally
coherent headers/tables (no zero-filled 'valid' fixtures), plus the structural/CPU boundary witnesses."""
from __future__ import annotations

import struct

import pytest

from src.core import update_exe_format as ef

_ARM64 = 0x0100000C
_X86_64 = 0x01000007
_ARM = 12
_ARM64_32 = 0x0200000C


# ── Structurally coherent fixture builders ────────────────────────────────────────────────────────────

def _elf(machine=ef._EM_X86_64, kind=ef._ET_DYN):
    b = bytearray(512)
    b[:16] = b"\x7fELF" + bytes([2, 1, 1, 0, 0]) + b"\x00" * 7
    struct.pack_into("<HHIQQQIHHHHHH", b, 16, kind, machine, 1, 0x400100,
                     64, 0, 0, 64, 56, 1, 64, 0, 0)
    struct.pack_into("<IIQQQQQQ", b, 64, 1, 5, 0, 0x400000, 0x400000, 512, 512, 4096)
    return b


def _pe(machine=ef._PE_AMD64):
    b = bytearray(1024)
    b[:2] = b"MZ"
    struct.pack_into("<I", b, 0x3C, 0x80)
    b[0x80:0x84] = b"PE\x00\x00"
    is64 = machine != ef._PE_I386
    optsize = 240 if is64 else 224
    struct.pack_into("<HHIIIHH", b, 0x84, machine, 1, 0, 0, 0, optsize, 0x22)
    o = 0x98
    struct.pack_into("<H", b, o, 0x20B if is64 else 0x10B)
    struct.pack_into("<III", b, o + 16, 0x1000, 0x1000, 0 if is64 else 0x2000)
    if is64:
        struct.pack_into("<Q", b, o + 24, 0x140000000)
    else:
        struct.pack_into("<I", b, o + 28, 0x400000)
    struct.pack_into("<II", b, o + 32, 4096, 512)
    struct.pack_into("<HH", b, o + 40, 6, 0)
    struct.pack_into("<HH", b, o + 48, 6, 0)
    struct.pack_into("<II", b, o + 56, 8192, 512)
    struct.pack_into("<H", b, o + 68, 3)
    struct.pack_into("<I", b, o + (108 if is64 else 92), 16)
    s = o + optsize
    b[s:s + 8] = b".text\x00\x00\x00"
    struct.pack_into("<IIIIIIHHI", b, s + 8, 1, 4096, 512, 512, 0, 0, 0, 0, 0x60000020)
    b[512] = 0xC3
    return b


def _macho(cpu=_ARM64, endian="<"):
    b = bytearray(256)
    struct.pack_into(endian + "IiiIIIII", b, 0, 0xFEEDFACF, cpu, 0, 2, 2, 96, 0x200085, 0)
    struct.pack_into(endian + "II16sQQQQiiII", b, 32, 0x19, 72, b"__TEXT",
                     0x100000000, 4096, 0, 256, 7, 5, 0, 0)
    struct.pack_into(endian + "IIQQ", b, 104, 0x80000028, 24, 128, 0)
    return b


def _fat(cpus=(_X86_64, _ARM64), endian=">"):
    b = bytearray(512 * (len(cpus) + 1))
    struct.pack_into(endian + "II", b, 0, 0xCAFEBABE, len(cpus))
    for index, cpu in enumerate(cpus):
        offset = 512 * (index + 1)
        struct.pack_into(endian + "iiIII", b, 8 + index * 20, cpu, 0, offset, 256, 9)
        b[offset:offset + 256] = _macho(cpu)
    return b


# ── ELF ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("machine,key,kind", [
    (ef._EM_X86_64, "linux-x64", ef._ET_EXEC),
    (ef._EM_X86_64, "linux-x64", ef._ET_DYN),          # a PIE is valid
    (ef._EM_AARCH64, "linux-arm64", ef._ET_DYN),
])
def test_complete_elf_controls(machine, key, kind):
    ef.validate_onefile_executable(_elf(machine, kind), key)


def test_elf_wrong_architecture_rejected():
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(_elf(ef._EM_AARCH64), "linux-x64")
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(_elf(ef._EM_X86_64), "linux-arm64")


def test_elf_32bit_rejected():
    b = _elf()
    b[4] = 1                                            # EI_CLASS ELFCLASS32
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(b, "linux-x64")


def test_elf_non_executable_type_rejected():
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(_elf(kind=1), "linux-x64")   # ET_REL is not runnable


@pytest.mark.parametrize("field,value", [
    ("ident_version", 0), ("version", 0), ("ehsize", 0), ("phentsize", 0),
    ("phoff", 0xFFFFFFFFFFFFFFFF),
])
def test_elf_contradictory_structural_header_is_rejected(field, value):
    # L2B-2: a complete ELF64 header + program-header table is required; a single contradictory mandatory field
    # (or a table offset that cannot name a finite in-prefix extent) must not return success.
    b = _elf()
    fmt, offset = {"ident_version": ("B", 6), "version": ("I", 20), "ehsize": ("H", 52),
                   "phentsize": ("H", 54), "phoff": ("Q", 32)}[field]
    struct.pack_into("<" + fmt, b, offset, value)
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(b, "linux-x64")


# ── PE ─────────────────────────────────────────────────────────────────────────────────────────────

def test_complete_pe_portable_and_installer_distinction():
    ef.validate_onefile_executable(_pe(), "windows-x64")
    assert ef.validate_windows_installer(_pe(ef._PE_I386)) == ef._PE_I386   # 32-bit bootstrap accepted
    with pytest.raises(ef.ExecutableFormatError):                           # ...but not as an x64 portable
        ef.validate_onefile_executable(_pe(ef._PE_I386), "windows-x64")


@pytest.mark.parametrize("variant", [
    "coff_truncated", "optional_truncated", "optional_absent", "optional_wrong_magic",
    "dll", "overlap_dos", "section_table_truncated",
])
def test_pe_malformed_structures_are_rejected(variant):
    # L2B-3: MZ + a PE signature + a two-byte machine field is not enough; a complete COFF header, coherent
    # PE32/PE32+ optional header and a contained section table are required.
    b = _pe()
    if variant == "coff_truncated":
        b = b[:0x86]
    elif variant == "optional_truncated":
        b = b[:0x9A]
    elif variant == "optional_absent":
        struct.pack_into("<H", b, 0x94, 0)
    elif variant == "optional_wrong_magic":
        struct.pack_into("<H", b, 0x98, 0x1234)
    elif variant == "dll":
        struct.pack_into("<H", b, 0x96, 0x2022)
    elif variant == "overlap_dos":
        struct.pack_into("<I", b, 0x3C, 16)
        b[16:22] = b"PE\x00\x00\x64\x86"
    elif variant == "section_table_truncated":
        b = b[:0x98 + 240 + 8]
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(b, "windows-x64")


def test_installer_rejects_truncated_coff_while_retaining_i386_bootstrap():
    assert ef.validate_windows_installer(_pe(ef._PE_I386)) == ef._PE_I386   # complete i386 installer accepted
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_windows_installer(_pe(ef._PE_I386)[:0x86])              # ...truncated COFF rejected


def test_pe_bad_signature_rejected():
    b = _pe()
    b[0x80:0x84] = b"XXXX"
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(b, "windows-x64")


# ── Mach-O ───────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("endian", ["<", ">"])
def test_complete_thin_arm64_controls(endian):
    ef.validate_onefile_executable(_macho(endian=endian), "macos-arm64")


def test_complete_fat_arm64_control():
    ef.validate_onefile_executable(_fat(), "macos-arm64")


@pytest.mark.parametrize("container", ["thin", "fat"])
@pytest.mark.parametrize("cpu", [_ARM, _ARM64_32, 0x0300000C])
def test_non_arm64_abi_is_not_promoted_by_low_cpu_bits(container, cpu):
    # L2B-1: the exact arm64 ABI value is required; a low-24-bit ARM family match must not promote arm (12),
    # arm64_32 (0x0200000C) or invalid variants to arm64, in either container.
    b = _macho(cpu) if container == "thin" else _fat((cpu,))
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(b, "macos-arm64")


@pytest.mark.parametrize("variant", ["not_executable", "commands_truncated", "command_size_zero"])
def test_thin_macho_structural_rejections(variant):
    # L2B-4 (thin): MH_EXECUTE and bounded, well-sized load commands are required.
    b = _macho()
    if variant == "not_executable":
        struct.pack_into("<I", b, 12, 6)          # MH_DYLIB
    elif variant == "commands_truncated":
        b = b[:64]
    else:
        struct.pack_into("<I", b, 36, 0)          # first command cmdsize = 0
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(b, "macos-arm64")


@pytest.mark.parametrize("variant", [
    "missing_later_descriptor", "zero_slice_size", "offset_past_bytes", "slice_wrong_cpu", "slice_not_macho",
])
def test_fat_arm64_descriptor_is_not_sufficient_slice_proof(variant):
    # L2B-4 (fat): a descriptor's arm64 label is not proof; the full descriptor table must be bounded and the
    # chosen slice header validated at its declared offset.
    b = _fat((_ARM64, _X86_64))                    # arm64 descriptor is index 0, slice at offset 512
    if variant == "missing_later_descriptor":
        struct.pack_into(">I", b, 4, 4)            # claim 4 descriptors...
        b = b[:64]                                 # ...but only provide 64 bytes
    elif variant == "zero_slice_size":
        struct.pack_into(">I", b, 20, 0)           # arm64 descriptor size = 0
    elif variant == "offset_past_bytes":
        struct.pack_into(">I", b, 16, 0xFFFFFF00)  # arm64 descriptor offset beyond the bytes
    elif variant == "slice_wrong_cpu":
        b[512:768] = _macho(_X86_64)               # slice at the arm64 offset is actually x86_64
    else:
        b[512:768] = b"\x00" * 256                 # slice at the arm64 offset is not a Mach-O
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(b, "macos-arm64")


def test_byte_swapped_fat_header_is_rejected_as_non_canonical():
    # Endianness policy: canonical macOS universal binaries store the fat header BIG-ENDIAN (FAT_MAGIC). A
    # byte-swapped on-disk fat header (FAT_CIGAM when read big-endian) is not produced by Apple's tooling and
    # is rejected rather than reinterpreted. (The prior candidate silently mis-read its count instead.)
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(_fat(endian="<"), "macos-arm64")


# ── Cross-format / minimum / unsupported ──────────────────────────────────────────────────────────────

def test_wrong_format_for_key_rejected():
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(_pe(), "linux-x64")      # a PE is not an ELF
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(_elf(), "windows-x64")   # an ELF is not a PE


@pytest.mark.parametrize("key", ["linux-x64", "windows-x64", "macos-arm64"])
def test_public_header_minimum_and_all_zero_rejected(key):
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(b"\x00" * 63, key)               # below MIN_HEADER_BYTES
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(b"\x00" * ef.RECOMMENDED_HEADER_BYTES, key)   # all-zero is no format


def test_unsupported_key_rejected():
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(_elf(), "linux-riscv64")


def test_installer_rejects_non_pe():
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_windows_installer(_elf())
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_windows_installer(b"\x00" * ef.MIN_HEADER_BYTES)


# ── 308b5a8 residual boundary groups (EXE-308-1..5) ────────────────────────────────────────────────────

def _pe_dirs(num_dirs):
    # A coherent PE32+ whose optional header is sized exactly for `num_dirs` data directories.
    b = bytearray(2048)
    b[:2] = b"MZ"
    struct.pack_into("<I", b, 0x3C, 0x80)
    b[0x80:0x84] = b"PE\x00\x00"
    optsize = 112 + num_dirs * 8
    struct.pack_into("<HHIIIHH", b, 0x84, ef._PE_AMD64, 1, 0, 0, 0, optsize, 0x22)
    o = 0x98
    struct.pack_into("<H", b, o, 0x20B)
    struct.pack_into("<III", b, o + 16, 0x1000, 0x1000, 0)
    struct.pack_into("<Q", b, o + 24, 0x140000000)
    struct.pack_into("<II", b, o + 32, 4096, 512)
    struct.pack_into("<HH", b, o + 40, 6, 0)
    struct.pack_into("<HH", b, o + 48, 6, 0)
    struct.pack_into("<II", b, o + 56, 8192, 512)
    struct.pack_into("<H", b, o + 68, 3)
    struct.pack_into("<I", b, o + 108, num_dirs)
    s = o + optsize
    b[s:s + 8] = b".text\x00\x00\x00"
    struct.pack_into("<IIIIIIHHI", b, s + 8, 1, 4096, 512, 512, 0, 0, 0, 0, 0x60000020)
    return b


def _macho_one_cmd(cmdsize, cpu=_ARM64, endian="<"):
    # A thin arm64 MH_EXECUTE with a single load command of the given size.
    b = bytearray(max(256, 32 + cmdsize + 8))
    struct.pack_into(endian + "IiiIIIII", b, 0, 0xFEEDFACF, cpu, 0, 2, 1, cmdsize, 0x200085, 0)
    struct.pack_into(endian + "II", b, 32, 0x1B, cmdsize)
    return b


def test_pe_one_byte_optional_header_raises_format_error_not_struct_error():
    # EXE-308-1: a 1-byte optional header must raise ExecutableFormatError (not leak struct.error from the
    # 2-byte magic read past the prefix) through BOTH public entrypoints.
    b = _pe()
    struct.pack_into("<H", b, 0x94, 1)          # SizeOfOptionalHeader = 1
    b = bytes(b[:0x99])                          # bytes end one past opt_start (0x98)
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(b, "windows-x64")
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_windows_installer(b)


@pytest.mark.parametrize("num_dirs", [0, 1, 16, 17])
def test_pe_variable_directory_count_controls_pass(num_dirs):
    ef.validate_onefile_executable(_pe_dirs(num_dirs), "windows-x64")


@pytest.mark.parametrize("bad", [17, 0xFFFFFFFF])
def test_pe_directory_count_exceeding_optional_header_rejected(bad):
    # EXE-308-2: a data-directory count that overflows the declared optional-header extent is rejected.
    b = _pe()                                    # PE32+ optsize 240 holds exactly 16 directories
    struct.pack_into("<I", b, 0x98 + 108, bad)
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(b, "windows-x64")


@pytest.mark.parametrize("member_size", [1, 31, 32, 40, 127])
def test_fat_slice_cannot_borrow_beyond_declared_member_size(member_size):
    # EXE-308-3: the selected member's header/commands must fit its OWN declared size, not later buffer bytes.
    b = _fat((_ARM64, _X86_64))                  # arm64 member at offset 512; later bytes present
    struct.pack_into(">I", b, 20, member_size)   # arm64 descriptor size
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(b, "macos-arm64")


@pytest.mark.parametrize("declared", [128, 256, 1024 * 1024])
def test_fat_member_prefix_control_passes_with_128_consumed(declared):
    # Positive control: a member declared 128/256/1MiB passes when the prefix ends exactly after the 128
    # consumed header/command bytes, and fails when one consumed byte is missing.
    b = _fat((_ARM64, _X86_64))
    struct.pack_into(">I", b, 20, declared)      # arm64 declared member size
    ef.validate_onefile_executable(bytes(b[:512 + 128]), "macos-arm64")
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(bytes(b[:512 + 127]), "macos-arm64")


@pytest.mark.parametrize("align,offset", [(10, 512), (32, 512), (0xFFFFFFFF, 512), (9, 513)])
def test_fat_slice_alignment_violations_rejected(align, offset):
    # EXE-308-4: the selected member's declared power-of-two alignment must be finite and satisfied by its offset.
    b = _fat((_ARM64, _X86_64))
    struct.pack_into(">I", b, 16, offset)        # arm64 descriptor offset
    struct.pack_into(">I", b, 24, align)         # arm64 descriptor align exponent
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(b, "macos-arm64")


@pytest.mark.parametrize("align", [0, 3, 9])
def test_fat_slice_alignment_controls_pass(align):
    b = _fat((_ARM64, _X86_64))                  # offset 512
    struct.pack_into(">I", b, 24, align)
    ef.validate_onefile_executable(b, "macos-arm64")


@pytest.mark.parametrize("container", ["thin", "fat"])
@pytest.mark.parametrize("cmdsize", [9, 12, 15])
def test_macho_command_size_must_be_8_aligned(container, cmdsize):
    # EXE-308-5: a 64-bit Mach-O load-command size must be a multiple of 8, in thin and selected-fat forms.
    if container == "thin":
        b = _macho_one_cmd(cmdsize)
    else:
        b = _fat((_ARM64,))
        slice_bytes = _macho_one_cmd(cmdsize)
        b[512:512 + len(slice_bytes)] = slice_bytes
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(bytes(b), "macos-arm64")


def test_macho_8_aligned_single_command_passes():
    ef.validate_onefile_executable(_macho_one_cmd(16), "macos-arm64")


@pytest.mark.parametrize("endian", ["<", ">"])
@pytest.mark.parametrize("container", ["thin", "fat"])
def test_macho_command_header_must_fit_declared_region(endian, container):
    # EXE-56-1: an earlier command that consumes the whole region must not permit reading a later command's
    # 8-byte header past it (which read raw struct.error before). ncmds=2, sizeofcmds=32, first command 32.
    b = bytearray(64)
    struct.pack_into(endian + "IiiIIIII", b, 0, 0xFEEDFACF, _ARM64, 0, 2, 2, 32, 0x200085, 0)
    struct.pack_into(endian + "II", b, 32, 0x19, 32)   # one command consuming the entire 32-byte region
    if container == "thin":
        blob = bytes(b)
    else:
        f = _fat((_ARM64,))                            # arm64 member at offset 512
        f[512:512 + 64] = b
        struct.pack_into(">I", f, 20, 64)              # member size 64: the bounded view exposes only 64 bytes
        blob = bytes(f)
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(blob, "macos-arm64")


def test_macho_two_command_region_control_passes():
    # Adjacent control: two 8-byte-aligned commands that exactly fill the region remain valid.
    b = bytearray(64)
    struct.pack_into("<IiiIIIII", b, 0, 0xFEEDFACF, _ARM64, 0, 2, 2, 32, 0x200085, 0)
    struct.pack_into("<II", b, 32, 0x1B, 16)
    struct.pack_into("<II", b, 48, 0x1B, 16)
    ef.validate_onefile_executable(bytes(b), "macos-arm64")


def test_real_amd64_pe_validates():
    # A genuine 64-bit Windows PE parses and validates as a windows-x64 onefile (skipped off Windows / if absent).
    import os
    path = "C:/Windows/System32/notepad.exe"
    if not os.path.exists(path):
        pytest.skip("no local AMD64 PE available")
    with open(path, "rb") as fh:
        header = fh.read(ef.RECOMMENDED_HEADER_BYTES)
    assert ef.validate_windows_installer(header) == ef._PE_AMD64
    ef.validate_onefile_executable(header, "windows-x64")


# ── Bounded prefix planning ──────────────────────────────────────────────────────────────────────

from tests import exe_images as img  # noqa: E402 — appended section


def _reader(data, short_at=None):
    calls = []

    def read(offset, length):
        calls.append((offset, length))
        chunk = data[offset:offset + length]
        if short_at is not None and offset == short_at:
            chunk = chunk[:-1]
        return chunk

    read.calls = calls
    return read


BEYOND_4096 = [
    ("linux-x64", img.elf64(phoff=8192), 8248),
    ("windows-x64", img.pe32plus(lfanew=0x2000), 8496),
    ("macos-arm64", img.macho_thin_arm64(sizeofcmds=4104), 4136),
    ("macos-arm64", img.macho_fat(first_offset=16384, stride=16384), 32896),
]
BEYOND_IDS = ["elf-phdrs-at-8192", "pe-lfanew-8192", "thin-cmds-4104", "fat-slice-at-32768"]


@pytest.mark.parametrize("key,image,expected", [
    ("linux-x64", img.elf64(img.EM_X86_64), 120),
    ("linux-arm64", img.elf64(img.EM_AARCH64), 120),
    ("windows-x64", img.pe32plus(), 432),
    ("macos-arm64", img.macho_thin_arm64(), 128),
    ("macos-arm64", img.macho_thin_arm64(endian=">"), 128),
    ("macos-arm64", img.macho_fat(), 1152),
    *BEYOND_4096,
], ids=["elf-x64", "elf-arm64", "pe-amd64", "thin-le", "thin-be", "fat", *BEYOND_IDS])
def test_planned_prefix_is_exactly_what_the_validator_needs(key, image, expected):
    read = _reader(image)
    need = ef.required_prefix_length(read, key, len(image))
    assert need == expected
    ef.validate_onefile_executable(image[:need], key)          # the planned prefix validates
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(image[:need - 1], key)  # one byte fewer is incomplete
    assert all(o >= 0 and n > 0 and o + n <= len(image) for o, n in read.calls), "reads bounded"


@pytest.mark.parametrize("key,image,_", BEYOND_4096, ids=BEYOND_IDS)
def test_a_fixed_4096_byte_prefix_refuses_these_valid_files(key, image, _):
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(image[:4096], key)


def test_extent_beyond_the_file_length_is_refused_not_clamped():
    image = img.elf64(phoff=8192)
    with pytest.raises(ef.ExecutableFormatError, match="beyond the file length"):
        ef.required_prefix_length(_reader(image), "linux-x64", 8000)


def test_extent_beyond_the_inspected_limit_is_refused_not_clamped():
    b = bytearray(img.elf64())
    struct.pack_into("<Q", b, 32, ef.MAX_INSPECTED_PREFIX_BYTES)   # e_phoff at the limit
    with pytest.raises(ef.ExecutableFormatError, match="inspected-prefix limit"):
        ef.required_prefix_length(_reader(bytes(b)), "linux-x64",
                                  ef.MAX_INSPECTED_PREFIX_BYTES + 4096)


@pytest.mark.parametrize("key,image,short_at", [
    ("windows-x64", img.pe32plus(), 0x80),        # the PE header read
    ("macos-arm64", img.macho_fat(), 8),          # the descriptor table
    ("macos-arm64", img.macho_fat(), 1024),       # the arm64 slice header
    ("linux-x64", img.elf64(), 0),                # the ELF header itself
], ids=["pe-header", "fat-table", "fat-slice-header", "elf-header"])
def test_short_intermediate_read_is_a_finite_refusal(key, image, short_at):
    with pytest.raises(ef.ExecutableFormatError, match="short read"):
        ef.required_prefix_length(_reader(image, short_at=short_at), key, len(image))


def test_fat_count_over_64_is_refused_before_its_table_is_read():
    b = bytearray(4096)
    struct.pack_into(">II", b, 0, 0xCAFEBABE, 65)
    read = _reader(bytes(b))
    assert ef.required_prefix_length(read, "macos-arm64", len(b)) == ef.MIN_HEADER_BYTES
    assert read.calls == [(0, 8)], "nothing beyond the 8-byte header was read"
    with pytest.raises(ef.ExecutableFormatError, match="implausible"):
        ef.validate_onefile_executable(bytes(b[:64]), "macos-arm64")


def test_declared_fat_slice_beyond_the_file_is_refused():
    b = bytearray(img.macho_fat())
    struct.pack_into(">I", b, 8 + 20 + 12, 1 << 20)                # arm64 entry size: 1 MiB
    with pytest.raises(ef.ExecutableFormatError, match="declared member"):
        ef.required_prefix_length(_reader(bytes(b)), "macos-arm64", len(b))


def test_fat_without_arm64_slice_plans_the_table_and_the_validator_refuses():
    image = img.macho_fat(cpus=(img.CPU_X86_64,))
    need = ef.required_prefix_length(_reader(image), "macos-arm64", len(image))
    assert need == ef.MIN_HEADER_BYTES
    with pytest.raises(ef.ExecutableFormatError, match="no arm64 slice"):
        ef.validate_onefile_executable(image[:need], "macos-arm64")


@pytest.mark.parametrize("key", ["linux-x64", "windows-x64", "macos-arm64"])
def test_wrong_format_plans_the_floor_and_the_validator_names_the_reason(key):
    image = b"\x00" * 512
    assert ef.required_prefix_length(_reader(image), key, len(image)) == ef.MIN_HEADER_BYTES
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(image[:64], key)


@pytest.mark.parametrize("length", [0, 63])
def test_files_below_the_public_floor_are_refused_before_any_read(length):
    read = _reader(b"\x7fELF" + b"\x00" * 60)
    with pytest.raises(ef.ExecutableFormatError, match="too short"):
        ef.required_prefix_length(read, "linux-x64", length)
    assert read.calls == []


def test_unsupported_key_and_bad_length_are_refused():
    with pytest.raises(ef.ExecutableFormatError):
        ef.required_prefix_length(_reader(img.elf64()), "windows-arm64", 512)
    with pytest.raises(ef.ExecutableFormatError):
        ef.required_prefix_length(_reader(img.elf64()), "linux-x64", -1)
    with pytest.raises(ef.ExecutableFormatError):
        ef.required_prefix_length(_reader(img.elf64()), "linux-x64", True)


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("key", ["linux-x64", "windows-x64", "macos-arm64"])
def test_arbitrary_bytes_never_raise_struct_error(seed, key):
    import random
    rnd = random.Random(seed)
    image = bytes(rnd.getrandbits(8) for _ in range(rnd.randint(64, 300)))
    try:
        need = ef.required_prefix_length(_reader(image), key, len(image))
        assert ef.MIN_HEADER_BYTES <= need <= len(image)
        ef.validate_onefile_executable(image[:need], key)
    except ef.ExecutableFormatError:
        pass


# ---- a short fat member never plans below the public floor ---------------------------------

def _fat_at_28(size, slice_bytes=b""):
    """The smallest fat layout the planner can meet: a 64-byte canonical fat header with one arm64
    descriptor whose member starts right after the table (offset 28, align 0) and declares *size*
    bytes."""
    data = bytearray(64)
    struct.pack_into(">II", data, 0, 0xCAFEBABE, 1)
    struct.pack_into(">iiIII", data, 8, 0x0100000C, 0, 28, size, 0)
    data[28:28 + len(slice_bytes)] = slice_bytes
    return bytes(data)


_TINY_SLICE = struct.pack("<IiiIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0) + bytes(4)


@pytest.mark.parametrize("image,reads", [
    (_fat_at_28(1), [(0, 8), (8, 20)]),                            # 28 + 1 = 29 without the floor
    (_fat_at_28(32, bytes([0xAA]) * 32), [(0, 8), (8, 20), (28, 32)]),   # 28 + 32 = 60
    (_fat_at_28(32, _TINY_SLICE), [(0, 8), (8, 20), (28, 32)]),    # 28 + min(32, 32 + 0) = 60
], ids=["member-size-1", "non-macho-slice-at-28", "macho-slice-at-28-cmds-0"])
def test_short_fat_member_never_plans_below_the_public_floor(image, reads):
    seen = []

    def read(offset, length):
        seen.append((offset, length))
        return image[offset:offset + length]

    need = ef.required_prefix_length(read, "macos-arm64", len(image))
    assert need == ef.MIN_HEADER_BYTES, "the promised floor, never 29 or 60"
    assert seen == reads, "only the header, the table and (when 32 bytes exist) the slice header"
    with pytest.raises(ef.ExecutableFormatError):
        ef.validate_onefile_executable(image[:need], "macos-arm64")
