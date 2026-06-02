"""Database schema for multiplayer tables.

Uses the same SQLite file as engine/persistence.py so there is only one
database file at the project root.  This module adds/migrates tables
that engine/persistence.py doesn't know about.
"""

import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Re-use the same database file as the engine persistence layer.
DB_PATH = Path(__file__).parent.parent / "chkobba_games.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def get_connection() -> sqlite3.Connection:
    """Return a raw connection (caller must close)."""
    return _connect()


def init_models() -> None:
    """Create multiplayer tables if they don't already exist."""
    conn = _connect()
    cur = conn.cursor()

    # ------------------------------------------------------------------
    # guests — one row per browser session; UUID is the primary key.
    # guest_id is stored in the Flask session cookie and in this table.
    # A guest can later be linked to an account via accounts.guest_id.
    # ------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS guests (
            guest_id    TEXT PRIMARY KEY,
            display_name TEXT NOT NULL DEFAULT 'Guest',
            created_at  TEXT NOT NULL
        )
    """)
    try:
        cur.execute("ALTER TABLE guests ADD COLUMN avatar_key TEXT")
    except Exception:
        pass

    # ------------------------------------------------------------------
    # accounts — optional registered users.
    # guest_id links the account back to the guest row created before
    # registration, so match history is preserved on sign-up.
    # ------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            email         TEXT    UNIQUE NOT NULL,
            password_hash TEXT    NOT NULL,
            display_name  TEXT    NOT NULL,
            guest_id      TEXT    REFERENCES guests(guest_id),
            created_at    TEXT    NOT NULL
        )
    """)

    # ------------------------------------------------------------------
    # mp_matches — records for completed multiplayer matches.
    # players_json : JSON array of {guest_id, display_name, seat}.
    # scores_json  : JSON array of [score_seat0, score_seat1].
    # mode         : '1v1' | '1v1_bot' | '2v2'
    # winner       : guest_id of winning player, or NULL for draw.
    # was_forfeit  : 1 if winner won because opponent disconnected/forfeited.
    # ------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mp_matches (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id      TEXT    NOT NULL,
            mode         TEXT    NOT NULL DEFAULT '1v1',
            players_json TEXT    NOT NULL DEFAULT '[]',
            scores_json  TEXT    NOT NULL DEFAULT '[]',
            winner       TEXT,
            was_forfeit  INTEGER NOT NULL DEFAULT 0,
            ended_at     TEXT
        )
    """)

    # Migrate: add was_forfeit to existing tables that don't have it yet.
    try:
        cur.execute("ALTER TABLE mp_matches ADD COLUMN was_forfeit INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # column already exists

    cur.execute("CREATE INDEX IF NOT EXISTS idx_mp_matches_room ON mp_matches(room_id)")

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_mp_matches_player
        ON mp_matches(players_json)
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS guest_friends (
            guest_id        TEXT NOT NULL,
            friend_guest_id TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            PRIMARY KEY (guest_id, friend_guest_id)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_guest_friends_friend
        ON guest_friends(friend_guest_id)
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS guest_friend_requests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            from_guest_id   TEXT NOT NULL,
            to_guest_id     TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            created_at      TEXT NOT NULL,
            responded_at    TEXT,
            UNIQUE(from_guest_id, to_guest_id)
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_friend_requests_to
        ON guest_friend_requests(to_guest_id, status)
    """)

    conn.commit()
    conn.close()
    logger.info("Multiplayer tables initialised in %s", DB_PATH)


def record_match(
    *,
    room_id: str,
    mode: str,
    players: list[dict],
    scores: list[int],
    winner_guest_id: str | None,
    was_forfeit: bool = False,
) -> None:
    """Insert one completed match row into mp_matches.

    ``players`` should be a list of dicts: [{guest_id, display_name, seat}, ...].
    ``scores``  should be [score_seat0, score_seat1].
    """
    import json
    from datetime import datetime, timezone
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT 1 FROM mp_matches WHERE room_id = ?", (room_id,)
        ).fetchone()
        if existing:
            return
        conn.execute(
            """
            INSERT INTO mp_matches
                (room_id, mode, players_json, scores_json, winner, was_forfeit, ended_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                room_id,
                mode,
                json.dumps(players),
                json.dumps(scores),
                winner_guest_id,
                1 if was_forfeit else 0,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_matches(guest_id: str, limit: int = 20) -> list[dict]:
    """Return the most recent ``limit`` matches that ``guest_id`` participated in."""
    import json
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, room_id, mode, players_json, scores_json,
                   winner, was_forfeit, ended_at
            FROM mp_matches
            WHERE players_json LIKE ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (f"%{guest_id}%", limit),
        ).fetchall()
    finally:
        conn.close()

    results = []
    for row in rows:
        players = json.loads(row["players_json"] or "[]")
        scores  = json.loads(row["scores_json"]  or "[]")
        # Verify the guest_id is actually in this match (LIKE can produce false positives).
        if not any(p.get("guest_id") == guest_id for p in players):
            continue
        results.append({
            "id":          row["id"],
            "room_id":     row["room_id"],
            "mode":        row["mode"],
            "players":     players,
            "scores":      scores,
            "winner":      row["winner"],
            "was_forfeit": bool(row["was_forfeit"]),
            "ended_at":    row["ended_at"],
        })
    return results


def get_matches_between_players(
    guest_id_a: str,
    guest_id_b: str,
    *,
    limit: int = 150,
) -> list[dict]:
    """Matches where *both* guests played (intersection by room_id, not one player's full list)."""
    a = (guest_id_a or "").strip()
    b = (guest_id_b or "").strip()
    if not a or not b or a == b:
        return []

    by_room: dict[str, dict] = {}
    for m in get_user_matches(a, limit=limit):
        by_room[m["room_id"]] = m

    shared: list[dict] = []
    seen_rooms: set[str] = set()
    for m in get_user_matches(b, limit=limit):
        rid = m["room_id"]
        if rid in seen_rooms or rid not in by_room:
            continue
        match = by_room[rid]
        gids = {p.get("guest_id") for p in match.get("players") or []}
        if a in gids and b in gids:
            seen_rooms.add(rid)
            shared.append(match)

    shared.sort(key=lambda x: x.get("id", 0), reverse=True)
    return shared[:limit]
