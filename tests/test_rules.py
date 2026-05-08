"""
Chkobba rule validation tests.

Run with: python -m pytest tests/test_rules.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.game_state import (
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
# MANDATORY CAPTURE TESTS
# =============================================================================

def test_mandatory_capture_exact_match():
    """
    When a single card matches exactly, capture is mandatory.
    Player cannot choose to play to table instead.
    """
    print("Testing: mandatory capture (exact match)...")
    
    p0 = PlayerState(player_id=0, hand=[Card(Suit.DENARI, Rank.SEVEN)])
    p1 = PlayerState(player_id=1, hand=[])
    table = [Card(Suit.CUPS, Rank.SEVEN)]  # exact match
    
    state = GameState(players=[p0, p1], table_cards=table, deck=[])
    legal_moves = state.legal_moves()
    
    # Should only have capture option
    assert_eq(len(legal_moves), 1, "Should only have 1 legal move")
    assert_true(legal_moves[0].is_capture, "Must be a capture")
    assert_eq(legal_moves[0].captured_cards[0].rank, Rank.SEVEN, "Should capture the 7")
    
    print("  PASSED")


def test_mandatory_capture_combination():
    """
    When no exact match but sum combination exists, capture is mandatory.
    """
    print("Testing: mandatory capture (combination)...")
    
    p0 = PlayerState(player_id=0, hand=[Card(Suit.DENARI, Rank.SEVEN)])
    p1 = PlayerState(player_id=1, hand=[])
    table = [
        Card(Suit.CUPS, Rank.FOUR),
        Card(Suit.SWORDS, Rank.THREE),
    ]  # 4+3=7, no single 7
    
    state = GameState(players=[p0, p1], table_cards=table, deck=[])
    legal_moves = state.legal_moves()
    
    # Should have 1 capture option (the combination)
    assert_eq(len(legal_moves), 1, "Should have 1 combination capture")
    assert_true(legal_moves[0].is_capture, "Must be a capture")
    assert_eq(len(legal_moves[0].captured_cards), 2, "Should capture 2 cards")
    
    print("  PASSED")


# =============================================================================
# SINGLE-CARD PRIORITY TESTS
# =============================================================================

def test_single_card_priority_over_combination():
    """
    If a single card matches, it has priority over sum combinations.
    Player cannot choose to capture via combination instead.
    """
    print("Testing: single-card priority over combination...")
    
    p0 = PlayerState(player_id=0, hand=[Card(Suit.DENARI, Rank.SEVEN)])
    p1 = PlayerState(player_id=1, hand=[])
    table = [
        Card(Suit.CUPS, Rank.SEVEN),  # exact match
        Card(Suit.SWORDS, Rank.FOUR),
        Card(Suit.CLUBS, Rank.THREE),
    ]  # Both single 7 and 4+3 available
    
    state = GameState(players=[p0, p1], table_cards=table, deck=[])
    legal_moves = state.legal_moves()
    
    # Should only have single-card capture option
    assert_eq(len(legal_moves), 1, "Should only have exact match option")
    assert_eq(len(legal_moves[0].captured_cards), 1, "Should capture exactly 1 card")
    assert_eq(legal_moves[0].captured_cards[0].rank, Rank.SEVEN, "Should be the 7")
    
    print("  PASSED")


# =============================================================================
# SUM CAPTURE TESTS
# =============================================================================

def test_sum_capture_only_when_no_single_match():
    """
    Sum combinations are only allowed when no single-card match exists.
    """
    print("Testing: sum capture only when no single match...")
    
    p0 = PlayerState(player_id=0, hand=[Card(Suit.DENARI, Rank.SIX)])
    p1 = PlayerState(player_id=1, hand=[])
    table = [
        Card(Suit.CUPS, Rank.FOUR),
        Card(Suit.SWORDS, Rank.TWO),
    ]  # 4+2=6, no single 6
    
    state = GameState(players=[p0, p1], table_cards=table, deck=[])
    legal_moves = state.legal_moves()
    
    # Should have 1 combination option
    assert_eq(len(legal_moves), 1, "Should have 1 combination capture")
    assert_true(legal_moves[0].is_capture, "Must be a capture")
    assert_eq(len(legal_moves[0].captured_cards), 2, "Should capture 2 cards")
    
    total = sum(c.value for c in legal_moves[0].captured_cards)
    assert_eq(total, 6, "Capture should sum to 6")
    
    print("  PASSED")


def test_no_capture_available():
    """
    When no capture is possible, player must play to table.
    """
    print("Testing: no capture available...")
    
    p0 = PlayerState(player_id=0, hand=[Card(Suit.DENARI, Rank.FIVE)])
    p1 = PlayerState(player_id=1, hand=[])
    table = [
        Card(Suit.CUPS, Rank.ACE),  # 1
        Card(Suit.SWORDS, Rank.TWO), # 2
    ]  # No way to make 5
    
    state = GameState(players=[p0, p1], table_cards=table, deck=[])
    legal_moves = state.legal_moves()
    
    # Should have 1 non-capture option
    assert_eq(len(legal_moves), 1, "Should have 1 move")
    assert_true(not legal_moves[0].is_capture, "Should not be a capture")
    
    print("  PASSED")


# =============================================================================
# CHKOBBA TESTS
# =============================================================================

def test_chkobba_counted():
    """
    Clearing the table (chkobba) is counted when not the final play.
    """
    print("Testing: chkobba counted (not final play)...")
    
    p0 = PlayerState(player_id=0, hand=[Card(Suit.DENARI, Rank.SEVEN)])
    p1 = PlayerState(player_id=1, hand=[Card(Suit.CUPS, Rank.ACE)])  # Other player still has cards
    table = [Card(Suit.SWORDS, Rank.SEVEN)]
    
    state = GameState(players=[p0, p1], table_cards=table, deck=[])
    state.current_player = 0
    
    # Capture the table
    move = Move(
        played_card=Card(Suit.DENARI, Rank.SEVEN),
        captured_cards=(Card(Suit.SWORDS, Rank.SEVEN),)
    )
    
    assert_true(state.is_legal_move(move), "Move should be legal")
    
    before_chkobbas = state.players[0].chkobbas
    state.apply_move(move)
    after_chkobbas = state.players[0].chkobbas
    
    assert_eq(after_chkobbas - before_chkobbas, 1, "Should gain 1 chkobba")
    
    print("  PASSED")


def test_chkobba_not_counted_on_final_play():
    """
    Clearing the table on the final play does NOT count as chkobba.
    """
    print("Testing: chkobba NOT counted on final play...")
    
    p0 = PlayerState(player_id=0, hand=[Card(Suit.DENARI, Rank.SEVEN)])
    p1 = PlayerState(player_id=1, hand=[])  # Other player has no cards
    table = [Card(Suit.SWORDS, Rank.SEVEN)]
    
    state = GameState(players=[p0, p1], table_cards=table, deck=[])
    state.current_player = 0
    
    # Capture the table on final play
    move = Move(
        played_card=Card(Suit.DENARI, Rank.SEVEN),
        captured_cards=(Card(Suit.SWORDS, Rank.SEVEN),)
    )
    
    before_chkobbas = state.players[0].chkobbas
    state.apply_move(move)
    after_chkobbas = state.players[0].chkobbas
    
    assert_eq(after_chkobbas - before_chkobbas, 0, "Final play should NOT count as chkobba")
    
    print("  PASSED")


# =============================================================================
# END OF ROUND TESTS
# =============================================================================

def test_leftover_table_cards_to_last_capturer():
    """
    At end of round, remaining table cards go to the last player who captured.
    """
    print("Testing: leftover table cards to last capturer...")
    
    p0 = PlayerState(player_id=0, hand=[], captured_cards=[])
    p1 = PlayerState(player_id=1, hand=[], captured_cards=[])
    table = [Card(Suit.DENARI, Rank.SEVEN), Card(Suit.CUPS, Rank.ACE)]
    
    state = GameState(players=[p0, p1], table_cards=table, deck=[])
    state.last_capturer = 1  # Player 1 was last to capture
    
    # Trigger collection
    state._collect_remaining_table_cards()
    
    assert_eq(len(state.players[1].captured_cards), 2, "Player 1 should get remaining cards")
    assert_eq(len(state.table_cards), 0, "Table should be empty")
    
    print("  PASSED")


def test_no_last_capturer_no_collection():
    """
    If no one captured during the round, leftover cards stay on table.
    """
    print("Testing: no last capturer - cards stay on table...")
    
    p0 = PlayerState(player_id=0, hand=[], captured_cards=[])
    p1 = PlayerState(player_id=1, hand=[], captured_cards=[])
    table = [Card(Suit.DENARI, Rank.SEVEN)]
    
    state = GameState(players=[p0, p1], table_cards=table, deck=[])
    state.last_capturer = None  # No one captured
    
    # Trigger collection
    state._collect_remaining_table_cards()
    
    assert_eq(len(state.table_cards), 1, "Cards should remain on table")
    assert_eq(len(state.players[0].captured_cards), 0, "Player 0 should have no cards")
    assert_eq(len(state.players[1].captured_cards), 0, "Player 1 should have no cards")
    
    print("  PASSED")


# =============================================================================
# BARMILA SCORING TESTS
# =============================================================================

def test_barmila_three_sevens():
    """
    Player with 3 or more sevens gets the barmila point.
    """
    print("Testing: barmila - 3+ sevens...")
    
    cards0 = [
        Card(Suit.DENARI, Rank.SEVEN),
        Card(Suit.CUPS, Rank.SEVEN),
        Card(Suit.SWORDS, Rank.SEVEN),
    ]  # 3 sevens
    cards1 = [Card(Suit.CLUBS, Rank.SEVEN)]  # 1 seven
    
    p0, p1 = tunisian_barmila_points(cards0, cards1)
    
    assert_eq(p0, 1, "Player with 3 sevens should get barmila")
    assert_eq(p1, 0, "Other player should get no barmila")
    
    print("  PASSED")


def test_barmila_four_sevens():
    """
    Player with 4 sevens gets the barmila point.
    """
    print("Testing: barmila - 4 sevens...")
    
    cards0 = [
        Card(Suit.DENARI, Rank.SEVEN),
        Card(Suit.CUPS, Rank.SEVEN),
        Card(Suit.SWORDS, Rank.SEVEN),
        Card(Suit.CLUBS, Rank.SEVEN),
    ]  # 4 sevens
    cards1 = []  # 0 sevens
    
    p0, p1 = tunisian_barmila_points(cards0, cards1)
    
    assert_eq(p0, 1, "Player with 4 sevens should get barmila")
    assert_eq(p1, 0, "Other player should get no barmila")
    
    print("  PASSED")


def test_barmila_tied_sevens_check_sixes():
    """
    When tied 2-2 on sevens, check sixes for tiebreaker.
    """
    print("Testing: barmila - tied sevens, check sixes...")
    
    # Both have 2 sevens
    cards0 = [
        Card(Suit.DENARI, Rank.SEVEN),
        Card(Suit.CUPS, Rank.SEVEN),
        Card(Suit.SWORDS, Rank.SIX),
        Card(Suit.CLUBS, Rank.SIX),
        Card(Suit.DENARI, Rank.SIX),  # 3 sixes
    ]
    cards1 = [
        Card(Suit.SWORDS, Rank.SEVEN),
        Card(Suit.CLUBS, Rank.SEVEN),
        Card(Suit.CUPS, Rank.SIX),  # 1 six
    ]
    
    p0, p1 = tunisian_barmila_points(cards0, cards1)
    
    assert_eq(p0, 1, "Player with 3 sixes should get barmila")
    assert_eq(p1, 0, "Other player should get no barmila")
    
    print("  PASSED")


def test_barmila_tied_both_no_point():
    """
    When tied 2-2 on sevens AND 2-2 on sixes, no barmila point.
    """
    print("Testing: barmila - tied on both sevens and sixes...")
    
    cards0 = [
        Card(Suit.DENARI, Rank.SEVEN),
        Card(Suit.CUPS, Rank.SEVEN),
        Card(Suit.SWORDS, Rank.SIX),
        Card(Suit.CLUBS, Rank.SIX),
    ]  # 2 sevens, 2 sixes
    cards1 = [
        Card(Suit.SWORDS, Rank.SEVEN),
        Card(Suit.CLUBS, Rank.SEVEN),
        Card(Suit.DENARI, Rank.SIX),
        Card(Suit.CUPS, Rank.SIX),
    ]  # 2 sevens, 2 sixes
    
    p0, p1 = tunisian_barmila_points(cards0, cards1)
    
    assert_eq(p0, 0, "No barmila point when tied on both")
    assert_eq(p1, 0, "No barmila point when tied on both")
    
    print("  PASSED")


def test_barmila_one_seven_each_no_point():
    """
    When each player has only 1 seven, no one gets barmila.
    """
    print("Testing: barmila - 1 seven each...")
    
    cards0 = [Card(Suit.DENARI, Rank.SEVEN)]
    cards1 = [Card(Suit.CUPS, Rank.SEVEN)]
    
    p0, p1 = tunisian_barmila_points(cards0, cards1)
    
    assert_eq(p0, 0, "No barmila with only 1 seven")
    assert_eq(p1, 0, "No barmila with only 1 seven")
    
    print("  PASSED")


# =============================================================================
# RUN ALL TESTS
# =============================================================================

TESTS = [
    # Mandatory capture
    test_mandatory_capture_exact_match,
    test_mandatory_capture_combination,
    
    # Single-card priority
    test_single_card_priority_over_combination,
    
    # Sum capture
    test_sum_capture_only_when_no_single_match,
    test_no_capture_available,
    
    # Chkobba
    test_chkobba_counted,
    test_chkobba_not_counted_on_final_play,
    
    # End of round
    test_leftover_table_cards_to_last_capturer,
    test_no_last_capturer_no_collection,
    
    # Barmila
    test_barmila_three_sevens,
    test_barmila_four_sevens,
    test_barmila_tied_sevens_check_sixes,
    test_barmila_tied_both_no_point,
    test_barmila_one_seven_each_no_point,
]


def run_all_tests():
    """Run all tests and report results."""
    print("=" * 60)
    print("Chkobba Rule Tests")
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
