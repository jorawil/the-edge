import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_projection_engine.py"
_SPEC = importlib.util.spec_from_file_location("validate_projection_engine", _SCRIPT)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_projection_validation_selects_on_validation_and_reports_test_separately(tmp_path):
    games = {}
    for day in range(1, 13):
        games[f"g{day}"] = {
            "id": f"g{day}",
            "date_utc": f"2026-01-{day:02d}T20:00:00Z",
            "season_type_id": 2,
            "players": [{
                "player": "Player", "athlete_id": "7", "category": "", "dnp": False,
                "stats": {
                    "points": str(day * 10),
                    "rebounds": "5",
                    "assists": "3",
                    "minutes": "30:00",
                    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": "2-5",
                },
            }],
        }
    path = tmp_path / "nba" / "games.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"games": games}), encoding="utf-8")

    result = _MODULE.validate(
        "nba", root=tmp_path, windows=(3,), shrinkage_values=(0.0, 5.0),
        min_history=3,
    )

    assert result["ok"] is True
    pts = next(row for row in result["validation_selections"] if row["market"] == "PTS")
    assert pts["selected"] is not None
    assert pts["held_out_test"]["split"] == "test"
    assert pts["held_out_test"]["n"] > 0
    assert "PRA" not in {row["market"] for row in result["metrics"]}
    assert any("PRA is not included" in item for item in result["limitations"])
