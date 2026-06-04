"""Guest session helpers.

The guest_id is a random hex token stored in the Flask session cookie.
This module records that identity in SQLite so it can be linked to a
future account, match history, and (eventually) a room.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from services.names import format_player_number

from .db import get_connection

logger = logging.getLogger(__name__)


def player_id_label(player_number: int | None) -> str:
    """Stable numeric id shown in UI (e.g. ``#0042``)."""
    if player_number is None:
        return ""
    return format_player_number(int(player_number))


def _allocate_player_number(conn) -> int:
    """Next global player number (1-based, monotonic)."""
    row = conn.execute(
        "SELECT COALESCE(MAX(player_number), 0) + 1 FROM guests"
    ).fetchone()
    return int(row[0]) if row else 1


def _default_display_name_for_number(player_number: int) -> str:
    return player_id_label(player_number) or "Guest"


_GUEST_RESTORE_MIGRATION_KEY = "guest_identity_restore_v1"


def _one_time_restore_all_guest_profiles(conn) -> None:
    """Reset every guest to their numeric id and require the welcome modal once."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    if conn.execute(
        "SELECT 1 FROM schema_meta WHERE key = ?",
        (_GUEST_RESTORE_MIGRATION_KEY,),
    ).fetchone():
        return

    guests = conn.execute(
        "SELECT guest_id, player_number FROM guests WHERE player_number IS NOT NULL"
    ).fetchall()
    for row in guests:
        id_name = _default_display_name_for_number(int(row["player_number"]))
        conn.execute(
            """
            UPDATE guests
            SET display_name = ?, profile_setup_done = 0
            WHERE guest_id = ?
            """,
            (id_name, row["guest_id"]),
        )

    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
        (_GUEST_RESTORE_MIGRATION_KEY, "1"),
    )
    logger.info(
        "One-time guest identity restore: reset %d profile(s) to numeric ids",
        len(guests),
    )


def sync_guest_identity(guest_id: str) -> Optional[dict]:
    """Ensure *guest_id* has a player number and default name when setup is pending."""
    gid = (guest_id or "").strip()
    if not gid:
        return None
    try:
        conn = get_connection()
        row = conn.execute(
            """
            SELECT guest_id, display_name, player_number, profile_setup_done
            FROM guests WHERE guest_id = ?
            """,
            (gid,),
        ).fetchone()
        if not row:
            conn.close()
            return None

        pn = row["player_number"]
        psd = int(row["profile_setup_done"] or 0)
        dn = (row["display_name"] or "").strip()
        changed = False

        if pn is None:
            pn = _allocate_player_number(conn)
            conn.execute(
                "UPDATE guests SET player_number = ? WHERE guest_id = ?",
                (pn, gid),
            )
            changed = True

        if not psd and pn is not None:
            id_name = _default_display_name_for_number(int(pn))
            if dn != id_name:
                conn.execute(
                    "UPDATE guests SET display_name = ? WHERE guest_id = ?",
                    (id_name, gid),
                )
                changed = True

        if changed:
            conn.commit()
        conn.close()
        return get_guest(gid)
    except Exception:
        logger.exception("Failed to sync guest identity %s", gid)
        return get_guest(gid)


def sync_all_guest_identities() -> None:
    """Backfill player numbers and default display names for all guests (startup migration)."""
    try:
        conn = get_connection()
        missing = conn.execute(
            "SELECT guest_id FROM guests WHERE player_number IS NULL"
        ).fetchall()
        for row in missing:
            pn = _allocate_player_number(conn)
            conn.execute(
                "UPDATE guests SET player_number = ? WHERE guest_id = ?",
                (pn, row["guest_id"]),
            )

        _one_time_restore_all_guest_profiles(conn)

        pending = conn.execute(
            """
            SELECT guest_id, player_number, display_name
            FROM guests
            WHERE profile_setup_done = 0 AND player_number IS NOT NULL
            """
        ).fetchall()
        for row in pending:
            pn = int(row["player_number"])
            id_name = _default_display_name_for_number(pn)
            dn = (row["display_name"] or "").strip()
            if dn != id_name:
                conn.execute(
                    "UPDATE guests SET display_name = ? WHERE guest_id = ?",
                    (id_name, row["guest_id"]),
                )

        placeholders = conn.execute(
            """
            SELECT guest_id, player_number
            FROM guests
            WHERE player_number IS NOT NULL
              AND profile_setup_done = 1
              AND (TRIM(display_name) = '' OR display_name = 'Guest')
            """
        ).fetchall()
        for row in placeholders:
            pn = int(row["player_number"])
            conn.execute(
                """
                UPDATE guests
                SET display_name = ?, profile_setup_done = 0
                WHERE guest_id = ?
                """,
                (_default_display_name_for_number(pn), row["guest_id"]),
            )

        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Failed to sync all guest identities")


def ensure_guest(
    guest_id: str,
    display_name: str | None = None,
    *,
    profile_setup_done: int = 1,
) -> str:
    """Insert a guest row if needed; return the row's display_name."""
    gid = (guest_id or "").strip()
    if not gid:
        return display_name or "Guest"

    try:
        conn = get_connection()
        existing = conn.execute(
            """
            SELECT display_name, player_number, profile_setup_done
            FROM guests WHERE guest_id = ?
            """,
            (gid,),
        ).fetchone()
        if existing:
            conn.close()
            synced = sync_guest_identity(gid)
            if synced:
                return (synced.get("display_name") or "").strip() or "Guest"
            return (existing["display_name"] or "").strip() or "Guest"

        player_number = _allocate_player_number(conn)
        dn = (display_name or "").strip()
        if not dn or dn == "Guest":
            dn = _default_display_name_for_number(player_number)

        conn.execute(
            """
            INSERT INTO guests
                (guest_id, display_name, created_at, profile_setup_done, player_number)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                gid,
                dn,
                datetime.now(timezone.utc).isoformat(),
                1 if profile_setup_done else 0,
                player_number,
            ),
        )
        conn.commit()
        conn.close()
        return dn
    except Exception:
        logger.exception("Failed to ensure guest %s", gid)
        return display_name or "Guest"


def mark_profile_setup_done(guest_id: str) -> None:
    """Mark that the guest completed the first-time name/avatar setup."""
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE guests SET profile_setup_done = 1 WHERE guest_id = ?",
            (guest_id,),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Failed to mark profile setup for guest %s", guest_id)


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


def update_avatar_key(guest_id: str, avatar_key: str | None) -> None:
    """Persist the guest's chosen portrait key (``male`` / ``female``), or clear it."""
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE guests SET avatar_key = ? WHERE guest_id = ?",
            (avatar_key, guest_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Failed to update avatar for guest %s", guest_id)


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
