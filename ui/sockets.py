"""ui/sockets.py — Flask-SocketIO event handlers for Chkobba.

Imported at the bottom of ui/app.py (after all symbols are defined) to avoid
circular imports.

Per-process state
-----------------
_sid_to_room  : sid → room_id
_sid_to_seat  : sid → seat index (0 or 1; 0 for all solo clients)
_room_to_sids : room_id → {seat → sid}  (for private multiplayer snapshots)
_room_seqs    : room_id → monotonic sequence counter

Phase 4-Break2 additions
------------------------
* join_lobby / leave_lobby   — subscribe to real-time lobby_update events
* cancel_matchmaking         — remove player from matchmaking queue
* on_game_join updated       — tracks seat, handles multiplayer start
* on_play_card updated       — seat-aware, sends private state snapshots in 1v1
* _start_mp_game             — fires when both players are connected
* _emit_private_snapshots    — sends per-seat state to each player's sid
"""

from __future__ import annotations

import logging
import os
import random

from flask import session, request
from flask_socketio import emit, join_room, leave_room

from ui.app import (
    app,
    socketio,
    GameManager,
    RoomNotFoundError,
    GamePhase,
    card_from_data,
)
from engine.utils import card_to_str
from services.names import avatar_color
from models.db import record_match

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-process bookkeeping
# ---------------------------------------------------------------------------

_sid_to_room:      dict[str, str | None]         = {}
_sid_to_seat:      dict[str, int]                = {}   # seat per connected client
_room_to_sids:     dict[str, dict[int, str]]     = {}   # room_id → {seat → sid}
_room_seqs:        dict[str, int]                = {}
# Active disconnection timers: room_id → guest_id being waited on.
# A background task checks this before replacing the player with a bot.
# Setting it to "" cancels the pending replacement.
_disconnect_tasks: dict[str, str]                = {}

BOT_THINKING_DELAY: float = (
    float(os.environ.get("BOT_THINKING_DELAY_MS", "800")) / 1000.0
)
# Solo: first bot move after opening deal — lets the client show table → deal → pause
# before the server emits the pending-card snapshot (aligns with index.html UX).
SOLO_OPENING_BOT_DELAY_S: float = float(os.environ.get("SOLO_OPENING_BOT_DELAY_S", "2.2"))


def _next_seq(room_id: str) -> int:
    _room_seqs[room_id] = _room_seqs.get(room_id, 0) + 1
    return _room_seqs[room_id]


def _seat_to_sid(room_id: str, seat: int, blob: dict | None = None) -> str | None:
    """Return the active socket SID for a seat.

    Primary source is the in-process map (fast path).  When players connect on
    different workers, fall back to the shared room blob's player.sid field so
    private emits still reach both participants.
    """
    sid = _room_to_sids.get(room_id, {}).get(seat)
    if sid:
        return sid

    if blob is None:
        blob = app.game_store.get(room_id)
    if not blob:
        return None

    for p in blob.get("players", []):
        if p.get("seat") == seat and p.get("sid"):
            sid = str(p["sid"])
            _room_to_sids.setdefault(room_id, {})[seat] = sid
            return sid
    return None


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


@socketio.on("connect")
def on_connect():
    guest_id = session.get("guest_id", "unknown")
    _sid_to_room[request.sid] = None
    _sid_to_seat[request.sid] = 0
    logger.info("[socket] connect  sid=%.8s  guest=%s", request.sid, guest_id)


@socketio.on("disconnect")
def on_disconnect():
    sid     = request.sid
    room_id = _sid_to_room.pop(sid, None)
    seat    = _sid_to_seat.pop(sid, 0)

    # Remove from room-to-sids mapping.
    if room_id and room_id in _room_to_sids:
        _room_to_sids[room_id].pop(seat, None)
        if not _room_to_sids[room_id]:
            del _room_to_sids[room_id]

    if room_id:
        blob = app.game_store.get(room_id)
        if blob and blob.get("mode") == "1v1":
            disc_player: dict | None = None
            for p in blob.get("players", []):
                if p.get("sid") == sid:
                    p["connected"] = False
                    p["sid"]       = None
                    disc_player    = p
                    break

            if blob.get("status") == "active" and disc_player:
                # Active 1v1 game — start the 30-second reconnect window.
                from datetime import datetime, timezone, timedelta
                deadline = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
                blob["disconnect"] = {
                    "seat":     seat,
                    "guest_id": disc_player.get("guest_id", ""),
                    "deadline": deadline,
                }
                app.game_store.set(room_id, blob)

                display_name = disc_player.get("display_name", "Opponent")
                _disconnect_tasks[room_id] = disc_player.get("guest_id", "")

                socketio.emit(
                    "player_disconnected",
                    {
                        "seat":         seat,
                        "display_name": display_name,
                        "deadline_secs": 30,
                        "seq":          _next_seq(room_id),
                    },
                    to=room_id,
                )
                socketio.start_background_task(
                    _reconnect_timeout, room_id, seat,
                    disc_player.get("guest_id", ""), app.game_store,
                )
            else:
                app.game_store.set(room_id, blob)

    logger.info("[socket] disconnect  sid=%.8s  room=%s  seat=%s", sid, room_id, seat)


# ---------------------------------------------------------------------------
# Lobby subscription
# ---------------------------------------------------------------------------


@socketio.on("join_lobby")
def on_join_lobby(_data=None):
    join_room("lobby")
    logger.info("[socket] join_lobby  sid=%.8s", request.sid)


@socketio.on("leave_lobby")
def on_leave_lobby(_data=None):
    leave_room("lobby")


# ---------------------------------------------------------------------------
# Matchmaking
# ---------------------------------------------------------------------------


@socketio.on("cancel_matchmaking")
def on_cancel_matchmaking(_data=None):
    guest_id = session.get("guest_id", "")
    if guest_id:
        app.matchmaking_queue.cancel(guest_id)
    logger.info("[socket] cancel_matchmaking  guest=%.8s", guest_id)


# ---------------------------------------------------------------------------
# Waiting-room chat
# ---------------------------------------------------------------------------


@socketio.on("room_chat")
def on_room_chat(data: dict):
    """A player sends a chat message in the waiting room (or in-game)."""
    data = data or {}
    room_id = data.get("room_id") or _sid_to_room.get(request.sid)
    if not room_id:
        return

    raw_msg = str(data.get("message", "")).strip()[:200]
    if not raw_msg:
        return

    display_name = session.get("display_name", "Guest")

    blob = app.game_store.get(room_id)
    if blob is None:
        return

    from datetime import datetime, timezone
    entry = {
        "display_name": display_name,
        "message": raw_msg,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    chat = blob.setdefault("chat", [])
    chat.append(entry)
    if len(chat) > 50:
        blob["chat"] = chat[-50:]
    app.game_store.set(room_id, blob)

    socketio.emit(
        "chat_message",
        {"display_name": display_name, "message": raw_msg},
        to=room_id,
    )
    logger.debug("[chat] room=%.8s  %s: %s", room_id, display_name, raw_msg[:40])


# ---------------------------------------------------------------------------
# Rematch
# ---------------------------------------------------------------------------


@socketio.on("rematch_offer")
def on_rematch_offer(data: dict):
    """Player who just finished a match offers a rematch."""
    data = data or {}
    old_room_id = data.get("room_id") or _sid_to_room.get(request.sid)
    if not old_room_id:
        return

    old_blob = app.game_store.get(old_room_id)
    if old_blob is None:
        emit("error", {"message": "Original room not found"}); return
    if old_blob.get("status") not in ("match_over", "active"):
        emit("error", {"message": "Match is not over yet"}); return

    guest_id     = session.get("guest_id", "")
    display_name = session.get("display_name", "Guest")

    # Create a fresh room with the same two players (swapped seats for fairness).
    old_players = old_blob.get("players", [])
    target_score = old_blob.get("target_score_mp", 11)
    mode         = old_blob.get("mode", "1v1")

    new_room_id = _new_room_id()
    new_players = []
    for p in old_players:
        new_players.append({
            "guest_id":    p.get("guest_id", ""),
            "display_name": p.get("display_name", "?"),
            "seat":        1 - p.get("seat", 0),   # swap seats
            "is_bot":      p.get("is_bot", False),
            "connected":   False,
            "sid":         None,
        })
    new_players.sort(key=lambda p: p["seat"])

    from datetime import datetime, timezone
    new_blob = {
        "room_id":       new_room_id,
        "mode":          mode,
        "status":        "waiting",
        "visibility":    "private",
        "created_by":    guest_id,
        "target_score_mp": target_score,
        "created_at":    datetime.now(timezone.utc).isoformat(),
        "last_action_at": datetime.now(timezone.utc).isoformat(),
        "players":       new_players,
        "chat":          [],
    }
    app.game_store.set(new_room_id, new_blob)

    # Notify the opponent.
    socketio.emit(
        "rematch_offered",
        {
            "from_player":  display_name,
            "new_room_id":  new_room_id,
            "old_room_id":  old_room_id,
        },
        to=old_room_id,
        skip_sid=request.sid,   # don't send back to the offerer
    )
    # Confirm to the offerer.
    emit("rematch_offered_sent", {"new_room_id": new_room_id})
    logger.info("[rematch] room=%.8s → new=%.8s  by=%s", old_room_id, new_room_id, display_name)


@socketio.on("rematch_response")
def on_rematch_response(data: dict):
    """Opponent responds to a rematch offer."""
    data     = data or {}
    room_id  = data.get("room_id")   # the NEW rematch room
    accepted = bool(data.get("accepted"))
    guest_id = session.get("guest_id", "")

    if not room_id:
        return

    if accepted:
        # Redirect both players to the new room.
        socketio.emit("rematch_accepted", {"room_id": room_id}, to=room_id)
    else:
        socketio.emit("rematch_declined", {}, to=room_id)
        # Clean up the rematch room since it won't be used.
        app.game_store.delete(room_id)

    logger.info("[rematch] response  room=%.8s  accepted=%s  guest=%.8s",
                room_id, accepted, guest_id)


def _new_room_id() -> str:
    """Generate a short random room ID (collision probability negligible at this scale)."""
    import secrets
    return secrets.token_urlsafe(9)   # 12-char URL-safe string


# ---------------------------------------------------------------------------
# Game lifecycle
# ---------------------------------------------------------------------------


@socketio.on("game_join")
def on_game_join(data: dict):
    data = data or {}
    room_id = data.get("room_id") or session.get("solo_room_id")
    if not room_id:
        emit("error", {"message": "No room ID — start a new game"})
        return

    blob = app.game_store.get(room_id)
    if blob is None:
        emit("error", {"message": "Room not found — start a new game"})
        return

    is_mp = (blob.get("mode") == "1v1")
    guest_id = session.get("guest_id", "")

    # Determine this client's seat.
    my_seat = 0
    if is_mp:
        seat_found = False
        for p in blob.get("players", []):
            if p.get("guest_id") == guest_id:
                my_seat = p["seat"]
                seat_found = True
                break
        if not seat_found:
            emit("error", {"message": "You are not a player in this room"})
            logger.warning(
                "[socket] game_join rejected  room=%.8s  guest=%.8s  reason=guest-not-in-room",
                room_id,
                guest_id,
            )
            return

    # Update connection bookkeeping.
    _sid_to_room[request.sid]  = room_id
    _sid_to_seat[request.sid]  = my_seat
    _room_to_sids.setdefault(room_id, {})[my_seat] = request.sid

    join_room(room_id)

    logger.info(
        "[socket] game_join  room=%.8s  mode=%s  seat=%s",
        room_id, blob.get("mode"), my_seat,
    )

    # ── Multiplayer ────────────────────────────────────────────────────────
    if is_mp:
        # Update only this player's presence in the latest blob snapshot.
        # This avoids clobbering another worker's concurrent join write.
        latest_blob = app.game_store.get(room_id) or blob
        for p in latest_blob.get("players", []):
            if p.get("guest_id") == guest_id:
                p["connected"] = True
                p["sid"] = request.sid
                break
        app.game_store.set(room_id, latest_blob)

        # Re-fetch (in case another worker modified it between our read and write).
        blob = app.game_store.get(room_id)

        all_present   = len(blob.get("players", [])) == 2
        all_connected = all(p.get("connected", False) for p in blob.get("players", []))

        if blob.get("status") == "active":
            # ── Late reconnect: player was already replaced by the bot ────
            if blob.get("bot_replacement_seat") == my_seat:
                emit("you_were_replaced", {
                    "message": "You were replaced by a bot — the match continued without you.",
                })
                return

            # ── In-time reconnect: player returns within the 30-second window ─
            disc = blob.get("disconnect") or {}
            if disc.get("guest_id") == guest_id and disc.get("seat") == my_seat:
                # Clear the disconnect record and cancel the pending timeout.
                blob.pop("disconnect", None)
                for p in blob.get("players", []):
                    if p.get("guest_id") == guest_id:
                        p["connected"] = True
                        p["sid"]       = request.sid
                        break
                app.game_store.set(room_id, blob)
                _disconnect_tasks.pop(room_id, None)   # signal task to bail

                reconnect_name = ""
                for p in blob.get("players", []):
                    if p.get("guest_id") == guest_id:
                        reconnect_name = p.get("display_name", "Player")
                        break
                socketio.emit(
                    "player_reconnected",
                    {
                        "seat":         my_seat,
                        "display_name": reconnect_name,
                        "seq":          _next_seq(room_id),
                    },
                    to=room_id,
                )

            # Send the game state (both normal reconnect and mid-timeout rejoin).
            try:
                manager = GameManager.load(room_id, app.game_store)
                vd = manager.view_data(viewer_seat=my_seat)
                vd["seq"] = _room_seqs.get(room_id, 0)
                emit("state_snapshot", vd)
            except RoomNotFoundError:
                emit("error", {"message": "Game state missing"})
            return

        if blob.get("status") == "starting":
            # Countdown is already in progress — send a fast-forward match_starting
            # (countdown=0) to this client so it skips straight to the game reveal.
            emit(
                "match_starting",
                {
                    "room_id":      room_id,
                    "countdown":    0,
                    "player_names": [p.get("display_name", "?") for p in blob.get("players", [])],
                },
            )
            return

        if all_present and all_connected:
            # Both players just connected — kick off countdown + game creation.
            socketio.start_background_task(_start_mp_game, room_id, app.game_store)
        elif all_present:
            # Two players in the blob but one isn't connected via socket yet.
            # Tell the joining player to keep waiting; game will auto-start when
            # the other player's socket also fires game_join.
            emit(
                "waiting_state",
                {
                    "players": blob.get("players", []),
                    "status": "waiting",
                    "room_id": room_id,
                },
            )
        else:
            # Still waiting for the second player to claim a seat.
            emit(
                "waiting_state",
                {
                    "players": blob.get("players", []),
                    "status": "waiting",
                    "room_id": room_id,
                },
            )
        return

    # ── Solo (legacy index.html path) ──────────────────────────────────────
    # Also handles Phase-8 bot rooms created via /api/rooms/create-bot and
    # served via /play/{room_id}.  When the room is still "waiting" we start
    # the game immediately (no countdown needed for vs-bot).
    if blob.get("status") == "waiting" and blob.get("mode") == "solo":
        # Mark the human as connected in the blob.
        for p in blob.get("players", []):
            if not p.get("is_bot") and p.get("guest_id") == session.get("guest_id", ""):
                p["connected"] = True
                p["sid"] = request.sid
                break
        app.game_store.set(room_id, blob)
        # Auto-start the bot game now.
        socketio.start_background_task(_start_bot_game, room_id, app.game_store)
        return

    try:
        manager = GameManager.load(room_id, app.game_store)
    except RoomNotFoundError:
        emit("error", {"message": "Room not found — start a new game"})
        return

    vd = manager.view_data()
    vd["seq"] = _room_seqs.get(room_id, 0)
    emit("state_snapshot", vd)

    # If a bot move is pending (e.g. page reload mid-bot-turn), restart the task.
    if manager.phase == GamePhase.BOT_MOVING and manager.pending_bot_move:
        socketio.start_background_task(_run_bot_turn, room_id, app.game_store)


# ---------------------------------------------------------------------------
# Player actions
# ---------------------------------------------------------------------------


@socketio.on("play_card")
def on_play_card(data: dict):
    data = data or {}
    room_id = data.get("room_id") or session.get("solo_room_id")
    if not room_id:
        emit("error", {"message": "No room ID"})
        return

    played_raw   = data.get("card")
    captures_raw = data.get("captures") or []

    try:
        manager = GameManager.load(room_id, app.game_store)
    except RoomNotFoundError:
        emit("error", {"message": "Room not found"})
        return

    if not manager.state:
        emit("error", {"message": "No active game state"})
        return

    # Determine acting seat from per-process mapping.
    acting_seat = _sid_to_seat.get(request.sid, 0)
    is_mp = (manager.mode == "1v1")

    # ------------------------------------------------------------------
    # Convert card-data payloads → hand / table indices
    # ------------------------------------------------------------------
    try:
        played_card  = card_from_data(played_raw)
        player       = manager.state.players[acting_seat]
        hand_index   = player.hand.index(played_card)

        table_indices: list[int] = []
        for cap_raw in captures_raw:
            cap_card = card_from_data(cap_raw)
            for i, tc in enumerate(manager.state.table_cards):
                if tc == cap_card and i not in table_indices:
                    table_indices.append(i)
                    break
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        emit("error", {"message": f"Invalid card data: {exc}"})
        return

    # ------------------------------------------------------------------
    # Capture pre-move state for delta events
    # ------------------------------------------------------------------
    pre_chk         = manager.state.players[acting_seat].chkobbas
    played_card_str = card_to_str(played_card)
    captured_strs   = [
        card_to_str(manager.state.table_cards[i]) for i in table_indices
    ]
    is_raw_capture = len(table_indices) > 0

    # ------------------------------------------------------------------
    # Execute the move (saves to store internally)
    # ------------------------------------------------------------------
    success = manager._apply_selected_move(hand_index, table_indices, seat=acting_seat)
    if not success:
        vd = manager.view_data(viewer_seat=acting_seat)
        vd["seq"] = _room_seqs.get(room_id, 0)
        emit("state_snapshot", vd)
        return

    is_chkobba = manager.state.players[acting_seat].chkobbas > pre_chk
    needs_bot  = (manager.phase == GamePhase.BOT_MOVING)

    seq = _next_seq(room_id)

    move_info = _make_move_info(
        played_card_str=played_card_str,
        captured_strs=captured_strs,
        is_capture=is_raw_capture,
        is_chkobba=is_chkobba,
        captured_count=len(manager.state.players[acting_seat].captured_cards),
        chkobbas=manager.state.players[acting_seat].chkobbas,
        next_seat=manager.state.current_player if manager.state else 0,
    )

    if is_mp:
        # Build private views for each human seat.
        vd0 = manager.view_data(viewer_seat=0)
        vd1 = manager.view_data(viewer_seat=1)
        vd0["seq"] = vd1["seq"] = seq
        private_views: dict | None = {0: vd0, 1: vd1}
        broadcast_vd = vd0
    else:
        broadcast_vd = manager.view_data()
        broadcast_vd["seq"] = seq
        private_views = None

    socketio.start_background_task(
        _bg_human_play,
        room_id,
        acting_seat,
        move_info,
        broadcast_vd,
        needs_bot,
        app.game_store,
        private_views,
    )


@socketio.on("cut_choice")
def on_cut_choice(data: dict):
    """Opening round: cutter chooses keep (Path A) or discard to table (Path B)."""
    data = data or {}
    room_id = data.get("room_id") or session.get("solo_room_id")
    if not room_id:
        emit("error", {"message": "No room ID"})
        return

    keep_raw = str(data.get("keep", "")).strip().lower()
    if keep_raw in ("1", "true", "yes", "keep"):
        keep_cut = True
    elif keep_raw in ("0", "false", "no", "discard", "table"):
        keep_cut = False
    else:
        emit("error", {"message": "Invalid keep"})
        return

    try:
        manager = GameManager.load(room_id, app.game_store)
    except RoomNotFoundError:
        emit("error", {"message": "Room not found"})
        return

    acting_seat = _sid_to_seat.get(request.sid, 0)
    if not manager.commit_opening_cut_choice(keep_cut, acting_seat=acting_seat):
        vd = manager.view_data(viewer_seat=acting_seat)
        vd["seq"] = _room_seqs.get(room_id, 0)
        emit("state_snapshot", vd)
        emit("error", {"message": "Cut choice rejected"})
        return

    seq = _next_seq(room_id)
    is_mp = manager.mode == "1v1"
    if is_mp:
        vd0 = manager.view_data(viewer_seat=0)
        vd1 = manager.view_data(viewer_seat=1)
        vd0["seq"] = vd1["seq"] = seq
        _emit_private_snapshots(room_id, {0: vd0, 1: vd1})
    else:
        vd = manager.view_data()
        vd["seq"] = seq
        emit("state_snapshot", vd)

    if manager.phase == GamePhase.BOT_MOVING and manager.pending_bot_move:
        socketio.start_background_task(_run_bot_turn, room_id, app.game_store)


@socketio.on("request_resync")
def on_request_resync(data: dict):
    data = data or {}
    room_id = data.get("room_id") or session.get("solo_room_id")
    if not room_id:
        return

    try:
        manager = GameManager.load(room_id, app.game_store)
    except RoomNotFoundError:
        return

    my_seat = _sid_to_seat.get(request.sid, 0)
    vd = manager.view_data(viewer_seat=my_seat)
    vd["seq"] = _room_seqs.get(room_id, 0)
    emit("state_snapshot", vd)
    logger.info("[socket] resync  room=%.8s  seat=%s", room_id, my_seat)


# ---------------------------------------------------------------------------
# Background task helpers
# ---------------------------------------------------------------------------


def _record_match_result(room_id: str, view_data: dict, *, was_forfeit: bool) -> None:
    """Persist the completed match to mp_matches.  Best-effort — never raises."""
    try:
        blob = app.game_store.get(room_id)
        if blob is None:
            return
        players_raw = blob.get("players", [])
        mode        = blob.get("mode", "1v1")
        scores      = view_data.get("match_scores") or [0, 0]
        winner_seat = view_data.get("match_winner")  # None = draw

        winner_guest_id: str | None = None
        if winner_seat is not None and len(players_raw) > winner_seat:
            winner_guest_id = players_raw[winner_seat].get("guest_id")

        players_for_db = [
            {
                "guest_id":    p.get("guest_id", ""),
                "display_name": p.get("display_name", "?"),
                "seat":        p.get("seat", i),
            }
            for i, p in enumerate(players_raw)
        ]
        record_match(
            room_id=room_id,
            mode=mode,
            players=players_for_db,
            scores=list(scores),
            winner_guest_id=winner_guest_id,
            was_forfeit=was_forfeit,
        )
        logger.info("[match] recorded  room=%.8s  winner=%s  forfeit=%s",
                    room_id, winner_guest_id, was_forfeit)
    except Exception as exc:
        logger.warning("[match] record_match failed  room=%.8s: %s", room_id, exc)


def _make_move_info(
    *,
    played_card_str: str,
    captured_strs: list[str],
    is_capture: bool,
    is_chkobba: bool,
    captured_count: int,
    chkobbas: int,
    next_seat: int,
) -> dict:
    return {
        "played_card":    played_card_str,
        "captured":       captured_strs,
        "is_capture":     is_capture,
        "is_chkobba":     is_chkobba,
        "captured_count": captured_count,
        "chkobbas":       chkobbas,
        "next_seat":      next_seat,
    }


def _emit_private_snapshots(room_id: str, private_views: dict) -> None:
    """Send a state_snapshot privately to each connected seat in a 1v1 room."""
    blob = app.game_store.get(room_id)
    for seat_idx, vd in private_views.items():
        sid = _seat_to_sid(room_id, seat_idx, blob)
        if sid:
            socketio.emit("state_snapshot", vd, to=sid)


def _emit_state_snapshot_after_move(
    room_id: str, view_data: dict, private_views: dict | None = None
) -> None:
    """Authoritative snapshot after a play (including round/match over).

    Clients rely on ``state_snapshot`` for ``round_end_sweep`` and scores; the
    ``round_over`` / ``match_over`` delta alone is not enough.
    """
    if private_views:
        blob = app.game_store.get(room_id)
        for seat_idx, vd in private_views.items():
            out = dict(vd)
            out["seq"] = _next_seq(room_id)
            sid = _seat_to_sid(room_id, seat_idx, blob)
            if sid:
                socketio.emit("state_snapshot", out, to=sid)
        return
    out = dict(view_data)
    out["seq"] = _next_seq(room_id)
    socketio.emit("state_snapshot", out, to=room_id)


def _emit_play_events(
    room_id: str,
    seat: int,
    move_info: dict,
    view_data: dict,
    private_views: dict | None = None,
) -> None:
    """Emit the ordered delta-event sequence for one completed play.

    Designed to run inside a background task where socketio.sleep() is safe.
    Uses socketio.emit() (not flask_socketio.emit()) — works outside request context.

    When *private_views* is provided (multiplayer), the final state_snapshot is
    sent privately per-seat instead of broadcast.
    """
    seq1 = _next_seq(room_id)
    if move_info["is_capture"]:
        socketio.emit(
            "cards_captured",
            {
                "seat":        seat,
                "played_card": move_info["played_card"],
                "captured":    move_info["captured"],
                "is_chkobba":  move_info["is_chkobba"],
                "seq":         seq1,
            },
            to=room_id,
        )
    else:
        socketio.emit(
            "card_played",
            {
                "seat": seat,
                "card": move_info["played_card"],
                "seq":  seq1,
            },
            to=room_id,
        )

    socketio.sleep(0.12)

    seq2 = _next_seq(room_id)
    socketio.emit(
        "score_updated",
        {
            "seat":           seat,
            "captured_count": move_info["captured_count"],
            "chkobbas":       move_info["chkobbas"],
            "seq":            seq2,
        },
        to=room_id,
    )

    socketio.sleep(0.12)

    if view_data.get("match_over"):
        seq3 = _next_seq(room_id)
        _record_match_result(room_id, view_data, was_forfeit=False)
        socketio.emit(
            "match_over",
            {
                "winner_seat":  view_data.get("match_winner"),
                "final_scores": view_data.get("match_scores"),
                "seq":          seq3,
            },
            to=room_id,
        )
        _emit_state_snapshot_after_move(room_id, view_data, private_views)
        return

    if view_data.get("round_over"):
        seq3 = _next_seq(room_id)
        socketio.emit(
            "round_over",
            {
                "breakdown": view_data.get("round_breakdown", []),
                "scores":    view_data.get("match_scores"),
                "seq":       seq3,
            },
            to=room_id,
        )
        _emit_state_snapshot_after_move(room_id, view_data, private_views)
        return

    seq3 = _next_seq(room_id)
    socketio.emit(
        "turn_changed",
        {"next_seat": move_info["next_seat"], "seq": seq3},
        to=room_id,
    )

    socketio.sleep(0.12)

    # Full state snapshot — private per-seat in multiplayer, broadcast in solo.
    if private_views:
        _emit_private_snapshots(room_id, private_views)
    else:
        socketio.emit("state_snapshot", view_data, to=room_id)


def _bg_human_play(
    room_id: str,
    seat: int,
    move_info: dict,
    view_data: dict,
    needs_bot: bool,
    store,
    private_views: dict | None = None,
) -> None:
    """Background task: emit human-play delta events, then optionally run bot."""
    _emit_play_events(room_id, seat, move_info, view_data, private_views)
    if needs_bot:
        _run_bot_turn(room_id, store)


def _run_bot_turn(room_id: str, store) -> None:
    """Background task: wait (thinking delay), apply bot move, emit events.

    Only used in solo mode — 1v1 rooms never call this.
    """
    socketio.sleep(BOT_THINKING_DELAY)

    seq = _next_seq(room_id)
    socketio.emit("bot_thinking", {"seat": 1, "seq": seq}, to=room_id)

    socketio.sleep(0.15)

    try:
        manager = GameManager.load(room_id, store)
    except RoomNotFoundError:
        logger.warning("[bot_turn] room %.8s gone from store", room_id)
        return

    if not manager.pending_bot_move:
        logger.warning("[bot_turn] no pending move in room %.8s", room_id)
        return

    # First move of the round in solo: client shows table, deal animation, then pause.
    if (
        manager.mode == "solo"
        and manager.state
        and len(manager.state.move_history) == 0
    ):
        socketio.sleep(SOLO_OPENING_BOT_DELAY_S)

    # Emit a pre-play snapshot so clients can show the bot's chosen card in the
    # opponent hand zone (pending_bot_played_card) before it hits the table.
    # This restores the classic "thinking -> reveal -> play" flow in solo mode.
    try:
        vd_pending = manager.view_data()
        vd_pending["seq"] = _next_seq(room_id)
        socketio.emit("state_snapshot", vd_pending, to=room_id)
    except Exception:
        # Best effort only; game logic continues even if preview emit fails.
        logger.exception("[bot_turn] failed to emit pending snapshot  room=%.8s", room_id)

    socketio.sleep(0.28)

    bot_move   = manager.pending_bot_move
    pre_chk    = manager.state.players[manager.bot_id].chkobbas

    played_card_str = card_to_str(bot_move.played_card)
    captured_strs   = [card_to_str(c) for c in bot_move.captured_cards]

    manager.commit_bot_move()   # saves to store internally

    is_chkobba = manager.state.players[manager.bot_id].chkobbas > pre_chk

    vd = manager.view_data()
    vd["seq"] = _next_seq(room_id)

    move_info = _make_move_info(
        played_card_str=played_card_str,
        captured_strs=captured_strs,
        is_capture=bot_move.is_capture,
        is_chkobba=is_chkobba,
        captured_count=len(manager.state.players[manager.bot_id].captured_cards),
        chkobbas=manager.state.players[manager.bot_id].chkobbas,
        next_seat=manager.state.current_player if manager.state else 0,
    )

    socketio.sleep(0.3)
    _emit_play_events(room_id, manager.bot_id, move_info, vd)


def _reconnect_timeout(room_id: str, seat: int, guest_id: str, store) -> None:
    """Background task: wait 30 s then replace the disconnected player with a bot.

    If the player reconnects before the deadline, ``_disconnect_tasks[room_id]``
    is cleared to ``""`` (or the key is removed) and this task bails out.
    """
    RECONNECT_WINDOW = 30  # seconds

    # Tick down and broadcast countdown updates every 5 seconds.
    for remaining in range(RECONNECT_WINDOW, 0, -5):
        socketio.sleep(5)
        if _disconnect_tasks.get(room_id) != guest_id:
            logger.info("[disc] player %.8s reconnected before timeout  room=%.8s", guest_id, room_id)
            return

    # Final check before replacing.
    if _disconnect_tasks.get(room_id) != guest_id:
        return

    blob = store.get(room_id)
    if blob is None or blob.get("status") != "active":
        _disconnect_tasks.pop(room_id, None)
        return

    disc = blob.get("disconnect") or {}
    if disc.get("guest_id") != guest_id or disc.get("seat") != seat:
        # Disconnect record was already cleared (reconnect happened between checks).
        _disconnect_tasks.pop(room_id, None)
        return

    # ── Replace the disconnected seat with the bot engine ──────────────────
    disc_name = ""
    for p in blob.get("players", []):
        if p.get("seat") == seat:
            disc_name = p.get("display_name", "Opponent")
            break

    # Clear the disconnect record from the blob BEFORE loading the manager so
    # save() doesn't re-copy it via the "preserve extra keys" logic.
    blob.pop("disconnect", None)
    store.set(room_id, blob)
    _disconnect_tasks.pop(room_id, None)

    try:
        manager = GameManager.load(room_id, store)
        manager.replace_with_bot(seat)   # sets mode=solo, queues bot move if needed
    except Exception as exc:
        logger.exception("[disc] replace_with_bot failed  room=%.8s: %s", room_id, exc)
        return

    socketio.emit(
        "player_replaced_with_bot",
        {
            "seat":         seat,
            "display_name": disc_name,
            "seq":          _next_seq(room_id),
        },
        to=room_id,
    )

    # If the bot needs to move immediately, kick off the task.
    if manager.phase == GamePhase.BOT_MOVING or manager.pending_bot_move:
        socketio.start_background_task(_run_bot_turn, room_id, store)

    logger.info("[disc] seat %d replaced with bot  room=%.8s", seat, room_id)


def _start_bot_game(room_id: str, store) -> None:
    """Start a solo-vs-bot game immediately (Phase 8 — /play/{room_id} bot path).

    No countdown, no waiting — creates the game and sends the state snapshot.
    """
    socketio.sleep(0.15)

    blob = store.get(room_id)
    if blob is None or blob.get("status") != "waiting":
        # Already started or gone.
        if blob and blob.get("status") == "active":
            # Send snapshot to the rejoining client.
            sid = _seat_to_sid(room_id, 0, blob)
            if sid:
                try:
                    manager = GameManager.load(room_id, store)
                    vd = manager.view_data()
                    vd["seq"] = _room_seqs.get(room_id, 0)
                    socketio.emit("state_snapshot", vd, to=sid)
                    if manager.phase == GamePhase.BOT_MOVING and manager.pending_bot_move:
                        socketio.start_background_task(_run_bot_turn, room_id, store)
                except RoomNotFoundError:
                    pass
        return

    target_score = blob.get("target_score_mp", 11)
    try:
        manager = GameManager.create(room_id, target_score, store)
    except Exception as exc:
        logger.exception("[bot_game] failed to create game  room=%.8s: %s", room_id, exc)
        return

    sid = _seat_to_sid(room_id, 0, blob)
    if sid:
        vd = manager.view_data()
        vd["seq"] = _room_seqs.get(room_id, 0)
        socketio.emit("state_snapshot", vd, to=sid)

    logger.info("[bot_game] started  room=%.8s  target=%d", room_id, target_score)


def _start_mp_game(room_id: str, store) -> None:
    """Background task — called when both players are connected to a 1v1 room.

    Sequence:
      1. Verify two players are present and room is still *waiting*.
      2. Mark blob status → "starting"; broadcast ``match_starting`` countdown.
      3. Sleep for the countdown duration so animations can play on clients.
      4. Re-check the blob (guard against cancellation during countdown).
      5. Create the game with a random first seat.
      6. Send private ``state_snapshot`` to each player's socket.
      7. Broadcast ``turn_changed`` so clients know who acts first.
    """
    socketio.sleep(0.25)  # small pause for UI to settle after second player joins

    blob = store.get(room_id)
    if blob is None:
        return
    if len(blob.get("players", [])) < 2:
        return
    # Guard against duplicate task invocations.
    if blob.get("status") in ("active", "starting", "round_over", "match_over"):
        return

    # ── Mark as "starting" before broadcasting so concurrent tasks bail out ──
    blob["status"] = "starting"
    store.set(room_id, blob)

    countdown_secs = 3
    socketio.emit(
        "match_starting",
        {
            "room_id": room_id,
            "countdown": countdown_secs,
            "player_names": [p.get("display_name", "?") for p in blob.get("players", [])],
        },
        to=room_id,
    )

    # Wait for the client countdown to finish (add a small buffer).
    socketio.sleep(countdown_secs + 0.15)

    # Re-fetch: the room may have been cancelled during the countdown.
    blob = store.get(room_id)
    if blob is None or blob.get("status") != "starting":
        logger.info("[mp] game cancelled or status changed during countdown  room=%.8s", room_id)
        return

    # ── Pick a random first player ──
    first_seat = random.randint(0, 1)
    target_score = blob.get("target_score_mp", 11)

    try:
        manager = GameManager.create_mp(room_id, target_score, store, starting_seat=first_seat)
    except Exception as exc:
        logger.exception("[mp] failed to start game in room %.8s: %s", room_id, exc)
        return

    # ── Send private state snapshots (each player sees their own hand) ──
    blob = store.get(room_id) or blob
    for seat_idx in (0, 1):
        sid = _seat_to_sid(room_id, seat_idx, blob)
        if sid:
            vd = manager.view_data(viewer_seat=seat_idx)
            vd["seq"] = _room_seqs.get(room_id, 0)
            socketio.emit("state_snapshot", vd, to=sid)

    # ── Broadcast whose turn it is ──
    socketio.sleep(0.1)
    socketio.emit(
        "turn_changed",
        {"next_seat": first_seat, "seq": _next_seq(room_id)},
        to=room_id,
    )

    logger.info(
        "[mp] game started  room=%.8s  target=%d  first_seat=%d  players=%s",
        room_id,
        target_score,
        first_seat,
        [p.get("display_name") for p in blob.get("players", [])],
    )
