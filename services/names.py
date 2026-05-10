"""services/names.py — Tunisian display-name generation and guest-avatar helpers.

Public surface
--------------
    generate_display_name() -> str
        Returns a random Tunisian-style name like "Si Ahmed", "Khalti Leila
        el-Kahwagi", or "Houcine Sfaxi".  No two calls are guaranteed to
        differ, so callers should store the result immediately.

    avatar_color(guest_id: str) -> str
        Returns a stable hex colour string for the given guest_id.
        The colour is deterministic: same guest_id → same colour.

    is_clean(name: str) -> bool
        Returns True when the name contains no words from the block-list.
        Intentionally minimal — avoids over-filtering Tunisian names.
"""

from __future__ import annotations

import hashlib
import random

# ---------------------------------------------------------------------------
# Name components
# ---------------------------------------------------------------------------

# Male given names / honorific combos
_MALE: list[str] = [
    "Si Ahmed",
    "Si Mohamed",
    "Si Brahim",
    "Houcine",
    "Youssef",
    "Karim",
    "Maher",
    "Ridha",
    "Tarek",
    "Jamel",
    "Nabil",
    "Sami",
    "Hatem",
    "Lotfi",
    "Chokri",
]

# Female given names / honorific combos
_FEMALE: list[str] = [
    "Khalti Aïcha",
    "Khalti Fatma",
    "Leila",
    "Samira",
    "Nadia",
    "Sonia",
    "Rania",
    "Ines",
    "Amira",
    "Sirine",
    "Salma",
    "Houda",
]

# Regional / occupational epithets (appended ~55% of the time)
_EPITHETS: list[str] = [
    "el-Kahwagi",
    "el-Tounsi",
    "Sfaxi",
    "Bizerti",
    "el-Qairouani",
    "Nabeuli",
    "Hammamet",
    "el-Ariani",
    "Monastiri",
    "Souassi",
    "Mahboubi",
    "Zarrougi",
]

# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def generate_display_name() -> str:
    """Return a random Tunisian-style display name (12–28 chars typical)."""
    name = random.choice(_MALE + _FEMALE)
    if random.random() < 0.55:
        name = f"{name} {random.choice(_EPITHETS)}"
    return name


# ---------------------------------------------------------------------------
# Avatar colour
# ---------------------------------------------------------------------------

_AVATAR_PALETTE: list[str] = [
    "#c0392b",  # pomegranate red
    "#d35400",  # pumpkin orange
    "#e6ac00",  # warm gold
    "#27ae60",  # nephritis green
    "#16a085",  # green-sea teal
    "#2980b9",  # belize blue
    "#8e44ad",  # wisteria purple
    "#2c3e50",  # midnight blue
]


def avatar_color(guest_id: str) -> str:
    """Return a stable hex colour for *guest_id* (deterministic, 8-colour palette)."""
    digest = int(hashlib.md5(guest_id.encode(), usedforsecurity=False).hexdigest()[:8], 16)
    return _AVATAR_PALETTE[digest % len(_AVATAR_PALETTE)]


# ---------------------------------------------------------------------------
# Profanity filter  (intentionally minimal)
# ---------------------------------------------------------------------------

_BLOCKED: frozenset[str] = frozenset({
    "fuck", "shit", "ass", "bitch", "cunt", "dick", "pussy",
    "cock", "whore", "slut", "nigger", "faggot",
})


def is_clean(name: str) -> bool:
    """Return True when *name* contains no blocked words (case-insensitive)."""
    lower = name.lower()
    return not any(w in lower for w in _BLOCKED)
