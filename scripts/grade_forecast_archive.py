"""Grade pending pregame forecasts from completed ESPN box scores.

A missing stat is never converted to zero. A listed DNP is voided. A player
missing from the box or a missing stat remains unresolved for human review.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from betting_model.features import _market_values
from betting_model.forecast_archive import (
    attach_forecast_result,
    archive_stats,
    forecast_metrics,
    pending_forecasts,
)


def _load_games(sport: str, root: Path) -> dict[str, Any]:
    path = Path(root) / sport / "games.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    games = payload.get("games") if isinstance(payload, dict) else None
    return games if isinstance(games, dict) else {}


def grade(archive_path: Path, data_root: Path) -> dict[str, Any]:
    pending = pending_forecasts(archive_path)
    games_by_sport: dict[str, dict[str, Any]] = {}
    counts = Counter()
    errors = []
    for snapshot in pending:
        sport = snapshot["sport"]
        if sport not in games_by_sport:
            games_by_sport[sport] = _load_games(sport, data_root)
        game = games_by_sport[sport].get(str(snapshot["event_id"]))
        if not isinstance(game, dict):
            counts["event_not_backfilled"] += 1
            continue
        status = str(game.get("status") or "").casefold()
        if "final" not in status and "complete" not in status:
            counts["event_not_final"] += 1
            continue
        retrieved_at = game.get("retrieved_at")
        result_source = game.get("source")
        if not retrieved_at or not result_source:
            counts["missing_result_provenance"] += 1
            continue
        players = [
            row for row in game.get("players") or []
            if isinstance(row, dict)
            and str(row.get("athlete_id") or "") == str(snapshot["player_id"])
        ]
        if not players:
            counts["player_not_in_box"] += 1
            continue
        if all(row.get("dnp") is True for row in players):
            result = attach_forecast_result(
                snapshot["snapshot_id"], actual_stat=None, game_status="void",
                result_source=result_source, retrieved_at=retrieved_at,
                path=archive_path,
            )
            if result.get("ok"):
                counts["void_dnp"] += 1
            else:
                counts["result_rejected"] += 1
                errors.append(result.get("reason"))
            continue
        actual, _ = _market_values(sport, snapshot["market"], players)
        if actual is None:
            counts["missing_actual_stat"] += 1
            continue
        result = attach_forecast_result(
            snapshot["snapshot_id"], actual_stat=actual, game_status="final",
            result_source=result_source, retrieved_at=retrieved_at,
            path=archive_path,
        )
        if result.get("ok"):
            counts["graded"] += 1
        else:
            counts["result_rejected"] += 1
            errors.append(result.get("reason"))

    metrics = forecast_metrics(archive_path)
    return {
        "ok": True,
        "checked_pending": len(pending),
        "graded": counts["graded"],
        "void_dnp": counts["void_dnp"],
        "unresolved": {key: value for key, value in counts.items()
                       if key not in {"graded", "void_dnp", "result_rejected"}},
        "result_rejected": counts["result_rejected"],
        "errors": errors,
        "metrics": metrics,
        "archive_summary": archive_stats(archive_path),
        "graded_at": datetime.now(timezone.utc).isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = grade(args.archive, args.data_root)
    rendered = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
