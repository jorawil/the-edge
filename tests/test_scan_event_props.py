import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scan_event_props.py"
_SPEC = importlib.util.spec_from_file_location("scan_event_props", _SCRIPT)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def _forecast(market, *, projected=20, projection_at=None):
    now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    return {
        "sport": "nfl", "event_id": "event-1", "event_start": (now + timedelta(hours=2)).isoformat(),
        "player_id": "123", "player_name": "Example Player", "team": "IND", "opponent": "HOU",
        "market": market, "projected_value": projected,
        "home_team_name": "Indianapolis Colts", "away_team_name": "Houston Texans",
        "model_version": "test-v1", "projection_at": (projection_at or now - timedelta(minutes=5)).isoformat(),
        "projection_input_at": (now - timedelta(minutes=10)).isoformat(),
    }


def test_build_projection_rows_selects_latest_and_maps_receptions():
    now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    older = _forecast("REC", projected=2.0, projection_at=now - timedelta(minutes=10))
    newer = _forecast("REC", projected=3.0, projection_at=now - timedelta(minutes=2))
    pass_yards = _forecast("PASS_YDS", projected=250.0)
    rows, counts = _MODULE.build_projection_rows(
        [older, newer, pass_yards],
        sport="nfl", event_id="event-1",
        markets=["player_receptions", "player_pass_yds"], now=now,
    )
    assert counts["selected"] == 2
    assert len(rows) == 4
    receptions = [row for row in rows if row["market"] == "RECEPTIONS"]
    assert {row["projected"] for row in receptions} == {3.0}
    assert {row["side"] for row in receptions} == {"Over", "Under"}


def test_build_projection_rows_refuses_stale_forecasts_before_api_call():
    now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    stale = _forecast("PASS_YDS", projection_at=now - timedelta(hours=7))
    with pytest.raises(ValueError, match="no fresh archived forecasts"):
        _MODULE.build_projection_rows(
            [stale], sport="nfl", event_id="event-1",
            markets=["player_pass_yds"], now=now,
        )


def test_build_projection_rows_rejects_started_events_and_unknown_markets():
    now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    started = _forecast("PASS_YDS")
    started["event_start"] = (now - timedelta(minutes=1)).isoformat()
    with pytest.raises(ValueError, match="no fresh archived forecasts"):
        _MODULE.build_projection_rows(
            [started], sport="nfl", event_id="event-1",
            markets=["player_pass_yds"], now=now,
        )
    with pytest.raises(ValueError, match="unsupported nfl Odds API markets"):
        _MODULE.build_projection_rows(
            [_forecast("PASS_YDS")], sport="nfl", event_id="event-1",
            markets=["player_tds"], now=now,
        )



def test_remap_price_event_ids_keeps_provider_id_for_audit():
    prices = [{"event_id": "odds-api-event-9", "player": "Example Player", "market": "player_pass_yds"}]
    mapped = _MODULE.remap_price_event_ids(prices, "espn-event-4")
    assert mapped[0]["event_id"] == "espn-event-4"
    assert mapped[0]["provider_event_id"] == "odds-api-event-9"
    assert prices[0]["event_id"] == "odds-api-event-9"



def test_verify_event_matchup_requires_exact_home_and_away_names():
    projections = [{
        "home_team_name": "Indianapolis Colts",
        "away_team_name": "Houston Texans",
    }]
    good = _MODULE.verify_event_matchup({
        "home_team": "Indianapolis Colts",
        "away_team": "Houston Texans",
    }, projections)
    assert good["ok"] is True
    bad = _MODULE.verify_event_matchup({
        "home_team": "Houston Texans",
        "away_team": "Indianapolis Colts",
    }, projections)
    assert bad["ok"] is False
    assert "do not exactly match" in bad["reason"]



def test_resolve_odds_event_id_requires_exact_match_and_start():
    projections = [{
        "home_team_name": "Indianapolis Colts",
        "away_team_name": "Houston Texans",
        "event_start": "2026-11-23T18:00:00+00:00",
    }]
    events = [
        {
            "id": "correct",
            "home_team": "Indianapolis Colts",
            "away_team": "Houston Texans",
            "commence_time": "2026-11-23T18:00:00Z",
        },
        {
            "id": "wrong-time",
            "home_team": "Indianapolis Colts",
            "away_team": "Houston Texans",
            "commence_time": "2026-11-23T20:00:00Z",
        },
        {
            "id": "wrong-teams",
            "home_team": "Houston Texans",
            "away_team": "Indianapolis Colts",
            "commence_time": "2026-11-23T18:00:00Z",
        },
    ]
    result = _MODULE.resolve_odds_event_id(events, projections)
    assert result["ok"] is True
    assert result["odds_event_id"] == "correct"


def test_resolve_odds_event_id_refuses_ambiguous_or_missing_events():
    projections = [{
        "home_team_name": "Indianapolis Colts",
        "away_team_name": "Houston Texans",
        "event_start": "2026-11-23T18:00:00+00:00",
    }]
    event = {
        "id": "duplicate",
        "home_team": "Indianapolis Colts",
        "away_team": "Houston Texans",
        "commence_time": "2026-11-23T18:00:00Z",
    }
    ambiguous = _MODULE.resolve_odds_event_id([event, {**event, "id": "duplicate-2"}], projections)
    assert ambiguous["ok"] is False
    missing = _MODULE.resolve_odds_event_id([], projections)
    assert missing["ok"] is False
