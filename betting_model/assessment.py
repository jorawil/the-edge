"""Decide whether a prop is worth a closer look.

QUALIFIED is not a lock and not a recommendation to bet. It means the
supplied inputs cleared the edge threshold and the data-quality checks
this program knows how to do.

Sigma defaults are not calibrated. A qualified row still says that.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .projections import describe_projection
from .timeutil import parse_aware_datetime, utc_now

# One percentage point under the threshold is "near", not a pass.
WATCH_BAND = 0.01
DEFAULT_MAX_PRICE_AGE_SECONDS = 30 * 60
KNOWN_SPORTS = {
    "nba",
    "basketball_nba",
    "wnba",
    "basketball_wnba",
    "nfl",
    "americanfootball_nfl",
    "mlb",
    "baseball_mlb",
}
# Only these structured values are treated as "available". Anything else
# is a flag for a person, not an automatic stat change.
AVAILABLE_ROLES = {"available", "active", "healthy", "starter", "probable"}


def _freshness(prop: dict[str, Any], row: dict[str, Any], as_of: datetime) -> dict[str, Any]:
    raw = (
        row.get("retrieved_at")
        or row.get("price_retrieved_at")
        or prop.get("retrieved_at")
        or prop.get("price_retrieved_at")
    )
    parsed = parse_aware_datetime(raw)
    max_age = prop.get("max_price_age_seconds", DEFAULT_MAX_PRICE_AGE_SECONDS)
    try:
        max_age_seconds = float(max_age)
    except (TypeError, ValueError):
        max_age_seconds = float(DEFAULT_MAX_PRICE_AGE_SECONDS)
    if parsed is None:
        return {
            "state": "unverified",
            "retrieved_at": None,
            "age_seconds": None,
            "detail": "no timezone-aware price timestamp",
        }
    age = (as_of - parsed).total_seconds()
    if age < 0:
        return {
            "state": "unverified",
            "retrieved_at": parsed.isoformat(),
            "age_seconds": age,
            "detail": "price timestamp is later than the evaluation time",
        }
    if age > max_age_seconds:
        return {
            "state": "stale",
            "retrieved_at": parsed.isoformat(),
            "age_seconds": round(age, 1),
            "detail": (
                f"price is {age / 60:.0f} minutes old, past the "
                f"{max_age_seconds / 60:.0f}-minute limit"
            ),
        }
    return {
        "state": "fresh",
        "retrieved_at": parsed.isoformat(),
        "age_seconds": round(age, 1),
        "detail": "price timestamp is inside the age limit",
    }


def _edge_sentence(row: dict[str, Any]) -> str:
    metric = row.get("threshold_edge_metric")
    if metric == "edge_vs_fair_market":
        return (
            "The pass/fail edge is the model win probability minus the "
            "no-vig fair probability."
        )
    if metric == "settlement_edge_vs_raw_implied":
        return (
            "The pass/fail edge removes the push chance, then subtracts the "
            "raw implied probability. The vig is still inside that price."
        )
    if metric == "edge_vs_raw_implied":
        return (
            "The pass/fail edge is the model probability minus the raw "
            "implied probability. The vig is still inside that price. "
            "A no-vig number was not available."
        )
    return "No edge was compared with the threshold."


def _role_blockers(prop: dict[str, Any]) -> list[str]:
    reasons = []
    if prop.get("role_uncertain") is True:
        reasons.append("the file flags the role as uncertain")
    status = prop.get("injury_status") or prop.get("availability")
    if status is not None:
        text = str(status).strip().lower()
        if text not in AVAILABLE_ROLES:
            reasons.append(
                f"availability is '{status}', which needs a person to review before any grade"
            )
    return reasons


def assess(
    prop: dict[str, Any],
    row: dict[str, Any],
    *,
    file_meta: dict[str, Any] | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Attach projection provenance, freshness, and a status. Does not change edge math."""
    moment = as_of or utc_now()
    if moment.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    projection = describe_projection(prop, row, file_meta)
    fresh = _freshness(prop, row, moment)
    reasons: list[str] = []
    kind = projection["kind"]

    if not row.get("evaluated"):
        status = "NOT_EVALUABLE"
        reasons.append(row.get("skip_reason") or "this prop could not be priced")
    elif kind == "recent_average":
        status = "NOT_EVALUABLE"
        reasons.append(
            "a recent average was used as the number, and a recent average is not a projection"
        )
    elif kind != "manual" and kind != "model":
        status = "NOT_EVALUABLE"
        reasons.append("no usable projection")
    elif row.get("push_handling") == "unsupported":
        status = "NOT_EVALUABLE"
        reasons.append(row.get("push_reason") or "push rules are not defined for this market")
    else:
        metric = row.get("threshold_edge")
        threshold = row.get("edge_threshold")
        if metric is None or threshold is None:
            status = "NOT_EVALUABLE"
            reasons.append("there is no edge number that can be compared with the threshold")
        else:
            cleared = bool(row.get("threshold_cleared"))
            near = (not cleared) and float(metric) > float(threshold) - WATCH_BAND
            blockers: list[str] = []
            if not projection["provenance_complete"]:
                blockers.append("projection source or timestamp is incomplete")
            if fresh["state"] != "fresh":
                blockers.append(fresh["detail"])
            if row.get("sigma_source") == "unverified_default":
                blockers.append(
                    "the sigma is the generic fallback, not a documented market default"
                )
            sport = str(prop.get("sport") or "").strip().lower()
            if sport not in KNOWN_SPORTS:
                blockers.append(f"sport '{prop.get('sport')}' is not in the supported list")
            blockers.extend(_role_blockers(prop))
            if row.get("sigma_calibrated") is not True:
                # Not a blocker. It is always said out loud.
                pass
            if blockers:
                reasons.extend(blockers)
                status = "WATCH" if cleared or near else "REJECTED"
            elif cleared:
                status = "QUALIFIED"
                reasons.append("supplied inputs cleared the threshold and the checks above")
            elif near:
                status = "WATCH"
                reasons.append("the edge is within 1 percentage point under the threshold")
            else:
                status = "REJECTED"
                reasons.append("the edge does not clear the threshold")

    if fresh["state"] == "stale":
        quality = "stale"
    elif status == "NOT_EVALUABLE" or kind == "recent_average":
        quality = "incomplete"
    elif status == "QUALIFIED" and fresh["state"] == "fresh":
        quality = "fresh"
    else:
        quality = "uncertain"

    sigma_note = (
        "Sigma is not a measured forecast error. "
        + (
            "The file supplied the sigma."
            if row.get("sigma_source") == "explicit"
            else "The program used an uncalibrated default."
            if row.get("sigma_source")
            else "Sigma was not used."
        )
    )
    explanation = " ".join(
        [
            projection["explanation"],
            _edge_sentence(row),
            sigma_note,
            "Status: " + status.replace("_", " ") + ".",
            " ".join(reasons),
        ]
    )
    return {
        "projection": projection,
        "projection_kind": kind,
        "illustrative_only": kind == "recent_average",
        "status": status,
        "status_reasons": reasons,
        "data_quality": quality,
        "freshness": fresh["state"],
        "price_retrieved_at": fresh["retrieved_at"],
        "price_age_seconds": fresh["age_seconds"],
        "freshness_detail": fresh["detail"],
        "explanation": " ".join(explanation.split()),
        "qualified": status == "QUALIFIED",
        "model_version": projection["model_version"],
    }


def apply_assessment(
    prop: dict[str, Any],
    row: dict[str, Any],
    *,
    file_meta: dict[str, Any] | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    row.update(assess(prop, row, file_meta=file_meta, as_of=as_of))
    books = row.get("books")
    if isinstance(books, dict):
        for book_row in books.values():
            if not isinstance(book_row, dict) or "status" in book_row:
                continue
            if book_row.get("missing") and "evaluated" not in book_row:
                book_row["status"] = "NOT_EVALUABLE"
                book_row["status_reasons"] = [book_row.get("reason") or "price not available from this input"]
                continue
            book_prop = dict(prop)
            for field in ("line", "odds", "source", "retrieved_at", "opposite"):
                if book_row.get(field) is not None:
                    book_prop[field] = book_row[field]
            # Nested rows were already priced. Assess them once.
            if "player" in book_row or "evaluated" in book_row:
                book_row.update(assess(book_prop, book_row, file_meta=file_meta, as_of=as_of))
    return row
