from __future__ import annotations

from typing import List, Tuple, Set
import random

from engine.game_state import GameState, Move, Card, Suit, Rank


SEVEN_OF_DENARI = Card(Suit.DENARI, Rank.SEVEN)

WEIGHTS = {
    "chkobba": 50,
    "seven_denari": 25,
    "seven": 8,
    "six": 2,
    "denari": 7,
    "card": 1,
    "leave_chkobba_risk": -10,
    "endgame_capture": 5,
}


def get_remaining_cards(state: GameState) -> Set[Card]:
    """Get all cards that haven't been played yet (not in hands, captured_cards, or table).
    
    Used for hidden-card sampling to avoid perfect information bias.
    """
    all_cards = set()
    for suit in Suit:
        for rank in Rank:
            all_cards.add(Card(suit, rank))
    
    # Remove cards we know about
    for player in state.players:
        all_cards -= set(player.hand)
        all_cards -= set(player.captured_cards)
    
    all_cards -= set(state.table_cards)
    all_cards -= set(state.deck)
    
    return all_cards


def sample_opponent_hand(state: GameState, num_samples: int = 100) -> List[Set[Card]]:
    """Sample plausible opponent hands based on remaining unknown cards.
    
    Args:
        state: Current game state
        num_samples: Number of samples to generate
        
    Returns:
        List of sampled possible opponent hands (as sets of cards)
    """
    opponent_idx = 1 - state.current_player
    opponent = state.players[opponent_idx]
    known_opp_cards = set(opponent.hand)
    unknown_count = len(opponent.hand)
    
    # If we can see all opponent cards (shouldn't happen but handle it)
    if unknown_count == 0:
        return [set() for _ in range(num_samples)]
    
    remaining_cards = get_remaining_cards(state)
    remaining_cards_list = list(remaining_cards)
    
    samples = []
    for _ in range(num_samples):
        # Sample unknown_count cards from remaining pool
        if len(remaining_cards_list) >= unknown_count:
            sampled = set(random.sample(remaining_cards_list, unknown_count))
        else:
            sampled = set(remaining_cards_list)
        samples.append(known_opp_cards | sampled)
    
    return samples


def can_capture_all_table(card: Card, table: List[Card]) -> bool:
    """
    Check if this card can clear the whole table.

    This is used for chkobba-risk detection.
    It respects the idea that exact single-card capture has priority.
    """
    if not table:
        return False

    if len(table) == 1:
        return table[0].value == card.value

    if any(c.value == card.value for c in table):
        return False

    return sum(c.value for c in table) == card.value


def captured_total(move: Move) -> int:
    """
    Total cards gained from a capture.
    Includes the played card.
    """
    if not move.is_capture:
        return 0

    return 1 + len(move.captured_cards)


def explain_move(state: GameState, move: Move) -> List[str]:
    reasons: List[str] = []

    if move.is_capture:
        gained_cards = [move.played_card] + list(move.captured_cards)
        reasons.append(f"captures {len(gained_cards)} cards")

        if SEVEN_OF_DENARI in gained_cards:
            reasons.append("takes 7 of denari")

        if any(c.rank == Rank.SEVEN for c in gained_cards):
            reasons.append("takes seven for barmila")

        if any(c.rank == Rank.SIX for c in gained_cards):
            reasons.append("takes six for barmila tie-break")

        denari_count = sum(1 for c in gained_cards if c.suit == Suit.DENARI)
        if denari_count:
            reasons.append(f"takes {denari_count} denari card(s)")

        table_after = len(state.table_cards) - len(move.captured_cards)
        if table_after == 0:
            reasons.append("clears the table")

    else:
        reasons.append("places card on table")

    return reasons


def evaluate_move(state: GameState, move: Move) -> float:
    """Evaluate a move using heuristic weights and risk sampling.
    
    Uses hidden-card sampling for fair AI that doesn't exploit perfect information.
    """
    score = 0.0
    player = state.players[state.current_player]
    opponent = state.players[1 - state.current_player]

    gained_cards = [move.played_card] + list(move.captured_cards)

    # 1. Chkobba reward
    table_after = len(state.table_cards) - len(move.captured_cards)

    if move.is_capture and table_after == 0:
        remaining_cards_after_move = (len(player.hand) - 1) + len(opponent.hand)

        if remaining_cards_after_move > 0 or len(state.deck) > 0:
            score += WEIGHTS["chkobba"]

    # 2. Captured-card reward
    if move.is_capture:
        for card in gained_cards:
            if card == SEVEN_OF_DENARI:
                score += WEIGHTS["seven_denari"]

            if card.rank == Rank.SEVEN:
                score += WEIGHTS["seven"]

            if card.rank == Rank.SIX:
                score += WEIGHTS["six"]

            if card.suit == Suit.DENARI:
                score += WEIGHTS["denari"]

            score += WEIGHTS["card"]

    # 3. Risk assessment using hidden-card sampling
    # 
    # Fair AI: sample plausible opponent hands instead of reading actual hand.
    # This avoids perfect-information bias while still modeling risk reasonably.
    num_samples = 50  # Balance between accuracy and performance
    opponent_hand_samples = sample_opponent_hand(state, num_samples)
    
    risk_penalty = 0.0
    
    if move.is_capture:
        next_table = [c for c in state.table_cards if c not in move.captured_cards]
        if next_table:
            # Count how many samples have a chkobba risk
            risk_count = 0
            for sample_hand in opponent_hand_samples:
                for opp_card in sample_hand:
                    if can_capture_all_table(opp_card, next_table):
                        risk_count += 1
                        break
            # Average risk across samples
            if risk_count > 0:
                risk_penalty = WEIGHTS["leave_chkobba_risk"] * (risk_count / num_samples)
                score += risk_penalty
    else:
        new_table = state.table_cards + [move.played_card]
        # Count how many samples have a chkobba risk
        risk_count = 0
        for sample_hand in opponent_hand_samples:
            for opp_card in sample_hand:
                if can_capture_all_table(opp_card, new_table):
                    risk_count += 1
                    break
        # Average risk across samples
        if risk_count > 0:
            risk_penalty = WEIGHTS["leave_chkobba_risk"] * (risk_count / num_samples)
            score += risk_penalty

    # 4. Endgame card-count pressure
    my_cards = len(player.captured_cards)
    opp_cards = len(opponent.captured_cards)

    near_endgame = len(state.deck) == 0 and len(player.hand) <= 2

    if near_endgame and my_cards <= opp_cards and move.is_capture:
        score += WEIGHTS["endgame_capture"]

    return score


def get_heuristic_move(state: GameState, verbose: bool = False) -> Move:
    legal_moves = state.legal_moves()

    if not legal_moves:
        raise ValueError("No legal moves available")

    if len(legal_moves) == 1:
        return legal_moves[0]

    scored_moves: List[Tuple[float, Move]] = []

    for move in legal_moves:
        score = evaluate_move(state, move)
        scored_moves.append((score, move))

        if verbose:
            cap_str = (
                f" captures {len(move.captured_cards)} table card(s)"
                if move.is_capture else " no capture"
            )
            reasons = ", ".join(explain_move(state, move))
            print(
                f"  Move: {move.played_card.rank.value}-{move.played_card.suit.value}"
                f" | {cap_str}"
                f" | score: {score:.1f}"
                f" | {reasons}"
            )

    scored_moves.sort(key=lambda x: x[0], reverse=True)

    best_score = scored_moves[0][0]
    tied_moves = [move for score, move in scored_moves if abs(score - best_score) < 0.001]

    selected = random.choice(tied_moves)

    if verbose:
        print(f"  Selected score: {best_score:.1f}")

    return selected


def get_greedy_move(state: GameState) -> Move:
    legal_moves = state.legal_moves()

    if not legal_moves:
        raise ValueError("No legal moves available")

    return max(legal_moves, key=captured_total)


def compare_bots(n_games: int = 100, seed: int = 42) -> dict:
    from engine.simulation import simulate_games

    results = {}

    print(f"Simulating {n_games} games per matchup...\n")

    matchups = [
        ("heuristic", "random"),
        ("random", "heuristic"),
        ("heuristic", "greedy"),
        ("greedy", "heuristic"),
        ("random", "random"),
    ]

    for p0, p1 in matchups:
        print(f"{p0} vs {p1}:")

        stats = simulate_games(
            n_games=n_games,
            player0_type=p0,
            player1_type=p1,
            seed=seed,
        )

        results[f"{p0}_vs_{p1}"] = stats

        print(f"  P0 wins: {stats['wins'][0]}")
        print(f"  P1 wins: {stats['wins'][1]}")
        print(f"  Ties: {stats['wins'][2]}")
        print(f"  Avg points: P0={stats['avg_points'][0]:.2f}, P1={stats['avg_points'][1]:.2f}")
        print()

    return results


if __name__ == "__main__":
    try:
        from engine.game_state import create_initial_state
        from engine.simulation import card_to_str
    except ImportError:
        from game_state import create_initial_state
        from simulation import card_to_str

    print("Heuristic Bot Test")
    print("=" * 60)

    state = create_initial_state(seed=42)

    for i in range(5):
        if state.is_round_over:
            break

        player = state.players[state.current_player]

        print(f"\nTurn {i + 1} - Player {state.current_player}")
        print(f"Hand: {[card_to_str(c) for c in player.hand]}")
        print(f"Table: {[card_to_str(c) for c in state.table_cards]}")

        move = get_heuristic_move(state, verbose=True)

        move_str = card_to_str(move.played_card)
        cap_str = (
            f" capturing {[card_to_str(c) for c in move.captured_cards]}"
            if move.is_capture else ""
        )

        print(f"  -> Plays: {move_str}{cap_str}")

        state.apply_move(move)

    print("\n" + "=" * 60)
    print("Test complete!")
    