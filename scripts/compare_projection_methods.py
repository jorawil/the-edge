"""Compare leakage-safe historical projection baselines for NBA and NFL."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
from collections import defaultdict
from itertools import groupby
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).with_name("walk_forward_backtest.py")
SPEC = importlib.util.spec_from_file_location("walk_forward_backtest", SCRIPT)
BASELINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASELINE)


def run_comparison(sport: str, *, root: Path | None = None,
                   windows: tuple[int, ...] = (5, 10, 20),
                   min_history: int = 3) -> dict[str, Any]:
    if sport not in BASELINE.SPORT_MARKETS:
        raise ValueError(f"unsupported sport: {sport}")
    if not windows or any(not isinstance(w, int) or w < 1 for w in windows):
        raise ValueError("windows must contain positive integers")
    if min_history < 1:
        raise ValueError("min_history must be positive")
    data_root = Path(root) if root is not None else BASELINE.DATA_DIR
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

    rows, exclusions = BASELINE._game_rows(sport, games)
    for row in rows:
        game = games[row["event_id"]]
        year = game.get("season_year")
        try:
            row["season_year"] = int(year) if year is not None else row["event_time"].year
        except (TypeError, ValueError):
            row["season_year"] = row["event_time"].year
    rows.sort(key=lambda r: (r["event_time"], r["event_id"], r["player_id"], r["market"]))
    split_by_event = BASELINE._split_events(rows)

    histories: dict[tuple[str, str], list[float]] = defaultdict(list)
    season_histories: dict[tuple[str, str, int], list[float]] = defaultdict(list)
    predictions: list[dict[str, Any]] = []

    for _, grouped in groupby(rows, key=lambda r: r["event_time"]):
        batch = list(grouped)
        for row in batch:
            key = (row["player_id"], row["market"])
            history = histories[key]
            season = season_histories[(row["player_id"], row["market"], row["season_year"])]
            common = {
                "event_id": row["event_id"],
                "split": split_by_event[row["event_id"]],
                "player_id": row["player_id"],
                "market": row["market"],
                "actual": row["actual"],
            }
            if len(history) >= min_history:
                for window in windows:
                    prior = history[-window:]
                    value = sum(prior) / len(prior)
                    predictions.append({**common, "method": "rolling_mean",
                                        "window": window, "projection": value})
                prior = history[-10:]
                if len(prior) >= min_history:
                    weights = range(1, len(prior) + 1)
                    value = sum(x * w for x, w in zip(prior, weights)) / sum(weights)
                    predictions.append({**common, "method": "recency_weighted_mean",
                                        "window": len(prior), "projection": value})
            if len(season) >= min_history:
                predictions.append({**common, "method": "season_to_date_mean",
                                    "window": None, "projection": sum(season) / len(season)})
        for row in batch:
            histories[(row["player_id"], row["market"])].append(row["actual"])
            season_histories[(row["player_id"], row["market"], row["season_year"])].append(row["actual"])

    groups: dict[tuple[str, str, str, int | None], list[float]] = defaultdict(list)
    events: dict[tuple[str, str, str, int | None], set[str]] = defaultdict(set)
    for row in predictions:
        key = (row["split"], row["market"], row["method"], row["window"])
        groups[key].append(row["actual"] - row["projection"])
        events[key].add(row["event_id"])

    metrics = []
    for key in sorted(groups, key=lambda k: (k[0], k[1], k[2], -1 if k[3] is None else k[3])):
        split, market, method, window = key
        errors = groups[key]
        metrics.append({
            "split": split, "market": market, "method": method, "window_games": window,
            "n_player_game_predictions": len(errors), "n_events": len(events[key]),
            "mae": sum(abs(e) for e in errors) / len(errors),
            "rmse": math.sqrt(sum(e * e for e in errors) / len(errors)),
            "mean_signed_error": sum(errors) / len(errors),
        })

    validation = [m for m in metrics if m["split"] == "validation"]
    test = [m for m in metrics if m["split"] == "test"]
    warnings = []
    if not validation:
        warnings.append("No validation forecasts; do not select a projection method.")
    if not test:
        warnings.append("No test forecasts; out-of-sample performance cannot be reviewed.")
    for metric in test:
        if metric["n_events"] < 30:
            warnings.append(
                f'{metric["market"]} {metric["method"]}: test sample has '
                f'{metric["n_events"]} events; descriptive only.'
            )

    # This is a validation-only ranking aid. Test results are never used to rank methods.
    rankings = []
    for market in sorted({m["market"] for m in validation}):
        candidates = [m for m in validation if m["market"] == market]
        candidates.sort(key=lambda m: (m["mae"], m["rmse"]))
        for rank, metric in enumerate(candidates, 1):
            rankings.append({"market": market, "rank": rank, "method": metric["method"],
                             "window_games": metric["window_games"], "validation_mae": metric["mae"],
                             "validation_rmse": metric["rmse"]})

    return {
        "ok": True, "sport": sport, "methods": ["rolling_mean", "season_to_date_mean", "recency_weighted_mean"],
        "windows": list(windows), "minimum_history": min_history,
        "events_in_regular_season_data": len({r["event_id"] for r in rows}),
        "player_market_actual_rows": len(rows), "prediction_rows": len(predictions),
        "split_event_counts": {s: len({r["event_id"] for r in rows if split_by_event.get(r["event_id"]) == s})
                               for s in ("train", "validation", "test")},
        "exclusions": exclusions, "metrics": metrics, "validation_rankings": rankings,
        "evaluation_warnings": warnings,
        "limitations": [
            "All forecasts use only appearances strictly before the forecast event; same-time events are scored before any same-time actual is appended.",
            "Season-to-date history resets by ESPN season_year; calendar year is used only when season_year is absent.",
            "Recency weighting is a simple linear weighting of up to the last ten appearances, not a calibrated distribution.",
            "Validation rankings are descriptive method comparisons, not proof of betting edge or profitability.",
            "The held-out test split is reported but never used to rank or select a method.",
            "No injuries, minutes, role, opponent, rest, lineup context, sportsbook odds, EV, ROI, or calibration are modeled.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare historical projection baselines without look-ahead.")
    parser.add_argument("sport", choices=sorted(BASELINE.SPORT_MARKETS))
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--windows", default="5,10,20")
    parser.add_argument("--min-history", type=int, default=3)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        windows = tuple(int(v.strip()) for v in args.windows.split(",") if v.strip())
        result = run_comparison(args.sport, root=args.data_root, windows=windows,
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
