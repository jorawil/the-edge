"""Generate pregame NBA/NFL point forecasts from ESPN schedule, rosters, and prior box scores.

No sportsbook prices are fetched. Forecasts are written to the immutable
forecast-only archive and remain uncalibrated.
"""
from __future__ import annotations

import argparse
import json
import math
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from betting_model.features import build_league_efficiency_prior, build_player_history
from betting_model.forecast_archive import archive_forecasts
from betting_model.projection_engine import project_nba, project_nfl, project_rolling_mean

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
SPORT_PATHS = {"nba": "basketball/nba", "nfl": "football/nfl"}
SELECTION_VERSION = "walk-forward-selection-2026-11-22"
MARKET_SELECTIONS = {
    "nba": {
        "PTS": ("opportunity_rate", 10, 20.0),
        "REB": ("opportunity_rate", 20, 20.0),
        "AST": ("rolling_mean", 20, 0.0),
        "3PM": ("rolling_mean", 20, 0.0),
        "PRA": ("opportunity_rate", 10, 10.0),
    },
    "nfl": {
        "PASS_YDS": ("opportunity_rate", 20, 20.0),
        "REC": ("opportunity_rate", 20, 20.0),
        "REC_YDS": ("opportunity_rate", 20, 20.0),
        "RUSH_YDS": ("opportunity_rate", 5, 20.0),
    },
}


def fetch_json(url: str, *, attempts: int = 3) -> dict[str, Any]:
    last_error = "request failed"
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
            if not isinstance(payload, dict):
                raise ValueError("ESPN response is not a JSON object")
            return payload
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code != 429:
                raise RuntimeError(f"ESPN returned non-retryable HTTP {exc.code}: {exc.reason}") from exc
            last_error = f"HTTP {exc.code}: {exc.reason}"
            if attempt + 1 < attempts:
                time.sleep(attempt + 1)
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
            if attempt + 1 < attempts:
                time.sleep(attempt + 1)
    raise RuntimeError(f"ESPN request failed: {last_error}")


def _season_type_id(event: dict[str, Any]) -> int | None:
    season = event.get("season") or {}
    value = season.get("type")
    if isinstance(value, dict):
        value = value.get("id")
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _aware_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def upcoming_events(sport: str, *, start_day: date, days: int) -> tuple[list[dict[str, Any]], list[str]]:
    events: dict[str, dict[str, Any]] = {}
    errors = []
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        url = f"{ESPN_BASE}/{SPORT_PATHS[sport]}/scoreboard?dates={day:%Y%m%d}&limit=500"
        try:
            payload = fetch_json(url)
        except Exception as exc:
            errors.append(f"scoreboard {day.isoformat()}: {exc}")
            continue
        for event in payload.get("events") or []:
            if isinstance(event, dict) and event.get("id"):
                events[str(event["id"])] = event

    now = datetime.now(timezone.utc)
    output = []
    for event_id, event in events.items():
        if _season_type_id(event) != 2:
            continue
        start = _aware_time(event.get("date"))
        if start is None or start <= now:
            continue
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competitors = competitions[0].get("competitors") or []
        home = next((item for item in competitors if item.get("homeAway") == "home"), None)
        away = next((item for item in competitors if item.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        home_team, away_team = home.get("team") or {}, away.get("team") or {}
        if not home_team.get("id") or not away_team.get("id"):
            continue
        output.append({
            "event_id": event_id,
            "event_start": start.isoformat(),
            "home_team_id": str(home_team["id"]),
            "away_team_id": str(away_team["id"]),
            "home_team": home_team.get("abbreviation") or home_team.get("displayName"),
            "away_team": away_team.get("abbreviation") or away_team.get("displayName"),
        })
    output.sort(key=lambda row: (row["event_start"], row["event_id"]))
    return output, errors


def _walk_roster(node: Any, found: dict[str, dict[str, Any]]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_roster(item, found)
        return
    if not isinstance(node, dict):
        return
    player_id = node.get("id")
    name = node.get("displayName") or node.get("fullName") or node.get("name")
    if player_id is not None and name:
        position = node.get("position")
        if isinstance(position, dict):
            position = position.get("abbreviation") or position.get("name")
        found[str(player_id)] = {
            "player_id": str(player_id),
            "player_name": str(name),
            "position": str(position) if position else None,
            "roster_status": (node.get("status") or {}).get("name")
            if isinstance(node.get("status"), dict) else node.get("status"),
        }
    for key in ("athletes", "items", "players", "entries"):
        if key in node:
            _walk_roster(node[key], found)


def team_roster(sport: str, team_id: str) -> list[dict[str, Any]]:
    url = f"{ESPN_BASE}/{SPORT_PATHS[sport]}/teams/{team_id}/roster"
    payload = fetch_json(url)
    found: dict[str, dict[str, Any]] = {}
    _walk_roster(payload.get("athletes") or payload.get("players") or payload, found)
    return [found[key] for key in sorted(found)]


def _load_games(sport: str, root: Path) -> tuple[dict[str, Any], str | None]:
    path = Path(root) / sport / "games.json"
    if not path.exists():
        raise FileNotFoundError(f"missing historical data file: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    games = payload.get("games") if isinstance(payload, dict) else None
    if not isinstance(games, dict):
        raise ValueError(f"invalid historical data shape: {path}")
    updated = payload.get("updated")
    if updated and _aware_time(updated) is None:
        raise ValueError(f"historical data updated timestamp is invalid: {path}")
    return games, updated


def generate(
    sport: str,
    *,
    data_root: Path,
    archive_path: Path,
    days: int = 7,
    start_day: date | None = None,
) -> dict[str, Any]:
    if sport not in SPORT_PATHS:
        raise ValueError(f"unsupported sport: {sport}")
    if days < 1 or days > 14:
        raise ValueError("days must be between 1 and 14")
    games, data_updated = _load_games(sport, data_root)
    first_day = start_day or datetime.now(timezone.utc).date()
    events, errors = upcoming_events(sport, start_day=first_day, days=days)
    roster_cache: dict[str, list[dict[str, Any]]] = {}
    league_cache: dict[str, dict[str, Any]] = {}
    history_cache: dict[tuple[str, str], dict[str, Any]] = {}
    counts = Counter()
    by_market = Counter()
    snapshots_to_archive: list[dict[str, Any]] = []
    now_text = datetime.now(timezone.utc).isoformat()
    source = f"ESPN schedule/roster and historical box scores; {SELECTION_VERSION}"

    for event in events:
        event_start = event["event_start"]
        if data_updated and _aware_time(data_updated) and _aware_time(data_updated) > _aware_time(event_start):
            errors.append(f'{event["event_id"]}: historical data update is after event start')
            counts["event_skipped_input_after_start"] += 1
            continue
        teams = [
            (event["home_team_id"], event["home_team"], event["away_team"]),
            (event["away_team_id"], event["away_team"], event["home_team"]),
        ]
        for team_id, team, opponent in teams:
            if team_id not in roster_cache:
                try:
                    roster_cache[team_id] = team_roster(sport, team_id)
                except Exception as exc:
                    roster_cache[team_id] = []
                    errors.append(f"roster {sport} team {team_id}: {exc}")
            for player in roster_cache[team_id]:
                for market, (method, window, shrinkage) in MARKET_SELECTIONS[sport].items():
                    history_key = (player["player_id"], market)
                    if history_key not in history_cache:
                        history_cache[history_key] = build_player_history(
                            games, sport=sport, player_id=player["player_id"],
                            market=market, before_event_time=now_text,
                            data_updated_at=data_updated,
                            data_updated_source=f"historical-data/data/{sport}/games.json updated",
                        )
                    history_result = history_cache[history_key]
                    history = history_result["history"][-window:]
                    if len(history) < 3:
                        counts["insufficient_history"] += 1
                        continue
                    context = {
                        "projection_at": now_text,
                        "projection_input_at": history_result["projection_input_at"],
                        "data_sources": [
                            f"ESPN historical-data/data/{sport}/games.json",
                            "ESPN public schedule and roster",
                        ],
                    }
                    if method == "rolling_mean":
                        result = project_rolling_mean(market, history, context)
                        model_version = f"rolling-mean-v1:{sport}:{market}:w{window}:{SELECTION_VERSION}"
                    else:
                        if market not in league_cache:
                            league_cache[market] = build_league_efficiency_prior(
                                games, sport=sport, market=market, before_event_time=now_text
                            )
                        prior = league_cache[market].get("prior_efficiency")
                        result_context = {**context, "prior_efficiency": prior}
                        engine = project_nba if sport == "nba" else project_nfl
                        result = engine(
                            market, history, result_context,
                            shrinkage_strength=shrinkage,
                        )
                        model_version = (
                            f"opportunity-rate-v1:{sport}:{market}:w{window}:k{shrinkage:g}:"
                            f"{SELECTION_VERSION}"
                        )
                    if not result.get("ok"):
                        counts[result.get("reason") or "projection_failed"] += 1
                        continue
                    warnings = list(result.get("warnings") or [])
                    warnings.extend([
                        "injuries, confirmed starters, teammate availability, opponent, and game script are not adjusted",
                        "point forecast only; no calibrated probability distribution or betting recommendation",
                    ])
                    snapshot = {
                        "sport": sport,
                        "event_id": event["event_id"],
                        "event_start": event_start,
                        "player_id": player["player_id"],
                        "player_name": player["player_name"],
                        "team": team,
                        "opponent": opponent,
                        "market": market,
                        "projected_value": result["projected_value"],
                        "projected_opportunity": result.get("projected_opportunity"),
                        "projected_efficiency": result.get("projected_efficiency"),
                        "n_history": result["n_history"],
                        "n_opportunity": result["n_opportunity"],
                        "effective_opportunity_after_shrinkage": result.get("effective_opportunity_after_shrinkage"),
                        "sigma": result.get("sigma"),
                        "distribution": result.get("distribution"),
                        "calibrated": result.get("calibrated") is True,
                        "model_version": model_version,
                        "projection_at": now_text,
                        "projection_input_at": history_result["projection_input_at"],
                        "projection_input_source": history_result.get("projection_input_source"),
                        "source": source,
                        "warnings": warnings,
                    }
                    snapshots_to_archive.append(snapshot)

    batch = archive_forecasts(snapshots_to_archive, archive_path)
    for snapshot, saved in zip(snapshots_to_archive, batch["results"]):
        if saved.get("ok"):
            counts["archived"] += 1
            by_market[snapshot["market"]] += 1
        elif saved.get("reason") == "duplicate forecast snapshot":
            counts["duplicates"] += 1
        else:
            counts["archive_rejected"] += 1
            errors.append(
                f'{snapshot["event_id"]}/{snapshot["player_id"]}/{snapshot["market"]}: '
                f'{saved.get("reason")}'
            )

    return {
        "ok": True,
        "sport": sport,
        "days_ahead": days,
        "events_found": len(events),
        "players_in_rosters": sum(len(rows) for rows in roster_cache.values()),
        "forecasts_archived": counts["archived"],
        "duplicates": counts["duplicates"],
        "skipped": {key: value for key, value in counts.items()
                    if key not in {"archived", "duplicates"}},
        "forecasts_by_market": dict(sorted(by_market.items())),
        "errors": errors,
        "archive_path": str(archive_path),
        "model_selection_version": SELECTION_VERSION,
        "odds_api_calls": 0,
        "calibration_claimed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", choices=("nba", "nfl", "both"), required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    sports = ("nba", "nfl") if args.sport == "both" else (args.sport,)
    results = []
    for sport in sports:
        results.append(generate(
            sport, data_root=args.data_root, archive_path=args.archive,
            days=args.days, start_day=args.start_date,
        ))
    report = {"ok": all(row.get("ok") for row in results), "results": results}
    rendered = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
