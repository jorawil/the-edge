"""Tests for historical ESPN player-stat feature coverage."""
import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_projection_features.py"
_SPEC = importlib.util.spec_from_file_location("audit_projection_features", _SCRIPT)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
audit_feature_coverage = _MODULE.audit_feature_coverage


def test_audit_counts_fields_by_category_and_excludes_dnp_and_nonregular(tmp_path):
    path = tmp_path / "nba" / "games.json"
    path.parent.mkdir(parents=True)
    games = {
        "regular": {"season_type_id": 2, "players": [
            {"category": "", "dnp": False, "stats": {"minutes": "32:10", "points": "20", "assists": "4"}},
            {"category": "", "dnp": False, "stats": {"minutes": "18:00", "points": "8", "assists": None}},
            {"category": "", "dnp": True, "stats": {"minutes": "0", "points": "0"}},
        ]},
        "preseason": {"season_type_id": 1, "players": [
            {"category": "", "dnp": False, "stats": {"points": "99", "usage": "40"}}]},
        "unknown": {"players": [
            {"category": "", "dnp": False, "stats": {"points": "100"}}]},
    }
    path.write_text(json.dumps({"games": games}), encoding="utf-8")
    result = audit_feature_coverage("nba", root=tmp_path)
    assert result["ok"] is True
    assert result["regular_season_games"] == 1
    assert result["played_player_rows"] == 2
    assert result["dnp_player_rows_excluded"] == 1
    fields = {row["field"]: row for row in result["categories"]["uncategorized"]["fields"]}
    assert fields["minutes"]["rows_present"] == 2
    assert fields["points"]["coverage_pct"] == 100.0
    assert fields["assists"]["rows_present"] == 1
    assert "usage" not in fields
    assert "athlete_id" not in json.dumps(result)


def test_audit_reports_missing_file_without_claiming_empty_data(tmp_path):
    result = audit_feature_coverage("nfl", root=tmp_path)
    assert result["ok"] is False
    assert result["reason"] == "unavailable"
