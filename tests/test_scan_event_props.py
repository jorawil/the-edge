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
