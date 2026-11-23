from datetime import datetime, timedelta, timezone

from betting_model import forecast_archive
from betting_model.forecast_validation import (
    _event_split,
    _evaluate,
    load_active_residual_rows,
    validate_residual_distributions,
)


def test_event_split_never_splits_one_event_across_slices():
    rows = []
    for day in range(10):
        for player in ("p1", "p2"):
            rows.append({
                "event_id": f"e{day}", "event_start": f"2026-01-{day+1:02d}T00:00:00Z",
                "residual": float(day), "player_id": player,
            })
    parts = _event_split(rows)
    event_sets = [{row["event_id"] for row in parts[name]}
                  for name in ("train", "validation", "test")]
    assert not (event_sets[0] & event_sets[1])
    assert not (event_sets[0] & event_sets[2])
    assert not (event_sets[1] & event_sets[2])
    assert sum(len(part) for part in parts.values()) == len(rows)


def test_empirical_and_normal_interval_diagnostics_are_reported():
    fit = [-3.0, -2.0, -1.0, 0.0, 0.0, 1.0, 2.0, 9.0]
    rows = [{"residual": value} for value in (-2.0, 0.0, 1.0, 8.0)]
    normal = _evaluate(fit, rows, "normal")
    empirical = _evaluate(fit, rows, "empirical")
    assert normal["80"]["n"] == 4
    assert empirical["90"]["target_coverage"] == 0.9
    assert empirical["80"]["mean_interval_width"] > 0


def test_archive_validation_uses_active_results_and_never_promotes(tmp_path, monkeypatch):
    path = tmp_path / "forecasts.sqlite"
    clock = {"now": datetime(2026, 1, 1, 12, tzinfo=timezone.utc)}
    monkeypatch.setattr(forecast_archive, "_now", lambda: clock["now"])

    for index in range(50):
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc) + timedelta(days=index)
        clock["now"] = now
        start = now + timedelta(hours=2)
        snapshot = {
            "sport": "nba", "event_id": f"event-{index}",
            "event_start": start.isoformat(), "player_id": "player-1",
            "player_name": "Fixture Player", "market": "PTS",
            "projected_value": 20.0, "n_history": 5, "n_opportunity": 5,
            "model_version": "fixture-v1", "projection_at": now.isoformat(),
            "projection_input_at": (now - timedelta(hours=1)).isoformat(),
            "projection_input_source": "fixture", "source": "unit test",
        }
        saved = forecast_archive.archive_forecast(snapshot, path)
        assert saved["ok"]
        clock["now"] = start + timedelta(hours=1)
        graded = forecast_archive.attach_forecast_result(
            saved["snapshot_id"], actual_stat=20.0 + ((index % 7) - 3),
            game_status="final", result_source="fixture",
            retrieved_at=clock["now"].isoformat(), path=path,
        )
        assert graded["ok"]

    loaded = load_active_residual_rows(path)
    assert len(loaded) == 50
    report = validate_residual_distributions(
        path, min_fit_rows=10, min_evaluation_rows=5,
    )
    assert report["scored_forecasts"] == 50
    group = report["groups"][0]
    assert group["n_train_events"] == 30
    assert group["n_validation_events"] == 10
    assert group["n_test_events"] == 10
    assert group["minimum_sample_gate_passed"] is True
    assert set(group["candidates"]) == {"normal", "empirical"}
    assert group["promotion_status"] == "NOT_PROMOTED"
    assert report["distribution_registry_changed"] is False
    assert report["qualified_promotions"] == 0


def test_empty_archive_returns_empty_non_promoting_report(tmp_path):
    report = validate_residual_distributions(tmp_path / "missing.sqlite")
    assert report["ok"] is True
    assert report["groups"] == []
    assert report["qualified_promotions"] == 0
