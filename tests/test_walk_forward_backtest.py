"""Tests for chronological, leakage-safe historical baseline forecasts."""

import importlib.util
import json
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "walk_forward_backtest.py"
_SPEC = importlib.util.spec_from_file_location("walk_forward_backtest", _SCRIPT)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
run_backtest = _MODULE.run_backtest


def _game(event_id, day, actual, *, season_type_id=2, player_id="42"):
    return {
        "id": event_id,
        "date_utc": f"2026-01-{day:02d}T20:00:00Z",
        "season_type_id": season_type_id,
        "players": [{
            "player": "Test Player",
            "athlete_id": player_id,
            "category": "",
            "dnp": False,
            "stats": {"points": str(actual), "rebounds": "4", "assists": "2"},
        }],
    }


def _write(root: Path, games: dict):
    path = root / "nba" / "games.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"games": games}), encoding="utf-8")


def test_forecast_uses_only_prior_games_and_reports_chronological_splits(tmp_path):
    games = {
        f"event-{day}": _game(f"event-{day}", day, 999 if day == 12 else 10 + day)
        for day in range(1, 13)
    }
    _write(tmp_path, games)

    result = run_backtest("nba", root=tmp_path, windows=(3,), min_history=3)

    assert result["ok"] is True
    assert result["events_in_regular_season_data"] == 12
    assert result["split_event_counts"] == {"train": 7, "validation": 2, "test": 3}
    # On day 4, only days 1-3 are in the forecast history: (11+12+13)/3.
    first = next(row for row in result["metrics"]
                 if row["split"] == "train" and row["market"] == "PTS")
    assert first["n_player_game_predictions"] == 4
    assert first["n_events"] == 4
    # The extreme future outcome is in the held-out test period and must not
    # change the four earlier train forecasts (each has an error of exactly 2).
    assert first["mae"] == 2.0
    assert any("fewer than the 30-event review minimum" in warning
               for warning in result["evaluation_warnings"])


def test_preseason_and_unknown_season_records_are_excluded(tmp_path):
    games = {
        "regular-1": _game("regular-1", 1, 11),
        "regular-2": _game("regular-2", 2, 12),
        "regular-3": _game("regular-3", 3, 13),
        "regular-4": _game("regular-4", 4, 14),
        "preseason": _game("preseason", 5, 99, season_type_id=1),
        "unknown": {
            "id": "unknown",
            "date_utc": "2026-01-06T20:00:00Z",
            "players": _game("unknown", 6, 100)["players"],
        },
    }
    _write(tmp_path, games)

    result = run_backtest("nba", root=tmp_path, windows=(3,), min_history=3)

    assert result["events_in_regular_season_data"] == 4
    assert result["prediction_rows"] == 3  # PTS, REB, and AST each have one forecast
    assert result["exclusions"]["excluded_non_regular_or_unknown_season"] == 2


def test_missing_player_id_and_missing_stats_are_not_filled(tmp_path):
    games = {}
    for day in range(1, 5):
        game = _game(f"event-{day}", day, 10 + day)
        if day == 1:
            game["players"][0]["athlete_id"] = None
        if day == 2:
            del game["players"][0]["stats"]["points"]
        games[f"event-{day}"] = game
    _write(tmp_path, games)

    result = run_backtest("nba", root=tmp_path, windows=(3,), min_history=2)

    # Only days 3 and 4 have valid PTS observations; one prior value is not
    # enough for a two-game minimum history, so no PTS forecast is emitted.
    assert not any(row["market"] == "PTS" for row in result["metrics"])
    assert result["exclusions"]["excluded_missing_athlete_id"] >= 1


def test_nfl_market_mapping_uses_player_id_and_category(tmp_path):
    path = tmp_path / "nfl" / "games.json"
    path.parent.mkdir(parents=True)
    games = {}
    for day in range(1, 5):
        games[f"event-{day}"] = {
            "id": f"event-{day}",
            "date_utc": f"2026-09-{day:02d}T17:00:00Z",
            "season_type_id": 2,
            "players": [{
                "player": "Quarterback",
                "athlete_id": "99",
                "category": "passing",
                "dnp": False,
                "stats": {"passingYards": str(200 + day)},
            }],
        }
    path.write_text(json.dumps({"games": games}), encoding="utf-8")

    result = run_backtest("nfl", root=tmp_path, windows=(2,), min_history=2)

    assert result["player_market_actual_rows"] == 4
    assert any(row["market"] == "PASS_YDS" for row in result["metrics"])


def test_nba_three_point_made_attempted_stat_is_parsed_as_made_value(tmp_path):
    games = {}
    for day, made in enumerate((1, 2, 3, 4), start=1):
        game = _game(f"event-{day}", day, 10 + day)
        stats = game["players"][0]["stats"]
        # ESPN box scores commonly encode made/attempted as "made-attempted".
        stats["threePointFieldGoalsMade-threePointFieldGoalsAttempted"] = f"{made}-8"
        games[f"event-{day}"] = game
    _write(tmp_path, games)

    result = run_backtest("nba", root=tmp_path, windows=(2,), min_history=2)

    three_pm = [row for row in result["metrics"] if row["market"] == "3PM"]
    assert three_pm
    assert sum(row["n_player_game_predictions"] for row in three_pm) == 2

def test_espn_compact_display_labels_cover_nba_threes_and_nfl_yards(tmp_path):
    nba_games = {}
    for day, made in enumerate((1, 2, 3, 4), start=1):
        game = _game(f"event-{day}", day, 10 + day)
        game["players"][0]["stats"] = {
            "PTS": str(10 + day), "REB": "4", "AST": "2", "3PT": f"{made}-8"
        }
        nba_games[f"event-{day}"] = game
    nba_root = tmp_path / "nba_alias"
    _write(nba_root, nba_games)
    nba = run_backtest("nba", root=nba_root, windows=(2,), min_history=2)
    assert any(row["market"] == "3PM" for row in nba["metrics"])

    nfl_root = tmp_path / "nfl_alias"
    path = nfl_root / "nfl" / "games.json"
    path.parent.mkdir(parents=True)
    nfl_games = {}
    for day in range(1, 5):
        nfl_games[f"event-{day}"] = {
            "id": f"event-{day}",
            "date_utc": f"2026-09-{day:02d}T17:00:00Z",
            "season_type_id": 2,
            "players": [{
                "player": "Quarterback", "athlete_id": "99",
                "category": "passing", "dnp": False,
                "stats": {"YDS": str(200 + day)},
            }],
        }
    path.write_text(json.dumps({"games": nfl_games}), encoding="utf-8")
    nfl = run_backtest("nfl", root=nfl_root, windows=(2,), min_history=2)
    assert any(row["market"] == "PASS_YDS" for row in nfl["metrics"])

def test_simultaneous_events_are_scored_before_any_same_time_actual_is_added(tmp_path):
    games = {
        "event-1": _game("event-1", 1, 10),
        "event-2": _game("event-2", 2, 11),
        "event-3": _game("event-3", 3, 12),
        "event-4a": _game("event-4a", 4, 40),
        "event-4b": _game("event-4b", 4, 50),
        "event-5": _game("event-5", 5, 60),
    }
    _write(tmp_path, games)

    result = run_backtest("nba", root=tmp_path, windows=(3,), min_history=3)
    # Both same-time events stay together in validation and are forecast
    # from days 1-3 only: predictions are 11, errors are 29 and 39.
    pts = next(
        row for row in result["metrics"]
        if row["split"] == "validation" and row["market"] == "PTS"
    )
    assert pts["n_player_game_predictions"] == 2
    assert pts["mae"] == 34.0
    assert result["split_event_counts"] == {"train": 3, "validation": 2, "test": 1}
