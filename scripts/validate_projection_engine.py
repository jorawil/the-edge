"""Chronological validation for opportunity × efficiency projections.

Compares a trailing rolling-mean baseline with the v1 opportunity-rate engine
on identical player-game samples. Shrinkage is selected on validation only;
the held-out test split is reported after selection and never drives it.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
from collections import defaultdict
from itertools import groupby
from pathlib import Path
from typing import Any

from betting_model.features import _market_values
from betting_model.projection_engine import project_nba, project_nfl

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


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [row["actual"] - row["projection"] for row in rows]
    if not errors:
        return {"n": 0, "n_events": 0, "mae": None, "rmse": None, "mean_signed_error": None}
    return {
        "n": len(errors),
        "n_events": len({row["event_id"] for row in rows}),
        "mae": sum(abs(value) for value in errors) / len(errors),
        "rmse": math.sqrt(sum(value * value for value in errors) / len(errors)),
        "mean_signed_error": sum(errors) / len(errors),
    }


def validate(
    sport: str,
    *,
    root: Path,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
    shrinkage_values: tuple[float, ...] = DEFAULT_SHRINKAGE,
    min_history: int = 3,
) -> dict[str, Any]:
    if sport not in _BASELINE.SPORT_MARKETS:
        raise ValueError(f"unsupported sport: {sport}")
    if not windows or any(value < 1 for value in windows):
        raise ValueError("windows must be positive integers")
    if not isinstance(min_history, int) or isinstance(min_history, bool) or min_history < 3:
        raise ValueError("min_history must be at least 3 for the projection engine")
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
        # PRA is a point-estimate market built from the three source components.
        # Its joint uncertainty remains unavailable until residual covariance is validated.
        for event_id, game in games.items():
            if not isinstance(game, dict) or game.get("season_type_id") != 2:
                continue
            event_time = _BASELINE._event_time(game)
            if event_time is None:
                continue
            grouped_players: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for player in game.get("players") or []:
                if isinstance(player, dict) and not player.get("dnp") and player.get("athlete_id") is not None:
                    grouped_players[str(player["athlete_id"])].append(player)
            for player_id, player_rows in grouped_players.items():
                actual, _ = _market_values("nba", "PRA", player_rows)
                if actual is not None:
                    target_rows.append({
                        "event_id": str(event_id), "event_time": event_time,
                        "player_id": player_id,
                        "player": next((item.get("player") for item in player_rows if item.get("player")), None),
                        "market": "PRA", "actual": actual,
                    })
    player_rows_by_event: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event_id, game in games.items():
        if not isinstance(game, dict) or game.get("season_type_id") != 2:
            continue
        for player in game.get("players") or []:
            if isinstance(player, dict) and not player.get("dnp") and player.get("athlete_id") is not None:
                player_rows_by_event[(str(event_id), str(player["athlete_id"]))].append(player)

    observations = []
    coverage: dict[str, dict[str, int]] = defaultdict(lambda: {"target_rows": 0, "with_opportunity": 0})
    for row in target_rows:
        coverage[row["market"]]["target_rows"] += 1
        player_rows = player_rows_by_event.get((row["event_id"], row["player_id"]), [])
        opportunity = (_OPP._minutes((player_rows[0].get("stats") or {}).get("minutes"))
                       if sport == "nba" and row["market"] == "PRA" and player_rows
                       else _OPP._opportunity(sport, row["market"], player_rows))
        if opportunity is None or (opportunity == 0 and row["actual"] > 0):
            continue
        coverage[row["market"]]["with_opportunity"] += 1
        observations.append({**row, "opportunity": opportunity})

    observations.sort(key=lambda row: (row["event_time"], row["event_id"], row["player_id"], row["market"]))
    split_by_event = _BASELINE._split_events(target_rows)
    histories: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    league_totals: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    predictions: list[dict[str, Any]] = []

    for _, grouped in groupby(observations, key=lambda row: row["event_time"]):
        batch = list(grouped)
        for row in batch:
            key = (row["player_id"], row["market"])
            prior_all = histories[key]
            if len(prior_all) < min_history:
                continue
            split = split_by_event[row["event_id"]]
            for window in windows:
                prior = prior_all[-window:]
                baseline = sum(actual for actual, _ in prior) / len(prior)
                market_actual, market_opportunity = league_totals[row["market"]]
                league_rate = (
                    market_actual / market_opportunity if market_opportunity > 0 else None
                )
                history = [{"actual": actual, "opportunity": opp} for actual, opp in prior]
                engine = project_nba if sport == "nba" else project_nfl
                for shrinkage in shrinkage_values:
                    result = engine(
                        row["market"], history,
                        {"prior_efficiency": league_rate},
                        shrinkage_strength=shrinkage,
                    )
                    if not result.get("ok"):
                        continue
                    predictions.append({
                        "event_id": row["event_id"], "split": split,
                        "market": row["market"], "method": "opportunity_rate",
                        "window": window, "shrinkage": shrinkage,
                        "projection": result["projected_value"], "actual": row["actual"],
                    })
                predictions.append({
                    "event_id": row["event_id"], "split": split,
                    "market": row["market"], "method": "rolling_mean",
                    "window": window, "shrinkage": None,
                    "projection": baseline, "actual": row["actual"],
                })
        # No same-time game contributes to another same-time game's features.
        for row in batch:
            histories[(row["player_id"], row["market"])].append(
                (row["actual"], row["opportunity"])
            )
            totals = league_totals[row["market"]]
            totals[0] += row["actual"]
            totals[1] += row["opportunity"]

    grouped_predictions: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for row in predictions:
        grouped_predictions[(row["split"], row["market"], row["method"], row["window"], row["shrinkage"])].append(row)

    metric_rows = []
    for key, rows in sorted(grouped_predictions.items(), key=lambda item: str(item[0])):
        split, market, method, window, shrinkage = key
        metric_rows.append({
            "split": split, "market": market, "method": method,
            "window_games": window, "shrinkage_strength": shrinkage,
            **_metrics(rows),
        })

    selections = []
    for market in sorted({row["market"] for row in metric_rows}):
        candidates = [row for row in metric_rows
                      if row["split"] == "validation" and row["market"] == market
                      and row["mae"] is not None]
        if not candidates:
            selections.append({"market": market, "selected": None, "reason": "no_validation_predictions"})
            continue
        candidates.sort(key=lambda row: (row["mae"], row["rmse"], row["window_games"],
                                         row["shrinkage_strength"] if row["shrinkage_strength"] is not None else -1))
        best = candidates[0]
        selected = {
            "method": best["method"], "window_games": best["window_games"],
            "shrinkage_strength": best["shrinkage_strength"],
            "validation_mae": best["mae"], "validation_rmse": best["rmse"],
        }
        test = next((row for row in metric_rows
                     if row["split"] == "test" and row["market"] == market
                     and row["method"] == best["method"]
                     and row["window_games"] == best["window_games"]
                     and row["shrinkage_strength"] == best["shrinkage_strength"]), None)
        selections.append({"market": market, "selected": selected, "held_out_test": test})

    for market, counts in coverage.items():
        counts["coverage_pct"] = round(
            100 * counts["with_opportunity"] / counts["target_rows"], 2
        ) if counts["target_rows"] else None

    return {
        "ok": True, "sport": sport, "model_version": "opportunity-rate-v1",
        "windows": list(windows), "shrinkage_candidates": list(shrinkage_values),
        "minimum_history": min_history,
        "events": len({row["event_id"] for row in target_rows}),
        "prediction_rows": len(predictions),
        "opportunity_coverage": dict(sorted(coverage.items())),
        "split_event_counts": {
            split: len({row["event_id"] for row in target_rows if split_by_event.get(row["event_id"]) == split})
            for split in ("train", "validation", "test")
        },
        "exclusions": exclusions,
        "metrics": metric_rows,
        "validation_selections": selections,
        "limitations": [
            "Expected opportunity is forecast from prior player-game opportunities; target-game opportunity is used only for scoring.",
            "League efficiency priors use only earlier event-time observations and are not position-specific.",
            "Opponent, rest, home/away, injuries, lineups, role changes, pace, and game script are not adjusted.",
            "No calibrated probability distribution, sportsbook price, edge, EV, ROI, or bet qualification is produced.",
            "PRA is not included; its joint uncertainty requires residual covariance validation.",
            "ESPN game timestamps are available, but source ingestion timestamps are not recorded.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", choices=("nba", "nfl"), required=True)
    parser.add_argument("--data-root", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    parser.add_argument("--windows", default="5,10,20")
    parser.add_argument("--shrinkage", default="0,5,10,20")
    parser.add_argument("--min-history", type=int, default=3)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    windows = tuple(int(value) for value in args.windows.split(",") if value.strip())
    shrinkage = tuple(float(value) for value in args.shrinkage.split(",") if value.strip())
    result = validate(args.sport, root=args.data_root, windows=windows,
                      shrinkage_values=shrinkage, min_history=args.min_history)
    rendered = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
