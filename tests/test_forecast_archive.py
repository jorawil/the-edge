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



def test_forecast_archive_adds_metadata_columns_to_existing_v1_database(tmp_path):
    import sqlite3

    path = tmp_path / "old-forecast.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE forecast_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO forecast_meta(key,value) VALUES('schema_version','1');
        CREATE TABLE forecast_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            observation_key TEXT NOT NULL UNIQUE,
            archived_at TEXT NOT NULL,
            sport TEXT NOT NULL,
            event_id TEXT NOT NULL,
            event_start TEXT NOT NULL,
            player_id TEXT NOT NULL,
            player_name TEXT,
            team TEXT,
            opponent TEXT,
            market TEXT NOT NULL,
            projected_value REAL NOT NULL,
            projected_opportunity REAL,
            projected_efficiency REAL,
            model_version TEXT NOT NULL,
            projection_at TEXT NOT NULL,
            projection_input_at TEXT,
            projection_input_source TEXT,
            source TEXT NOT NULL,
            warnings_json TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
    """)
    conn.close()

    with archive.connect(path) as migrated:
        columns = {row["name"] for row in migrated.execute("PRAGMA table_info(forecast_snapshots)")}
        version = migrated.execute(
            "SELECT value FROM forecast_meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert {"n_history", "n_opportunity", "effective_opportunity", "sigma",
            "distribution", "calibrated"} <= columns
    assert version == "2"



def test_archive_forecasts_batches_in_one_archive_and_detects_in_batch_duplicates(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    first = _snapshot(now)
    second = {**_snapshot(now), "event_id": "game-2"}
    batch = archive.archive_forecasts([first, first, second], tmp_path / "batch.sqlite")
    assert batch["ok"] is True
    assert batch["archived"] == 2
    assert batch["duplicates"] == 1
    assert batch["rejected"] == 0
    assert len(archive.pending_forecasts(tmp_path / "batch.sqlite")) == 2



def test_archive_stats_reports_durable_snapshot_and_result_counts(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    path = tmp_path / "stats.sqlite"
    missing = archive.archive_stats(path)
    assert missing["exists"] is False
    saved = archive.archive_forecast(_snapshot(now), path)
    assert saved["ok"]
    stats = archive.archive_stats(path)
    assert stats["exists"] is True
    assert stats["snapshots"] == 1
    assert stats["results"] == 0
    assert stats["size_bytes"] > 0



def test_forecast_metrics_use_latest_snapshot_per_player_event_market(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    clock = {"now": now}
    monkeypatch.setattr(archive, "_now", lambda: clock["now"])
    path = tmp_path / "latest-only.sqlite"

    first_snapshot = _snapshot(now)
    first_snapshot["event_start"] = (now + timedelta(hours=3)).isoformat()
    first = archive.archive_forecast(first_snapshot, path)
    assert first["ok"]

    clock["now"] = now + timedelta(minutes=30)
    latest_snapshot = _snapshot(clock["now"])
    latest_snapshot["event_start"] = (now + timedelta(hours=3)).isoformat()
    latest_snapshot["projected_value"] = 26.5
    latest_snapshot["projection_input_at"] = (clock["now"] - timedelta(minutes=5)).isoformat()
    latest = archive.archive_forecast(latest_snapshot, path)
    assert latest["ok"]

    clock["now"] = now + timedelta(hours=4)
    retrieved_at = clock["now"].isoformat()
    for saved in (first, latest):
        result = archive.attach_forecast_result(
            saved["snapshot_id"], actual_stat=30, game_status="final",
            result_source="ESPN", retrieved_at=retrieved_at, path=path,
        )
        assert result["ok"]

    report = archive.forecast_metrics(path)
    assert report["forecast_rows"] == 1
    assert report["groups"][0]["n_forecasts"] == 1
    assert report["groups"][0]["n_events"] == 1
    assert report["groups"][0]["mean_signed_error"] == 3.5
