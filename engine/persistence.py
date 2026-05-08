"""Database persistence layer for Chkobba game history and saved sessions."""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "chkobba_games.db"
SCHEMA_VERSION = 2


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_database() -> None:
    """Initialize database schema and lightweight migrations."""
    conn = _connect()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            target_score INTEGER NOT NULL,
            human_score INTEGER NOT NULL,
            bot_score INTEGER NOT NULL,
            winner INTEGER,
            duration_seconds INTEGER,
            notes TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            round_number INTEGER NOT NULL,
            human_points INTEGER NOT NULL,
            bot_points INTEGER NOT NULL,
            FOREIGN KEY (match_id) REFERENCES matches(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_key TEXT PRIMARY KEY,
            updated_at TEXT NOT NULL,
            data_json TEXT NOT NULL
        )
    """)
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
    sessions_exists = cursor.fetchone() is not None
    cursor.execute("PRAGMA table_info(sessions)")
    session_cols = {row[1] for row in cursor.fetchall()}
    if sessions_exists and "session_key" not in session_cols:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions_legacy'")
        if cursor.fetchone() is None:
            cursor.execute("ALTER TABLE sessions RENAME TO sessions_legacy")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_key TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL,
                data_json TEXT NOT NULL
            )
        """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    cursor.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_matches_created_at ON matches(created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rounds_match_id ON rounds(match_id)")

    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", DB_PATH)


def save_match(target_score: int, human_score: int, bot_score: int, 
               winner: Optional[int], duration_seconds: int, 
               round_scores: List[tuple]) -> int:
    """Save a completed match to the database.
    
    Args:
        target_score: Target score for the match
        human_score: Final human score
        bot_score: Final bot score
        winner: 0 (human), 1 (bot), or None (tie)
        duration_seconds: Total duration of match
        round_scores: List of (human_points, bot_points) tuples
        
    Returns:
        The match ID inserted
    """
    try:
        conn = _connect()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO matches (created_at, target_score, human_score, bot_score, winner, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(), target_score, human_score, bot_score, winner, duration_seconds))

        match_id = cursor.lastrowid

        # Save round scores
        for round_num, (human_pts, bot_pts) in enumerate(round_scores, 1):
            cursor.execute("""
                INSERT INTO rounds (match_id, round_number, human_points, bot_points)
                VALUES (?, ?, ?, ?)
            """, (match_id, round_num, human_pts, bot_pts))
        
        conn.commit()
        conn.close()
        logger.info("Saved match %d to database", match_id)
        return match_id
        
    except Exception as e:
        logger.error("Error saving match: %s", e)
        raise


def get_match_history(limit: int = 10) -> List[Dict]:
    """Get recent match history from the database.
    
    Args:
        limit: Maximum number of matches to retrieve
        
    Returns:
        List of match dictionaries
    """
    try:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM matches
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,))

        matches = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        return matches
        
    except Exception as e:
        logger.error("Error retrieving match history: %s", e)
        return []


def get_statistics() -> Dict:
    """Get overall game statistics.
    
    Returns:
        Dictionary with statistics
    """
    try:
        conn = _connect()
        cursor = conn.cursor()

        # Total matches
        cursor.execute("SELECT COUNT(*) FROM matches")
        total_matches = cursor.fetchone()[0]
        
        # Win/loss record
        cursor.execute("SELECT COUNT(*) FROM matches WHERE winner = 0")
        human_wins = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM matches WHERE winner = 1")
        bot_wins = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM matches WHERE winner IS NULL")
        ties = cursor.fetchone()[0]
        
        # Average match score
        cursor.execute("""
            SELECT AVG(human_score), AVG(bot_score), AVG(target_score)
            FROM matches
        """)
        avg_human, avg_bot, avg_target = cursor.fetchone()
        
        conn.close()
        
        stats = {
            "total_matches": total_matches,
            "human_wins": human_wins,
            "bot_wins": bot_wins,
            "ties": ties,
            "win_rate": human_wins / total_matches if total_matches > 0 else 0,
            "average_human_score": avg_human or 0,
            "average_bot_score": avg_bot or 0,
            "average_target_score": avg_target or 0,
        }
        
        return stats
        
    except Exception as e:
        logger.error("Error retrieving statistics: %s", e)
        return {}


def clear_history() -> None:
    """Clear all game history from the database (careful!)."""
    try:
        conn = _connect()
        cursor = conn.cursor()

        cursor.execute("DELETE FROM rounds")
        cursor.execute("DELETE FROM matches")

        conn.commit()
        conn.close()
        logger.warning("Cleared all game history")

    except Exception as e:
        logger.error("Error clearing history: %s", e)
        raise


def save_session(session_key: str, data: Dict[str, Any]) -> None:
    """Save (or replace) one in-progress session by key."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR REPLACE INTO sessions (session_key, updated_at, data_json)
        VALUES (?, ?, ?)
        """,
        (session_key, datetime.now().isoformat(), json.dumps(data)),
    )
    conn.commit()
    conn.close()


def load_session(session_key: str) -> Optional[Dict[str, Any]]:
    """Load saved in-progress session for the given key, if present."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT data_json FROM sessions WHERE session_key = ?", (session_key,))
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    return json.loads(row["data_json"])


def clear_session(session_key: Optional[str] = None) -> None:
    """Delete saved in-progress session for the given key."""
    if session_key is None:
        logger.debug("clear_session called without session_key; nothing to delete")
        return
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sessions WHERE session_key = ?", (session_key,))
    conn.commit()
    conn.close()
