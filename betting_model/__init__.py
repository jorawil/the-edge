"""Probability-first betting edge model. Never places bets."""

from .core import implied_probability, calculate_edge, SIGMA_DEFAULTS
from .nba import get_nba_player_recent_avg
from .odds import fetch_live_odds
from .books import BOOKS, BOOK_KEYS, normalize_book
from .sports import (
    evaluate_nba_prop,
    evaluate_wnba_prop,
    evaluate_nfl_prop,
    evaluate_mlb_prop,
    evaluate_prop,
)

__all__ = [
    "implied_probability",
    "calculate_edge",
    "SIGMA_DEFAULTS",
    "get_nba_player_recent_avg",
    "fetch_live_odds",
    "BOOKS",
    "BOOK_KEYS",
    "normalize_book",
    "evaluate_nba_prop",
    "evaluate_wnba_prop",
    "evaluate_nfl_prop",
    "evaluate_mlb_prop",
    "evaluate_prop",
]

__version__ = "0.1.0"
