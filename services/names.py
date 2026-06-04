"""services/names.py — Default display names and guest-avatar helpers.

Public surface
--------------
    format_player_number(n: int) -> str
        Formats a stable numeric player id (e.g. "#1042").

    ensure_default_display_name(guest_id: str) -> str
        Ensures the guest row exists and returns its default numeric name.

    avatar_color(guest_id: str) -> str
        Stable hex colour from guest_id.

    is_clean(name: str) -> bool
        Minimal profanity check for custom names.
"""

from __future__ import annotations

import hashlib


def format_player_number(player_number: int) -> str:
    """Format a sequential player id for display (4+ digits when under 10k)."""
    n = int(player_number)
    if n < 1:
        n = 1
    return f"#{n:04d}" if n < 10000 else f"#{n}"


def ensure_default_display_name(guest_id: str, *, profile_setup_done: int = 1) -> str:
    """Create guest if missing with a numeric default name; return display_name."""
    from models.guests import ensure_guest  # local import avoids app↔models cycles

    return ensure_guest(guest_id, profile_setup_done=profile_setup_done)


def generate_display_name() -> str:
    """Deprecated: names are assigned per guest in the database. Use ensure_default_display_name."""
    raise RuntimeError(
        "generate_display_name() is obsolete; call ensure_default_display_name(guest_id) instead."
    )


# ---------------------------------------------------------------------------
# Avatar colour
# ---------------------------------------------------------------------------

_AVATAR_PALETTE: list[str] = [
    "#c0392b",
    "#d35400",
    "#e6ac00",
    "#27ae60",
    "#16a085",
    "#2980b9",
    "#8e44ad",
    "#2c3e50",
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
