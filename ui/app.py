from __future__ import annotations

# ---------------------------------------------------------------------------
# Async runtime — must be patched before any other stdlib imports so that
# eventlet's cooperative I/O replaces blocking sockets everywhere.
# Falls back gracefully to threading mode (e.g. when eventlet is not yet
# installed in the venv, which is common on a fresh checkout before
# `pip install -r requirements.txt`).
# ---------------------------------------------------------------------------
try:
    import eventlet
    eventlet.monkey_patch()
    _ASYNC_MODE = "eventlet"
except ImportError:
    _ASYNC_MODE = "threading"

import os
import sys
import logging
import hmac
import secrets
import random
from datetime import timedelta
from pathlib import Path
from functools import wraps
from enum import Enum
from time import time

# Ensure project root is on path for engine imports
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from flask import Flask, render_template, redirect, url_for, flash, request, session, jsonify
from flask_socketio import SocketIO

from engine.game_state import (
    GameState,
    Move,
    Card,
    Suit,
    Rank,
    PlayerState,
    create_initial_state,
    tunisian_barmila_points,
    full_deck,
    apply_opening_deal_from_cut,
    choose_opening_cut_index,
    OPENING_CUT_MARGIN,
)
from engine.heuristic_bot import get_heuristic_move
from engine.utils import card_to_str
from engine.persistence import (
    save_match,
    init_database,
    get_match_history,
    get_statistics,
)

from services.game_store import GameStore, get_game_store
from services.names import generate_display_name, avatar_color, is_clean
from services.avatars import (
    DEFAULT_PLAYER_AVATAR_KEY,
    bot_avatar_file_exists,
    bot_avatar_static_path,
    is_valid_player_avatar_key,
    list_player_avatar_keys,
    player_avatar_static_path,
    PLAYER_AVATAR_LABELS,
)
from services.matchmaking import MatchmakingQueue, get_matchmaking_queue

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize solo-game database tables (engine layer)
init_database()
# Initialize multiplayer tables (models layer)
from models.db import init_models as _init_models
from models.guests import (
    ensure_guest as _ensure_guest,
    get_guest as _get_guest,
    update_display_name as _update_display_name,
    update_avatar_key as _update_avatar_key,
)
_init_models()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RoomNotFoundError(Exception):
    """Raised by GameManager.load() when the room_id is absent from the store."""


# ---------------------------------------------------------------------------
# Game Phase State Machine
# ---------------------------------------------------------------------------

class GamePhase(Enum):
    """Explicit game phase for state machine."""
    MENU = "menu"              # Start screen, no game running
    CUT_DECISION = "cut_decision"  # Opening: cutter chooses keep vs table
    PLAYING_HUMAN = "playing_human"  # Human's turn to move
    PLAYING_BOT = "playing_bot"      # Bot is thinking and about to move
    BOT_MOVING = "bot_moving"  # Bot move animation in progress
    ROUND_OVER = "round_over"  # Round finished, show scores
    MATCH_OVER = "match_over"  # Match completed, show winner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _move_to_indices(state: GameState, move: Move, human_id: int) -> tuple[int, list[int]]:
    """Convert a Move to (hand_index, [table_indices])."""
    human = state.players[human_id]
    hand_index = human.hand.index(move.played_card)

    table_indices = []
    used = set()
    for cap_card in move.captured_cards:
        for i, t_card in enumerate(state.table_cards):
            if t_card == cap_card and i not in used:
                table_indices.append(i)
                used.add(i)
                break

    return hand_index, table_indices

# ---------------------------------------------------------------------------
# Bot personality config
# ---------------------------------------------------------------------------
# Change BOT_DEFAULT_NAME to customise; add more names to BOT_NAMES for future use.
BOT_NAMES = [
    "Sidi Daoued",
    "Khalti Aïcha",
    "Houcine el-Kahwagi",
    "Brahim",
    "Youssef",
]
BOT_DEFAULT_NAME: str = BOT_NAMES[0]  # change index to pick a different persona

# Commentary lines shown as toasts on notable game events (Tunisian dialect).
_COMMENTARY: dict[str, str] = {
    "human_chkobba":    "Mabrouk! 🎉",
    "bot_chkobba":      "Aâlach hakka? 🤔",
    "player_wins_round": "Yezzi! 💪",
    "bot_wins_round":   "Aâlach hakka? 🤔",
    "close_call":       "Chouf chouf 👀",
}

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "templates"),
    static_folder=str(Path(__file__).parent / "static"),
)

# Production: set SECRET_KEY in the Render dashboard (or .env locally). Do not use the dev default online.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "chkobba-dev-key")
app.config["DEBUG"] = False
app.config["DEBUG_MODE"] = False
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)

GUEST_ID_COOKIE = "chk_guest_id"
GUEST_ID_COOKIE_MAX_AGE = 365 * 24 * 60 * 60

# Render sets RENDER=true; used to disable debug toggles and unsafe defaults in production.
IS_PRODUCTION = os.environ.get("RENDER", "").lower() == "true"

# ---------------------------------------------------------------------------
# Real-time layer (Flask-SocketIO + optional Redis message broker)
# ---------------------------------------------------------------------------
# REDIS_URL is optional for local dev (single worker) but required in
# production when running multiple gunicorn workers so they can all share
# room state via a pub/sub message queue.
# Set it in the Render dashboard → Environment → REDIS_URL=redis://<host>:6379
# ---------------------------------------------------------------------------
REDIS_URL: str | None = os.environ.get("REDIS_URL", None)

socketio = SocketIO(
    app,
    async_mode=_ASYNC_MODE,
    message_queue=REDIS_URL,   # None → in-process only (fine for 1 worker)
    cors_allowed_origins="*",
    logger=False,
    engineio_logger=False,
)

# Game-state store — must be created after app so _get_manager() can reference
# app.game_store.  The factory reads REDIS_URL; logs which backend is active.
app.game_store = get_game_store()
app.matchmaking_queue = get_matchmaking_queue()

# Pending direct play invites between friends: invite_id → metadata.
_friend_play_invites: dict[str, dict] = {}

TARGET_SCORES = [11, 21, 31]
MP_TARGET_SCORES = [11, 21, 31]   # available target scores for multiplayer rooms

# ---------------------------------------------------------------------------
# Deployment note (Gunicorn / multi-worker / horizontal scale)
# ---------------------------------------------------------------------------
# Game state is now stored in app.game_store (MemoryGameStore or RedisGameStore),
# keyed by solo_room_id stored in each browser's session cookie.
# - MemoryGameStore: in-process dict, safe for single-worker dev.
# - RedisGameStore: safe for multiple Gunicorn workers; set REDIS_URL env var.
# - With -w 1 and no REDIS_URL, MemoryGameStore is used (zero extra deps).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Guest-session middleware
# ---------------------------------------------------------------------------

def _hydrate_guest_profile_from_db(guest_id: str) -> None:
    """Load saved display name and avatar from SQLite into the Flask session."""
    guest = _get_guest(guest_id)
    if not guest:
        return
    dn = (guest.get("display_name") or "").strip()
    if dn:
        session["display_name"] = dn
    _resolve_guest_avatar_key(guest_id)


@app.before_request
def _ensure_guest_session() -> None:
    """Give every browser a persistent guest identity (UUID in session cookie).

    Profile is stored in SQLite and restored via a long-lived guest-id cookie
    when the Flask session expires.
    """
    session.permanent = True
    guest_id = session.get("guest_id")

    if not guest_id:
        restored = request.cookies.get(GUEST_ID_COOKIE)
        if restored and _get_guest(restored):
            guest_id = restored
            session["guest_id"] = guest_id

    if not guest_id:
        guest_id = secrets.token_hex(16)
        session["guest_id"] = guest_id
        name = generate_display_name()
        _ensure_guest(guest_id, display_name=name)
        session["display_name"] = name
        if is_valid_player_avatar_key(DEFAULT_PLAYER_AVATAR_KEY):
            session["avatar_key"] = DEFAULT_PLAYER_AVATAR_KEY
            _update_avatar_key(guest_id, DEFAULT_PLAYER_AVATAR_KEY)
    else:
        _ensure_guest(guest_id, display_name=session.get("display_name", "Guest"))

    _hydrate_guest_profile_from_db(guest_id)


@app.after_request
def _persist_guest_id_cookie(response):
    """Mirror guest_id in a long-lived cookie so profile survives session expiry."""
    guest_id = session.get("guest_id")
    if guest_id:
        response.set_cookie(
            GUEST_ID_COOKIE,
            guest_id,
            max_age=GUEST_ID_COOKIE_MAX_AGE,
            samesite="Lax",
            secure=IS_PRODUCTION,
            httponly=False,
        )
    return response


def csrf_protect(f):
    """Decorator to validate CSRF tokens on POST requests."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if request.method == 'POST':
            token = session.get('_csrf_token', None)
            if not token or not hmac.compare_digest(token, request.form.get('_csrf_token', '')):
                logger.warning("CSRF validation failed for request to %s", request.path)
                flash("Invalid request. Please refresh and try again.")
                return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function


def generate_csrf_token():
    """Generate CSRF token for session."""
    if '_csrf_token' not in session:
        import secrets
        session['_csrf_token'] = secrets.token_hex(32)
    return session['_csrf_token']


app.jinja_env.globals['csrf_token'] = generate_csrf_token
app.jinja_env.filters['avatar_color_filter'] = avatar_color


def _static_url(filename: str) -> str:
    """Return a versioned URL for a static file using its mtime as cache-buster.

    If the file doesn't exist (e.g. in test), falls back to the plain URL.
    Works outside Flask app/request context (e.g. eventlet background tasks).
    """
    from flask import has_app_context, url_for as _url_for

    fpath = Path(__file__).parent / "static" / filename
    try:
        v = int(fpath.stat().st_mtime)
    except OSError:
        v = None

    if has_app_context():
        if v is None:
            return _url_for("static", filename=filename)
        return _url_for("static", filename=filename, v=v)

    base = f"/static/{filename}"
    return base if v is None else f"{base}?v={v}"


app.jinja_env.globals["static_url"] = _static_url


def _bot_avatar_url(display_name: str) -> str | None:
    """Versioned URL for a bot portrait, or ``None`` if none configured."""
    rel = bot_avatar_static_path(display_name)
    if not rel or not bot_avatar_file_exists(display_name):
        return None
    return _static_url(rel)


app.jinja_env.filters["bot_avatar_url_filter"] = _bot_avatar_url


def _player_avatar_url(avatar_key: str | None) -> str | None:
    rel = player_avatar_static_path(avatar_key)
    if not rel:
        return None
    return _static_url(rel)


def _avatar_key_for_guest(guest_id: str) -> str | None:
    """Resolve avatar key for any guest (session, DB, or default male)."""
    if not guest_id:
        return None
    if guest_id == session.get("guest_id"):
        key = session.get("avatar_key")
        if key and is_valid_player_avatar_key(key):
            return key
    guest = _get_guest(guest_id)
    if guest:
        db_key = guest.get("avatar_key")
        if db_key and is_valid_player_avatar_key(db_key):
            return db_key
    if is_valid_player_avatar_key(DEFAULT_PLAYER_AVATAR_KEY):
        return DEFAULT_PLAYER_AVATAR_KEY
    return None


def _player_presence_fields(guest_id: str) -> dict:
    """Avatar fields stored on room player blobs."""
    key = _avatar_key_for_guest(guest_id)
    return {"avatar_key": key, "avatar_url": _player_avatar_url(key)}


def _resolve_guest_avatar_key(guest_id: str) -> str | None:
    if guest_id != session.get("guest_id"):
        return _avatar_key_for_guest(guest_id)
    key = session.get("avatar_key")
    if key and is_valid_player_avatar_key(key):
        return key
    guest = _get_guest(guest_id) if guest_id else None
    if guest:
        db_key = guest.get("avatar_key")
        if db_key and is_valid_player_avatar_key(db_key):
            session["avatar_key"] = db_key
            return db_key
    if is_valid_player_avatar_key(DEFAULT_PLAYER_AVATAR_KEY):
        session["avatar_key"] = DEFAULT_PLAYER_AVATAR_KEY
        if guest_id:
            _update_avatar_key(guest_id, DEFAULT_PLAYER_AVATAR_KEY)
        return DEFAULT_PLAYER_AVATAR_KEY
    session.pop("avatar_key", None)
    return None


def _player_at_seat(blob: dict | None, seat: int) -> dict | None:
    """Return the room player dict for *seat* (not raw list index)."""
    if not blob:
        return None
    players = blob.get("players", [])
    for p in players:
        if p.get("seat") == seat:
            return p
    if 0 <= seat < len(players):
        return players[seat]
    return None


def _opponent_avatar_url(
    blob: dict | None,
    opp_id: int,
    *,
    is_solo: bool,
    opp_name: str,
) -> str | None:
    if is_solo:
        return _bot_avatar_url(opp_name)
    p = _player_at_seat(blob, opp_id)
    if p is None:
        return None
    if p.get("is_bot"):
        return _bot_avatar_url(BOT_DEFAULT_NAME)
    url = p.get("avatar_url")
    if url:
        return url
    key = p.get("avatar_key") or _avatar_key_for_guest(p.get("guest_id", ""))
    return _player_avatar_url(key)


def _ensure_blob_player_avatars(blob: dict) -> bool:
    """Backfill avatar_key/url on human players; return True if blob was updated."""
    changed = False
    for p in blob.get("players", []):
        if p.get("is_bot"):
            continue
        if p.get("avatar_url"):
            continue
        fields = _player_presence_fields(p.get("guest_id", ""))
        p.update(fields)
        changed = True
    return changed


def _human_opponent_from_blob(blob: dict | None, my_seat: int) -> dict | None:
    """Return the other human player in a 1v1 blob, or None (bot / missing)."""
    if not blob or blob.get("mode") != "1v1":
        return None
    for p in blob.get("players", []):
        if p.get("is_bot"):
            continue
        if p.get("seat") == my_seat:
            continue
        gid = p.get("guest_id") or ""
        if gid and gid != "bot":
            return p
    return None


def _opponent_profile_fields(
    blob: dict | None,
    opp_id: int,
    *,
    is_solo: bool,
) -> dict:
    """guest_id + is_human for the opponent (MP human only)."""
    if is_solo or not blob:
        return {"opponent_guest_id": None, "opponent_is_human": False}
    p = _player_at_seat(blob, opp_id)
    if p is None:
        return {"opponent_guest_id": None, "opponent_is_human": False}
    if p.get("is_bot"):
        return {"opponent_guest_id": None, "opponent_is_human": False}
    gid = p.get("guest_id") or ""
    if not gid or gid == "bot":
        return {"opponent_guest_id": None, "opponent_is_human": False}
    return {"opponent_guest_id": gid, "opponent_is_human": True}


def _opponent_avatar_extras(opp_name: str, guest_id: str | None) -> dict:
    """Fallback avatar dot fields when opponent has no image URL."""
    if not guest_id:
        return {"opponent_avatar_color": None, "opponent_avatar_initial": None}
    initial = opp_name[0].upper() if opp_name else "?"
    return {
        "opponent_avatar_color": avatar_color(guest_id),
        "opponent_avatar_initial": initial,
    }


def _opponent_ui_fields(
    blob: dict | None,
    opp_id: int,
    *,
    is_solo: bool,
    opp_name: str,
) -> dict:
    """Profile id, human flag, and fallback avatar dot for view_data / sockets."""
    profile = _opponent_profile_fields(blob, opp_id, is_solo=is_solo)
    return {
        **profile,
        **_opponent_avatar_extras(opp_name, profile.get("opponent_guest_id")),
    }


def _opp_ui_context(blob: dict | None, my_seat: int) -> dict:
    """Opponent name, avatar URL, profile ids for play.html (lobby + in-game)."""
    opp = _human_opponent_from_blob(blob, my_seat)
    if not opp:
        return {
            "opponent_guest_id": None,
            "opponent_is_human": False,
            "opponent_avatar_color": None,
            "opponent_avatar_initial": None,
            "bot_name": "Opponent",
            "bot_avatar_url": None,
        }
    gid = opp.get("guest_id") or ""
    name = opp.get("display_name") or "Opponent"
    opp_seat = opp.get("seat", 1 - my_seat)
    profile = {"opponent_guest_id": gid, "opponent_is_human": True}
    return {
        **profile,
        "bot_name": name,
        "bot_avatar_url": _opponent_avatar_url(
            blob, opp_seat, is_solo=False, opp_name=name
        ),
        **_opponent_avatar_extras(name, gid),
    }


def _sync_guest_profile_to_rooms(
    guest_id: str,
    display_name: str,
    avatar_key: str | None,
) -> None:
    avatar_url = _player_avatar_url(avatar_key)
    for room_id in app.game_store.list_active():
        blob = app.game_store.get(room_id)
        if not blob:
            continue
        changed = False
        for p in blob.get("players", []):
            if p.get("guest_id") == guest_id:
                p["display_name"] = display_name
                p["avatar_key"] = avatar_key
                p["avatar_url"] = avatar_url
                changed = True
        if changed:
            app.game_store.set(room_id, blob)
            socketio.emit(
                "player_profile_updated",
                {
                    "guest_id": guest_id,
                    "display_name": display_name,
                    "avatar_key": avatar_key,
                    "avatar_url": avatar_url,
                    "avatar_initial": display_name[0].upper() if display_name else "?",
                    "avatar_color": avatar_color(guest_id),
                },
                to=room_id,
            )


def _player_avatar_options() -> list[dict]:
    opts: list[dict] = []
    for key in list_player_avatar_keys():
        rel = player_avatar_static_path(key)
        if not rel:
            continue
        opts.append({
            "key": key,
            "label": PLAYER_AVATAR_LABELS.get(key, key.title()),
            "url": _static_url(rel),
        })
    return opts


def _apply_guest_profile(ctx: dict) -> None:
    """Merge display name + avatar fields into a template context dict."""
    gid = session.get("guest_id", "")
    dn = session.get("display_name", "Guest")
    avatar_key = _resolve_guest_avatar_key(gid)
    ctx["display_name"] = dn
    ctx["guest_id"] = gid
    ctx["avatar_initial"] = dn[0].upper() if dn else "G"
    ctx["avatar_color"] = avatar_color(gid)
    ctx["avatar_key"] = avatar_key
    ctx["human_avatar_url"] = _player_avatar_url(avatar_key)
    ctx["player_avatar_options"] = _player_avatar_options()


def _guest_profile_kwargs() -> dict:
    ctx: dict = {}
    _apply_guest_profile(ctx)
    return ctx


def _merge_template_ctx(*layers: dict) -> dict:
    """Merge template context dicts (last wins). Avoids duplicate ``**`` keyword errors."""
    ctx: dict = {}
    for layer in layers:
        ctx.update(layer)
    return ctx


def describe_move(move: Move) -> str:
    played = card_to_str(move.played_card)
    if move.is_capture:
        captured = ", ".join(card_to_str(c) for c in move.captured_cards)
        return f"played {played} and captured [{captured}]"
    return f"played {played} to the table"


def card_to_data(card: Card) -> dict:
    return {"suit": card.suit.name, "rank": card.rank.value}


def card_from_data(data: dict) -> Card:
    return Card(Suit[data["suit"]], Rank(data["rank"]))


def move_to_data(move: Move) -> dict:
    return {
        "played_card": card_to_data(move.played_card),
        "captured_cards": [card_to_data(c) for c in move.captured_cards],
    }


def move_from_data(data: dict) -> Move:
    return Move(
        played_card=card_from_data(data["played_card"]),
        captured_cards=tuple(card_from_data(c) for c in data.get("captured_cards", [])),
    )


def _coerce_target_score(value: object | None) -> int | None:
    """Parse target score from JSON / forms; return None if missing or invalid."""
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _coerce_match_pair(value: object | None) -> list[int]:
    """Normalise stored match scores to a pair of ints."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return [0, 0]
    try:
        return [int(value[0]), int(value[1])]
    except (TypeError, ValueError):
        return [0, 0]


# Table UI: five columns left→right; column 2 is the visual center.
# Fill priority (lower = earlier): inner pair first, then center, then outer edges.
TABLE_COLUMN_PRIORITIES: tuple[int, ...] = (3, 1, 2, 1, 3)
TABLE_INITIAL_COLUMNS = 5
TABLE_INITIAL_SLOT_COUNT = TABLE_INITIAL_COLUMNS * 2


# ---------------------------------------------------------------------------
# In-memory game state
# ---------------------------------------------------------------------------

class GameManager:
    """Holds one active game, match state, and UI messages.
    
    Uses explicit GamePhase enum instead of multiple boolean flags for clearer state management.
    """

    def __init__(self, room_id: str = "__no_room__", store: GameStore | None = None) -> None:
        # room_id  — unique per browser session (solo) or per match (multiplayer).
        # store    — GameStore instance (MemoryGameStore or RedisGameStore).
        self.room_id = room_id
        self.session_key = room_id   # legacy alias used internally; do not remove
        self._store: GameStore | None = store
        # Game state
        self.state: GameState | None = None
        self.phase: GamePhase = GamePhase.MENU
        
        # Match tracking
        self.match_scores: list[int] = [0, 0]
        self.target_score: int | None = None
        self.match_winner: int | None = None
        self.final_points: list[int] | None = None
        self.match_start_time: float | None = None
        self.round_history: list[tuple[int, int]] = []  # List of (human_pts, bot_pts)
        
        # Bot move animation
        self.pending_bot_move: Move | None = None
        self.pending_bot_captured_indices: list[int] = []
        self.pending_bot_played_card: str | None = None
        self.pending_bot_is_capture: bool = False
        self.pending_bot_is_chkobba: bool = False
        
        # Move tracking for animations
        self.last_human_played_table_index: int | None = None
        self.last_played_card: str | None = None
        self.last_played_by: str | None = None
        self.last_human_move: str | None = None
        self.last_bot_move: str | None = None
        
        # UI table slots (preserve empty positions after captures)
        self.table_slots: list[Card | None] = []
        # Opening cut: index into state.deck for the cut card (pre-deal)
        self.opening_cut_index: int | None = None

        # Player IDs — may be flipped by replace_with_bot() for seat-1-replaced games
        self.human_id: int = 0
        self.bot_id: int = 1

        # Set to the seat index that was replaced by the bot engine in a 1v1 game
        # (None means this is a normal solo or live-1v1 game)
        self.bot_replacement_seat: int | None = None

        # Game mode: "solo" (human vs bot) | "1v1" (human vs human)
        self.mode: str = "solo"
        
        # UI messaging
        self.messages: list[str] = []
        # Commentary toast to show on next page render (consumed once)
        self.pending_commentary: str | None = None
        # Per-round score breakdown for the notebook scorecard
        self.round_breakdown: list[dict] = []
        self.restart(clear_saved=False)

    def restart(self, clear_saved: bool = True) -> None:
        """Return to start screen."""
        self.state = None
        self.phase = GamePhase.MENU
        self.match_scores = [0, 0]
        self.target_score = None
        self.match_winner = None
        self.final_points = None
        self.match_start_time = None
        self.round_history = []
        
        # Clear all move/animation state
        self._clear_move_state()
        self.messages = []
        self.table_slots = []
        self.pending_commentary = None
        if clear_saved and self._store is not None:
            self._store.delete(self.room_id)
        
    def _clear_move_state(self) -> None:
        """Clear all move and animation tracking state."""
        self.pending_bot_move = None
        self.pending_bot_captured_indices = []
        self.pending_bot_played_card = None
        self.pending_bot_is_capture = False
        self.pending_bot_is_chkobba = False
        self.last_human_played_table_index = None
        self.last_played_card = None
        self.last_played_by = None
        self.last_human_move = None
        self.last_bot_move = None
        self.opening_cut_index = None

    def _setup_opening_cut(self, starting_seat: int, rng: random.Random | None = None) -> None:
        """Shuffle deck and enter CUT_DECISION; *starting_seat* is the cutter (plays first)."""
        rng = rng or random.Random()
        deck = full_deck()
        rng.shuffle(deck)
        self.state = GameState(
            players=[PlayerState(player_id=0), PlayerState(player_id=1)],
            table_cards=[],
            deck=deck,
            current_player=starting_seat,
            last_capturer=None,
        )
        self.opening_cut_index = choose_opening_cut_index(rng, len(deck))
        self.phase = GamePhase.CUT_DECISION
        self.table_slots = []
        self._try_bot_opening_cut_solo()

    def _try_bot_opening_cut_solo(self) -> None:
        """Solo: if the bot is the opening cutter, choose keep vs table automatically.

        Rule: cut card capture value < 6 → discard to table (Path B); else keep (Path A).
        """
        if self.mode != "solo":
            return
        if self.phase != GamePhase.CUT_DECISION:
            return
        if self.state is None or self.opening_cut_index is None:
            return
        if self.state.current_player != self.bot_id:
            return
        cut = self.state.deck[self.opening_cut_index]
        keep_cut = cut.value >= 6
        if not self.commit_opening_cut_choice(keep_cut, acting_seat=self.bot_id):
            logger.warning(
                "Bot opening cut auto-choice failed  room=%.8s",
                self.room_id,
            )

    def commit_opening_cut_choice(
        self,
        keep_cut: bool,
        acting_seat: int,
        client_cut_index: int | None = None,
    ) -> bool:
        """Apply Path A (keep) or B (discard) for the opening cut. Returns False if invalid."""
        if (
            self.phase != GamePhase.CUT_DECISION
            or self.state is None
            or self.opening_cut_index is None
        ):
            return False
        if acting_seat != self.state.current_player:
            return False
        k = self.opening_cut_index
        if client_cut_index is not None:
            n = len(self.state.deck)
            lo = OPENING_CUT_MARGIN
            hi = n - OPENING_CUT_MARGIN - 1
            if hi < lo or not (lo <= client_cut_index <= hi):
                return False
            k = int(client_cut_index)
        cutter = self.state.current_player
        try:
            new_state = apply_opening_deal_from_cut(
                self.state.deck, k, keep_cut, cutter_seat=cutter
            )
        except ValueError:
            return False
        self.state = new_state
        self.opening_cut_index = None
        self._populate_table_slots_from_cards(new_state.table_cards)
        self.phase = GamePhase.PLAYING_HUMAN
        self.messages.append(
            "Opening deal: cut card stays in the cutter's hand."
            if keep_cut
            else "Opening deal: cut card face-up on the table."
        )
        if self.mode == "solo" and new_state.current_player == self.bot_id:
            self.phase = GamePhase.BOT_MOVING
            self._queue_bot_move()
        self.save()
        return True

    def start_game(self, target_score: int, starting_seat: int = 0) -> None:
        """Initialize a new match with the given target score."""
        self.target_score = target_score
        self.match_scores = [0, 0]
        self.match_winner = None
        self.final_points = None
        self.match_start_time = time()
        self.round_history = []
        self._clear_move_state()

        self._setup_opening_cut(starting_seat, random.Random())
        logger.info("Started new game with target score %d (cut phase)", target_score)
        self.messages = [f"New game started. Target: {target_score} points.", "Cut the deck — choose to keep the cut card or place it on the table."]
        self.last_human_move = None
        self.last_bot_move = None
        self.round_over = False
        self.final_points = None
        self.save()

    def next_round(self) -> None:
        """Start a new round within the same match."""
        self.show_next_round = False
        self.round_over = False
        self.final_points = None
        self.last_human_move = None
        self.last_bot_move = None
        self.pending_bot_move = None
        self.pending_bot_captured_indices = []
        self.pending_bot_played_card = None
        self.pending_bot_is_capture = False
        self.last_human_played_table_index = None
        self.last_played_card = None
        self.last_played_by = None
        # Alternate who cuts / plays first each round (seat 0, then 1, then 0, …).
        next_opener = len(self.round_history) % 2
        self._setup_opening_cut(next_opener, random.Random())
        self.messages.append("New round — cut the deck.")
        self.save()

    # ------------------------------------------------------------------
    # Persistence helpers (Phase 2 — store-backed)
    # ------------------------------------------------------------------

    def _phase_to_status(self) -> str:
        if self.phase == GamePhase.MATCH_OVER:
            return "match_over"
        if self.phase == GamePhase.ROUND_OVER:
            return "round_over"
        if self.phase == GamePhase.MENU:
            return "waiting"
        return "active"

    def _build_game_data(self) -> dict:
        """Serialise the in-progress game to a plain dict (inner 'game' key)."""
        state_dict = None
        if self.state is not None:
            state_dict = {
                "players": [
                    {
                        "player_id": p.player_id,
                        "hand": [card_to_data(c) for c in p.hand],
                        "captured_cards": [card_to_data(c) for c in p.captured_cards],
                        "chkobbas": p.chkobbas,
                    }
                    for p in self.state.players
                ],
                "table_cards": [card_to_data(c) for c in self.state.table_cards],
                "deck": [card_to_data(c) for c in self.state.deck],
                "current_player": self.state.current_player,
                "last_capturer": self.state.last_capturer,
                "move_history": [move_to_data(m) for m in self.state.move_history],
                "match_scores": self.state.match_scores,
            }
        return {
            "phase": self.phase.value,
            "match_scores": self.match_scores,
            "target_score": self.target_score,
            "match_winner": self.match_winner,
            "final_points": self.final_points,
            "match_start_time": self.match_start_time,
            "round_history": self.round_history,
            "last_human_played_table_index": self.last_human_played_table_index,
            "last_played_card": self.last_played_card,
            "last_played_by": self.last_played_by,
            "last_human_move": self.last_human_move,
            "last_bot_move": self.last_bot_move,
            "messages": self.messages[-10:],
            "pending_bot_move": move_to_data(self.pending_bot_move) if self.pending_bot_move else None,
            "pending_bot_captured_indices": self.pending_bot_captured_indices,
            "pending_bot_played_card": self.pending_bot_played_card,
            "pending_bot_is_capture": self.pending_bot_is_capture,
            "table_slots": [card_to_data(c) if c is not None else None for c in self.table_slots],
            "pending_bot_is_chkobba": self.pending_bot_is_chkobba,
            "round_breakdown": self.round_breakdown,
            "mode": self.mode,
            "human_id": self.human_id,
            "bot_id": self.bot_id,
            "bot_replacement_seat": self.bot_replacement_seat,
            "opening_cut_index": self.opening_cut_index,
            "state": state_dict,
        }

    def save(self, ttl_seconds: int = 86400) -> None:
        """Persist current game state to the store.

        No-op if this is a transient MENU manager with no store / room.
        """
        if self._store is None or self.room_id == "__no_room__":
            return
        if self.state is None and self.target_score is None:
            # Nothing meaningful to persist.
            return

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        # Preserve created_at from the existing blob if it exists.
        existing = self._store.get(self.room_id)
        created_at = existing.get("created_at", now) if existing else now

        # Resolve guest_id safely — save() may be called from a background task
        # (no Flask request context), e.g. from _run_bot_turn in sockets.py.
        try:
            guest_id = session.get("guest_id", "unknown")
        except RuntimeError:
            # Outside request context: preserve from the existing blob if available.
            guest_id = (existing or {}).get("players", [{}])[0].get("guest_id", "unknown")

        existing_players = (existing or {}).get("players", [])
        if self.mode == "solo" and self.bot_replacement_seat is None and not existing_players:
            # Brand-new solo game with no pre-existing player array.
            players_blob = [
                {
                    "guest_id": guest_id,
                    "display_name": "You",
                    "seat": 0,
                    "is_bot": False,
                    "connected": True,
                    "sid": None,
                },
                {
                    "guest_id": "bot",
                    "display_name": BOT_DEFAULT_NAME,
                    "seat": 1,
                    "is_bot": True,
                    "connected": True,
                    "sid": None,
                },
            ]
        else:
            # 1v1, replaced-bot, or solo with an existing array — preserve it.
            players_blob = existing_players

        # Preserve lobby-specific metadata for multiplayer rooms.
        visibility        = (existing or {}).get("visibility", "private")
        created_by        = (existing or {}).get("created_by", guest_id)
        target_score_blob = (existing or {}).get("target_score_mp", self.target_score)

        blob = {
            "room_id": self.room_id,
            "mode": self.mode,
            "visibility": visibility,
            "created_by": created_by,
            "target_score_mp": target_score_blob,
            "status": self._phase_to_status(),
            "created_at": created_at,
            "last_action_at": now,
            "players": players_blob,
            "game": self._build_game_data(),
        }

        # Expose bot_replacement_seat at the top level so on_game_join can detect
        # late reconnects without loading the full manager.
        if self.bot_replacement_seat is not None:
            blob["bot_replacement_seat"] = self.bot_replacement_seat

        # Preserve extra blob fields that game logic doesn't manage directly.
        for _key in ("chat", "disconnect"):
            _val = (existing or {}).get(_key)
            if _val is not None:
                blob[_key] = _val

        self._store.set(self.room_id, blob, ttl_seconds)

    def _restore_from_game_data(self, data: dict) -> None:
        """Overwrite this manager's state from a serialised 'game' dict."""
        state_data = data.get("state")
        if not state_data:
            return
        players = [
            PlayerState(
                player_id=p["player_id"],
                hand=[card_from_data(c) for c in p["hand"]],
                captured_cards=[card_from_data(c) for c in p["captured_cards"]],
                chkobbas=p["chkobbas"],
            )
            for p in state_data["players"]
        ]
        inner_ms = _coerce_match_pair(state_data.get("match_scores"))
        self.state = GameState(
            players=players,
            table_cards=[card_from_data(c) for c in state_data["table_cards"]],
            deck=[card_from_data(c) for c in state_data["deck"]],
            current_player=state_data["current_player"],
            last_capturer=state_data["last_capturer"],
            move_history=[move_from_data(m) for m in state_data.get("move_history", [])],
            match_scores=inner_ms,
            round_end_sweep=None,
        )
        self.phase = GamePhase(data.get("phase", GamePhase.PLAYING_HUMAN.value))
        self.match_scores = _coerce_match_pair(data.get("match_scores"))
        self.target_score = _coerce_target_score(data.get("target_score"))
        self.match_winner = data.get("match_winner")
        self.final_points = data.get("final_points")
        self.match_start_time = data.get("match_start_time")
        self.round_history = [tuple(r) for r in data.get("round_history", [])]
        self.last_human_played_table_index = data.get("last_human_played_table_index")
        self.last_played_card = data.get("last_played_card")
        self.last_played_by = data.get("last_played_by")
        self.last_human_move = data.get("last_human_move")
        self.last_bot_move = data.get("last_bot_move")
        self.messages = data.get("messages", [])
        self.pending_bot_move = move_from_data(data["pending_bot_move"]) if data.get("pending_bot_move") else None
        self.pending_bot_captured_indices = data.get("pending_bot_captured_indices", [])
        self.pending_bot_played_card = data.get("pending_bot_played_card")
        self.pending_bot_is_capture = data.get("pending_bot_is_capture", False)
        self.pending_bot_is_chkobba = data.get("pending_bot_is_chkobba", False)
        self.round_breakdown = data.get("round_breakdown", [])
        self.mode = data.get("mode", "solo")
        self.human_id = data.get("human_id", 0)
        self.bot_id = data.get("bot_id", 1)
        self.bot_replacement_seat = data.get("bot_replacement_seat", None)
        self.opening_cut_index = data.get("opening_cut_index")
        self.table_slots = [card_from_data(c) if c is not None else None for c in data.get("table_slots", [])]
        if self.state is not None:
            if not self.table_slots:
                self._populate_table_slots_from_cards(list(self.state.table_cards))
            else:
                slot_cards = [c for c in self.table_slots if c is not None]
                if sorted(slot_cards, key=str) != sorted(self.state.table_cards, key=str):
                    self._populate_table_slots_from_cards(list(self.state.table_cards))
        if self.state is not None:
            self.state.match_scores = self.match_scores.copy()

    @classmethod
    def load(cls, room_id: str, store: GameStore) -> "GameManager":
        """Load a game from the store.

        Raises RoomNotFoundError if room_id is not in the store.
        """
        blob = store.get(room_id)
        if blob is None:
            raise RoomNotFoundError(f"Room {room_id!r} not found in store")
        manager = cls(room_id=room_id, store=store)
        manager._restore_from_game_data(blob.get("game", {}))
        if manager.target_score is None:
            fallback_ts = _coerce_target_score(blob.get("target_score_mp"))
            if fallback_ts is not None:
                manager.target_score = fallback_ts
        return manager

    @classmethod
    def create(cls, room_id: str, target_score: int, store: GameStore) -> "GameManager":
        """Create a fresh single-player game, persist it, return the manager."""
        manager = cls(room_id=room_id, store=store)
        manager.start_game(target_score)   # start_game() calls save() at the end
        return manager

    @classmethod
    def create_mp(
        cls,
        room_id: str,
        target_score: int,
        store: "GameStore",
        starting_seat: int = 0,
    ) -> "GameManager":
        """Create a fresh 1v1 multiplayer game.

        The room blob's players array must already be populated by the lobby
        system before this is called; save() will preserve it.
        ``starting_seat`` controls which player (0 or 1) acts first.
        """
        manager = cls(room_id=room_id, store=store)
        manager.mode = "1v1"
        manager.start_game(target_score, starting_seat=starting_seat)
        return manager

    def replace_with_bot(self, disconnected_seat: int) -> None:
        """Replace a disconnected player's seat with the bot AI.

        Converts the game from 1v1 to solo-style execution so the remaining
        human continues against the engine.  Called by the 30-second
        disconnection timeout task in sockets.py.
        """
        self.bot_replacement_seat = disconnected_seat
        self.mode = "solo"
        self.bot_id = disconnected_seat
        self.human_id = 1 - disconnected_seat

        # If it is currently the replaced player's turn, prime the pending bot
        # move so _run_bot_turn() can execute it immediately.
        if (
            self.state is not None
            and self.state.current_player == disconnected_seat
            and not self.state.is_round_over
            and self.phase != GamePhase.CUT_DECISION
        ):
            self.phase = GamePhase.BOT_MOVING
            self._queue_bot_move()

        self._try_bot_opening_cut_solo()

        logger.info(
            "Seat %d replaced with bot  room=%.8s  remaining_human=%d",
            disconnected_seat, self.room_id, self.human_id,
        )
        self.save()

    # ------------------------------------------------------------------
    # Legacy shim — kept so existing call-sites that check for a saved
    # session still compile; always returns False under the new store
    # architecture (the game is auto-loaded by _get_manager()).
    # ------------------------------------------------------------------
    def resume_saved_session(self) -> bool:
        return False

    # ------------------------------------------------------------------
    # Personality helpers
    # ------------------------------------------------------------------

    def _get_bot_mood(self) -> str:
        """Return an emoji representing the bot's current mood."""
        if self.last_human_move and "chkobba" in (self.last_human_move or "").lower():
            return "😲"  # surprised: player just swept
        if self.pending_bot_move is not None:
            return "🎯"  # focused on playing
        if self.phase == GamePhase.BOT_MOVING or (
            self.state is not None and self.state.current_player == self.bot_id
        ):
            return "🤔"  # thinking
        if self.state is not None:
            bot_caps  = len(self.state.players[self.bot_id].captured_cards)
            hum_caps  = len(self.state.players[self.human_id].captured_cards)
            if bot_caps > hum_caps + 4:
                return "😏"  # smug
        return "😐"  # default focused

    def _queue_commentary(self, event: str) -> None:
        """Stage a commentary toast for the next page render."""
        msg = _COMMENTARY.get(event)
        if msg:
            self.pending_commentary = msg

    def _consume_commentary(self) -> str | None:
        """Return and clear the pending commentary (so it shows only once)."""
        msg = self.pending_commentary
        self.pending_commentary = None
        return msg

    def _compute_round_breakdown(self) -> list[dict]:
        """Build the per-category scoring rows for the notebook scorecard."""
        if self.state is None:
            return []
        p0 = self.state.players[self.human_id]  # You
        p1 = self.state.players[self.bot_id]    # Bot
        rows: list[dict] = []

        c0, c1 = len(p0.captured_cards), len(p1.captured_cards)
        rows.append({
            "label": f"Most cards  ({c0} vs {c1})",
            "you": 1 if c0 > c1 else 0,
            "bot": 1 if c1 > c0 else 0,
        })

        den0 = sum(1 for c in p0.captured_cards if c.suit == Suit.DENARI)
        den1 = sum(1 for c in p1.captured_cards if c.suit == Suit.DENARI)
        rows.append({
            "label": f"Denari ♦  ({den0} vs {den1})",
            "you": 1 if den0 > den1 else 0,
            "bot": 1 if den1 > den0 else 0,
        })

        seven_d = Card(Suit.DENARI, Rank.SEVEN)
        rows.append({
            "label": "7♦  Seba' Denari",
            "you": 1 if seven_d in p0.captured_cards else 0,
            "bot": 1 if seven_d in p1.captured_cards else 0,
        })

        b0, b1 = tunisian_barmila_points(p0.captured_cards, p1.captured_cards)
        rows.append({"label": "Barmila", "you": b0, "bot": b1})

        rows.append({
            "label": "Chkobba ✦",
            "you": p0.chkobbas,
            "bot": p1.chkobbas,
        })
        return rows

    def _remove_card_from_slots(self, card: Card) -> None:
        """Mark the first matching slot as empty."""
        for i, slot in enumerate(self.table_slots):
            if slot == card:
                self.table_slots[i] = None
                return

    @staticmethod
    def _init_table_slots() -> list[Card | None]:
        return [None] * TABLE_INITIAL_SLOT_COUNT

    @staticmethod
    def _column_fill_priority(col_index: int) -> int:
        if col_index < len(TABLE_COLUMN_PRIORITIES):
            return TABLE_COLUMN_PRIORITIES[col_index]
        return 10 + col_index

    def _ensure_slot_index(self, idx: int) -> None:
        if idx >= len(self.table_slots):
            self.table_slots.extend([None] * (idx + 1 - len(self.table_slots)))

    def _first_empty_slot_by_priority(self) -> int | None:
        """Return the empty slot index with lowest column priority (top before bottom)."""
        num_cols = max(TABLE_INITIAL_COLUMNS, (len(self.table_slots) + 1) // 2)
        candidates: list[tuple[int, int, int, int]] = []
        for j in range(num_cols):
            pri = self._column_fill_priority(j)
            for half in (0, 1):
                idx = 2 * j + half
                self._ensure_slot_index(idx)
                if self.table_slots[idx] is None:
                    candidates.append((pri, j, half, idx))
        if not candidates:
            return None
        candidates.sort(key=lambda t: (t[0], t[1], t[2]))
        return candidates[0][3]

    def _populate_table_slots_from_cards(self, cards: list[Card]) -> None:
        """Lay out table cards into the five-column grid using fill priority."""
        self.table_slots = self._init_table_slots()
        for card in cards:
            self._place_card_in_slots(card, "deal")

    def _place_card_in_slots(self, card: Card, played_by: str = 'deal') -> int:
        """Place the card in the highest-priority empty slot (see TABLE_COLUMN_PRIORITIES).

        Slots are paired per column: indices ``2*j`` and ``2*j+1`` are top/bottom of
        column ``j`` (0 = left, 2 = center). Opening deal keeps the center column
        empty until inner columns are filled. ``played_by`` is kept for callers.
        Returns the slot index used.
        """
        idx = self._first_empty_slot_by_priority()
        if idx is None:
            self.table_slots.extend([None, None])
            idx = len(self.table_slots) - 2
        self.table_slots[idx] = card
        return idx

    def _sync_table_slots_after_move(self, move: Move, played_by: str = 'deal') -> None:
        """Update UI slots to preserve table positions across captures."""
        if move.is_capture:
            for captured in move.captured_cards:
                self._remove_card_from_slots(captured)
        else:
            self._place_card_in_slots(move.played_card, played_by)

        # Safety: if slot cards don't match real table cards, rebuild with column priority.
        slot_cards = [c for c in self.table_slots if c is not None]
        if self.state is not None and sorted(slot_cards, key=str) != sorted(self.state.table_cards, key=str):
            self._populate_table_slots_from_cards(list(self.state.table_cards))

    def _is_human_turn(self, seat: int | None = None) -> bool:
        """Check if it's the specified seat's turn to move.

        If *seat* is None, defaults to ``self.human_id`` (solo-mode behaviour).
        For multiplayer, pass the viewer's seat.
        """
        check_seat = self.human_id if seat is None else seat
        return (
            self.phase == GamePhase.PLAYING_HUMAN
            and self.state is not None
            and self.pending_bot_move is None
            and self.state.current_player == check_seat
        )

    def _apply_human_move(self, move_index: int) -> None:
        """Apply a human move by index from legal moves list. Includes detailed validation logging."""
        if self.phase not in (GamePhase.PLAYING_HUMAN, GamePhase.BOT_MOVING):
            logger.warning("Attempted move during phase: %s", self.phase)
            self.messages.append("Game is over.")
            return
            
        if self.state is None:
            logger.warning("Attempted move with no active game state")
            self.messages.append("Game is over.")
            return
            
        if not self._is_human_turn():
            logger.warning("Move attempt on non-human turn. Current player: %s", 
                          self.state.current_player if self.state else "None")
            self.messages.append("Not your turn!")
            return
            
        legal = self.state.legal_moves()
        if not (0 <= move_index < len(legal)):
            logger.warning("Invalid move index %d (legal moves: %d)", move_index, len(legal))
            self.messages.append("Invalid move index.")
            return
            
        move = legal[move_index]
        logger.info("Human player executing move: %s (index %d of %d)", 
                   describe_move(move), move_index, len(legal))
        self._execute_human_move(move)

    def _apply_selected_move(
        self, hand_index: int, table_indices: list[int], seat: int | None = None
    ) -> bool:
        """Apply a move built from user-selected cards.

        *seat* identifies the acting player (0 or 1).  Defaults to
        ``self.human_id`` for backward-compat with solo mode.
        Returns True if move was applied, False if invalid.
        """
        acting_seat = self.human_id if seat is None else seat

        if self.phase == GamePhase.CUT_DECISION:
            flash("Choose what to do with the cut card first.")
            return False

        if self.phase not in (GamePhase.PLAYING_HUMAN, GamePhase.BOT_MOVING):
            logger.warning("Attempted selected move during phase: %s", self.phase)
            flash("Game is over.")
            return False
            
        if self.state is None:
            logger.warning("Attempted selected move with no active game state")
            flash("Game is over.")
            return False
            
        if not self._is_human_turn(acting_seat):
            logger.warning("Selected move attempt on non-human turn (seat=%s)", acting_seat)
            flash("Not your turn!")
            return False

        player = self.state.players[acting_seat]

        # Validate hand index
        if not (0 <= hand_index < len(player.hand)):
            logger.warning("Invalid hand index %d (hand size: %d)", hand_index, len(player.hand))
            flash("Invalid hand card selected.")
            return False

        played_card = player.hand[hand_index]

        # Validate table indices
        invalid_table_idx = [i for i in table_indices if i < 0 or i >= len(self.state.table_cards)]
        if invalid_table_idx:
            logger.warning("Invalid table indices %s (table size: %d)", 
                          invalid_table_idx, len(self.state.table_cards))
            flash("Invalid table card selected.")
            return False

        captured_cards = [self.state.table_cards[i] for i in table_indices]

        # Build the selected move
        selected_move = Move(played_card=played_card, captured_cards=tuple(captured_cards))

        # Find matching legal move (set comparison for captured cards)
        legal = self.state.legal_moves()
        matching = None
        for legal_move in legal:
            if (
                legal_move.played_card == selected_move.played_card
                and set(legal_move.captured_cards) == set(selected_move.captured_cards)
            ):
                matching = legal_move
                break

        if matching is None:
            logger.warning(
                "Selected move not in legal moves. Played: %s, Captured: %s (legal moves: %d)",
                card_to_str(played_card),
                [card_to_str(c) for c in captured_cards],
                len(legal),
            )
            flash("Illegal move. Please select a valid capture.")
            return False

        logger.info("Human player executing selected move: %s", describe_move(matching))
        self._execute_human_move(matching, seat=acting_seat)
        return True

    def _execute_human_move(self, move: Move, seat: int | None = None) -> None:
        """Execute a validated human move.

        For solo mode: queues a bot move afterwards.
        For 1v1 mode: simply switches turn (no bot).
        *seat* defaults to ``self.human_id`` for backward-compat.
        """
        acting_seat = self.human_id if seat is None else seat
        # Slot placement: seat 0 → 'human' (bottom row), seat 1 → 'bot' (top row).
        slot_side = 'human' if acting_seat == 0 else 'bot'

        self.last_human_move = describe_move(move)
        before_chk = self.state.players[acting_seat].chkobbas
        self.state.apply_move(move)
        if self.state.players[acting_seat].chkobbas > before_chk:
            self._queue_commentary("human_chkobba")
        self._sync_table_slots_after_move(move, slot_side)
        self.messages.append(f"Seat {acting_seat} {self.last_human_move}.")

        # Track played table card for animation
        if not move.is_capture:
            self.last_played_card = card_to_str(move.played_card)
            self.last_played_by = slot_side
            self.last_human_played_table_index = None
            for i, slot in enumerate(self.table_slots):
                if slot == move.played_card:
                    self.last_human_played_table_index = i
                    break

        # Check if round ended
        if self.state.is_round_over:
            self._finish_round()
            return

        if self.mode == "solo":
            # Solo: queue the bot move for animation.
            self.phase = GamePhase.BOT_MOVING
            self._queue_bot_move()
        else:
            # 1v1: both players are human — just keep PLAYING_HUMAN.
            self.phase = GamePhase.PLAYING_HUMAN
        self.save()

    def _queue_bot_move(self) -> None:
        """Compute bot move and store it for animation phase."""
        if self.state is None or self.phase not in (GamePhase.BOT_MOVING, GamePhase.PLAYING_BOT):
            logger.warning("Queue bot move called during invalid phase: %s", self.phase)
            return
        if self.state.current_player != self.bot_id:
            logger.warning("Unexpected turn state: current player is %s, not bot %s", 
                          self.state.current_player, self.bot_id)
            self.messages.append("Unexpected turn state.")
            return

        move = get_heuristic_move(self.state, verbose=app.config["DEBUG_MODE"])
        self.pending_bot_move = move
        self.last_bot_move = describe_move(move)
        self.pending_bot_played_card = card_to_str(move.played_card)
        self.pending_bot_is_capture = move.is_capture
        self.pending_bot_is_chkobba = (
            move.is_capture and len(move.captured_cards) == len(self.state.table_cards)
        )

        # Compute table indices that will be captured
        self.pending_bot_captured_indices = []
        used = set()
        for cap_card in move.captured_cards:
            for i, t_card in enumerate(self.state.table_cards):
                if t_card == cap_card and i not in used:
                    self.pending_bot_captured_indices.append(i)
                    used.add(i)
                    break

    def commit_bot_move(self) -> None:
        """Apply the pending bot move after animation delay."""
        if self.pending_bot_move is None or self.state is None:
            return

        if not self.pending_bot_move.is_capture:
            self.last_played_card = card_to_str(self.pending_bot_move.played_card)
            self.last_played_by = "bot"
        before_chk = self.state.players[self.bot_id].chkobbas
        self.state.apply_move(self.pending_bot_move)
        if self.state.players[self.bot_id].chkobbas > before_chk:
            self._queue_commentary("bot_chkobba")
        self._sync_table_slots_after_move(self.pending_bot_move, 'bot')
        self.messages.append(f"Bot {self.last_bot_move}.")
        self.pending_bot_move = None
        self.pending_bot_captured_indices = []
        self.pending_bot_played_card = None
        self.pending_bot_is_capture = False
        self.last_human_played_table_index = None

        if self.state.is_round_over:
            self._finish_round()
        else:
            # Bot move is done; hand control back to the human player.
            self.phase = GamePhase.PLAYING_HUMAN
            self.save()

    def _finish_round(self) -> None:
        """End the round, update match scores, check for match winner."""
        self.round_breakdown = self._compute_round_breakdown()
        round_points = self.state.round_points()
        self.final_points = round_points
        self.match_scores[0] += round_points[0]
        self.match_scores[1] += round_points[1]
        if self.state is not None:
            self.state.match_scores[0] = self.match_scores[0]
            self.state.match_scores[1] = self.match_scores[1]
        # Stage round-end commentary (chkobba events take priority)
        if self.pending_commentary is None:
            diff = abs(round_points[0] - round_points[1])
            if diff <= 1:
                self._queue_commentary("close_call")
            elif round_points[0] > round_points[1]:
                self._queue_commentary("player_wins_round")
            else:
                self._queue_commentary("bot_wins_round")
        
        # Track round for persistence
        self.round_history.append((round_points[0], round_points[1]))

        logger.info("Round finished. Points: You %d - Bot %d. Match total: %d - %d",
                   round_points[0], round_points[1],
                   self.match_scores[0], self.match_scores[1])

        self.messages.append(
            f"Round: You {round_points[0]} - Bot {round_points[1]}"
        )
        self.messages.append(
            f"Match: You {self.match_scores[0]} - Bot {self.match_scores[1]}"
        )

        ts = _coerce_target_score(self.target_score)
        match_points_capped = False
        if ts is not None:
            match_points_capped = self.match_scores[0] >= ts or self.match_scores[1] >= ts
        else:
            logger.error(
                "Round ended but target_score is unset; match will not auto-end. room=%s match=%s",
                self.room_id,
                self.match_scores,
            )

        # Check if match is over
        if match_points_capped:
            self.phase = GamePhase.MATCH_OVER
            if self.match_scores[0] > self.match_scores[1]:
                self.match_winner = 0
                logger.info("Match over. Human wins %d-%d", self.match_scores[0], self.match_scores[1])
            elif self.match_scores[1] > self.match_scores[0]:
                self.match_winner = 1
                logger.info("Match over. Bot wins %d-%d", self.match_scores[1], self.match_scores[0])
            else:
                self.match_winner = None  # Tie
                logger.info("Match over. Tie %d-%d", self.match_scores[0], self.match_scores[1])
            
            # Save match to database
            self._persist_match()
            # Persist final state so the user sees the match-over screen
            # after a page reload; admin cleanup removes it after TTL.
            self.save()
            return

        self.phase = GamePhase.ROUND_OVER
        self.save()
        
    def _guest_identity_for_persist(self) -> tuple[str, str]:
        """Resolve guest id + display name (works in HTTP and socket background tasks)."""
        guest_id = ""
        display_name = "Guest"
        try:
            guest_id = session.get("guest_id", "") or ""
            display_name = session.get("display_name", "Guest") or "Guest"
        except RuntimeError:
            pass
        if self._store and self.room_id and self.room_id != "__no_room__":
            blob = self._store.get(self.room_id)
            if blob:
                for p in blob.get("players", []):
                    if not p.get("is_bot"):
                        guest_id = p.get("guest_id") or guest_id
                        display_name = p.get("display_name") or display_name
                        break
        return guest_id, display_name

    def _persist_match(self) -> None:
        """Save the completed match to the database."""
        if self.match_start_time is None or self.target_score is None:
            logger.warning("Cannot persist match: missing start time or target score")
            return

        try:
            duration = int(time() - self.match_start_time)
            save_match(
                target_score=self.target_score,
                human_score=self.match_scores[0],
                bot_score=self.match_scores[1],
                winner=self.match_winner,
                duration_seconds=duration,
                round_scores=self.round_history,
            )
            logger.info("Match persisted to database")
        except Exception as e:
            logger.error("Failed to persist match: %s", e)

        # Personal history page (mp_matches) — solo and multiplayer.
        guest_id, display_name = self._guest_identity_for_persist()
        if not guest_id or self.room_id in ("", "__no_room__"):
            return

        from models.db import record_match

        if self.mode == "solo":
            players_for_db = [
                {"guest_id": guest_id, "display_name": display_name, "seat": 0},
                {"guest_id": "bot", "display_name": BOT_DEFAULT_NAME, "seat": 1},
            ]
            winner_guest_id: str | None
            if self.match_winner == 0:
                winner_guest_id = guest_id
            elif self.match_winner == 1:
                winner_guest_id = "bot"
            else:
                winner_guest_id = None
            try:
                record_match(
                    room_id=self.room_id,
                    mode="solo",
                    players=players_for_db,
                    scores=list(self.match_scores),
                    winner_guest_id=winner_guest_id,
                )
            except Exception as e:
                logger.error("Failed to record solo match history: %s", e)

    def _view_data_cut_phase(self, viewer_seat: int, debug: bool, is_solo: bool) -> dict:
        """Snapshot while waiting for the cutter to choose keep vs discard."""
        assert self.state is not None and self.opening_cut_index is not None
        my_id = viewer_seat
        opp_id = 1 - viewer_seat
        human = self.state.players[my_id]
        opp = self.state.players[opp_id]
        cut_card_obj = self.state.deck[self.opening_cut_index]
        cut_str = card_to_str(cut_card_obj)
        is_cutter = my_id == self.state.current_player

        opp_name = BOT_DEFAULT_NAME
        opp_mood = self._get_bot_mood() if is_solo else "😐"
        room_blob = self._store.get(self.room_id) if self._store else None
        if not is_solo and room_blob:
            opp_p = _player_at_seat(room_blob, opp_id)
            if opp_p:
                opp_name = opp_p.get("display_name", "Opponent")

        return {
            "show_start_screen": False,
            "awaiting_cut_choice": True,
            "cut_card": cut_str if is_cutter else None,
            "is_opening_cutter": is_cutter,
            "debug": debug,
            "debug_data": {},
            "capture_map": {},
            "target_score": self.target_score,
            "match_scores": self.match_scores,
            "match_over": self.phase == GamePhase.MATCH_OVER,
            "match_winner": self.match_winner,
            "show_next_round": False,
            "has_pending_bot": False,
            "opp_first_js_deal": False,
            "last_human_played_table_index": None,
            "pending_bot_captured_indices": [],
            "pending_bot_played_card": None,
            "pending_bot_is_capture": False,
            "pending_bot_is_chkobba": False,
            "last_played_card": None,
            "last_played_by": None,
            "table_cards": [],
            "table_slots": [],
            "human_hand": [],
            "human_captured_count": len(human.captured_cards),
            "human_chkobbas": human.chkobbas,
            "human_is_current": is_cutter,
            "bot_name": opp_name,
            "bot_mood": opp_mood,
            "bot_avatar_url": _opponent_avatar_url(
                room_blob, opp_id, is_solo=is_solo, opp_name=opp_name
            ),
            **_opponent_ui_fields(
                room_blob, opp_id, is_solo=is_solo, opp_name=opp_name
            ),
            "bot_hand_count": len(opp.hand),
            "bot_captured_count": len(opp.captured_cards),
            "bot_chkobbas": opp.chkobbas,
            "move_buttons": [],
            "round_over": False,
            "messages": self.messages[-6:],
            "last_human_move": self.last_human_move,
            "last_bot_move": self.last_bot_move,
            "final_points": self.final_points,
            "round_breakdown": self.round_breakdown,
            "commentary_toast": self._consume_commentary(),
            "deck_remaining": len(self.state.deck),
            "round_moves_played": 0,
            "room_id": self.room_id,
            "my_seat": viewer_seat,
            "game_mode": self.mode,
            "game_active": True,
            "round_end_sweep": None,
        }

    def view_data(self, viewer_seat: int = 0) -> dict:
        """Build a dict for the Jinja template / SocketIO state snapshot.

        *viewer_seat* (0 or 1) controls the perspective:
          0 → player 0's hand is shown as "human_hand" (default, solo mode)
          1 → player 1's hand is shown as "human_hand" (multiplayer player 1)
        The "bot" fields always represent the *opponent* from the viewer's POV.
        """
        debug = app.config["DEBUG_MODE"]
        is_solo = (self.mode == "solo")

        # Start screen (no active game yet)
        if self.state is None:
            return {
                "show_start_screen": True,
                "target_scores": TARGET_SCORES,
                "debug": debug,
                "match_scores": [0, 0],
                "target_score": None,
                "has_saved_session": False,
                "room_id": self.room_id,
                "my_seat": viewer_seat,
                "game_mode": self.mode,
            }

        if self.phase == GamePhase.CUT_DECISION and self.opening_cut_index is not None:
            return self._view_data_cut_phase(viewer_seat, debug, is_solo)

        my_id  = viewer_seat
        opp_id = 1 - viewer_seat

        # One-shot: leftover table cards swept to last capturer (for client animation).
        round_end_sweep_view: dict | None = None
        if self.state.round_end_sweep is not None:
            recv_seat, swept = self.state.round_end_sweep
            round_end_sweep_view = {
                "receiver_seat": recv_seat,
                "to_human_side": recv_seat == my_id,
                "cards": [card_to_str(c) for c in swept],
            }
            self.state.round_end_sweep = None

        human = self.state.players[my_id]
        opp   = self.state.players[opp_id]

        legal = self.state.legal_moves()
        move_buttons = []
        if self._is_human_turn(my_id):
            for i, move in enumerate(legal):
                if move.is_capture:
                    cap = ", ".join(card_to_str(c) for c in move.captured_cards)
                    label = f"{card_to_str(move.played_card)} → capture [{cap}]"
                else:
                    label = f"{card_to_str(move.played_card)} → table"
                move_buttons.append({"index": i, "label": label})

        # Capture map: hand_index -> list of {table_indices, is_capture}
        capture_map = {}
        if self._is_human_turn(my_id):
            for move in legal:
                hand_idx, table_idxs = _move_to_indices(self.state, move, my_id)
                capture_map.setdefault(hand_idx, []).append({
                    "table_indices": table_idxs,
                    "is_capture": move.is_capture,
                })

        # Opponent display name — use real name from room blob for multiplayer.
        opp_name = BOT_DEFAULT_NAME
        opp_mood = self._get_bot_mood() if is_solo else "😐"
        room_blob = self._store.get(self.room_id) if self._store else None
        if not is_solo and room_blob:
            opp_p = _player_at_seat(room_blob, opp_id)
            if opp_p:
                opp_name = opp_p.get("display_name", "Opponent")

        # Debug info
        debug_data = {}
        if debug:
            debug_data = {
                "all_legal_moves": [describe_move(m) for m in legal],
                "current_player": self.state.current_player,
                "deck_count": len(self.state.deck),
                "table_count": len(self.state.table_cards),
                "last_capturer": self.state.last_capturer,
                "bot_hand": [card_to_str(c) for c in opp.hand],
            }

        # Map each visual slot → compact table index for move validation.
        slot_table_indices: list[int | None] = [None] * len(self.table_slots)
        used_slots: set[int] = set()
        for table_idx, table_card in enumerate(self.state.table_cards):
            for slot_idx, slot_card in enumerate(self.table_slots):
                if slot_idx in used_slots:
                    continue
                if slot_card == table_card:
                    slot_table_indices[slot_idx] = table_idx
                    used_slots.add(slot_idx)
                    break

        # Pending-bot fields are only meaningful in solo mode.
        has_pending_bot        = is_solo and (self.pending_bot_move is not None)
        pending_bot_played_card    = self.pending_bot_played_card if is_solo else None
        pending_bot_captured_idxs  = self.pending_bot_captured_indices if is_solo else []
        pending_bot_is_capture     = self.pending_bot_is_capture if is_solo else False
        pending_bot_is_chkobba     = self.pending_bot_is_chkobba if is_solo else False

        human_is_current = self.state.current_player == my_id

        # Solo: bot leads with a pending *opening* move — client runs empty-board → deal animation.
        # Must be gated on move_history == 0 so mid-round bot turns (also BOT_MOVING + pending)
        # do not flip SSR / flags, and so a follow-up state_snapshot (e.g. vd_pending) does not
        # look like a "fresh deal" client-side (isDeal false) and repaint the full board.
        opp_first_js_deal = bool(
            is_solo
            and has_pending_bot
            and not human_is_current
            and len(human.hand) > 0
            and len(opp.hand) > 0
            and len(self.state.move_history) == 0
        )

        # last_played_by is stored in absolute seat terms (seat 0 -> "human",
        # seat 1 -> "bot").  In multiplayer, seat 1's UI is mirrored by
        # view_data(), so we remap this flag to viewer-relative terms.
        last_played_by_view = self.last_played_by
        if not is_solo and viewer_seat == 1:
            if self.last_played_by == "human":
                last_played_by_view = "bot"
            elif self.last_played_by == "bot":
                last_played_by_view = "human"

        return {
            "show_start_screen": False,
            "debug": debug,
            "debug_data": debug_data,
            "capture_map": capture_map,
            "target_score": self.target_score,
            "match_scores": self.match_scores,
            "match_over": self.phase == GamePhase.MATCH_OVER,
            "match_winner": self.match_winner,
            "show_next_round": self.phase == GamePhase.ROUND_OVER,
            "has_pending_bot": has_pending_bot,
            "opp_first_js_deal": opp_first_js_deal,
            "last_human_played_table_index": self.last_human_played_table_index,
            "pending_bot_captured_indices": pending_bot_captured_idxs,
            "pending_bot_played_card": pending_bot_played_card,
            "pending_bot_is_capture": pending_bot_is_capture,
            "pending_bot_is_chkobba": pending_bot_is_chkobba,
            "last_played_card": self.last_played_card,
            "last_played_by": last_played_by_view,

            # Table
            "table_cards": [card_to_str(c) for c in self.state.table_cards],
            "table_slots": [
                {
                    "card": card_to_str(c) if c is not None else None,
                    "table_index": slot_table_indices[i],
                }
                for i, c in enumerate(self.table_slots)
            ],

            # Viewer's hand ("human")
            "human_hand": [card_to_str(c) for c in human.hand],
            "human_captured_count": len(human.captured_cards),
            "human_chkobbas": human.chkobbas,
            "human_is_current": human_is_current,

            # Opponent ("bot" slot — could be the actual bot or the other human)
            "bot_name": opp_name,
            "bot_mood": opp_mood,
            "bot_avatar_url": _opponent_avatar_url(
                room_blob, opp_id, is_solo=is_solo, opp_name=opp_name
            ),
            **_opponent_ui_fields(
                room_blob, opp_id, is_solo=is_solo, opp_name=opp_name
            ),
            "bot_hand_count": len(opp.hand),
            "bot_captured_count": len(opp.captured_cards),
            "bot_chkobbas": opp.chkobbas,

            # Moves
            "move_buttons": move_buttons,
            "round_over": self.phase in (GamePhase.ROUND_OVER, GamePhase.MATCH_OVER),

            # Messages
            "messages": self.messages[-6:],
            "last_human_move": self.last_human_move,
            "last_bot_move": self.last_bot_move,

            # Final score
            "final_points": self.final_points,
            "round_breakdown": self.round_breakdown,

            # Commentary toast (consumed once per page render)
            "commentary_toast": self._consume_commentary(),

            # Meta
            "deck_remaining": len(self.state.deck),

            # Plays this round — client uses 0 vs >0 to tell opening deal from mid-round hand refresh.
            "round_moves_played": len(self.state.move_history),

            # SocketIO routing
            "room_id": self.room_id,
            "my_seat": viewer_seat,
            "game_mode": self.mode,

            "awaiting_cut_choice": False,
            "cut_card": None,
            "is_opening_cutter": False,
            "game_active": True,
            "round_end_sweep": round_end_sweep_view,
        }


# ---------------------------------------------------------------------------
# Store-aware session helpers
# ---------------------------------------------------------------------------

def _get_solo_room_id() -> str:
    """Return the solo room_id for this browser session, creating one if absent."""
    room_id = session.get("solo_room_id")
    if not room_id:
        room_id = secrets.token_hex(16)
        session["solo_room_id"] = room_id
    return room_id


def _get_manager() -> GameManager:
    """Load the current game from the store, or return a fresh MENU manager.

    If the store has the room, the game is resumed transparently.
    If the room has expired or the user has no session, shows the start screen.
    """
    room_id = session.get("solo_room_id")
    if room_id:
        try:
            return GameManager.load(room_id, app.game_store)
        except RoomNotFoundError:
            # Room expired in the store — clear the stale session key.
            session.pop("solo_room_id", None)
    # No room or expired → fresh manager shows the start screen.
    return GameManager(room_id="__no_room__", store=app.game_store)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if not IS_PRODUCTION:
        # Local only: toggle debug panel via ?debug=1 or ?debug=0
        debug_param = request.args.get("debug")
        if debug_param == "1":
            app.config["DEBUG_MODE"] = True
        elif debug_param == "0":
            app.config["DEBUG_MODE"] = False

    manager = _get_manager()
    # ?next_round=1 triggers auto-transition (used by "Next Round" button)
    if (
        request.args.get("next_round") == "1"
        and manager.state is not None
        and manager.phase == GamePhase.ROUND_OVER
    ):
        manager.next_round()
    ctx = manager.view_data()

    _apply_guest_profile(ctx)

    guest_id = session.get("guest_id", "")
    from models.social import list_friend_ids

    ctx["friends"] = _friends_list_for_session()

    return render_template("index.html", **ctx)


    # /history route is defined later in the multiplayer section


@app.route("/move/<int:move_index>", methods=["POST"])
@csrf_protect
def move(move_index: int):
    manager = _get_manager()
    manager._apply_human_move(move_index)
    return redirect(url_for("index"))


@app.route("/play_selected", methods=["POST"])
@csrf_protect
def play_selected():
    manager = _get_manager()
    hand_index_str = request.form.get("hand_index", "")
    table_indices_str = request.form.get("table_indices", "")

    try:
        hand_index = int(hand_index_str)
    except (ValueError, TypeError):
        flash("Invalid hand selection.")
        return redirect(url_for("index"))

    table_indices = []
    if table_indices_str:
        try:
            table_indices = [int(x) for x in table_indices_str.split(",") if x]
        except (ValueError, TypeError):
            flash("Invalid table selection.")
            return redirect(url_for("index"))

    manager._apply_selected_move(hand_index, table_indices)
    return redirect(url_for("index"))


@app.route("/start", methods=["POST"])
@csrf_protect
def start():
    target_score = request.form.get("target_score", type=int)
    if target_score not in TARGET_SCORES:
        flash("Invalid target score.")
        return redirect(url_for("index"))
    room_id = _get_solo_room_id()          # creates/reuses session entry
    GameManager.create(room_id, target_score, app.game_store)
    return redirect(url_for("index"))


@app.route("/resume", methods=["POST"])
@csrf_protect
def resume():
    # Game state is now auto-resumed from the store by _get_manager()
    # on every GET /.  This route is kept for backward-compat with any
    # existing "Resume" button in the template.
    return redirect(url_for("index"))


@app.route("/commit_bot", methods=["POST"])
@csrf_protect
def commit_bot():
    manager = _get_manager()
    manager.commit_bot_move()
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# JSON API endpoints (used by the AJAX game engine)
# ---------------------------------------------------------------------------

@app.route("/api/play", methods=["POST"])
@csrf_protect
def api_play():
    """Process a human move and return the new game state as JSON."""
    manager = _get_manager()
    hand_index_str  = request.form.get("hand_index", "")
    table_indices_str = request.form.get("table_indices", "")

    try:
        hand_index = int(hand_index_str)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid hand selection"}), 400

    table_indices: list[int] = []
    if table_indices_str:
        try:
            table_indices = [int(x) for x in table_indices_str.split(",") if x]
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid table selection"}), 400

    manager._apply_selected_move(hand_index, table_indices)
    return jsonify(manager.view_data())


@app.route("/api/bot_move", methods=["POST"])
@csrf_protect
def api_bot_move():
    """Commit the pending bot move and return the new game state as JSON."""
    manager = _get_manager()
    manager.commit_bot_move()
    return jsonify(manager.view_data())


@app.route("/api/cut_choice", methods=["POST"])
@csrf_protect
def api_cut_choice():
    """Solo: cutter commits keep (Path A) or discard to table (Path B)."""
    manager = _get_manager()
    if manager.state is None:
        return jsonify({"error": "No game"}), 400
    keep_raw = (request.form.get("keep") or "").strip().lower()
    if keep_raw in ("1", "true", "yes", "keep"):
        keep_cut = True
    elif keep_raw in ("0", "false", "no", "discard", "table"):
        keep_cut = False
    else:
        return jsonify({"error": "Invalid keep (use true or false)"}), 400
    ok = manager.commit_opening_cut_choice(keep_cut, acting_seat=manager.human_id)
    if not ok:
        return jsonify({"error": "Cannot apply cut choice"}), 400
    return jsonify(manager.view_data())


@app.route("/api/profile/name", methods=["POST"])
def api_update_name():
    """Update the guest's display name.  Accepts JSON: {name, _csrf_token}."""
    data = request.get_json(force=True, silent=True) or {}

    # CSRF validation (read token from JSON body)
    token = session.get("_csrf_token")
    if not token or not hmac.compare_digest(token, str(data.get("_csrf_token", ""))):
        return jsonify({"error": "Invalid CSRF token"}), 403

    name = (data.get("name") or "").strip()[:30]
    if not name:
        return jsonify({"error": "Name cannot be empty"}), 400
    if not is_clean(name):
        return jsonify({"error": "Name contains inappropriate words"}), 400

    guest_id = session.get("guest_id")
    if not guest_id:
        return jsonify({"error": "No guest session"}), 401

    avatar_key_raw = data.get("avatar_key")
    avatar_key: str | None
    if avatar_key_raw is None:
        avatar_key = _resolve_guest_avatar_key(guest_id)
    elif avatar_key_raw == "":
        avatar_key = None
        session.pop("avatar_key", None)
        _update_avatar_key(guest_id, None)
    elif is_valid_player_avatar_key(str(avatar_key_raw)):
        avatar_key = str(avatar_key_raw)
        session["avatar_key"] = avatar_key
        _update_avatar_key(guest_id, avatar_key)
    else:
        return jsonify({"error": "Invalid avatar choice"}), 400

    _update_display_name(guest_id, name)
    session["display_name"] = name
    _sync_guest_profile_to_rooms(guest_id, name, avatar_key)
    logger.info("Guest %.8s changed name to %r avatar=%r", guest_id, name, avatar_key)
    return jsonify({
        "ok": True,
        "guest_id": guest_id,
        "name": name,
        "avatar_key": avatar_key,
        "avatar_url": _player_avatar_url(avatar_key),
        "avatar_initial": name[0].upper() if name else "G",
        "avatar_color": avatar_color(guest_id),
    })


@app.route("/api/session/restore", methods=["POST"])
def api_session_restore():
    """Re-attach a previous guest_id (e.g. from localStorage after cookies cleared)."""
    data = request.get_json(force=True, silent=True) or {}
    guest_id = (data.get("guest_id") or "").strip()
    if not guest_id or len(guest_id) > 64:
        return jsonify({"error": "Invalid guest id"}), 400
    guest = _get_guest(guest_id)
    if not guest:
        return jsonify({"error": "Guest not found"}), 404

    session.permanent = True
    session["guest_id"] = guest_id
    _hydrate_guest_profile_from_db(guest_id)
    logger.info("Restored guest session %.8s", guest_id)
    return jsonify({
        "ok": True,
        "guest_id": guest_id,
        "name": session.get("display_name"),
        "avatar_key": session.get("avatar_key"),
        "avatar_url": _player_avatar_url(session.get("avatar_key")),
        "avatar_initial": (session.get("display_name") or "G")[0].upper(),
        "avatar_color": avatar_color(guest_id),
    })


@app.route("/api/players/<opponent_guest_id>/summary")
def api_player_summary(opponent_guest_id: str):
    """Head-to-head stats vs the current user (for opponent profile popup)."""
    _ensure_guest_session()
    viewer_id = session.get("guest_id", "")
    if not viewer_id:
        return jsonify({"error": "No guest session"}), 401
    if opponent_guest_id == viewer_id:
        return jsonify({"error": "Cannot view your own summary"}), 400
    if opponent_guest_id in ("bot", "") or len(opponent_guest_id) > 64:
        return jsonify({"error": "Invalid player"}), 400

    from models.social import get_head_to_head_stats, get_friend_relation
    from models.guests import get_guest as _get_guest_row

    opp = _get_guest_row(opponent_guest_id)
    display_name = "Player"
    avatar_key = None
    if opp:
        display_name = (opp.get("display_name") or "Player").strip()
        avatar_key = opp.get("avatar_key")

    # Fallback name from active room if guest row is stale.
    room_id = request.args.get("room_id")
    if room_id:
        blob = app.game_store.get(room_id)
        if blob:
            for p in blob.get("players", []):
                if p.get("guest_id") == opponent_guest_id:
                    display_name = p.get("display_name") or display_name
                    if p.get("avatar_key"):
                        avatar_key = p.get("avatar_key")
                    break

    h2h = get_head_to_head_stats(viewer_id, opponent_guest_id)
    rel = get_friend_relation(viewer_id, opponent_guest_id)
    return jsonify({
        "guest_id": opponent_guest_id,
        "display_name": display_name,
        "avatar_url": _player_avatar_url(avatar_key),
        "avatar_initial": display_name[0].upper() if display_name else "?",
        "avatar_color": avatar_color(opponent_guest_id),
        "head_to_head": h2h,
        "is_friend": rel["status"] == "friends",
        "friend_status": rel["status"],
        "friend_request_id": rel["request_id"],
        "is_self": False,
    })


def _friends_list_for_session() -> list[dict]:
    """Accepted friends for invite UI (lobby, home, waiting room)."""
    guest_id = session.get("guest_id", "")
    from models.social import list_friend_ids

    return [_guest_notify_payload(fid) for fid in list_friend_ids(guest_id)]


def _guest_notify_payload(guest_id: str) -> dict:
    """Minimal profile for socket notifications."""
    from models.guests import get_guest as _get_guest_row

    row = _get_guest_row(guest_id) if guest_id else None
    name = (row.get("display_name") if row else None) or "Player"
    key = row.get("avatar_key") if row else None
    return {
        "guest_id": guest_id,
        "display_name": name,
        "avatar_url": _player_avatar_url(key),
        "avatar_initial": name[0].upper() if name else "?",
        "avatar_color": avatar_color(guest_id),
    }


def _emit_friend_event(guest_id: str, event: str, payload: dict) -> None:
    from ui.sockets import emit_to_guest

    emit_to_guest(guest_id, event, payload)


@app.route("/api/friends/add", methods=["POST"])
def api_friends_add():
    """Send a friend request (other player must accept)."""
    _ensure_guest_session()
    data = request.get_json(force=True, silent=True) or {}
    token = session.get("_csrf_token")
    if not token or not hmac.compare_digest(token, str(data.get("_csrf_token", ""))):
        return jsonify({"error": "Invalid CSRF token"}), 403

    viewer_id = session.get("guest_id", "")
    friend_id = (data.get("guest_id") or "").strip()
    if not viewer_id or not friend_id:
        return jsonify({"error": "Missing guest id"}), 400
    if friend_id == viewer_id:
        return jsonify({"error": "Cannot add yourself"}), 400

    from models.social import send_friend_request, get_friend_relation

    rel = get_friend_relation(viewer_id, friend_id)
    if rel["status"] == "friends":
        return jsonify({"ok": True, "friend_status": "friends"})
    if rel["status"] == "pending_sent":
        return jsonify({"ok": True, "friend_status": "pending_sent"})

    result = send_friend_request(viewer_id, friend_id)
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "Could not send request")}), 400

    request_id = result["request_id"]
    _emit_friend_event(
        friend_id,
        "friend_request_received",
        {
            "request_id": request_id,
            "from": _guest_notify_payload(viewer_id),
        },
    )
    return jsonify({
        "ok": True,
        "friend_status": "pending_sent",
        "request_id": request_id,
    })


@app.route("/api/friends/respond", methods=["POST"])
def api_friends_respond():
    """Accept or decline an incoming friend request."""
    _ensure_guest_session()
    data = request.get_json(force=True, silent=True) or {}
    token = session.get("_csrf_token")
    if not token or not hmac.compare_digest(token, str(data.get("_csrf_token", ""))):
        return jsonify({"error": "Invalid CSRF token"}), 403

    viewer_id = session.get("guest_id", "")
    request_id = int(data.get("request_id") or 0)
    accept = bool(data.get("accept"))

    if not viewer_id or not request_id:
        return jsonify({"error": "Missing data"}), 400

    from models.social import respond_friend_request

    result = respond_friend_request(request_id, viewer_id, accept=accept)
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "Could not respond")}), 400

    sender_id = result["from_guest_id"]
    _emit_friend_event(
        sender_id,
        "friend_request_resolved",
        {
            "request_id": request_id,
            "accept": accept,
            "from": _guest_notify_payload(sender_id),
            "to": _guest_notify_payload(viewer_id),
            "friend_status": "friends" if accept else "declined",
        },
    )
    return jsonify({
        "ok": True,
        "accept": accept,
        "friend_status": "friends" if accept else "none",
    })


@app.route("/api/friends/requests", methods=["GET"])
def api_friends_requests():
    """Pending incoming friend requests."""
    _ensure_guest_session()
    viewer_id = session.get("guest_id", "")
    from models.social import list_incoming_friend_requests

    raw = list_incoming_friend_requests(viewer_id)
    out = []
    for r in raw:
        out.append({
            **r,
            "from": _guest_notify_payload(r["from_guest_id"]),
        })
    return jsonify({"requests": out})


@app.route("/api/friends/outgoing", methods=["GET"])
def api_friends_outgoing():
    """Pending outgoing friend requests sent by the current user."""
    _ensure_guest_session()
    viewer_id = session.get("guest_id", "")
    from models.social import list_outgoing_friend_requests

    raw = list_outgoing_friend_requests(viewer_id)
    out = []
    for r in raw:
        out.append({
            **r,
            "to": _guest_notify_payload(r["to_guest_id"]),
        })
    return jsonify({"requests": out})


@app.route("/api/friends/list", methods=["GET"])
def api_friends_list():
    """Friends list for invite UI."""
    _ensure_guest_session()
    viewer_id = session.get("guest_id", "")
    from models.social import list_friend_ids

    friends = [
        _guest_notify_payload(fid) for fid in list_friend_ids(viewer_id)
    ]
    return jsonify({"friends": friends})


@app.route("/api/friends/play-invite", methods=["POST"])
def api_friends_play_invite():
    """Invite a friend to a private 1v1 room."""
    _ensure_guest_session()
    data = request.get_json(force=True, silent=True) or {}
    token = session.get("_csrf_token")
    if not token or not hmac.compare_digest(token, str(data.get("_csrf_token", ""))):
        return jsonify({"error": "Invalid CSRF token"}), 403

    viewer_id = session.get("guest_id", "")
    friend_id = (data.get("guest_id") or "").strip()
    display_name = session.get("display_name", "Guest")
    target_score = int(data.get("target_score", 11))
    if target_score not in MP_TARGET_SCORES:
        target_score = 11

    if not viewer_id or not friend_id:
        return jsonify({"error": "Missing guest id"}), 400

    from models.social import is_friend

    if not is_friend(viewer_id, friend_id):
        return jsonify({"error": "You can only invite friends"}), 403

    blob = _create_mp_room("private", viewer_id, display_name, target_score)
    room_id = blob["room_id"]
    blob["friend_invite_to"] = friend_id
    blob["visibility"] = "friend_invite"
    app.game_store.set(room_id, blob)

    invite_id = secrets.token_hex(8)
    _friend_play_invites[invite_id] = {
        "invite_id": invite_id,
        "room_id": room_id,
        "from_guest_id": viewer_id,
        "to_guest_id": friend_id,
        "target_score": target_score,
        "status": "pending",
    }

    _emit_friend_event(
        friend_id,
        "play_invite_received",
        {
            "invite_id": invite_id,
            "room_id": room_id,
            "target_score": target_score,
            "from": _guest_notify_payload(viewer_id),
        },
    )
    return jsonify({"ok": True, "invite_id": invite_id, "room_id": room_id})


@app.route("/api/friends/play-invite/respond", methods=["POST"])
def api_friends_play_invite_respond():
    """Accept or decline a direct play invite."""
    _ensure_guest_session()
    data = request.get_json(force=True, silent=True) or {}
    token = session.get("_csrf_token")
    if not token or not hmac.compare_digest(token, str(data.get("_csrf_token", ""))):
        return jsonify({"error": "Invalid CSRF token"}), 403

    viewer_id = session.get("guest_id", "")
    invite_id = (data.get("invite_id") or "").strip()
    accept = bool(data.get("accept"))

    if not viewer_id or not invite_id:
        return jsonify({"error": "Missing data"}), 400

    inv = _friend_play_invites.get(invite_id)
    if not inv or inv.get("status") != "pending":
        return jsonify({"error": "Invite not found or expired"}), 404
    if inv["to_guest_id"] != viewer_id:
        return jsonify({"error": "Not authorized"}), 403

    inviter_id = inv["from_guest_id"]
    room_id = inv["room_id"]

    if not accept:
        inv["status"] = "declined"
        app.game_store.delete(room_id)
        _emit_friend_event(
            inviter_id,
            "play_invite_resolved",
            {"invite_id": invite_id, "accept": False, "to": _guest_notify_payload(viewer_id)},
        )
        return jsonify({"ok": True, "accept": False})

    blob = app.game_store.get(room_id)
    if blob is None:
        inv["status"] = "expired"
        return jsonify({"error": "Room expired"}), 404

    display_name = session.get("display_name", "Guest")
    from datetime import datetime, timezone

    presence = _player_presence_fields(viewer_id)
    blob["players"].append(
        {
            "guest_id": viewer_id,
            "display_name": display_name,
            "seat": 1,
            "is_bot": False,
            "connected": False,
            "sid": None,
            **presence,
        }
    )
    blob["last_action_at"] = datetime.now(timezone.utc).isoformat()
    blob["visibility"] = "private_matched"
    app.game_store.set(room_id, blob)
    inv["status"] = "accepted"

    _emit_friend_event(
        inviter_id,
        "play_invite_resolved",
        {
            "invite_id": invite_id,
            "accept": True,
            "room_id": room_id,
            "to": _guest_notify_payload(viewer_id),
        },
    )
    socketio.emit(
        "room_player_joined",
        {
            "seat": 1,
            "display_name": display_name,
            "guest_id": viewer_id,
            "avatar_initial": display_name[0].upper(),
            "avatar_color": avatar_color(viewer_id),
            "avatar_key": presence["avatar_key"],
            "avatar_url": presence["avatar_url"],
            "room_id": room_id,
        },
        to=room_id,
    )
    return jsonify({
        "ok": True,
        "accept": True,
        "room_id": room_id,
        "play_url": url_for("play_room", room_id=room_id),
    })


@app.route("/api/friends/remove", methods=["POST"])
def api_friends_remove():
    _ensure_guest_session()
    data = request.get_json(force=True, silent=True) or {}
    token = session.get("_csrf_token")
    if not token or not hmac.compare_digest(token, str(data.get("_csrf_token", ""))):
        return jsonify({"error": "Invalid CSRF token"}), 403

    viewer_id = session.get("guest_id", "")
    friend_id = (data.get("guest_id") or "").strip()
    if not viewer_id or not friend_id:
        return jsonify({"error": "Missing guest id"}), 400

    from models.social import remove_friend

    remove_friend(viewer_id, friend_id)
    return jsonify({"ok": True})


@app.route("/next_round", methods=["POST"])
@csrf_protect
def next_round():
    manager = _get_manager()
    if manager.phase == GamePhase.ROUND_OVER:
        manager.next_round()
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Multiplayer lobby routes (Phase 4 — Break 2)
# ---------------------------------------------------------------------------

def _create_mp_room(
    visibility: str,
    guest_id: str,
    display_name: str,
    target_score: int = 11,
) -> dict:
    """Create a multiplayer room blob with one player (the creator) and save it."""
    from datetime import datetime, timezone

    room_id = secrets.token_hex(12)
    now = datetime.now(timezone.utc).isoformat()
    presence = _player_presence_fields(guest_id)
    blob = {
        "room_id": room_id,
        "mode": "1v1",
        "visibility": visibility,
        "created_by": guest_id,
        "target_score_mp": target_score,
        "status": "waiting",
        "created_at": now,
        "last_action_at": now,
        "players": [
            {
                "guest_id": guest_id,
                "display_name": display_name,
                "seat": 0,
                "is_bot": False,
                "connected": False,
                "sid": None,
                **presence,
            }
        ],
        "game": {},
    }
    app.game_store.set(room_id, blob, ttl_seconds=7200)
    return blob


@app.route("/lobby")
def lobby():
    """Public lobby — lists open public rooms."""
    _ensure_guest_session()
    open_rooms = []
    for room_id in app.game_store.list_active():
        blob = app.game_store.get(room_id)
        if blob is None:
            continue
        if blob.get("mode") != "1v1":
            continue
        if blob.get("visibility") != "public":
            continue
        if blob.get("status") != "waiting":
            continue
        if len(blob.get("players", [])) >= 2:
            continue
        creator = blob["players"][0] if blob.get("players") else {}
        dn = creator.get("display_name", "?")
        open_rooms.append(
            {
                "room_id": room_id,
                "creator_name": dn,
                "creator_initial": dn[0].upper() if dn else "?",
                "creator_color": avatar_color(creator.get("guest_id", "")),
                "created_at": blob.get("created_at", ""),
                "target_score": blob.get("target_score_mp", 11),
            }
        )
    profile = _guest_profile_kwargs()
    return render_template(
        "lobby.html",
        **_merge_template_ctx(
            {"open_rooms": open_rooms, "friends": _friends_list_for_session()},
            profile,
        ),
    )


@app.route("/play/<room_id>")
def play_room(room_id: str):
    """Multiplayer game room — join if there is a free seat, else spectate."""
    _ensure_guest_session()
    guest_id = session.get("guest_id", "")
    display_name = session.get("display_name", "Guest")

    blob = app.game_store.get(room_id)
    profile = _guest_profile_kwargs()
    if blob is not None and blob.get("mode") == "1v1":
        if _ensure_blob_player_avatars(blob):
            app.game_store.set(room_id, blob)

    if blob is None or blob.get("mode") not in ("1v1", "solo"):
        return render_template(
            "play.html",
            **_merge_template_ctx(
                {
                    "room_not_found": True,
                    "room_full": False,
                    "blob": {"room_id": room_id, "players": [], "target_score_mp": 11, "chat": []},
                    "my_seat": 0,
                    "game_active": False,
                    "invite_url": None,
                },
                profile,
                _empty_game_ctx(),
            ),
        )

    # Solo bot room: skip seat-claiming logic — the human is always seat 0.
    if blob.get("mode") == "solo":
        game_active = blob.get("status") == "active"
        game_ctx: dict = {}
        if game_active:
            try:
                mgr = GameManager.load(room_id, app.game_store)
                game_ctx = mgr.view_data(viewer_seat=0)
            except Exception:
                game_active = False
                game_ctx = _empty_game_ctx()
        else:
            game_ctx = _empty_game_ctx()
            game_ctx["bot_name"] = BOT_DEFAULT_NAME
            game_ctx["bot_avatar_url"] = _bot_avatar_url(BOT_DEFAULT_NAME)
        # Strip keys that are passed explicitly to avoid duplicate-keyword errors.
        for _k in ("my_seat", "room_id", "game_mode", "show_start_screen", "game_active"):
            game_ctx.pop(_k, None)
        return render_template(
            "play.html",
            **_merge_template_ctx(
                {
                    "room_not_found": False,
                    "room_full": False,
                    "blob": blob,
                    "room_id": room_id,
                    "game_mode": "solo",
                    "my_seat": 0,
                    "game_active": game_active,
                    "invite_url": None,
                },
                profile,
                game_ctx,
            ),
        )

    # Find whether the visitor already has a seat.
    my_seat: int | None = None
    for p in blob.get("players", []):
        if p.get("guest_id") == guest_id:
            my_seat = p["seat"]
            break

    room_full = len(blob.get("players", [])) >= 2

    if my_seat is None and room_full:
        # Room is full — show a "game full" page.
        return render_template(
            "play.html",
            **_merge_template_ctx(
                {
                    "room_not_found": False,
                    "room_full": True,
                    "blob": blob,
                    "my_seat": 0,
                    "game_active": False,
                    "invite_url": None,
                },
                profile,
                _empty_game_ctx(),
            ),
        )

    if my_seat is None:
        # Join as player 1.
        from datetime import datetime, timezone
        my_seat = len(blob["players"])  # will be 1
        presence = _player_presence_fields(guest_id)
        blob["players"].append(
            {
                "guest_id": guest_id,
                "display_name": display_name,
                "seat": my_seat,
                "is_bot": False,
                "connected": False,
                "sid": None,
                **presence,
            }
        )
        blob["last_action_at"] = datetime.now(timezone.utc).isoformat()
        # If the room was public, remove it from lobby visibility now.
        if blob.get("visibility") == "public":
            blob["visibility"] = "private_matched"
        app.game_store.set(room_id, blob)
        # Notify any connected SocketIO clients in the room about the new player.
        socketio.emit(
            "room_player_joined",
            {
                "seat": my_seat,
                "display_name": display_name,
                "guest_id": guest_id,
                "avatar_initial": display_name[0].upper(),
                "avatar_color": avatar_color(guest_id),
                "avatar_key": presence["avatar_key"],
                "avatar_url": presence["avatar_url"],
                "room_id": room_id,
            },
            to=room_id,
        )
        # Announce removal to lobby viewers.
        socketio.emit(
            "lobby_update",
            {"action": "remove", "room_id": room_id},
            to="lobby",
        )

    game_active = blob.get("status") == "active"
    game_ctx: dict = {}
    if game_active:
        try:
            mgr = GameManager.load(room_id, app.game_store)
            # ?next_round=1 auto-transitions from ROUND_OVER to next round
            if request.args.get("next_round") == "1" and mgr.phase == GamePhase.ROUND_OVER:
                mgr.next_round()
            game_ctx = mgr.view_data(viewer_seat=my_seat)
        except Exception:
            game_active = False
            game_ctx = _empty_game_ctx()
    else:
        game_ctx = _empty_game_ctx()

    invite_url = (
        request.host_url.rstrip("/") + f"/play/{room_id}"
        if blob.get("visibility") in ("public", "private")
        else None
    )

    opp_ctx = _opp_ui_context(blob, my_seat)

    # Strip keys that are passed explicitly to avoid duplicate keyword arguments.
    for _k in (
        "my_seat", "room_id", "game_mode", "show_start_screen", "game_active",
        "opponent_guest_id", "opponent_is_human",
        "opponent_avatar_color", "opponent_avatar_initial",
    ):
        game_ctx.pop(_k, None)
    # Avoid duplicate **kwargs (PEP 448): only one source for opponent display fields.
    if game_active:
        for _k in ("bot_avatar_url", "bot_name"):
            opp_ctx.pop(_k, None)
    else:
        for _k in ("bot_avatar_url", "bot_name"):
            game_ctx.pop(_k, None)

    return render_template(
        "play.html",
        **_merge_template_ctx(
            {
                "room_not_found": False,
                "room_full": False,
                "blob": blob,
                "room_id": room_id,
                "game_mode": "1v1",
                "my_seat": my_seat,
                "game_active": game_active,
                "invite_url": invite_url,
                "friends": _friends_list_for_session(),
            },
            opp_ctx,
            profile,
            game_ctx,
        ),
    )


def _empty_game_ctx() -> dict:
    """Placeholder values for game-board Jinja2 variables when game hasn't started."""
    return {
        "show_start_screen": False,
        "debug": False,
        "debug_data": {},
        "capture_map": {},
        "target_score": None,
        "match_scores": [0, 0],
        "match_over": False,
        "match_winner": None,
        "show_next_round": False,
        "has_pending_bot": False,
        "opp_first_js_deal": False,
        "last_human_played_table_index": None,
        "pending_bot_captured_indices": [],
        "pending_bot_played_card": None,
        "pending_bot_is_capture": False,
        "pending_bot_is_chkobba": False,
        "last_played_card": None,
        "last_played_by": None,
        "table_cards": [],
        "table_slots": [],
        "human_hand": [],
        "human_captured_count": 0,
        "human_chkobbas": 0,
        "human_is_current": False,
        "bot_name": "Opponent",
        "bot_mood": "😐",
        "bot_avatar_url": None,
        "opponent_guest_id": None,
        "opponent_is_human": False,
        "opponent_avatar_color": None,
        "opponent_avatar_initial": None,
        "bot_hand_count": 0,
        "bot_captured_count": 0,
        "bot_chkobbas": 0,
        "move_buttons": [],
        "round_over": False,
        "messages": [],
        "last_human_move": None,
        "last_bot_move": None,
        "final_points": None,
        "round_breakdown": [],
        "commentary_toast": None,
        "deck_remaining": 0,
        "awaiting_cut_choice": False,
        "cut_card": None,
        "is_opening_cutter": False,
        "round_end_sweep": None,
    }


@app.route("/api/rooms/quickmatch", methods=["POST"])
def api_quickmatch():
    """Join the matchmaking queue or pair with a waiting opponent."""
    _ensure_guest_session()
    data = request.get_json(force=True, silent=True) or {}

    token = session.get("_csrf_token")
    if not token or not hmac.compare_digest(token, str(data.get("_csrf_token", ""))):
        return jsonify({"error": "Invalid CSRF token"}), 403

    guest_id     = session.get("guest_id", "")
    display_name = session.get("display_name", "Guest")
    target_score = int(data.get("target_score", 11))
    if target_score not in MP_TARGET_SCORES:
        target_score = 11

    queue: MatchmakingQueue = app.matchmaking_queue

    # Try to find an opponent already waiting.
    opponent = queue.pop_opponent(exclude_guest_id=guest_id)
    if opponent:
        room_id = opponent["room_id"]
        blob = app.game_store.get(room_id)
        if blob is None:
            # Their room expired — treat as no opponent and queue ourselves.
            opponent = None
        else:
            # Pair: add ourselves as player 1.
            from datetime import datetime, timezone
            presence = _player_presence_fields(guest_id)
            blob["players"].append(
                {
                    "guest_id": guest_id,
                    "display_name": display_name,
                    "seat": 1,
                    "is_bot": False,
                    "connected": False,
                    "sid": None,
                    **presence,
                }
            )
            blob["last_action_at"] = datetime.now(timezone.utc).isoformat()
            blob["visibility"] = "private_matched"
            app.game_store.set(room_id, blob)

            # Notify the opponent that someone joined.
            socketio.emit(
                "matchmaking_status",
                {
                    "status": "matched",
                    "room_id": room_id,
                    "opponent_name": display_name,
                },
                to=room_id,
            )
            socketio.emit(
                "room_player_joined",
                {
                    "seat": 1,
                    "display_name": display_name,
                    "guest_id": guest_id,
                    "avatar_initial": display_name[0].upper(),
                    "avatar_color": avatar_color(guest_id),
                    "avatar_key": presence["avatar_key"],
                    "avatar_url": presence["avatar_url"],
                    "room_id": room_id,
                },
                to=room_id,
            )
            logger.info(
                "[matchmaking] paired %s with %s in room %.8s",
                guest_id,
                opponent["guest_id"],
                room_id,
            )
            return jsonify({"matched": True, "room_id": room_id, "opponent": opponent["display_name"]})

    # No opponent — create a waiting room and queue ourselves.
    blob = _create_mp_room("private", guest_id, display_name, target_score)
    room_id = blob["room_id"]
    queue.enqueue(guest_id, display_name, room_id)

    return jsonify({"matched": False, "room_id": room_id})


@app.route("/api/rooms/cancel_queue", methods=["POST"])
def api_cancel_queue():
    """Remove the current player from the matchmaking queue."""
    _ensure_guest_session()
    data = request.get_json(force=True, silent=True) or {}
    token = session.get("_csrf_token")
    if not token or not hmac.compare_digest(token, str(data.get("_csrf_token", ""))):
        return jsonify({"error": "Invalid CSRF token"}), 403

    guest_id = session.get("guest_id", "")
    app.matchmaking_queue.cancel(guest_id)
    return jsonify({"ok": True})


@app.route("/api/rooms/invite", methods=["POST"])
def api_create_invite():
    """Create a private room and return the invite URL."""
    _ensure_guest_session()
    data = request.get_json(force=True, silent=True) or {}
    token = session.get("_csrf_token")
    if not token or not hmac.compare_digest(token, str(data.get("_csrf_token", ""))):
        return jsonify({"error": "Invalid CSRF token"}), 403

    guest_id     = session.get("guest_id", "")
    display_name = session.get("display_name", "Guest")
    target_score = int(data.get("target_score", 11))
    if target_score not in MP_TARGET_SCORES:
        target_score = 11

    blob     = _create_mp_room("private", guest_id, display_name, target_score)
    room_id  = blob["room_id"]
    invite_url = request.host_url.rstrip("/") + f"/play/{room_id}"
    return jsonify({"room_id": room_id, "invite_url": invite_url})


@app.route("/api/rooms/public", methods=["POST"])
def api_create_public_room():
    """Create a public room visible in the lobby."""
    _ensure_guest_session()
    data = request.get_json(force=True, silent=True) or {}
    token = session.get("_csrf_token")
    if not token or not hmac.compare_digest(token, str(data.get("_csrf_token", ""))):
        return jsonify({"error": "Invalid CSRF token"}), 403

    guest_id     = session.get("guest_id", "")
    display_name = session.get("display_name", "Guest")
    target_score = int(data.get("target_score", 11))
    if target_score not in MP_TARGET_SCORES:
        target_score = 11

    blob    = _create_mp_room("public", guest_id, display_name, target_score)
    room_id = blob["room_id"]

    # Announce to all current lobby viewers.
    socketio.emit(
        "lobby_update",
        {
            "action": "add",
            "room": {
                "room_id": room_id,
                "creator_name": display_name,
                "creator_initial": display_name[0].upper() if display_name else "?",
                "creator_color": avatar_color(guest_id),
                "target_score": target_score,
            },
        },
        to="lobby",
    )
    return jsonify({"room_id": room_id})


@app.route("/api/rooms/create-bot", methods=["POST"])
def api_create_bot_room():
    """Create a solo-vs-bot room and return its ID.

    The room uses mode='solo' so all existing bot logic applies unchanged.
    The bot seat is pre-filled; the game starts the moment the human connects.
    """
    _ensure_guest_session()
    guest_id     = session.get("guest_id", "")
    display_name = session.get("display_name", "Guest")

    data         = request.get_json(force=True, silent=True) or {}
    target_score = int(data.get("target_score", 11))
    if target_score not in MP_TARGET_SCORES:
        target_score = 11

    from datetime import datetime, timezone
    room_id = secrets.token_urlsafe(9)
    now     = datetime.now(timezone.utc).isoformat()
    blob = {
        "room_id":         room_id,
        "mode":            "solo",
        "status":          "waiting",
        "visibility":      "private",
        "created_by":      guest_id,
        "target_score_mp": target_score,
        "created_at":      now,
        "last_action_at":  now,
        "players": [
            {"guest_id": guest_id,  "display_name": display_name, "seat": 0,
             "is_bot": False, "connected": False, "sid": None},
            {"guest_id": "bot",     "display_name": BOT_DEFAULT_NAME, "seat": 1,
             "is_bot": True,  "connected": True,  "sid": None},
        ],
        "chat": [],
    }
    app.game_store.set(room_id, blob)
    return jsonify({"room_id": room_id})


@app.route("/history")
def history():
    """Show the current user's last 20 matches."""
    _ensure_guest_session()
    from models.db import get_user_matches
    guest_id = session.get("guest_id", "")
    profile = _guest_profile_kwargs()
    matches = get_user_matches(guest_id, limit=20)
    return render_template(
        "history.html",
        matches=matches,
        **profile,
    )


@app.route("/api/rooms/<room_id>/cancel", methods=["POST"])
def api_cancel_room(room_id: str):
    """Room creator cancels a waiting (or starting) room."""
    _ensure_guest_session()
    guest_id = session.get("guest_id", "")
    blob = app.game_store.get(room_id)
    if blob is None:
        return jsonify({"error": "Room not found"}), 404
    players = blob.get("players", [])
    if not players or players[0].get("guest_id") != guest_id:
        return jsonify({"error": "Not authorized — only the room creator can cancel"}), 403
    if blob.get("status") not in ("waiting", "starting"):
        return jsonify({"error": "Cannot cancel an active or finished game"}), 400
    app.game_store.delete(room_id)
    socketio.emit("room_closed", {"room_id": room_id}, to=room_id)
    if blob.get("visibility") == "public":
        socketio.emit("lobby_update", {"action": "remove", "room_id": room_id}, to="lobby")
    return jsonify({"ok": True, "redirect": url_for("lobby")})


@app.route("/restart", methods=["POST"])
@csrf_protect
def restart():
    room_id = session.pop("solo_room_id", None)
    if room_id:
        app.game_store.delete(room_id)
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Admin utilities
# ---------------------------------------------------------------------------

@app.route("/admin/cleanup", methods=["POST"])
def admin_cleanup():
    """Delete match_over rooms older than 1 hour.

    Protected by ADMIN_TOKEN env var.  Call with:
        curl -X POST -H "X-Admin-Token: <token>" http://host/admin/cleanup
    """
    from datetime import datetime, timezone, timedelta

    expected = os.environ.get("ADMIN_TOKEN", "")
    provided = request.headers.get("X-Admin-Token", "") or request.form.get("admin_token", "")
    if not expected or not hmac.compare_digest(expected, provided):
        return jsonify({"error": "unauthorized"}), 403

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    deleted: list[str] = []

    for room_id in app.game_store.list_active():
        blob = app.game_store.get(room_id)
        if blob is None:
            continue
        if blob.get("status") != "match_over":
            continue
        last_action = blob.get("last_action_at", "")
        try:
            if datetime.fromisoformat(last_action) < cutoff:
                app.game_store.delete(room_id)
                deleted.append(room_id)
        except (ValueError, TypeError):
            pass

    logger.info("Admin cleanup: removed %d stale match_over rooms", len(deleted))
    return jsonify({"deleted": len(deleted), "room_ids": deleted})


# ---------------------------------------------------------------------------
# Register SocketIO event handlers.
# Must come AFTER all symbols in this module are defined (sockets.py imports
# app, socketio, GameManager, etc. from here) but BEFORE socketio.run()
# blocks.  This placement ensures handlers are registered when the server is
# started with `python app.py` (where __name__ == "__main__" would block).
# ---------------------------------------------------------------------------
# Ensure `import ui.app` points to this exact module instance when the app is
# launched as a script (`python ui/app.py`).  Without this alias, ui.sockets
# can import a second module instance, causing event handlers to bind to a
# different SocketIO object than the one that is actually serving requests.
if __name__ == "__main__":
    sys.modules.setdefault("ui.app", sys.modules[__name__])

import ui.sockets  # noqa: F401, E402

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Local development:  python ui/app.py
    # Production:         gunicorn -k eventlet -w 1 --bind 0.0.0.0:$PORT ui.app:app
    _port = int(os.environ.get("PORT", "5000"))
    _debug = os.environ.get("FLASK_DEBUG", "1").lower() in ("1", "true", "yes")
    print("Chkobba Web UI — http://127.0.0.1:%s  (async_mode=%s)" % (_port, _ASYNC_MODE))
    socketio.run(app, host="0.0.0.0", port=_port, debug=_debug, use_reloader=_debug)
