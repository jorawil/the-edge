"""Missing stats stay missing. Real zeros stay zeros. No game file means unavailable."""

import json
from pathlib import Path

from betting_model.data_store import actual_stat, inspect_store, load_store, player_dnp, player_log, recent_avg


def _write_games(root: Path) -> None:
    game = {
        "id": "game-1",
        "date_utc": "2026-01-02T00:00:00Z",
        "name": "Away at Home",
        "players": [
            {
                "player": "Complete Player",
                "athlete_id": "9",
                "team": "HOM",
                "category": "",
                "dnp": False,
                "stats": {"points": "10", "rebounds": "0", "assists": "2"},
            },
            {
                "player": "Missing Assists",
                "athlete_id": "8",
                "team": "HOM",
                "category": "",
                "dnp": False,
                "stats": {"points": "10", "rebounds": "5"},
            },
            {
                "player": "Sat Out",
                "athlete_id": "7",
                "team": "HOM",
                "category": "",
                "dnp": True,
                "stats": {},
            },
        ],
    }
    path = root / "nba" / "games.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"games": {"game-1": game}}), encoding="utf-8")


def test_missing_file_is_unavailable(tmp_path: Path):
    store = load_store("nba", tmp_path)
    assert store["ok"] is False
    assert store["reason"] == "unavailable"
    assert recent_avg("nba", "Nobody", root=tmp_path)["ok"] is False


def test_zero_is_kept_and_missing_is_not_filled(tmp_path: Path):
    _write_games(tmp_path)
    complete = player_log("nba", "Complete Player", tmp_path)[0]
    assert complete["REB"] == 0
    assert complete["PRA"] == 12
    assert complete["player_id"] == "9"
    missing = player_log("nba", "Missing Assists", tmp_path)[0]
    assert "AST" not in missing
    assert "PRA" not in missing
    assert player_dnp("nba", "Sat Out", tmp_path)[0]["dnp"] is True
    assert player_log("nba", "Sat Out", tmp_path) == []
    actual = actual_stat("nba", event_id="game-1", market="AST", player="Missing Assists", root=tmp_path)
    assert actual["ok"] is False
    assert actual["reason"] == "stat_missing"
    assert "actual" not in actual


def test_duplicate_rows_are_reported(tmp_path: Path):
    _write_games(tmp_path)
    path = tmp_path / "nba" / "games.json"
    store = json.loads(path.read_text(encoding="utf-8"))
    store["games"]["game-1"]["players"].append(dict(store["games"]["game-1"]["players"][0]))
    path.write_text(json.dumps(store), encoding="utf-8")
    report = inspect_store("nba", tmp_path)
    assert any(issue["issue"] == "duplicate player row" for issue in report["issues"])
