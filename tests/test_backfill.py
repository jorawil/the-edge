"""Backfill helpers do not turn a missing score into zero. No network call."""

import importlib.util
from pathlib import Path


def _load():
    script = Path(__file__).resolve().parents[1] / "scripts" / "backfill_espn.py"
    spec = importlib.util.spec_from_file_location("backfill_espn", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_score_is_not_zero():
    module = _load()
    assert module._score(None) is None
    assert module._score("") is None
    assert module._score(0) == 0
    assert module._score("0") == 0


def test_box_without_a_stat_does_not_invent_one():
    module = _load()
    players, _teams = module.parse_box(
        {
            "boxscore": {
                "players": [
                    {
                        "team": {"abbreviation": "HOM"},
                        "statistics": [
                            {
                                "name": "starters",
                                "keys": ["points", "rebounds"],
                                "athletes": [
                                    {
                                        "athlete": {"displayName": "Example", "id": "1"},
                                        "stats": ["10", "0"],
                                        "didNotPlay": False,
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "teams": [],
            }
        }
    )
    assert players[0]["stats"] == {"points": "10", "rebounds": "0"}
    assert "assists" not in players[0]["stats"]
