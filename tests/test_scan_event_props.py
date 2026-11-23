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



def test_point_projection_line_comparisons_are_descriptive_and_side_aware():
    projections = [
        {"player": "Example Player", "player_id": "123", "market": "RECEPTIONS",
         "projected": 3.0, "projection_at": "2026-11-23T11:55:00Z",
         "projection_source": "model-v2"},
    ]
    prices = [
        {"player": "Example Player", "market": "player_receptions", "side": "Over",
         "line": 2.5, "american_odds": -110, "bookmaker_key": "fanduel",
         "freshness": "fresh", "retrieved_at": "2026-11-23T12:00:00Z",
         "provider_last_update": "2026-11-23T11:59:00Z",
         "region_verified": False, "source_note": "not verified as FanDuel Indiana"},
        {"player": "Example Player", "market": "player_receptions", "side": "Under",
         "line": 2.5, "american_odds": -110, "bookmaker_key": "fanduel",
         "freshness": "fresh", "retrieved_at": "2026-11-23T12:00:00Z",
         "provider_last_update": "2026-11-23T11:59:00Z"},
        {"player": "Example Player", "market": "player_receptions", "side": "Over",
         "line": 2.5, "american_odds": 100, "bookmaker_key": "draftkings",
         "freshness": "stale", "retrieved_at": "2026-11-23T12:00:00Z"},
    ]
    result = _MODULE.build_point_projection_line_comparisons("nfl", projections, prices)
    assert result["counts"]["matched"] == 2
    assert result["counts"]["not_fresh_or_unsupported"] == 1
    over = next(row for row in result["comparisons"] if row["side"] == "Over")
    under = next(row for row in result["comparisons"] if row["side"] == "Under")
    assert over["projection_minus_line"] == pytest.approx(0.5)
    assert over["side_directional_gap"] == pytest.approx(0.5)
    assert over["region_verified"] is False
    assert over["region_note"] == "not verified as FanDuel Indiana"
    assert under["side_directional_gap"] == pytest.approx(-0.5)
    assert "does not represent a probability" in result["interpretation"]


def test_point_projection_line_comparisons_refuse_ambiguous_player_names():
    projections = [
        {"player": "John Smith", "player_id": "1", "market": "PASS_YDS", "projected": 200},
        {"player": "James Smith", "player_id": "2", "market": "PASS_YDS", "projected": 210},
    ]
    prices = [{
        "player": "J Smith", "market": "player_pass_yds", "side": "Over",
        "line": 199.5, "american_odds": -110, "bookmaker_key": "fanduel",
        "freshness": "fresh",
    }]
    result = _MODULE.build_point_projection_line_comparisons("nfl", projections, prices)
    assert result["comparisons"] == []
    assert result["counts"]["unmatched_player_or_market"] == 1



def test_point_projection_line_comparisons_skip_duplicate_book_offers():
    projections = [
        {"player": "Example Player", "player_id": "123", "market": "PASS_YDS", "projected": 200},
    ]
    price = {
        "player": "Example Player", "market": "player_pass_yds", "side": "Over",
        "line": 199.5, "american_odds": -110, "bookmaker_key": "fanduel",
        "freshness": "fresh", "duplicate": True,
    }
    result = _MODULE.build_point_projection_line_comparisons("nfl", projections, [price])
    assert result["comparisons"] == []
    assert result["counts"]["duplicate_prices_skipped"] == 1
    assert result["counts"]["matched"] == 0


def test_scan_cli_returns_nonzero_and_writes_report_when_scan_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(_MODULE, "scan_event", lambda **kwargs: {
        "ok": False, "reason": "no fresh forecasts", "odds_api_calls": 0,
    })
    out = tmp_path / "refused.json"
    code = _MODULE.main([
        "--sport", "nfl",
        "--event-id", "event-1",
        "--markets", "player_pass_yds",
        "--forecast-archive", str(tmp_path / "forecast.db"),
        "--odds-archive", str(tmp_path / "odds.db"),
        "--journal", str(tmp_path / "journal.db"),
        "--out", str(out),
    ])
    assert code == 2
    assert out.exists()
    import json
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["odds_api_calls"] == 0


def test_scan_cli_returns_zero_when_scan_succeeds(monkeypatch, tmp_path):
    monkeypatch.setattr(_MODULE, "scan_event", lambda **kwargs: {
        "ok": True, "comparisons": [], "odds_api_calls": 1,
    })
    code = _MODULE.main([
        "--sport", "nfl",
        "--event-id", "event-1",
        "--markets", "player_pass_yds",
        "--forecast-archive", str(tmp_path / "forecast.db"),
        "--odds-archive", str(tmp_path / "odds.db"),
        "--journal", str(tmp_path / "journal.db"),
    ])
    assert code == 0
