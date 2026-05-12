from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from itertools import combinations
from typing import List, Optional, Tuple
import random

class Suit(str, Enum):
    DENARI = "Denari"    # diamonds / dīnārī
    CUPS = "7ob"
    SWORDS = "Sbata"
    CLUBS = "Dheben"


class Rank(int, Enum):
    ACE = 1
    TWO = 2
    THREE = 3
    FOUR = 4
    FIVE = 5
    SIX = 6
    SEVEN = 7
    KNIGHT = 8
    JACK = 9
    KING = 10


@dataclass(frozen=True, slots=True)
class Card:
    suit: Suit
    rank: Rank

    @property
    def value(self) -> int:
        """Value used for capture sums."""
        return int(self.rank)

    def __str__(self) -> str:
        return f"{self.rank.value}-{self.suit.value}"


@dataclass(frozen=True, slots=True)
class Move:
    """
    A move is:
    - playing one card from hand
    - optionally capturing one or more cards from the table
    """
    played_card: Card
    captured_cards: Tuple[Card, ...] = ()

    @property
    def is_capture(self) -> bool:
        return len(self.captured_cards) > 0


@dataclass
class PlayerState:
    player_id: int
    hand: List[Card] = field(default_factory=list)
    captured_cards: List[Card] = field(default_factory=list)
    chkobbas: int = 0

    def remove_from_hand(self, card: Card) -> None:
        self.hand.remove(card)

    def add_captured(self, cards: List[Card]) -> None:
        self.captured_cards.extend(cards)


@dataclass
class GameState:
    """
    Engine state for one round of 2-player Chkobba.
    """
    players: List[PlayerState]
    table_cards: List[Card]
    deck: List[Card]
    current_player: int = 0
    last_capturer: Optional[int] = None
    move_history: List[Move] = field(default_factory=list)
    # One-shot UI hint: (receiver_seat, swept_cards) set when round ends before table is cleared.
    round_end_sweep: Optional[Tuple[int, Tuple[Card, ...]]] = None

    # Optional match score (across rounds)
    match_scores: List[int] = field(default_factory=lambda: [0, 0])

    def clone(self) -> GameState:
        """Useful for AI search."""
        return GameState(
            players=[
                PlayerState(
                    player_id=p.player_id,
                    hand=p.hand.copy(),
                    captured_cards=p.captured_cards.copy(),
                    chkobbas=p.chkobbas,
                )
                for p in self.players
            ],
            table_cards=self.table_cards.copy(),
            deck=self.deck.copy(),
            current_player=self.current_player,
            last_capturer=self.last_capturer,
            move_history=self.move_history.copy(),
            match_scores=self.match_scores.copy(),
            round_end_sweep=(
                (self.round_end_sweep[0], tuple(self.round_end_sweep[1]))
                if self.round_end_sweep is not None
                else None
            ),
        )

    @property
    def opponent_player(self) -> int:
        return 1 - self.current_player

    @property
    def is_round_over(self) -> bool:
        return (
            len(self.deck) == 0
            and all(len(p.hand) == 0 for p in self.players)
        )
    
    def is_legal_move(self, move: Move) -> bool:
        return move in self.legal_moves()

    def deal_if_needed(self, cards_each: int = 3) -> None:
        """
        Deal cards if both players have empty hands and deck still has cards.
        """
        if any(len(p.hand) > 0 for p in self.players):
            return
        if len(self.deck) == 0:
            return

        for _ in range(cards_each):
            for player in self.players:
                if self.deck:
                    player.hand.append(self.deck.pop(0))

    def legal_moves(self) -> List[Move]:
        """
        Generate all legal moves for current player under Tunisian Chkobba rules:
        - capture is mandatory for a played card if possible
        - exact single-card capture has priority over sum captures
        """
        player = self.players[self.current_player]
        moves: List[Move] = []

        for card in player.hand:
            capture_sets = self._find_capture_sets(card)
            if capture_sets:
                for captured in capture_sets:
                    moves.append(Move(played_card=card, captured_cards=tuple(captured)))
            else:
                moves.append(Move(played_card=card, captured_cards=()))

        return moves

    def apply_move(self, move: Move) -> None:
        """
        Apply one move to the state.
        """
        if move not in self.legal_moves():
            raise ValueError(f"Illegal move: {move}")
        
        player = self.players[self.current_player]
        player.remove_from_hand(move.played_card)

        if move.is_capture:
            # Remove captured cards from table
            for c in move.captured_cards:
                self.table_cards.remove(c)

            # Capturing player keeps played card + captured cards
            captured_bundle = [move.played_card] + list(move.captured_cards)
            player.add_captured(captured_bundle)

            # Chkobba: table becomes empty after capture, and not final play of round
            if len(self.table_cards) == 0 and not self._is_last_play_of_round_after_this_move():
                player.chkobbas += 1

            self.last_capturer = self.current_player
        else:
            # Just place card on table
            self.table_cards.append(move.played_card)

        self.move_history.append(move)

        # Switch turn
        self.current_player = 1 - self.current_player

        # If both hands empty, deal next batch
        self.deal_if_needed()

        # End-of-round cleanup: leftover table cards go to last capturer
        if self.is_round_over:
            if self.last_capturer is not None and self.table_cards:
                self.round_end_sweep = (self.last_capturer, tuple(self.table_cards))
            else:
                self.round_end_sweep = None
            self._collect_remaining_table_cards()

    def card_has_capture(self, card: Card) -> bool:
        return len(self._find_capture_sets(card)) > 0

    @property
    def played_cards(self) -> List[Card]:
        """All cards captured so far by both players."""
        cards = []
        for p in self.players:
            cards.extend(p.captured_cards)
        return cards
    
    def next_state(self, move: Move) -> GameState:
        new_state = self.clone()
        new_state.apply_move(move)
        return new_state

    def unseen_cards_for_player(self, player_id: int) -> List[Card]:
        """
        Cards not in player's hand, not on table, not already captured.
        Useful for AI hidden-information modeling.
        """
        known = set(self.players[player_id].hand)
        known.update(self.table_cards)

        for p in self.players:
            known.update(p.captured_cards)

        return [c for c in full_deck() if c not in known]

    def round_points(self) -> List[int]:
        """
        Tunisian Chkobba round scoring:
        - chkobbas
        - most cards
        - most denari
        - 7 of denari
        - barmila (Tunisian rule: 7s first, then 6s)
        """
        points = [0, 0]

        # chkobbas
        points[0] += self.players[0].chkobbas
        points[1] += self.players[1].chkobbas

        # most cards
        count0 = len(self.players[0].captured_cards)
        count1 = len(self.players[1].captured_cards)
        if count0 > count1:
            points[0] += 1
        elif count1 > count0:
            points[1] += 1

        # most denari
        den0 = sum(1 for c in self.players[0].captured_cards if c.suit == Suit.DENARI)
        den1 = sum(1 for c in self.players[1].captured_cards if c.suit == Suit.DENARI)
        if den0 > den1:
            points[0] += 1
        elif den1 > den0:
            points[1] += 1

        # 7 of denari
        seven_denari = Card(Suit.DENARI, Rank.SEVEN)
        if seven_denari in self.players[0].captured_cards:
            points[0] += 1
        elif seven_denari in self.players[1].captured_cards:
            points[1] += 1

        # Tunisian barmila
        b0, b1 = tunisian_barmila_points(
            self.players[0].captured_cards,
            self.players[1].captured_cards,
        )
        points[0] += b0
        points[1] += b1

        return points

    

    def _find_capture_sets(self, played_card: Card) -> List[List[Card]]:
        """
        Return legal capture sets for the played card according to Tunisian Chkobba rules:

        1. If a single matching table card exists, it has priority.
        2. Otherwise, any combinations summing to the played card value are allowed.
        3. Capture is mandatory if any capture exists.
        """
        target = played_card.value

        # Rule 1: exact single-card matches have priority
        single_matches = [[card] for card in self.table_cards if card.value == target]
        if single_matches:
            return single_matches

        # Rule 2: otherwise check sum combinations
        results: List[List[Card]] = []
        for r in range(2, len(self.table_cards) + 1):
            for combo in combinations(self.table_cards, r):
                if sum(card.value for card in combo) == target:
                    results.append(list(combo))

        return results

    def _is_last_play_of_round_after_this_move(self) -> bool:
        """
        True if after this move there will be no cards left in any hand and no deck left.
        Used to avoid counting final sweep as chkobba.
        """
        current = self.players[self.current_player]
        remaining_in_current_hand_after_play = len(current.hand)

        other_player = self.players[1 - self.current_player]
        total_hands_after_play = remaining_in_current_hand_after_play + len(other_player.hand)

        return total_hands_after_play == 0 and len(self.deck) == 0

    def _collect_remaining_table_cards(self) -> None:
        """
        At round end, leftover table cards go to the last capturer.
        """
        if self.table_cards and self.last_capturer is not None:
            self.players[self.last_capturer].captured_cards.extend(self.table_cards)
            self.table_cards.clear()


def full_deck() -> List[Card]:
    deck = []
    for suit in Suit:
        for rank in Rank:
            deck.append(Card(suit, rank))
    return deck


def tunisian_barmila_points(cards0: List[Card], cards1: List[Card]) -> Tuple[int, int]:
    p0_7 = sum(1 for c in cards0 if c.rank == Rank.SEVEN)
    p1_7 = sum(1 for c in cards1 if c.rank == Rank.SEVEN)

    if p0_7 >= 3:
        return (1, 0)
    if p1_7 >= 3:
        return (0, 1)

    # here it must be 2-2 on sevens, so check sixes
    p0_6 = sum(1 for c in cards0 if c.rank == Rank.SIX)
    p1_6 = sum(1 for c in cards1 if c.rank == Rank.SIX)

    if p0_6 >= 3:
        return (1, 0)
    if p1_6 >= 3:
        return (0, 1)

    # then it must be 2-2 on sixes too
    return (0, 0)


# Minimum cards each side of the cut so both "halves" are non-trivial.
OPENING_CUT_MARGIN = 8


def apply_opening_deal_from_cut(
    deck: List[Card],
    cut_index: int,
    keep_cut: bool,
    cutter_seat: int,
) -> GameState:
    """
    Tunisian opening deal after the cutter has seen the cut card.

    *cut_index* — index into *deck* (0 = top of pile, same as deal_if_needed pop(0)).
    The card at that index is the "cut card" (top of the lower half).

    Path A (*keep_cut*): cut card → cutter's hand; dealer gives cutter 2 more;
    opponent 3; 4 to table (all from the top of the remaining deck).

    Path B (not *keep_cut*): cut card face-up on table; 3 more to table;
    cutter 3; opponent 3.
    """
    n = len(deck)
    if not (0 <= cut_index < n):
        raise ValueError("cut_index out of range")
    deck = deck.copy()
    cut_card = deck.pop(cut_index)

    players = [PlayerState(player_id=0), PlayerState(player_id=1)]
    cutter = players[cutter_seat]
    opp = players[1 - cutter_seat]

    if keep_cut:
        cutter.hand.append(cut_card)
        for _ in range(2):
            cutter.hand.append(deck.pop(0))
        for _ in range(3):
            opp.hand.append(deck.pop(0))
        table_cards = [deck.pop(0) for _ in range(4)]
    else:
        table_cards = [cut_card]
        for _ in range(3):
            table_cards.append(deck.pop(0))
        for _ in range(3):
            cutter.hand.append(deck.pop(0))
        for _ in range(3):
            opp.hand.append(deck.pop(0))

    return GameState(
        players=players,
        table_cards=table_cards,
        deck=deck,
        current_player=cutter_seat,
        last_capturer=None,
    )


def choose_opening_cut_index(rng: random.Random, deck_len: int) -> int:
    """Pick a cut index with OPENING_CUT_MARGIN cards above and below."""
    lo = OPENING_CUT_MARGIN
    hi = deck_len - OPENING_CUT_MARGIN - 1
    if hi < lo:
        return deck_len // 2
    return rng.randint(lo, hi)


def create_initial_state(seed: Optional[int] = None) -> GameState:
    """
    Build a fully dealt round (for tests / simulations).

    Uses the cut-and-deal rules with a random cut index and keep/discard
    choice derived from *seed* for reproducibility.
    """
    rng = random.Random(seed)
    deck = full_deck()
    rng.shuffle(deck)
    cut_index = choose_opening_cut_index(rng, len(deck))
    keep_cut = bool(rng.getrandbits(1))
    return apply_opening_deal_from_cut(deck, cut_index, keep_cut, cutter_seat=0)

def count_rank(cards: List[Card], rank: Rank) -> int:
        return sum(1 for c in cards if c.rank == rank)