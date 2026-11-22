"""Sigma and probability checks from archived pregame projections.

Forecast error is actual stat minus the projection that was saved before
the event. The spread of a player's raw game log is a different number and
is not used here.

This module never replaces SIGMA_DEFAULTS. A residual standard deviation is
reported only as a description, and only when the sample is large enough.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .core import SIGMA_DEFAULTS, calculate_edge
from .timeutil import parse_aware_datetime
from .version import MODEL_VERSION

MIN_MARKET_SAMPLE = 30
MIN_PLAYER_SAMPLE = 100
REQUIRED_SNAPSHOT_FIELDS = (
    "snapshot_id",
    "player",
    "sport",
    "market",
    "projected_value",
    "generated_at",
    "event_start",
    "source",
)


def _aware(value: object, label: str):
    parsed = parse_aware_datetime(value)
    if parsed is None:
        raise ValueError(f"{label} must be a timezone-aware ISO timestamp")
    return parsed


def load_archive(path: Path) -> dict[str, Any]:
    file = Path(path)
    if not file.exists():
        return {
            "ok": False,
            "reason": "no archived pregame projections",
            "path": str(file),
            "snapshots": [],
        }
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "reason": f"archive is not valid JSON ({exc})",
            "path": str(file),
            "snapshots": [],
        }
    snapshots = data.get("snapshots") if isinstance(data, dict) else None
    if not isinstance(snapshots, list):
        return {
            "ok": False,
            "reason": "archive has no snapshots list and was not modified",
            "path": str(file),
            "snapshots": [],
        }
    return {"ok": True, "path": str(file), "snapshots": snapshots, "raw": data}


def archive_projection(snapshot: dict[str, Any], path: Path) -> dict[str, Any]:
    """Append one pregame snapshot. Existing snapshot ids are never edited."""
    missing = [field for field in REQUIRED_SNAPSHOT_FIELDS if snapshot.get(field) in (None, "")]
    if missing:
        return {"ok": False, "reason": "missing required fields", "fields": missing}
    try:
        generated = _aware(snapshot.get("generated_at"), "generated_at")
        start = _aware(snapshot.get("event_start"), "event_start")
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    if generated >= start:
        return {
            "ok": False,
            "reason": "projection was not made before the event, so it was not archived",
        }
    file = Path(path)
    existing = load_archive(file)
    if file.exists() and not existing["ok"]:
        return {"ok": False, "reason": existing["reason"], "path": str(file)}
    snapshots = list(existing["snapshots"])
    if any(row.get("snapshot_id") == snapshot["snapshot_id"] for row in snapshots):
        return {
            "ok": False,
            "reason": "snapshot_id already exists and was not changed",
            "snapshot_id": snapshot["snapshot_id"],
        }
    stored = dict(snapshot)
    stored["archived_model_version"] = MODEL_VERSION
    stored["sealed"] = True
    snapshots.append(stored)
    payload = {"model_version": MODEL_VERSION, "snapshots": snapshots}
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {"ok": True, "snapshot_id": snapshot["snapshot_id"], "path": str(file), "count": len(snapshots)}


def leakage_reasons(snapshot: dict[str, Any]) -> list[str]:
    """Reasons a snapshot must not be used for scoring."""
    reasons = []
    try:
        generated = _aware(snapshot.get("generated_at"), "generated_at")
        start = _aware(snapshot.get("event_start"), "event_start")
    except ValueError as exc:
        return [str(exc)]
    if generated >= start:
        reasons.append("projection timestamp is not before the event")
    for raw in snapshot.get("feature_times") or []:
        feature_time = parse_aware_datetime(raw)
        if feature_time is None or feature_time >= start:
            reasons.append("a feature timestamp is not before the event")
            break
    return reasons


def chronological_split(
    rows: list[dict[str, Any]],
    *,
    train_fraction: float = 0.6,
    validation_fraction: float = 0.2,
) -> dict[str, list[dict[str, Any]]]:
    """Split by event time. Later games are never placed in an earlier slice."""
    if train_fraction <= 0 or validation_fraction < 0 or train_fraction + validation_fraction >= 1:
        raise ValueError("fractions must leave a non-empty test slice at the end")

    def sort_key(row: dict[str, Any]) -> str:
        return str(row.get("event_start") or row.get("generated_at") or "")

    ordered = sorted(rows, key=sort_key)
    count = len(ordered)
    train_end = int(count * train_fraction)
    valid_end = train_end + int(count * validation_fraction)
    return {
        "train": ordered[:train_end],
        "validation": ordered[train_end:valid_end],
        "test": ordered[valid_end:],
    }


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def mae(errors: list[float]) -> float | None:
    if not errors:
        return None
    return sum(abs(value) for value in errors) / len(errors)


def rmse(errors: list[float]) -> float | None:
    if not errors:
        return None
    return math.sqrt(sum(value * value for value in errors) / len(errors))


def brier_score(probabilities: list[float], outcomes: list[int]) -> float | None:
    if not probabilities or len(probabilities) != len(outcomes):
        return None
    total = 0.0
    for probability, outcome in zip(probabilities, outcomes):
        total += (probability - outcome) ** 2
    return total / len(probabilities)


def log_loss(probabilities: list[float], outcomes: list[int]) -> float | None:
    if not probabilities or len(probabilities) != len(outcomes):
        return None
    total = 0.0
    for probability, outcome in zip(probabilities, outcomes):
        clipped = min(max(probability, 1e-6), 1 - 1e-6)
        total += -(outcome * math.log(clipped) + (1 - outcome) * math.log(1 - clipped))
    return total / len(probabilities)


def reliability_table(
    probabilities: list[float],
    outcomes: list[int],
    *,
    bins: int = 10,
) -> list[dict[str, Any]]:
    table = []
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        chosen = [
            (probability, outcome)
            for probability, outcome in zip(probabilities, outcomes)
            if (lower <= probability < upper) or (index == bins - 1 and probability == 1)
        ]
        hits = sum(outcome for _, outcome in chosen)
        table.append(
            {
                "lower": lower,
                "upper": upper,
                "count": len(chosen),
                "mean_predicted": None if not chosen else round(sum(p for p, _ in chosen) / len(chosen), 6),
                "observed_rate": None if not chosen else round(hits / len(chosen), 6),
            }
        )
    return table


def wilson_interval(successes: int, count: int, *, z: float = 1.96) -> dict[str, float] | None:
    """A 95% Wilson interval for a hit rate. Empty samples return None."""
    if count <= 0:
        return None
    proportion = successes / count
    denominator = 1 + z * z / count
    center = (proportion + z * z / (2 * count)) / denominator
    margin = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * count)) / count) / denominator
    return {"low": max(0.0, center - margin), "high": min(1.0, center + margin)}


def _lookup_actual(
    snapshot: dict[str, Any],
    actuals: dict[tuple, Any],
) -> dict[str, Any]:
    keys = []
    if snapshot.get("event_id") and snapshot.get("player_id"):
        keys.append((str(snapshot["event_id"]), str(snapshot["player_id"]), str(snapshot.get("market"))))
    if snapshot.get("event_id") and snapshot.get("player"):
        keys.append((str(snapshot["event_id"]), str(snapshot["player"]).casefold(), str(snapshot.get("market"))))
    for key in keys:
        if key in actuals:
            value = actuals[key]
            if isinstance(value, dict) and value.get("missing"):
                return {"ok": False, "reason": value.get("reason") or "stat_missing"}
            if value is None:
                return {"ok": False, "reason": "stat_missing"}
            return {"ok": True, "actual": float(value["actual"] if isinstance(value, dict) else value)}
    return {"ok": False, "reason": "no actual result stored for this projection"}


def score_snapshots(
    snapshots: list[dict[str, Any]],
    actuals: dict[tuple, Any],
) -> dict[str, Any]:
    """Score every archived projection that has a real result.

    Props the model would not have liked are included. Missing results are
    counted and are not filled with zero.
    """
    rows = []
    excluded = []
    missing = []
    for snapshot in snapshots:
        leaks = leakage_reasons(snapshot)
        if leaks:
            excluded.append({"snapshot_id": snapshot.get("snapshot_id"), "reasons": leaks})
            continue
        found = _lookup_actual(snapshot, actuals)
        if not found["ok"]:
            missing.append({"snapshot_id": snapshot.get("snapshot_id"), "reason": found["reason"]})
            continue
        actual = float(found["actual"])
        projected = float(snapshot["projected_value"])
        error = actual - projected
        row = {
            "snapshot_id": snapshot.get("snapshot_id"),
            "sport": snapshot.get("sport"),
            "market": snapshot.get("market"),
            "player": snapshot.get("player"),
            "player_id": snapshot.get("player_id"),
            "event_start": snapshot.get("event_start"),
            "projected": projected,
            "actual": actual,
            "error": error,
        }
        if snapshot.get("recent_average_at_archive") is not None:
            row["recent_average_error"] = actual - float(snapshot["recent_average_at_archive"])
        if snapshot.get("season_average_at_archive") is not None:
            row["season_average_error"] = actual - float(snapshot["season_average_at_archive"])
        if snapshot.get("line") is not None and snapshot.get("odds") is not None and "is_over" in snapshot:
            try:
                edge = calculate_edge(
                    projected,
                    snapshot["line"],
                    snapshot["odds"],
                    snapshot["is_over"],
                    market=str(snapshot.get("market") or ""),
                    sigma=snapshot.get("sigma"),
                )
            except ValueError as exc:
                row["edge_error"] = str(exc)
            else:
                win_probability = edge.get("model_probability")
                if win_probability is None:
                    win_probability = edge.get("model_prob")
                won = _won(actual, float(snapshot["line"]), bool(snapshot["is_over"]))
                row["model_probability"] = win_probability
                row["outcome"] = None if won is None else int(won)
                row["push"] = won is None
                row["positive_edge"] = edge.get("positive_edge")
                row["threshold_cleared"] = edge.get("threshold_cleared")
                row["expected_value_per_unit"] = edge.get("expected_value_per_unit")
        rows.append(row)
    return {"scored": rows, "excluded_leakage": excluded, "missing_actuals": missing}


def _won(actual: float, line: float, is_over: bool) -> bool | None:
    if abs(actual - line) <= 1e-9:
        return None
    over_hit = actual > line
    return over_hit if is_over else not over_hit


def _group_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [row["error"] for row in rows]
    probabilities = [row["model_probability"] for row in rows if row.get("outcome") is not None and row.get("model_probability") is not None]
    outcomes = [row["outcome"] for row in rows if row.get("outcome") is not None and row.get("model_probability") is not None]
    recent = [row["recent_average_error"] for row in rows if "recent_average_error" in row]
    season = [row["season_average_error"] for row in rows if "season_average_error" in row]
    return {
        "sample_size": len(rows),
        "mae": mae(errors),
        "rmse": rmse(errors),
        "mean_error": _mean(errors),
        "brier_score": brier_score(probabilities, outcomes),
        "log_loss": log_loss(probabilities, outcomes),
        "reliability": reliability_table(probabilities, outcomes) if probabilities else [],
        "probability_sample_size": len(probabilities),
        "recent_average_baseline_mae": mae(recent) if recent else None,
        "season_average_baseline_mae": mae(season) if season else None,
        "recent_average_baseline": "stored on the snapshot" if recent else "unavailable",
        "season_average_baseline": "stored on the snapshot" if season else "unavailable",
        "closing_line_baseline": "unavailable",
    }


def calibrate(
    snapshots: list[dict[str, Any]],
    actuals: dict[tuple, Any],
) -> dict[str, Any]:
    """Describe residual error. Does not change sigma defaults."""
    if not snapshots:
        return {
            "ok": False,
            "reason": "no archived pregame projections",
            "sigma_defaults_changed": False,
            "applied_sigma": None,
            "sigma_defaults": dict(SIGMA_DEFAULTS),
        }
    scored = score_snapshots(snapshots, actuals)
    rows = scored["scored"]
    by_market: dict[tuple, list[dict[str, Any]]] = {}
    by_player: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        by_market.setdefault((row.get("sport"), row.get("market")), []).append(row)
        by_player.setdefault((row.get("sport"), row.get("market"), row.get("player_id") or row.get("player")), []).append(row)
    markets = []
    for key, group in sorted(by_market.items()):
        summary = _group_metrics(group)
        residual_std = None
        if summary["sample_size"] >= MIN_MARKET_SAMPLE and summary["rmse"] is not None:
            # RMSE of the error is a descriptive spread, not a new default.
            residual_std = summary["rmse"]
        markets.append(
            {
                "sport": key[0],
                "market": key[1],
                **summary,
                "residual_std": residual_std,
                "residual_std_applied": False,
                "residual_std_note": (
                    "sample is large enough to describe, and the number was still not applied"
                    if residual_std is not None
                    else f"fewer than {MIN_MARKET_SAMPLE} pregame projections with results"
                ),
            }
        )
    players = []
    for key, group in sorted(by_player.items(), key=lambda item: str(item[0])):
        if len(group) >= MIN_PLAYER_SAMPLE:
            note = "player sample meets the minimum, and no player sigma was applied"
        else:
            note = (
                f"player sample {len(group)} is below {MIN_PLAYER_SAMPLE}; "
                "no player sigma was fit, and nothing was shrunk into the defaults"
            )
        players.append(
            {
                "sport": key[0],
                "market": key[1],
                "player": key[2],
                "sample_size": len(group),
                "applied_sigma": None,
                "note": note,
            }
        )
    return {
        "ok": True,
        "model_version": MODEL_VERSION,
        "sigma_defaults_changed": False,
        "applied_sigma": None,
        "reason_not_applied": (
            "Sigma defaults stay in place. This report does not promote a residual "
            "spread into the model, even when the sample is large."
        ),
        "scored": len(rows),
        "missing_actuals": len(scored["missing_actuals"]),
        "excluded_leakage": len(scored["excluded_leakage"]),
        "missing_detail": scored["missing_actuals"],
        "leakage_detail": scored["excluded_leakage"],
        "overall": _group_metrics(rows),
        "markets": markets,
        "players": players,
        "closing_line_baseline": "unavailable",
        "profitability_claim": False,
    }
