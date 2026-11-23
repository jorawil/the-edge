"""Regression tests for the read-only forecast lookup.

The lookup is the first connection between the archived ``opportunity-rate-v2``
forecasts and live evaluation. It must return the newest fresh snapshot for
one (event_id, player_id, market) triple, or a stable rejection reason — and
it must never write to the archive, never average projections, and never
enable probability output.
"""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from betting_model import forecast_archive as archive
from betting_model.forecast_lookup import REASONS, get_latest_forecast


def _snapshot(now, **overrides):
    snapshot = {
        "sport": "nba",
        "event_id": "game-1",
        "event_start": (now + timedelta(hours=2)).isoformat(),
        "player_id": "123",
        "player_name": "Example Player",
        "market": "PTS",
        "projected_value": 24.5,
        "projected_opportunity": 34.0,
        "projected_efficiency": 0.72,
        "n_history": 10,
        "n_opportunity": 10,
        "model_version": "opportunity-rate-v2:nba:PTS:o5:e20:k5",
        "projection_at": now.isoformat(),
        "projection_input_at": (now - timedelta(minutes=5)).isoformat(),
        "projection_input_source": "historical-data/games.json updated timestamp",
        "source": "THE EDGE projection engine",
        "warnings": ["uncalibrated"],
        "opportunity_window_games": 5,
        "efficiency_window_games": 20,
        "n_efficiency_history": 20,
    }
    snapshot.update(overrides)
    return snapshot


def _archive(path, now, **overrides):
    saved = archive.archive_forecast(_snapshot(now, **overrides), path)
    assert saved["ok"] is True, saved
    return saved


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_lookup_returns_newest_fresh_forecast_with_provenance(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    path = tmp_path / "forecast.sqlite"
    _archive(path, now, projected_value=24.5)
    monkeypatch.setattr(archive, "_now", lambda: now + timedelta(minutes=30))
    _archive(path, now + timedelta(minutes=30), projected_value=26.5)

    result = get_latest_forecast(
        path, event_id="game-1", player_id="123", market="pts",
        sport="nba", as_of=now + timedelta(minutes=45),
    )
    assert result["ok"] is True
    assert result["reason"] is None
    forecast = result["forecast"]
    assert forecast["projected_value"] == 26.5
    assert forecast["player_id"] == "123"
    assert forecast["event_id"] == "game-1"
    assert forecast["market"] == "PTS"
    assert forecast["model_version"] == "opportunity-rate-v2:nba:PTS:o5:e20:k5"
    assert forecast["snapshot_id"]
    assert forecast["projection_at"] == (now + timedelta(minutes=30)).isoformat()
    assert forecast["opportunity_window_games"] == 5
    assert forecast["efficiency_window_games"] == 20
    assert forecast["n_history"] == 10
    assert forecast["probability_permitted"] is False
    assert "uncalibrated" in forecast["probability_permitted_reason"]


def test_lookup_does_not_create_or_modify_the_archive(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    missing = tmp_path / "missing.sqlite"
    assert get_latest_forecast(
        missing, event_id="game-1", player_id="123", market="PTS", as_of=now,
    ) == {"ok": False, "forecast": None, "reason": "archive_not_found"}
    assert not missing.exists()

    path = tmp_path / "forecast.sqlite"
    _archive(path, now)
    before = _hash(path)
    result = get_latest_forecast(
        path, event_id="game-1", player_id="123", market="PTS", as_of=now,
    )
    assert result["ok"] is True
    assert _hash(path) == before
    assert archive.archive_stats(path)["snapshots"] == 1


def test_lookup_rejects_missing_identifiers_and_unknown_triples(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    path = tmp_path / "forecast.sqlite"
    _archive(path, now)

    assert get_latest_forecast(
        path, event_id="", player_id="123", market="PTS", as_of=now,
    )["reason"] == "missing_identifier"
    assert get_latest_forecast(
        path, event_id="game-1", player_id=" ", market="PTS", as_of=now,
    )["reason"] == "missing_identifier"
    assert get_latest_forecast(
        path, event_id="game-1", player_id="999", market="PTS", as_of=now,
    ) == {"ok": False, "forecast": None, "reason": "not_found"}
    assert get_latest_forecast(
        path, event_id="game-1", player_id="123", market="REB", as_of=now,
    )["reason"] == "not_found"
    assert get_latest_forecast(
        path, event_id="game-1", player_id="123", market="PTS",
        sport="nfl", as_of=now,
    )["reason"] == "not_found"


def test_lookup_refuses_started_events_and_stale_projections(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    path = tmp_path / "forecast.sqlite"
    _archive(path, now)

    # Event starts at now + 2h; looking up after it has started refuses.
    assert get_latest_forecast(
        path, event_id="game-1", player_id="123", market="PTS",
        as_of=now + timedelta(hours=2),
    ) == {"ok": False, "forecast": None, "reason": "event_started"}

    # Same snapshot, looked up 7 hours later but before a far-future event,
    # is stale rather than fresh. Archive it against a later event start.
    far_path = tmp_path / "far.sqlite"
    _archive(far_path, now, event_id="game-9",
             event_start=(now + timedelta(hours=10)).isoformat())
    assert get_latest_forecast(
        far_path, event_id="game-9", player_id="123", market="PTS",
        as_of=now + timedelta(hours=7),
    ) == {"ok": False, "forecast": None, "reason": "stale_projection"}


def test_lookup_skips_invalid_newest_snapshot_and_uses_older_fresh_one(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    path = tmp_path / "forecast.sqlite"
    # Archive a valid snapshot, then insert a second one whose projection
    # timestamp is exactly the event start by writing through the archive
    # at a later clock time is impossible, so use a direct row instead:
    # the lookup must refuse it even though the archive accepted the first.
    _archive(path, now)
    import sqlite3

    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT * FROM forecast_snapshots WHERE event_id='game-1'"
    ).fetchone()
    columns = [d[0] for d in conn.execute(
        "SELECT * FROM forecast_snapshots LIMIT 0").description]
    values = dict(zip(columns, row))
    values["snapshot_id"] = "late-snapshot"
    values["observation_key"] = "late-key"
    values["projection_at"] = (now + timedelta(hours=2)).isoformat()
    values["projected_value"] = 30.0
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO forecast_snapshots ({', '.join(columns)}) VALUES ({placeholders})",
        [values[c] for c in columns],
    )
    conn.commit()
    conn.close()

    result = get_latest_forecast(
        path, event_id="game-1", player_id="123", market="PTS",
        as_of=now + timedelta(minutes=30),
    )
    # The newest candidate is the at-kickoff projection, which is refused;
    # the older fresh snapshot is still usable, so the lookup returns it.
    assert result["ok"] is True
    assert result["forecast"]["projected_value"] == 24.5


def test_lookup_refuses_ambiguous_snapshots_at_the_newest_timestamp(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    path = tmp_path / "forecast.sqlite"
    _archive(path, now, projected_value=24.5)
    _archive(path, now, projected_value=27.5)

    result = get_latest_forecast(
        path, event_id="game-1", player_id="123", market="PTS", as_of=now,
    )
    assert result == {"ok": False, "forecast": None, "reason": "ambiguous_snapshots"}


def test_lookup_requires_timezone_aware_as_of(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    path = tmp_path / "forecast.sqlite"
    _archive(path, now)
    with pytest.raises(ValueError, match="as_of must include a timezone"):
        get_latest_forecast(
            path, event_id="game-1", player_id="123", market="PTS",
            as_of=datetime(2026, 11, 22, 12, 0),
        )


def test_lookup_reasons_are_the_documented_set():
    assert REASONS == {
        "missing_identifier", "archive_not_found", "archive_invalid",
        "not_found", "invalid_event_start", "invalid_projection_time",
        "event_started", "projection_not_before_event_start",
        "projection_in_future", "stale_projection", "ambiguous_snapshots",
    }


def test_lookup_reports_invalid_archive_instead_of_crashing(tmp_path):
    path = tmp_path / "not-a-database.sqlite"
    path.write_text("this is not sqlite", encoding="utf-8")
    result = get_latest_forecast(
        path, event_id="game-1", player_id="123", market="PTS",
        as_of=datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc),
    )
    assert result == {"ok": False, "forecast": None, "reason": "archive_invalid"}


def test_lookup_orders_by_utc_instant_not_stored_text(tmp_path, monkeypatch):
    """Lexical order of the stored text disagrees with chronological order.

    Snapshot A is written "12:00:00+05:00" (07:00 UTC) and snapshot B
    "09:00:00Z" (09:00 UTC). As text, "12:00..." sorts after "09:00...",
    so a text ORDER BY would wrongly pick A as newest. Chronologically
    B (09:00 UTC) is the newest snapshot.
    """
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    path = tmp_path / "forecast.sqlite"
    _archive(
        path, now, projected_value=24.5,
        projection_at="2026-11-22T12:00:00+05:00",
        projection_input_at="2026-11-22T11:55:00+05:00",
    )
    _archive(
        path, now, projected_value=26.5,
        projection_at="2026-11-22T09:00:00Z",
        projection_input_at="2026-11-22T08:55:00Z",
    )

    result = get_latest_forecast(
        path, event_id="game-1", player_id="123", market="PTS", as_of=now,
    )
    assert result["ok"] is True
    assert result["forecast"]["projected_value"] == 26.5
    assert result["forecast"]["projection_at"] == "2026-11-22T09:00:00Z"


def test_lookup_treats_same_instant_in_different_offsets_as_ambiguous(tmp_path, monkeypatch):
    """12:00Z and 07:00-05:00 are the same instant; two different values
    at that instant are ambiguous, even though the stored texts differ."""
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    path = tmp_path / "forecast.sqlite"
    _archive(
        path, now, projected_value=24.5,
        projection_at="2026-11-22T12:00:00Z",
        projection_input_at="2026-11-22T11:55:00Z",
    )
    _archive(
        path, now, projected_value=27.5,
        projection_at="2026-11-22T07:00:00-05:00",
        projection_input_at="2026-11-22T06:55:00-05:00",
    )

    result = get_latest_forecast(
        path, event_id="game-1", player_id="123", market="PTS", as_of=now,
    )
    assert result == {"ok": False, "forecast": None, "reason": "ambiguous_snapshots"}


def test_lookup_same_instant_same_value_in_different_offsets_is_not_ambiguous(tmp_path, monkeypatch):
    """The same instant and the same value, written with different
    offsets, is one forecast restated — not a conflict."""
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    path = tmp_path / "forecast.sqlite"
    _archive(
        path, now, projected_value=24.5,
        projection_at="2026-11-22T12:00:00Z",
        projection_input_at="2026-11-22T11:55:00Z",
    )
    _archive(
        path, now, projected_value=24.5,
        projection_at="2026-11-22T07:00:00-05:00",
        projection_input_at="2026-11-22T06:55:00-05:00",
    )

    result = get_latest_forecast(
        path, event_id="game-1", player_id="123", market="PTS", as_of=now,
    )
    assert result["ok"] is True
    assert result["forecast"]["projected_value"] == 24.5


def test_lookup_freshness_uses_utc_age_for_offset_timestamps(tmp_path, monkeypatch):
    """A projection written "13:00+02:00" is 11:00 UTC — one hour old at
    a 12:00 UTC lookup, not thirteen hours old and not in the future."""
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    path = tmp_path / "forecast.sqlite"
    _archive(
        path, now, projected_value=24.5,
        projection_at="2026-11-22T13:00:00+02:00",
        projection_input_at="2026-11-22T12:55:00+02:00",
    )

    result = get_latest_forecast(
        path, event_id="game-1", player_id="123", market="PTS", as_of=now,
    )
    assert result["ok"] is True
    assert result["forecast"]["projected_value"] == 24.5
