from datetime import datetime

import pytest

from betting_model.features import build_player_history
from betting_model.projection_engine import project_nba, project_nfl


def test_nba_projection_uses_prior_minutes_and_shrinks_efficiency():
    history = [
        {"actual": 20, "opportunity": 20},
        {"actual": 30, "opportunity": 30},
        {"actual": 40, "opportunity": 40},
    ]
    result = project_nba("PTS", history, {"prior_efficiency": 0.5}, shrinkage_strength=10)
    assert result["ok"] is True
    assert result["projected_opportunity"] == pytest.approx(30)
    assert result["projected_efficiency"] == pytest.approx(95 / 100)
    assert result["projected_value"] == pytest.approx(28.5)
    assert result["calibrated"] is False
    assert result["sigma"] is None
    assert result["interval_80"] is None


def test_nfl_receiving_projection_uses_targets_as_opportunity():
    history = [
        {"actual": 4, "opportunity": 5},
        {"actual": 6, "opportunity": 8},
        {"actual": 3, "opportunity": 4},
    ]
    result = project_nfl("REC", history, {"prior_efficiency": 0.6}, shrinkage_strength=5)
    assert result["ok"] is True
    assert result["projected_opportunity"] == pytest.approx(17 / 3)
    assert result["projected_efficiency"] == pytest.approx(16 / 22)
    assert result["projected_value"] == pytest.approx((17 / 3) * (16 / 22))


def test_engine_refuses_unsupported_markets_and_insufficient_history():
    pra = project_nba("PRA", [{"actual": 10, "opportunity": 20},
                              {"actual": 15, "opportunity": 30},
                              {"actual": 20, "opportunity": 40}], {})
    assert pra["ok"] is True
    assert pra["sigma"] is None
    assert any("PRA joint residual covariance" in warning for warning in pra["warnings"])
    with pytest.raises(ValueError):
        project_nba("NOT_A_MARKET", [], {})
    result = project_nfl("PASS_YDS", [{"actual": 100, "opportunity": 20}], {})
    assert result["ok"] is False
    assert result["reason"] == "insufficient_history"


def test_features_exclude_target_and_future_games_and_keep_dnp_out():
    games = {
        "prior": {
            "date_utc": "2026-01-01T20:00:00Z", "season_type_id": 2,
            "players": [{"athlete_id": "7", "category": "", "dnp": False,
                         "stats": {"points": "20", "minutes": "30:00"}}],
        },
        "same_time": {
            "date_utc": "2026-01-02T20:00:00Z", "season_type_id": 2,
            "players": [{"athlete_id": "7", "category": "", "dnp": False,
                         "stats": {"points": "99", "minutes": "40:00"}}],
        },
        "future": {
            "date_utc": "2026-01-03T20:00:00Z", "season_type_id": 2,
            "players": [{"athlete_id": "7", "category": "", "dnp": False,
                         "stats": {"points": "50", "minutes": "35:00"}}],
        },
        "dnp": {
            "date_utc": "2026-01-01T18:00:00Z", "season_type_id": 2,
            "players": [{"athlete_id": "7", "category": "", "dnp": True,
                         "stats": {"points": "0", "minutes": "0"}}],
        },
        "preseason": {
            "date_utc": "2025-12-31T20:00:00Z", "season_type_id": 1,
            "players": [{"athlete_id": "7", "category": "", "dnp": False,
                         "stats": {"points": "100", "minutes": "40:00"}}],
        },
    }
    result = build_player_history(
        games, sport="nba", player_id="7", market="PTS",
        before_event_time="2026-01-02T20:00:00Z",
    )
    assert result["n_history"] == 1
    assert result["history"][0]["actual"] == 20
    assert result["history"][0]["opportunity"] == 30
    assert result["projection_input_at"] is None
    with_input_time = build_player_history(
        games, sport="nba", player_id="7", market="PTS",
        before_event_time="2026-01-02T20:00:00Z",
        data_updated_at="2026-01-02T19:00:00Z",
        data_updated_source="games.json updated field",
    )
    assert with_input_time["projection_input_at"] == "2026-01-02T19:00:00Z"
    assert with_input_time["projection_input_source"] == "games.json updated field"


def test_feature_builder_requires_timezone_aware_cutoff():
    with pytest.raises(ValueError):
        build_player_history({}, sport="nba", player_id="1", market="PTS",
                             before_event_time=datetime(2026, 1, 1))


def test_feature_builder_rejects_data_update_after_target_event():
    with pytest.raises(ValueError, match="after the target event"):
        build_player_history({}, sport="nba", player_id="1", market="PTS",
                             before_event_time="2026-01-01T20:00:00Z",
                             data_updated_at="2026-01-02T20:00:00Z")
