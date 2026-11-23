import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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
        "home_team_name": "Home City Team", "away_team_name": "Away City Team",
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

    index_calls = []
    original_index = _MODULE.build_history_index
    def tracked_index(*args, **kwargs):
        index_calls.append(kwargs["sport"])
        return original_index(*args, **kwargs)
    monkeypatch.setattr(_MODULE, "build_history_index", tracked_index)

    archive_path = tmp_path / "forecasts.sqlite"
    report = _MODULE.generate(
        "nba", data_root=tmp_path / "data", archive_path=archive_path, days=7,
    )

    assert report["ok"] is True
    assert report["forecasts_archived"] == 10
    assert report["forecast_rows"] == 10
    assert len(report["forecasts"]) == 10
    assert {row["market"] for row in report["forecasts"]} == {"PTS", "REB", "AST", "3PM", "PRA"}
    assert all(row["calibrated"] is False for row in report["forecasts"])
    assert all(row["projected_value"] is not None for row in report["forecasts"])
    assert all(row["home_team_name"] == "Home City Team" for row in report["forecasts"])
    assert all(row["away_team_name"] == "Away City Team" for row in report["forecasts"])
    assert all(row["warnings"] for row in report["forecasts"])
    assert index_calls == ["nba"]
    assert report["odds_api_calls"] == 0
    assert report["calibration_claimed"] is False
    assert report["archive_storage_format"].startswith("gzip-compressed")
    assert report["archive_summary"]["snapshots"] == 10
    assert report["archive_summary"]["results"] == 0
    assert report["archive_summary"]["size_bytes"] > 0
    assert report["history_index"]["eligible_events"] == 3
    assert report["history_index"]["player_market_histories"] == 5
    assert report["history_index"]["projection_input_at"] == (now - timedelta(minutes=5)).isoformat()
    saved = pending_forecasts(archive_path)
    assert len(saved) == 10
    assert {row["market"] for row in saved} == {"PTS", "REB", "AST", "3PM", "PRA"}
    assert all(row["projection_input_at"] == (now - timedelta(minutes=5)).isoformat()
               for row in saved)
    assert all(_MODULE._aware_time(row["projection_at"]) < event_start for row in saved)
    assert all(_MODULE._aware_time(row["projection_at"]) >= now for row in saved)



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



def test_fetch_json_does_not_retry_permanent_http_errors(monkeypatch):
    calls = []

    def fail(url_request, timeout):
        calls.append(url_request.full_url)
        raise _MODULE.urllib.error.HTTPError(
            url_request.full_url, 404, "Not Found", hdrs=None, fp=None,
        )

    monkeypatch.setattr(_MODULE.urllib.request, "urlopen", fail)
    with pytest.raises(RuntimeError, match="non-retryable HTTP 404"):
        _MODULE.fetch_json("https://example.invalid/roster")
    assert len(calls) == 1



def test_generate_does_not_archive_if_kickoff_passes_during_projection(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    event_start = now + timedelta(days=2)
    games = {}
    for index in range(3):
        when = now - timedelta(days=3 - index)
        games[f"prior-{index}"] = {
            "date_utc": when.isoformat(), "season_type_id": 2,
            "players": [{
                "athlete_id": "123", "player": "Example Player",
                "category": "", "dnp": False,
                "stats": {
                    "points": str(20 + index), "rebounds": "5",
                    "assists": "4", "minutes": "30:00",
                    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": "2-5",
                },
            }],
        }
    data_path = tmp_path / "data" / "nba" / "games.json"
    data_path.parent.mkdir(parents=True)
    data_path.write_text(json.dumps({
        "updated": (now - timedelta(minutes=5)).isoformat(), "games": games,
    }), encoding="utf-8")
    event = {
        "event_id": "racing-game", "event_start": event_start.isoformat(),
        "home_team_id": "100", "away_team_id": "200",
        "home_team": "HOM", "away_team": "AWY",
    }
    monkeypatch.setattr(_MODULE, "upcoming_events", lambda *args, **kwargs: ([event], []))
    monkeypatch.setattr(_MODULE, "team_roster", lambda sport, team_id: ([{
        "player_id": "123", "player_name": "Example Player",
        "position": "G", "roster_status": "Active",
    }] if team_id == "100" else []))
    calls = []
    def advancing_clock():
        calls.append(None)
        return now if len(calls) == 1 else event_start + timedelta(seconds=1)
    monkeypatch.setattr(_MODULE, "_utc_now", advancing_clock)

    archive_path = tmp_path / "forecasts.sqlite"
    report = _MODULE.generate(
        "nba", data_root=tmp_path / "data", archive_path=archive_path, days=1,
    )
    assert report["forecasts_archived"] == 0
    assert report["skipped"]["event_started_during_projection"] == 5
    assert report["archive_summary"]["snapshots"] == 0
