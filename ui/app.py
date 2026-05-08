from __future__ import annotations

import os
import sys
import logging
import hmac
import secrets
from pathlib import Path
from functools import wraps
from enum import Enum
from time import time

# Ensure project root is on path for engine imports
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from flask import Flask, render_template, redirect, url_for, flash, request, session

from engine.game_state import GameState, Move, Card, Suit, Rank, PlayerState, create_initial_state
from engine.heuristic_bot import get_heuristic_move
from engine.utils import card_to_str
from engine.persistence import (
    save_match,
    init_database,
    get_match_history,
    get_statistics,
    save_session,
    load_session,
    clear_session,
)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize database
init_database()


# ---------------------------------------------------------------------------
# Game Phase State Machine
# ---------------------------------------------------------------------------

class GamePhase(Enum):
    """Explicit game phase for state machine."""
    MENU = "menu"              # Start screen, no game running
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

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "templates"),
    static_folder=str(Path(__file__).parent / "static"),
)

# Production: set SECRET_KEY in the Render dashboard (or .env locally). Do not use the dev default online.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "chkobba-dev-key")
app.config["DEBUG"] = False
app.config["DEBUG_MODE"] = False

# Render sets RENDER=true; used to disable debug toggles and unsafe defaults in production.
IS_PRODUCTION = os.environ.get("RENDER", "").lower() == "true"

TARGET_SCORES = [11, 21, 31]

# ---------------------------------------------------------------------------
# Deployment note (Gunicorn / multi-worker / horizontal scale)
# ---------------------------------------------------------------------------
# Game state lives in process memory: MANAGERS (per Flask session cookie key).
# - With multiple Gunicorn workers, each worker has its own MANAGERS dict; a user
#   can hit different workers and see inconsistent or reset games unless you use
#   sticky sessions or a single worker.
# - SQLite + saved sessions use the instance filesystem; on Render free tier the
#   disk is ephemeral — treat history/resume as best-effort unless you add a DB addon.
# Future improvement: external store (Redis/Postgres) keyed by session or game_id.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# In-memory game state
# ---------------------------------------------------------------------------

class GameManager:
    """Holds one active game, match state, and UI messages.
    
    Uses explicit GamePhase enum instead of multiple boolean flags for clearer state management.
    """

    def __init__(self, session_key: str | None = None) -> None:
        # Per-browser keys come from _client_session_key(); None is only for legacy/smoke tests.
        self.session_key = session_key or "__legacy__"
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
        
        # Move tracking for animations
        self.last_human_played_table_index: int | None = None
        self.last_played_card: str | None = None
        self.last_played_by: str | None = None
        self.last_human_move: str | None = None
        self.last_bot_move: str | None = None
        
        # UI table slots (preserve empty positions after captures)
        self.table_slots: list[Card | None] = []
        
        # Player IDs
        self.human_id: int = 0
        self.bot_id: int = 1
        
        # UI messaging
        self.messages: list[str] = []
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
        if clear_saved:
            clear_session(self.session_key)
        
    def _clear_move_state(self) -> None:
        """Clear all move and animation tracking state."""
        self.pending_bot_move = None
        self.pending_bot_captured_indices = []
        self.pending_bot_played_card = None
        self.pending_bot_is_capture = False
        self.last_human_played_table_index = None
        self.last_played_card = None
        self.last_played_by = None
        self.last_human_move = None
        self.last_bot_move = None

    def start_game(self, target_score: int) -> None:
        """Initialize a new match with the given target score."""
        self.target_score = target_score
        self.match_scores = [0, 0]
        self.match_winner = None
        self.final_points = None
        self.match_start_time = time()
        self.round_history = []
        self._clear_move_state()
        
        self.state = create_initial_state()
        self.table_slots = self.state.table_cards.copy()
        self.phase = GamePhase.PLAYING_HUMAN
        logger.info("Started new game with target score %d", target_score)
        self.messages = [f"New game started. Target: {target_score} points."]
        self.last_human_move = None
        self.last_bot_move = None
        self.round_over = False
        self.final_points = None
        self._save_session_state()

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
        self.state = create_initial_state()
        self.table_slots = self.state.table_cards.copy()
        self.phase = GamePhase.PLAYING_HUMAN
        self.messages.append("New round started!")
        self._save_session_state()

    def _save_session_state(self) -> None:
        """Persist current in-progress game session."""
        if self.state is None or self.target_score is None:
            clear_session(self.session_key)
            return
        save_session(self.session_key, {
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
            "state": {
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
            },
        })

    def resume_saved_session(self) -> bool:
        """Load persisted in-progress session into memory."""
        data = load_session(self.session_key)
        if not data:
            return False
        state_data = data.get("state")
        if not state_data:
            return False
        players = [
            PlayerState(
                player_id=p["player_id"],
                hand=[card_from_data(c) for c in p["hand"]],
                captured_cards=[card_from_data(c) for c in p["captured_cards"]],
                chkobbas=p["chkobbas"],
            )
            for p in state_data["players"]
        ]
        self.state = GameState(
            players=players,
            table_cards=[card_from_data(c) for c in state_data["table_cards"]],
            deck=[card_from_data(c) for c in state_data["deck"]],
            current_player=state_data["current_player"],
            last_capturer=state_data["last_capturer"],
            move_history=[move_from_data(m) for m in state_data.get("move_history", [])],
            match_scores=state_data.get("match_scores", [0, 0]),
        )
        self.phase = GamePhase(data.get("phase", GamePhase.PLAYING_HUMAN.value))
        self.match_scores = data.get("match_scores", [0, 0])
        self.target_score = data.get("target_score")
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
        self.table_slots = [card_from_data(c) if c is not None else None for c in data.get("table_slots", [])]
        if not self.table_slots:
            self.table_slots = self.state.table_cards.copy()
        return True

    def _remove_card_from_slots(self, card: Card) -> None:
        """Mark the first matching slot as empty."""
        for i, slot in enumerate(self.table_slots):
            if slot == card:
                self.table_slots[i] = None
                return

    def _place_card_in_slots(self, card: Card) -> int:
        """Place card in first empty slot, or append if none. Returns slot index."""
        for i, slot in enumerate(self.table_slots):
            if slot is None:
                self.table_slots[i] = card
                return i
        self.table_slots.append(card)
        return len(self.table_slots) - 1

    def _sync_table_slots_after_move(self, move: Move) -> None:
        """Update UI slots to preserve table positions across captures."""
        if move.is_capture:
            for captured in move.captured_cards:
                self._remove_card_from_slots(captured)
        else:
            self._place_card_in_slots(move.played_card)

        # Safety: if slot cards don't match real table cards, rebuild compactly.
        slot_cards = [c for c in self.table_slots if c is not None]
        if self.state is not None and sorted(slot_cards, key=str) != sorted(self.state.table_cards, key=str):
            self.table_slots = self.state.table_cards.copy()

    def _is_human_turn(self) -> bool:
        """Check if it's the human player's turn to move."""
        return (
            self.phase == GamePhase.PLAYING_HUMAN
            and self.state is not None
            and self.pending_bot_move is None
            and self.state.current_player == self.human_id
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

    def _apply_selected_move(self, hand_index: int, table_indices: list[int]) -> bool:
        """
        Apply a move built from user-selected cards.
        Validates against state.legal_moves() before applying.
        Returns True if move was applied, False if invalid.
        Includes detailed validation logging.
        """
        if self.phase not in (GamePhase.PLAYING_HUMAN, GamePhase.BOT_MOVING):
            logger.warning("Attempted selected move during phase: %s", self.phase)
            flash("Game is over.")
            return False
            
        if self.state is None:
            logger.warning("Attempted selected move with no active game state")
            flash("Game is over.")
            return False
            
        if not self._is_human_turn():
            logger.warning("Selected move attempt on non-human turn")
            flash("Not your turn!")
            return False

        human = self.state.players[self.human_id]

        # Validate hand index
        if not (0 <= hand_index < len(human.hand)):
            logger.warning("Invalid hand index %d (hand size: %d)", hand_index, len(human.hand))
            flash("Invalid hand card selected.")
            return False

        played_card = human.hand[hand_index]

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
            logger.warning("Selected move not in legal moves. Played: %s, Captured: %s (legal moves: %d)",
                          card_to_str(played_card), 
                          [card_to_str(c) for c in captured_cards],
                          len(legal))
            flash("Illegal move. Please select a valid capture.")
            return False

        logger.info("Human player executing selected move: %s", describe_move(matching))
        # Apply the actual legal move (preserves tuple ordering)
        self._execute_human_move(matching)
        return True

    def _execute_human_move(self, move: Move) -> None:
        """Execute a validated human move and queue bot move for animation."""
        self.last_human_move = describe_move(move)
        self.state.apply_move(move)
        self._sync_table_slots_after_move(move)
        self.messages.append(f"You {self.last_human_move}.")

        # Track human-played table card for animation
        if not move.is_capture:
            self.last_played_card = card_to_str(move.played_card)
            self.last_played_by = "human"
            self.last_human_played_table_index = None
            for i, slot in enumerate(self.table_slots):
                if slot == move.played_card:
                    self.last_human_played_table_index = i
                    break

        # Check if round ended after human move
        if self.state.is_round_over:
            self._finish_round()
            return

        # Queue bot move for animation (do not apply yet)
        self.phase = GamePhase.BOT_MOVING
        self._queue_bot_move()
        self._save_session_state()

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
        self.state.apply_move(self.pending_bot_move)
        self._sync_table_slots_after_move(self.pending_bot_move)
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
            self._save_session_state()

    def _finish_round(self) -> None:
        """End the round, update match scores, check for match winner."""
        round_points = self.state.round_points()
        self.final_points = round_points
        self.match_scores[0] += round_points[0]
        self.match_scores[1] += round_points[1]
        
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

        # Check if match is over
        if (
            self.match_scores[0] >= self.target_score
            or self.match_scores[1] >= self.target_score
        ):
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
            clear_session(self.session_key)
            return

        self.phase = GamePhase.ROUND_OVER
        self._save_session_state()
        
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
                round_scores=self.round_history
            )
            logger.info("Match persisted to database")
        except Exception as e:
            logger.error("Failed to persist match: %s", e)

    def view_data(self) -> dict:
        """Build a dict for the Jinja template."""
        debug = app.config["DEBUG_MODE"]

        # Start screen
        if self.state is None:
            return {
                "show_start_screen": True,
                "target_scores": TARGET_SCORES,
                "debug": debug,
                "match_scores": [0, 0],
                "target_score": None,
                "has_saved_session": load_session(self.session_key) is not None,
            }

        human = self.state.players[self.human_id]
        bot = self.state.players[self.bot_id]

        legal = self.state.legal_moves()
        move_buttons = []
        if self._is_human_turn():
            for i, move in enumerate(legal):
                if move.is_capture:
                    cap = ", ".join(card_to_str(c) for c in move.captured_cards)
                    label = f"{card_to_str(move.played_card)} → capture [{cap}]"
                else:
                    label = f"{card_to_str(move.played_card)} → table"
                move_buttons.append({"index": i, "label": label})

        # Capture map: hand_index -> list of {table_indices, is_capture}
        capture_map = {}
        if self._is_human_turn():
            for move in legal:
                hand_idx, table_idxs = _move_to_indices(self.state, move, self.human_id)
                capture_map.setdefault(hand_idx, []).append({
                    "table_indices": table_idxs,
                    "is_capture": move.is_capture,
                })

        # Debug info
        debug_data = {}
        if debug:
            debug_data = {
                "all_legal_moves": [describe_move(m) for m in legal],
                "current_player": self.state.current_player,
                "deck_count": len(self.state.deck),
                "table_count": len(self.state.table_cards),
                "last_capturer": self.state.last_capturer,
                "bot_hand": [card_to_str(c) for c in bot.hand],
            }

        # Map each visual slot to the current compact table index used by move validation.
        # This keeps selection/highlight correct even when slots preserve empty gaps.
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
            "has_pending_bot": self.pending_bot_move is not None,
            "last_human_played_table_index": self.last_human_played_table_index,
            "pending_bot_captured_indices": self.pending_bot_captured_indices,
            "pending_bot_played_card": self.pending_bot_played_card,
            "pending_bot_is_capture": self.pending_bot_is_capture,
            "last_played_card": self.last_played_card,
            "last_played_by": self.last_played_by,

            # Table
            "table_cards": [card_to_str(c) for c in self.state.table_cards],
            "table_slots": [
                {
                    "card": card_to_str(c) if c is not None else None,
                    "table_index": slot_table_indices[i],
                }
                for i, c in enumerate(self.table_slots)
            ],

            # Human
            "human_hand": [card_to_str(c) for c in human.hand],
            "human_captured_count": len(human.captured_cards),
            "human_chkobbas": human.chkobbas,
            "human_is_current": self.state.current_player == self.human_id,

            # Bot
            "bot_hand_count": len(bot.hand),
            "bot_captured_count": len(bot.captured_cards),
            "bot_chkobbas": bot.chkobbas,

            # Moves
            "move_buttons": move_buttons,
            "round_over": self.phase in (GamePhase.ROUND_OVER, GamePhase.MATCH_OVER),

            # Messages
            "messages": self.messages[-6:],
            "last_human_move": self.last_human_move,
            "last_bot_move": self.last_bot_move,

            # Final score
            "final_points": self.final_points,

            # Meta
            "deck_remaining": len(self.state.deck),
        }


# Per-browser game instances keyed by client_session_key (Flask session).
# See deployment note above: not shared across Gunicorn workers without sticky sessions.
MANAGERS: dict[str, GameManager] = {}


def _client_session_key() -> str:
    key = session.get("client_session_key")
    if not key:
        key = secrets.token_hex(16)
        session["client_session_key"] = key
    return key


def _get_manager() -> GameManager:
    key = _client_session_key()
    manager = MANAGERS.get(key)
    if manager is None:
        manager = GameManager(session_key=key)
        manager.resume_saved_session()
        MANAGERS[key] = manager
    return manager


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
    return render_template("index.html", **manager.view_data())


@app.route("/history")
def history():
    return render_template(
        "history.html",
        matches=get_match_history(limit=50),
        stats=get_statistics(),
    )


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
    manager = _get_manager()
    target_score = request.form.get("target_score", type=int)
    if target_score not in TARGET_SCORES:
        flash("Invalid target score.")
        return redirect(url_for("index"))
    manager.start_game(target_score)
    return redirect(url_for("index"))


@app.route("/resume", methods=["POST"])
@csrf_protect
def resume():
    manager = _get_manager()
    if manager.resume_saved_session():
        flash("Resumed saved game session.")
    else:
        flash("No saved session found.")
    return redirect(url_for("index"))


@app.route("/commit_bot", methods=["POST"])
@csrf_protect
def commit_bot():
    manager = _get_manager()
    manager.commit_bot_move()
    return redirect(url_for("index"))


@app.route("/next_round", methods=["POST"])
@csrf_protect
def next_round():
    manager = _get_manager()
    manager.next_round()
    return redirect(url_for("index"))


@app.route("/restart", methods=["POST"])
@csrf_protect
def restart():
    manager = _get_manager()
    manager.restart()
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Local development only. Production uses: gunicorn ui.app:app
    _port = int(os.environ.get("PORT", "5000"))
    _debug = os.environ.get("FLASK_DEBUG", "1").lower() in ("1", "true", "yes")
    print("Chkobba Web UI — http://127.0.0.1:%s (FLASK_DEBUG=%s)" % (_port, _debug))
    app.run(host="0.0.0.0", port=_port, debug=_debug)
