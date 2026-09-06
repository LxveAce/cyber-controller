"""Executable-format validation for a staged update binary (Layer 2b) — pure, no I/O, no dispatch.

Given the leading bytes of a staged file, validate that its executable FORMAT and CPU ARCHITECTURE match the
expected platform key, before any replacement. This is a bounded structural check of the header only; it does
NOT download, stage, replace, launch, or read a whole file, and it deliberately does not claim RUNTIME
compatibility.

Two contract points the reviewed plan (§4) is explicit about:

* **A valid position-independent ELF executable is ``ET_DYN`` (a PIE), not ``ET_EXEC``.** Rejecting ``ET_DYN``
  would reject a correct modern build, so both are accepted.
* **A Windows installer's bootstrap PE machine is NOT the packaged application's architecture.** Inno Setup
  permits a 32-bit installer to install a 64-bit application (this project's script sets
  ``ArchitecturesInstallIn64BitMode=x64compatible``), so the installer shape is validated only as a well-formed
  PE — its packaged-app architecture is a separate installer-contract concern (a later layer), never inferred
  from the bootstrap here.

Architecture is also NOT runtime compatibility: the Linux GLIBC floors (x64 2.35 vs ARM64 2.39) and the macOS
deployment target are separate qualifications and are never inferred from a CPU match. An unsupported CPU/host
is an explicit failure, not a coerced near-match.

Bounded-input contract: validation succeeds only when the provided bytes CONTAIN the structures it checks
(fixed headers, declared tables, and a fat slice header at its declared offset). If a declared structure lies
beyond the provided prefix, the result is an explicit failure ("incomplete"), never a success — a leading
prefix length is never equated with the whole staged file length.
"""

from __future__ import annotations

import struct

# Enough leading bytes to reach a PE header via e_lfanew (which is small in practice) and to read ELF/Mach-O
# identification and tables. Callers read at least this many bytes of the staged file and pass them in.
MIN_HEADER_BYTES = 64
RECOMMENDED_HEADER_BYTES = 4096

# The onefile keys this validator understands (the installer is validated separately).
_ONEFILE_KEYS = frozenset({"windows-x64", "linux-x64", "linux-arm64", "macos-arm64"})

# ELF constants.
_EM_X86_64 = 62
_EM_AARCH64 = 183
_ET_EXEC = 2
_ET_DYN = 3          # a PIE executable — valid, not to be rejected
_ELF_EHSIZE = 64     # ELF64 fixed header size
_ELF_PHENTSIZE = 56  # ELF64 program-header entry size

# PE machine values (IMAGE_FILE_MACHINE_*).
_PE_AMD64 = 0x8664
_PE_I386 = 0x014C
_PE_ARM64 = 0xAA64
# PE COFF characteristics + optional-header magics.
_IMAGE_FILE_EXECUTABLE_IMAGE = 0x0002
_IMAGE_FILE_DLL = 0x2000
_PE_OPT_MAGIC_PE32 = 0x010B
_PE_OPT_MAGIC_PE32_PLUS = 0x020B
_PE_OPT_MIN_PE32 = 96          # fixed optional header through NumberOfRvaAndSizes (PE32)
_PE_OPT_MIN_PE32_PLUS = 112    # fixed optional header through NumberOfRvaAndSizes (PE32+)
_PE_NUMRVA_OFF_PE32 = 92       # NumberOfRvaAndSizes offset within a PE32 optional header
_PE_NUMRVA_OFF_PE32_PLUS = 108 # NumberOfRvaAndSizes offset within a PE32+ optional header

# Mach-O magics. Thin magic may be stored in either byte order; the fat/universal header on disk is CANONICALLY
# big-endian (FAT_MAGIC). FAT_CIGAM is the byte-swapped value Apple's tooling does not write to disk.
_MACHO_MAGIC_64 = 0xFEEDFACF
_MACHO_CIGAM_64 = 0xCFFAEDFE
_FAT_MAGIC = 0xCAFEBABE
_FAT_CIGAM = 0xBEBAFECA
_MH_EXECUTE = 2
# Mach-O cputype for arm64 (CPU_TYPE_ARM | CPU_ARCH_ABI64). The EXACT ABI value is required — a low-24-bit ARM
# family match (12) would wrongly promote arm (12), arm64_32 (0x0200000C) and invalid variants to arm64.
_CPU_ARM64 = 0x0100000C
_FAT_ARCH_SIZE = 20   # cputype, cpusubtype, offset, size, align
_FAT_MAX_ARCHES = 64


class ExecutableFormatError(Exception):
    """The staged file's format/architecture does not match the expected platform key. Caller stays put."""


# ── ELF ────────────────────────────────────────────────────────────────────────────────────────────

def _validate_elf(header: bytes, want_machine: int) -> None:
    if len(header) < _ELF_EHSIZE:
        raise ExecutableFormatError("ELF64 header is incomplete")
    if header[:4] != b"\x7fELF":
        raise ExecutableFormatError("not an ELF file")
    if header[4] != 2:                       # EI_CLASS ELFCLASS64
        raise ExecutableFormatError("ELF is not 64-bit")
    ei_data = header[5]
    if ei_data == 1:
        endian = "<"
    elif ei_data == 2:
        endian = ">"
    else:
        raise ExecutableFormatError("ELF has an invalid data-encoding byte (EI_DATA)")
    if header[6] != 1:                       # EI_VERSION must be EV_CURRENT
        raise ExecutableFormatError("ELF identification version is not EV_CURRENT")
    e_type, e_machine = struct.unpack_from(endian + "HH", header, 16)
    e_version = struct.unpack_from(endian + "I", header, 20)[0]
    e_phoff = struct.unpack_from(endian + "Q", header, 32)[0]
    e_ehsize, e_phentsize, e_phnum = struct.unpack_from(endian + "HHH", header, 52)
    if e_version != 1:                       # e_version EV_CURRENT
        raise ExecutableFormatError("ELF version is not EV_CURRENT")
    if e_ehsize != _ELF_EHSIZE:
        raise ExecutableFormatError(f"ELF header size is not 64 (e_ehsize={e_ehsize})")
    if e_type not in (_ET_EXEC, _ET_DYN):    # ET_DYN is a PIE executable — accepted
        raise ExecutableFormatError(f"ELF is not an executable (e_type={e_type})")
    if e_machine != want_machine:
        raise ExecutableFormatError(f"ELF architecture mismatch (e_machine={e_machine}, want {want_machine})")
    if e_phnum:
        if e_phentsize != _ELF_PHENTSIZE:
            raise ExecutableFormatError(f"ELF program-header entry size is not 56 (e_phentsize={e_phentsize})")
        end = e_phoff + e_phnum * e_phentsize     # Python big ints: no wraparound
        if e_phoff < _ELF_EHSIZE or end > len(header):
            raise ExecutableFormatError("ELF program-header table is outside the provided header bytes")


# ── Mach-O ─────────────────────────────────────────────────────────────────────────────────────────

def _validate_macho_arm64(header: bytes) -> None:
    if len(header) < 8:
        raise ExecutableFormatError("Mach-O header too short")
    magic_be = struct.unpack_from(">I", header, 0)[0]
    if magic_be == _FAT_MAGIC:
        _validate_fat_macho_arm64(header)
        return
    if magic_be == _FAT_CIGAM:
        # Canonical macOS universal binaries store the fat header big-endian. A byte-swapped on-disk fat header
        # is not produced by Apple's tooling; reject it rather than guess an alternate encoding.
        raise ExecutableFormatError("non-canonical (byte-swapped) fat Mach-O header is not a supported universal binary")
    _validate_thin_macho_arm64(header)


def _validate_thin_macho_arm64(header: bytes) -> None:
    if len(header) < 32:
        raise ExecutableFormatError("thin Mach-O header is incomplete")
    magic_be = struct.unpack_from(">I", header, 0)[0]
    magic_le = struct.unpack_from("<I", header, 0)[0]
    if magic_le == _MACHO_MAGIC_64:
        endian = "<"
    elif magic_be == _MACHO_MAGIC_64:
        endian = ">"
    else:
        raise ExecutableFormatError("not a 64-bit thin Mach-O file")
    cputype = struct.unpack_from(endian + "i", header, 4)[0]
    filetype, ncmds, sizeofcmds = struct.unpack_from(endian + "III", header, 12)
    if (cputype & 0xFFFFFFFF) != _CPU_ARM64:
        raise ExecutableFormatError(f"Mach-O CPU is not the exact arm64 ABI (cputype={cputype})")
    if filetype != _MH_EXECUTE:
        raise ExecutableFormatError(f"Mach-O is not an executable (filetype={filetype})")
    # Bounded load commands: the region [32, 32+sizeofcmds) must be present, and the ncmds commands must chain
    # exactly across it with each cmdsize >= 8. A truncated region or a zero/oversized cmdsize is rejected.
    end = 32 + sizeofcmds
    if ncmds == 0 or sizeofcmds < 8 * ncmds or end > len(header):
        raise ExecutableFormatError("Mach-O load commands are truncated or implausible")
    off = 32
    for _ in range(ncmds):
        # The next command's 8-byte header must fit the declared region before it is read: sizeofcmds >= 8*ncmds
        # bounds the aggregate but an earlier oversized command can still exhaust the region (EXE-56-1). `end`
        # is already <= len(header), so this also bounds the read within the provided bytes.
        if off + 8 > end:
            raise ExecutableFormatError("Mach-O load-command header exceeds the declared region")
        cmdsize = struct.unpack_from(endian + "I", header, off + 4)[0]
        # A 64-bit Mach-O load command size must be >= 8 and a multiple of 8 (loader.h), and fit the region.
        if cmdsize < 8 or cmdsize % 8 != 0 or off + cmdsize > end:
            raise ExecutableFormatError("Mach-O load command has an invalid size")
        off += cmdsize
    if off != end:
        raise ExecutableFormatError("Mach-O load commands do not fill the declared region")


def _validate_fat_macho_arm64(header: bytes) -> None:
    # Fat header (big-endian): magic, nfat_arch, then nfat_arch × (cputype, cpusubtype, offset, size, align).
    if len(header) < 8:
        raise ExecutableFormatError("fat Mach-O header is incomplete")
    nfat = struct.unpack_from(">I", header, 4)[0]
    if nfat == 0 or nfat > _FAT_MAX_ARCHES:
        raise ExecutableFormatError(f"fat Mach-O architecture count is implausible (nfat={nfat})")
    table_end = 8 + nfat * _FAT_ARCH_SIZE
    if table_end > len(header):
        raise ExecutableFormatError("fat Mach-O descriptor table is truncated")
    for i in range(nfat):
        cputype, _cpusubtype, offset, size, align = struct.unpack_from(">iiIII", header, 8 + i * _FAT_ARCH_SIZE)
        if (cputype & 0xFFFFFFFF) != _CPU_ARM64:
            continue
        if size == 0:
            raise ExecutableFormatError("fat Mach-O arm64 slice has zero size")
        if offset < table_end or offset >= len(header):
            raise ExecutableFormatError("fat Mach-O arm64 slice offset is outside the provided bytes (incomplete)")
        if align > 31:                                     # a 32-bit fat offset cannot satisfy an enormous 2**align
            raise ExecutableFormatError(f"fat Mach-O slice alignment exponent is unsupported (align={align})")
        if offset % (1 << align) != 0:
            raise ExecutableFormatError(f"fat Mach-O slice offset violates its declared alignment (offset={offset}, align={align})")
        # An arm64 table LABEL is not slice proof, and a member must not borrow bytes beyond its declared extent:
        # validate the slice header/commands within BOTH the available prefix and the declared member size.
        _validate_thin_macho_arm64(header[offset:offset + size])
        return
    raise ExecutableFormatError("fat Mach-O has no arm64 slice")


# ── PE ─────────────────────────────────────────────────────────────────────────────────────────────

def _parse_pe(header: bytes) -> int:
    """Validate a complete-enough PE EXECUTABLE image header and return its COFF machine value.

    Requires MZ, a PE signature outside the DOS header, a full COFF header, an executable (non-DLL) image, a
    coherent PE32/PE32+ optional header contained in the provided bytes, and a contained declared section
    table. Raises :class:`ExecutableFormatError` otherwise. Does NOT check the machine value (that is the
    caller's portable-vs-installer decision).
    """
    if len(header) < 0x40 or header[:2] != b"MZ":
        raise ExecutableFormatError("not a PE/MZ file")
    e_lfanew = struct.unpack_from("<I", header, 0x3C)[0]
    if e_lfanew < 0x40:
        raise ExecutableFormatError("PE header offset overlaps the DOS header")
    coff = e_lfanew + 4
    if coff + 20 > len(header):
        raise ExecutableFormatError("PE/COFF header is truncated")
    if header[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        raise ExecutableFormatError("missing PE signature")
    machine, nsections = struct.unpack_from("<HH", header, coff)
    opt_size, characteristics = struct.unpack_from("<HH", header, coff + 16)
    if not characteristics & _IMAGE_FILE_EXECUTABLE_IMAGE:
        raise ExecutableFormatError("PE is not an executable image")
    if characteristics & _IMAGE_FILE_DLL:
        raise ExecutableFormatError("PE is a DLL, not an executable")
    opt_start = coff + 20
    if opt_size == 0 or opt_start + opt_size > len(header):
        raise ExecutableFormatError("PE optional header is absent or truncated")
    if opt_size < 2:                                   # the 2-byte magic must fit the declared span (and prefix)
        raise ExecutableFormatError("PE optional header is too small for its magic")
    opt_magic = struct.unpack_from("<H", header, opt_start)[0]
    if opt_magic == _PE_OPT_MAGIC_PE32_PLUS:
        min_opt, numrva_off = _PE_OPT_MIN_PE32_PLUS, _PE_NUMRVA_OFF_PE32_PLUS
    elif opt_magic == _PE_OPT_MAGIC_PE32:
        min_opt, numrva_off = _PE_OPT_MIN_PE32, _PE_NUMRVA_OFF_PE32
    else:
        raise ExecutableFormatError(f"PE optional header magic is invalid (0x{opt_magic:04x})")
    if opt_size < min_opt:
        raise ExecutableFormatError("PE optional header is too small to be coherent")
    # The declared data-directory count must fit within the declared optional-header extent — the fixed portion
    # plus num_dirs*8 records cannot exceed opt_size (this span is distinct from trailing section bytes).
    num_dirs = struct.unpack_from("<I", header, opt_start + numrva_off)[0]
    if min_opt + num_dirs * 8 > opt_size:
        raise ExecutableFormatError(f"PE data-directory count exceeds the optional header (n={num_dirs})")
    sect_start = opt_start + opt_size
    if sect_start + nsections * 40 > len(header):
        raise ExecutableFormatError("PE section table is truncated")
    return machine


def _validate_pe(header: bytes, want_machine: int) -> None:
    machine = _parse_pe(header)
    if machine != want_machine:
        raise ExecutableFormatError(f"PE architecture mismatch (machine={machine:#06x}, want {want_machine:#06x})")


# ── Public API ─────────────────────────────────────────────────────────────────────────────────────

def validate_onefile_executable(header: bytes, key: str) -> None:
    """Validate that *header* is a well-formed ONEFILE executable of the architecture for *key*.

    Raises :class:`ExecutableFormatError` on any format/architecture mismatch or incomplete structure. Validates
    format + CPU only — never runtime compatibility (GLIBC floor / macOS deployment target are separate
    qualifications). *key* must be one of the four canonical onefile keys; the Windows installer shape is
    validated by :func:`validate_windows_installer` instead.
    """
    if key not in _ONEFILE_KEYS:
        raise ExecutableFormatError(f"unsupported onefile key {key!r}")
    if not isinstance(header, (bytes, bytearray)) or len(header) < MIN_HEADER_BYTES:
        raise ExecutableFormatError("header is too short to validate")
    header = bytes(header)
    if key == "windows-x64":
        _validate_pe(header, _PE_AMD64)
    elif key == "linux-x64":
        _validate_elf(header, _EM_X86_64)
    elif key == "linux-arm64":
        _validate_elf(header, _EM_AARCH64)
    elif key == "macos-arm64":
        _validate_macho_arm64(header)


def validate_windows_installer(header: bytes) -> int:
    """Validate a Windows installer (``…-setup.exe``) is a well-formed PE and return its bootstrap machine.

    Applies the SAME structural PE validation as the portable path (so a truncated/corrupt bootstrap is
    rejected), but does NOT assert the packaged application's architecture: an Inno Setup 32-bit installer can
    install a 64-bit application, so the bootstrap PE machine is not evidence of the app arch. The packaged-app
    architecture is a separate installer-contract concern (a later layer with a full-package fixture).
    """
    if not isinstance(header, (bytes, bytearray)) or len(header) < MIN_HEADER_BYTES:
        raise ExecutableFormatError("header is too short to validate")
    return _parse_pe(bytes(header))
