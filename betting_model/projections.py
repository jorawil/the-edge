"""Tell a recent average apart from a projection.

This module does not build a forecast. It records what the file supplied,
or labels a recent average when that is the only number available.
Season averages and model forecasts are left as "unavailable" unless the
file itself contains them. Nothing here is invented.
"""

from __future__ import annotations

from typing import Any

from .timeutil import parse_aware_datetime
from .version import MODEL_VERSION


def describe_projection(
    prop: dict[str, Any],
    row: dict[str, Any],
    file_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    projected = row.get("projected", prop.get("projected"))
    proj_source = str(row.get("proj_source") or "")
    supplied_kind = str(prop.get("projection_kind") or "").strip().lower()
    user_supplied_projection = prop.get("projected") is not None

    if projected is None:
        kind = "unavailable"
    elif proj_source.startswith("nba_api") or (
        not user_supplied_projection and row.get("recent_avg")
    ):
        kind = "recent_average"
    elif supplied_kind == "model":
        kind = "model"
    else:
        kind = "manual"

    prop_ts = parse_aware_datetime(prop.get("projection_at") or prop.get("generated_at"))
    file_ts = None
    if isinstance(file_meta, dict):
        file_ts = parse_aware_datetime(
            file_meta.get("research_at") or file_meta.get("generated_at")
        )
    if prop_ts is not None:
        generated_at = prop_ts.isoformat()
        scope = "prop"
    elif file_ts is not None:
        generated_at = file_ts.isoformat()
        scope = "file"
    else:
        generated_at = None
        scope = "missing"

    named_source = prop.get("projection_source") or prop.get("source")
    source = named_source or (proj_source or None)
    warnings: list[str] = []
    if kind == "recent_average":
        warnings.append(
            "This is the average of recent games, not a prediction for the next game."
        )
    elif kind == "model":
        warnings.append(
            "The file calls this a model projection. This program did not calculate that number."
        )
    elif kind == "manual":
        warnings.append(
            "This number was typed into the file. This program did not calculate it."
        )
    else:
        warnings.append("No projection was available.")
    if scope == "missing":
        warnings.append("There is no timestamp on this projection.")
    if scope == "file":
        warnings.append(
            "The only timestamp is on the whole file, not on this player."
        )
    if not source:
        warnings.append("The projection source is missing.")
    if prop.get("season_average") is None:
        warnings.append("Season average: unavailable.")
    if kind != "model":
        warnings.append("Model projection from this program: unavailable.")

    provenance_complete = (
        kind in {"manual", "model"} and bool(named_source) and scope == "prop"
    )
    recent_value = prop.get("recent_average")
    if kind == "recent_average":
        recent_value = projected
    return {
        "player": prop.get("player") or row.get("player"),
        "player_id": prop.get("player_id") or (row.get("recent_avg") or {}).get("player_id"),
        "event_id": prop.get("event_id") or prop.get("game_id"),
        "sport": prop.get("sport") or row.get("sport"),
        "market": prop.get("market") or row.get("market"),
        "projected_value": projected,
        "kind": kind,
        "recent_average": recent_value,
        "season_average": prop.get("season_average"),
        "model_projection": projected if kind == "model" else None,
        "generated_at": generated_at,
        "generated_at_scope": scope,
        "source": source,
        "model_version": MODEL_VERSION,
        "warnings": warnings,
        "explanation": " ".join(warnings),
        "provenance_complete": provenance_complete,
        "evaluable_as_forecast": provenance_complete,
    }
