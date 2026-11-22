"""Leakage-safe historical rolling-mean benchmarks for NFL and NBA player stats.

This is a forecast evaluation tool, not a betting model. It uses only earlier
appearances for each player, keeps games together in chronological splits,
requires stable ESPN athlete IDs, and never reads sportsbook odds or prices.
It intentionally reports several fixed lookback windows rather than choosing
a winner on the held-out test set.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from typing import Any

SPORT_MARKETS = {
    "nba": {
        "PTS": ("", ("points", "PTS")),
        "REB": ("", ("rebounds", "REB")),
        "AST": ("", ("assists", "AST")),
        # ESPN box-score display labels can be abbreviations (3PT) or
        # descriptive keys, depending on the response shape.
        "3PM": ("", ("threePointFieldGoalsMade-threePointFieldGoalsAttempted",
                     "threePointFieldGoalsMade", "3PT", "3P")),
    },
    "nfl": {
        # Category disambiguates the common ESPN YDS display label.
        "PASS_YDS": ("passing", ("passingYards", "YDS")),
        "RUSH_YDS": ("rushing", ("rushingYards", "YDS")),
        "REC_YDS": ("receiving", ("receivingYards", "YDS")),
        "REC": ("receiving", ("receptions", "REC")),
    },
}
DEFAULT_WINDOWS = (5, 10, 20)
MIN_HISTORY = 3
DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _event_time(game: dict[str, Any]) -> datetime | None:
    raw = game.get("date_utc")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _stat_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    # ESPN represents made/attempted stats as "made-attempted".
    text = str(value).strip()
    if "-" in text:
        text = text.split("-", 1)[0]
    try:
        number = float(text.replace(",", "").replace("+", ""))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _game_rows(sport: str, games: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    markets = SPORT_MARKETS[sport]
    rows: list[dict[str, Any]] = []
    counts = defaultdict(int)
    for event_id, game in games.items():
        if not isinstance(game, dict):
            counts["invalid_game"] += 1
            continue
        # New backfills explicitly record season type. Missing metadata is
        # excluded rather than silently mixing unknown competitions.
        if game.get("season_type_id") != 2:
            counts["excluded_non_regular_or_unknown_season"] += 1
            continue
        event_time = _event_time(game)
        if event_time is None:
            counts["excluded_missing_or_naive_timestamp"] += 1
            continue
        seen: set[tuple[str, str]] = set()
        for player in game.get("players") or []:
            if not isinstance(player, dict) or player.get("dnp"):
                continue
            athlete_id = player.get("athlete_id")
            if athlete_id is None or str(athlete_id).strip() == "":
                counts["excluded_missing_athlete_id"] += 1
                continue
            stats = player.get("stats") or {}
            if not isinstance(stats, dict):
                continue
            for market, (category, keys) in markets.items():
                if category and player.get("category") != category:
                    continue
                candidates = [key for key in keys if key in stats]
                if not candidates:
                    continue
                parsed_values = [
                    (key, _stat_number(stats.get(key))) for key in candidates
                ]
                valid = next(
                    ((key, value) for key, value in parsed_values if value is not None),
                    None,
                )
                if valid is None:
                    counts["excluded_invalid_stat"] += 1
                    continue
                _, value = valid
                identity = (str(athlete_id), market)
                if identity in seen:
                    counts["excluded_duplicate_player_market"] += 1
                    continue
                seen.add(identity)
                rows.append({
                    "event_id": str(event_id),
                    "event_time": event_time,
                    "player_id": str(athlete_id),
                    "player": player.get("player"),
                    "market": market,
                    "actual": value,
                })
    return rows, dict(counts)


def _split_events(rows: list[dict[str, Any]]) -> dict[str, str]:
    event_times: dict[str, datetime] = {}
    for row in rows:
        event_times[row["event_id"]] = min(
            event_times.get(row["event_id"], row["event_time"]), row["event_time"]
        )
    # Events with the same kickoff instant are one chronological unit.
    # This prevents a simultaneous slate from being split across boundaries.
    ordered_times = sorted(set(event_times.values()))
    total = len(ordered_times)
    train_end = int(total * 0.60)
    validation_end = int(total * 0.80)
    split_by_time = {
        event_time: ("train" if i < train_end else
                    "validation" if i < validation_end else "test")
        for i, event_time in enumerate(ordered_times)
    }
    return {
        event_id: split_by_time[event_time]
        for event_id, event_time in event_times.items()
    }


def run_backtest(
    sport: str,
    *,
    root: Path | None = None,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
    min_history: int = MIN_HISTORY,
) -> dict[str, Any]:
    if sport not in SPORT_MARKETS:
        raise ValueError(f"unsupported sport: {sport}")
    if not windows or any(not isinstance(w, int) or w < 1 for w in windows):
        raise ValueError("windows must contain positive integers")
    if min_history < 1:
        raise ValueError("min_history must be positive")
    data_root = Path(root) if root is not None else DATA_DIR
    path = data_root / sport / "games.json"
    if not path.exists():
        return {"ok": False, "reason": "unavailable", "detail": f"no game file at {path}"}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": "invalid_data", "detail": str(exc)}
    games = raw.get("games") if isinstance(raw, dict) else None
    if not isinstance(games, dict):
        return {"ok": False, "reason": "invalid_shape", "detail": "games file has no games object"}

    rows, exclusions = _game_rows(sport, games)
    rows.sort(key=lambda row: (row["event_time"], row["event_id"], row["player_id"], row["market"]))
    split_by_event = _split_events(rows)
    histories: dict[tuple[str, str], list[float]] = defaultdict(list)
    scored: list[dict[str, Any]] = []

    # Score every game at a kickoff timestamp against history available
    # strictly before that timestamp. Append all actuals only after every
    # same-time event has been forecast, preventing same-time look-ahead.
    for _, same_time_rows in groupby(rows, key=lambda item: item["event_time"]):
        batch = list(same_time_rows)
        for row in batch:
            key = (row["player_id"], row["market"])
            history = histories[key]
            split = split_by_event[row["event_id"]]
            if len(history) >= min_history:
                for window in windows:
                    prior = history[-window:]
                    prediction = sum(prior) / len(prior)
                    scored.append({
                        "event_id": row["event_id"],
                        "event_time": row["event_time"].isoformat(),
                        "split": split,
                        "player_id": row["player_id"],
                        "player": row["player"],
                        "market": row["market"],
                        "window": window,
                        "history_games": len(prior),
                        "projection": prediction,
                        "actual": row["actual"],
                        "error": row["actual"] - prediction,
                    })
        for row in batch:
            histories[(row["player_id"], row["market"])].append(row["actual"])

    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in scored:
        groups[(row["split"], row["market"], row["window"])].append(row)

    metrics = []
    for (split, market, window), values in sorted(groups.items()):
        errors = [item["error"] for item in values]
        metrics.append({
            "split": split,
            "market": market,
            "window_games": window,
            "n_player_game_predictions": len(values),
            "n_events": len({item["event_id"] for item in values}),
            "mae": sum(abs(error) for error in errors) / len(errors),
            "rmse": math.sqrt(sum(error * error for error in errors) / len(errors)),
            "mean_signed_error": sum(errors) / len(errors),
        })

    test_metrics = [item for item in metrics if item["split"] == "test"]
    evaluation_warnings = []
    if not test_metrics:
        evaluation_warnings.append(
            "No test-split forecasts were available; predictive performance cannot be assessed."
        )
    for item in test_metrics:
        if item["n_events"] < 30:
            evaluation_warnings.append(
                f'{item["market"]} window {item["window_games"]}: test metrics use '
                f'{item["n_events"]} events, fewer than the 30-event review minimum; '
                "treat them as descriptive only, not evidence of predictive skill."
            )

    return {
        "ok": True,
        "sport": sport,
        "method": "historical rolling mean; prediction uses only prior appearances",
        "windows": list(windows),
        "minimum_history": min_history,
        "events_in_regular_season_data": len({row["event_id"] for row in rows}),
        "player_market_actual_rows": len(rows),
        "prediction_rows": len(scored),
        "split_event_counts": {
            split: len({row["event_id"] for row in rows if split_by_event.get(row["event_id"]) == split})
            for split in ("train", "validation", "test")
        },
        "exclusions": exclusions,
        "metrics": metrics,
        "minimum_test_events_for_descriptive_review": 30,
        "evaluation_warnings": evaluation_warnings,
        "limitations": [
            "This is a baseline benchmark, not a calibrated projection model.",
            "No sportsbook odds, historical prices, edge, EV, profit, or ROI are evaluated.",
            "No projection is generated for a player-market without enough prior appearances.",
            "Only regular-season records with explicit season_type_id=2 and timezone-aware timestamps are used.",
            "Players are joined by ESPN athlete ID; rows without IDs are excluded.",
            "The train/validation/test split is chronological by event and keeps each event in one split.",
            "Fixed windows are reported separately; no window is selected using the held-out test slice.",
            "This does not account for injuries, minutes, role changes, opponents, rest, or lineup context.",
        ],
        "metrics_definition": {
            "mae": "mean absolute error in stat units; lower is better",
            "rmse": "root mean squared error in stat units; lower is better",
            "mean_signed_error": "actual minus projection; positive means underprojection",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Leakage-safe historical rolling-mean benchmark.")
    parser.add_argument("sport", choices=sorted(SPORT_MARKETS))
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--windows", default="5,10,20", help="Comma-separated prior-game windows.")
    parser.add_argument("--min-history", type=int, default=MIN_HISTORY)
    parser.add_argument("--out", type=Path, default=None, help="Optional JSON output path.")
    args = parser.parse_args(argv)
    try:
        windows = tuple(int(value.strip()) for value in args.windows.split(",") if value.strip())
        result = run_backtest(args.sport, root=args.data_root, windows=windows,
                              min_history=args.min_history)
    except ValueError as exc:
        parser.error(str(exc))
    payload = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
