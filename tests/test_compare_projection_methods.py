"""Tests for the comparative historical projection benchmark."""
import json
from pathlib import Path

import importlib.util

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_projection_methods.py"
_SPEC = importlib.util.spec_from_file_location("compare_projection_methods", _SCRIPT)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
run_comparison = _MODULE.run_comparison


def _write(root: Path, years=None):
    games = {}
    for day, actual in enumerate((10, 12, 14, 16, 18, 30, 32, 34, 36, 38, 40, 42), 1):
        games[f"event-{day}"] = {
            "id": f"event-{day}",
            "date_utc": f"2026-01-{day:02d}T20:00:00Z",
            "season_type_id": 2,
            "season_year": (years or {}).get(day, 2026),
            "players": [{
                "player": "Test Player", "athlete_id": "42", "category": "",
                "dnp": False,
                "stats": {"points": str(actual), "rebounds": "4", "assists": "2"},
            }],
        }
    path = root / "nba" / "games.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"games": games}), encoding="utf-8")


def test_comparison_reports_all_methods_and_ranks_on_validation_only(tmp_path):
    _write(tmp_path)
    result = run_comparison("nba", root=tmp_path, windows=(3, 5, 10), min_history=3)

    assert result["ok"] is True
    assert set(result["methods"]) == {
        "rolling_mean", "season_to_date_mean", "recency_weighted_mean"
    }
    validation = [m for m in result["metrics"] if m["split"] == "validation"]
    assert {m["method"] for m in validation} == set(result["methods"])
    assert result["validation_rankings"]
    assert all(row["rank"] >= 1 for row in result["validation_rankings"])
    assert all(row["split"] == "test" for row in result["metrics"] if row["split"] == "test")



def test_recency_weighted_windows_use_comparable_fixed_samples(tmp_path):
    _write(tmp_path)
    result = run_comparison("nba", root=tmp_path, windows=(3, 5, 10), min_history=3)

    for split in ("train", "validation", "test"):
        for market in ("PTS", "REB", "AST"):
            for window in (3, 5, 10):
                rolling = next(
                    row for row in result["metrics"]
                    if row["split"] == split and row["market"] == market
                    and row["method"] == "rolling_mean" and row["window_games"] == window
                )
                weighted = next(
                    row for row in result["metrics"]
                    if row["split"] == split and row["market"] == market
                    and row["method"] == "recency_weighted_mean"
                    and row["window_games"] == window
                )
                assert weighted["n_player_game_predictions"] == rolling["n_player_game_predictions"]
                assert weighted["n_events"] == rolling["n_events"]


def test_season_to_date_history_resets_at_new_season(tmp_path):
    _write(tmp_path, {day: 2025 for day in range(1, 6)})
    result = run_comparison("nba", root=tmp_path, windows=(3,), min_history=3)

    # Day 6 is the first appearance in season_year 2026, so it cannot use
    # the prior season's four/five appearances for its season-to-date mean.
    season_rows = [
        row for row in result["metrics"]
        if row["method"] == "season_to_date_mean" and row["market"] == "PTS"
    ]
    assert season_rows
    assert all(row["n_player_game_predictions"] < 9 for row in season_rows)


def test_future_extreme_actual_does_not_change_earlier_validation_forecasts(tmp_path):
    _write(tmp_path)
    before = run_comparison("nba", root=tmp_path, windows=(3,), min_history=3)
    path = tmp_path / "nba" / "games.json"
    data = json.loads(path.read_text())
    data["games"]["event-12"]["players"][0]["stats"]["points"] = "9999"
    path.write_text(json.dumps(data))
    after = run_comparison("nba", root=tmp_path, windows=(3,), min_history=3)

    def validation_mae(result, method):
        return next(m["mae"] for m in result["metrics"]
                    if m["split"] == "validation" and m["market"] == "PTS"
                    and m["method"] == method)

    for method in ("rolling_mean", "season_to_date_mean", "recency_weighted_mean"):
        assert validation_mae(before, method) == validation_mae(after, method)
