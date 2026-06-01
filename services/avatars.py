"""Player avatar assets under ``ui/static/avatar/``."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Bot display name (normalized) → filename
_BOT_AVATAR_FILES: dict[str, str] = {
    "sidi daoued": "sidi daoued.png",
}

# Human avatar key → filename
PLAYER_AVATAR_FILES: dict[str, str] = {
    "male": "male.webp",
    "female": "wom.jpg",
}

PLAYER_AVATAR_LABELS: dict[str, str] = {
    "male": "Male",
    "female": "Female",
}

DEFAULT_PLAYER_AVATAR_KEY = "male"

_STATIC_AVATAR_DIR = Path(__file__).resolve().parent.parent / "ui" / "static" / "avatar"


def _normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name.strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s)


def bot_avatar_filename(display_name: str) -> str | None:
    """Return the avatar filename for a bot display name, if configured."""
    return _BOT_AVATAR_FILES.get(_normalize_name(display_name))


def bot_avatar_static_path(display_name: str) -> str | None:
    rel = bot_avatar_filename(display_name)
    return f"avatar/{rel}" if rel else None


def bot_avatar_file_exists(display_name: str) -> bool:
    filename = bot_avatar_filename(display_name)
    if not filename:
        return False
    return (_STATIC_AVATAR_DIR / filename).is_file()


def is_valid_player_avatar_key(key: str | None) -> bool:
    if not key:
        return False
    filename = PLAYER_AVATAR_FILES.get(key)
    if not filename:
        return False
    return (_STATIC_AVATAR_DIR / filename).is_file()


def player_avatar_static_path(key: str | None) -> str | None:
    if not is_valid_player_avatar_key(key):
        return None
    assert key is not None
    return f"avatar/{PLAYER_AVATAR_FILES[key]}"


def list_player_avatar_keys() -> list[str]:
    return [k for k in PLAYER_AVATAR_FILES if is_valid_player_avatar_key(k)]
