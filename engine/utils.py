"""Shared utilities for card formatting and display."""

from __future__ import annotations

from typing import List

from engine.game_state import Card, Suit, Rank


# Card formatting constants
SUIT_SYMBOLS = {
    Suit.DENARI: "♦",
    Suit.CUPS: "♥",
    Suit.SWORDS: "♠",
    Suit.CLUBS: "♣",
}

RANK_NAMES = {
    Rank.ACE: "A",
    Rank.TWO: "2",
    Rank.THREE: "3",
    Rank.FOUR: "4",
    Rank.FIVE: "5",
    Rank.SIX: "6",
    Rank.SEVEN: "7",
    Rank.JACK: "J",
    Rank.KNIGHT: "Q",
    Rank.KING: "K",
}


def card_to_str(card: Card) -> str:
    """Convert a card to a readable string representation.
    
    Args:
        card: The card to format
        
    Returns:
        String like "7♦" (rank + suit symbol)
    """
    return f"{RANK_NAMES[card.rank]}{SUIT_SYMBOLS[card.suit]}"


def format_hand(cards: List[Card]) -> str:
    """Format a hand of cards for display.
    
    Args:
        cards: List of cards in hand
        
    Returns:
        String like "[0]A♦  [1]7♥  [2]K♠"
    """
    return "  ".join(f"[{i}]{card_to_str(c)}" for i, c in enumerate(cards))


def format_table(cards: List[Card]) -> str:
    """Format table cards for display.
    
    Args:
        cards: Cards on the table
        
    Returns:
        String like "A♦ 7♥ K♠" or "(empty)"
    """
    if not cards:
        return "(empty)"
    return "  ".join(card_to_str(c) for c in cards)
