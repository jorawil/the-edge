"""Unit tests for the read-only NBA starter/bench diagnostic."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "starter_bench_diagnostic.py"
SPEC = importlib.util.spec_from_file_location("starter_bench_diagnostic", SCRIPT)
DIAGNOSTIC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIAGNOSTIC)


def _archive(path: Path, snapshots: list[dict], results: list[dict]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE forecast_snapshots (
            snapshot_id TEXT PRIMARY KEY, sport TEXT, event_id TEXT, player_id TEXT,
            market TEXT, model_version TEXT, projected_value REAL,
            projection_at TEXT, archived_at TEXT
        );
        CREATE TABLE forecast_results (
            result_id TEXT PRIMARY KEY, snapshot_id TEXT, actual_stat REAL,
            game_status TEXT, supersedes_result_id TEXT
        );
    """)
    conn.executemany(
        "INSERT INTO forecast_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (s["snapshot_id"], s.get("sport", "nba"), s["event_id"], s["player_id"],
             s["market"], s.get("model_version", "v1"), s["projected_value"],
             s.get("projection_at", "2026-01-01T10:00:00Z"),
             s.get("archived_at", "2026-01-01T10:00:00Z"))
            for s in snapshots
        ],
    )
    conn.executemany(
        "INSERT INTO forecast_results VALUES (?,?,?,?,?)",
        [
            (r["result_id"], r["snapshot_id"], r["actual_stat"], r["game_status"],
             r.get("supersedes_result_id"))
            for r in results
        ],
    )
    conn.commit()
    conn.close()


def _games(root: Path, games: dict) -> None:
    nba = root / "nba"
    nba.mkdir(parents=True)
    (nba / "games.json").write_text(json.dumps({"games": games}), encoding="utf-8")


def _player(player_id, name, points, starter, *, dnp=False):
    return {
        "athlete_id": str(player_id), "player": name, "dnp": dnp,
        "starter": starter, "category": "",
        "stats": {} if dnp else {"points": str(points)},
    }


def test_analyze_groups_starters_and_bench_and_excludes_ineligible_rows(tmp_path):
    archive = tmp_path / "forecast.sqlite"
    snapshots = [
        {"snapshot_id": "s1", "event_id": "g1", "player_id": "1", "market": "PTS", "projected_value": 20},
        {"snapshot_id": "s2", "event_id": "g1", "player_id": "2", "market": "PTS", "projected_value": 10},
        {"snapshot_id": "s3", "event_id": "g2", "player_id": "3", "market": "PTS", "projected_value": 5},
        {"snapshot_id": "s4", "event_id": "g2", "player_id": "4", "market": "PTS", "projected_value": 8},
        {"snapshot_id": "s5", "event_id": "g2", "player_id": "5", "market": "PTS", "projected_value": 9},
        {"snapshot_id": "s6", "event_id": "g2", "player_id": "6", "market": "PTS", "projected_value": 9},
        {"snapshot_id": "s7", "event_id": "g2", "player_id": "7", "market": "PTS", "projected_value": 9},
    ]
    results = [
        {"result_id": "r1", "snapshot_id": "s1", "actual_stat": 24, "game_status": "final"},
        {"result_id": "r2", "snapshot_id": "s2", "actual_stat": 8, "game_status": "final"},
        {"result_id": "r3", "snapshot_id": "s3", "actual_stat": 7, "game_status": "final"},
        {"result_id": "r4", "snapshot_id": "s4", "actual_stat": 8, "game_status": "final"},
        {"result_id": "r5", "snapshot_id": "s5", "actual_stat": 9, "game_status": "final"},
        {"result_id": "r6", "snapshot_id": "s6", "actual_stat": 9, "game_status": "final"},
        # No result for s7: it is ungraded and must not be selected.
    ]
    _archive(archive, snapshots, results)
    _games(tmp_path / "historical", {
        "g1": {"status": "Final", "players": [
            _player("1", "Starter A", 24, True), _player("2", "Bench A", 8, False),
        ]},
        "g2": {"status": "Final", "players": [
            _player("3", "Starter B", 7, True),
            _player("4", "DNP", 0, False, dnp=True),
            _player("5", "Unknown Role", 9, None),
            # Player 6 intentionally absent from box score.
        ]},
    })

    report = DIAGNOSTIC.analyze(archive, tmp_path / "historical")
    by_key = {(row["market"], row["role"]): row for row in report["groups"]}
    assert by_key[("PTS", "starter")]["n_forecasts"] == 2
    assert by_key[("PTS", "starter")]["n_events"] == 2
    assert by_key[("PTS", "starter")]["mae"] == pytest.approx(3.0)
    assert by_key[("PTS", "starter")]["rmse"] == pytest.approx((10 ** 0.5))
    assert by_key[("PTS", "starter")]["mean_signed_error"] == pytest.approx(3.0)
    assert by_key[("PTS", "bench")]["n_forecasts"] == 1
    assert by_key[("PTS", "bench")]["n_events"] == 1
    assert by_key[("PTS", "bench")]["mean_signed_error"] == pytest.approx(-2.0)
    assert report["exclusions"]["dnp"] == 1
    assert report["exclusions"]["starter_tag_missing_or_invalid"] == 1
    assert report["exclusions"]["player_not_in_box"] == 1
    assert report["included_forecasts"] == 3


def test_archive_connection_is_read_only(tmp_path):
    archive = tmp_path / "forecast.sqlite"
    _archive(
        archive,
        [{"snapshot_id": "s1", "event_id": "g1", "player_id": "1",
          "market": "PTS", "projected_value": 20}],
        [{"result_id": "r1", "snapshot_id": "s1", "actual_stat": 21, "game_status": "final"}],
    )
    _games(tmp_path / "historical", {
        "g1": {"status": "Final", "players": [_player("1", "Starter", 21, True)]},
    })
    with pytest.raises(sqlite3.OperationalError):
        conn = sqlite3.connect(f"file:{archive}?mode=ro", uri=True)
        try:
            conn.execute("DELETE FROM forecast_snapshots")
        finally:
            conn.close()
    assert DIAGNOSTIC.analyze(archive, tmp_path / "historical")["included_forecasts"] == 1


def test_latest_active_result_and_latest_snapshot_are_used(tmp_path):
    archive = tmp_path / "forecast.sqlite"
    snapshots = [
        {"snapshot_id": "old", "event_id": "g1", "player_id": "1", "market": "PTS",
         "projected_value": 18, "projection_at": "2026-01-01T09:00:00Z"},
        {"snapshot_id": "new", "event_id": "g1", "player_id": "1", "market": "PTS",
         "projected_value": 20, "projection_at": "2026-01-01T10:00:00Z"},
    ]
    results = [
        {"result_id": "r-old", "snapshot_id": "old", "actual_stat": 21, "game_status": "final"},
        {"result_id": "r-new", "snapshot_id": "new", "actual_stat": 21, "game_status": "final"},
        {"result_id": "r-correction-old", "snapshot_id": "old", "actual_stat": 22,
         "game_status": "final", "supersedes_result_id": "r-old"},
    ]
    _archive(archive, snapshots, results)
    _games(tmp_path / "historical", {
        "g1": {"status": "Final", "players": [_player("1", "Starter", 21, True)]},
    })

    report = DIAGNOSTIC.analyze(archive, tmp_path / "historical")
    assert report["included_forecasts"] == 1
    assert report["groups"][0]["mae"] == pytest.approx(1.0)


def test_non_nba_forecasts_are_not_included(tmp_path):
    archive = tmp_path / "forecast.sqlite"
    _archive(
        archive,
        [{"snapshot_id": "s1", "sport": "nfl", "event_id": "g1", "player_id": "1",
          "market": "PASS_YDS", "projected_value": 200}],
        [{"result_id": "r1", "snapshot_id": "s1", "actual_stat": 220, "game_status": "final"}],
    )
    _games(tmp_path / "historical", {"g1": {"status": "Final", "players": []}})
    assert DIAGNOSTIC.analyze(archive, tmp_path / "historical")["included_forecasts"] == 0
