"""Read-only calibration and projection report for the SQLite pregame archive.

The JSON helpers in ``calibration`` stay the scorer for that older snapshot
file. This module does not archive a second copy and does not recompute a
probability from the eventual stat. It reads the probability that was stored
on the snapshot and the settlement that was attached later.

Conditional win probability is win / (win + loss). Pushes are left out of
that score. Unconditional win, loss, and push probabilities are scored on
their own, and expected value stays with those unconditional probabilities.
Projection error is a third report. Markets and settlement types are not
pooled into one calibration group.

Events are ordered by kickoff and kept whole, so a later game cannot land in
an earlier slice. Nothing in this module is fit on the train slice, and a
large sample does not mark the model calibrated or a row Qualified.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import timezone
from pathlib import Path
from typing import Any

from .archive import connect, schema_version
from .calibration import (
    MIN_MARKET_SAMPLE,
    brier_score,
    chronological_split,
    log_loss,
    mae,
    reliability_table,
    rmse,
)
from .core import EDGE_THRESHOLD
from .distributions import INTEGER_COUNT_MARKETS, line_kind
from .timeutil import parse_aware_datetime
from .version import MODEL_VERSION

# The same descriptive bar the JSON calibration report already uses.
MIN_GROUP_SAMPLE = MIN_MARKET_SAMPLE
MIN_BIN_SAMPLE = MIN_MARKET_SAMPLE
# A claim would need at least this many distinct held-out events in one
# sport, market, and settlement family. This report never makes that claim.
MIN_CALIBRATION_CLAIM_EVENTS = 300
_PROBABILITY_SUM_TOLERANCE = 1e-4

REASON_NO_RESULT = "no result has been attached"
REASON_PUSH = "push; left out of conditional win probability"
REASON_VOID = (
    "void; a did-not-play, postponement, or cancellation is not a win, a loss, or a zero stat"
)
REASON_UNKNOWN = "unknown settlement"
REASON_MISSING_STAT = "actual stat is missing and was not treated as zero"
REASON_UNKNOWN_NO_STAT = "unknown settlement; no played stat was stored"
REASON_UNSUPPORTED = (
    "push rules are not defined for this market and line, so the probability was not scored"
)
REASON_UNRECOGNIZED = "settlement type is not recognized, so it was not grouped"
REASON_UNRECOGNIZED_SETTLEMENT = "settlement value is not Win, Loss, Push, Void, or Unknown"
REASON_NO_CONDITIONAL = "conditional win probability was not stored"
REASON_CONDITIONAL_RANGE = "conditional win probability is outside 0 to 1"
REASON_NO_UNCONDITIONAL = "unconditional win, loss, and push probabilities were not stored"
REASON_BAD_UNCONDITIONAL = "unconditional probabilities are outside 0 to 1 or do not sum to 1"
REASON_FAMILY_CONFLICT = (
    "stored push handling does not match the market and line, so the probability was not scored"
)
REASON_NOT_STAT = "the archived number is a win probability, not a stat projection"
REASON_NO_PROJECTION = "projected value is missing"
REASON_LEAK_PROJECTION = "projection timestamp is not before the event"
REASON_LEAK_PRICE = "price timestamp is not before the event"
REASON_LEAK_ARCHIVED = "archive timestamp is not before the event"
REASON_LEAK_RESULT = "result timestamp is before the event"
REASON_LEAK_INPUT = "a projection input timestamp is not before the event"
REASON_BAD_EVENT = "event start is missing or not a timestamp"
REASON_BAD_PROJECTION_TIME = "projection timestamp is missing or not a timestamp"
REASON_BAD_PRICE_TIME = "price timestamp is missing or not a timestamp"
REASON_BAD_ARCHIVE_TIME = "archive timestamp is missing or not a timestamp"
REASON_BAD_RESULT_TIME = "result timestamp is missing or not a timestamp"
REASON_BAD_INPUT_TIME = "projection input timestamp is not a timestamp"
REASON_EVENT_CONFLICT = "event start disagrees across snapshots for one event"
REASON_CONFLICTING_ACTUALS = "conflicting actuals for one projection"
REASON_RESULT_HISTORY = "the result history has no current result, so it was not scored"

CONDITIONAL_DEFINITION = (
    "Conditional win probability, win / (win + loss), stored before the event. "
    "Pushes, voids, and unknown results are not in this score. "
    "The probability is not refit after the result."
)
UNCONDITIONAL_DEFINITION = (
    "Unconditional win, loss, and push probabilities stored before the event. "
    "Pushes stay in this score. Voids and unknown results stay out. "
    "The Brier score and log loss score all three outcomes. "
    "The probability ranges compare stored win probability with how often the side won; "
    "a push counts as not a win in that table only."
)
PROJECTION_DEFINITION = (
    "Actual stat minus the projection stored before the event. "
    "Positive mean signed error means the projection was low. "
    "This is not a probability score. Recent averages are not used, "
    "and the same projection is not counted once per sportsbook."
)
EV_DEFINITION = (
    "Mean of expected_value_per_unit stored before the event. "
    "That number uses unconditional win, loss, and push probabilities, and a push is worth zero. "
    "This is not profit, ROI, or a record of a bet that was placed."
)
SPLIT_RULE = (
    "Events are ordered by kickoff. Every snapshot from one event stays in the same slice. "
    "Later events are never placed in an earlier slice. "
    "No outcome is used to change a stored probability, and the test slice is not used to score earlier slices. "
    "Nothing is fit on the train slice."
)
CLAIM_REASON = (
    "This report does not claim that the model is calibrated. "
    f"A defensible claim would need genuine out-of-sample results for one sport, "
    f"market, and settlement type, at least {MIN_CALIBRATION_CLAIM_EVENTS} distinct events "
    "in the held-out test slice, and a decision outside this tool. "
    "Sample size alone does not qualify a pick, and this tool does not change the Qualified gate."
)
EVALUATION_MEANS = (
    "The sample is large enough to read. It is not a statement that the probabilities are calibrated."
)
PROFIT_REASON = (
    "Profit and ROI are not reported. The archive does not record a stake or a bet that was placed. "
    "Expected value stored on a snapshot is the model's number before the event, not a result."
)

_HANDLING_FAMILY = {
    "no_push_half_point": "half_point",
    "continuity_correction": "whole_number",
    "binary_no_push": "binary_no_push",
    "unsupported": "unsupported",
}
_SCOREABLE_FAMILIES = frozenset({"half_point", "whole_number", "binary_no_push"})
_SETTLED = frozenset({"Win", "Loss", "Push", "Void"})
_SLICE_ORDER = ("test", "validation", "train", "all_rows_not_held_out")
_SLICE_COPY = {
    "test": (
        "Out-of-sample test slice",
        True,
        "Latest events. This is the held-out read. These results are not used to score earlier slices, "
        "and no probability is adjusted.",
    ),
    "validation": (
        "Validation slice",
        False,
        "Events after the train slice and before the test slice. Nothing is fit here. "
        "A short archive can leave this slice empty.",
    ),
    "train": (
        "Train slice",
        False,
        "Earlier events. Later results are not included. Nothing is fit on this slice.",
    ),
    "all_rows_not_held_out": (
        "All events in the split (not a held-out test)",
        False,
        "Every event that entered the split, including the test events. "
        "Do not read this section as out-of-sample performance.",
    ),
}
LIMITATIONS = [
    PROFIT_REASON,
    "Stored probabilities are not refit, and later results are not used to change earlier scores.",
    "Conditional scores omit pushes, voids, unknown results, and rows without a stored conditional win probability.",
    "Unconditional scores keep pushes and omit voids and unknown results.",
    "Over and Under, and the same projection at two books, are not independent samples. Event counts sit beside row counts.",
    "Projection error uses the stored projection and the stored actual only. Recent averages are not read.",
    "Duplicate prices of one projection count once in the projection sample and are listed as collapsed rows.",
    "An unknown result with no stat is excluded. An unknown result that still has a stored stat stays in the projection sample only.",
    "A group below the minimum sample is shown with a warning. It is not evidence that the model is calibrated.",
    "Qualified is unchanged. This report does not lower the 3-point edge threshold and does not place a bet.",
    "Corrections are counted and are not applied. Superseded results are counted and are not scored.",
    "Opening an existing archive applies the same pending schema migration as the other archive commands. Snapshot rows are not rewritten. A missing file is not created.",
]


def _unique(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _r6(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def select_current_result(results: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, int, int]:
    """Return the current result, how many older results were set aside, and extras.

    This follows ``archive._current_result``. A result named by
    ``supersedes_result_id`` is history. Among the rest, the latest
    ``attached_at`` and then ``result_id`` is current. When every row is
    history, nothing is guessed.
    """
    if not results:
        return None, 0, 0
    superseded_ids = {
        row["supersedes_result_id"] for row in results if row.get("supersedes_result_id")
    }
    history = [row for row in results if row.get("result_id") in superseded_ids]
    current = [row for row in results if row.get("result_id") not in superseded_ids]
    if not current:
        return None, len(history), 0
    chosen = max(current, key=lambda row: (row.get("attached_at") or "", row.get("result_id") or ""))
    return chosen, len(history), len(current) - 1


def multiclass_brier(
    probabilities: list[tuple[float, float, float]],
    outcomes: list[str],
) -> float | None:
    """Mean squared error over win, loss, and push. Empty input returns None."""
    if not probabilities or len(probabilities) != len(outcomes):
        return None
    if any(outcome not in {"win", "loss", "push"} for outcome in outcomes):
        return None
    names = ("win", "loss", "push")
    total = 0.0
    for probs, outcome in zip(probabilities, outcomes):
        if len(probs) != 3:
            return None
        for index, name in enumerate(names):
            target = 1.0 if name == outcome else 0.0
            total += (float(probs[index]) - target) ** 2
    return total / len(probabilities)


def multiclass_log_loss(
    probabilities: list[tuple[float, float, float]],
    outcomes: list[str],
) -> float | None:
    """Mean log loss of the outcome that happened. Probabilities are clipped at 1e-6."""
    if not probabilities or len(probabilities) != len(outcomes):
        return None
    if any(outcome not in {"win", "loss", "push"} for outcome in outcomes):
        return None
    index_of = {"win": 0, "loss": 1, "push": 2}
    total = 0.0
    for probs, outcome in zip(probabilities, outcomes):
        if len(probs) != 3:
            return None
        probability = min(max(float(probs[index_of[outcome]]), 1e-6), 1 - 1e-6)
        total += -math.log(probability)
    return total / len(probabilities)


def describe_reliability(
    probabilities: list[float],
    outcomes: list[int],
    *,
    bins: int = 10,
    min_bin_sample: int = MIN_BIN_SAMPLE,
) -> list[dict[str, Any]]:
    """Ten probability ranges, each with its sample size and a small-sample warning."""
    table = reliability_table(probabilities, outcomes, bins=bins)
    described = []
    for row in table:
        item = dict(row)
        count = int(item["count"])
        item["sample_size"] = count
        if count == 0:
            item["warning"] = "no observations in this probability range"
            item["enough_to_evaluate"] = False
        elif count < min_bin_sample:
            item["warning"] = (
                f"sample size {count} is below {min_bin_sample}; "
                "this probability range is too small to judge"
            )
            item["enough_to_evaluate"] = False
        else:
            item["warning"] = None
            item["enough_to_evaluate"] = True
        if item["mean_predicted"] is not None and item["observed_rate"] is not None:
            item["observed_minus_predicted"] = round(
                float(item["observed_rate"]) - float(item["mean_predicted"]), 6
            )
        else:
            item["observed_minus_predicted"] = None
        described.append(item)
    return described


def _push_handling(snapshot: dict[str, Any]) -> str | None:
    raw = snapshot.get("payload_json")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("push_handling")
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _derived_family(record: dict[str, Any]) -> str:
    market = record.get("canonical_market") or ""
    line = record.get("line")
    if line is None:
        return "unrecognized"
    try:
        kind = line_kind(float(line))
    except (TypeError, ValueError):
        return "unrecognized"
    if market in INTEGER_COUNT_MARKETS and kind == "half":
        return "half_point"
    if market in INTEGER_COUNT_MARKETS and kind == "whole":
        return "whole_number"
    return "unsupported"


def _resolve_family(record: dict[str, Any]) -> tuple[str, str, bool]:
    handling = record.get("push_handling")
    derived = _derived_family(record)
    if handling is None:
        return derived, "market_and_line", False
    if handling not in _HANDLING_FAMILY:
        return "unrecognized", "stored_push_handling", False
    family = _HANDLING_FAMILY[handling]
    if family == "binary_no_push":
        # A supplied win probability is its own model. It is not grouped with
        # the normal curve. On a whole-number line that can push, the stored
        # model assumed no push, so the row is left out instead of mixed.
        conflict = derived == "whole_number"
    else:
        conflict = family != derived
    return family, "stored_push_handling", conflict


def _unconditional_parts(
    record: dict[str, Any],
) -> tuple[tuple[float, float, float] | None, str | None]:
    p_over = record.get("p_over")
    p_under = record.get("p_under")
    if p_over is None or p_under is None:
        return None, "missing"
    if not (0.0 <= float(p_over) <= 1.0 and 0.0 <= float(p_under) <= 1.0):
        return None, "range"
    raw_push = 1.0 - float(p_over) - float(p_under)
    if raw_push < -_PROBABILITY_SUM_TOLERANCE or raw_push > 1.0 + _PROBABILITY_SUM_TOLERANCE:
        return None, "range"
    p_push = min(1.0, max(0.0, raw_push))
    side = record.get("side")
    if side == "Over":
        return (float(p_over), float(p_under), p_push), None
    if side == "Under":
        return (float(p_under), float(p_over), p_push), None
    return None, "missing"


def _leakage_reasons(record: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    start = parse_aware_datetime(record.get("event_start"))
    projected = parse_aware_datetime(record.get("projection_created_at"))
    odds = parse_aware_datetime(record.get("odds_retrieved_at"))
    archived = parse_aware_datetime(record.get("archived_at"))
    if start is None:
        reasons.append(REASON_BAD_EVENT)
    if projected is None:
        reasons.append(REASON_BAD_PROJECTION_TIME)
    if odds is None:
        reasons.append(REASON_BAD_PRICE_TIME)
    if archived is None:
        reasons.append(REASON_BAD_ARCHIVE_TIME)
    if start is not None and projected is not None and projected >= start:
        reasons.append(REASON_LEAK_PROJECTION)
    if start is not None and odds is not None and odds >= start:
        reasons.append(REASON_LEAK_PRICE)
    if start is not None and archived is not None and archived >= start:
        reasons.append(REASON_LEAK_ARCHIVED)
    raw_input = record.get("projection_input_at")
    if raw_input not in (None, ""):
        parsed_input = parse_aware_datetime(raw_input)
        if parsed_input is None:
            reasons.append(REASON_BAD_INPUT_TIME)
        elif start is not None and parsed_input >= start:
            reasons.append(REASON_LEAK_INPUT)
    retrieved_raw = record.get("result_retrieved_at")
    if record.get("settlement") is not None or retrieved_raw not in (None, ""):
        retrieved = parse_aware_datetime(retrieved_raw) if isinstance(retrieved_raw, str) else None
        if retrieved is None:
            reasons.append(REASON_BAD_RESULT_TIME)
        elif start is not None and retrieved < start:
            reasons.append(REASON_LEAK_RESULT)
    return _unique(reasons)


def _apply_outcome(record: dict[str, Any]) -> None:
    settlement = record.get("settlement")
    if settlement == "Win":
        record["conditional_outcome"] = 1
        record["outcome_class"] = "win"
    elif settlement == "Loss":
        record["conditional_outcome"] = 0
        record["outcome_class"] = "loss"
    elif settlement == "Push":
        record["conditional_outcome"] = None
        record["outcome_class"] = "push"
    else:
        record["conditional_outcome"] = None
        record["outcome_class"] = None


def _apply_probability_reasons(record: dict[str, Any]) -> None:
    if record.get("result_problem"):
        record["conditional_reasons"].append(record["result_problem"])
        record["unconditional_reasons"].append(record["result_problem"])
        return
    settlement = record.get("settlement")
    if settlement is None:
        record["conditional_reasons"].append(REASON_NO_RESULT)
        record["unconditional_reasons"].append(REASON_NO_RESULT)
        return
    if settlement == "Void":
        record["conditional_reasons"].append(REASON_VOID)
        record["unconditional_reasons"].append(REASON_VOID)
        return
    if settlement == "Unknown":
        record["conditional_reasons"].append(REASON_UNKNOWN)
        record["unconditional_reasons"].append(REASON_UNKNOWN)
        return
    if settlement not in {"Win", "Loss", "Push"}:
        record["conditional_reasons"].append(REASON_UNRECOGNIZED_SETTLEMENT)
        record["unconditional_reasons"].append(REASON_UNRECOGNIZED_SETTLEMENT)
        return
    family = record["line_family"]
    if record["family_conflict"]:
        record["conditional_reasons"].append(REASON_FAMILY_CONFLICT)
        record["unconditional_reasons"].append(REASON_FAMILY_CONFLICT)
    elif family == "unrecognized":
        record["conditional_reasons"].append(REASON_UNRECOGNIZED)
        record["unconditional_reasons"].append(REASON_UNRECOGNIZED)
    elif family not in _SCOREABLE_FAMILIES:
        record["conditional_reasons"].append(REASON_UNSUPPORTED)
        record["unconditional_reasons"].append(REASON_UNSUPPORTED)
    scoreable = family in _SCOREABLE_FAMILIES and not record["family_conflict"]
    if settlement == "Push":
        record["conditional_reasons"].append(REASON_PUSH)
    elif scoreable:
        probability = record.get("conditional_win_probability")
        if probability is None:
            record["conditional_reasons"].append(REASON_NO_CONDITIONAL)
        elif probability < 0.0 or probability > 1.0:
            record["conditional_reasons"].append(REASON_CONDITIONAL_RANGE)
    if scoreable:
        if record.get("p_win") is None:
            if record.get("unconditional_problem") == "missing":
                record["unconditional_reasons"].append(REASON_NO_UNCONDITIONAL)
            else:
                record["unconditional_reasons"].append(REASON_BAD_UNCONDITIONAL)


def _apply_projection_reasons(record: dict[str, Any]) -> None:
    if record.get("result_problem"):
        record["projection_reasons"].append(record["result_problem"])
        return
    if record["line_family"] == "binary_no_push" or record.get("push_handling") == "binary_no_push":
        record["projection_reasons"].append(REASON_NOT_STAT)
        return
    settlement = record.get("settlement")
    if settlement is None:
        record["projection_reasons"].append(REASON_NO_RESULT)
        return
    if settlement == "Void":
        record["projection_reasons"].append(REASON_VOID)
        return
    if settlement == "Unknown" and record.get("actual") is None:
        reason = record.get("settlement_reason") or ""
        if "missing" in str(reason).lower():
            record["projection_reasons"].append(REASON_MISSING_STAT)
        else:
            record["projection_reasons"].append(REASON_UNKNOWN_NO_STAT)
        return
    if settlement not in {"Win", "Loss", "Push", "Unknown"}:
        record["projection_reasons"].append(REASON_UNRECOGNIZED_SETTLEMENT)
        return
    if record.get("actual") is None:
        record["projection_reasons"].append(REASON_MISSING_STAT)
        return
    if record.get("projected_value") is None:
        record["projection_reasons"].append(REASON_NO_PROJECTION)


def _classify(record: dict[str, Any]) -> dict[str, Any]:
    family, source, conflict = _resolve_family(record)
    record["line_family"] = family
    record["line_family_source"] = source
    record["family_conflict"] = conflict
    record["event_conflict"] = False
    probs, problem = _unconditional_parts(record)
    record["unconditional_problem"] = problem
    if probs is None:
        record["p_win"] = None
        record["p_loss"] = None
        record["p_push"] = None
    else:
        record["p_win"], record["p_loss"], record["p_push"] = probs
    record["leakage_reasons"] = _leakage_reasons(record)
    record["conditional_reasons"] = list(record["leakage_reasons"])
    record["unconditional_reasons"] = list(record["leakage_reasons"])
    record["projection_reasons"] = list(record["leakage_reasons"])
    _apply_outcome(record)
    _apply_probability_reasons(record)
    _apply_projection_reasons(record)
    for key in ("conditional_reasons", "unconditional_reasons", "projection_reasons"):
        record[key] = _unique(record[key])
    return record


def _normalize(
    snapshot: dict[str, Any],
    current: dict[str, Any] | None,
    result_problem: str | None,
) -> dict[str, Any]:
    actual = None
    settlement = None
    settlement_reason = None
    retrieved = None
    if current is not None:
        settlement = current.get("settlement")
        settlement_reason = current.get("settlement_reason")
        retrieved = current.get("result_retrieved_at")
        flagged_missing = bool(int(current.get("actual_missing") or 0))
        if not flagged_missing:
            actual = _number(current.get("actual_stat"))
    projected = _number(snapshot.get("projected_value"))
    record = {
        "archive_id": snapshot["archive_id"],
        "sport": snapshot.get("sport"),
        "event_id": str(snapshot.get("event_id")),
        "event_start": snapshot.get("event_start"),
        "player_id": snapshot.get("player_id"),
        "player_name": snapshot.get("player_name"),
        "canonical_market": snapshot.get("canonical_market"),
        "market": snapshot.get("market"),
        "side": snapshot.get("side"),
        "line": _number(snapshot.get("line")),
        "sportsbook": snapshot.get("sportsbook") or "",
        "sportsbook_key": (snapshot.get("sportsbook") or "").casefold(),
        "projected_value": projected,
        "projection_created_at": snapshot.get("projection_created_at"),
        "projection_input_at": snapshot.get("projection_input_at"),
        "odds_retrieved_at": snapshot.get("odds_retrieved_at"),
        "archived_at": snapshot.get("archived_at"),
        "conditional_win_probability": _number(snapshot.get("conditional_win_probability")),
        "p_over": _number(snapshot.get("p_over")),
        "p_under": _number(snapshot.get("p_under")),
        "expected_value_per_unit": _number(snapshot.get("expected_value_per_unit")),
        "qualified": snapshot.get("qualified") == 1 or snapshot.get("qualified") is True,
        "push_handling": _push_handling(snapshot),
        "settlement": settlement,
        "settlement_reason": settlement_reason,
        "actual": actual,
        "result_retrieved_at": retrieved,
        "result_problem": result_problem,
        "projection_error": None
        if actual is None or projected is None
        else actual - projected,
    }
    return _classify(record)


def _mark_event_conflicts(records: list[dict[str, Any]]) -> None:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record["leakage_reasons"]:
            continue
        groups.setdefault(record["event_id"], []).append(record)
    for group in groups.values():
        instants = set()
        for record in group:
            parsed = parse_aware_datetime(record.get("event_start"))
            instants.add(None if parsed is None else parsed.astimezone(timezone.utc))
        if len(instants) > 1:
            for record in group:
                record["event_conflict"] = True
                for key in ("conditional_reasons", "unconditional_reasons", "projection_reasons"):
                    record[key].append(REASON_EVENT_CONFLICT)
                    record[key] = _unique(record[key])


def _player_key(row: dict[str, Any]) -> tuple[str, str]:
    if row.get("player_id"):
        return ("id", str(row["player_id"]))
    name = (row.get("player_name") or "").strip().casefold()
    if name:
        return ("name", name)
    return ("archive", str(row["archive_id"]))


def _projection_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("sport"),
        row.get("event_id"),
        _player_key(row),
        row.get("canonical_market"),
        row.get("projection_created_at"),
        round(float(row["projected_value"]), 6),
    )


def _dedupe_projections(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(_projection_key(row), []).append(row)
    kept = []
    conflicts = []
    for items in buckets.values():
        actuals = {round(float(item["actual"]), 6) for item in items}
        if len(actuals) != 1:
            conflicts.append(items)
            continue
        ordered = sorted(items, key=lambda item: item["archive_id"])
        primary = dict(ordered[0])
        primary["collapsed_archive_ids"] = [item["archive_id"] for item in ordered[1:]]
        kept.append(primary)
    return kept, conflicts


def _mark_projection_conflicts(records: list[dict[str, Any]]) -> None:
    candidates = [row for row in records if not row["projection_reasons"]]
    _kept, conflicts = _dedupe_projections(candidates)
    for items in conflicts:
        for item in items:
            item["projection_reasons"].append(REASON_CONFLICTING_ACTUALS)
            item["projection_reasons"] = _unique(item["projection_reasons"])


def _set_usable(records: list[dict[str, Any]]) -> None:
    for record in records:
        record["usable_conditional"] = not record["conditional_reasons"]
        record["usable_unconditional"] = not record["unconditional_reasons"]
        record["usable_projection"] = not record["projection_reasons"]


def _ensure_chronological(ordered: dict[str, list[str]], starts: dict[str, Any]) -> None:
    previous = None
    seen: set[str] = set()
    for name in ("train", "validation", "test"):
        for event_id in ordered[name]:
            if event_id in seen:
                raise RuntimeError("an event was placed in two chronological slices")
            seen.add(event_id)
            moment = starts[event_id]
            if previous is not None and moment < previous:
                raise RuntimeError("chronological split placed a later event in an earlier slice")
            previous = moment


def _event_split(
    records: list[dict[str, Any]],
    *,
    train_fraction: float,
    validation_fraction: float,
) -> dict[str, list[str]]:
    by_event: dict[str, dict[str, Any]] = {}
    for record in records:
        if record["leakage_reasons"] or record.get("event_conflict"):
            continue
        by_event.setdefault(record["event_id"], record)
    reps = []
    for event_id in sorted(by_event):
        row = by_event[event_id]
        reps.append(
            {
                "snapshot_id": event_id,
                "event_id": event_id,
                "event_start": row["event_start"],
            }
        )
    parts = chronological_split(
        reps,
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
    )
    ordered = {name: [item["event_id"] for item in parts[name]] for name in ("train", "validation", "test")}
    starts = {}
    for event_id, row in by_event.items():
        parsed = parse_aware_datetime(row.get("event_start"))
        if parsed is None:
            raise RuntimeError(f"event {event_id} entered the split without a kickoff")
        starts[event_id] = parsed.astimezone(timezone.utc)
    _ensure_chronological(ordered, starts)
    index = {event_id: name for name, event_ids in ordered.items() for event_id in event_ids}
    for record in records:
        if record["leakage_reasons"] or record.get("event_conflict"):
            record["slice"] = None
        else:
            record["slice"] = index.get(record["event_id"])
    return ordered


def _sample_warnings(
    sample: int,
    events: int,
    minimum: int,
    ranges: list[dict[str, Any]] | None,
    min_bin: int,
) -> list[str]:
    warnings = []
    if sample < minimum or events < minimum:
        warnings.append(
            f"sample size {sample} across {events} events is below {minimum}; "
            "this group is shown and is too small to evaluate"
        )
    if events and sample != events:
        warnings.append(
            f"{sample} rows come from {events} events, so these rows are not independent samples"
        )
    if ranges is not None:
        small = [row for row in ranges if row["sample_size"] and not row["enough_to_evaluate"]]
        if small:
            warnings.append(f"{len(small)} probability ranges are below {min_bin} observations")
    return warnings


def _probability_group(
    rows: list[dict[str, Any]],
    *,
    mode: str,
    min_group: int,
    min_bin: int,
    include_books: bool,
) -> dict[str, Any]:
    rows = sorted(rows, key=lambda row: row["archive_id"])
    first = rows[0]
    events = sorted({row["event_id"] for row in rows})
    if mode == "conditional":
        if any(row["conditional_outcome"] not in (0, 1) for row in rows):
            raise RuntimeError("conditional group included a row without a win or a loss")
        probabilities = [float(row["conditional_win_probability"]) for row in rows]
        outcomes = [int(row["conditional_outcome"]) for row in rows]
        score = brier_score(probabilities, outcomes)
        loss = log_loss(probabilities, outcomes)
        ranges = describe_reliability(probabilities, outcomes, min_bin_sample=min_bin)
        wins = sum(outcomes)
        outcome_counts = {"win": wins, "loss": len(outcomes) - wins, "push": 0}
        mean_predicted = _mean(probabilities)
        observed = _mean([float(item) for item in outcomes])
        definition = CONDITIONAL_DEFINITION
        score_type = "binary_conditional"
        extra: dict[str, Any] = {}
    elif mode == "unconditional":
        if any(row["outcome_class"] not in {"win", "loss", "push"} for row in rows):
            raise RuntimeError("unconditional group included a row without a win, loss, or push")
        if any(row["p_win"] is None for row in rows):
            raise RuntimeError("unconditional group included a row without probabilities")
        triples = [(float(row["p_win"]), float(row["p_loss"]), float(row["p_push"])) for row in rows]
        classes = [row["outcome_class"] for row in rows]
        win_outcomes = [1 if name == "win" else 0 for name in classes]
        score = multiclass_brier(triples, classes)
        loss = multiclass_log_loss(triples, classes)
        ranges = describe_reliability(
            [item[0] for item in triples],
            win_outcomes,
            min_bin_sample=min_bin,
        )
        outcome_counts = {"win": 0, "loss": 0, "push": 0}
        for name in classes:
            outcome_counts[name] += 1
        mean_predicted = _mean([item[0] for item in triples])
        observed = _mean([float(item) for item in win_outcomes])
        definition = UNCONDITIONAL_DEFINITION
        score_type = "multiclass_unconditional"
        count = len(rows)
        extra = {
            "mean_predicted_win": _r6(_mean([item[0] for item in triples])),
            "mean_predicted_loss": _r6(_mean([item[1] for item in triples])),
            "mean_predicted_push": _r6(_mean([item[2] for item in triples])),
            "observed_win_rate": _r6(outcome_counts["win"] / count),
            "observed_loss_rate": _r6(outcome_counts["loss"] / count),
            "observed_push_rate": _r6(outcome_counts["push"] / count),
        }
    else:
        raise ValueError(f"unknown probability mode {mode}")
    group = {
        "sport": first["sport"],
        "market": first["canonical_market"],
        "source_markets": sorted({str(row["market"]) for row in rows}),
        "line_family": first["line_family"],
        "push_handling": sorted({row["push_handling"] or "not_stored" for row in rows}),
        "score_type": score_type,
        "probability_definition": definition,
        "sample_size": len(rows),
        "event_count": len(events),
        "event_ids": events,
        "enough_to_evaluate": len(rows) >= min_group and len(events) >= min_group,
        "evaluation_means": EVALUATION_MEANS,
        "calibration_claimed": False,
        "probabilities_recomputed": False,
        "warnings": _sample_warnings(len(rows), len(events), min_group, ranges, min_bin),
        "mean_predicted": _r6(mean_predicted),
        "observed_rate": _r6(observed),
        "observed_minus_predicted": None
        if mean_predicted is None or observed is None
        else _r6(observed - mean_predicted),
        "brier_score": score,
        "log_loss": loss,
        "outcome_counts": outcome_counts,
        "probability_ranges": ranges,
        "archive_ids": [row["archive_id"] for row in rows],
    }
    group.update(extra)
    if include_books:
        by_book: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_book.setdefault(row["sportsbook_key"], []).append(row)
        books = []
        for key in sorted(by_book):
            book_rows = sorted(by_book[key], key=lambda row: row["archive_id"])
            subgroup = _probability_group(
                book_rows,
                mode=mode,
                min_group=min_group,
                min_bin=min_bin,
                include_books=False,
            )
            subgroup["sportsbook"] = book_rows[0]["sportsbook"]
            books.append(subgroup)
        group["sportsbooks"] = books
    return group


def _projection_group(rows: list[dict[str, Any]], *, min_group: int) -> dict[str, Any]:
    rows = sorted(rows, key=lambda row: row["archive_id"])
    first = rows[0]
    errors = [float(row["projection_error"]) for row in rows]
    events = sorted({row["event_id"] for row in rows})
    collapsed: list[str] = []
    for row in rows:
        collapsed.extend(row.get("collapsed_archive_ids") or [])
    sample = len(rows)
    return {
        "sport": first["sport"],
        "market": first["canonical_market"],
        "source_markets": sorted({str(row["market"]) for row in rows}),
        "definition": PROJECTION_DEFINITION,
        "sample_size": sample,
        "source_rows": sample + len(collapsed),
        "collapsed_duplicate_rows": len(collapsed),
        "collapsed_archive_ids": sorted(collapsed),
        "event_count": len(events),
        "event_ids": events,
        "enough_to_evaluate": sample >= min_group and len(events) >= min_group,
        "evaluation_means": EVALUATION_MEANS,
        "calibration_claimed": False,
        "separate_from_probability_calibration": True,
        "warnings": _sample_warnings(sample, len(events), min_group, None, 0),
        "mae": mae(errors),
        "rmse": rmse(errors),
        "mean_signed_error": _r6(_mean(errors)),
        "bias_definition": "actual minus projected. Positive means the projection was low.",
        "archive_ids": [row["archive_id"] for row in rows],
    }


def _group_by(rows: list[dict[str, Any]], keyfn: Any, builder: Any) -> list[dict[str, Any]]:
    buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(keyfn(row), []).append(row)
    groups = []
    for key in sorted(buckets, key=lambda item: tuple(str(part) for part in item)):
        groups.append(builder(buckets[key]))
    return groups


def _prob_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (row.get("sport"), row.get("canonical_market"), row.get("line_family"))


def _slice_report(
    name: str,
    rows: list[dict[str, Any]],
    event_ids: list[str],
    *,
    min_group: int,
    min_bin: int,
) -> dict[str, Any]:
    label, held_out, note = _SLICE_COPY[name]
    conditional_rows = [row for row in rows if row["usable_conditional"]]
    unconditional_rows = [row for row in rows if row["usable_unconditional"]]
    projection_source = [row for row in rows if row["usable_projection"]]
    kept, conflicts = _dedupe_projections(projection_source)
    if conflicts:
        raise RuntimeError("conflicting projections reached a scored slice")
    return {
        "name": name,
        "label": label,
        "note": note,
        "held_out": held_out,
        "event_ids": list(event_ids),
        "event_count": len(event_ids),
        "conditional_groups": _group_by(
            conditional_rows,
            _prob_key,
            lambda items: _probability_group(
                items,
                mode="conditional",
                min_group=min_group,
                min_bin=min_bin,
                include_books=True,
            ),
        ),
        "unconditional_groups": _group_by(
            unconditional_rows,
            _prob_key,
            lambda items: _probability_group(
                items,
                mode="unconditional",
                min_group=min_group,
                min_bin=min_bin,
                include_books=True,
            ),
        ),
        "projection_groups": _group_by(
            kept,
            lambda row: (row.get("sport"), row.get("canonical_market")),
            lambda items: _projection_group(items, min_group=min_group),
        ),
    }


def _ev_group(rows: list[dict[str, Any]], min_group: int) -> dict[str, Any]:
    rows = sorted(rows, key=lambda row: row["archive_id"])
    first = rows[0]
    values = [float(row["expected_value_per_unit"]) for row in rows]
    events = sorted({row["event_id"] for row in rows})
    unsettled = any(row.get("settlement") not in {"Win", "Loss", "Push"} for row in rows)
    return {
        "sport": first["sport"],
        "market": first["canonical_market"],
        "line_family": first["line_family"],
        "sample_size": len(rows),
        "event_count": len(events),
        "mean_archived_expected_value_per_unit": _r6(_mean(values)),
        "includes_unsettled": unsettled,
        "enough_to_evaluate": len(rows) >= min_group and len(events) >= min_group,
        "warnings": _sample_warnings(len(rows), len(events), min_group, None, 0),
        "not_profit": True,
        "profit_reported": False,
        "definition": EV_DEFINITION,
        "archive_ids": [row["archive_id"] for row in rows],
    }


def _expected_value(records: list[dict[str, Any]], min_group: int) -> dict[str, Any]:
    usable = []
    without = 0
    not_grouped = 0
    with_value = 0
    for record in records:
        if record.get("expected_value_per_unit") is None:
            without += 1
            continue
        with_value += 1
        blocked = record["leakage_reasons"] or record.get("event_conflict") or record.get("family_conflict")
        if blocked:
            not_grouped += 1
            continue
        usable.append(record)
    return {
        "definition": EV_DEFINITION,
        "profit_reported": False,
        "roi_reported": False,
        "groups": _group_by(usable, _prob_key, lambda items: _ev_group(items, min_group)),
        "rows_with_expected_value": with_value,
        "rows_grouped": len(usable),
        "rows_without_expected_value": without,
        "rows_not_grouped": not_grouped,
    }


def _tally(records: list[dict[str, Any]], field: str) -> dict[str, Any]:
    buckets: dict[str, list[str]] = {}
    ids = []
    for record in records:
        reasons = record[field]
        if not reasons:
            continue
        ids.append(record["archive_id"])
        for reason in reasons:
            buckets.setdefault(reason, []).append(record["archive_id"])
    return {
        "rows": len(ids),
        "reasons": [
            {"reason": reason, "count": len(bucket), "archive_ids": sorted(bucket)}
            for reason, bucket in sorted(buckets.items())
        ],
    }


def _enough(groups: list[dict[str, Any]], *, projection: bool) -> list[dict[str, Any]]:
    found = []
    for group in groups:
        if not group["enough_to_evaluate"]:
            continue
        item = {
            "sport": group["sport"],
            "market": group["market"],
            "sample_size": group["sample_size"],
            "event_count": group["event_count"],
        }
        if not projection:
            item["line_family"] = group["line_family"]
        found.append(item)
    return found


def _coverage(records: list[dict[str, Any]]) -> dict[str, Any]:
    settlement_counts = {"Win": 0, "Loss": 0, "Push": 0, "Void": 0, "Unknown": 0, "none": 0, "other": 0}
    for record in records:
        settlement = record.get("settlement")
        if settlement is None:
            settlement_counts["none"] += 1
        elif settlement in settlement_counts:
            settlement_counts[settlement] += 1
        else:
            settlement_counts["other"] += 1
    projection_rows = [row for row in records if row["usable_projection"]]
    kept, conflicts = _dedupe_projections(projection_rows)
    if conflicts:
        raise RuntimeError("conflicting actuals were left in the usable projection set")
    conditional = _tally(records, "conditional_reasons")
    unconditional = _tally(records, "unconditional_reasons")
    projection = _tally(records, "projection_reasons")
    total = len(records)
    for name, block, flag in (
        ("conditional", conditional, "usable_conditional"),
        ("unconditional", unconditional, "usable_unconditional"),
        ("projection", projection, "usable_projection"),
    ):
        usable = sum(1 for row in records if row[flag])
        if usable + block["rows"] != total:
            raise RuntimeError(f"{name} exclusion counts do not add up to the archive")
    return {
        "predictions_archived": total,
        "with_a_result": sum(1 for row in records if row.get("settlement") is not None),
        "without_a_result": settlement_counts["none"],
        "settled": sum(settlement_counts[name] for name in ("Win", "Loss", "Push", "Void")),
        "settled_definition": (
            "A current result of Win, Loss, Push, or Void. Unknown is not settled. "
            "A missing result is not settled."
        ),
        "settlement_counts": settlement_counts,
        "usable_conditional_calibration": sum(1 for row in records if row["usable_conditional"]),
        "usable_unconditional_outcomes": sum(1 for row in records if row["usable_unconditional"]),
        "usable_projection_rows": len(projection_rows),
        "distinct_projections_scored": len(kept),
        "collapsed_duplicate_projection_rows": len(projection_rows) - len(kept),
        "conditional_exclusions": conditional,
        "unconditional_exclusions": unconditional,
        "projection_exclusions": projection,
    }


def _questions(coverage: dict[str, Any], slices: dict[str, Any]) -> dict[str, Any]:
    test = slices["test"]
    overall = slices["all_rows_not_held_out"]
    return {
        "predictions_archived": coverage["predictions_archived"],
        "with_a_result": coverage["with_a_result"],
        "settled": coverage["settled"],
        "usable_for_conditional_calibration": coverage["usable_conditional_calibration"],
        "usable_for_unconditional_outcomes": coverage["usable_unconditional_outcomes"],
        "usable_for_projection_accuracy": coverage["distinct_projections_scored"],
        "projection_rows_before_dedupe": coverage["usable_projection_rows"],
        "collapsed_duplicate_projection_rows": coverage["collapsed_duplicate_projection_rows"],
        "excluded_from_conditional": coverage["conditional_exclusions"]["rows"],
        "excluded_from_unconditional": coverage["unconditional_exclusions"]["rows"],
        "excluded_from_projection": coverage["projection_exclusions"]["rows"],
        "markets_with_enough_out_of_sample_conditional_data": _enough(test["conditional_groups"], projection=False),
        "markets_with_enough_out_of_sample_projection_data": _enough(test["projection_groups"], projection=True),
        "markets_with_enough_descriptive_conditional_data": _enough(
            overall["conditional_groups"], projection=False
        ),
        "markets_with_enough_descriptive_projection_data": _enough(
            overall["projection_groups"], projection=True
        ),
    }


def _build(
    records: list[dict[str, Any]],
    meta: dict[str, Any],
    path: Path,
    *,
    train_fraction: float,
    validation_fraction: float,
    min_group: int,
    min_bin: int,
) -> dict[str, Any]:
    _mark_event_conflicts(records)
    _mark_projection_conflicts(records)
    _set_usable(records)
    ordered = _event_split(
        records,
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
    )
    by_slice = {
        name: [row for row in records if row.get("slice") == name]
        for name in ("train", "validation", "test")
    }
    all_ids = ordered["train"] + ordered["validation"] + ordered["test"]
    all_rows = [row for row in records if row.get("slice") is not None]
    slices = {
        "test": _slice_report("test", by_slice["test"], ordered["test"], min_group=min_group, min_bin=min_bin),
        "validation": _slice_report(
            "validation",
            by_slice["validation"],
            ordered["validation"],
            min_group=min_group,
            min_bin=min_bin,
        ),
        "train": _slice_report("train", by_slice["train"], ordered["train"], min_group=min_group, min_bin=min_bin),
        "all_rows_not_held_out": _slice_report(
            "all_rows_not_held_out",
            all_rows,
            all_ids,
            min_group=min_group,
            min_bin=min_bin,
        ),
    }
    coverage = _coverage(records)
    return {
        "ok": True,
        "archive_exists": meta["exists"],
        "archive_path": str(path),
        "schema_version": meta["schema_version"],
        "model_version": MODEL_VERSION,
        "calibration_claimed": False,
        "calibration_claim_reason": CLAIM_REASON,
        "qualified_snapshots": sum(1 for row in records if row["qualified"]),
        "qualified_gate_changed": False,
        "sigma_defaults_changed": False,
        "edge_threshold": EDGE_THRESHOLD,
        "edge_threshold_changed": False,
        "automatic_betting": False,
        "profit_reported": False,
        "roi_reported": False,
        "profit_reason": PROFIT_REASON,
        "corrections_not_applied": meta["corrections"],
        "superseded_results_not_scored": meta["superseded"],
        "additional_unsuperseded_results_not_scored": meta["extra_current"],
        "orphan_results": meta["orphan_results"],
        "minimum_sample": {
            "per_group": min_group,
            "per_event": min_group,
            "per_probability_range": min_bin,
            "claim_events": MIN_CALIBRATION_CLAIM_EVENTS,
            "note": (
                "Meeting the group minimum makes a table large enough to read. "
                "It does not claim the model is calibrated, and it does not change Qualified."
            ),
        },
        "coverage": coverage,
        "questions": _questions(coverage, slices),
        "chronological_split": {
            "unit": "event",
            "train_fraction": train_fraction,
            "validation_fraction": validation_fraction,
            "train_event_ids": ordered["train"],
            "validation_event_ids": ordered["validation"],
            "test_event_ids": ordered["test"],
            "rule": SPLIT_RULE,
        },
        "slices": slices,
        "archived_expected_value": _expected_value(records, min_group),
        "limitations": list(LIMITATIONS),
    }


def _empty_meta() -> dict[str, Any]:
    return {
        "exists": False,
        "schema_version": None,
        "corrections": 0,
        "superseded": 0,
        "extra_current": 0,
        "orphan_results": 0,
    }


def _load(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    conn = connect(path)
    try:
        snapshots = [
            _row_dict(row)
            for row in conn.execute(
                "SELECT * FROM snapshots ORDER BY event_start, archived_at, archive_id"
            )
        ]
        results = [
            _row_dict(row)
            for row in conn.execute("SELECT * FROM results ORDER BY attached_at, result_id")
        ]
        corrections = conn.execute("SELECT COUNT(*) AS n FROM corrections").fetchone()["n"]
        version = schema_version(conn)
    finally:
        conn.close()
    by_archive: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        by_archive.setdefault(result["archive_id"], []).append(result)
    known = {row["archive_id"] for row in snapshots}
    records = []
    superseded = 0
    extra = 0
    for snapshot in snapshots:
        attached = by_archive.get(snapshot["archive_id"], [])
        current, history, extra_here = select_current_result(attached)
        superseded += history
        extra += extra_here
        problem = REASON_RESULT_HISTORY if attached and current is None else None
        records.append(_normalize(snapshot, current, problem))
    meta = {
        "exists": True,
        "schema_version": version,
        "corrections": corrections,
        "superseded": superseded,
        "extra_current": extra,
        "orphan_results": sum(1 for row in results if row["archive_id"] not in known),
    }
    return records, meta


def _failed(path: Path, reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "archive_path": str(path),
        "archive_exists": path.exists(),
        "calibration_claimed": False,
        "qualified_gate_changed": False,
        "sigma_defaults_changed": False,
        "edge_threshold_changed": False,
        "automatic_betting": False,
        "profit_reported": False,
        "roi_reported": False,
    }


def analyze_archive(
    path: Path,
    *,
    train_fraction: float = 0.6,
    validation_fraction: float = 0.2,
    min_group_sample: int | None = None,
    min_bin_sample: int | None = None,
) -> dict[str, Any]:
    """Score one SQLite archive. A missing file is an empty report and is not created.

    ``min_group_sample`` only changes the warning label. It does not claim the
    model is calibrated and it does not change Qualified. The command-line
    report uses the default minimum.
    """
    path = Path(path)
    minimum_group = MIN_GROUP_SAMPLE if min_group_sample is None else min_group_sample
    minimum_bin = MIN_BIN_SAMPLE if min_bin_sample is None else min_bin_sample
    try:
        if minimum_group < 1 or minimum_bin < 1:
            raise ValueError("minimum sample sizes must be at least 1")
        if not path.exists():
            records, meta = [], _empty_meta()
        elif path.is_dir():
            return _failed(path, "archive path is a directory")
        else:
            records, meta = _load(path)
        return _build(
            records,
            meta,
            path,
            train_fraction=train_fraction,
            validation_fraction=validation_fraction,
            min_group=minimum_group,
            min_bin=minimum_bin,
        )
    except ValueError as exc:
        return _failed(path, str(exc))
    except (sqlite3.Error, OSError) as exc:
        return _failed(path, f"could not read the archive ({exc})")


def _fmt(value: object, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _reason_lines(block: dict[str, Any]) -> list[str]:
    if not block["reasons"]:
        return ["_None._", ""]
    lines = ["| Reason | Rows |", "|---|---:|"]
    for item in block["reasons"]:
        lines.append(f"| {item['reason']} | {item['count']} |")
    lines.append("")
    lines.append("Archive ids for each reason are in the JSON report.")
    lines.append("")
    return lines


def _range_lines(ranges: list[dict[str, Any]]) -> list[str]:
    shown = [row for row in ranges if row["sample_size"]]
    if not shown:
        return ["- No observations in any probability range.", ""]
    lines = []
    for row in shown:
        warning = row["warning"] or "This range meets the sample minimum."
        lines.append(
            f"- {row['lower']:.1f} to {row['upper']:.1f}: sample size {row['sample_size']}, "
            f"predicted {_fmt(row['mean_predicted'])}, observed {_fmt(row['observed_rate'])}. {warning}"
        )
    lines.append("")
    lines.append("Ranges with no observations are omitted here. The JSON lists all ten.")
    lines.append("")
    return lines


def _metric_lines(groups: list[dict[str, Any]]) -> list[str]:
    if not groups:
        return ["_No scored rows in this slice._", ""]
    lines: list[str] = []
    for group in groups:
        family = str(group["line_family"]).replace("_", " ")
        lines.append(
            f"**{group['sport']} {group['market']}** ({family}): "
            f"sample size {group['sample_size']}, events {group['event_count']}, "
            f"predicted {_fmt(group['mean_predicted'])}, observed {_fmt(group['observed_rate'])}, "
            f"Brier {_fmt(group['brier_score'])}, log loss {_fmt(group['log_loss'])}."
        )
        if group["enough_to_evaluate"]:
            lines.append("Large enough to read. This is not a claim that the model is calibrated.")
        for warning in group["warnings"]:
            lines.append(f"Warning: {warning}")
        counts = group["outcome_counts"]
        lines.append(
            f"Outcomes: win {counts['win']}, loss {counts['loss']}, push {counts['push']}."
        )
        lines.extend(_range_lines(group["probability_ranges"]))
        for book in group.get("sportsbooks") or []:
            lines.append(
                f"- {book['sportsbook']}: sample size {book['sample_size']}, "
                f"events {book['event_count']}, Brier {_fmt(book['brier_score'])}, "
                f"log loss {_fmt(book['log_loss'])}."
            )
            for warning in book["warnings"]:
                lines.append(f"  Warning: {warning}")
        if group.get("sportsbooks"):
            lines.append("")
    return lines


def _projection_lines(groups: list[dict[str, Any]]) -> list[str]:
    if not groups:
        return ["_No scored projections in this slice._", ""]
    lines = [
        "| Sport | Market | Sample size | Events | MAE | RMSE | Bias | Enough to read |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    notes = []
    for group in groups:
        lines.append(
            f"| {group['sport']} | {group['market']} | {group['sample_size']} | {group['event_count']} | "
            f"{_fmt(group['mae'])} | {_fmt(group['rmse'])} | {_fmt(group['mean_signed_error'])} | "
            f"{'yes' if group['enough_to_evaluate'] else 'no'} |"
        )
        for warning in group["warnings"]:
            notes.append(f"Warning ({group['sport']} {group['market']}): {warning}")
        if group["collapsed_duplicate_rows"]:
            notes.append(
                f"{group['sport']} {group['market']}: {group['collapsed_duplicate_rows']} "
                "duplicate price rows were counted once."
            )
    lines.append("")
    lines.extend(notes)
    if notes:
        lines.append("")
    return lines


def _market_lines(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return ["**none**."]
    lines = []
    for item in items:
        family = item.get("line_family")
        suffix = f", {str(family).replace('_', ' ')}" if family else ""
        lines.append(
            f"- {item['sport']} {item['market']}{suffix}: "
            f"sample size {item['sample_size']}, events {item['event_count']}"
        )
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    """Plain-language report. Sample sizes and the limits are in the text."""
    if not report.get("ok"):
        reason = report.get("reason") or "the archive could not be read"
        return "\n".join(
            [
                "# Calibration and performance",
                "",
                "The archive could not be read.",
                "",
                str(reason),
                "",
                "This report does not claim that the model is calibrated.",
                "",
            ]
        )
    questions = report["questions"]
    coverage = report["coverage"]
    counts = coverage["settlement_counts"]
    minimum = report["minimum_sample"]["per_group"]
    lines = [
        "# Calibration and performance",
        "",
        "This report scores probabilities and projections that were stored before the events. "
        "It does not place a bet, it does not change Qualified, and it does not claim that the model is calibrated.",
        "",
        report["calibration_claim_reason"],
        "",
        "## Answers",
        "",
        f"- Predictions archived: **{questions['predictions_archived']}**",
        f"- With a current result: **{questions['with_a_result']}**",
        f"- Settled (Win, Loss, Push, or Void): **{questions['settled']}**",
        f"- Usable for conditional calibration: **{questions['usable_for_conditional_calibration']}**",
        f"- Usable for unconditional outcomes: **{questions['usable_for_unconditional_outcomes']}**",
        f"- Distinct projections usable for accuracy: **{questions['usable_for_projection_accuracy']}**",
        f"- Projection rows before duplicate prices were collapsed: **{questions['projection_rows_before_dedupe']}**",
        f"- Excluded from conditional calibration: **{questions['excluded_from_conditional']}**",
        f"- Excluded from unconditional outcomes: **{questions['excluded_from_unconditional']}**",
        f"- Excluded from projection accuracy: **{questions['excluded_from_projection']}**",
        "- Profit reported: **no**. ROI reported: **no**.",
        "",
        "### Which samples are large enough?",
        "",
        f"A group is large enough to read at {minimum} scored rows and {minimum} events. "
        "That bar is not a calibration claim.",
        "",
        "Out-of-sample conditional groups:",
    ]
    lines.extend(_market_lines(questions["markets_with_enough_out_of_sample_conditional_data"]))
    lines.append("")
    lines.append("Out-of-sample projection groups:")
    lines.extend(_market_lines(questions["markets_with_enough_out_of_sample_projection_data"]))
    lines.append("")
    lines.append("Large enough across every event in the split (not a held-out test), conditional:")
    lines.extend(_market_lines(questions["markets_with_enough_descriptive_conditional_data"]))
    lines.append("")
    lines.append("Large enough across every event in the split (not a held-out test), projections:")
    lines.extend(_market_lines(questions["markets_with_enough_descriptive_projection_data"]))
    lines.extend(
        [
            "",
            "## What was archived",
            "",
            f"- Win {counts['Win']}, Loss {counts['Loss']}, Push {counts['Push']}, "
            f"Void {counts['Void']}, Unknown {counts['Unknown']}, no result {counts['none']}.",
            f"- Qualified snapshots stored in the archive: {report['qualified_snapshots']}. "
            "This report did not change them.",
            f"- Corrections recorded and not applied: {report['corrections_not_applied']}.",
            f"- Superseded results not scored: {report['superseded_results_not_scored']}.",
            f"- Extra current results not scored: {report['additional_unsuperseded_results_not_scored']}.",
            "",
            coverage["settled_definition"],
            "",
            "### Excluded from conditional win probability",
            "",
        ]
    )
    lines.extend(_reason_lines(coverage["conditional_exclusions"]))
    lines.append("### Excluded from unconditional outcomes")
    lines.append("")
    lines.extend(_reason_lines(coverage["unconditional_exclusions"]))
    lines.append("### Excluded from projection accuracy")
    lines.append("")
    lines.extend(_reason_lines(coverage["projection_exclusions"]))
    lines.extend(
        [
            "## How the scores are separated",
            "",
            CONDITIONAL_DEFINITION,
            "",
            UNCONDITIONAL_DEFINITION,
            "",
            PROJECTION_DEFINITION,
            "",
            EV_DEFINITION,
            "",
            "## Chronological split",
            "",
            report["chronological_split"]["rule"],
            "",
            f"- Train events: {len(report['chronological_split']['train_event_ids'])}",
            f"- Validation events: {len(report['chronological_split']['validation_event_ids'])}",
            f"- Test events: {len(report['chronological_split']['test_event_ids'])}",
            "",
        ]
    )
    for name in _SLICE_ORDER:
        section = report["slices"][name]
        lines.extend(
            [
                f"## {section['label']}",
                "",
                section["note"],
                "",
                f"Events in this slice: **{section['event_count']}**.",
                "",
                "### Conditional win probability",
                "",
            ]
        )
        lines.extend(_metric_lines(section["conditional_groups"]))
        lines.append("### Unconditional outcomes, including pushes")
        lines.append("")
        lines.extend(_metric_lines(section["unconditional_groups"]))
        lines.append("### Projection accuracy")
        lines.append("")
        lines.extend(_projection_lines(section["projection_groups"]))
    expected = report["archived_expected_value"]
    lines.extend(
        [
            "## Expected value stored before the event",
            "",
            expected["definition"],
            "",
            f"Rows with a stored expected value: {expected['rows_with_expected_value']}. "
            f"Rows without one: {expected['rows_without_expected_value']}. "
            f"Rows left out of the groups: {expected['rows_not_grouped']}.",
            "",
        ]
    )
    if not expected["groups"]:
        lines.extend(["_No stored expected value to report._", ""])
    else:
        for group in expected["groups"]:
            family = str(group["line_family"]).replace("_", " ")
            lines.append(
                f"**{group['sport']} {group['market']}** ({family}): "
                f"sample size {group['sample_size']}, events {group['event_count']}, "
                f"mean archived expected value {_fmt(group['mean_archived_expected_value_per_unit'])}. "
                "Not profit."
            )
            for warning in group["warnings"]:
                lines.append(f"Warning: {warning}")
        lines.append("")
    lines.extend(["## Limitations", ""])
    for item in report["limitations"]:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)
