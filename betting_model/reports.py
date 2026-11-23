"""Plain-English report header for diagnostic-only evaluation output."""

from __future__ import annotations

from typing import Any

from .core import parse_side


def group_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups = {
        "QUALIFIED": [],
        "SCREEN_PASS_UNCALIBRATED": [],
        "WATCH": [],
        "REJECTED": [],
        "NOT_EVALUABLE": [],
    }
    for row in rows:
        status = str(row.get("status") or "NOT_EVALUABLE")
        groups.setdefault(status, []).append(row)
    return groups


def plain_header(payload: dict[str, Any]) -> str:
    """Opening section a non-programmer can read before the old tables."""
    summary = payload["summary"]
    lines = [
        "# THE EDGE report",
        "",
        "This is an analysis only. It does not place a bet. "
        "A qualified prop is not a lock and not a recommendation by itself.",
        "",
        f"- Generated: **{payload['generated_at']}** ({payload['timezone']})",
        f"- Props file: `{payload['props_path']}`",
        f"- Model version: `{payload.get('model_version', 'unknown')}`",
        "- Probability and expected-value fields are diagnostic only: distribution assumptions "
        "are not calibrated, so this report does not identify betting edges.",
        "- Sigma values in this version are **not calibrated** to forecast misses. "
        "Do not read them as a proven error bar.",
        "",
        "## What the grades mean",
        "",
        "- **QUALIFIED**: the edge cleared the threshold and the sigma and distribution "
        "used for that edge have been calibrated. This version does not assign Qualified, "
        "because those pieces are not calibrated yet.",
        "- **SCREEN PASS UNCALIBRATED**: an older diagnostic condition was met, but its "
        "probability assumptions are uncalibrated. This is not an edge or recommendation.",
        "- **WATCH**: interesting or close, but something is missing, stale, or uncertain.",
        "- **REJECTED**: the edge does not clear the threshold, or a data problem rules it out.",
        "- **NOT EVALUABLE**: a required piece is missing, or the only number is a recent average. "
        "A recent average is history, not a prediction.",
        "",
        "## Counts",
        "",
        f"- Qualified: **{summary.get('qualified', 0)}**",
        f"- Screen pass, uncalibrated: **{summary.get('screen_pass_uncalibrated', 0)}**",
        f"- Watch: {summary.get('watch', 0)}",
        f"- Rejected: {summary.get('rejected', 0)}",
        f"- Not evaluable: {summary.get('not_evaluable', 0)}",
        "",
    ]
    qualified = payload.get("qualified") or []
    lines.append("## Qualified")
    lines.append("")
    if not qualified:
        lines.append("_None. Showing no qualified prop is a valid result._")
        lines.append("")
    else:
        lines.extend(_grade_table(qualified))
    screen = payload.get("screen_pass_uncalibrated") or []
    lines.append("## Screen pass, uncalibrated")
    lines.append("")
    if not screen:
        lines.append("_None._")
        lines.append("")
    else:
        lines.append(
            "These rows met a legacy diagnostic condition only. Their probabilities are uncalibrated. "
            "They must not be treated as betting opportunities."
        )
        lines.append("")
        lines.append(f"Showing {min(15, len(screen))} of {len(screen)}. The JSON file has every row.")
        lines.append("")
        lines.extend(_grade_table(screen[:15]))
    watch = payload.get("watch") or []
    lines.append("## Watch")
    lines.append("")
    if not watch:
        lines.append("_None._")
        lines.append("")
    else:
        lines.append(f"Showing {min(15, len(watch))} of {len(watch)}. The JSON file has every row.")
        lines.append("")
        lines.extend(_grade_table(watch[:15]))
    lines += [
        "## Rejected and not evaluable",
        "",
        f"Rejected: {summary.get('rejected', 0)}. "
        f"Not evaluable: {summary.get('not_evaluable', 0)}.",
        "Reasons are on each row in the JSON (`status_reasons` and `explanation`).",
        "",
        "",
    ]
    return "\n".join(lines)


def _grade_table(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Player | Market | Side | Line | Odds | Projection | Grade | Why |",
        "|---|---|:---:|---:|---:|---|---|---|",
    ]
    for row in rows:
        try:
            side = "Over" if parse_side(row.get("is_over")) else "Under"
        except ValueError:
            side = "Unknown side"
        projection = row.get("projection") or {}
        kind = projection.get("kind") or row.get("projection_kind") or ""
        why = "; ".join(row.get("status_reasons") or [])
        lines.append(
            f"| {row.get('player')} | {row.get('market')} | {side} | {row.get('line')} | "
            f"{row.get('odds')} | {row.get('projected')} ({kind}) | {row.get('status')} | {why} |"
        )
    lines.append("")
    return lines
