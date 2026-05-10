"""Guest session helpers.

The guest_id is a random hex token stored in the Flask session cookie.
This module records that identity in SQLite so it can be linked to a
future account, match history, and (eventually) a room.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from .db import get_connection

logger = logging.getLogger(__name__)


def ensure_guest(guest_id: str, display_name: str = "Guest") -> None:
    """Insert a guest row if it doesn't already exist (idempotent)."""
    try:
        conn = get_connection()
        conn.execute(
            """
            INSERT OR IGNORE INTO guests (guest_id, display_name, created_at)
            VALUES (?, ?, ?)
            """,
            (guest_id, display_name, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Failed to ensure guest %s", guest_id)


def get_guest(guest_id: str) -> Optional[dict]:
    """Return the guest row as a dict, or None if not found."""
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT * FROM guests WHERE guest_id = ?", (guest_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        logger.exception("Failed to fetch guest %s", guest_id)
        return None


def update_display_name(guest_id: str, display_name: str) -> None:
    """Persist a new display name for an existing guest row."""
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE guests SET display_name = ? WHERE guest_id = ?",
            (display_name, guest_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Failed to update display name for guest %s", guest_id)


def link_guest_to_account(guest_id: str, account_id: int) -> None:
    """Set accounts.guest_id so match history survives sign-up."""
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE accounts SET guest_id = ? WHERE id = ?",
            (guest_id, account_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Failed to link guest %s → account %d", guest_id, account_id)
