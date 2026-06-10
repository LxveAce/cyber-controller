"""Profile loader — reconcile the rich firmware-profile JSON schema with the
hardware-validated flash core.

The shipped ``src/config/profiles/*.json`` use a rich schema:

    {"id", "name", "description", "backend", "protocol",
     "boards": [{"name","chip","flash_size","flash_mode","flash_freq",...}],
     "default_baud", "firmware_urls": {"latest": "..."}, ...}

The flat ``flash_engine.FirmwareProfile`` dataclass only understood
``{name,board,backend,files,baud,chip,erase_first,extra_args}`` — so loading a
shipped profile dropped EVERY rich key (the keys don't intersect) and produced an
esptool ``write_flash`` with **zero** address/binary pairs (a silently broken flash).

This module is the adapter. It maps a JSON profile ``id`` to a concrete
:mod:`src.core.flash_core` profile (which already encodes the correct per-board
variant selection, merged-vs-multi image model, and per-chip bootloader offsets,
including the ESP32-C5 0x2000 gotcha) and exposes helpers the flash engine uses to
select a chip and resolve the download URL.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# JSON profile id -> flash_core PROFILES key. (The flash core keys differ in a few
# cases: ghost_esp->ghostesp, esp32_div->esp32-div, minigotchi->minigotchi-v3,
# flipper_momentum->momentum, flipper_unleashed->unleashed, *_*->*-*.)
CORE_ID_MAP: dict[str, str] = {
    "marauder": "marauder",
    "esp32_div": "esp32-div",
    "bruce": "bruce",
    "ghost_esp": "ghostesp",
    "halehound": "halehound",
    "meshtastic": "meshtastic",
    "flock_you": "flock-you",
    "oui_spy": "oui-spy",
    "sky_spy": "sky-spy",
    "airtag_scanner": "airtag-scanner",
    "cyt_ng": "cyt-ng",
    "minigotchi": "minigotchi-v3",
    "flipper_momentum": "momentum",
    "flipper_unleashed": "unleashed",
    # ids that are already canonical / new profiles map to themselves; unknown -> custom
}


def core_id_for(json_id: str) -> str:
    """Return the flash_core profile id for a JSON profile id (defaults to itself,
    then to 'custom' for a truly unknown local .bin profile)."""
    if not json_id:
        return "custom"
    return CORE_ID_MAP.get(json_id, json_id)


def load_rich(path: str | Path) -> dict[str, Any]:
    """Load the full rich profile JSON dict (nothing dropped)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def list_boards(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the profile's board list (rich schema), or an empty list."""
    boards = data.get("boards")
    return boards if isinstance(boards, list) else []


def select_board(data: dict[str, Any], board_name: str | None = None) -> dict[str, Any] | None:
    """Pick a board entry by name, else the first board, else None."""
    boards = list_boards(data)
    if not boards:
        return None
    if board_name:
        for b in boards:
            if b.get("name") == board_name:
                return b
    return boards[0]


def select_chip(data: dict[str, Any], requested_chip: str | None = None, board_name: str | None = None) -> str:
    """Resolve the esptool chip id for a flash.

    Precedence: explicit *requested_chip* > the chosen board's ``chip`` >
    ``"auto"`` (which signals the engine to run chip detection).
    """
    if requested_chip:
        return requested_chip
    board = select_board(data, board_name)
    if board and board.get("chip"):
        return str(board["chip"])
    return "auto"


def default_baud(data: dict[str, Any]) -> int:
    """Resolve the flash baud (rich ``default_baud`` > 921600)."""
    try:
        return int(data.get("default_baud") or 921600)
    except (TypeError, ValueError):
        return 921600


def expected_sha256(data: dict[str, Any], version: str = "latest") -> str | None:
    """Return a pinned SHA-256 for a firmware version, if the profile declares one.

    Looked up under ``firmware_sha256: {version: hex}`` or a per-url ``sha256`` map.
    Returning None means 'not pinned' (the vault will warn but not block).
    """
    pins = data.get("firmware_sha256")
    if isinstance(pins, dict):
        val = pins.get(version) or pins.get("latest")
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
    return None


def firmware_url(data: dict[str, Any], version: str = "latest") -> str | None:
    """Resolve the firmware download URL for a version from ``firmware_urls``."""
    urls = data.get("firmware_urls")
    if isinstance(urls, dict):
        return urls.get(version) or urls.get("latest")
    return None
