"""Tests for lagged opportunity/rate projection benchmarks."""
import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "opportunity_projection_benchmark.py"
_SPEC = importlib.util.spec_from_file_location("opportunity_projection_benchmark", _SCRIPT)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
run_opportunity_benchmark = _MODULE.run_opportunity_benchmark


def _nba_game(event, day, points, minutes, threes="1-4"):
    return {
        "id": event, "date_utc": f"2026-01-{day:02d}T20:00:00Z",
        "season_type_id": 2,
        "players": [{
            "player": "Player", "athlete_id": "7", "category": "", "dnp": False,
            "stats": {"points": str(points), "rebounds": "4", "assists": "2",
                      "minutes": minutes,
                      "threePointFieldGoalsMade-threePointFieldGoalsAttempted": threes},
        }],
    }


def test_nba_opportunity_model_uses_prior_minutes_and_matches_baseline_samples(tmp_path):
    games = {
        "g1": _nba_game("g1", 1, 10, "20:00"),
        "g2": _nba_game("g2", 2, 30, "30:00"),
        "g3": _nba_game("g3", 3, 60, "60:00"),
        "g4": _nba_game("g4", 4, 25, "25:00"),
        "g5": _nba_game("g5", 5, 35, "35:00"),
    }
    path = tmp_path / "nba" / "games.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"games": games}), encoding="utf-8")

    result = run_opportunity_benchmark("nba", root=tmp_path, windows=(3,), min_history=3)

    assert result["ok"] is True
    assert result["opportunity_coverage"]["PTS"]["coverage_pct"] == 100.0
    validation = [m for m in result["metrics"] if m["split"] == "validation" and m["market"] == "PTS"]
    assert len(validation) == 2
    assert {m["n_player_game_predictions"] for m in validation} == {1}
    assert {m["method"] for m in validation} == {"opportunity_rate_product", "matched_rolling_mean"}
    # Historical rates are 0.5, 1.0, and 1.0; mean minutes is 36.6667.
    opportunity = next(m for m in validation if m["method"] == "opportunity_rate_product")
    assert round(opportunity["mae"], 3) == round(abs(25 - (110 / 3) * (5 / 6)), 3)

    # Expected minutes are evaluated separately from the final stat projection.
    opportunity_metrics = [
        row for row in result["opportunity_metrics"]
        if row["split"] == "validation" and row["market"] == "PTS"
    ]
    assert len(opportunity_metrics) == 1
    assert opportunity_metrics[0]["n_player_game_predictions"] == 1
    assert round(opportunity_metrics[0]["mae"], 3) == round(abs(25 - (110 / 3)), 3)
    selected = next(
        row for row in result["opportunity_validation_selections"]
        if row["market"] == "PTS"
    )
    assert selected["selected_window_games"] == 3
    assert selected["held_out_test"]["split"] == "test"


def test_nfl_features_are_joined_across_category_rows(tmp_path):
    games = {}
    for day in range(1, 6):
        games[f"g{day}"] = {
            "id": f"g{day}", "date_utc": f"2026-09-{day:02d}T17:00:00Z",
            "season_type_id": 2,
            "players": [
                {"player": "Player", "athlete_id": "99", "category": "passing",
                 "dnp": False, "stats": {"passingYards": str(100 * day),
                                            "completions/passingAttempts": "10/20"}},
                {"player": "Player", "athlete_id": "99", "category": "receiving",
                 "dnp": False, "stats": {"receivingYards": str(20 * day),
                                            "receptions": str(day),
                                            "receivingTargets": "5"}},
                {"player": "Player", "athlete_id": "99", "category": "rushing",
                 "dnp": False, "stats": {"rushingYards": str(10 * day),
                                            "rushingAttempts": "4"}},
            ],
        }
    path = tmp_path / "nfl" / "games.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"games": games}), encoding="utf-8")

    result = run_opportunity_benchmark("nfl", root=tmp_path, windows=(3,), min_history=3)

    assert result["ok"] is True
    assert result["opportunity_coverage"]["PASS_YDS"]["coverage_pct"] == 100.0
    assert result["opportunity_coverage"]["REC"]["coverage_pct"] == 100.0
    assert result["opportunity_coverage"]["RUSH_YDS"]["coverage_pct"] == 100.0
    assert any(m["market"] == "PASS_YDS" and m["method"] == "opportunity_rate_product"
               for m in result["metrics"])
    pass_attempts = next(
        row for row in result["opportunity_metrics"]
        if row["split"] == "validation" and row["market"] == "PASS_YDS"
    )
    assert pass_attempts["mae"] == 0.0


def test_missing_opportunity_is_not_filled_with_zero(tmp_path):
    games = {
        "g1": _nba_game("g1", 1, 10, "20:00"),
        "g2": _nba_game("g2", 2, 20, "bad"),
        "g3": _nba_game("g3", 3, 30, "30:00"),
        "g4": _nba_game("g4", 4, 40, "40:00"),
        "g5": _nba_game("g5", 5, 50, "50:00"),
    }
    path = tmp_path / "nba" / "games.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"games": games}), encoding="utf-8")

    result = run_opportunity_benchmark("nba", root=tmp_path, windows=(2,), min_history=2)

    assert result["opportunity_coverage"]["PTS"]["with_opportunity"] == 4
    assert result["opportunity_coverage"]["PTS"]["target_rows"] == 5
    # The invalid minute observation cannot become an invented zero-minute game.
    assert all(m["n_player_game_predictions"] <= 2 for m in result["metrics"]
               if m["market"] == "PTS" and m["split"] == "test")
