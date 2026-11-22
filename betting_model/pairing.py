"""Pair Over and Under prices from the same book, line, and time.

If anything is ambiguous, the other side is left blank. A no-vig
probability is never guessed.
"""

from __future__ import annotations

from typing import Any

from .core import DEFAULT_MAX_SIDE_SKEW_SECONDS, canonical_market, parse_side
from .timeutil import parse_aware_datetime, seconds_apart


def _book_name(prop: dict[str, Any]) -> str | None:
    book = prop.get("book") or prop.get("sportsbook")
    if book is None:
        return None
    text = str(book).strip()
    return text.casefold() if text else None


def _pair_key(prop: dict[str, Any]) -> tuple | None:
    book = _book_name(prop)
    if not book:
        return None
    player = str(prop.get("player_id") or prop.get("player") or "").strip().casefold()
    market = canonical_market(str(prop.get("market") or ""))
    if not player or not market or prop.get("line") is None:
        return None
    try:
        line = round(float(prop["line"]), 6)
    except (TypeError, ValueError):
        return None
    return (
        player,
        str(prop.get("sport") or "").strip().casefold(),
        market,
        line,
        book,
    )


def attach_opposites(
    props: list[dict[str, Any]],
    *,
    max_side_skew_seconds: float = DEFAULT_MAX_SIDE_SKEW_SECONDS,
) -> list[dict[str, Any]]:
    """Return copies. Existing ``opposite`` values are left alone."""
    copies = [dict(prop) if isinstance(prop, dict) else prop for prop in props]
    groups: dict[tuple, list[int]] = {}
    for index, prop in enumerate(copies):
        if not isinstance(prop, dict) or prop.get("opposite"):
            continue
        key = _pair_key(prop)
        if key is None:
            continue
        groups.setdefault(key, []).append(index)

    for indexes in groups.values():
        overs = []
        unders = []
        for index in indexes:
            try:
                side_over = parse_side(copies[index].get("is_over"))
            except ValueError:
                continue
            if side_over:
                overs.append(index)
            else:
                unders.append(index)
        if len(overs) != 1 or len(unders) != 1:
            for index in indexes:
                copies[index]["fair_pair"] = "ambiguous_or_incomplete"
            continue
        over = copies[overs[0]]
        under = copies[unders[0]]
        over_ts = parse_aware_datetime(over.get("retrieved_at"))
        under_ts = parse_aware_datetime(under.get("retrieved_at"))
        if over_ts is None or under_ts is None:
            over["fair_pair"] = "missing_timestamp"
            under["fair_pair"] = "missing_timestamp"
            continue
        if seconds_apart(over_ts, under_ts) > max_side_skew_seconds:
            over["fair_pair"] = "timestamps_too_far_apart"
            under["fair_pair"] = "timestamps_too_far_apart"
            continue
        over["opposite"] = {
            "odds": under.get("odds"),
            "line": under.get("line"),
            "retrieved_at": under.get("retrieved_at"),
        }
        under["opposite"] = {
            "odds": over.get("odds"),
            "line": over.get("line"),
            "retrieved_at": over.get("retrieved_at"),
        }
        over["fair_pair"] = "paired"
        under["fair_pair"] = "paired"
    return copies
