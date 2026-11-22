"""Attach verified finals to archived pregame snapshots.

Grading uses ``attach_result``. It does not update a snapshot and it does
not delete a result. A Win, Loss, Push, or Void that is already current is
left alone. A second current result is a conflict: nothing new is attached
and nothing is chosen. A missing stat is not stored as zero, and an
unverified result is not stored as a loss.

Records that are already settled are not selected again, so running this
twice does not grade them twice.
"""

from __future__ import annotations

import logging
from datetime import timezone
from pathlib import Path
from typing import Any

from . import archive as archive_mod
from .archive_analysis import select_current_result
from .journal import Journal
from .player_names import match_players, normalize_label
from .timeutil import parse_aware_datetime
from .version import MODEL_VERSION

logger = logging.getLogger(__name__)

_SETTLED = {"Win", "Loss", "Push", "Void"}
_VOID_STATUS = {"postponed", "canceled"}


def _utc_text() -> str:
    moment = archive_mod._utc_now()
    if moment.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now():
    moment = archive_mod._utc_now()
    if moment.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return moment.astimezone(timezone.utc)


def _same_number(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return abs(float(left) - float(right)) <= 1e-9
    except (TypeError, ValueError):
        return False


def player_team_check(snapshot_team: object, player_team: object) -> str:
    """Confirmed when the labels fold to the same text.

    An abbreviation beside a full name stays unconfirmed. Two different
    abbreviations, or two different full names, are a contradiction.
    """
    left = normalize_label(snapshot_team)
    right = normalize_label(player_team)
    if not left or not right:
        return "unconfirmed"
    if left == right:
        return "confirmed"
    left_code = " " not in left and len(left) <= 4
    right_code = " " not in right and len(right) <= 4
    if left_code != right_code:
        return "unconfirmed"
    return "contradiction"


def _item(action: str, snapshot: dict[str, Any], reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "action": action,
        "reason": reason,
        "archive_id": snapshot.get("archive_id"),
        "player": snapshot.get("player_name"),
        "event_id": snapshot.get("event_id"),
        "market": snapshot.get("canonical_market"),
        "sport": snapshot.get("sport"),
        **extra,
    }


def _journal(journal: Journal, run_id: str, item: dict[str, Any], *, raw: object = None) -> None:
    journal.add(
        run_id,
        action=item["action"],
        archive_id=item.get("archive_id"),
        player=item.get("player"),
        event_id=item.get("event_id"),
        market=item.get("market"),
        sport=item.get("sport"),
        sportsbook=item.get("sportsbook"),
        reason=item.get("reason"),
        settlement=item.get("settlement"),
        result_id=item.get("result_id"),
        raw=raw,
    )
    if item["action"] != "graded":
        logger.warning("grading %s %s: %s", item["action"], item.get("archive_id"), item.get("reason"))


def grade_archive(
    database: Path,
    source: Any,
    *,
    journal_path: Path | None = None,
) -> dict[str, Any]:
    """Grade eligible snapshots from one result source. Unverified rows stay ungraded."""
    database = Path(database)
    journal = Journal(journal_path)
    run_id = journal.start_run("grade")
    try:
        if not database.exists():
            summary = _empty("archive does not exist; no file was created")
            summary["run_id"] = run_id
            journal.finish_run(run_id, "completed", summary)
            return summary
        summary = _grade(database, source, journal, run_id)
        summary["run_id"] = run_id
        summary["model_version"] = MODEL_VERSION
        summary["calibration_claimed"] = False
        summary["profit_reported"] = False
        summary["roi_reported"] = False
        summary["automatic_betting"] = False
        summary["note"] = (
            "Grading attaches verified outcomes. It does not measure calibration, profit, or ROI."
        )
        journal.finish_run(run_id, "completed", {key: value for key, value in summary.items() if key != "items"})
        logger.info(
            "grading processed=%s graded=%s skipped=%s unresolved=%s conflicts=%s provider_failures=%s",
            summary["processed"],
            summary["graded"],
            summary["skipped"],
            summary["unresolved"],
            summary["conflicts"],
            summary["provider_failures"],
        )
        return summary
    except Exception:
        journal.finish_run(run_id, "failed", {"ok": False, "provider_failures": 1})
        raise


def _empty(reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "processed": 0,
        "graded": 0,
        "skipped": 0,
        "unresolved": 0,
        "conflicts": 0,
        "provider_failures": 0,
        "items": [],
        "model_version": MODEL_VERSION,
        "calibration_claimed": False,
        "profit_reported": False,
        "automatic_betting": False,
    }


def _grade(database: Path, source: Any, journal: Journal, run_id: str) -> dict[str, Any]:
    pending = archive_mod.list_ungraded(database)
    items = []
    for snapshot in pending:
        item = _one(database, snapshot, source)
        items.append(item)
        _journal(journal, run_id, item, raw=item.pop("raw", None))
    return {
        "ok": True,
        "processed": len(items),
        "graded": sum(1 for item in items if item["action"] == "graded"),
        "skipped": sum(1 for item in items if item["action"] == "skipped"),
        "unresolved": sum(1 for item in items if item["action"] == "unresolved"),
        "conflicts": sum(1 for item in items if item["action"] == "conflict"),
        "provider_failures": sum(1 for item in items if item["action"] == "provider_failure"),
        "items": items,
    }


def _one(database: Path, snapshot: dict[str, Any], source: Any) -> dict[str, Any]:
    full = archive_mod.get_snapshot(database, snapshot["archive_id"]) or snapshot
    current, _history, live = select_current_result(full.get("results") or [])
    if live > 1:
        return _item(
            "conflict",
            snapshot,
            "more than one current result is already stored, so none was chosen and nothing was attached",
        )
    start = parse_aware_datetime(snapshot.get("event_start"))
    if start is None:
        return _item("unresolved", snapshot, "archived event start could not be read, so nothing was attached")
    if _now() < start.astimezone(timezone.utc):
        return _item(
            "skipped",
            snapshot,
            "archive clock is before the scheduled start, so no result was attached",
        )

    fetched = source.fetch_event(snapshot)
    if not fetched.get("ok"):
        action = "provider_failure" if fetched.get("failure") else "unresolved"
        return _item(action, snapshot, fetched.get("reason") or "result source did not verify this event")

    status = str(fetched.get("status") or "unknown")
    if status in _VOID_STATUS:
        return _attach(
            database,
            snapshot,
            current,
            game_status=status,
            actual=None,
            actual_provided=False,
            source_text=_source_text(fetched, "event"),
            raw={"status": status, "event_id": fetched.get("event_id"), "matched_by": fetched.get("matched_by")},
        )
    if status != "final":
        return _item(
            "unresolved",
            snapshot,
            f"event status is {status}; it is not final and was not graded",
        )

    players = fetched.get("players") or []
    matched = match_players(snapshot.get("player_name"), snapshot.get("player_id"), players)
    if not matched["ok"]:
        action = "conflict" if matched.get("ambiguous") else "unresolved"
        reason = matched.get("reason") or "player could not be matched"
        if not matched.get("ambiguous"):
            reason = reason + "; this was not graded as a loss or a zero"
        return _item(action, snapshot, reason)
    player = players[matched["index"]]
    team_check = player_team_check(snapshot.get("team"), player.get("team"))
    if team_check == "contradiction":
        return _item(
            "conflict",
            snapshot,
            "player team does not match the archived team, so the stat was not attached",
        )
    if player.get("dnp_conflict"):
        return _item(
            "conflict",
            snapshot,
            "source marked the player did not play and also listed stats, so nothing was attached",
        )
    if player.get("dnp"):
        return _attach(
            database,
            snapshot,
            current,
            game_status="dnp",
            actual=None,
            actual_provided=False,
            source_text=_source_text(fetched, matched["method"]),
            raw={"player_id": player.get("player_id"), "dnp": True},
        )
    market = snapshot.get("canonical_market")
    if market in (player.get("stat_conflicts") or []):
        return _item(
            "conflict",
            snapshot,
            f"source listed more than one {market} value, so none was chosen",
        )
    stats = player.get("stats") or {}
    if market not in stats:
        return _item(
            "unresolved",
            snapshot,
            f"{market} was not in the box score and was not treated as zero",
        )
    return _attach(
        database,
        snapshot,
        current,
        game_status="final",
        actual=stats[market],
        actual_provided=True,
        source_text=_source_text(fetched, matched["method"]),
        raw={
            "player_id": player.get("player_id"),
            "matched_by": matched["method"],
            "market": market,
            "actual": stats[market],
            "event_id": fetched.get("event_id"),
        },
    )


def _source_text(fetched: dict[str, Any], player_method: str) -> str:
    return (
        f"{fetched.get('source')}; event {fetched.get('event_id')}; "
        f"event matched by {fetched.get('matched_by')}; player matched by {player_method}"
    )


def _attach(
    database: Path,
    snapshot: dict[str, Any],
    current: dict[str, Any] | None,
    *,
    game_status: str,
    actual: float | None,
    actual_provided: bool,
    source_text: str,
    raw: dict[str, Any],
) -> dict[str, Any]:
    try:
        settlement, _reason, stored_actual, _missing = archive_mod.settle_prop(
            market=snapshot["canonical_market"],
            line=snapshot["line"],
            side=snapshot["side"],
            actual=actual,
            game_status=game_status,
            actual_provided=actual_provided,
        )
    except ValueError as exc:
        return _item("unresolved", snapshot, str(exc))
    if current is not None:
        same = current.get("settlement") == settlement and _same_number(current.get("actual_stat"), stored_actual)
        if same:
            item = _item("skipped", snapshot, "this result is already recorded and was not attached again")
            item["settlement"] = settlement
            return item
        if current.get("settlement") in _SETTLED:
            return _item(
                "conflict",
                snapshot,
                "a settled result is already stored and the new result disagrees, so the old result was kept",
            )
        if (
            current.get("settlement") == "Unknown"
            and current.get("actual_stat") is not None
            and not _same_number(current.get("actual_stat"), stored_actual)
        ):
            return _item(
                "conflict",
                snapshot,
                "the stored unresolved result disagrees with the new stat, so neither was chosen",
            )
    attached = archive_mod.attach_result(
        database,
        snapshot["archive_id"],
        game_status=game_status,
        result_source=source_text,
        result_retrieved_at=_utc_text(),
        actual=actual,
        actual_provided=actual_provided,
        event_id=str(snapshot.get("event_id")) if snapshot.get("event_id") is not None else None,
        player_id=str(snapshot.get("player_id")) if snapshot.get("player_id") not in (None, "") else None,
    )
    if not attached.get("ok"):
        return _item("unresolved", snapshot, attached.get("reason") or "result was not attached")
    item = _item("graded", snapshot, attached.get("reason") or settlement)
    item["settlement"] = attached.get("settlement")
    item["result_id"] = attached.get("result_id")
    item["raw"] = raw
    logger.info("graded %s as %s", snapshot.get("archive_id"), attached.get("settlement"))
    return item


def grade_with_fixture(database: Path, document: dict[str, Any], *, journal_path: Path | None = None) -> dict[str, Any]:
    from .result_sources import load_result_fixture

    return grade_archive(database, load_result_fixture(document), journal_path=journal_path)
