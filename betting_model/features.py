"""Pre-event player history extraction for the NBA/NFL projection engine.

Only regular-season, completed player-game observations with stable ESPN IDs
and parseable opportunity are returned. The caller supplies the target event
time; observations at or after that instant are excluded.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .projection_engine import NBA_MARKETS, NFL_MARKETS


def _event_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "").replace("+", "")
    try:
        result = float(text)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _minutes(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if ":" not in text:
        return _number(text)
    parts = text.split(":")
    if len(parts) != 2:
        return None
    minutes, seconds = _number(parts[0]), _number(parts[1])
    if minutes is None or seconds is None or seconds >= 60:
        return None
    return minutes + seconds / 60.0


def _pair(value: Any, separator: str, index: int) -> float | None:
    if value is None:
        return None
    parts = str(value).strip().split(separator)
    if len(parts) != 2:
        return None
    return _number(parts[index])


def _market_values(sport: str, market: str, player_rows: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    if sport == "nba":
        row = next((item for item in player_rows if not item.get("category")), None)
        stats = (row or {}).get("stats") or {}
        actual_keys = {"PTS": ("points", "PTS"), "REB": ("rebounds", "REB"),
                       "AST": ("assists", "AST")}
        if market in actual_keys:
            actual = next((_number(stats.get(key)) for key in actual_keys[market]
                           if stats.get(key) is not None), None)
            opportunity = _minutes(stats.get("minutes"))
            return actual, opportunity
        if market == "3PM":
            made = _pair(stats.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"), "-", 0)
            attempts = _pair(stats.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"), "-", 1)
            if made is None:
                made = _number(stats.get("threePointFieldGoalsMade"))
            return made, attempts
        return None, None

    category, actual_keys, opportunity_key, separator, index = {
        "PASS_YDS": ("passing", ("passingYards", "YDS"), "completions/passingAttempts", "/", 1),
        "RUSH_YDS": ("rushing", ("rushingYards", "YDS"), "rushingAttempts", None, None),
        "REC_YDS": ("receiving", ("receivingYards", "YDS"), "receivingTargets", None, None),
        "REC": ("receiving", ("receptions", "REC"), "receivingTargets", None, None),
    }[market]
    row = next((item for item in player_rows if item.get("category") == category), None)
    stats = (row or {}).get("stats") or {}
    actual = next((_number(stats.get(key)) for key in actual_keys if stats.get(key) is not None), None)
    opportunity = (_pair(stats.get(opportunity_key), separator, index)
                   if separator else _number(stats.get(opportunity_key)))
    return actual, opportunity


def build_player_history(
    games: dict[str, Any],
    *,
    sport: str,
    player_id: str,
    market: str,
    before_event_time: str | datetime,
) -> dict[str, Any]:
    """Build prior actual/opportunity pairs for one ESPN athlete ID and market."""
    allowed = NBA_MARKETS if sport == "nba" else NFL_MARKETS if sport == "nfl" else set()
    if market not in allowed:
        raise ValueError(f"unsupported {sport} market: {market}")
    cutoff = _event_time(before_event_time) if isinstance(before_event_time, str) else before_event_time
    if cutoff is None or cutoff.tzinfo is None:
        raise ValueError("before_event_time must be timezone-aware")
    cutoff = cutoff.astimezone(timezone.utc)

    grouped: list[tuple[datetime, str, list[dict[str, Any]]]] = []
    for event_id, game in games.items():
        if not isinstance(game, dict) or game.get("season_type_id") != 2:
            continue
        when = _event_time(game.get("date_utc"))
        if when is None or when >= cutoff:
            continue
        player_rows = [row for row in game.get("players") or []
                       if isinstance(row, dict) and not row.get("dnp")
                       and str(row.get("athlete_id") or "") == str(player_id)]
        if player_rows:
            grouped.append((when, str(event_id), player_rows))

    history = []
    exclusions = {"missing_actual": 0, "missing_opportunity": 0, "invalid_zero_opportunity": 0}
    for when, event_id, player_rows in sorted(grouped, key=lambda item: (item[0], item[1])):
        actual, opportunity = _market_values(sport, market, player_rows)
        if actual is None:
            exclusions["missing_actual"] += 1
            continue
        if opportunity is None:
            exclusions["missing_opportunity"] += 1
            continue
        if opportunity == 0 and actual > 0:
            exclusions["invalid_zero_opportunity"] += 1
            continue
        history.append({
            "event_id": event_id,
            "event_time": when.isoformat(),
            "actual": actual,
            "opportunity": opportunity,
            "team": next((row.get("team") for row in player_rows if row.get("team")), None),
        })

    return {
        "ok": True,
        "sport": sport,
        "player_id": str(player_id),
        "market": market,
        "history": history,
        "n_history": len(history),
        "latest_observation_at": history[-1]["event_time"] if history else None,
        # ESPN backfill stores game times, not the time the source data became
        # available. Do not fabricate an ingestion timestamp for archive use.
        "projection_input_at": None,
        "warnings": ["source ingestion timestamp is not recorded; projection_input_at remains unknown"],
        "exclusions": exclusions,
    }
