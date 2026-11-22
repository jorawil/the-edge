"""Reproducible backtest from saved pregame inputs.

Every archived prop with a result is counted, not only the ones that looked
good. The output is a simulation. It is not a record of bets that were placed,
and it is not a profit claim.
"""

from __future__ import annotations

import math
from typing import Any

from .calibration import (
    chronological_split,
    score_snapshots,
    wilson_interval,
)
from .core import EDGE_THRESHOLD
from .version import MODEL_VERSION


def _std_error(values: list[float]) -> float | None:
    count = len(values)
    if count < 2:
        return None
    mean = sum(values) / count
    variance = sum((value - mean) ** 2 for value in values) / (count - 1)
    return math.sqrt(variance / count)


def run_backtest(
    snapshots: list[dict[str, Any]],
    actuals: dict[tuple, Any],
    *,
    threshold: float = EDGE_THRESHOLD,
    model_version: str = MODEL_VERSION,
) -> dict[str, Any]:
    """Score archived props in time order.

    ``actuals`` maps (event id, player id or name, market) to a number.
    A missing result must be omitted or marked missing. It is not a zero.
    """
    if not snapshots:
        return {
            "ok": False,
            "reason": "no archived pregame projections",
            "result_type": "not_run",
            "profitability_claim": False,
            "model_version": model_version,
            "sigma_defaults_changed": False,
        }
    versions = sorted({str(row.get("model_version") or row.get("archived_model_version") or "unspecified") for row in snapshots})
    scored = score_snapshots(snapshots, actuals)
    rows = scored["scored"]
    splits = chronological_split(rows) if rows else {"train": [], "validation": [], "test": []}
    decided = [row for row in rows if row.get("outcome") is not None]
    hits = sum(int(row["outcome"]) for row in decided)
    liked = [row for row in decided if row.get("threshold_cleared")]
    liked_hits = sum(int(row["outcome"]) for row in liked)
    values = [row["expected_value_per_unit"] for row in decided if row.get("expected_value_per_unit") is not None]
    liked_values = [row["expected_value_per_unit"] for row in liked if row.get("expected_value_per_unit") is not None]
    error = _std_error(values)
    mean_ev = None if not values else sum(values) / len(values)
    return {
        "ok": True,
        "result_type": "simulated_from_archived_inputs",
        "actual_tracked_bets": "unavailable",
        "profitability_claim": False,
        "note": (
            "This is a simulation from saved inputs. It is not money that was "
            "won or lost, and a short sample can look good or bad by luck."
        ),
        "model_version_requested": model_version,
        "model_versions_in_archive": versions,
        "threshold": threshold,
        "eligible_props": len(rows),
        "missing_actuals": len(scored["missing_actuals"]),
        "excluded_leakage": len(scored["excluded_leakage"]),
        "missing_detail": scored["missing_actuals"],
        "leakage_detail": scored["excluded_leakage"],
        "props_clearing_threshold": len(liked),
        "props_not_clearing_threshold": len(decided) - len(liked),
        "hit_rate_all_decided": None if not decided else hits / len(decided),
        "hit_rate_interval_all_decided": wilson_interval(hits, len(decided)),
        "hit_rate_clearing_threshold": None if not liked else liked_hits / len(liked),
        "hit_rate_interval_clearing_threshold": wilson_interval(liked_hits, len(liked)),
        "mean_expected_value_per_unit_all_decided": mean_ev,
        "expected_value_standard_error": error,
        "expected_value_interval": (
            None
            if mean_ev is None or error is None
            else {"low": mean_ev - 1.96 * error, "high": mean_ev + 1.96 * error}
        ),
        "mean_expected_value_clearing_threshold": (
            None if not liked_values else sum(liked_values) / len(liked_values)
        ),
        "closing_line_value": "unavailable",
        "splits": {name: len(group) for name, group in splits.items()},
        "test_ids": [row.get("snapshot_id") for row in splits["test"]],
        "sigma_defaults_changed": False,
    }
