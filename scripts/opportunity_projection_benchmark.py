"""Leakage-safe opportunity/rate projection benchmarks for NBA and NFL."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
from collections import defaultdict
from itertools import groupby
from pathlib import Path
from typing import Any

_BASELINE_PATH = Path(__file__).with_name("walk_forward_backtest.py")
_SPEC = importlib.util.spec_from_file_location("walk_forward_backtest", _BASELINE_PATH)
_BASELINE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BASELINE)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(str(value).strip().replace(",", "").replace("+", ""))
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _minutes(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if ":" in text:
        parts = text.split(":")
        if len(parts) != 2:
            return None
        minutes = _number(parts[0])
        seconds = _number(parts[1])
        if minutes is None or seconds is None or seconds >= 60:
            return None
        return minutes + seconds / 60.0
    return _number(text)


def _pair_second(value: Any, separator: str) -> float | None:
    if value is None:
        return None
    parts = str(value).strip().split(separator)
    if len(parts) != 2:
        return None
    return _number(parts[1])


def _opportunity(sport: str, market: str, player_rows: list[dict[str, Any]]) -> float | None:
    if sport == "nba":
        stats = next((p.get("stats") for p in player_rows
                      if not p.get("category") and isinstance(p.get("stats"), dict)), None)
        if not isinstance(stats, dict):
            return None
        if market in ("PTS", "REB", "AST"):
            return _minutes(stats.get("minutes"))
        if market == "3PM":
            return _pair_second(
                stats.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"), "-"
            )
        return None

    category = {
        "PASS_YDS": "passing", "REC": "receiving",
        "REC_YDS": "receiving", "RUSH_YDS": "rushing",
    }.get(market)
    stats = next((p.get("stats") for p in player_rows
                  if p.get("category") == category and isinstance(p.get("stats"), dict)), None)
    if not isinstance(stats, dict):
        return None
    if market == "PASS_YDS":
        return _pair_second(stats.get("completions/passingAttempts"), "/")
    if market in ("REC", "REC_YDS"):
        return _number(stats.get("receivingTargets"))
    if market == "RUSH_YDS":
        return _number(stats.get("rushingAttempts"))
    return None


def run_opportunity_benchmark(
    sport: str, *, root: Path, windows: tuple[int, ...] = (5, 10, 20),
    min_history: int = 3,
) -> dict[str, Any]:
    if sport not in _BASELINE.SPORT_MARKETS:
        raise ValueError(f"unsupported sport: {sport}")
    if not windows or any(not isinstance(w, int) or w < 1 for w in windows):
        raise ValueError("windows must contain positive integers")
    if min_history < 1:
        raise ValueError("min_history must be positive")
    path = Path(root) / sport / "games.json"
    if not path.exists():
        return {"ok": False, "reason": "unavailable", "detail": f"no game file at {path}"}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": "invalid_data", "detail": str(exc)}
    games = raw.get("games") if isinstance(raw, dict) else None
    if not isinstance(games, dict):
        return {"ok": False, "reason": "invalid_shape", "detail": "games file has no games object"}

    target_rows, exclusions = _BASELINE._game_rows(sport, games)
    player_game_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event_id, game in games.items():
        if not isinstance(game, dict) or game.get("season_type_id") != 2:
            continue
        for player in game.get("players") or []:
            if not isinstance(player, dict) or player.get("dnp"):
                continue
            player_id = player.get("athlete_id")
            stats = player.get("stats")
            if player_id is None or not isinstance(stats, dict):
                continue
            player_game_rows[(str(event_id), str(player_id))].append(player)

    observations = []
    coverage: dict[str, dict[str, int]] = defaultdict(lambda: {"target_rows": 0, "with_opportunity": 0})
    for row in target_rows:
        coverage[row["market"]]["target_rows"] += 1
        player_rows = player_game_rows.get((row["event_id"], row["player_id"]), [])
        opportunity = _opportunity(sport, row["market"], player_rows)
        if opportunity is None:
            continue
        # Zero opportunity with positive production is internally inconsistent;
        # exclude it rather than inventing a rate.
        if opportunity == 0 and row["actual"] > 0:
            continue
        coverage[row["market"]]["with_opportunity"] += 1
        observations.append({**row, "opportunity": opportunity})

    observations.sort(key=lambda r: (r["event_time"], r["event_id"], r["player_id"], r["market"]))
    split_by_event = _BASELINE._split_events(target_rows)
    histories: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    predictions = []
    opportunity_predictions = []

    for _, grouped in groupby(observations, key=lambda r: r["event_time"]):
        batch = list(grouped)
        for row in batch:
            key = (row["player_id"], row["market"])
            history = histories[key]
            if len(history) < min_history:
                continue
            for window in windows:
                prior = history[-window:]
                opportunities = [item[1] for item in prior]
                rates = [(target / opportunity if opportunity > 0 else 0.0)
                         for target, opportunity in prior]
                projected_opportunity = sum(opportunities) / len(opportunities)
                projected_rate = sum(rates) / len(rates)
                opportunity_projection = projected_opportunity * projected_rate
                baseline_projection = sum(target for target, _ in prior) / len(prior)
                common = {
                    "event_id": row["event_id"],
                    "split": split_by_event[row["event_id"]],
                    "market": row["market"],
                    "actual": row["actual"],
                    "window": window,
                }
                opportunity_predictions.append({
                    "event_id": row["event_id"],
                    "split": split_by_event[row["event_id"]],
                    "market": row["market"],
                    "window": window,
                    "actual": row["opportunity"],
                    "projection": projected_opportunity,
                })
                predictions.append({**common, "method": "opportunity_rate_product",
                                    "projection": opportunity_projection})
                predictions.append({**common, "method": "matched_rolling_mean",
                                    "projection": baseline_projection})
        # Same-time events are all scored before any same-time actual is appended.
        for row in batch:
            histories[(row["player_id"], row["market"])].append(
                (row["actual"], row["opportunity"])
            )

    groups: dict[tuple[str, str, str, int], list[float]] = defaultdict(list)
    event_sets: dict[tuple[str, str, str, int], set[str]] = defaultdict(set)
    for row in predictions:
        key = (row["split"], row["market"], row["method"], row["window"])
        groups[key].append(row["actual"] - row["projection"])
        event_sets[key].add(row["event_id"])

    metrics = []
    for key in sorted(groups):
        split, market, method, window = key
        errors = groups[key]
        metrics.append({
            "split": split, "market": market, "method": method, "window_games": window,
            "n_player_game_predictions": len(errors), "n_events": len(event_sets[key]),
            "mae": sum(abs(e) for e in errors) / len(errors),
            "rmse": math.sqrt(sum(e * e for e in errors) / len(errors)),
            "mean_signed_error": sum(errors) / len(errors),
        })

    opportunity_groups: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in opportunity_predictions:
        opportunity_groups[(row["split"], row["market"], row["window"])].append(row)

    opportunity_metrics = []
    for key in sorted(opportunity_groups):
        split, market, window = key
        rows = opportunity_groups[key]
        errors = [row["actual"] - row["projection"] for row in rows]
        opportunity_metrics.append({
            "split": split, "market": market, "window_games": window,
            "n_player_game_predictions": len(rows),
            "n_events": len({row["event_id"] for row in rows}),
            "mae": sum(abs(error) for error in errors) / len(errors),
            "rmse": math.sqrt(sum(error * error for error in errors) / len(errors)),
            "mean_signed_error": sum(errors) / len(errors),
        })

    opportunity_validation_selections = []
    for market in sorted({row["market"] for row in opportunity_metrics}):
        candidates = [row for row in opportunity_metrics
                      if row["split"] == "validation" and row["market"] == market]
        candidates.sort(key=lambda row: (row["mae"], row["rmse"], row["window_games"]))
        if not candidates:
            opportunity_validation_selections.append({
                "market": market, "selected": None, "reason": "no_validation_predictions",
            })
            continue
        best = candidates[0]
        held_out = next((row for row in opportunity_metrics
                         if row["split"] == "test" and row["market"] == market
                         and row["window_games"] == best["window_games"]), None)
        opportunity_validation_selections.append({
            "market": market,
            "selected_window_games": best["window_games"],
            "validation_mae": best["mae"],
            "validation_rmse": best["rmse"],
            "held_out_test": held_out,
        })

    validation = [m for m in metrics if m["split"] == "validation"]
    rankings = []
    for market in sorted({m["market"] for m in validation}):
        candidates = [m for m in validation if m["market"] == market]
        candidates.sort(key=lambda m: (m["mae"], m["rmse"]))
        for rank, metric in enumerate(candidates, 1):
            rankings.append({
                "market": market, "rank": rank, "method": metric["method"],
                "window_games": metric["window_games"],
                "validation_mae": metric["mae"], "validation_rmse": metric["rmse"],
            })

    warnings = []
    for metric in metrics:
        if metric["split"] == "test" and metric["n_events"] < 30:
            warnings.append(
                f'{metric["market"]} {metric["method"]} window {metric["window_games"]}: '
                f'{metric["n_events"]} test events; descriptive only.'
            )
    for market, counts in coverage.items():
        if counts["target_rows"]:
            counts["coverage_pct"] = round(
                100.0 * counts["with_opportunity"] / counts["target_rows"], 2
            )
    return {
        "ok": True, "sport": sport, "windows": list(windows),
        "minimum_history": min_history,
        "events_in_regular_season_data": len({r["event_id"] for r in target_rows}),
        "opportunity_coverage": dict(sorted(coverage.items())),
        "prediction_rows": len(predictions),
        "split_event_counts": {
            split: len({r["event_id"] for r in target_rows if split_by_event.get(r["event_id"]) == split})
            for split in ("train", "validation", "test")
        },
        "exclusions": exclusions, "metrics": metrics,
        "opportunity_metrics": opportunity_metrics,
        "opportunity_validation_selections": opportunity_validation_selections,
        "validation_rankings": rankings, "evaluation_warnings": warnings,
        "limitations": [
            "Opportunity and efficiency inputs are derived only from the player's prior appearances.",
            "The opportunity-rate product is compared with a rolling mean on identical prediction samples for each market and window.",
            "Minutes, attempts, and targets are historical realized opportunities, not known future-game opportunities.",
            "No injuries, lineup changes, opponent, rest, sportsbook odds, EV, ROI, or calibrated probability distributions are modeled.",
            "Validation rankings are for research only; held-out test results are never used to rank candidates.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark lagged opportunity/rate player projections.")
    parser.add_argument("sport", choices=("nba", "nfl"))
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--windows", default="5,10,20")
    parser.add_argument("--min-history", type=int, default=3)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        windows = tuple(int(v.strip()) for v in args.windows.split(",") if v.strip())
        result = run_opportunity_benchmark(
            args.sport, root=args.data_root, windows=windows, min_history=args.min_history
        )
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
