from __future__ import annotations

from typing import List, Optional
import random

from engine.game_state import (
    GameState, Move, Card, Suit, Rank,
    create_initial_state
)
from engine.heuristic_bot import get_heuristic_move, get_greedy_move
from engine.utils import card_to_str, format_hand, format_table

HEURISTIC_AVAILABLE = True


def display_state(state: GameState, show_opponent_hand: bool = False) -> None:
    print("\n" + "=" * 60)

    opponent = state.players[state.opponent_player]
    opp_hand = format_hand(opponent.hand) if show_opponent_hand else f"({len(opponent.hand)} cards)"
    print(f"Player {opponent.player_id} (opponent): {opp_hand}")
    print(f"  Captured: {len(opponent.captured_cards)} cards | Chkobbas: {opponent.chkobbas}")

    print("-" * 40)
    print(f"TABLE: {format_table(state.table_cards)}")
    print("-" * 40)

    current = state.players[state.current_player]
    print(f"Player {current.player_id} (current): {format_hand(current.hand)}")
    print(f"  Captured: {len(current.captured_cards)} cards | Chkobbas: {current.chkobbas}")

    print(f"\nDeck: {len(state.deck)} cards remaining")
    print("=" * 60)


def get_human_move(state: GameState) -> Move:
    player = state.players[state.current_player]
    legal_moves = state.legal_moves()

    print(f"\nYour hand: {format_hand(player.hand)}")
    print(f"Table: {format_table(state.table_cards)}")

    print("\nLegal moves:")
    for i, move in enumerate(legal_moves):
        if move.is_capture:
            captured_str = ", ".join(card_to_str(c) for c in move.captured_cards)
            print(f"  [{i}] Play {card_to_str(move.played_card)} → CAPTURE [{captured_str}]")
        else:
            print(f"  [{i}] Play {card_to_str(move.played_card)} → table")

    while True:
        try:
            choice = input(f"\nSelect move (0-{len(legal_moves) - 1}): ").strip()
            idx = int(choice)
            if 0 <= idx < len(legal_moves):
                return legal_moves[idx]
            print("Invalid selection. Try again.")
        except (ValueError, EOFError):
            print("Please enter a valid number.")


def get_random_move(state: GameState) -> Move:
    return random.choice(state.legal_moves())


def play_game(
    player0_type: str = "human",
    player1_type: str = "random",
    seed: Optional[int] = None,
    verbose: bool = True,
    show_opponent_hands: bool = False,
) -> List[int]:
    state = create_initial_state(seed)

    def get_move_func(player_type: str):
        if player_type == "human":
            return get_human_move

        if player_type == "random":
            return get_random_move

        if player_type == "heuristic":
            if not HEURISTIC_AVAILABLE:
                raise ValueError("Heuristic bot not available")
            return lambda s: get_heuristic_move(s, verbose=False)

        if player_type == "greedy":
            if not HEURISTIC_AVAILABLE:
                raise ValueError("Greedy bot not available")
            return get_greedy_move

        raise ValueError(f"Unknown player type: {player_type}")

    move_getters = {
        0: get_move_func(player0_type),
        1: get_move_func(player1_type),
    }

    while not state.is_round_over:
        if verbose:
            display_state(state, show_opponent_hands)

        current_player = state.current_player
        before_chkobbas = state.players[current_player].chkobbas

        move = move_getters[current_player](state)

        if verbose:
            cap_str = (
                f" capturing {', '.join(card_to_str(c) for c in move.captured_cards)}"
                if move.is_capture else ""
            )
            print(f"\n>>> Player {current_player} plays {card_to_str(move.played_card)}{cap_str}")

        state.apply_move(move)

        after_chkobbas = state.players[current_player].chkobbas

        if verbose and after_chkobbas > before_chkobbas:
            print(f"*** CHKOBBA! Player {current_player} cleared the table! ***")

    points = state.round_points()

    if verbose:
        print("\n" + "=" * 60)
        print("ROUND OVER!")
        print("=" * 60)

        for p in state.players:
            print(f"Player {p.player_id}: {len(p.captured_cards)} cards, {p.chkobbas} chkobbas")

        print(f"\nPoints: Player 0 = {points[0]}, Player 1 = {points[1]}")

    return points


def simulate_games(
    n_games: int,
    player0_type: str = "random",
    player1_type: str = "random",
    seed: Optional[int] = None,
) -> dict:
    wins = [0, 0, 0]
    total_points = [0, 0]

    for i in range(n_games):
        game_seed = None if seed is None else seed + i

        points = play_game(
            player0_type=player0_type,
            player1_type=player1_type,
            seed=game_seed,
            verbose=False,
            show_opponent_hands=False,
        )

        total_points[0] += points[0]
        total_points[1] += points[1]

        if points[0] > points[1]:
            wins[0] += 1
        elif points[1] > points[0]:
            wins[1] += 1
        else:
            wins[2] += 1

    return {
        "wins": wins,
        "total_points": total_points,
        "avg_points": [
            total_points[0] / n_games,
            total_points[1] / n_games,
        ],
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "simulate":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 100

        p0 = sys.argv[3] if len(sys.argv) > 3 else "heuristic"
        p1 = sys.argv[4] if len(sys.argv) > 4 else "random"

        print(f"Simulating {n} games: {p0} vs {p1}")

        stats = simulate_games(n, p0, p1, seed=42)

        print("\nResults:")
        print(f"  Player 0 wins: {stats['wins'][0]}")
        print(f"  Player 1 wins: {stats['wins'][1]}")
        print(f"  Ties: {stats['wins'][2]}")
        print(f"  Avg points: P0={stats['avg_points'][0]:.2f}, P1={stats['avg_points'][1]:.2f}")

    else:
        print("Chkobba Simulation")
        print("==================")
        print("Run with:")
        print("  python simulation.py simulate 1000 heuristic random")
        print()

        points = play_game(
            "human",
            "random",
            seed=42,
            verbose=True,
            show_opponent_hands=True,
        )

        print(f"\nFinal score: Player 0 = {points[0]}, Player 1 = {points[1]}")
        