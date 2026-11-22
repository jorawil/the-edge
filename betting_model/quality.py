"""Collection and grading counts.

These numbers describe the archive and the command journal. They do not say
the model is calibrated, profitable, or a betting edge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import archive as archive_mod
from .archive_analysis import select_current_result
from .journal import Journal, default_journal_path
from .version import MODEL_VERSION

_NOTE = (
    "These counts describe data collection and grading. "
    "They are not calibration, profitability, ROI, or a betting edge."
)


def quality_report(database: Path | None = None, journal_path: Path | None = None) -> dict[str, Any]:
    database = Path(database) if database is not None else archive_mod.default_db_path()
    journal_file = Path(journal_path) if journal_path is not None else default_journal_path()
    report = {
        "kind": "collection_and_grading_quality",
        "model_version": MODEL_VERSION,
        "calibration_claimed": False,
        "accuracy_claimed": False,
        "profit_reported": False,
        "roi_reported": False,
        "automatic_betting": False,
        "note": _NOTE,
        "archive": _archive_section(database),
        "journal": _journal_section(journal_file),
    }
    return report


def _archive_section(database: Path) -> dict[str, Any]:
    if not database.exists():
        return {
            "exists": False,
            "path": str(database),
            "snapshots": 0,
            "by_sport": {},
            "by_sportsbook": {},
            "by_model_version": {},
            "results_attached": 0,
            "current_settlements": {},
            "ungraded": 0,
            "note": "No archive file was found, and this report did not create one.",
        }
    conn = archive_mod.connect(database)
    try:
        snapshots = [dict(row) for row in conn.execute("SELECT * FROM snapshots").fetchall()]
        results = [dict(row) for row in conn.execute("SELECT * FROM results").fetchall()]
    finally:
        conn.close()
    by_sport: dict[str, int] = {}
    by_book: dict[str, int] = {}
    by_version: dict[str, int] = {}
    for row in snapshots:
        by_sport[row["sport"]] = by_sport.get(row["sport"], 0) + 1
        by_book[row["sportsbook"]] = by_book.get(row["sportsbook"], 0) + 1
        version = row.get("model_version") or "missing"
        by_version[version] = by_version.get(version, 0) + 1
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        grouped.setdefault(result["archive_id"], []).append(result)
    current_counts: dict[str, int] = {}
    ambiguous = 0
    for rows in grouped.values():
        current, _history, live = select_current_result(rows)
        if live != 1 or current is None:
            ambiguous += 1
            continue
        label = str(current.get("settlement") or "missing")
        current_counts[label] = current_counts.get(label, 0) + 1
    return {
        "exists": True,
        "path": str(database),
        "snapshots": len(snapshots),
        "by_sport": by_sport,
        "by_sportsbook": by_book,
        "by_model_version": by_version,
        "results_attached": len(results),
        "current_settlements": current_counts,
        "ambiguous_current_results": ambiguous,
        "ungraded": len(archive_mod.list_ungraded(database)),
    }


def _journal_section(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "stored": None,
            "duplicates_skipped": None,
            "rejected": None,
            "rejected_reasons": [],
            "graded": None,
            "skipped": None,
            "unresolved": None,
            "conflicts": None,
            "provider_failures": None,
            "provider_failure_reasons": [],
            "last_collection_at": None,
            "last_successful_collection_at": None,
            "last_grading_at": None,
            "last_successful_grading_at": None,
            "note": "No journal file was found. Rejection and failure counts are unknown, not zero work.",
        }
    journal = Journal(path)
    events = journal.events()
    reasons: dict[str, int] = {}
    failures: dict[str, int] = {}
    for event in events:
        if event["action"] == "rejected" and event.get("reason"):
            reasons[event["reason"]] = reasons.get(event["reason"], 0) + 1
        if event["action"] == "provider_failure" and event.get("reason"):
            failures[event["reason"]] = failures.get(event["reason"], 0) + 1

    def count(action: str) -> int:
        return sum(1 for event in events if event["action"] == action)

    def stamp(kind: str, *, successful: bool) -> str | None:
        runs = journal.runs(kind)
        if not successful:
            for run in runs:
                if run.get("finished_at"):
                    return run["finished_at"]
            return None
        return journal.last_success(kind)

    return {
        "exists": True,
        "path": str(path),
        "stored": count("stored"),
        "duplicates_skipped": count("duplicate"),
        "rejected": count("rejected"),
        "rejected_reasons": [{"reason": reason, "count": reasons[reason]} for reason in sorted(reasons)],
        "graded": count("graded"),
        "skipped": count("skipped"),
        "unresolved": count("unresolved"),
        "conflicts": count("conflict"),
        "provider_failures": count("provider_failure"),
        "provider_failure_reasons": [{"reason": reason, "count": failures[reason]} for reason in sorted(failures)],
        "last_collection_at": stamp("collect", successful=False),
        "last_successful_collection_at": stamp("collect", successful=True),
        "last_grading_at": stamp("grade", successful=False),
        "last_successful_grading_at": stamp("grade", successful=True),
    }


def render_markdown(report: dict[str, Any]) -> str:
    archive = report["archive"]
    journal = report["journal"]
    lines = [
        "# Collection and grading quality",
        "",
        report["note"],
        "",
        "This report does not say the model is calibrated. It does not report profit or ROI.",
        "",
        "## Archive",
        "",
        f"- Snapshots: {archive['snapshots']}",
        f"- By sport: {_pairs(archive['by_sport'])}",
        f"- By sportsbook: {_pairs(archive['by_sportsbook'])}",
        f"- By model version: {_pairs(archive['by_model_version'])}",
        f"- Result rows attached: {archive['results_attached']}",
        f"- Current settlements: {_pairs(archive['current_settlements'])}",
        f"- Still ungraded: {archive['ungraded']}",
        f"- Conflicting current results: {archive.get('ambiguous_current_results', 0)}",
        "",
        "## Journal",
        "",
        f"- Snapshots stored by collection: {_count(journal['stored'])}",
        f"- Duplicate records skipped: {_count(journal['duplicates_skipped'])}",
        f"- Rejected: {_count(journal['rejected'])}",
        f"- Results attached by grading: {_count(journal['graded'])}",
        f"- Grading skipped: {_count(journal['skipped'])}",
        f"- Unresolved: {_count(journal['unresolved'])}",
        f"- Conflicts: {_count(journal['conflicts'])}",
        f"- Provider failures: {_count(journal['provider_failures'])}",
        f"- Last collection: {journal['last_collection_at'] or 'none'}",
        f"- Last successful collection: {journal['last_successful_collection_at'] or 'none'}",
        f"- Last grading: {journal['last_grading_at'] or 'none'}",
        f"- Last successful grading: {journal['last_successful_grading_at'] or 'none'}",
        "",
    ]
    if journal["rejected_reasons"]:
        lines.append("Rejected or missing fields:")
        lines.append("")
        for item in journal["rejected_reasons"]:
            lines.append(f"- {item['count']}: {item['reason']}")
        lines.append("")
    if journal["provider_failure_reasons"]:
        lines.append("Provider failures:")
        lines.append("")
        for item in journal["provider_failure_reasons"]:
            lines.append(f"- {item['count']}: {item['reason']}")
        lines.append("")
    return "\n".join(lines)


def _count(value: object) -> str:
    if value is None:
        return "not recorded"
    return str(value)


def _pairs(values: dict[str, int]) -> str:
    if not values:
        return "none"
    return ", ".join(f"{key} {values[key]}" for key in sorted(values))
