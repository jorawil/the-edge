"""Check a prop before any probability is calculated.

A bad value becomes a message that names the prop. It does not become a guess.
"""

from __future__ import annotations

from typing import Any

from .core import parse_american_odds, parse_finite_number, parse_side


def prop_label(prop: dict[str, Any] | None) -> str:
    if not isinstance(prop, dict):
        return "this prop"
    player = prop.get("player") or prop.get("player_id") or "unknown player"
    market = prop.get("market") or "unknown market"
    return f"{player} / {market}"


def price_errors(prop: dict[str, Any]) -> list[str]:
    """Errors that make the price itself unusable. Empty list means usable."""
    label = prop_label(prop)
    errors: list[str] = []
    if not prop.get("player") and not prop.get("player_id"):
        errors.append(f"{label}: player name or player_id is required")
    if not str(prop.get("sport") or "").strip():
        errors.append(f"{label}: sport is required")
    if not str(prop.get("market") or "").strip():
        errors.append(f"{label}: market is required")
    if "line" not in prop or prop.get("line") is None:
        errors.append(f"{label}: line is required")
    else:
        try:
            parse_finite_number(prop.get("line"), "line")
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
    if "odds" not in prop or prop.get("odds") is None:
        errors.append(f"{label}: American odds are required")
    else:
        try:
            parse_american_odds(prop.get("odds"))
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
    if "is_over" not in prop or prop.get("is_over") is None:
        errors.append(f"{label}: Over/Under selection is required")
    else:
        try:
            parse_side(prop.get("is_over"))
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
    if prop.get("sigma") is not None:
        try:
            sigma = parse_finite_number(prop.get("sigma"), "sigma")
            if sigma <= 0:
                errors.append(f"{label}: sigma must be positive")
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
    if prop.get("projected") is not None:
        try:
            parse_finite_number(prop.get("projected"), "projected")
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
    return errors
