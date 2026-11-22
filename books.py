"""Supported sportsbooks (display order matters). Never invents prices."""

from __future__ import annotations

# Ordered: FanDuel Indiana, DraftKings, bet365, Fanatics.
BOOKS: list[tuple[str, str]] = [
    ("fanduel_in", "FanDuel IN"),
    ("draftkings", "DraftKings"),
    ("bet365", "bet365"),
    ("fanatics", "Fanatics"),
]
BOOK_KEYS = [k for k, _ in BOOKS]
BOOK_LABELS = dict(BOOKS)

# The Odds API bookmaker keys (bet365 is not offered in the Odds API US region).
ODDS_API_BOOKMAKERS = {"fanduel_in": "fanduel", "draftkings": "draftkings", "bet365": "bet365", "fanatics": "fanatics"}

_ALIASES = {
    "fd": "fanduel_in", "fanduel": "fanduel_in", "fanduel_in": "fanduel_in", "fanduel indiana": "fanduel_in", "fd in": "fanduel_in",
    "dk": "draftkings", "draftkings": "draftkings",
    "b365": "bet365", "bet365": "bet365",
    "fanatics": "fanatics", "fanatics sportsbook": "fanatics", "fan": "fanatics", "fs": "fanatics",
}


def normalize_book(name: str | None) -> str | None:
    """Map a free-text book name/abbrev to a BOOK_KEYS entry (None if unsupported)."""
    if not name:
        return None
    key = str(name).strip().lower().replace("-", " ").replace("_", " ")
    key = " ".join(key.split())
    return _ALIASES.get(key) or _ALIASES.get(key.replace(" ", "_"))
