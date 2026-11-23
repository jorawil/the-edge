import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from betting_model.forecast_archive import pending_forecasts

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_pregame_forecasts.py"
_SPEC = importlib.util.spec_from_file_location("generate_pregame_forecasts", _SCRIPT)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_generate_creates_future_forecasts_from_roster_and_prior_games(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    event_start = now + timedelta(days=2)
    games = {}
    for index in range(3):
        when = now - timedelta(days=3 - index)
        games[f"prior-{index}"] = {
            "date_utc": when.isoformat(),
            "season_type_id": 2,
            "players": [{
                "athlete_id": "123", "player": "Example Player",
                "category": "", "dnp": False,
                "stats": {
                    "points": str(20 + index),
                    "rebounds": "5",
                    "assists": "4",
                    "minutes": "30:00",
                    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": "2-5",
                },
            }],
        }
    data_path = tmp_path / "data" / "nba" / "games.json"
    data_path.parent.mkdir(parents=True)
    data_path.write_text(json.dumps({
        "updated": (now - timedelta(minutes=5)).isoformat(),
        "games": games,
    }), encoding="utf-8")

    event = {
        "event_id": "future-game",
        "event_start": event_start.isoformat(),
        "home_team_id": "100", "away_team_id": "200",
        "home_team": "HOM", "away_team": "AWY",
    }
    monkeypatch.setattr(_MODULE, "upcoming_events", lambda *args, **kwargs: ([event], []))
    monkeypatch.setattr(
        _MODULE, "team_roster",
        lambda sport, team_id: ([{
            "player_id": "123", "player_name": "Example Player",
            "position": "G", "roster_status": "Active",
        }] if team_id == "100" else []),
    )

    archive_path = tmp_path / "forecasts.sqlite"
    report = _MODULE.generate(
        "nba", data_root=tmp_path / "data", archive_path=archive_path, days=7,
    )

    assert report["ok"] is True
    assert report["forecasts_archived"] == 5
    assert report["odds_api_calls"] == 0
    assert report["calibration_claimed"] is False
    saved = pending_forecasts(archive_path)
    assert len(saved) == 5
    assert {row["market"] for row in saved} == {"PTS", "REB", "AST", "3PM", "PRA"}
    assert all(row["projection_input_at"] == (now - timedelta(minutes=5)).isoformat()
               for row in saved)
