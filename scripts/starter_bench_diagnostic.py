"""Read-only NBA starter-versus-bench forecast-error diagnostic.

Reads the append-only forecast archive and ESPN historical-data box scores.
It never grades forecasts, writes to the archive, or changes model behavior.
Run from the repository root:
    python scripts/starter_bench_diagnostic.py
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from betting_model.features import _market_values


def _load_games(data_root: Path) -> dict[str, Any]:
    path = Path(data_root) / "nba" / "games.json"
    if not path.is_file():
        raise FileNotFoundError(f"NBA historical box scores not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    games = payload.get("games") if isinstance(payload, dict) else None
    if not isinstance(games, dict):
        raise ValueError(f"invalid NBA games file shape: {path}")
    return games


def _load_archive_rows(archive_path: Path) -> list[dict[str, Any]]:
    """Return latest active, final results; connect with SQLite read-only mode."""
    archive_path = Path(archive_path).resolve()
    if not archive_path.is_file():
        raise FileNotFoundError(f"forecast archive not found: {archive_path}")
    uri = f"file:{quote(str(archive_path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {"forecast_snapshots", "forecast_results"} <= tables:
            raise ValueError("archive is missing forecast_snapshots or forecast_results")
        rows = conn.execute(
            """
            WITH active_final_results AS (
                SELECT r.*
                FROM forecast_results AS r
                WHERE r.game_status = 'final'
                  AND r.actual_stat IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM forecast_results AS newer
                      WHERE newer.supersedes_result_id = r.result_id
                  )
            ),
            ranked_forecasts AS (
                SELECT
                    s.sport, s.event_id, s.player_id, s.market,
                    s.model_version, s.projected_value, r.actual_stat,
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            s.sport, s.event_id, s.player_id, s.market, s.model_version
                        ORDER BY s.projection_at DESC, s.archived_at DESC
                    ) AS forecast_rank
                FROM forecast_snapshots AS s
                JOIN active_final_results AS r ON r.snapshot_id = s.snapshot_id
                WHERE lower(s.sport) = 'nba'
            )
            SELECT sport, event_id, player_id, market, model_version,
                   projected_value, actual_stat
            FROM ranked_forecasts
            WHERE forecast_rank = 1
            ORDER BY event_id, player_id, market, model_version
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _player_rows_by_key(games: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    indexed: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event_id, game in games.items():
        if not isinstance(game, dict):
            continue
        status = str(game.get("status") or "").casefold()
        if "final" not in status and "complete" not in status:
            continue
        for player in game.get("players") or []:
            if not isinstance(player, dict):
                continue
            player_id = player.get("athlete_id")
            if player_id in (None, ""):
                continue
            indexed[(str(event_id), str(player_id))].append(player)
    return indexed


def _summarize(errors: list[float], event_ids: set[str]) -> dict[str, Any]:
    if not errors:
        raise ValueError("cannot summarize an empty group")
    return {
        "n_forecasts": len(errors),
        "n_events": len(event_ids),
        "mae": sum(abs(error) for error in errors) / len(errors),
        "rmse": math.sqrt(sum(error * error for error in errors) / len(errors)),
        "mean_signed_error": sum(errors) / len(errors),
    }


def analyze(archive_path: Path, data_root: Path) -> dict[str, Any]:
    """Join graded NBA forecasts to ESPN boxes on (event_id, athlete_id)."""
    games = _load_games(data_root)
    player_index = _player_rows_by_key(games)
    forecasts = _load_archive_rows(archive_path)

    errors: dict[tuple[str, str], list[float]] = defaultdict(list)
    event_sets: dict[tuple[str, str], set[str]] = defaultdict(set)
    exclusions: dict[str, int] = defaultdict(int)

    for forecast in forecasts:
        event_id = str(forecast["event_id"])
        player_id = str(forecast["player_id"])
        market = str(forecast["market"])
        rows = player_index.get((event_id, player_id), [])
        if not rows:
            exclusions["player_not_in_box"] += 1
            continue
        if all(row.get("dnp") is True for row in rows):
            exclusions["dnp"] += 1
            continue

        played_rows = [row for row in rows if row.get("dnp") is not True]
        starter_values = [row.get("starter") for row in played_rows]
        # Do not infer a role from position, category, or a missing value.
        if not starter_values or any(type(value) is not bool for value in starter_values):
            exclusions["starter_tag_missing_or_invalid"] += 1
            continue
        if len(set(starter_values)) != 1:
            exclusions["starter_tag_conflict"] += 1
            continue

        box_actual, _ = _market_values("nba", market, played_rows)
        if box_actual is None:
            exclusions["box_actual_missing"] += 1
            continue
        archived_actual = float(forecast["actual_stat"])
        if not math.isclose(box_actual, archived_actual, rel_tol=0.0, abs_tol=1e-9):
            exclusions["archive_box_actual_mismatch"] += 1
            continue

        role = "starter" if starter_values[0] is True else "bench"
        error = archived_actual - float(forecast["projected_value"])
        key = (market, role)
        errors[key].append(error)
        event_sets[key].add(event_id)

    groups = []
    for (market, role), values in sorted(errors.items()):
        groups.append({
            "market": market,
            "role": role,
            **_summarize(values, event_sets[(market, role)]),
        })

    return {
        "ok": True,
        "sport": "nba",
        "join_key": ["event_id", "player_id"],
        "error_definition": "actual_stat - projected_value",
        "groups": groups,
        "included_forecasts": sum(len(values) for values in errors.values()),
        "exclusions": dict(sorted(exclusions.items())),
        "read_only": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive", type=Path,
        default=Path("historical-data/forecasts/forecast_archive.sqlite"),
        help="SQLite forecast archive (default: historical-data/forecasts/forecast_archive.sqlite)",
    )
    parser.add_argument(
        "--data-root", type=Path, default=Path("historical-data/data"),
        help="historical-data directory containing nba/games.json",
    )
    parser.add_argument("--out", type=Path, help="optional JSON report destination")
    args = parser.parse_args(argv)

    report = analyze(args.archive, args.data_root)
    rendered = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
