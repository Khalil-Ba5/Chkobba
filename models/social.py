"""Head-to-head stats and guest friends."""

from __future__ import annotations

import logging
from typing import Any

from .db import get_connection, get_matches_between_players

logger = logging.getLogger(__name__)

_BOT_GUEST_IDS = frozenset({"bot", ""})


def _is_human_guest_id(guest_id: str | None) -> bool:
    return bool(guest_id) and guest_id not in _BOT_GUEST_IDS


def _seat_for_guest(players: list[dict], guest_id: str) -> int | None:
    for p in players:
        if p.get("guest_id") == guest_id:
            return p.get("seat", 0)
    return None


def get_head_to_head_stats(
    viewer_guest_id: str,
    opponent_guest_id: str,
    *,
    recent_limit: int = 5,
    scan_limit: int = 150,
) -> dict[str, Any]:
    """Matches played together (1v1 human vs human) from *viewer*'s perspective."""
    viewer = (viewer_guest_id or "").strip()
    opponent = (opponent_guest_id or "").strip()
    if not _is_human_guest_id(viewer) or not _is_human_guest_id(opponent):
        return _empty_h2h(recent_limit)
    if viewer == opponent:
        return _empty_h2h(recent_limit)

    matches = get_matches_between_players(viewer, opponent, limit=scan_limit)
    all_h2h: list[dict[str, Any]] = []

    for m in matches:
        if m.get("mode") != "1v1":
            continue
        players = m.get("players") or []
        if any(p.get("is_bot") for p in players):
            continue
        if any((p.get("guest_id") or "") in _BOT_GUEST_IDS for p in players):
            continue

        viewer_seat = _seat_for_guest(players, viewer)
        opponent_seat = _seat_for_guest(players, opponent)
        if viewer_seat is None or opponent_seat is None:
            continue

        scores = m.get("scores") or []
        my_score = scores[viewer_seat] if len(scores) > viewer_seat else None
        opp_score = scores[opponent_seat] if len(scores) > opponent_seat else None
        winner = m.get("winner")
        if winner is None:
            result = "draw"
        elif winner == viewer:
            result = "won"
        elif winner == opponent:
            result = "lost"
        else:
            result = "draw"

        all_h2h.append({
            "result": result,
            "my_score": my_score,
            "opp_score": opp_score,
            "ended_at": m.get("ended_at"),
            "was_forfeit": bool(m.get("was_forfeit")),
        })

    wins = sum(1 for x in all_h2h if x["result"] == "won")
    losses = sum(1 for x in all_h2h if x["result"] == "lost")
    draws = sum(1 for x in all_h2h if x["result"] == "draw")
    decided = wins + losses
    win_rate = round(wins / decided, 3) if decided else None

    return {
        "total": len(all_h2h),
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "win_rate": win_rate,
        "win_rate_pct": int(round(win_rate * 100)) if win_rate is not None else None,
        "recent": all_h2h[:recent_limit],
    }


def _empty_h2h(recent_limit: int) -> dict[str, Any]:
    return {
        "total": 0,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "win_rate": None,
        "win_rate_pct": None,
        "recent": [],
    }


def is_friend(guest_id: str, friend_guest_id: str) -> bool:
    """True if either guest has added the other (mutual on accept)."""
    if guest_id == friend_guest_id:
        return False
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT 1 FROM guest_friends
            WHERE (guest_id = ? AND friend_guest_id = ?)
               OR (guest_id = ? AND friend_guest_id = ?)
            """,
            (guest_id, friend_guest_id, friend_guest_id, guest_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _add_friend_row(guest_id: str, friend_guest_id: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO guest_friends (guest_id, friend_guest_id, created_at)
            VALUES (?, ?, datetime('now'))
            """,
            (guest_id, friend_guest_id),
        )
        conn.commit()
    finally:
        conn.close()


def add_mutual_friends(guest_id: str, friend_guest_id: str) -> bool:
    if guest_id == friend_guest_id:
        return False
    if not _is_human_guest_id(guest_id) or not _is_human_guest_id(friend_guest_id):
        return False
    _add_friend_row(guest_id, friend_guest_id)
    _add_friend_row(friend_guest_id, guest_id)
    return True


def sync_friendships_for_guest(guest_id: str) -> None:
    """Ensure guest_friends rows exist for accepted requests (repairs one-way rows)."""
    gid = (guest_id or "").strip()
    if not _is_human_guest_id(gid):
        return
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT from_guest_id, to_guest_id
            FROM guest_friend_requests
            WHERE status = 'accepted'
              AND (from_guest_id = ? OR to_guest_id = ?)
            """,
            (gid, gid),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        add_mutual_friends(row["from_guest_id"], row["to_guest_id"])


def remove_friend(guest_id: str, friend_guest_id: str) -> bool:
    """Remove friendship in both directions."""
    conn = get_connection()
    try:
        conn.execute(
            """
            DELETE FROM guest_friends
            WHERE (guest_id = ? AND friend_guest_id = ?)
               OR (guest_id = ? AND friend_guest_id = ?)
            """,
            (guest_id, friend_guest_id, friend_guest_id, guest_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_friend_relation(viewer_id: str, other_id: str) -> dict[str, Any]:
    """
    Relationship between two guests for UI.

    Returns dict with:
      status: none | friends | pending_sent | pending_received | declined
      request_id: int | None (incoming pending request to respond to)
    """
    viewer = (viewer_id or "").strip()
    other = (other_id or "").strip()
    if not _is_human_guest_id(viewer) or not _is_human_guest_id(other) or viewer == other:
        return {"status": "none", "request_id": None}

    if is_friend(viewer, other):
        return {"status": "friends", "request_id": None}

    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, from_guest_id, to_guest_id, status
            FROM guest_friend_requests
            WHERE (from_guest_id = ? AND to_guest_id = ?)
               OR (from_guest_id = ? AND to_guest_id = ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (viewer, other, other, viewer),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return {"status": "none", "request_id": None}

    if row["status"] == "pending":
        if row["from_guest_id"] == viewer:
            return {"status": "pending_sent", "request_id": row["id"]}
        return {"status": "pending_received", "request_id": row["id"]}

    if row["status"] == "declined":
        if row["from_guest_id"] == viewer:
            return {"status": "declined", "request_id": None}
        return {"status": "none", "request_id": None}

    return {"status": "none", "request_id": None}


def send_friend_request(from_guest_id: str, to_guest_id: str) -> dict[str, Any]:
    """
    Send or re-open a friend request.

    Returns {"ok": True, "request_id": int} or {"ok": False, "error": str}.
    """
    from_id = (from_guest_id or "").strip()
    to_id = (to_guest_id or "").strip()
    if not _is_human_guest_id(from_id) or not _is_human_guest_id(to_id):
        return {"ok": False, "error": "Invalid player"}
    if from_id == to_id:
        return {"ok": False, "error": "Cannot add yourself"}

    rel = get_friend_relation(from_id, to_id)
    if rel["status"] == "friends":
        return {"ok": False, "error": "Already friends"}
    if rel["status"] == "pending_sent":
        return {"ok": False, "error": "Request already sent"}
    if rel["status"] == "pending_received":
        return {"ok": False, "error": "They already sent you a request — accept it instead"}

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        existing = conn.execute(
            """
            SELECT id, status FROM guest_friend_requests
            WHERE from_guest_id = ? AND to_guest_id = ?
            """,
            (from_id, to_id),
        ).fetchone()
        if existing:
            if existing["status"] == "pending":
                return {"ok": True, "request_id": existing["id"], "already_pending": True}
            conn.execute(
                """
                UPDATE guest_friend_requests
                SET status = 'pending', created_at = ?, responded_at = NULL
                WHERE id = ?
                """,
                (now, existing["id"]),
            )
            conn.commit()
            return {"ok": True, "request_id": existing["id"], "already_pending": False}

        cur = conn.execute(
            """
            INSERT INTO guest_friend_requests
                (from_guest_id, to_guest_id, status, created_at)
            VALUES (?, ?, 'pending', ?)
            """,
            (from_id, to_id, now),
        )
        conn.commit()
        return {"ok": True, "request_id": cur.lastrowid, "already_pending": False}
    finally:
        conn.close()


def respond_friend_request(
    request_id: int,
    responder_guest_id: str,
    *,
    accept: bool,
) -> dict[str, Any]:
    """Accept or decline an incoming friend request."""
    responder = (responder_guest_id or "").strip()
    if not _is_human_guest_id(responder):
        return {"ok": False, "error": "Invalid session"}

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, from_guest_id, to_guest_id, status
            FROM guest_friend_requests
            WHERE id = ?
            """,
            (request_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "Request not found"}
        if row["to_guest_id"] != responder:
            return {"ok": False, "error": "Not authorized"}
        if row["status"] != "pending":
            return {"ok": False, "error": "Request already handled"}

        from_id = row["from_guest_id"]
        to_id = row["to_guest_id"]
        new_status = "accepted" if accept else "declined"

        if accept:
            conn.execute(
                """
                INSERT OR IGNORE INTO guest_friends (guest_id, friend_guest_id, created_at)
                VALUES (?, ?, ?)
                """,
                (from_id, to_id, now),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO guest_friends (guest_id, friend_guest_id, created_at)
                VALUES (?, ?, ?)
                """,
                (to_id, from_id, now),
            )

        conn.execute(
            """
            UPDATE guest_friend_requests
            SET status = ?, responded_at = ?
            WHERE id = ?
            """,
            (new_status, now, request_id),
        )
        conn.commit()

        return {
            "ok": True,
            "accept": accept,
            "from_guest_id": from_id,
            "to_guest_id": to_id,
        }
    finally:
        conn.close()


def list_friend_ids(guest_id: str) -> list[str]:
    """Return friend guest ids for this player (deduped)."""
    gid = (guest_id or "").strip()
    if not _is_human_guest_id(gid):
        return []
    sync_friendships_for_guest(gid)
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT friend_guest_id AS fid FROM guest_friends WHERE guest_id = ?
            UNION
            SELECT guest_id AS fid FROM guest_friends WHERE friend_guest_id = ?
            """,
            (gid, gid),
        ).fetchall()
    finally:
        conn.close()
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        fid = (r["fid"] or "").strip()
        if fid and fid not in seen and fid != gid:
            seen.add(fid)
            out.append(fid)
    return out


def list_incoming_friend_requests(guest_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Pending friend requests addressed to this guest."""
    gid = (guest_id or "").strip()
    if not _is_human_guest_id(gid):
        return []

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, from_guest_id, created_at
            FROM guest_friend_requests
            WHERE to_guest_id = ? AND status = 'pending'
            ORDER BY id DESC
            LIMIT ?
            """,
            (gid, limit),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "request_id": r["id"],
            "from_guest_id": r["from_guest_id"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def list_outgoing_friend_requests(guest_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Pending friend requests sent by this guest."""
    gid = (guest_id or "").strip()
    if not _is_human_guest_id(gid):
        return []

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, to_guest_id, created_at
            FROM guest_friend_requests
            WHERE from_guest_id = ? AND status = 'pending'
            ORDER BY id DESC
            LIMIT ?
            """,
            (gid, limit),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "request_id": r["id"],
            "to_guest_id": r["to_guest_id"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
