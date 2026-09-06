"""Strict release/asset selection + size/checksum helpers for a future update-apply layer (Layer 2a).

These are PURE, side-effect-free helpers. They do NOT download, stage, replace, or restart anything — they only
decide, from release metadata, exactly one supported release + one exact asset for the running platform, and
validate its published size and checksum. The permissive selection in :mod:`src.core.self_update`
(``platform_key`` mapping unsupported machines to x64/arm64, ``select_asset`` accepting any substring/archive
match, ``find_release`` digit-equivalence) is unsafe for apply; this module is the strict replacement a future
apply layer will pin against. Executable-format (ELF/Mach-O/PE) validation is a separate follow-up (Layer 2b).

Contract highlights (from the reviewed updater plan §4/§7.2):
* Only exact supported ``(system, machine)`` aliases resolve to a key; riscv64/armv7l/i686 and Intel macOS are
  explicitly UNSUPPORTED, never silently mapped to a near key.
* An asset is selected by EXACT expected filename for the release tag + platform + build shape — never a
  substring or archive match, and never when the name is ambiguous (more than one candidate).
* The release is the exact published (non-draft, non-prerelease) tag identity — never a digit-only equivalent,
  draft, or prerelease.
* The asset size must be a positive plain integer within an independent maximum; the checksum must be one
  unambiguous digest for that exact filename.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from src.core import install

# The public release artifact name stem. Assets are ``<_STEM>-<tag>-<key>[suffix]``.
_STEM = "cyber-controller"

# An independent plausibility ceiling for a release binary/installer download (bytes). A real onefile build is
# tens–hundreds of MB; anything beyond this is rejected as implausible rather than trusted from metadata.
MAX_ASSET_BYTES = 1024 * 1024 * 1024   # 1 GiB

# Exact machine aliases per supported architecture. Anything outside these is UNSUPPORTED (never coerced).
_X64_ALIASES = frozenset({"x86_64", "amd64", "x64"})
_ARM64_ALIASES = frozenset({"aarch64", "arm64"})

# Exact supported system spellings (platform.system() lowercased, plus sys.platform's "win32"). An unknown
# prefix like "windowsce" is NOT windows (L2A-3).
_WINDOWS_SYSTEMS = frozenset({"windows", "win32"})

# The only canonical asset keys. A caller-supplied key outside this set (e.g. "linux-riscv64", "macos-x64",
# "windows-arm64") is rejected so the selector cannot be driven past supported_platform_key (L2A-3).
_SUPPORTED_KEYS = frozenset({"windows-x64", "linux-x64", "linux-arm64", "macos-arm64"})


class ReleaseSelectionError(Exception):
    """A finite, user-surfaceable selection/validation failure. Callers stay on the current build."""


class UnsupportedPlatform(ReleaseSelectionError):
    """The running (system, machine) has no supported release artifact — not a mislabelled asset."""


# ── Platform ────────────────────────────────────────────────────────────────────────────────────────

def supported_platform_key(system: str, machine: str) -> str:
    """Canonical asset key for a SUPPORTED (system, machine), else raise :class:`UnsupportedPlatform`.

    Unlike the permissive ``self_update.platform_key`` (which maps every non-ARM Linux machine to linux-x64 and
    Intel macOS to the arm64 package), this rejects any machine that is not an exact supported alias — a
    riscv64/armv7l/i686 Linux host and an Intel (x86_64) macOS host are UNSUPPORTED, not near-matched.
    """
    sys_l = (system or "").strip().lower()
    mach_l = (machine or "").strip().lower()
    if sys_l in _WINDOWS_SYSTEMS:            # exact spellings only — "windowsce"/"win-not-an-os" are not Windows
        if mach_l in _X64_ALIASES:
            return "windows-x64"
        raise UnsupportedPlatform(f"unsupported Windows machine for update: {machine!r}")
    if sys_l == "darwin":
        if mach_l in _ARM64_ALIASES:
            return "macos-arm64"
        raise UnsupportedPlatform(f"unsupported macOS machine for update: {machine!r} (only arm64 is published)")
    if sys_l == "linux":
        if mach_l in _X64_ALIASES:
            return "linux-x64"
        if mach_l in _ARM64_ALIASES:
            return "linux-arm64"
        raise UnsupportedPlatform(f"unsupported Linux machine for update: {machine!r}")
    raise UnsupportedPlatform(f"unsupported system for update: {system!r}")


# ── Exact asset name ─────────────────────────────────────────────────────────────────────────────────

def expected_asset_name(tag: str, key: str, *, installer: bool = False) -> str:
    """The EXACT published asset filename for *tag* + platform *key* + build shape.

    Windows portable is ``<stem>-<tag>-windows-x64.exe``; the Windows installer is
    ``<stem>-<tag>-windows-x64-setup.exe``. Linux/macOS onefile builds are extensionless
    (``<stem>-<tag>-<key>``). ``installer`` is only meaningful for the Windows key.
    """
    if not isinstance(tag, str) or not tag:
        raise ReleaseSelectionError("a release tag is required to build the expected asset name")
    if key not in _SUPPORTED_KEYS:
        # A caller must pass a canonical key (from supported_platform_key); an arbitrary key like
        # "linux-riscv64"/"macos-x64"/"windows-arm64" is rejected so the selector stays a strict boundary.
        raise ReleaseSelectionError(f"unsupported asset key {key!r}")
    base = f"{_STEM}-{tag}-{key}"
    if key == "windows-x64":
        return f"{base}-setup.exe" if installer else f"{base}.exe"
    if installer:
        raise ReleaseSelectionError(f"no installer asset shape for non-Windows key {key!r}")
    return base


# ── Release + asset selection ─────────────────────────────────────────────────────────────────────────

def select_published_release(releases: Sequence[Mapping[str, Any]], tag: str) -> dict:
    """Return the exact published (non-draft, non-prerelease) release whose ``tag_name`` EXACTLY equals *tag*.

    Unlike ``self_update.find_release``, this does NOT use digit-only version equivalence — apply must pin the
    exact tag the check offered, never a different tag that merely parses equal. Raises if there is no exact
    published match, or the exact match is a draft/prerelease.
    """
    if not isinstance(tag, str) or not tag:
        raise ReleaseSelectionError("no target release tag")
    # Exact SOURCE-string identity: the entry's tag_name must itself be a str equal to *tag* — never a numeric
    # tag_name coerced through str() (L2A-2). Non-str/other entries are simply not matches.
    exact = [r for r in releases
             if isinstance(r, Mapping) and isinstance(r.get("tag_name"), str) and r.get("tag_name") == tag]
    if not exact:
        raise ReleaseSelectionError(f"no release with exact tag {tag!r}")
    if len(exact) > 1:
        raise ReleaseSelectionError(f"ambiguous release tag {tag!r} ({len(exact)} matches)")
    rel = exact[0]
    # Publication must be EXPLICIT booleans: a missing flag, None, [] or 0 is unknown metadata, not proof of a
    # stable published release (L2A-2).
    if rel.get("draft") is not False:
        raise ReleaseSelectionError(f"release {tag!r} is not explicitly published (draft flag not False)")
    if rel.get("prerelease") is not False:
        raise ReleaseSelectionError(f"release {tag!r} is not explicitly stable (prerelease flag not False)")
    return dict(rel)


def select_release_asset(assets: Sequence[Mapping[str, Any]], tag: str, key: str, *,
                         installer: bool = False) -> dict:
    """Return the ONE asset whose name EXACTLY equals :func:`expected_asset_name`, else raise.

    Exact-equality selection rejects the substring/archive matches the permissive selector accepted (e.g.
    ``<stem>-<tag>-linux-x64.zip`` or ``debug-symbols-linux-x64.tar.gz``), and rejects an ambiguous catalog
    where more than one asset carries the exact expected name.
    """
    want = expected_asset_name(tag, key, installer=installer)
    matches = [a for a in assets if isinstance(a, Mapping) and str(a.get("name") or "") == want]
    if not matches:
        raise ReleaseSelectionError(f"release {tag!r} has no exact asset {want!r}")
    if len(matches) > 1:
        raise ReleaseSelectionError(f"ambiguous asset {want!r} ({len(matches)} entries)")
    return dict(matches[0])


# ── Size + checksum ───────────────────────────────────────────────────────────────────────────────────

def validate_asset_size(asset: Mapping[str, Any], *, maximum: int = MAX_ASSET_BYTES) -> int:
    """Return the asset's expected size as a positive plain ``int`` within *maximum*, else raise.

    Rejects a missing size, a bool, a float/non-integer, a non-positive size, and an implausibly large size —
    Content-Length alone is never trusted downstream; this is the independent expected-size bound.
    """
    # The ceiling itself must be a positive PLAIN int (L2A-4/L2A-4b): a nonfinite/float override (inf/nan)
    # would silently disable the guard, and an int SUBCLASS (Count(int)/IntEnum) is not the declared plain-int
    # contract. `type() is int` rejects bool and every subclass. A lower plain-int policy override is allowed.
    if type(maximum) is not int or maximum <= 0:
        raise ReleaseSelectionError(f"size maximum must be a positive integer, got {maximum!r}")
    size = asset.get("size")
    if type(size) is not int:   # exact built-in int only (rejects bool and int subclasses) — the plain-int contract
        raise ReleaseSelectionError(f"asset size must be an integer, got {size!r}")
    if size <= 0:
        raise ReleaseSelectionError(f"asset size must be positive, got {size}")
    if size > maximum:
        raise ReleaseSelectionError(f"asset size {size} exceeds the maximum {maximum}")
    return size


def parse_sums_strict(text: str) -> dict[str, str]:
    """Parse a ``sha256sum``-style manifest, raising on a name that carries CONFLICTING digests.

    The permissive ``self_update.parse_sha256sums`` keeps the last line for a repeated name, silently hiding an
    ambiguous manifest. Here a name mapped to two DIFFERENT digests is a hard error; an identical repeat is
    tolerated. Malformed/comment lines are skipped.
    """
    sums: dict[str, str] = {}
    # Split on ACTUAL LF record boundaries only (preserving the CRLF convention below). str.splitlines() also
    # breaks on \v, \f, \x85,   and   — none of which are LF delimiters in this untagged shape — which
    # would truncate a filename that literally contains one and misattribute its digest (L2A-1b).
    records = text.split("\n")
    last_index = len(records) - 1
    for index, raw in enumerate(records):
        # Remove at most ONE trailing CR, and only for a genuine CRLF terminator. A record is terminated by LF
        # only, so components before the last were LF-terminated (their final CR, if present, is that CRLF's CR)
        # while the last component was NOT terminated (any trailing CR there is filename data). Never strip
        # repeated CRs, and never strip a CR on the unterminated final record — those are filename bytes;
        # leaving them makes the name miss the exact canonical match rather than silently authorizing it
        # (L2A-1c). rstrip("\r") removed all of them and changed checksum filename identity.
        line = raw[:-1] if (index != last_index and raw.endswith("\r")) else raw
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # GNU untagged format: <64-hex><SP><mode><filename>, where <mode> is ' ' (text) or '*' (binary) and
        # the FILENAME is the entire remainder — it may contain spaces and asterisks (L2A-1). Never split on
        # all whitespace/take the last token, and never strip leading '*' from the name (that changes identity).
        if len(line) < 66 or line[64] != " ":
            continue
        digest = line[:64].lower()
        if any(c not in "0123456789abcdef" for c in digest):
            continue
        mode = line[65]
        if mode not in (" ", "*"):
            continue                          # not the untagged GNU shape — skip rather than guess identity
        name = line[66:]
        if not name:
            continue
        prev = sums.get(name)
        if prev is not None and prev != digest:
            raise ReleaseSelectionError(f"ambiguous checksum for {name!r}: two different digests")
        sums[name] = digest
    return sums


def checksum_for(sums: Mapping[str, str], name: str) -> str:
    """Return the one published digest for *name*, else raise. Pair with :func:`parse_sums_strict`."""
    digest = sums.get(name)
    if not digest:
        raise ReleaseSelectionError(f"no checksum published for {name!r}")
    return digest


def tag_is_newer(current: str, tag: str) -> bool:
    """True iff *tag* parses strictly newer than *current* (tolerant of v-prefixes). Availability only — the
    exact-tag pinning above is what apply uses, never this comparison."""
    return install._parse(tag) > install._parse(current)
