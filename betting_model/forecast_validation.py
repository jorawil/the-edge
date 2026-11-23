"""Chronological residual interval diagnostics; never promotes a model."""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path
from typing import Any

from .timeutil import parse_aware_datetime

MIN_FIT_ROWS = 30
MIN_EVALUATION_ROWS = 30
NORMAL_Z = {0.80: 1.2815515655446004, 0.90: 1.6448536269514722}


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a quantile from an empty sample")
    position = (len(ordered) - 1) * probability
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    fraction = position - low
    return ordered[low] * (1 - fraction) + ordered[high] * fraction


def load_active_residual_rows(path: Path) -> list[dict[str, Any]]:
    """Load the latest pregame snapshot per event/player/market/model with active final result."""
    if not Path(path).exists():
        return []
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            WITH active_results AS (
                SELECT r.* FROM forecast_results r
                WHERE r.game_status='final' AND r.actual_stat IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM forecast_results newer
                    WHERE newer.supersedes_result_id=r.result_id
                  )
            ),
            ranked AS (
                SELECT s.sport,s.market,s.model_version,s.event_id,s.player_id,
                       s.event_start,s.projected_value,r.actual_stat,
                       ROW_NUMBER() OVER (
                         PARTITION BY s.sport,s.event_id,s.player_id,s.market,s.model_version
                         ORDER BY s.projection_at DESC,s.archived_at DESC
                       ) AS forecast_rank
                FROM forecast_snapshots s
                JOIN active_results r ON r.snapshot_id=s.snapshot_id
            )
            SELECT sport,market,model_version,event_id,player_id,event_start,
                   projected_value,actual_stat
            FROM ranked WHERE forecast_rank=1
            ORDER BY event_start,event_id,player_id,market,model_version
        """).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["residual"] = float(item["actual_stat"]) - float(item["projected_value"])
        output.append(item)
    return output


def _event_split(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Split chronologically by unique kickoff instant, never separating simultaneous events."""
    event_times: dict[str, datetime] = {}
    for row in rows:
        event_id = str(row["event_id"])
        parsed = parse_aware_datetime(row["event_start"])
        if parsed is None:
            raise ValueError("all event_start values must be timezone-aware timestamps")
        instant = parsed
        # Compare true instants, not timezone-formatted strings.
        instant = instant.astimezone(timezone.utc)
        previous = event_times.get(event_id)
        if previous is not None and previous != instant:
            raise ValueError(f"event {event_id} has inconsistent event_start values")
        event_times[event_id] = instant

    events_by_instant: dict[datetime, set[str]] = defaultdict(set)
    for event_id, instant in event_times.items():
        events_by_instant[instant].add(event_id)
    ordered_instants = sorted(events_by_instant)
    train_end = int(len(ordered_instants) * 0.6)
    valid_end = train_end + int(len(ordered_instants) * 0.2)
    buckets = {"train": set(), "validation": set(), "test": set()}
    for index, instant in enumerate(ordered_instants):
        split = "train" if index < train_end else "validation" if index < valid_end else "test"
        buckets[split].update(events_by_instant[instant])
    return {name: [row for row in rows if str(row["event_id"]) in ids]
            for name, ids in buckets.items()}


def _bounds(residuals: list[float], distribution: str, coverage: float) -> tuple[float, float]:
    alpha = 1 - coverage
    if distribution == "empirical":
        return _quantile(residuals, alpha / 2), _quantile(residuals, 1 - alpha / 2)
    mean = sum(residuals) / len(residuals)
    variance = sum((x - mean) ** 2 for x in residuals) / max(1, len(residuals) - 1)
    spread = math.sqrt(variance) * NORMAL_Z[coverage]
    return mean - spread, mean + spread


def _evaluate(fit: list[float], rows: list[dict[str, Any]], distribution: str) -> dict[str, Any]:
    result = {}
    errors = [float(row["residual"]) for row in rows]
    for coverage in (0.80, 0.90):
        lower, upper = _bounds(fit, distribution, coverage)
        alpha = 1 - coverage
        scores = []
        for error in errors:
            score = upper - lower
            if error < lower:
                score += (2 / alpha) * (lower - error)
            elif error > upper:
                score += (2 / alpha) * (error - upper)
            scores.append(score)
        result[str(int(coverage * 100))] = {
            "target_coverage": coverage,
            "observed_coverage": (
                sum(lower <= e <= upper for e in errors) / len(errors) if errors else None
            ),
            "mean_interval_width": upper - lower,
            "mean_interval_score": sum(scores) / len(scores) if scores else None,
            "residual_lower": lower,
            "residual_upper": upper,
            "n": len(errors),
        }
    return result


def validate_residual_distributions(
    path: Path, *, min_fit_rows: int = MIN_FIT_ROWS,
    min_evaluation_rows: int = MIN_EVALUATION_ROWS,
) -> dict[str, Any]:
    """Compare normal and empirical residual intervals using train/validation/test."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in load_active_residual_rows(path):
        grouped[(row["sport"], row["market"], row["model_version"])].append(row)

    reports = []
    for (sport, market, version), group in sorted(grouped.items()):
        parts = _event_split(group)
        train, validation, test = parts["train"], parts["validation"], parts["test"]
        fit = [float(row["residual"]) for row in train]
        n_val_events = len({row["event_id"] for row in validation})
        ready = (len(train) >= min_fit_rows and len(validation) >= min_evaluation_rows
                 and n_val_events >= min_evaluation_rows)
        candidates = {}
        if fit:
            for name in ("normal", "empirical"):
                candidates[name] = {
                    "validation": _evaluate(fit, validation, name),
                    "test": _evaluate(fit, test, name),
                }
        reports.append({
            "sport": sport, "market": market, "model_version": version,
            "n_total": len(group),
            "n_events_total": len({r["event_id"] for r in group}),
            "n_train": len(train),
            "n_train_events": len({r["event_id"] for r in train}),
            "n_validation": len(validation),
            "n_validation_events": n_val_events,
            "n_test": len(test),
            "n_test_events": len({r["event_id"] for r in test}),
            "fit_residual_mean": sum(fit) / len(fit) if fit else None,
            "fit_residual_rmse": math.sqrt(sum(x*x for x in fit) / len(fit)) if fit else None,
            "minimum_sample_gate_passed": ready,
            "candidates": candidates,
            "promotion_status": "NOT_PROMOTED",
            "reason": (
                "sample gate met for interval diagnostics only; probability calibration "
                "and independent review are still required"
                if ready else
                f"need at least {min_fit_rows} fit rows and {min_evaluation_rows} "
                f"validation rows across {min_evaluation_rows} events"
            ),
        })
    return {
        "ok": True, "archive": str(path), "groups": reports,
        "scored_forecasts": sum(r["n_total"] for r in reports),
        "distribution_registry_changed": False, "sigma_defaults_changed": False,
        "qualified_promotions": 0, "probability_calibration_claim": False,
        "method": (
            "60/20/20 chronological event split; residual intervals fit on train only; "
            "validation compares candidates; test is untouched; no automatic promotion"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = validate_residual_distributions(args.archive)
    rendered = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
