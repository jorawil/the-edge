"""Backfill helpers do not turn a missing score into zero. No network call."""

import importlib.util
import json
from pathlib import Path


def _load():
    script = Path(__file__).resolve().parents[1] / "scripts" / "backfill_espn.py"
    spec = importlib.util.spec_from_file_location("backfill_espn", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rerun_repairs_an_incomplete_box_and_skips_a_complete_one(tmp_path, monkeypatch):
    module = _load()
    monkeypatch.setattr(module, "DATA", tmp_path)
    incomplete = {
        "id": "401",
        "date_utc": "2026-01-02T00:00:00Z",
        "name": "A at B",
        "source": "old",
        "scores": {"A": 0, "B": 1},
        "players": [
            {"player": "Example", "athlete_id": "1", "dnp": False, "stats": {}}
        ],
    }
    complete = {
        "id": "402",
        "date_utc": "2026-01-02T00:00:00Z",
        "name": "C at D",
        "source": "already complete",
        "scores": {"C": 2, "D": 2},
        "players": [
            {"player": "Keeper", "athlete_id": "2", "dnp": False, "stats": {"points": "4"}}
        ],
    }
    path = tmp_path / "nba" / "games.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"games": {"401": incomplete, "402": complete}}),
        encoding="utf-8",
    )
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if "scoreboard" in url:
            return {
                "events": [
                    _event("401", "A", "B"),
                    _event("402", "C", "D"),
                ]
            }
        return {
            "boxscore": {
                "players": [
                    {
                        "team": {"abbreviation": "B"},
                        "statistics": [
                            {
                                "name": "starters",
                                "keys": ["points"],
                                "athletes": [
                                    {
                                        "athlete": {"displayName": "Example", "id": "1"},
                                        "stats": ["18"],
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

    report = module.run(
        "nba",
        module.dt.date(2026, 1, 2),
        module.dt.date(2026, 1, 2),
        fetcher=fake_fetch,
    )
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["games"]["401"]["players"][0]["stats"]["points"] == "18"
    assert stored["games"]["402"]["source"] == "already complete"
    assert report["repaired"] == 1
    assert report["added"] == 0
    assert sum(1 for url in calls if "summary?event=402" in url) == 0
    assert sum(1 for url in calls if "summary?event=401" in url) == 1


def _event(event_id, away, home, season_type_id=2):
    season_type = None if season_type_id is None else {
        "id": str(season_type_id),
        "name": {1: "Preseason", 2: "Regular Season", 3: "Postseason"}[season_type_id],
    }
    event = {
        "id": event_id,
        "date": "2026-01-02T00:00:00Z",
        "name": f"{away} at {home}",
        "status": {"type": {"completed": True, "description": "Final"}},
        "week": {"number": 1},
        "competitions": [
            {
                "competitors": [
                    {"homeAway": "away", "score": "0", "team": {"abbreviation": away}},
                    {"homeAway": "home", "score": "1", "team": {"abbreviation": home}},
                ]
            }
        ],
    }
    if season_type is not None:
        event["season"] = {"year": 2026, "type": season_type}
    return event



def test_season_metadata_accepts_espn_scalar_and_object_shapes():
    module = _load()
    scalar = module._season_metadata({"season": {"year": 2026, "type": 2}})
    numeric_string = module._season_metadata({"season": {"year": 2026, "type": "2"}})
    object = module._season_metadata({
        "season": {"year": 2026, "type": {"id": "2", "name": "Regular Season"}}
    })
    assert scalar == {"season_year": 2026, "season_type_id": 2, "season_type_name": None}
    assert numeric_string == scalar
    assert object == {"season_year": 2026, "season_type_id": 2, "season_type_name": "Regular Season"}


def test_backfill_excludes_preseason_postseason_and_unknown_by_default(tmp_path, monkeypatch):
    module = _load()
    monkeypatch.setattr(module, "DATA", tmp_path)
    events = [
        _event("regular", "A", "B", 2),
        _event("preseason", "C", "D", 1),
        _event("postseason", "E", "F", 3),
        _event("unknown", "G", "H", None),
    ]
    summary_calls = []

    def fake_fetch(url):
        if "scoreboard" in url:
            return {"events": events}
        summary_calls.append(url)
        return {"boxscore": {"players": [{
            "team": {"abbreviation": "B"},
            "statistics": [{"name": "starters", "keys": ["points"], "athletes": [{
                "athlete": {"displayName": "Example", "id": "1"},
                "stats": ["10"], "didNotPlay": False,
            }]}],
        }], "teams": []}}

    report = module.run("nba", module.dt.date(2026, 1, 2), module.dt.date(2026, 1, 2),
                        fetcher=fake_fetch)
    stored = json.loads((tmp_path / "nba" / "games.json").read_text(encoding="utf-8"))
    assert set(stored["games"]) == {"regular"}
    assert stored["games"]["regular"]["season_type_id"] == 2
    assert len(summary_calls) == 1
    assert {item["id"] for item in report["skipped_season_type"]} == {
        "preseason", "postseason", "unknown"
    }


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


def test_invalid_score_values_are_missing_not_fatal():
    module = _load()
    assert module._score(None) is None
    assert module._score("") is None
    assert module._score("—") is None
    assert module._score("not-a-score") is None
    assert module._score(True) is None
    assert module._score(0) == 0
    assert module._score("17") == 17


def test_record_with_malformed_score_is_not_considered_complete():
    module = _load()
    record = {
        "date_utc": "2026-01-02T00:00:00Z",
        "source": "ESPN",
        "scores": {"AWY": "—", "HME": "17"},
        "players": [
            {"player": "Example", "dnp": False, "stats": {"points": "10"}}
        ],
    }
    assert not module.record_is_complete(record)
    record["scores"]["AWY"] = "0"
    assert module.record_is_complete(record)
