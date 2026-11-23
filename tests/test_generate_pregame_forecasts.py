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
    second_event = {**event, "event_id": "future-game-2",
                    "event_start": (event_start + timedelta(days=1)).isoformat()}
    monkeypatch.setattr(_MODULE, "upcoming_events", lambda *args, **kwargs: ([event, second_event], []))
    monkeypatch.setattr(
        _MODULE, "team_roster",
        lambda sport, team_id: ([{
            "player_id": "123", "player_name": "Example Player",
            "position": "G", "roster_status": "Active",
        }] if team_id == "100" else []),
    )

    history_calls = []
    original_history = _MODULE.build_player_history
    def tracked_history(*args, **kwargs):
        history_calls.append((kwargs["player_id"], kwargs["market"]))
        return original_history(*args, **kwargs)
    monkeypatch.setattr(_MODULE, "build_player_history", tracked_history)

    archive_path = tmp_path / "forecasts.sqlite"
    report = _MODULE.generate(
        "nba", data_root=tmp_path / "data", archive_path=archive_path, days=7,
    )

    assert report["ok"] is True
    assert report["forecasts_archived"] == 10
    assert len(history_calls) == 5
    assert len(set(history_calls)) == 5
    assert report["odds_api_calls"] == 0
    assert report["calibration_claimed"] is False
    saved = pending_forecasts(archive_path)
    assert len(saved) == 10
    assert {row["market"] for row in saved} == {"PTS", "REB", "AST", "3PM", "PRA"}
    assert all(row["projection_input_at"] == (now - timedelta(minutes=5)).isoformat()
               for row in saved)



def test_roster_parser_handles_nested_position_groups():
    found = {}
    _MODULE._walk_roster({
        "athletes": [{
            "position": "Guard",
            "items": [{
                "id": "123", "fullName": "Example Player",
                "position": {"abbreviation": "G"},
            }],
        }],
    }, found)
    assert found["123"]["player_name"] == "Example Player"
    assert found["123"]["position"] == "G"


def test_upcoming_events_filters_non_regular_and_past_games(monkeypatch):
    from datetime import date

    future = datetime.now(timezone.utc) + timedelta(days=1)
    payload = {
        "events": [{
            "id": "future-regular",
            "date": future.isoformat(),
            "season": {"type": {"id": "2"}},
            "competitions": [{
                "competitors": [
                    {"homeAway": "home", "team": {"id": "100", "abbreviation": "HOM"}},
                    {"homeAway": "away", "team": {"id": "200", "abbreviation": "AWY"}},
                ],
            }],
        }, {
            "id": "future-postseason",
            "date": future.isoformat(),
            "season": {"type": 3},
            "competitions": [{
                "competitors": [
                    {"homeAway": "home", "team": {"id": "100", "abbreviation": "HOM"}},
                    {"homeAway": "away", "team": {"id": "200", "abbreviation": "AWY"}},
                ],
            }],
        }],
    }
    monkeypatch.setattr(_MODULE, "fetch_json", lambda url: payload)
    events, errors = _MODULE.upcoming_events("nba", start_day=date.today(), days=1)
    assert errors == []
    assert [event["event_id"] for event in events] == ["future-regular"]
