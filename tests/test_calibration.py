"""Calibration uses archived pregame projections only.

The numbers below are test fixtures. They are not real players, prices, or results.
"""

from datetime import datetime, timedelta, timezone

from betting_model.backtest import run_backtest
from betting_model.calibration import (
    archive_projection,
    calibrate,
    chronological_split,
    score_snapshots,
)
from betting_model.core import SIGMA_DEFAULTS

START = datetime(2026, 1, 10, 0, 0, tzinfo=timezone.utc)


def _snap(index: int, *, projected: float = 20.0, actual_shift_hours: int = -2, **extra):
    start = START + timedelta(days=index)
    generated = start + timedelta(hours=actual_shift_hours)
    row = {
        "snapshot_id": f"snap-{index}",
        "player": "Fixture Player",
        "player_id": "fix-1",
        "event_id": f"event-{index}",
        "sport": "nba",
        "market": "PTS",
        "projected_value": projected,
        "line": 19.5,
        "odds": -110,
        "is_over": True,
        "generated_at": generated.isoformat(),
        "event_start": start.isoformat(),
        "source": "unit-test fixture, not a real projection",
        "model_version": "0.2.0",
    }
    row.update(extra)
    return row


def test_archive_rejects_late_projections_and_edits(tmp_path):
    path = tmp_path / "snapshots.json"
    late = _snap(1, actual_shift_hours=1)
    refused = archive_projection(late, path)
    assert refused["ok"] is False
    assert not path.exists()
    saved = archive_projection(_snap(1), path)
    assert saved["ok"] is True
    again = archive_projection(_snap(1, projected=99), path)
    assert again["ok"] is False
    assert "not changed" in again["reason"]
    stored = archive_projection(_snap(2), path)
    assert stored["count"] == 2


def test_empty_archive_does_not_invent_a_sigma():
    before = dict(SIGMA_DEFAULTS)
    result = calibrate([], {})
    assert result["ok"] is False
    assert result["reason"] == "no archived pregame projections"
    assert result["sigma_defaults_changed"] is False
    assert SIGMA_DEFAULTS == before


def test_missing_result_is_not_zero_and_defaults_stay():
    snapshots = [_snap(1), _snap(2, projected=30)]
    actuals = {
        ("event-1", "fix-1", "PTS"): 22.0,
        ("event-2", "fix-1", "PTS"): {"missing": True, "reason": "stat_missing"},
    }
    before = dict(SIGMA_DEFAULTS)
    scored = score_snapshots(snapshots, actuals)
    assert len(scored["scored"]) == 1
    assert scored["scored"][0]["actual"] == 22.0
    assert scored["scored"][0]["error"] == 2.0
    assert scored["missing_actuals"][0]["reason"] == "stat_missing"
    report = calibrate(snapshots, actuals)
    assert report["sigma_defaults_changed"] is False
    assert report["applied_sigma"] is None
    assert report["markets"][0]["residual_std"] is None
    assert report["overall"]["recent_average_baseline"] == "unavailable"
    assert SIGMA_DEFAULTS == before


def test_stored_baseline_is_used_and_a_future_leak_is_dropped():
    good = _snap(1, recent_average_at_archive=18.0)
    leaked = _snap(2, actual_shift_hours=3)
    actuals = {("event-1", "fix-1", "PTS"): 21.0, ("event-2", "fix-1", "PTS"): 50.0}
    scored = score_snapshots([good, leaked], actuals)
    assert len(scored["scored"]) == 1
    assert scored["scored"][0]["recent_average_error"] == 3.0
    assert scored["excluded_leakage"]
    report = calibrate([good, leaked], actuals)
    assert report["overall"]["recent_average_baseline"] == "stored on the snapshot"
    assert report["closing_line_baseline"] == "unavailable"


def test_split_orders_mixed_timezones_by_instant_not_text():
    # 01:00Z is earlier than 23:30 in US Eastern on the same calendar date,
    # even though the Eastern string starts with an earlier date.
    earlier = _snap(1)
    earlier["event_start"] = "2026-01-02T01:00:00+00:00"
    earlier["snapshot_id"] = "earlier-utc"
    later = _snap(2)
    later["event_start"] = "2026-01-01T23:30:00-05:00"
    later["snapshot_id"] = "later-eastern"
    parts = chronological_split([later, earlier], train_fraction=0.5, validation_fraction=0.0)
    assert [row["snapshot_id"] for row in parts["train"]] == ["earlier-utc"]
    assert [row["snapshot_id"] for row in parts["test"]] == ["later-eastern"]


def test_string_false_is_an_under_and_pushes_are_not_scored_as_losses():
    under = _snap(1, projected=10, is_over="false", line=20)
    push = _snap(2, projected=18, line=20)
    unsupported = _snap(3, projected=12, market="fantasy_points", sigma=3, line=10)
    actuals = {
        ("event-1", "fix-1", "PTS"): 12.0,
        ("event-2", "fix-1", "PTS"): 20.0,
        ("event-3", "fix-1", "fantasy_points"): 9.0,
    }
    scored = score_snapshots([under, push, unsupported], actuals)
    by_id = {row["snapshot_id"]: row for row in scored["scored"]}
    assert by_id["snap-1"]["outcome"] == 1
    assert by_id["snap-1"]["model_probability"] is not None
    assert by_id["snap-2"]["push"] is True
    assert by_id["snap-2"]["outcome"] is None
    assert by_id["snap-2"]["model_probability"] is None
    assert "push" in by_id["snap-2"]["probability_skip_reason"]
    assert by_id["snap-3"]["model_probability"] is None
    assert by_id["snap-3"]["outcome"] is None
    assert "legacy probability was not scored" in by_id["snap-3"]["probability_skip_reason"]
    report = calibrate([under, push, unsupported], actuals)
    # Only the decided Under is a probability outcome. The push and the
    # unsupported market stay in the projection sample and out of Brier.
    assert report["overall"]["sample_size"] == 3
    assert report["overall"]["probability_sample_size"] == 1
    assert report["overall"]["brier_score"] is not None


def test_split_keeps_the_latest_games_in_the_test_slice():
    rows = [_snap(index) for index in (3, 1, 2, 5, 4)]
    parts = chronological_split(rows)
    assert [row["snapshot_id"] for row in parts["train"]] == ["snap-1", "snap-2", "snap-3"]
    assert [row["snapshot_id"] for row in parts["validation"]] == ["snap-4"]
    assert [row["snapshot_id"] for row in parts["test"]] == ["snap-5"]


def test_backtest_counts_props_the_screen_would_have_rejected():
    liked = _snap(1, projected=40)
    disliked = _snap(2, projected=10)
    actuals = {("event-1", "fix-1", "PTS"): 30.0, ("event-2", "fix-1", "PTS"): 12.0}
    report = run_backtest([liked, disliked], actuals)
    assert report["ok"] is True
    assert report["result_type"] == "simulated_from_archived_inputs"
    assert report["profitability_claim"] is False
    assert report["actual_tracked_bets"] == "unavailable"
    assert report["closing_line_value"] == "unavailable"
    assert report["eligible_props"] == 2
    assert report["props_clearing_threshold"] == 1
    assert report["props_not_clearing_threshold"] == 1
    assert report["hit_rate_interval_all_decided"] is not None
    assert report["sigma_defaults_changed"] is False
