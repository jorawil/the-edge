"""Leakage-safe opportunity × efficiency projection primitives.

The caller supplies only observations available before the target event. This
module does not fetch data, apply sportsbook prices, estimate probabilities,
or claim calibration. Context adjustments are intentionally not applied until
they have been tested out of sample.
"""
from __future__ import annotations

import math
from typing import Any

MODEL_VERSION = "opportunity-rate-v1"
NBA_MARKETS = {"PTS", "REB", "AST", "3PM"}
NFL_MARKETS = {"PASS_YDS", "RUSH_YDS", "REC_YDS", "REC"}


def _finite_nonnegative(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _clean_history(history: list[dict[str, Any]]) -> list[tuple[float, float]]:
    rows = []
    for row in history:
        if not isinstance(row, dict):
            continue
        actual = _finite_nonnegative(row.get("actual"))
        opportunity = _finite_nonnegative(row.get("opportunity"))
        if actual is None or opportunity is None:
            continue
        if opportunity == 0 and actual > 0:
            continue
        rows.append((actual, opportunity))
    return rows


def _project(
    market: str,
    history: list[dict[str, Any]],
    context: dict[str, Any] | None,
    *,
    allowed: set[str],
    shrinkage_strength: float,
) -> dict[str, Any]:
    if market not in allowed:
        raise ValueError(f"unsupported market: {market}")
    if not math.isfinite(float(shrinkage_strength)) or shrinkage_strength < 0:
        raise ValueError("shrinkage_strength must be a finite non-negative number")
    rows = _clean_history(history)
    context = context or {}
    warnings: list[str] = []
    if len(rows) < 3:
        return {
            "ok": False, "reason": "insufficient_history", "market": market,
            "n_history": len(rows), "model_version": MODEL_VERSION,
            "calibrated": False,
        }

    opportunities = [opportunity for _, opportunity in rows]
    total_opportunity = sum(opportunities)
    if total_opportunity <= 0:
        return {
            "ok": False, "reason": "no_positive_opportunity_history",
            "market": market, "n_history": len(rows),
            "model_version": MODEL_VERSION, "calibrated": False,
        }

    prior_rate = _finite_nonnegative(context.get("prior_efficiency"))
    total_actual = sum(actual for actual, _ in rows)
    if prior_rate is None:
        # Do not silently invent a league prior. A raw rate is still useful as
        # a transparent baseline, but it is not shrunk or calibration-ready.
        efficiency = total_actual / total_opportunity
        effective_opportunity = total_opportunity
        warnings.append("no pre-event league/position efficiency prior; shrinkage not applied")
    else:
        efficiency = (total_actual + shrinkage_strength * prior_rate) / (
            total_opportunity + shrinkage_strength
        )
        effective_opportunity = total_opportunity + shrinkage_strength

    projected_opportunity = sum(opportunities) / len(opportunities)
    projected_value = projected_opportunity * efficiency
    if len(rows) < 10:
        warnings.append(f"only {len(rows)} prior player-games")
    if context.get("role_change") is True:
        warnings.append("role change flagged; adjustment is not modelled")
    if context.get("injury_uncertain") is True:
        warnings.append("availability is uncertain; no injury adjustment applied")
    if context.get("opponent") and context.get("opponent_games", 0) < 10:
        warnings.append("opponent sample is under 10 games or unavailable")
    if context.get("home") is not None or context.get("rest_days") is not None:
        warnings.append("schedule context recorded but not adjusted; adjustment requires validation")

    return {
        "ok": True,
        "sport": "nba" if market in NBA_MARKETS else "nfl",
        "market": market,
        "projected_value": projected_value,
        "projected_opportunity": projected_opportunity,
        "projected_efficiency": efficiency,
        "efficiency_unit": "per opportunity",
        "n_history": len(rows),
        "n_opportunity": len(rows),
        "total_historical_opportunity": total_opportunity,
        "effective_opportunity_after_shrinkage": effective_opportunity,
        "shrinkage_strength": shrinkage_strength if prior_rate is not None else None,
        "prior_efficiency": prior_rate,
        "model_version": MODEL_VERSION,
        "calibrated": False,
        "distribution": None,
        "sigma": None,
        "interval_80": None,
        "interval_90": None,
        "projection_at": context.get("projection_at"),
        "projection_input_at": context.get("projection_input_at"),
        "data_sources": context.get("data_sources") or [],
        "warnings": warnings,
    }


def project_nba(
    market: str,
    history: list[dict[str, Any]],
    context: dict[str, Any] | None = None,
    *,
    shrinkage_strength: float = 0.0,
) -> dict[str, Any]:
    """Project PTS/REB/AST per minute or 3PM per 3PA."""
    return _project(
        market, history, context, allowed=NBA_MARKETS,
        shrinkage_strength=shrinkage_strength,
    )


def project_nfl(
    market: str,
    history: list[dict[str, Any]],
    context: dict[str, Any] | None = None,
    *,
    shrinkage_strength: float = 0.0,
) -> dict[str, Any]:
    """Project passing/rushing yards per attempt, receiving per target, or catches per target."""
    return _project(
        market, history, context, allowed=NFL_MARKETS,
        shrinkage_strength=shrinkage_strength,
    )
