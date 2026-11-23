import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from betting_model import forecast_archive as archive

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "grade_forecast_archive.py"
_SPEC = importlib.util.spec_from_file_location("grade_forecast_archive", _SCRIPT)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def _snapshot(now, event_id="game-1", player_id="123"):
    return {
        "sport": "nba", "event_id": event_id,
        "event_start": (now + timedelta(hours=1)).isoformat(),
        "player_id": player_id, "player_name": "Example Player",
        "market": "PTS", "projected_value": 24.5,
        "projected_opportunity": 34.0, "projected_efficiency": 0.72,
        "n_history": 10, "n_opportunity": 10,
        "model_version": "opportunity-rate-v1",
        "projection_at": now.isoformat(),
        "projection_input_at": (now - timedelta(minutes=5)).isoformat(),
        "source": "THE EDGE projection engine",
        "warnings": [],
    }


def _game(now, player, *, status="Final"):
    event_start = now + timedelta(hours=1)
    return {
        "date_utc": event_start.isoformat(),
        "status": status,
        "retrieved_at": (event_start + timedelta(hours=2)).isoformat(),
        "source": "ESPN box score",
        "players": [player],
    }


def test_grade_archive_attaches_final_actual_and_dnp_void(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    clock = {"now": now}
    monkeypatch.setattr(archive, "_now", lambda: clock["now"])
    archive_path = tmp_path / "forecast.sqlite"

    final = archive.archive_forecast(_snapshot(now), archive_path)
    dnp = archive.archive_forecast(_snapshot(now, "game-2", "456"), archive_path)
    assert final["ok"] and dnp["ok"]

    games = {
        "game-1": _game(now, {
            "athlete_id": "123", "category": "", "dnp": False,
            "stats": {"points": "28", "minutes": "34:00"},
        }),
        "game-2": _game(now, {
            "athlete_id": "456", "category": "", "dnp": True,
            "stats": {"points": "0", "minutes": "0"},
        }),
    }
    data_path = tmp_path / "nba" / "games.json"
    data_path.parent.mkdir()
    data_path.write_text(json.dumps({"games": games}), encoding="utf-8")
    clock["now"] = now + timedelta(hours=4)

    report = _MODULE.grade(archive_path, tmp_path)
    assert report["graded"] == 1
    assert report["void_dnp"] == 1
    assert report["metrics"]["groups"][0]["n_forecasts"] == 1
    assert report["metrics"]["groups"][0]["mean_signed_error"] == 3.5
    assert report["archive_summary"]["snapshots"] == 2
    assert report["archive_summary"]["results"] == 2


def test_grade_archive_does_not_turn_missing_stat_into_zero(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    archive_path = tmp_path / "forecast.sqlite"
    saved = archive.archive_forecast(_snapshot(now), archive_path)
    assert saved["ok"]

    games = {"game-1": _game(now, {
        "athlete_id": "123", "category": "", "dnp": False, "stats": {},
    })}
    data_path = tmp_path / "nba" / "games.json"
    data_path.parent.mkdir()
    data_path.write_text(json.dumps({"games": games}), encoding="utf-8")
    monkeypatch.setattr(archive, "_now", lambda: now + timedelta(hours=4))

    report = _MODULE.grade(archive_path, tmp_path)
    assert report["graded"] == 0
    assert report["unresolved"]["missing_actual_stat"] == 1
    assert len(archive.pending_forecasts(archive_path)) == 1
    assert report["archive_summary"]["snapshots"] == 1
    assert report["archive_summary"]["results"] == 0


def test_grade_archive_skips_forecasts_for_future_events(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(archive, "_now", lambda: now)
    archive_path = tmp_path / "forecast.sqlite"
    saved = archive.archive_forecast(_snapshot(now), archive_path)
    assert saved["ok"]

    report = _MODULE.grade(archive_path, tmp_path)
    assert report["checked_pending"] == 0
    assert report["future_pending_not_checked"] == 1
    assert report["total_pending"] == 1
    assert report["unresolved"] == {}
    assert report["archive_summary"]["results"] == 0
