"""
services/game_store.py — Room-state storage abstraction.

Public surface
--------------
    GameStore       — abstract base; both backends implement this.
    MemoryGameStore — in-process dict + threading.Lock (dev / single-worker).
    RedisGameStore  — redis.Redis backend (production / multi-worker).
    get_game_store()— factory; reads REDIS_URL env var, returns the right impl.

Each room is stored as a single JSON blob under the key  game:<room_id>.
The blob shape is:

    {
        "room_id":        str,
        "mode":           "solo" | "1v1" | "2v2",
        "status":         "waiting" | "active" | "round_over" | "match_over",
        "created_at":     ISO-8601 str,
        "last_action_at": ISO-8601 str,
        "players": [
            {"guest_id": str, "display_name": str, "seat": int,
             "is_bot": bool, "connected": bool, "sid": str | None}
        ],
        "game": { ... all GameManager serialisable fields ... }
    }

Callers never reach into the blob shape — they treat it as an opaque dict.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger(__name__)

_KEY_PREFIX = "game:"


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class GameStore(ABC):

    @abstractmethod
    def get(self, room_id: str) -> Optional[dict]:
        """Return the room blob, or None if it does not exist."""

    @abstractmethod
    def set(self, room_id: str, state: dict, ttl_seconds: int = 86400) -> None:
        """Persist the room blob.  ttl_seconds is honoured only by Redis."""

    @abstractmethod
    def delete(self, room_id: str) -> None:
        """Remove the room from the store (idempotent)."""

    @abstractmethod
    def list_active(self) -> list[str]:
        """Return a list of all currently-stored room_ids."""

    @abstractmethod
    def exists(self, room_id: str) -> bool:
        """Return True if the room is in the store."""


# ---------------------------------------------------------------------------
# In-memory backend  (dev / single worker)
# ---------------------------------------------------------------------------

class MemoryGameStore(GameStore):
    """Backed by a plain dict; lives for the duration of the server process.

    TTL is not enforced — entries survive until the process restarts or the
    room is explicitly deleted.  That is fine for development.
    """

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, room_id: str) -> Optional[dict]:
        with self._lock:
            return self._store.get(room_id)

    def set(self, room_id: str, state: dict, ttl_seconds: int = 86400) -> None:
        with self._lock:
            self._store[room_id] = state

    def delete(self, room_id: str) -> None:
        with self._lock:
            self._store.pop(room_id, None)

    def list_active(self) -> list[str]:
        with self._lock:
            return list(self._store.keys())

    def exists(self, room_id: str) -> bool:
        with self._lock:
            return room_id in self._store


# ---------------------------------------------------------------------------
# Redis backend  (production / multi-worker)
# ---------------------------------------------------------------------------

class RedisGameStore(GameStore):
    """Backed by a Redis instance.

    Each room is stored as a JSON string at key  game:<room_id>  with a TTL
    so abandoned games are automatically evicted.

    list_active() uses SCAN (not KEYS) to avoid blocking Redis on large
    key-spaces.
    """

    def __init__(self, redis_url: str) -> None:
        import redis as _redis
        self._r = _redis.Redis.from_url(redis_url, decode_responses=True)

    def _key(self, room_id: str) -> str:
        return f"{_KEY_PREFIX}{room_id}"

    def get(self, room_id: str) -> Optional[dict]:
        raw = self._r.get(self._key(room_id))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.error("Corrupt blob for room %s", room_id)
            return None

    def set(self, room_id: str, state: dict, ttl_seconds: int = 86400) -> None:
        self._r.setex(self._key(room_id), ttl_seconds, json.dumps(state))

    def delete(self, room_id: str) -> None:
        self._r.delete(self._key(room_id))

    def list_active(self) -> list[str]:
        prefix_len = len(_KEY_PREFIX)
        room_ids: list[str] = []
        cursor = 0
        while True:
            cursor, keys = self._r.scan(cursor, match=f"{_KEY_PREFIX}*", count=100)
            room_ids.extend(k[prefix_len:] for k in keys)
            if cursor == 0:
                break
        return room_ids

    def exists(self, room_id: str) -> bool:
        return bool(self._r.exists(self._key(room_id)))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_game_store() -> GameStore:
    """Return the appropriate backend based on environment.

    If REDIS_URL is set, a RedisGameStore is returned.
    Otherwise, a MemoryGameStore is returned (zero external dependencies).

    Call this once at app startup and attach the result to the Flask app:
        app.game_store = get_game_store()
    """
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        logger.info("[game_store] Using Redis backend (REDIS_URL set)")
        return RedisGameStore(redis_url)
    logger.info("[game_store] Using in-memory backend (dev mode, no Redis)")
    return MemoryGameStore()
