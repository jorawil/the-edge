from datetime import datetime, timedelta, timezone

import pytest

from betting_model import forecast_archive as archive


def _snapshot(now):
    return {
        "sport": "nba",
        "event_id": "game-1",
        "event_start": (now + timedelta(hours=2)).isoformat(),
        "player_id": "123",
        "player_name": "Example Player",
        "market": "PTS",
        "projected_value": 24.5,
        "projected_opportunity": 34.0,
        "projected_efficiency": 0.72,
        "n_history": 3,
        "n_opportunity": 3,
        "model_version": "opportunity-rate-v1",
        "projection_at": now.isoformat(),
        "projection_input_at": (now - timedelta(minutes=5)).isoformat(),
        "projection_input_source": "historical-data/games.json updated timestamp",
        "source": "THE EDGE projection engine",
        "warnings": ["uncalibrated"],
    }


def test_archive_forecast_is_append_only_and_rejects_duplicate(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    path = tmp_path / "forecast.sqlite"
    first = archive.archive_forecast(_snapshot(now), path)
    assert first["ok"] is True
    duplicate = archive.archive_forecast(_snapshot(now), path)
    assert duplicate["ok"] is False
    assert duplicate["reason"] == "duplicate forecast snapshot"

    with archive.connect(path) as conn:
        row = conn.execute("SELECT projected_value FROM forecast_snapshots").fetchone()
        with pytest.raises(Exception, match="forecast snapshots are immutable"):
            conn.execute(
                "UPDATE forecast_snapshots SET projected_value=1 WHERE snapshot_id=?",
                (first["snapshot_id"],),
            )


def test_archive_rejects_projection_input_after_projection(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    snapshot = _snapshot(now)
    snapshot["projection_input_at"] = (now + timedelta(minutes=1)).isoformat()
    result = archive.archive_forecast(snapshot, tmp_path / "forecast.sqlite")
    assert result["ok"] is False
    assert "input timestamp is after" in result["reason"]


def test_forecast_results_are_appended_and_corrections_supersede(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    clock = {"now": now}
    monkeypatch.setattr(archive, "_now", lambda: clock["now"])
    path = tmp_path / "forecast.sqlite"
    saved = archive.archive_forecast(_snapshot(now), path)
    assert saved["ok"] is True

    clock["now"] = now + timedelta(hours=3)
    retrieved = clock["now"].isoformat()
    first = archive.attach_forecast_result(
        saved["snapshot_id"], actual_stat=30, game_status="final",
        result_source="ESPN", retrieved_at=retrieved, path=path,
    )
    assert first["ok"] is True

    conflict = archive.attach_forecast_result(
        saved["snapshot_id"], actual_stat=29, game_status="final",
        result_source="ESPN correction", retrieved_at=retrieved, path=path,
    )
    assert conflict["ok"] is False

    corrected = archive.attach_forecast_result(
        saved["snapshot_id"], actual_stat=29, game_status="final",
        result_source="ESPN corrected box", retrieved_at=retrieved, path=path,
        supersedes_result_id=first["result_id"],
    )
    assert corrected["ok"] is True

    report = archive.forecast_metrics(path)
    assert report["forecast_rows"] == 1
    assert report["groups"][0]["n_forecasts"] == 1
    assert report["groups"][0]["mean_signed_error"] == pytest.approx(4.5)


def test_forecast_archive_rejects_postgame_fields_and_future_results(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    snapshot = _snapshot(now)
    snapshot["actual"] = 25
    refused = archive.archive_forecast(snapshot, tmp_path / "forecast.sqlite")
    assert refused["ok"] is False
    assert refused["reason"] == "postgame fields are not allowed"

    clean = _snapshot(now)
    saved = archive.archive_forecast(clean, tmp_path / "forecast.sqlite")
    assert saved["ok"] is True
    future_time = now + timedelta(days=1)
    result = archive.attach_forecast_result(
        saved["snapshot_id"], actual_stat=25, game_status="final",
        result_source="ESPN", retrieved_at=future_time.isoformat(),
        path=tmp_path / "forecast.sqlite",
    )
    assert result["ok"] is False
    assert "future" in result["reason"]
