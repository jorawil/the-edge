"""Local game/box-score store (data/<sport>/games.json, filled by scripts/backfill_espn.py
from ESPN public API). Read-only helpers; never invent stats — missing => ok False."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SPORTS = ("nba", "nhl", "nfl", "cfb")

def _num(v: Any) -> float | None:
    try:
        return float(str(v).split("-")[0].split("/")[0].replace("+", ""))
    except (TypeError, ValueError):
        return None

def load_games(sport: str) -> list[dict[str, Any]]:
    p = DATA_DIR / sport / "games.json"
    if not p.exists():
        return []
    return sorted(json.loads(p.read_text())["games"].values(), key=lambda g: g["date_utc"])

MARKETS = {
    "nfl": {"PASS_YDS": ("passing", "passingYards"), "RUSH_YDS": ("rushing", "rushingYards"),
            "REC_YDS": ("receiving", "receivingYards"), "REC": ("receiving", "receptions")},
    "cfb": {"PASS_YDS": ("passing", "passingYards"), "RUSH_YDS": ("rushing", "rushingYards"),
            "REC_YDS": ("receiving", "receivingYards")},
    "nba": {"PTS": ("", "points"), "REB": ("", "rebounds"), "AST": ("", "assists"),
            "3PM": ("", "threePointFieldGoalsMade-threePointFieldGoalsAttempted")},
    "nhl": {"SOG": (None, "shotsTotal"), "G": (None, "goals"), "A": (None, "assists"),
            "SAVES": ("goalies", "saves")},
}

def player_log(sport: str, player: str) -> list[dict[str, Any]]:
    """Per-game rows {date, game, team, <market>: value} for a player, oldest first."""
    rows = []
    for g in load_games(sport):
        row: dict[str, Any] = {}
        for p in g["players"]:
            if p["player"].lower() != player.lower() or p.get("dnp"):
                continue
            row.setdefault("team", p["team"])
            for mk, (cat, key) in MARKETS[sport].items():
                if (cat is None or p["category"] == cat) and key in p["stats"]:
                    row[mk] = _num(p["stats"][key])
        if row:
            row.update(date=g["date_utc"][:10], game=g["name"], week=g.get("week"))
            if sport == "nba":
                row["PRA"] = sum(row.get(k) or 0 for k in ("PTS", "REB", "AST"))
                row["DD"] = int(sum(1 for k in ("PTS", "REB", "AST") if (row.get(k) or 0) >= 10) >= 2)
            if sport == "nhl" and "G" in row:
                row["PTS"] = (row.get("G") or 0) + (row.get("A") or 0)
            rows.append(row)
    return rows

def recent_avg(sport: str, player: str, n: int = 5) -> dict[str, Any]:
    log = player_log(sport, player)[-n:]
    if not log:
        return {"ok": False, "player": player, "error": "no games in local store"}
    keys = {k for r in log for k, v in r.items() if isinstance(v, (int, float)) and k != "week"}
    avg = {k: round(sum(r[k] for r in log if r.get(k) is not None) / max(1, sum(1 for r in log if r.get(k) is not None)), 2) for k in keys}
    return {"ok": True, "player": player, "games": len(log), "last": log[-1]["date"], **avg}
