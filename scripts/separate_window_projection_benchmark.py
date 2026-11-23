"""Chronological benchmark with independent opportunity and efficiency windows.

The candidate forecast is mean prior opportunity over one window multiplied by
an efficiency rate estimated over a separately chosen window. All candidates
use the same eligible target rows; selection is validation-only and test
metrics are reported only after selection.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
from collections import defaultdict
from itertools import groupby
from pathlib import Path
from typing import Any

from betting_model.features import _market_values

_BASELINE_PATH = Path(__file__).with_name("walk_forward_backtest.py")
_SPEC = importlib.util.spec_from_file_location("walk_forward_backtest", _BASELINE_PATH)
_BASELINE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BASELINE)

_OPP_PATH = Path(__file__).with_name("opportunity_projection_benchmark.py")
_OPP_SPEC = importlib.util.spec_from_file_location("opportunity_projection_benchmark", _OPP_PATH)
_OPP = importlib.util.module_from_spec(_OPP_SPEC)
_OPP_SPEC.loader.exec_module(_OPP)

DEFAULT_WINDOWS = (5, 10, 20)
DEFAULT_SHRINKAGE = (0.0, 5.0, 10.0, 20.0)


def _add_metric(store: dict, key: tuple, error: float, event_id: str) -> None:
    item = store[key]
    item["sum_abs"] += abs(error)
    item["sum_sq"] += error * error
    item["sum_error"] += error
    item["n"] += 1
    item["events"].add(event_id)
    by_event = item.setdefault("by_event", {})
    event_stats = by_event.setdefault(event_id, [0.0, 0])
    event_stats[0] += abs(error)
    event_stats[1] += 1


def _paired_event_bootstrap(
    candidate: dict[str, Any],
    baseline: dict[str, Any],
    *,
    iterations: int = 1000,
    seed: int = 20261123,
) -> dict[str, Any]:
    """Paired bootstrap of event-cluster MAE differences on untouched test data."""
    candidate_events = candidate.get("by_event") or {}
    baseline_events = baseline.get("by_event") or {}
    common = sorted(set(candidate_events) & set(baseline_events))
    if len(common) < 30:
        return {
            "available": False,
            "n_paired_events": len(common),
            "reason": "at least 30 paired test events are required for this diagnostic",
        }
    rng = random.Random(seed)
    deltas = []
    for _ in range(iterations):
        sampled = [rng.choice(common) for _ in range(len(common))]
        candidate_abs = candidate_n = baseline_abs = baseline_n = 0
        for event_id in sampled:
            c_abs, c_n = candidate_events[event_id]
            b_abs, b_n = baseline_events[event_id]
            candidate_abs += c_abs
            candidate_n += c_n
            baseline_abs += b_abs
            baseline_n += b_n
        if candidate_n and baseline_n:
            deltas.append(candidate_abs / candidate_n - baseline_abs / baseline_n)
    deltas.sort()
    if not deltas:
        return {"available": False, "n_paired_events": len(common), "reason": "no valid bootstrap samples"}
    lower = deltas[int(0.025 * (len(deltas) - 1))]
    upper = deltas[int(0.975 * (len(deltas) - 1))]
    candidate_mae = candidate["sum_abs"] / candidate["n"]
    baseline_mae = baseline["sum_abs"] / baseline["n"]
    if upper < 0:
        interpretation = "candidate_better"
    elif lower > 0:
        interpretation = "rolling_mean_better"
    else:
        interpretation = "inconclusive"
    return {
        "available": True,
        "n_paired_events": len(common),
        "iterations": iterations,
        "candidate_test_mae": candidate_mae,
        "rolling_mean_test_mae": baseline_mae,
        "observed_mae_difference": candidate_mae - baseline_mae,
        "mae_difference_ci95": [lower, upper],
        "interpretation": interpretation,
        "definition": "candidate MAE minus rolling-mean MAE; negative favors candidate",
    }


def _render_metrics(store: dict, *, key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    rows = []
    for key, item in sorted(store.items(), key=lambda pair: str(pair[0])):
        n = item["n"]
        row = dict(zip(key_fields, key))
        row.update({
            "n_player_game_predictions": n,
            "n_events": len(item["events"]),
            "mae": item["sum_abs"] / n,
            "rmse": math.sqrt(item["sum_sq"] / n),
            "mean_signed_error": item["sum_error"] / n,
        })
        rows.append(row)
    return rows


def benchmark(
    sport: str,
    *,
    root: Path,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
    shrinkage_values: tuple[float, ...] = DEFAULT_SHRINKAGE,
    min_history: int = 3,
) -> dict[str, Any]:
    if sport not in _BASELINE.SPORT_MARKETS:
        raise ValueError(f"unsupported sport: {sport}")
    if not windows or any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in windows):
        raise ValueError("windows must be positive integers")
    if not isinstance(min_history, int) or isinstance(min_history, bool) or min_history < 3:
        raise ValueError("min_history must be at least 3")
    if not shrinkage_values or any(not math.isfinite(value) or value < 0 for value in shrinkage_values):
        raise ValueError("shrinkage values must be finite and non-negative")

    path = Path(root) / sport / "games.json"
    if not path.exists():
        return {"ok": False, "reason": "unavailable", "detail": f"no game file at {path}"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": "invalid_data", "detail": str(exc)}
    games = payload.get("games") if isinstance(payload, dict) else None
    if not isinstance(games, dict):
        return {"ok": False, "reason": "invalid_shape", "detail": "games file has no games object"}

    target_rows, exclusions = _BASELINE._game_rows(sport, games)
    if sport == "nba":
        for event_id, game in games.items():
            if not isinstance(game, dict) or game.get("season_type_id") != 2:
                continue
            event_time = _BASELINE._event_time(game)
            if event_time is None:
                continue
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for player in game.get("players") or []:
                if isinstance(player, dict) and not player.get("dnp") and player.get("athlete_id") is not None:
                    grouped[str(player["athlete_id"])].append(player)
            for player_id, player_rows in grouped.items():
                actual, _ = _market_values("nba", "PRA", player_rows)
                if actual is not None:
                    target_rows.append({
                        "event_id": str(event_id), "event_time": event_time,
                        "player_id": player_id, "market": "PRA", "actual": actual,
                    })

    rows_by_event_player: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event_id, game in games.items():
        if not isinstance(game, dict) or game.get("season_type_id") != 2:
            continue
        for player in game.get("players") or []:
            if isinstance(player, dict) and not player.get("dnp") and player.get("athlete_id") is not None:
                rows_by_event_player[(str(event_id), str(player["athlete_id"]))].append(player)

    observations = []
    coverage: dict[str, dict[str, int]] = defaultdict(lambda: {"target_rows": 0, "with_opportunity": 0})
    for row in target_rows:
        coverage[row["market"]]["target_rows"] += 1
        player_rows = rows_by_event_player.get((row["event_id"], row["player_id"]), [])
        if sport == "nba" and row["market"] == "PRA" and player_rows:
            base = next((p for p in player_rows if not p.get("category")), player_rows[0])
            opportunity = _OPP._minutes((base.get("stats") or {}).get("minutes"))
        else:
            opportunity = _OPP._opportunity(sport, row["market"], player_rows)
        if opportunity is None or (opportunity == 0 and row["actual"] > 0):
            continue
        coverage[row["market"]]["with_opportunity"] += 1
        observations.append({**row, "opportunity": opportunity})

    observations.sort(key=lambda row: (
        row["event_time"], row["event_id"], row["player_id"], row["market"]
    ))
    split_by_event = _BASELINE._split_events(target_rows)
    histories: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    league_totals: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    candidate_store: dict = defaultdict(lambda: {
        "sum_abs": 0.0, "sum_sq": 0.0, "sum_error": 0.0, "n": 0, "events": set()
    })
    baseline_store: dict = defaultdict(lambda: {
        "sum_abs": 0.0, "sum_sq": 0.0, "sum_error": 0.0, "n": 0, "events": set()
    })

    for _, grouped in groupby(observations, key=lambda row: row["event_time"]):
        batch = list(grouped)
        for row in batch:
            history = histories[(row["player_id"], row["market"])]
            if len(history) < min_history:
                continue
            split = split_by_event[row["event_id"]]
            market_actual, market_opportunity = league_totals[row["market"]]
            league_rate = market_actual / market_opportunity if market_opportunity > 0 else None

            for efficiency_window in windows:
                efficiency_history = history[-efficiency_window:]
                baseline_projection = sum(actual for actual, _ in efficiency_history) / len(efficiency_history)
                baseline_error = row["actual"] - baseline_projection
                _add_metric(
                    baseline_store,
                    (split, row["market"], efficiency_window),
                    baseline_error, row["event_id"],
                )

                for opportunity_window in windows:
                    opportunity_history = history[-opportunity_window:]
                    projected_opportunity = sum(opp for _, opp in opportunity_history) / len(opportunity_history)
                    for shrinkage in shrinkage_values:
                        actual_total = sum(actual for actual, _ in efficiency_history)
                        opportunity_total = sum(opp for _, opp in efficiency_history)
                        if opportunity_total <= 0:
                            continue
                        if league_rate is not None and shrinkage > 0:
                            efficiency = (actual_total + shrinkage * league_rate) / (opportunity_total + shrinkage)
                        else:
                            efficiency = actual_total / opportunity_total
                        projection = projected_opportunity * efficiency
                        _add_metric(
                            candidate_store,
                            (split, row["market"], opportunity_window, efficiency_window, shrinkage),
                            row["actual"] - projection, row["event_id"],
                        )

        # Same-time events are scored before any same-time outcomes update histories or priors.
        for row in batch:
            histories[(row["player_id"], row["market"])].append(
                (row["actual"], row["opportunity"])
            )
            totals = league_totals[row["market"]]
            totals[0] += row["actual"]
            totals[1] += row["opportunity"]

    candidate_metrics = _render_metrics(
        candidate_store,
        key_fields=("split", "market", "opportunity_window_games", "efficiency_window_games", "shrinkage_strength"),
    )
    baseline_metrics = _render_metrics(
        baseline_store,
        key_fields=("split", "market", "window_games"),
    )

    selections = []
    markets = sorted({row["market"] for row in candidate_metrics})
    for market in markets:
        candidates = [row for row in candidate_metrics
                      if row["split"] == "validation" and row["market"] == market]
        candidates.sort(key=lambda row: (
            row["mae"], row["rmse"], row["opportunity_window_games"],
            row["efficiency_window_games"], row["shrinkage_strength"],
        ))
        if not candidates:
            selections.append({"market": market, "selected": None, "reason": "no_validation_predictions"})
            continue
        best = candidates[0]
        test = next((row for row in candidate_metrics
                     if row["split"] == "test" and row["market"] == market
                     and row["opportunity_window_games"] == best["opportunity_window_games"]
                     and row["efficiency_window_games"] == best["efficiency_window_games"]
                     and row["shrinkage_strength"] == best["shrinkage_strength"]), None)
        baseline_validation = next((row for row in baseline_metrics
                                     if row["split"] == "validation" and row["market"] == market
                                     and row["window_games"] == best["efficiency_window_games"]), None)
        baseline_test = next((row for row in baseline_metrics
                              if row["split"] == "test" and row["market"] == market
                              and row["window_games"] == best["efficiency_window_games"]), None)
        candidate_key = (
            "test", market, best["opportunity_window_games"],
            best["efficiency_window_games"], best["shrinkage_strength"],
        )
        baseline_key = ("test", market, best["efficiency_window_games"])
        if test is None or baseline_test is None:
            paired_test = {
                "available": False,
                "reason": "selected candidate or rolling-mean baseline has no held-out test metrics",
            }
        else:
            paired_test = _paired_event_bootstrap(
                candidate_store[candidate_key], baseline_store[baseline_key],
                seed=20261123 + sum(ord(char) for char in market),
            )
        selections.append({
            "market": market,
            "selected": {
                "opportunity_window_games": best["opportunity_window_games"],
                "efficiency_window_games": best["efficiency_window_games"],
                "shrinkage_strength": best["shrinkage_strength"],
                "validation_mae": best["mae"],
                "validation_rmse": best["rmse"],
            },
            "validation_rolling_mean_baseline": baseline_validation,
            "held_out_test": test,
            "held_out_test_rolling_mean_baseline": baseline_test,
            "test_paired_event_bootstrap": paired_test,
        })

    for market, counts in coverage.items():
        counts["coverage_pct"] = round(
            100.0 * counts["with_opportunity"] / counts["target_rows"], 2
        ) if counts["target_rows"] else None

    warnings = []
    for row in candidate_metrics:
        if row["split"] == "test" and row["n_events"] < 30:
            warnings.append(
                f'{row["market"]} separate-window candidate has only {row["n_events"]} test events; descriptive only.'
            )
    return {
        "ok": True,
        "sport": sport,
        "windows": list(windows),
        "shrinkage_candidates": list(shrinkage_values),
        "minimum_history": min_history,
        "events": len({row["event_id"] for row in target_rows}),
        "split_event_counts": {
            split: len({row["event_id"] for row in target_rows if split_by_event.get(row["event_id"]) == split})
            for split in ("train", "validation", "test")
        },
        "opportunity_coverage": dict(sorted(coverage.items())),
        "exclusions": exclusions,
        "candidate_metrics": candidate_metrics,
        "rolling_mean_metrics": baseline_metrics,
        "validation_selections": selections,
        "evaluation_warnings": warnings,
        "limitations": [
            "Opportunity and efficiency windows are selected jointly on validation only; the held-out test split is not used to select them.",
            "The opportunity forecast uses prior realized minutes, attempts, or targets; no future role, injury, lineup, opponent, or game-script context is modelled.",
            "League efficiency priors use only earlier event-time observations and are not position-specific.",
            "No sportsbook prices, calibrated probability distributions, EV, ROI, or bet qualification are produced.",
            "PRA is a point estimate only; joint residual covariance is not modelled.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sport", choices=("nba", "nfl"))
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--windows", default="5,10,20")
    parser.add_argument("--shrinkage", default="0,5,10,20")
    parser.add_argument("--min-history", type=int, default=3)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        windows = tuple(int(value.strip()) for value in args.windows.split(",") if value.strip())
        shrinkage = tuple(float(value.strip()) for value in args.shrinkage.split(",") if value.strip())
        result = benchmark(
            args.sport, root=args.data_root, windows=windows,
            shrinkage_values=shrinkage, min_history=args.min_history,
        )
    except ValueError as exc:
        parser.error(str(exc))
    rendered = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
