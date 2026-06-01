"""
Heuristic bot hidden-information sampling tests.

Run with: python -m pytest tests/test_heuristic_bot.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.game_state import GameState, Card, Suit, Rank, PlayerState, create_initial_state
from engine.heuristic_bot import get_hidden_card_pool, sample_opponent_hand


class TestFailure(Exception):
    pass


def assert_eq(actual, expected, msg: str = ""):
    if actual != expected:
        raise TestFailure(f"{msg}: expected {expected}, got {actual}")


def assert_true(condition: bool, msg: str = ""):
    if not condition:
        raise TestFailure(msg)


def test_hidden_pool_equals_deck_plus_opponent_hand():
    """Hidden pool size must match unknown deck stock + opponent hand size."""
    print("Testing: hidden pool size...")
    state = create_initial_state(seed=7)
    viewer = state.current_player
    opp = state.players[1 - viewer]
    pool = get_hidden_card_pool(state, viewer=viewer)
    expected = len(state.deck) + len(opp.hand)
    assert_eq(len(pool), expected, "hidden pool size")
    print("  PASS")


def test_hidden_pool_excludes_visible_cards():
    """Sample pool must not contain bot hand or table cards."""
    print("Testing: hidden pool excludes visible cards...")
    state = create_initial_state(seed=11)
    viewer = state.current_player
    pool = get_hidden_card_pool(state, viewer=viewer)
    for card in state.players[viewer].hand:
        assert_true(card not in pool, f"bot hand card {card} leaked into pool")
    for card in state.table_cards:
        assert_true(card not in pool, f"table card {card} leaked into pool")
    print("  PASS")


def test_samples_do_not_use_actual_opponent_hand():
    """Samples must not always include the opponent's real cards (no perfect info)."""
    print("Testing: samples omit actual opponent hand...")
    ace = Card(Suit.DENARI, Rank.ACE)
    king = Card(Suit.CUPS, Rank.KING)
    secret = Card(Suit.SWORDS, Rank.SEVEN)
    filler = Card(Suit.CLUBS, Rank.FOUR)

    p0 = PlayerState(player_id=0, hand=[ace])
    p1 = PlayerState(player_id=1, hand=[secret, king])
    state = GameState(
        players=[p0, p1],
        table_cards=[filler],
        deck=[Card(Suit.CLUBS, Rank.TWO), Card(Suit.CLUBS, Rank.THREE)],
        current_player=0,
        last_capturer=None,
    )

    samples = sample_opponent_hand(state, num_samples=80)
    assert_true(all(len(s) == 2 for s in samples), "each sample matches opponent hand size")
    always_has_secret = all(secret in s for s in samples)
    assert_true(not always_has_secret, "samples must not always contain opponent's actual card")
    for sample in samples:
        assert_true(ace not in sample, "sample must not contain bot's hand")
        assert_true(filler not in sample, "sample must not contain table card")
    print("  PASS")


def run_all():
    tests = [
        test_hidden_pool_equals_deck_plus_opponent_hand,
        test_hidden_pool_excludes_visible_cards,
        test_samples_do_not_use_actual_opponent_hand,
    ]
    passed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except TestFailure as e:
            print(f"  FAIL: {e}")
        except Exception as e:
            print(f"  ERROR: {e}")
    print(f"\n{passed}/{len(tests)} tests passed")
    return passed == len(tests)


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)
