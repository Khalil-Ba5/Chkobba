"""services/matchmaking.py — In-memory matchmaking queue.

Follows the same "dev-friendly, no external deps" philosophy as MemoryGameStore.
A Redis-backed implementation can be added later following the same interface.

Queue entry shape:
    {
        "guest_id":    str,
        "display_name": str,
        "room_id":     str,      # pre-created waiting room
        "enqueued_at": float,    # time.time()
    }
"""

from __future__ import annotations

import threading
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

QUEUE_TTL_SECONDS: float = 60.0


class MatchmakingQueue:
    """Thread-safe FIFO matchmaking queue (in-process, no external deps)."""

    def __init__(self) -> None:
        self._queue: list[dict] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(self, guest_id: str, display_name: str, room_id: str) -> None:
        """Add or update a player's entry in the queue."""
        with self._lock:
            # Remove any stale entry for the same guest first.
            self._queue = [e for e in self._queue if e["guest_id"] != guest_id]
            self._queue.append(
                {
                    "guest_id": guest_id,
                    "display_name": display_name,
                    "room_id": room_id,
                    "enqueued_at": time.time(),
                }
            )
        logger.info("[matchmaking] enqueued guest %.8s (queue len=%d)", guest_id, len(self._queue))

    def pop_opponent(self, exclude_guest_id: str = "") -> Optional[dict]:
        """Remove and return the oldest entry that isn't *exclude_guest_id*.

        Stale entries (> QUEUE_TTL_SECONDS) are discarded.  Returns None if
        no suitable opponent is waiting.
        """
        with self._lock:
            now = time.time()
            # Purge stale
            self._queue = [
                e for e in self._queue if now - e["enqueued_at"] < QUEUE_TTL_SECONDS
            ]
            for i, entry in enumerate(self._queue):
                if entry["guest_id"] != exclude_guest_id:
                    return self._queue.pop(i)
        return None

    def cancel(self, guest_id: str) -> None:
        """Remove a specific player from the queue."""
        with self._lock:
            before = len(self._queue)
            self._queue = [e for e in self._queue if e["guest_id"] != guest_id]
        logger.info(
            "[matchmaking] cancelled guest %.8s (removed=%s)", guest_id, len(self._queue) < before
        )

    def is_queued(self, guest_id: str) -> bool:
        with self._lock:
            now = time.time()
            return any(
                e["guest_id"] == guest_id
                and now - e["enqueued_at"] < QUEUE_TTL_SECONDS
                for e in self._queue
            )

    def cleanup_stale(self) -> list[str]:
        """Remove entries older than QUEUE_TTL_SECONDS. Returns removed guest_ids."""
        with self._lock:
            now = time.time()
            stale = [e["guest_id"] for e in self._queue if now - e["enqueued_at"] >= QUEUE_TTL_SECONDS]
            self._queue = [e for e in self._queue if now - e["enqueued_at"] < QUEUE_TTL_SECONDS]
        if stale:
            logger.info("[matchmaking] cleaned up %d stale entries: %s", len(stale), stale)
        return stale

    def snapshot(self) -> list[dict]:
        """Return a copy of the current queue (for introspection/admin)."""
        with self._lock:
            now = time.time()
            return [
                {**e, "waiting_for": round(now - e["enqueued_at"], 1)}
                for e in self._queue
                if now - e["enqueued_at"] < QUEUE_TTL_SECONDS
            ]


def get_matchmaking_queue() -> MatchmakingQueue:
    """Factory — returns a shared in-memory queue.

    Called once at app init and stored on app.matchmaking_queue.
    A future Redis implementation would return a RedisMatchmakingQueue here.
    """
    return MatchmakingQueue()
