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
        if market == "PRA":
            components = [_number(stats.get(key)) for key in ("points", "rebounds", "assists")]
            actual = sum(components) if all(value is not None for value in components) else None
            return actual, _minutes(stats.get("minutes"))
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
    data_updated_at: str | None = None,
    data_updated_source: str | None = None,
) -> dict[str, Any]:
    """Build prior actual/opportunity pairs for one ESPN athlete ID and market."""
    allowed = NBA_MARKETS if sport == "nba" else NFL_MARKETS if sport == "nfl" else set()
    if market not in allowed:
        raise ValueError(f"unsupported {sport} market: {market}")
    cutoff = _event_time(before_event_time) if isinstance(before_event_time, str) else before_event_time
    if cutoff is None or cutoff.tzinfo is None:
        raise ValueError("before_event_time must be timezone-aware")
    cutoff = cutoff.astimezone(timezone.utc)
    input_time, input_text = (None, None)
    if data_updated_at:
        input_time, input_text = _event_time(data_updated_at), str(data_updated_at).strip()
        if input_time is None:
            raise ValueError("data_updated_at must be timezone-aware")
        if input_time > cutoff:
            raise ValueError("data_updated_at is after the target event time")

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
        "projection_input_at": input_text,
        "projection_input_source": data_updated_source if input_text else None,
        "warnings": ([] if input_text else ["source ingestion timestamp is not recorded; projection_input_at remains unknown"]),
        "exclusions": exclusions,
    }



def build_league_efficiency_prior(
    games: dict[str, Any],
    *,
    sport: str,
    market: str,
    before_event_time: str | datetime,
) -> dict[str, Any]:
    """Compute a league-wide rate from regular-season games strictly before a target event."""
    allowed = NBA_MARKETS if sport == "nba" else NFL_MARKETS if sport == "nfl" else set()
    if market not in allowed:
        raise ValueError(f"unsupported {sport} market: {market}")
    cutoff = _event_time(before_event_time) if isinstance(before_event_time, str) else before_event_time
    if cutoff is None or cutoff.tzinfo is None:
        raise ValueError("before_event_time must be timezone-aware")
    cutoff = cutoff.astimezone(timezone.utc)

    total_actual = 0.0
    total_opportunity = 0.0
    event_ids: set[str] = set()
    players: set[str] = set()
    for event_id, game in games.items():
        if not isinstance(game, dict) or game.get("season_type_id") != 2:
            continue
        when = _event_time(game.get("date_utc"))
        if when is None or when >= cutoff:
            continue
        grouped: dict[str, list[dict[str, Any]]] = {}
        for player in game.get("players") or []:
            if not isinstance(player, dict) or player.get("dnp") or player.get("athlete_id") is None:
                continue
            grouped.setdefault(str(player["athlete_id"]), []).append(player)
        for player_id, player_rows in grouped.items():
            actual, opportunity = _market_values(sport, market, player_rows)
            if actual is None or opportunity is None or (opportunity == 0 and actual > 0):
                continue
            total_actual += actual
            total_opportunity += opportunity
            event_ids.add(str(event_id))
            players.add(player_id)

    return {
        "ok": total_opportunity > 0,
        "sport": sport,
        "market": market,
        "prior_efficiency": total_actual / total_opportunity if total_opportunity > 0 else None,
        "total_actual": total_actual,
        "total_opportunity": total_opportunity,
        "n_events": len(event_ids),
        "n_players": len(players),
        "as_of": cutoff.isoformat(),
    }



def build_history_index(
    games: dict[str, Any],
    *,
    sport: str,
    markets: list[str] | tuple[str, ...],
    before_event_time: str | datetime,
    data_updated_at: str | None = None,
    data_updated_source: str | None = None,
) -> dict[str, Any]:
    """Index all prior player-market histories in one pass for daily forecast generation."""
    allowed = NBA_MARKETS if sport == "nba" else NFL_MARKETS if sport == "nfl" else set()
    requested = tuple(dict.fromkeys(markets))
    unsupported = [market for market in requested if market not in allowed]
    if unsupported:
        raise ValueError(f"unsupported {sport} markets: {', '.join(unsupported)}")
    cutoff = _event_time(before_event_time) if isinstance(before_event_time, str) else before_event_time
    if cutoff is None or cutoff.tzinfo is None:
        raise ValueError("before_event_time must be timezone-aware")
    cutoff = cutoff.astimezone(timezone.utc)
    input_time, input_text = (None, None)
    if data_updated_at:
        input_time, input_text = _event_time(data_updated_at), str(data_updated_at).strip()
        if input_time is None:
            raise ValueError("data_updated_at must be timezone-aware")
        if input_time > cutoff:
            raise ValueError("data_updated_at is after the target event time")

    eligible = []
    for event_id, game in games.items():
        if not isinstance(game, dict) or game.get("season_type_id") != 2:
            continue
        when = _event_time(game.get("date_utc"))
        if when is not None and when < cutoff:
            eligible.append((when, str(event_id), game))
    eligible.sort(key=lambda item: (item[0], item[1]))

    histories: dict[tuple[str, str], list[dict[str, Any]]] = {}
    totals = {market: [0.0, 0.0, set(), set()] for market in requested}
    exclusions = {market: {"missing_actual": 0, "missing_opportunity": 0,
                           "invalid_zero_opportunity": 0} for market in requested}
    for when, event_id, game in eligible:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for player in game.get("players") or []:
            if not isinstance(player, dict) or player.get("dnp") or player.get("athlete_id") is None:
                continue
            grouped.setdefault(str(player["athlete_id"]), []).append(player)
        for player_id, player_rows in grouped.items():
            team = next((row.get("team") for row in player_rows if row.get("team")), None)
            for market in requested:
                actual, opportunity = _market_values(sport, market, player_rows)
                if actual is None:
                    exclusions[market]["missing_actual"] += 1
                    continue
                if opportunity is None:
                    exclusions[market]["missing_opportunity"] += 1
                    continue
                if opportunity == 0 and actual > 0:
                    exclusions[market]["invalid_zero_opportunity"] += 1
                    continue
                histories.setdefault((player_id, market), []).append({
                    "event_id": event_id,
                    "event_time": when.isoformat(),
                    "actual": actual,
                    "opportunity": opportunity,
                    "team": team,
                })
                totals[market][0] += actual
                totals[market][1] += opportunity
                totals[market][2].add(event_id)
                totals[market][3].add(player_id)

    league_priors = {}
    for market in requested:
        actual, opportunity, event_ids, players = totals[market]
        league_priors[market] = {
            "ok": opportunity > 0,
            "sport": sport,
            "market": market,
            "prior_efficiency": actual / opportunity if opportunity > 0 else None,
            "total_actual": actual,
            "total_opportunity": opportunity,
            "n_events": len(event_ids),
            "n_players": len(players),
            "as_of": cutoff.isoformat(),
        }

    return {
        "ok": True,
        "sport": sport,
        "as_of": cutoff.isoformat(),
        "projection_input_at": input_text,
        "projection_input_source": data_updated_source if input_text else None,
        "histories": histories,
        "league_priors": league_priors,
        "eligible_events": len(eligible),
        "player_market_histories": len(histories),
        "exclusions": exclusions,
        "warnings": ([] if input_text else [
            "source ingestion timestamp is not recorded; projection_input_at remains unknown"
        ]),
    }
