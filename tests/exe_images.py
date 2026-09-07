"""Structurally complete, minimal executable images for inert updater tests.

These are not real programs and are never executed: each builder yields bytes whose fixed headers
and declared tables are coherent for ``src.core.update_exe_format``, so tests can stage, plan and
validate without a real binary. Parameters move the data-dependent structures (program-header
table, PE header, load commands, fat slice) so that extents beyond 4096 bytes can be exercised.
"""
from __future__ import annotations

import struct

EM_X86_64, EM_AARCH64 = 62, 183
PE_AMD64, PE_I386, PE_ARM64 = 0x8664, 0x014C, 0xAA64
CPU_ARM64, CPU_X86_64 = 0x0100000C, 0x01000007


def elf64(machine=EM_X86_64, *, phoff=64, phnum=1, kind=3, total=None):
    """ELF64 executable (ET_DYN by default) with ``phnum`` program headers at ``phoff``; the
    validator needs exactly ``phoff + phnum * 56`` bytes."""
    end = phoff + phnum * 56
    b = bytearray(total or max(512, end + 64))
    b[:16] = b"\x7fELF" + bytes([2, 1, 1, 0, 0]) + b"\x00" * 7
    struct.pack_into("<HHIQQQIHHHHHH", b, 16, kind, machine, 1, 0x400100, phoff, 0, 0,
                     64, 56, phnum, 64, 0, 0)
    for i in range(phnum):
        struct.pack_into("<IIQQQQQQ", b, phoff + i * 56,
                         1, 5, 0, 0x400000, 0x400000, 512, 512, 4096)
    return bytes(b)


def pe32plus(machine=PE_AMD64, *, lfanew=0x80, total=None):
    """PE executable image with the PE header at ``lfanew`` and one section; the validator needs
    exactly ``lfanew + 24 + SizeOfOptionalHeader + 40`` bytes (PE32+ for AMD64/ARM64, PE32 for
    i386)."""
    is64 = machine != PE_I386
    optsize = 240 if is64 else 224
    o = lfanew + 24
    end = o + optsize + 40
    b = bytearray(total or max(1024, end + 64))
    b[:2] = b"MZ"
    struct.pack_into("<I", b, 0x3C, lfanew)
    b[lfanew:lfanew + 4] = b"PE\x00\x00"
    struct.pack_into("<HHIIIHH", b, lfanew + 4, machine, 1, 0, 0, 0, optsize, 0x22)
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
    return bytes(b)


def macho_thin(cpu=CPU_ARM64, *, endian="<", sizeofcmds=None, total=None):
    """64-bit thin Mach-O executable. Default: two coherent load commands (sizeofcmds 96, 256 bytes
    total). With ``sizeofcmds`` (a multiple of 8, >= 8): one command of that size, so the validator
    needs exactly ``32 + sizeofcmds`` bytes."""
    if sizeofcmds is None:
        b = bytearray(total or 256)
        struct.pack_into(endian + "IiiIIIII", b, 0, 0xFEEDFACF, cpu, 0, 2, 2, 96, 0x200085, 0)
        struct.pack_into(endian + "II16sQQQQiiII", b, 32, 0x19, 72, b"__TEXT",
                         0x100000000, 4096, 0, 256, 7, 5, 0, 0)
        struct.pack_into(endian + "IIQQ", b, 104, 0x80000028, 24, 128, 0)
        return bytes(b)
    if sizeofcmds < 8 or sizeofcmds % 8:
        raise ValueError("sizeofcmds must be a multiple of 8 and at least 8")
    b = bytearray(total or max(256, 32 + sizeofcmds + 64))
    struct.pack_into(endian + "IiiIIIII", b, 0, 0xFEEDFACF, cpu, 0, 2, 1, sizeofcmds, 0x200085, 0)
    struct.pack_into(endian + "II", b, 32, 0x1B, sizeofcmds)
    return bytes(b)


def macho_thin_arm64(**kwargs):
    return macho_thin(CPU_ARM64, **kwargs)


def macho_fat(*, cpus=(CPU_X86_64, CPU_ARM64), first_offset=512, stride=512, total=None):
    """Canonical big-endian universal file: one 256-byte thin slice per cpu, the i-th at
    ``first_offset + i * stride`` (multiples of 512, alignment exponent 9). The validator needs the
    descriptor table plus the arm64 slice's header and commands: ``offset + 128``."""
    n = len(cpus)
    last = first_offset + (n - 1) * stride
    b = bytearray(total or max(last + 256, first_offset + n * stride))
    struct.pack_into(">II", b, 0, 0xCAFEBABE, n)
    for i, cpu in enumerate(cpus):
        off = first_offset + i * stride
        struct.pack_into(">iiIII", b, 8 + i * 20, cpu, 0, off, 256, 9)
        b[off:off + 256] = macho_thin(cpu)
    return bytes(b)


IMAGE_FOR_KEY = {
    "windows-x64": pe32plus,
    "linux-x64": lambda: elf64(EM_X86_64),
    "linux-arm64": lambda: elf64(EM_AARCH64),
    "macos-arm64": macho_thin_arm64,
}


def image_for(key: str) -> bytes:
    """A minimal valid image for a published platform key."""
    return IMAGE_FOR_KEY[key]()
