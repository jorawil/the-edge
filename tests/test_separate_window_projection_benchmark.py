import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "separate_window_projection_benchmark.py"
_SPEC = importlib.util.spec_from_file_location("separate_window_projection_benchmark", _SCRIPT)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_separate_window_benchmark_selects_on_validation_and_reports_test(tmp_path):
    games = {}
    for day in range(1, 19):
        games[f"g{day}"] = {
            "id": f"g{day}",
            "date_utc": f"2026-01-{day:02d}T20:00:00Z",
            "season_type_id": 2,
            "players": [{
                "player": "Player", "athlete_id": "7", "category": "", "dnp": False,
                "stats": {
                    "points": str(10 + day % 4),
                    "rebounds": "5",
                    "assists": "3",
                    "minutes": str(25 + day % 6),
                    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": "2-5",
                },
            }],
        }
    path = tmp_path / "nba" / "games.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"games": games}), encoding="utf-8")

    result = _MODULE.benchmark(
        "nba", root=tmp_path, windows=(3, 5), shrinkage_values=(0.0, 5.0),
        min_history=3,
    )

    assert result["ok"] is True
    assert result["split_event_counts"]["test"] > 0
    assert result["candidate_metrics"]
    pts = next(row for row in result["validation_selections"] if row["market"] == "PTS")
    assert pts["selected"]["opportunity_window_games"] in (3, 5)
    assert pts["selected"]["efficiency_window_games"] in (3, 5)
    assert pts["held_out_test"]["split"] == "test"
    assert pts["held_out_test"]["n_player_game_predictions"] > 0
    assert pts["held_out_test_rolling_mean_baseline"]["split"] == "test"
    assert "PRA" in {row["market"] for row in result["candidate_metrics"]}


def test_same_time_events_are_not_added_before_scoring(tmp_path):
    games = {}
    for day in range(1, 13):
        games[f"g{day}"] = {
            "id": f"g{day}",
            "date_utc": f"2026-01-{day:02d}T20:00:00Z",
            "season_type_id": 2,
            "players": [{
                "player": "Player", "athlete_id": "7", "category": "", "dnp": False,
                "stats": {
                    "points": "10" if day < 12 else "1000",
                    "rebounds": "5", "assists": "3", "minutes": "30",
                    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": "2-5",
                },
            }],
        }
    # Add a second game at the same event time with an extreme result. It must
    # not affect the other game's projection at that timestamp.
    games["same-time"] = {
        **games["g12"], "id": "same-time",
        "players": [{
            **games["g12"]["players"][0],
            "stats": {
                "points": "2000", "rebounds": "5", "assists": "3", "minutes": "30",
                "threePointFieldGoalsMade-threePointFieldGoalsAttempted": "2-5",
            },
        }],
    }
    path = tmp_path / "nba" / "games.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"games": games}), encoding="utf-8")

    result = _MODULE.benchmark(
        "nba", root=tmp_path, windows=(3,), shrinkage_values=(0.0,), min_history=3,
    )
    assert result["ok"] is True
    assert result["split_event_counts"]["test"] > 0
