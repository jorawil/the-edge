"""Parlay notes. This version does not estimate a joint probability.

Legs are graded one at a time. Same-game and cross-game parlays are both
labeled correlation_not_modeled. Multiplying the leg probabilities is not
a parlay probability, so that product is not computed and the ticket is
not called model-approved.
"""

from __future__ import annotations

from typing import Any

from .core import canonical_market, implied_probability, parse_american_odds, profit_per_unit
from .sports import evaluate_prop

# Pairs that often move together. This list is a warning, not a correlation model.
RELATED_MARKETS = (
    frozenset({"PASS_YDS", "REC_YDS"}),
    frozenset({"PASS_YDS", "RECEPTIONS"}),
    frozenset({"PASS_YDS", "PASS_TDS"}),
    frozenset({"PASS_YDS", "COMPLETIONS"}),
    frozenset({"RUSH_YDS", "PASS_YDS"}),
    frozenset({"PTS", "REB"}),
    frozenset({"PTS", "AST"}),
    frozenset({"PTS", "PRA"}),
    frozenset({"REB", "AST"}),
)


def _event_key(leg: dict[str, Any]) -> str | None:
    for field in ("event_id", "game_id", "game"):
        value = leg.get(field)
        if value:
            return str(value).strip().casefold()
    return None


def _related(legs: list[dict[str, Any]]) -> list[str]:
    notes = []
    events = [_event_key(leg) for leg in legs]
    markets = [canonical_market(str(leg.get("market") or "")) for leg in legs]
    players = [str(leg.get("player") or "").strip().casefold() for leg in legs]
    if any(event and events.count(event) > 1 for event in events):
        notes.append("at least two legs share an event id")
    if any(event is None for event in events):
        notes.append("one or more legs have no event id, so same-game correlation could not be checked")
    for index, left in enumerate(markets):
        for right, other_player in zip(markets[index + 1 :], players[index + 1 :]):
            pair = frozenset({left, right})
            if left and right and pair in RELATED_MARKETS:
                notes.append(f"{left} and {right} can move together")
            if left and left == right and other_player and other_player != players[index]:
                notes.append(f"two legs are the same market ({left})")
    if any(player and players.count(player) > 1 for player in players):
        notes.append("the same player appears on more than one leg")
    return notes


def evaluate_parlay(
    legs: list[dict[str, Any]],
    *,
    quoted_american_odds: int | None = None,
    quoted_book: str | None = None,
    as_of: Any = None,
    file_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Grade the legs and stop before inventing a parlay probability."""
    if not isinstance(legs, list) or len(legs) < 2:
        return {
            "ok": False,
            "status": "NOT_EVALUABLE",
            "model_approved": False,
            "correlation": "correlation_not_modeled",
            "joint_model_probability": None,
            "reason": "a parlay needs at least two legs",
            "legs": [],
        }
    evaluated = []
    for leg in legs:
        if not isinstance(leg, dict):
            evaluated.append(
                {
                    "evaluated": False,
                    "status": "NOT_EVALUABLE",
                    "skip_reason": "leg must be a JSON object",
                }
            )
            continue
        evaluated.append(evaluate_prop(leg, as_of=as_of, file_meta=file_meta))
    quote: dict[str, Any]
    if quoted_american_odds is None:
        quote = {
            "american_odds": None,
            "implied_probability": None,
            "profit_per_unit_if_win": None,
            "book": quoted_book,
            "note": "No parlay price was supplied, so none was invented.",
        }
    else:
        try:
            odds_value = parse_american_odds(quoted_american_odds)
            quote = {
                "american_odds": odds_value,
                "implied_probability": round(implied_probability(odds_value), 6),
                "profit_per_unit_if_win": round(profit_per_unit(odds_value), 6),
                "book": quoted_book,
                "note": "This is the quoted parlay price, not a price built by multiplying the legs.",
            }
        except (ValueError, TypeError) as exc:
            quote = {
                "american_odds": None,
                "implied_probability": None,
                "book": quoted_book,
                "note": f"The quoted parlay price could not be read ({exc}).",
            }
    reasons = _related(legs)
    weak = [row.get("player") for row in evaluated if row.get("status") != "QUALIFIED"]
    if weak:
        reasons.append("not every leg is qualified on its own")
    return {
        "ok": True,
        "status": "NOT_EVALUABLE",
        "model_approved": False,
        "correlation": "correlation_not_modeled",
        "joint_model_probability": None,
        "independent_product_shown": False,
        "reason": (
            "No joint probability was calculated. Multiplying the legs would "
            "pretend they are independent, and that model does not exist here."
        ),
        "warnings": reasons,
        "quoted_payout": quote,
        "expected_value_per_unit": None,
        "legs": evaluated,
        "leg_statuses": [row.get("status") for row in evaluated],
    }
