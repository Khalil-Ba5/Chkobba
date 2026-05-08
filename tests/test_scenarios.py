"""
Test scenarios for Chkobba game rules.

Each test creates a specific game state, applies moves, and validates outcomes.
Run with: python test_scenarios.py
"""

from typing import List
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from engine.game_state import (
        GameState, Move, Card, Suit, Rank, PlayerState,
        create_initial_state, tunisian_barmila_points
    )
except ImportError:
    from game_state import (
        GameState, Move, Card, Suit, Rank, PlayerState,
        create_initial_state, tunisian_barmila_points
    )


class TestFailure(Exception):
    """Raised when a test fails."""
    pass


def assert_eq(actual, expected, msg: str = ""):
    """Assert equality with descriptive message."""
    if actual != expected:
        raise TestFailure(f"{msg}: expected {expected}, got {actual}")


def assert_true(condition: bool, msg: str = ""):
    """Assert condition is true."""
    if not condition:
        raise TestFailure(msg)


# =============================================================================
# TEST CASES
# =============================================================================

def test_exact_match_priority():
    """
    Test: If a single card on table matches the played card's value,
    it must be captured (priority over combinations).
    """
    print("Testing: exact match priority...")
    
    # Setup: Player has a 7, table has a 7 and (4+3)
    p0 = PlayerState(player_id=0, hand=[Card(Suit.DENARI, Rank.SEVEN)])
    p1 = PlayerState(player_id=1, hand=[])
    table = [
        Card(Suit.CUPS, Rank.SEVEN),  # exact match
        Card(Suit.SWORDS, Rank.FOUR),
        Card(Suit.CLUBS, Rank.THREE),
    ]
    
    state = GameState(players=[p0, p1], table_cards=table, deck=[])
    
    legal_moves = state.legal_moves()
    
    # Should have exactly 1 move: capture the 7
    assert_eq(len(legal_moves), 1, "Should have exactly 1 legal move (exact match)")
    
    move = legal_moves[0]
    assert_true(move.is_capture, "Move should be a capture")
    assert_eq(len(move.captured_cards), 1, "Should capture exactly 1 card")
    assert_eq(move.captured_cards[0].rank, Rank.SEVEN, "Should capture the 7")
    
    print("  PASSED")


def test_combination_capture():
    """
    Test: When no exact match, allow sum combinations.
    """
    print("Testing: combination capture...")
    
    # Setup: Player has a 7, table has 4+3, 5+2, no single 7
    p0 = PlayerState(player_id=0, hand=[Card(Suit.DENARI, Rank.SEVEN)])
    p1 = PlayerState(player_id=1, hand=[])
    table = [
        Card(Suit.CUPS, Rank.FOUR),
        Card(Suit.CLUBS, Rank.THREE),
        Card(Suit.SWORDS, Rank.FIVE),
        Card(Suit.DENARI, Rank.TWO),
    ]
    
    state = GameState(players=[p0, p1], table_cards=table, deck=[])
    
    legal_moves = state.legal_moves()
    
    # Should have 2 capture options: (4+3) and (5+2)
    assert_eq(len(legal_moves), 2, "Should have 2 combination capture options")
    
    # Verify both sum to 7
    for move in legal_moves:
        assert_true(move.is_capture, "Move should be a capture")
        total = sum(c.value for c in move.captured_cards)
        assert_eq(total, 7, f"Capture should sum to 7, got {total}")
    
    print("  PASSED")


def test_no_capture_available():
    """
    Test: When no capture possible, must play to table.
    """
    print("Testing: no capture available...")
    
    # Setup: Player has a 5, table has cards that don't sum to 5
    p0 = PlayerState(player_id=0, hand=[Card(Suit.DENARI, Rank.FIVE)])
    p1 = PlayerState(player_id=1, hand=[])
    table = [
        Card(Suit.CUPS, Rank.ACE),   # value 1
        Card(Suit.SWORDS, Rank.TWO), # value 2
    ]
    
    state = GameState(players=[p0, p1], table_cards=table, deck=[])
    
    legal_moves = state.legal_moves()
    
    # Should have 1 move: play to table
    assert_eq(len(legal_moves), 1, "Should have 1 move (no capture)")
    assert_true(not legal_moves[0].is_capture, "Move should NOT be a capture")
    
    print("  PASSED")


def test_mandatory_capture():
    """
    Test: Capture is mandatory - cannot choose to play to table if capture exists.
    """
    print("Testing: mandatory capture...")
    
    # Setup: Player has a card that can capture
    p0 = PlayerState(player_id=0, hand=[Card(Suit.DENARI, Rank.SEVEN)])
    p1 = PlayerState(player_id=1, hand=[])
    table = [Card(Suit.CUPS, Rank.SEVEN)]
    
    state = GameState(players=[p0, p1], table_cards=table, deck=[])
    
    legal_moves = state.legal_moves()
    
    # Should only have capture option, not the option to play to table
    assert_eq(len(legal_moves), 1, "Should only have capture option")
    assert_true(legal_moves[0].is_capture, "Only move should be a capture")
    
    print("  PASSED")


def test_chkobba_counted():
    """
    Test: Chkobba (clearing table) is counted when not the final play.
    """
    print("Testing: chkobba counted...")
    
    # Setup: Player captures all table cards, hands and deck still have cards
    p0 = PlayerState(player_id=0, hand=[Card(Suit.DENARI, Rank.SEVEN)])
    p1 = PlayerState(player_id=1, hand=[Card(Suit.CUPS, Rank.ACE)])  # Other player has card
    table = [Card(Suit.SWORDS, Rank.SEVEN)]
    
    state = GameState(players=[p0, p1], table_cards=table, deck=[Card(Suit.CLUBS, Rank.TWO)])  # Deck not empty
    state.current_player = 0
    
    # Player 0 captures the 7
    move = Move(
        played_card=Card(Suit.DENARI, Rank.SEVEN),
        captured_cards=(Card(Suit.SWORDS, Rank.SEVEN),)
    )
    
    state.apply_move(move)
    
    # Should have 1 chkobba
    assert_eq(state.players[0].chkobbas, 1, "Should have 1 chkobba")
    
    print("  PASSED")


def test_final_play_not_chkobba():
    """
    Test: Clearing table on final play does NOT count as chkobba.
    """
    print("Testing: final play not chkobba...")
    
    # Setup: Final play of the round (both hands empty, deck empty after this)
    p0 = PlayerState(player_id=0, hand=[Card(Suit.DENARI, Rank.SEVEN)])
    p1 = PlayerState(player_id=1, hand=[])  # Other player has no cards
    table = [Card(Suit.SWORDS, Rank.SEVEN)]
    
    state = GameState(players=[p0, p1], table_cards=table, deck=[])  # Empty deck
    state.current_player = 0
    
    # Player 0 captures the 7 (this is the last card in play)
    move = Move(
        played_card=Card(Suit.DENARI, Rank.SEVEN),
        captured_cards=(Card(Suit.SWORDS, Rank.SEVEN),)
    )
    
    state.apply_move(move)
    
    # Should NOT count as chkobba
    assert_eq(state.players[0].chkobbas, 0, "Final play should NOT be a chkobba")
    
    print("  PASSED")


def test_remaining_cards_to_last_capturer():
    """
    Test: At round end, remaining table cards go to last capturer.
    """
    print("Testing: remaining cards to last capturer...")
    
    # Setup: Both players empty-handed, deck empty, cards on table
    p0 = PlayerState(player_id=0, hand=[], captured_cards=[])
    p1 = PlayerState(player_id=1, hand=[], captured_cards=[])
    table = [Card(Suit.DENARI, Rank.SEVEN), Card(Suit.CUPS, Rank.ACE)]
    
    state = GameState(players=[p0, p1], table_cards=table, deck=[])
    state.last_capturer = 1  # Player 1 was last to capture
    
    # Trigger end-of-round
    assert_true(state.is_round_over, "Round should be over")
    state._collect_remaining_table_cards()
    
    # Table cards should go to player 1
    assert_eq(len(state.players[1].captured_cards), 2, "Player 1 should get remaining cards")
    assert_eq(len(state.table_cards), 0, "Table should be empty")
    
    print("  PASSED")


def test_scoring_most_cards():
    """
    Test: Most cards scoring.
    """
    print("Testing: most cards...")
    
    # Setup: Player 0 has more cards than Player 1, but equal denari (both 0)
    # and no sevens/denari to avoid other points
    p0 = PlayerState(player_id=0, hand=[], captured_cards=[Card(Suit.CUPS, Rank.ACE)] * 21)
    p1 = PlayerState(player_id=1, hand=[], captured_cards=[Card(Suit.SWORDS, Rank.ACE)] * 19)
    
    state = GameState(players=[p0, p1], table_cards=[], deck=[])
    
    points = state.round_points()
    
    # Player 0 should get the "most cards" point (and that's it)
    assert_eq(points[0], 1, "Player 0 should get most cards point")
    assert_eq(points[1], 0, "Player 1 should get no points")
    
    print("  PASSED")


def test_scoring_most_denari():
    """
    Test: Most denari (diamonds) scoring.
    """
    print("Testing: most denari...")
    
    # Setup: Player 0 has more denari, but equal card count to isolate denari point
    p0_cards = [Card(Suit.DENARI, Rank.ACE), Card(Suit.DENARI, Rank.TWO)] + [Card(Suit.CUPS, Rank.FOUR)]
    p1_cards = [Card(Suit.DENARI, Rank.THREE)] + [Card(Suit.CUPS, Rank.FIVE), Card(Suit.SWORDS, Rank.SIX)]
    # Both have 3 cards, p0 has 2 denari, p1 has 1 denari
    
    p0 = PlayerState(player_id=0, hand=[], captured_cards=p0_cards)
    p1 = PlayerState(player_id=1, hand=[], captured_cards=p1_cards)
    
    state = GameState(players=[p0, p1], table_cards=[], deck=[])
    
    points = state.round_points()
    
    # Player 0 should get the "most denari" point (and that's it since card count is tied)
    assert_eq(points[0], 1, "Player 0 should get most denari point")
    assert_eq(points[1], 0, "Player 1 should get no points")
    
    print("  PASSED")


def test_scoring_seven_of_denari():
    """
    Test: 7 of denari scoring.
    """
    print("Testing: 7 of denari...")
    
    # Setup: Player 0 has 7 of denari, equal cards/denari to isolate this point
    p0_cards = [Card(Suit.DENARI, Rank.SEVEN), Card(Suit.CUPS, Rank.ACE)]
    p1_cards = [Card(Suit.SWORDS, Rank.TWO), Card(Suit.DENARI, Rank.THREE)]
    # Both have 2 cards, 1 denari each (tied), no other scoring cards
    
    p0 = PlayerState(player_id=0, hand=[], captured_cards=p0_cards)
    p1 = PlayerState(player_id=1, hand=[], captured_cards=p1_cards)
    
    state = GameState(players=[p0, p1], table_cards=[], deck=[])
    
    points = state.round_points()
    
    # Player 0 should get the "7 of denari" point (and that's it)
    assert_eq(points[0], 1, "Player 0 should get 7 of denari point")
    assert_eq(points[1], 0, "Player 1 should get no points")
    
    print("  PASSED")


def test_barmila_three_sevens():
    """
    Test: Barmila - player with 3+ sevens gets the point.
    """
    print("Testing: barmila three sevens...")
    
    cards0 = [
        Card(Suit.DENARI, Rank.SEVEN),
        Card(Suit.CUPS, Rank.SEVEN),
        Card(Suit.SWORDS, Rank.SEVEN),
    ]
    cards1 = [Card(Suit.CLUBS, Rank.SEVEN)]  # Only 1 seven
    
    p0, p1 = tunisian_barmila_points(cards0, cards1)
    
    assert_eq(p0, 1, "Player 0 with 3 sevens should get barmila point")
    assert_eq(p1, 0, "Player 1 should get no barmila point")
    
    print("  PASSED")


def test_barmila_tied_sevens():
    """
    Test: Barmila - tied on sevens (2-2), check sixes.
    """
    print("Testing: barmila tied sevens check sixes...")
    
    # Both have 2 sevens
    cards0 = [
        Card(Suit.DENARI, Rank.SEVEN),
        Card(Suit.CUPS, Rank.SEVEN),
        Card(Suit.DENARI, Rank.SIX),
        Card(Suit.CUPS, Rank.SIX),
        Card(Suit.SWORDS, Rank.SIX),  # 3 sixes
    ]
    cards1 = [
        Card(Suit.SWORDS, Rank.SEVEN),
        Card(Suit.CLUBS, Rank.SEVEN),
        Card(Suit.CLUBS, Rank.SIX),
    ]  # Only 1 six
    
    p0, p1 = tunisian_barmila_points(cards0, cards1)
    
    assert_eq(p0, 1, "Player 0 with 3 sixes should get barmila point")
    assert_eq(p1, 0, "Player 1 should get no barmila point")
    
    print("  PASSED")


def test_barmila_tied_both():
    """
    Test: Barmila - tied on sevens (2-2) and tied on sixes (2-2) = no point.
    """
    print("Testing: barmila tied on both...")
    
    cards0 = [
        Card(Suit.DENARI, Rank.SEVEN),
        Card(Suit.CUPS, Rank.SEVEN),
        Card(Suit.DENARI, Rank.SIX),
        Card(Suit.CUPS, Rank.SIX),
    ]
    cards1 = [
        Card(Suit.SWORDS, Rank.SEVEN),
        Card(Suit.CLUBS, Rank.SEVEN),
        Card(Suit.SWORDS, Rank.SIX),
        Card(Suit.CLUBS, Rank.SIX),
    ]
    
    p0, p1 = tunisian_barmila_points(cards0, cards1)
    
    assert_eq(p0, 0, "No barmila point when tied on both")
    assert_eq(p1, 0, "No barmila point when tied on both")
    
    print("  PASSED")


def test_clone_integrity():
    """
    Test: Cloning state creates independent copy.
    """
    print("Testing: clone integrity...")
    
    state = create_initial_state(seed=42)
    original_table_len = len(state.table_cards)
    
    # Clone and modify
    cloned = state.clone()
    cloned.table_cards.pop()
    
    # Original should be unchanged
    assert_eq(len(state.table_cards), original_table_len, "Original state should be unchanged")
    assert_eq(len(cloned.table_cards), original_table_len - 1, "Clone should be modified")
    
    # Hands should be independent
    cloned.players[0].hand.pop()
    assert_true(
        len(state.players[0].hand) > len(cloned.players[0].hand),
        "Original hand should be unchanged"
    )
    
    print("  PASSED")


def test_full_game_completes():
    """
    Test: A full game runs to completion without errors.
    """
    print("Testing: full game completes...")
    
    state = create_initial_state(seed=123)
    max_moves = 200  # Safety limit
    move_count = 0
    
    while not state.is_round_over and move_count < max_moves:
        legal_moves = state.legal_moves()
        assert_true(len(legal_moves) > 0, f"No legal moves at move {move_count}")
        
        # Pick first legal move
        move = legal_moves[0]
        state.apply_move(move)
        move_count += 1
    
    assert_true(state.is_round_over, "Game should complete within move limit")
    
    # Should be able to compute points
    points = state.round_points()
    assert_eq(len(points), 2, "Should have points for both players")
    
    print(f"  PASSED (completed in {move_count} moves)")


# =============================================================================
# RUN ALL TESTS
# =============================================================================

TESTS = [
    test_exact_match_priority,
    test_combination_capture,
    test_no_capture_available,
    test_mandatory_capture,
    test_chkobba_counted,
    test_final_play_not_chkobba,
    test_remaining_cards_to_last_capturer,
    test_scoring_most_cards,
    test_scoring_most_denari,
    test_scoring_seven_of_denari,
    test_barmila_three_sevens,
    test_barmila_tied_sevens,
    test_barmila_tied_both,
    test_clone_integrity,
    test_full_game_completes,
]


def run_all_tests():
    """Run all tests and report results."""
    print("=" * 60)
    print("Chkobba Engine Test Suite")
    print("=" * 60)
    
    passed = 0
    failed = 0
    
    for test in TESTS:
        try:
            test()
            passed += 1
        except TestFailure as e:
            print(f"  FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1
    
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
